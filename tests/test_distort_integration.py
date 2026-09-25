"""Distort canvas interaction, stage processing, baking and asset integration."""
import uuid

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter, QTransform

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BlenderComicViewSourceDescriptor, BoundGeometry,
    ChapterDocument, DistortModifier, ImageObject, OutlineModifier, ParameterMaskBinding, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, apply_modifier_stack


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=160)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 160, 160))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def png_bytes(image):
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(data)


def owner(scene, kind="raster"):
    canvas, chapter, page = scene
    if kind == "raster":
        obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 100)))
        canvas.tiles.paint_dab(obj.object_id, QPointF(30, 35), 14, QColor("red"), square=True, antialias=False)
    else:
        source = BlenderComicViewSourceDescriptor(project_uuid=uuid.uuid4().hex, view_uuid=uuid.uuid4().hex) if kind == "blender" else None
        obj = ImageObject(pixel_width=100, pixel_height=100, x=0, y=0)
        if source:
            obj.source = source
            obj.sync_source_metadata()
        chapter.add_object(page.layer_id, obj)
        image = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        painter.fillRect(23, 28, 14, 14, QColor("red"))
        painter.end()
        canvas.images.put(obj.object_id, "frame.png", png_bytes(image), "image/png")
    canvas.set_selection("object", obj.object_id)
    return obj


def attach(scene, obj, kind="distort_affine", **parameters):
    canvas, chapter, _ = scene
    modifier = DistortModifier(modifier_type=kind, frame=(0, 0, 100, 100), center=(50, 50), radius=35,
                               parameters=parameters)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    canvas.modifier_mode = True
    canvas.active_modifier_id = modifier.modifier_id
    return modifier


@pytest.mark.parametrize("kind", ["raster", "image", "blender"])
def test_each_target_renders_distortion_and_mute(scene, kind):
    canvas, _, _ = scene
    obj = owner(scene, kind)
    original = render(canvas)
    modifier = attach(scene, obj, offset_x=20., interpolation="nearest")
    result = render(canvas)
    assert result.pixelColor(50, 35) == QColor("red")
    assert result.pixelColor(30, 35).alpha() == 0
    modifier.muted = True
    assert np.array_equal(_qimage_premultiplied(render(canvas)), _qimage_premultiplied(original))


def test_raster_apply_and_undo_preserve_chained_preview(scene):
    canvas, chapter, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, offset_x=20., interpolation="nearest")
    outline = OutlineModifier(thickness=2)
    chapter.add_modifier(outline, [("object", obj.object_id)])
    before = _qimage_premultiplied(render(canvas))
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert obj.modifier_source_frame is not None
    assert obj.modifier_ids == [outline.modifier_id]
    np.testing.assert_allclose(_qimage_premultiplied(render(canvas)), before, atol=1/255)
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [modifier.modifier_id, outline.modifier_id]
    np.testing.assert_allclose(_qimage_premultiplied(render(canvas)), before, atol=1/255)


@pytest.mark.parametrize("kind,handle", [("distort_twirl", "center"), ("distort_twirl", "radius"),
    ("distort_perspective", 0), ("distort_mesh_warp", 5), ("distort_mirror", "input_angle")])
def test_gizmo_drag_is_undoable_and_escape_restores(scene, kind, handle):
    canvas, chapter, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, kind)
    before = modifier.to_dict()
    point = dict(canvas._distort_handle_points(modifier))[handle]
    assert canvas._begin_modifier_handle(point)
    assert canvas._move_modifier_handle(point + QPointF(17, 11))
    assert modifier.to_dict() != before
    assert canvas._finish_modifier_handle()
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before
    modifier = canvas.chapter.modifiers[modifier.modifier_id]
    canvas.active_modifier_id = modifier.modifier_id
    point = dict(canvas._distort_handle_points(modifier))[handle]
    assert canvas._begin_modifier_handle(point)
    canvas._move_modifier_handle(point + QPointF(20, 5))
    canvas.keyPressEvent(QKeyEvent(QKeyEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before


def test_deform_add_move_remove_pin_and_source_edit(scene):
    canvas, chapter, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, "distort_deform")
    point = canvas.document_to_widget(QPointF(25, 30))
    assert canvas._begin_modifier_handle(point)
    canvas._move_modifier_handle(point + QPointF(10, 0))
    canvas._finish_modifier_handle()
    assert len(modifier.points) == len(modifier.source_points) == 1
    assert modifier.points != modifier.source_points
    canvas.distort_edit_source = True
    assert canvas._begin_modifier_handle(point)
    canvas._move_modifier_handle(point + QPointF(0, 10))
    canvas._finish_modifier_handle()
    assert modifier.source_points[0][1] > .3
    canvas.distort_pin_mode = "remove"
    assert canvas._begin_modifier_handle(dict(canvas._distort_handle_points(modifier))[0])
    assert not modifier.points
    canvas.command_stack.undo()
    assert len(canvas.chapter.modifiers[modifier.modifier_id].points) == 1


def test_handle_painting_all_rigs(scene):
    canvas, chapter, _ = scene
    obj = owner(scene)
    image = QImage(400, 400, QImage.Format_ARGB32_Premultiplied)
    for kind in ("distort_twirl", "distort_perspective", "distort_mesh_warp", "distort_mirror", "distort_deform"):
        modifier = attach(scene, obj, kind)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        try:
            assert canvas._draw_distort_handles(painter)
        finally:
            painter.end()
        chapter.remove_modifier(modifier.modifier_id)


def test_intensity_masks_align_with_expanded_output_and_direct_api(scene, monkeypatch):
    canvas, _, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, offset_x=20., interpolation="nearest")
    source = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)
    painter = QPainter(source); painter.fillRect(23, 28, 14, 14, QColor("red")); painter.end()
    modifier.intensity = 50
    result, bounds = render_stages(canvas, source, QRectF(0, 0, 100, 100), [modifier], QTransform())
    assert result.pixelColor(30-int(bounds.x()),35-int(bounds.y())).alpha() in {127,128}
    assert result.pixelColor(50-int(bounds.x()),35-int(bounds.y())).alpha() in {127,128}
    direct = apply_modifier_stack(source, [modifier], (0, 0))
    assert direct.pixelColor(50,35).alpha() in {127,128}
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("test-mask", 0, 100)
    def fields(modifiers, width, height, mapping, world_bounds):
        # Keep the source red square on the left, and display the warped square
        # on the right. Coordinates belong to the expanded output, not input.
        mask = np.broadcast_to((np.arange(width) + world_bounds.x() >= 40).astype(float), (height,width))
        return {(modifier.modifier_id, "intensity"): mask}
    monkeypatch.setattr(canvas, "_modifier_mask_fields", fields)
    result, bounds = render_stages(canvas, source, QRectF(0,0,100,100), [modifier], QTransform())
    assert result.pixelColor(30-int(bounds.x()),35-int(bounds.y())) == QColor("red")
    assert result.pixelColor(50-int(bounds.x()),35-int(bounds.y())) == QColor("red")


def test_async_stage_retains_full_quality_and_copies_mutable_parameters(scene, monkeypatch):
    canvas, _, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, "distort_twirl", angle=60.)
    source = QImage(200, 200, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    pending = []
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *args, **kwargs: pending.append(args) or True)
    canvas._interactive_render = True
    draft, bounds = render_stages(canvas, source, QRectF(0, 0, 200, 200), [modifier], QTransform(), request_scope=("object", obj.object_id))
    assert pending and draft.size() == source.size()
    compute = pending[-1][2]
    exact = compute()
    modifier.parameters["angle"] = -180
    np.testing.assert_array_equal(_qimage_premultiplied(compute()), _qimage_premultiplied(exact))
    assert compute(lambda: True) is None


def test_gizmo_frame_and_points_follow_sole_target_transform(scene):
    canvas, _, _ = scene
    obj = owner(scene)
    modifier = attach(scene, obj, "distort_perspective")
    transform = QTransform.fromTranslate(20, 10)
    canvas._transform_single_target_focal_modifiers("object", obj.object_id, transform)
    assert modifier.frame == (20.,10.,100.,100.)
    assert modifier.center == (70.,60.)
    assert modifier.points[0] == (0.,0.)


def test_asset_extraction_and_placement_translate_distortion_rig(scene):
    canvas, chapter, page = scene
    obj = owner(scene)
    modifier = attach(scene, obj, "distort_twirl", angle=45.)
    manifest, asset_tiles = extract_asset(chapter, canvas.tiles, "object", obj.object_id, "Warped")
    asset = manifest.document
    clone = next(iter(asset.modifiers.values()))
    delta = np.asarray(clone.center) - modifier.center
    np.testing.assert_allclose(np.asarray(clone.frame[:2]) - modifier.frame[:2], delta)
    kind, identifier, _ = instantiate_asset(manifest, asset_tiles, chapter, canvas.tiles, page.layer_id, 100, 100)
    placed = chapter.modifiers[chapter.modifier_target(kind, identifier).modifier_ids[0]]
    delta = np.asarray(placed.center) - clone.center
    np.testing.assert_allclose(np.asarray(placed.frame[:2]) - clone.frame[:2], delta)
