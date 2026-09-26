"""Color filters: real pixels, masks, UI undo, persistence and raster baking."""
import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform
from PySide6.QtWidgets import QComboBox, QSlider

from comic_editor.core.models import (BoundGeometry, ChapterDocument, DitheringModifier,
    SharpnessModifier, KuwaharaModifier, BrightnessContrastModifier,
    ParameterMaskBinding, RasterObject, modifier_from_dict)
from comic_editor.core.image_filters import dither, sharpen
from comic_editor.core.modifier_presets import preset_from_modifier, apply_modifier_preset
from comic_editor.core.images import ImageStore
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied, _premultiplied_qimage


def sample():
    yy, xx = np.mgrid[:48, :64]
    rgb = np.stack([xx / 63, yy / 47, ((xx + yy) % 13) / 12], axis=2).astype(np.float32)
    alpha = np.where(xx < 10, 0., np.where(yy < 8, .4, 1.)).astype(np.float32)[..., None]
    return np.concatenate([rgb * alpha, alpha], axis=2)


@pytest.mark.parametrize("factory", [DitheringModifier, SharpnessModifier])
def test_roundtrip_presets_validation(factory):
    modifier = factory(parameter_masks={"strength": ParameterMaskBinding("m", 0, 90)})
    assert modifier_from_dict(json.loads(json.dumps(modifier.to_dict()))) == modifier
    preset = preset_from_modifier("Test", modifier)
    clone = apply_modifier_preset(factory(), preset)
    assert clone.strength == modifier.strength
    assert not clone.parameter_masks
    modifier.strength = float("nan")
    with pytest.raises(ValueError, match="finite"):
        modifier.validate()


@pytest.mark.parametrize("method", ["ordered", "noise"])
def test_dither_quantizes_repeatably_and_preserves_coverage(method):
    source = sample()
    modifier = DitheringModifier(method=method, levels=3, seed=241)
    result = dither(source, modifier)
    assert np.array_equal(result, dither(source, modifier))
    assert np.array_equal(result[..., 3], source[..., 3])
    assert np.all(result[..., :3][source[..., 3] == 0] == 0)
    assert set(np.unique(result[10:, 10:, :3])) <= {0., .5, 1.}
    flat = np.ones((32, 32, 4), np.float32)
    flat[..., :3] = .4
    pattern = dither(flat, DitheringModifier(method=method, levels=2))
    assert .3 < np.mean(pattern[..., 0]) < .5
    if method == "noise":
        assert not np.array_equal(pattern, dither(flat, DitheringModifier(method=method, levels=2, seed=5)))


def test_dither_monochrome_and_pattern_scale():
    source = sample()
    result = dither(source, DitheringModifier(monochrome=True, pixel_size=4))
    np.testing.assert_array_equal(result[..., 0], result[..., 1])
    np.testing.assert_array_equal(result[..., 1], result[..., 2])
    flat = np.full((16, 16, 4), .5, np.float32)
    flat[..., 3] = 1
    tiled = dither(flat, DitheringModifier(levels=2, pixel_size=4))
    assert np.all(tiled[:4, :4, 0] == tiled[0, 0, 0])
    assert not np.array_equal(tiled, dither(flat, DitheringModifier(levels=2, pixel_size=1)))


def test_sharpness_increases_edges_and_threshold_protects_them():
    source = np.ones((16, 32, 4), np.float32)
    source[:, :16, :3], source[:, 16:, :3] = .3, .7
    result = sharpen(source, radius=2)
    assert result[8, 15, 0] < .3 and result[8, 16, 0] > .7
    np.testing.assert_array_equal(sharpen(source, threshold=100), source)
    np.testing.assert_array_equal(sharpen(source, strength=0), source)
    np.testing.assert_array_equal(sharpen(source, radius=0), source)


def test_sharpness_has_no_dark_fringe_and_masked_radius_changes_filter():
    source = np.zeros((24, 40, 4), np.float32)
    source[:, 10:30] = [.6, .3, .1, 1.]
    result = sharpen(source, radius=4, strength=400)
    np.testing.assert_allclose(result, source, atol=1e-6)
    source = sample()
    radius = np.broadcast_to(np.linspace(0, 20, 64, dtype=np.float32), source.shape[:2])
    result = sharpen(source, radius=radius)
    np.testing.assert_array_equal(result[:, 0], source[:, 0])
    np.testing.assert_array_equal(result[..., 3], source[..., 3])
    assert not np.allclose(result, sharpen(source, radius=2))


@pytest.mark.parametrize("factory", [DitheringModifier, SharpnessModifier])
def test_parameter_mask_and_intensity_endpoints(factory):
    image = _premultiplied_qimage(sample())
    modifier = factory()
    baseline = _qimage_premultiplied(apply_modifier_stack(image, [modifier], (0, 0)))
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("m", 0, 100)
    mask = np.zeros((48, 64), np.float32)
    mask[:, 32:] = 1
    result = _qimage_premultiplied(apply_modifier_stack(image, [modifier], (0, 0),
        {(modifier.modifier_id, "intensity"): mask}))
    np.testing.assert_array_equal(result[:, :32], _qimage_premultiplied(image)[:, :32])
    np.testing.assert_array_equal(result[:, 32:], baseline[:, 32:])


@pytest.mark.parametrize("factory", [DitheringModifier, SharpnessModifier])
def test_modifier_menu_edit_undo_bake_and_reopen(qapp, factory):
    chapter = ChapterDocument(width=120, height=120, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 120, 120))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 100)))
    canvas.tiles.paint_dab(obj.object_id, QPointF(40, 40), 35, QColor(130, 95, 72))
    canvas.set_selection("object", obj.object_id)
    controls = ModifierControls(canvas)
    try:
        next(a for a in controls.add_button.menu().actions() if a.text() == factory().name).trigger()
        mid = controls.common_ids()[0]
        card = controls._cards[mid]
        sliders = card.findChildren(QSlider)
        revision = canvas.command_stack.revision
        sliders[1].sliderPressed.emit()
        sliders[1].setValue(3 if factory is DitheringModifier else 180)
        sliders[1].sliderReleased.emit()
        assert canvas.command_stack.revision == revision + 1
        canvas.command_stack.undo()
        canvas.command_stack.redo()
        restored = ChapterDocument.from_dict(json.loads(json.dumps(canvas.chapter.to_dict())))
        assert isinstance(restored.modifiers[mid], factory)
        from comic_editor.ui.baking import apply_raster_modifiers
        output = QImage(120, 120, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(output)
        before = _qimage_premultiplied(output)
        apply_raster_modifiers(canvas, mid)
        assert not canvas.chapter.objects[obj.object_id].modifier_ids
        canvas.render_preview(output)
        np.testing.assert_allclose(_qimage_premultiplied(output), before, atol=2/255)
        canvas.command_stack.undo()
        assert canvas.chapter.objects[obj.object_id].modifier_ids == [mid]
    finally:
        canvas._effect_jobs.cancel()
        controls.deleteLater()
        canvas.deleteLater()


@pytest.mark.parametrize("modifier", [DitheringModifier(), SharpnessModifier(),
                                     KuwaharaModifier(variant="original")])
@pytest.mark.parametrize("regional", [True, False])
def test_requested_crop_keeps_filter_neighborhood_and_pattern_origin(qapp, modifier, regional):
    from comic_editor.ui.effect_pipeline import render_stages
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(ChapterDocument(document_kind="asset", width=64, height=48), TileStore())
    canvas._interactive_render = regional
    canvas._effect_region_requests = regional
    canvas._projection_exact = True
    source = _premultiplied_qimage(sample())
    suffix = BrightnessContrastModifier(brightness=10)
    for item in (modifier, suffix):
        canvas.chapter.modifiers[item.modifier_id] = item
    try:
        expected, _ = render_stages(canvas, source, QRectF(0, 0, 64, 48),
                                    [modifier, suffix], QTransform())
        requested = QRectF(17, 13, 27, 20)
        output, bounds = render_stages(canvas, source, QRectF(0, 0, 64, 48),
            [modifier, suffix], QTransform(), required=requested)
        assert bounds == requested
        np.testing.assert_array_equal(_qimage_premultiplied(output),
                                      _qimage_premultiplied(expected.copy(requested.toRect())))
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()


def test_limited_circular_mask_changes_actual_kuwahara_radius_after_reload(qapp):
    from comic_editor.core.models import (ColorFillGradientObject, ColorGradientRamp,
        ColorGradientStop, LimitedMaskGradient, LineGradientField, PathNode, ToneMask)
    chapter = ChapterDocument(document_kind="asset", width=64, height=48)
    gradient = ColorFillGradientObject(gradient_shape="circular",
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=32.5, y=24.5), PathNode(x=46.5, y=24.5)], False)),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#FFFFFFFF"),
                                     ColorGradientStop(position=1, color="#00FFFFFF")]))
    mask = ToneMask(limited_gradients=[LimitedMaskGradient(gradient=gradient)])
    chapter.masks[mask.mask_id] = mask
    modifier = KuwaharaModifier(size=8, parameter_masks={"size": ParameterMaskBinding(mask.mask_id, 8, 0)})
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 64, 48))
    target = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 64, 48)))
    chapter.add_modifier(modifier, [("object", target.object_id)])
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(ChapterDocument.from_dict(json.loads(json.dumps(chapter.to_dict()))), TileStore())
    modifier = canvas.chapter.modifiers[modifier.modifier_id]
    source = _premultiplied_qimage(sample())
    try:
        masks = canvas._modifier_mask_fields([modifier], 64, 48, QTransform(), QRectF(0, 0, 64, 48))
        actual = _qimage_premultiplied(apply_modifier_stack(source, [modifier], (0, 0), masks))
        original = _qimage_premultiplied(source)
        uniform = _qimage_premultiplied(apply_modifier_stack(source, [KuwaharaModifier(size=8)], (0, 0)))
        np.testing.assert_array_equal(actual[24, 32], original[24, 32])
        np.testing.assert_array_equal(actual[24, 55], uniform[24, 55])
        assert not np.array_equal(actual[24, 55], original[24, 55])
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
