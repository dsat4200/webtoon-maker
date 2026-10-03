"""Attached native pixels, rigs and exact-output reuse follow object moves."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, CageTransformModifier,
    ChapterDocument, ColorFillGradientObject, DistortModifier, ImageObject,
    LimitedMaskGradient, MirrorModifier, ParameterMaskBinding, PathNode, RadialBlurModifier,
    RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.attached_translation import transform_attached
from comic_editor.ui.transform_modifier_preview import effective_preview_modifier


@pytest.fixture
def scene(qapp, monkeypatch):
    chapter = ChapterDocument(width=400, height=300, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 400, 300))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    obj = chapter.add_object(page.layer_id, ImageObject(x=80, y=80, pixel_width=64, pixel_height=64))
    pixels = np.random.default_rng(29).integers(0, 256, (64, 64, 4), dtype=np.uint8)
    pixels[..., 3] = 255
    image = QImage(pixels.data, 64, 64, 256, QImage.Format_RGBA8888).copy()
    canvas.images.put_decoded(obj.object_id, "source.png", b"", image)
    monkeypatch.setattr("comic_editor.ui.gpu_textures.renderer_for", lambda _: None)
    yield canvas, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(400, 300, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return np.frombuffer(image.constBits(), np.uint8).reshape(300, 400, 4).copy()


def preview(canvas, obj, dx=35, dy=23):
    canvas.set_selection("object", obj.object_id)
    canvas._model_before = deepcopy(canvas.chapter.to_dict())
    canvas._transform_start_quad = [canvas.layer_world_transform(obj.parent_layer_id).inverted()[0].map(QPointF(*p)).toTuple()
                                    for p in canvas.object_world_quad(obj.object_id)]
    canvas._transform_preview_quad = [(x + dx, y + dy) for x, y in canvas._transform_start_quad]
    canvas._transform_drag_mode = "translate"


def mask_for(canvas, obj, parameter=None):
    gradient = ColorFillGradientObject(mask_only=True)
    gradient.ramp.stops[0].color = "#00FFFFFF"
    gradient.ramp.stops[-1].color = "#FFFFFFFF"
    gradient.line_field.geometry = BoundGeometry.path([PathNode(x=80, y=80), PathNode(x=144, y=144)])
    mask = ToneMask(gradient=gradient, limited_gradients=[LimitedMaskGradient(
        gradient=deepcopy(gradient), half_width=10, feather=2, operation="subtract")])
    mask.limited_gradients[0].gradient.object_id += "limited"
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(90, 90), 20, QColor("white"))
    binding = ParameterMaskBinding(mask.mask_id, 0, 1 if parameter is None else 100)
    if parameter is None:
        obj.opacity_mask = binding
    else:
        parameter.parameter_masks["intensity"] = binding
    return mask


def cage():
    modifier = CageTransformModifier(frame=(80, 80, 64, 64))
    modifier.validate()
    x, y = modifier.points[5]
    modifier.points[5] = (x + 8, y - 4)
    return modifier


def mesh():
    modifier = DistortModifier(modifier_type="distort_mesh_warp", frame=(80, 80, 64, 64))
    modifier.validate()
    x, y = modifier.points[len(modifier.points) // 2]
    modifier.points[len(modifier.points) // 2] = (x + .1, y - .05)
    return modifier


def smudge():
    from test_smudge_rendering import modifier, stroke
    value = modifier(stroke(start=(90, 105), end=(130, 116), radius=12.3))
    value.frame = (80, 80, 64, 64)
    value.center = (112, 112)
    return value


FACTORIES = [
    lambda: BlurModifier(strength=2, mode="focal", focal_center=(110, 110), focal_radius=32),
    lambda: ArrayModifier(count=1, axis_start=(80, 80), axis_end=(90, 80), center=(112, 112)),
    lambda: MirrorModifier(axis_start=(112, 60), axis_end=(112, 170), intensity=65),
    lambda: RadialBlurModifier(center=(112, 112), angle=20),
    cage,
    lambda: DistortModifier(modifier_type="distort_twirl", frame=(80, 80, 64, 64), center=(112, 112), radius=32, parameters={"angle": 50}),
    mesh,
    smudge,
]


@pytest.mark.parametrize("factory", FACTORIES)
def test_preview_commit_reuse_and_cold_native_pixels(scene, monkeypatch, factory):
    canvas, obj = scene
    modifier = factory()
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    mask_for(canvas, obj, modifier)
    mask = mask_for(canvas, obj)
    before = render(canvas)
    tiles_before = canvas.tiles.object_signature(mask.mask_id)
    saved = deepcopy(canvas.chapter.to_dict())
    preview(canvas, obj)
    effective = deepcopy(effective_preview_modifier(canvas, modifier).to_dict())
    captures = []
    fields = []
    original = canvas._render_object_content
    original_fields = canvas._modifier_mask_fields
    original_mask = canvas.render_tone_mask_field
    mask_fields = []
    def capture(*args, **kwargs):
        captures.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "_render_object_content", capture)
    def parameter_fields(*args, **kwargs):
        fields.append(1)
        return original_fields(*args, **kwargs)
    monkeypatch.setattr(canvas, "_modifier_mask_fields", parameter_fields)
    def tone_field(*args, **kwargs):
        mask_fields.append(1)
        return original_mask(*args, **kwargs)
    monkeypatch.setattr(canvas, "render_tone_mask_field", tone_field)
    during = render(canvas)
    assert canvas.chapter.to_dict() == saved
    assert not captures, "translation recaptured unchanged source"
    assert not fields, "translation reevaluated unchanged effect parameters"
    assert not mask_fields, "translation resampled unchanged attached masks"
    canvas._commit_object_transform()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == effective
    assert mask.paint_offset == (35, 23)
    assert canvas.tiles.object_signature(mask.mask_id) == tiles_before
    after = render(canvas)
    np.testing.assert_array_equal(during, after)
    np.testing.assert_array_equal(before[40:185, 40:190], after[63:208, 75:225])
    assert not captures
    assert not fields
    assert not mask_fields
    # Force the reference renderer to recompute, independently of every alias.
    monkeypatch.setattr("comic_editor.ui.translation_cache.get", lambda *_: None)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    for scope in list(canvas._effect_jobs.retained):
        canvas._effect_jobs.retained_remove(scope)
    np.testing.assert_array_equal(after, render(canvas))
    assert captures
    canvas.command_stack.undo()
    np.testing.assert_array_equal(before, render(canvas))
    canvas.command_stack.redo()
    np.testing.assert_array_equal(after, render(canvas))


@pytest.mark.parametrize("factory", [FACTORIES[1], FACTORIES[3], FACTORIES[5], FACTORIES[6]])
@pytest.mark.parametrize("delta", [(35, 23), (35.5, -23.25)])
def test_raster_translation_keeps_native_effect_grid(scene, monkeypatch, factory, delta):
    canvas, image_obj = scene
    obj = canvas.chapter.add_object(image_obj.parent_layer_id, RasterObject(x=80, y=80, interaction_rect=(0, 0, 64, 64)))
    canvas.chapter.delete_entity("object", image_obj.object_id)
    tile = canvas.tiles._empty(canvas.tiles.tile_size)
    from PySide6.QtGui import QPainter
    painter = QPainter(tile)
    painter.drawImage(0, 0, canvas.images.image(image_obj.object_id))
    painter.end()
    canvas.tiles.set_tile(obj.object_id, (0, 0), tile)
    modifier = factory()
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    mask_for(canvas, obj, modifier)
    mask_for(canvas, obj)
    render(canvas)
    calls = []
    original = canvas._modifier_mask_fields
    def fields(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "_modifier_mask_fields", fields)
    preview(canvas, obj, *delta)
    during = render(canvas)
    assert not calls
    canvas._commit_object_transform()
    after = render(canvas)
    np.testing.assert_array_equal(during, after)
    assert not calls
    monkeypatch.setattr("comic_editor.ui.translation_cache.get", lambda *_: None)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    for scope in list(canvas._effect_jobs.retained):
        canvas._effect_jobs.retained_remove(scope)
    np.testing.assert_array_equal(after, render(canvas))
    assert calls


def test_shared_data_only_moves_when_all_consumers_move(scene):
    canvas, obj = scene
    other = canvas.chapter.add_object(obj.parent_layer_id, ImageObject())
    modifier = RadialBlurModifier(center=(100, 100))
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id), ("object", other.object_id)])
    mask = mask_for(canvas, obj)
    other.opacity_mask = deepcopy(obj.opacity_mask)
    transform_attached(canvas, [("object", obj.object_id)], QTransform.fromTranslate(15, 10))
    assert modifier.center == (100, 100) and mask.paint_offset == (0, 0)
    transform_attached(canvas, [("object", obj.object_id), ("object", other.object_id)], QTransform.fromTranslate(15, 10))
    assert modifier.center == (115, 110) and mask.paint_offset == (15, 10)


def test_multi_move_shared_rig_and_mask_are_translated_once_across_parents(scene, monkeypatch):
    canvas, obj = scene
    parent = canvas.chapter.add_layer(obj.parent_layer_id, "Rotated", BoundGeometry.rectangle(0, 0, 100, 100))
    parent.transform_frame = (0, 0, 100, 100)
    parent.transform_quad = [(300, 80), (300, 180), (200, 180), (200, 80)]
    parent.fill_color, parent.border_width = None, 0
    other = canvas.chapter.add_object(parent.layer_id, ImageObject(pixel_width=64, pixel_height=64))
    canvas.images.put_decoded(other.object_id, "other.png", b"", canvas.images.image(obj.object_id))
    modifier = RadialBlurModifier(center=(170, 115), angle=10)
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id), ("object", other.object_id)])
    mask = mask_for(canvas, obj, modifier)
    render(canvas)
    canvas._model_before = deepcopy(canvas.chapter.to_dict())
    saved = deepcopy(canvas._model_before)
    canvas._geometry_transform_target = ("multi", "")
    canvas._transform_start_quad = [(40, 40), (340, 40), (340, 220), (40, 220)]
    canvas._transform_preview_quad = [(x + 35, y + 23) for x, y in canvas._transform_start_quad]
    canvas._transform_drag_mode = "translate"
    for target in (obj, other):
        world_quad = canvas.object_world_quad(target.object_id)
        inverse = canvas.layer_world_transform(target.parent_layer_id).inverted()[0]
        canvas._multi_transform_preview_quads[target.object_id] = [inverse.map(QPointF(x + 35, y + 23)).toTuple() for x, y in world_quad]
    monkeypatch.setattr(canvas, "_modifier_mask_fields", lambda *_: pytest.fail("shared translation reran effects"))
    during = render(canvas)
    assert canvas.chapter.to_dict() == saved
    canvas._commit_geometry_transform()
    assert modifier.center == (205, 138)
    assert mask.paint_offset == (35, 23)
    np.testing.assert_array_equal(during, render(canvas))


@pytest.mark.parametrize("dependency", ["pixels", "paint", "gradient", "rig", "fractional_position", "linked_contributor"])
def test_real_dependency_edits_after_translation_match_cold_render(scene, monkeypatch, dependency):
    canvas, obj = scene
    modifier = FACTORIES[5]()
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    mask = mask_for(canvas, obj, modifier)
    render(canvas)
    preview(canvas, obj)
    canvas._commit_object_transform()
    before = render(canvas)
    if dependency == "pixels":
        replacement = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
        replacement.fill(QColor("blue"))
        canvas.images.put_decoded(obj.object_id, "source.png", b"", replacement)
    elif dependency == "paint":
        canvas.tiles.paint_dab(mask.mask_id, QPointF(120, 125), 25, QColor("white"))
        mask.touch()
    elif dependency == "gradient":
        mask.gradient.line_field.geometry.nodes[0].x += 20
        mask.gradient.touch_revision()
    elif dependency == "rig":
        modifier.center = (modifier.center[0] + 20, modifier.center[1] + 10)
    elif dependency == "fractional_position":
        preview(canvas, obj, .25, 0)
        canvas._commit_object_transform()
    else:
        other = canvas.chapter.add_object(obj.parent_layer_id, ImageObject(x=obj.x, y=obj.y, pixel_width=64, pixel_height=64))
        canvas.images.put_decoded(other.object_id, "contributor.png", b"", canvas.images.image(obj.object_id))
        other.mask_only = True
        mask.contributors = [("object", other.object_id)]
    canvas.documentChanged.emit(None)
    after = render(canvas)
    assert not np.array_equal(before, after)
    monkeypatch.setattr("comic_editor.ui.translation_cache.get", lambda *_: None)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    for scope in list(canvas._effect_jobs.retained):
        canvas._effect_jobs.retained_remove(scope)
    np.testing.assert_array_equal(after, render(canvas))


def test_mask_offset_roundtrip_and_editing(scene):
    canvas, obj = scene
    mask = mask_for(canvas, obj)
    transform_attached(canvas, [("object", obj.object_id)], QTransform.fromTranslate(.5, -256.25))
    loaded = ChapterDocument.from_dict(canvas.chapter.to_dict())
    assert loaded.masks[mask.mask_id].paint_offset == (.5, -256.25)
    old = mask.to_dict()
    old.pop("paint_offset")
    assert ToneMask.from_dict(old).paint_offset == (0, 0)
    canvas.active_tone_mask_id = mask.mask_id
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_mask_stroke(QPointF(100.5, -156.25), 1)
    canvas._end_mask_stroke()
    assert canvas.tiles.content_bounds(mask.mask_id).contains(QPointF(100, 100))


@pytest.mark.parametrize("factory", [FACTORIES[0], FACTORIES[1], FACTORIES[3], FACTORIES[4]])
def test_group_translation_reuses_subtree_with_attached_child_effects(scene, monkeypatch, factory):
    canvas, obj = scene
    group = canvas.chapter.add_layer(obj.parent_layer_id, "Group", BoundGeometry.rectangle(60, 60, 160, 160))
    group.fill_color, group.border_width = None, 0
    canvas.chapter.move_entity("object", obj.object_id, group.layer_id, 0)
    modifier = factory()
    canvas.chapter.add_modifier(modifier, [("layer", group.layer_id)])
    child_effect = FACTORIES[5]()
    canvas.chapter.add_modifier(child_effect, [("object", obj.object_id)])
    mask_for(canvas, obj, child_effect)
    before = render(canvas)
    canvas.set_selection("layer", group.layer_id)
    canvas._model_before = deepcopy(canvas.chapter.to_dict())
    canvas._geometry_transform_target = ("layer_group", group.layer_id)
    canvas._transform_start_quad = canvas._rect_quad(QRectF(*group.bound.bbox()))
    canvas._transform_preview_quad = [(x + 35, y + 23) for x, y in canvas._transform_start_quad]
    canvas._transform_drag_mode = "translate"
    fields = []
    original = canvas._modifier_mask_fields
    def count(*args, **kwargs):
        fields.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(canvas, "_modifier_mask_fields", count)
    during = render(canvas)
    assert not fields
    canvas._commit_geometry_transform()
    after = render(canvas)
    np.testing.assert_array_equal(during, after)
    assert not fields
    np.testing.assert_array_equal(before[40:225, 40:225], after[63:248, 75:260])
    monkeypatch.setattr("comic_editor.ui.translation_cache.get", lambda *_: None)
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    for scope in list(canvas._effect_jobs.retained):
        canvas._effect_jobs.retained_remove(scope)
    np.testing.assert_array_equal(after, render(canvas))


def test_disk_backing_uses_the_same_translated_exact_key_and_excludes_preview(scene, tmp_path, monkeypatch):
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.ui.cache_dependencies import RenderDependencies
    canvas, obj = scene
    modifier = FACTORIES[5]()
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    mask_for(canvas, obj, modifier)
    def bind():
        backing = PersistentRenderCache(tmp_path, contract=canvas.chapter.pixel_contract.signature,
                                        environment=RenderDependencies.environment())
        dependencies = RenderDependencies(canvas, backing)
        canvas._persistent_render_cache = backing
        canvas._render_dependencies = dependencies
        canvas.tiles.render_fingerprint = dependencies.tiles
        canvas.images.render_fingerprint = dependencies.image
        return backing
    backing = bind()
    try:
        canvas._projection_exact = True
        with backing.record():
            render(canvas)
        backing.drain()
        saved_keys = set(backing.entries)
        preview(canvas, obj)
        with backing.record():
            during = render(canvas)
        backing.drain()
        assert set(backing.entries) == saved_keys
        canvas._commit_object_transform()
        backing.close()
        backing = bind()
        canvas._projection_exact = True
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        canvas._modifier_source_cache.clear()
        canvas._modifier_source_cache_bytes = 0
        for scope in list(canvas._effect_jobs.retained):
            canvas._effect_jobs.retained_remove(scope)
        monkeypatch.setattr(canvas, "_render_object_content", lambda *_: pytest.fail("disk exact output was recaptured"))
        np.testing.assert_array_equal(during, render(canvas))
    finally:
        canvas._persistent_render_cache = canvas._render_dependencies = None
        canvas.tiles.render_fingerprint = canvas.images.render_fingerprint = None
        backing.close()
