"""Linked Comic View targets use published image pixels without Blender running."""
from dataclasses import replace
import uuid

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BlenderComicViewSourceDescriptor, BoundGeometry, ChapterDocument,
    HalftoneModifier, ImageObject, RasterObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.integrations.blender_controller import BlenderImageSourceController
from comic_editor.integrations.blender_source import ComicViewInfo
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.halftone_source import render_color_source, source_signature
from comic_editor.ui.modifier_rendering import _qimage_premultiplied
from comic_editor.ui.pattern_rendering import apply_pattern_effect


PROJECT_UUID = uuid.UUID(int=8401).hex
VIEW_UUID = uuid.UUID(int=8402).hex


def image(color, width=32, height=24):
    result = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor(color))
    return result


def png_bytes(frame):
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly)
    assert frame.save(buffer, "PNG")
    buffer.close()
    return bytes(data)


@pytest.fixture
def linked_target_scene(qapp):
    chapter = ChapterDocument(width=240, height=180, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 240, 180))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Blender views", BoundGeometry.rectangle(0, 0, 240, 180))
    parent.fill_color, parent.border_width = None, 0
    parent.translate_x, parent.translate_y = 13, 9
    target = chapter.add_object(parent.layer_id, ImageObject(
        name="Linked Blender Comic View", pixel_width=32, pixel_height=24,
        transform_frame=(0, 0, 32, 24),
        transform_quad=[(7, 11), (71, 11), (71, 59), (7, 59)],
        source=BlenderComicViewSourceDescriptor(project_uuid=PROJECT_UUID,
            view_uuid=VIEW_UUID, display_name="Panel 12", last_revision=1),
    ))
    owner = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 120, 90)))
    modifier = HalftoneModifier(color_mode="target_layer", target_layer_id=target.object_id,
        base_resolution=120, spacing=12, blur=0, transparent_background=True)
    chapter.add_modifier(modifier, [("object", owner.object_id)])
    store = ImageStore()
    store.put(target.object_id, "last-frame.png", png_bytes(image("red")), "image/png")
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore(), store)
    incoming = image("#606060", 120, 90)
    frame, placement = QRectF(0, 0, 120, 90), QTransform.fromTranslate(10, 5)
    yield canvas, chapter, target, modifier, incoming, frame, placement
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def capture(scene):
    canvas, _, _, modifier, incoming, frame, placement = scene
    return render_color_source(canvas, modifier, incoming, frame, placement)


def effect(scene):
    canvas, _, _, modifier, incoming, frame, placement = scene
    return render_stages(canvas, incoming, frame, [modifier], placement)[0]


def test_linked_blender_object_uses_cached_frame_at_transformed_coordinates(linked_target_scene):
    canvas, _, target, _, _, _, _ = linked_target_scene
    assert target.is_blender_linked
    stored_key = canvas.images.image(target.object_id).cacheKey()
    result = capture(linked_target_scene)
    assert result is not None
    # Target local (7,11) + parent (13,9) - incoming origin (10,5).
    assert result.pixelColor(11, 16) == QColor("red")
    assert result.pixelColor(72, 61) == QColor("red")
    assert result.pixelColor(5, 10).alpha() == 0
    assert result.pixelColor(76, 65).alpha() == 0
    assert capture(linked_target_scene).cacheKey() == result.cacheKey()
    assert canvas.images.image(target.object_id).cacheKey() == stored_key


def test_linked_blender_target_parent_movement_invalidates_capture(linked_target_scene):
    canvas, chapter, target, _, _, _, _ = linked_target_scene
    before_signature = source_signature(canvas, target.object_id)
    before = capture(linked_target_scene)
    chapter.layers[target.parent_layer_id].translate_x += 25
    assert source_signature(canvas, target.object_id) != before_signature
    after = capture(linked_target_scene)
    assert before is not None and after is not None
    assert after.cacheKey() != before.cacheKey()
    assert before.pixelColor(11, 16) == QColor("red")
    assert after.pixelColor(11, 16).alpha() == 0
    assert after.pixelColor(36, 16) == QColor("red")


def test_blender_target_fitted_to_parent_recaptures_after_bound_resize(linked_target_scene):
    canvas, chapter, target, _, _, _, _ = linked_target_scene
    target.placement_mode, target.fit_mode = "fit_parent", "stretch"
    before_signature = source_signature(canvas, target.object_id)
    before = capture(linked_target_scene)
    assert before is not None and before.pixelColor(80, 50) == QColor("red")
    chapter.layers[target.parent_layer_id].bound = BoundGeometry.rectangle(0, 0, 24, 18)
    assert source_signature(canvas, target.object_id) != before_signature
    after = capture(linked_target_scene)
    assert after is not None and after.cacheKey() != before.cacheKey()
    assert after.pixelColor(80, 50).alpha() == 0
    assert after.pixelColor(10, 10) == QColor("red")


def test_replacing_cached_blender_pixels_recaptures_without_descriptor_edit(linked_target_scene):
    canvas, _, target, _, _, _, _ = linked_target_scene
    before_signature = source_signature(canvas, target.object_id)
    before_descriptor = target.source.to_dict()
    before = capture(linked_target_scene)
    blue = image("blue")
    canvas.images.put_decoded(target.object_id, "last-frame.png", png_bytes(blue), blue)
    assert target.source.to_dict() == before_descriptor
    assert source_signature(canvas, target.object_id) != before_signature
    after = capture(linked_target_scene)
    assert before is not None and after is not None
    assert after.cacheKey() != before.cacheKey()
    assert after.pixelColor(30, 30) == QColor("blue")


def test_new_published_blender_render_refreshes_target_effect_and_dirty_region(
        linked_target_scene, tmp_path, monkeypatch):
    canvas, chapter, target, _, _, _, _ = linked_target_scene
    before = effect(linked_target_scene)
    published = tmp_path / "revision-2.png"
    published.write_bytes(png_bytes(image("blue")))
    dirty_regions = []
    monkeypatch.setattr(canvas, "_queue_visual_dirty", lambda dirty: dirty_regions.append(QRectF(dirty)))
    controller = BlenderImageSourceController(canvas)
    try:
        controller._set_views([ComicViewInfo(PROJECT_UUID, VIEW_UUID, "Panel 12", 2,
            32, 24, False, QImage(), str(published))])
        assert target.source.last_revision == 2
        assert canvas.images.image(target.object_id).pixelColor(3, 3) == QColor("blue")
        after = effect(linked_target_scene)
        assert after != before
        np.testing.assert_array_equal(_qimage_premultiplied(after)[..., 3],
                                      _qimage_premultiplied(before)[..., 3])
        assert dirty_regions
        assert dirty_regions[-1].contains(QRectF(0, 0, chapter.width, chapter.height))
        assert capture(linked_target_scene).pixelColor(30, 30) == QColor("blue")
    finally:
        controller.shutdown()
        controller.deleteLater()


def test_hidden_blender_object_remains_usable_and_supports_target_hsl(linked_target_scene):
    _, _, target, modifier, _, _, _ = linked_target_scene
    target.visible = False
    target.mask_only = True
    red_capture = capture(linked_target_scene)
    assert red_capture is not None
    assert red_capture.pixelColor(30, 30) == QColor("red")
    before = effect(linked_target_scene)
    modifier.target_hue = 120
    green = effect(linked_target_scene)
    assert target.visible is False and target.mask_only is True
    before_pixels, green_pixels = _qimage_premultiplied(before), _qimage_premultiplied(green)
    np.testing.assert_array_equal(before_pixels[..., 3], green_pixels[..., 3])
    region = green_pixels[25:50, 25:60]
    opaque = region[..., 3] > .999
    assert opaque.any()
    np.testing.assert_allclose(region[opaque, :3], np.tile([0., 1., 0.], (opaque.sum(), 1)), atol=1 / 255)


def test_missing_blender_frame_uses_incoming_colors_instead_of_waiting_placeholder(linked_target_scene):
    canvas, _, target, modifier, incoming, _, _ = linked_target_scene
    canvas.images.remove(target.object_id)
    assert canvas.images.image(target.object_id).isNull()
    result = capture(linked_target_scene)
    assert result is None or not _qimage_premultiplied(result)[..., 3].any()
    expected = apply_pattern_effect(incoming, replace(modifier, color_mode="source"))
    assert effect(linked_target_scene) == expected
