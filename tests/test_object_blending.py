"""Object blends agree across export/canvas, masks, hierarchy and retained tiles."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.blend_modes import OBJECT_BLEND_MODES, composite_blend
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ImageObject, ParameterMaskBinding, RasterObject, TextObject, ToneMask,
    VectorDrawingObject, object_from_dict,
)
from comic_editor.core.vector_geometry import FreehandSample
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.selection_settings import SelectionSettingsPanel


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(height=240, background="#3c78b4")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 240))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.setFixedSize(320, 240)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 160, 120, 1
    canvas._document_projection_enabled = True
    obj = chapter.add_object(page.layer_id, RasterObject(opacity_locked=False))
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(150, 90, 30))
    canvas.tiles.set_tile(obj.object_id, (0, 0), image)
    yield canvas, page, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def preview(canvas, scale=1):
    image = QImage(round(canvas.chapter.width * scale), round(canvas.chapter.height * scale),
                   QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def rgba(image):
    converted = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.ndarray((converted.height(), converted.width(), 4), np.uint8,
                      buffer=converted.constBits(), strides=(converted.bytesPerLine(), 4, 1)).copy()


@pytest.mark.parametrize("mode,_label", OBJECT_BLEND_MODES)
def test_every_mode_matches_math_on_export_and_retained_canvas(scene, mode, _label):
    canvas, page, obj = scene
    obj.blend_mode = mode
    canvas.documentChanged.emit(None)
    exported = preview(canvas)
    actual = rgba(exported)[100, 100]
    b = np.array([[[60, 120, 180, 255]]], np.float32) / 255
    f = np.array([[[150, 90, 30, 255]]], np.float32) / 255
    expected = np.rint(composite_blend(b, f, mode, height_shade=np.zeros((1, 1, 1))) * 255)[0, 0]
    np.testing.assert_allclose(actual, expected, atol=2)
    canvas._ensure_scene_cache()
    background = np.array([36, 36, 40], np.float32)
    presented = np.rint(actual[:3] + background * (1 - actual[3] / 255))
    np.testing.assert_allclose(canvas._scene_cache.pixelColor(100, 100).getRgb()[:3], presented, atol=1)


@pytest.mark.parametrize("mode,_label", OBJECT_BLEND_MODES)
def test_round_trip_defaults_and_object_types(mode, _label):
    for kind in (RasterObject, VectorDrawingObject, ImageObject, TextObject, ColorFillGradientObject):
        obj = kind(blend_mode=mode)
        assert object_from_dict(obj.to_dict()).blend_mode == mode
        old = obj.to_dict()
        old.pop("blend_mode")
        assert object_from_dict(old).blend_mode == "normal"


@pytest.mark.parametrize("mode", [mode for mode, _ in OBJECT_BLEND_MODES if mode != "height_modulate"])
def test_premultiplied_transparency_and_modulation_coverage(mode):
    back = np.array([[[.1, .2, .3, .5], [0, 0, 0, 0], [.1, .2, .3, .5]]], np.float32)
    front = np.array([[[.3, .1, .2, .5], [.3, .1, .2, .5], [0, 0, 0, 0]]], np.float32)
    result = composite_blend(back, front, mode)
    np.testing.assert_allclose(result[0, 2], back[0, 2])
    assert np.isfinite(result).all()
    assert (result >= 0).all() and (result[..., :3] <= result[..., 3:4] + 1e-7).all()
    if mode.startswith("texture") or "modulate" in mode:
        np.testing.assert_allclose(result[0, 1], 0)
        assert result[0, 0, 3] <= back[0, 0, 3]
    else:
        np.testing.assert_allclose(result[0, 1], front[0, 1])
        assert result[0, 0, 3] == pytest.approx(.75)


def test_settings_all_modes_multiselect_undo_and_no_chapter_snapshot(scene, monkeypatch):
    canvas, page, obj = scene
    other = canvas.chapter.add_object(page.layer_id, TextObject(blend_mode="screen"))
    canvas.set_selection_set([("object", obj.object_id), ("object", other.object_id)])
    panel = SelectionSettingsPanel(canvas, canvas.settings, lambda: None)
    assert panel.blend_mode.count() == 24
    assert panel.blend_mode.currentIndex() == -1
    canvas._transform_static_cache = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    canvas._vector_eraser_background_cache = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    monkeypatch.setattr(canvas.chapter, "to_dict", lambda: pytest.fail("Blend control copied the entire chapter"))
    panel.blend_mode.setCurrentIndex(panel.blend_mode.findData("multiply"))
    assert obj.blend_mode == other.blend_mode == "multiply"
    assert canvas._transform_static_cache.isNull()
    assert canvas._vector_eraser_background_cache.isNull()
    canvas.command_stack.undo()
    assert obj.blend_mode == "normal" and other.blend_mode == "screen"
    assert panel.blend_mode.currentIndex() == -1
    canvas.command_stack.redo()
    assert obj.blend_mode == other.blend_mode == "multiply"
    canvas.set_selection("layer", page.layer_id, False)
    panel.refresh()
    assert panel.blend_row.isHidden()
    panel.deleteLater()


@pytest.mark.parametrize("mode", ["multiply", "linear_burn", "color", "texture_contrast", "alpha_modulate"])
def test_opacity_masks_modifiers_and_parent_clips(scene, mode):
    canvas, page, obj = scene
    obj.blend_mode = mode
    obj.opacity = .5
    modifier = canvas.chapter.add_modifier(BlurModifier(strength=2), [("object", obj.object_id)])
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    obj.opacity_mask = ParameterMaskBinding(mask.mask_id, .5, .5)
    page.bound = BoundGeometry.rectangle(0, 0, 140, 240)
    actual = preview(canvas)
    b = np.array([[[60, 120, 180, 255]]], np.float32) / 255
    f = np.array([[[150, 90, 30, 255]]], np.float32) / 255 * .25
    expected = np.rint(composite_blend(b, f, mode) * 255)[0, 0]
    np.testing.assert_allclose(rgba(actual)[100, 100], expected, atol=3)
    assert actual.pixelColor(170, 100).getRgb() == (60, 120, 180, 255)


def test_blend_applies_to_isolated_object_once(scene, monkeypatch):
    canvas, page, obj = scene
    obj.blend_mode = "multiply"

    def content(painter, _obj, _visible):
        painter.fillRect(QRectF(0, 0, 180, 180), QColor(150, 90, 30, 128))
        painter.fillRect(QRectF(60, 60, 180, 180), QColor(150, 90, 30, 128))

    monkeypatch.setattr(canvas, "_render_object_content", content)
    actual = preview(canvas).pixelColor(100, 100).getRgb()
    source = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.transparent)
    painter = QPainter(source)
    painter.fillRect(source.rect(), QColor(150, 90, 30, 128))
    painter.fillRect(source.rect(), QColor(150, 90, 30, 128))
    painter.end()
    expected = composite_blend(np.array([[[60, 120, 180, 255]]], np.float32) / 255,
                               rgba(source).astype(np.float32) / 255, "multiply")
    np.testing.assert_allclose(actual, np.rint(expected[0, 0] * 255), atol=2)


def test_emboss_block_boundaries_are_seamless(scene, monkeypatch):
    from comic_editor.ui import object_blending
    canvas, page, obj = scene
    obj.blend_mode = "height_modulate"
    for x in range(4):
        values = np.zeros((256, 256, 4), np.uint8)
        intensity = np.rint(128 + 90 * np.sin((np.arange(256) + x * 256) / 9))
        values[..., :3] = intensity[None, :, None]
        values[..., 3] = 255
        image = QImage(values.data, 256, 256, 1024, QImage.Format_RGBA8888).copy()
        canvas.tiles.set_tile(obj.object_id, (x, 0), image)
    original = rgba(preview(canvas))
    monkeypatch.setattr(object_blending, "CAPTURE_SIDE", 128)
    np.testing.assert_array_equal(rgba(preview(canvas)), original)
    assert not np.array_equal(original[100, 100], original[100, 105])


def test_native_and_custom_blends_keep_transforms_and_hidpi(scene):
    canvas, page, obj = scene
    obj.x, obj.y = 30, 20
    page.transform_frame = (0, 0, 1080, 240)
    page.transform_quad = [(10, 0), (1090, 0), (1090, 240), (10, 240)]
    for mode in ("multiply", "linear_burn", "color"):
        obj.blend_mode = mode
        full = preview(canvas)
        scaled = preview(canvas, 2)
        assert full.pixelColor(100, 100) == scaled.pixelColor(200, 200)
        image = QImage(640, 480, QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(2)
        canvas._scene_cache = image
        canvas.documentChanged.emit(None)
        canvas._render_scene_cache_rect(canvas.rect(), projection=False)
        np.testing.assert_allclose(image.pixelColor(200, 200).getRgb(), full.pixelColor(100, 100).getRgb(), atol=1)


def test_show_on_top_blends_with_ordinary_backdrop(scene):
    canvas, page, obj = scene
    obj.show_on_top = True
    obj.blend_mode = "multiply"
    front = canvas.chapter.add_object(page.layer_id, RasterObject())
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor(200, 150, 100))
    canvas.tiles.set_tile(front.object_id, (0, 0), tile)
    canvas.documentChanged.emit(None)
    expected = (118, 53, 12, 255)
    np.testing.assert_allclose(preview(canvas).pixelColor(100, 100).getRgb(), expected, atol=1)
    canvas._ensure_scene_cache()
    np.testing.assert_allclose(canvas._scene_cache.pixelColor(100, 100).getRgb(), expected, atol=1)


def test_normal_is_allocation_free_and_warm_projection_reuses_blends(scene, monkeypatch):
    from comic_editor.ui import object_blending
    canvas, page, obj = scene
    render = object_blending.render_blended_object
    calls = []

    def counted(*args):
        calls.append(1)
        return render(*args)

    monkeypatch.setattr(object_blending, "render_blended_object", counted)
    preview(canvas)
    assert not calls
    obj.blend_mode = "color"
    canvas.documentChanged.emit(None)
    canvas._ensure_scene_cache()
    assert calls
    count = len(calls)
    canvas._invalidate_scene_cache(projection=False)
    canvas._ensure_scene_cache()
    assert len(calls) == count


@pytest.mark.parametrize("mode", ["multiply", "color", "texture_multiply", "alpha_modulate"])
def test_rasterize_preserves_source_blend_and_undo(scene, mode):
    from comic_editor.ui.baking import rasterize
    canvas, page, obj = scene
    obj.blend_mode = mode
    before = rgba(preview(canvas))
    rasterize(canvas, "object", obj.object_id)
    replacement = canvas.chapter.objects[obj.object_id]
    assert isinstance(replacement, ImageObject)
    assert replacement.blend_mode == mode
    assert canvas.images.image(obj.object_id).pixelColor(100, 100).getRgb() == (150, 90, 30, 255)
    np.testing.assert_array_equal(rgba(preview(canvas)), before)
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].blend_mode == mode
    np.testing.assert_array_equal(rgba(preview(canvas)), before)


def test_changing_blend_reuses_finished_object_modifiers(scene, monkeypatch):
    canvas, page, obj = scene
    canvas.chapter.add_modifier(BlurModifier(strength=2), [("object", obj.object_id)])
    original = canvas._render_object_content
    captures = []

    def counted(*args):
        captures.append(1)
        return original(*args)

    monkeypatch.setattr(canvas, "_render_object_content", counted)
    obj.blend_mode = "multiply"
    before = preview(canvas).pixelColor(100, 100)
    assert captures
    count = len(captures)
    obj.blend_mode = "screen"
    after = preview(canvas).pixelColor(100, 100)
    assert after != before
    assert len(captures) == count


@pytest.mark.parametrize("modifier_target", [None, "object", "layer"])
def test_live_vector_ink_is_blended_once_before_modifiers(scene, modifier_target):
    canvas, page, raster = scene
    raster.visible = False
    group = canvas.chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(0, 0, 320, 240))
    group.fill_color, group.border_width = None, 0
    obj = canvas.chapter.add_object(group.layer_id, VectorDrawingObject(blend_mode="multiply"))
    canvas.set_selection("object", obj.object_id)
    if modifier_target:
        target = obj.object_id if modifier_target == "object" else group.layer_id
        canvas.chapter.add_modifier(BlurModifier(strength=2), [(modifier_target, target)])
    canvas._vector_gesture_mode = "pencil"
    canvas._vector_samples = [FreehandSample(100, 100)]
    canvas._vector_preview_tiles.paint_dab(canvas._vector_preview_id, QPointF(100, 100), 60, QColor(150, 90, 30))
    actual = preview(canvas)
    # A parent modifier isolates its subtree from the external backdrop.
    expected = (150, 90, 30) if modifier_target == "layer" else (35, 42, 21)
    np.testing.assert_allclose(actual.pixelColor(100, 100).getRgb()[:3], expected, atol=2)


def test_custom_blend_uses_cpu_when_gpu_declines(scene, monkeypatch):
    from comic_editor.ui import gpu_object_blending
    canvas, page, obj = scene
    obj.blend_mode = "color"
    expected = rgba(preview(canvas))

    class UnavailableOutput:
        def composite(self, *_args):
            return None

    monkeypatch.setattr(gpu_object_blending, "renderer_for", lambda _: UnavailableOutput())
    np.testing.assert_allclose(rgba(preview(canvas)), expected, atol=1)
