from __future__ import annotations

import copy
import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QComboBox, QSlider

from comic_editor.core.kuwahara import apply_kuwahara, _orientation, KuwaharaCache
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, KuwaharaModifier, ModifierPreset,
    ParameterMaskBinding, RasterObject, ToneMask, modifier_from_dict,
)
from comic_editor.core.modifier_presets import preset_from_modifier, apply_modifier_preset
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _premultiplied_qimage, _qimage_premultiplied


VARIANTS = ("original", "generalized", "anisotropic")


def noise(shape=(19, 23), seed=8):
    rgba = np.random.default_rng(seed).random((*shape, 4), dtype=np.float32)
    rgba[..., :3] *= rgba[..., 3:4]
    return rgba


def brute_original(rgba, radii):
    result = np.zeros_like(rgba)
    height, width = rgba.shape[:2]
    for y in range(height):
        for x in range(width):
            radius = int(radii[y, x])
            best = float("inf")
            for dy, dx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                yy = np.clip(y+dy*np.arange(radius+1), 0, height-1)
                xx = np.clip(x+dx*np.arange(radius+1), 0, width-1)
                samples = rgba[yy[:, None], xx].astype(np.float64)
                alpha = samples[..., 3:4]
                rgb = np.divide(samples[..., :3], alpha, out=np.zeros_like(samples[..., :3]), where=alpha > 0)
                mean = np.sum(rgb*alpha, axis=(0, 1)) / max(alpha.sum(), 1e-12)
                variance = np.maximum(0, np.sum(rgb*rgb*alpha, axis=(0, 1))/max(alpha.sum(), 1e-12)-mean*mean).sum()
                if variance < best:
                    best = variance
                    result[y, x, :3] = mean*rgba[y, x, 3]
    result[..., 3] = rgba[..., 3]
    return result


@pytest.mark.parametrize("variant", VARIANTS)
def test_serialization_presets_masks_and_validation(variant):
    modifier = KuwaharaModifier(variant=variant, size=11, strength=63, quality="high",
        overlap=44, iterations=2, processing_scale=50,
        parameter_masks={"size": ParameterMaskBinding("ramp", 0, 30),
                         "strength": ParameterMaskBinding("second", 40, 100)})
    restored = modifier_from_dict(json.loads(json.dumps(modifier.to_dict())))
    assert restored == modifier
    preset = ModifierPreset.from_dict(json.loads(json.dumps(preset_from_modifier("Paint", modifier).to_dict())))
    target = KuwaharaModifier(parameter_masks={"size": ParameterMaskBinding("local", 2, 19)})
    result = apply_modifier_preset(target, preset)
    assert result.variant == variant
    assert result.processing_scale == 50
    assert result.parameter_masks == target.parameter_masks
    assert result.modifier_id == target.modifier_id


@pytest.mark.parametrize("parameter", ["size", "strength", "sharpness", "hardness", "overlap",
    "anisotropy", "tensor_radius", "iterations", "processing_scale", "intensity"])
def test_nonfinite_parameters_rejected(parameter):
    modifier = KuwaharaModifier()
    setattr(modifier, parameter, float("nan"))
    with pytest.raises(ValueError, match="finite"):
        modifier.validate()


def test_original_exact_quadrants_with_variable_radius_and_alpha():
    rgba = noise((9, 11))
    radii = np.random.default_rng(3).integers(0, 4, (9, 11))
    result = apply_kuwahara(rgba, KuwaharaModifier(variant="original"), size=radii)
    assert np.allclose(result, brute_original(rgba, radii), atol=2e-6)
    fractional = apply_kuwahara(rgba, KuwaharaModifier(variant="original"), size=radii+.25)
    expected = .75*brute_original(rgba, radii)+.25*brute_original(rgba, radii+1)
    assert np.allclose(fractional, expected, atol=2e-6)


@pytest.mark.parametrize("variant", VARIANTS)
@pytest.mark.parametrize("scale", [25, 100])
def test_alpha_constant_color_transparency_and_zero_radius(variant, scale):
    rgba = noise()
    rgba[..., :3] = np.array([.2, .6, .9])*rgba[..., 3:4]
    rgba[:, 0] = 0
    settings = KuwaharaModifier(variant=variant, processing_scale=scale)
    result = apply_kuwahara(rgba, settings)
    assert np.array_equal(result[..., 3], rgba[..., 3])
    assert np.allclose(result, rgba, atol=2e-5)
    assert np.all(result[:, 0] == 0)
    assert np.array_equal(apply_kuwahara(rgba, settings, size=0), rgba)
    assert np.array_equal(apply_kuwahara(rgba, settings, strength=0), rgba)


@pytest.mark.parametrize("variant", VARIANTS)
def test_size_mask_changes_support_and_strength_mask_blends_only_rgb(variant):
    rgba = noise()
    modifier = KuwaharaModifier(variant=variant, size=5)
    sizes = np.where(np.indices(rgba.shape[:2])[1] < 10, 0., 5.).astype(np.float32)
    variable = apply_kuwahara(rgba, modifier, size=sizes)
    full = apply_kuwahara(rgba, modifier)
    assert np.array_equal(variable[:, :10], rgba[:, :10])
    assert np.allclose(variable[:, 10:], full[:, 10:], atol=2e-6)
    strength = np.linspace(0, 100, rgba.shape[1])[None, :]
    mixed = apply_kuwahara(rgba, modifier, strength=np.broadcast_to(strength, rgba.shape[:2]))
    assert np.allclose(mixed[..., :3], rgba[..., :3]+(full[..., :3]-rgba[..., :3])*strength[..., None]/100)
    assert np.array_equal(mixed[..., 3], rgba[..., 3])


def test_structure_tensor_follows_edge_tangent_and_variants_are_distinct():
    rgba = np.ones((40, 40, 4), dtype=np.float32)
    rgba[:, :20, :3] = 0
    cs, sn, coherence = _orientation(rgba, 2)
    assert np.max(np.abs(cs[:, 18:22])) < 1e-5
    assert np.min(np.abs(sn[:, 18:22])) > .999
    assert np.min(coherence[:, 18:22]) > .999
    source = noise((30, 30))
    outputs = [apply_kuwahara(source, KuwaharaModifier(variant=variant)) for variant in VARIANTS]
    assert not np.allclose(outputs[0], outputs[1], atol=.001)
    assert not np.allclose(outputs[1], outputs[2], atol=.001)


@pytest.mark.parametrize("variant", ["generalized", "anisotropic"])
def test_sectors_remove_texture_preserve_hard_edge_and_extreme_weights_stay_finite(variant):
    rgba = np.ones((31, 50, 4), dtype=np.float32)
    rng = np.random.default_rng(8)
    rgba[:, :25, :3] = rng.uniform(.1, .2, (31, 25, 3))
    rgba[:, 25:, :3] = rng.uniform(.8, .9, (31, 25, 3))
    result = apply_kuwahara(rgba, KuwaharaModifier(variant=variant, size=5, hardness=100, sharpness=16))
    assert np.isfinite(result).all()
    assert result[:, :18, :3].var() < rgba[:, :18, :3].var()
    assert result[:, 24, :3].mean() < .3
    assert result[:, 25, :3].mean() > .7


def test_cache_reuses_filter_when_only_strength_changes_and_respects_budget():
    source = noise((12, 12))
    cache = KuwaharaCache(budget=source.nbytes*2)
    modifier = KuwaharaModifier(variant="original")
    first = cache.filtered(source, 3, modifier)
    modifier.strength = 20
    assert cache.filtered(source, 3, modifier) is first
    second = cache.filtered(source, 4, modifier)
    assert second is not first
    cache.filtered(source, 5, modifier)
    assert cache.bytes <= cache.budget
    assert len(cache._values) == 2


@pytest.mark.parametrize("variant", VARIANTS)
def test_cancelled_tiles_are_not_cached_as_complete_results(variant):
    source = noise((110, 110))
    modifier = KuwaharaModifier(variant=variant, quality="draft")
    cache = KuwaharaCache()
    checks = 0
    def cancelled():
        nonlocal checks
        checks += 1
        return checks >= 3
    assert cache.filtered(source, 5., modifier, cancelled=cancelled) is None
    assert not cache._values
    assert cache.bytes == 0


@pytest.mark.parametrize("variant", VARIANTS)
def test_render_stack_uses_both_masks_and_preserves_alpha(variant):
    image = _premultiplied_qimage(noise())
    modifier = KuwaharaModifier(variant=variant, parameter_masks={
        "size": ParameterMaskBinding("one", 0, 5),
        "strength": ParameterMaskBinding("two", 0, 100)})
    size = np.ones((image.height(), image.width()), dtype=np.float32)
    size[:, :8] = 0
    strength = np.ones_like(size)
    strength[:5] = 0
    result = _qimage_premultiplied(apply_modifier_stack(image, [modifier], (0, 0), {
        (modifier.modifier_id, "size"): size,
        (modifier.modifier_id, "strength"): strength}))
    original = _qimage_premultiplied(image)
    assert np.array_equal(result[:, :8], original[:, :8])
    assert np.array_equal(result[:5], original[:5])
    assert np.array_equal(result[..., 3], original[..., 3])
    assert not np.array_equal(result[5:, 8:, :3], original[5:, 8:, :3])


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=80, height=80, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 80, 80))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    image = QImage(80, 80, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return _qimage_premultiplied(image)


def test_menu_variants_controls_undo_and_save_reload(scene):
    canvas, chapter, page = scene
    obj = chapter.add_object(page.layer_id, ImageObject(x=10, y=10, pixel_width=30, pixel_height=30))
    canvas.images.put_decoded(obj.object_id, "source.png", b"", _premultiplied_qimage(noise((30, 30))))
    canvas.set_selection("object", obj.object_id)
    controls = ModifierControls(canvas)
    try:
        assert [a.text() for a in controls.kuwahara_menu.actions()] == [
            "Original Kuwahara", "Papari Generalized Kuwahara", "Anisotropic Kuwahara"]
        controls.kuwahara_menu.actions()[2].trigger()
        modifier_id = controls.common_ids()[0]
        card = controls._cards[modifier_id]
        sliders = card.findChildren(QSlider)
        assert [(s.minimum(), s.maximum()) for s in sliders] == [(0, 100), (0, 64), (0, 100)]
        revision = canvas.command_stack.revision
        sliders[1].sliderPressed.emit()
        sliders[1].setValue(10)
        sliders[1].setValue(14)
        sliders[1].sliderReleased.emit()
        assert canvas.command_stack.revision == revision+1
        canvas.command_stack.undo()
        assert canvas.chapter.modifiers[modifier_id].size == 6
        canvas.command_stack.redo()
        assert canvas.chapter.modifiers[modifier_id].size == 14
        expected = render(canvas)
        restored = ChapterDocument.from_dict(json.loads(json.dumps(canvas.chapter.to_dict())))
        canvas.set_document(restored, canvas.tiles, canvas.images)
        assert np.array_equal(render(canvas), expected)
    finally:
        controls.deleteLater()


def test_raster_bake_keeps_result_and_undo(scene):
    canvas, chapter, page = scene
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 80, 80)))
    canvas.set_selection("object", obj.object_id)
    for index in range(9):
        canvas.tiles.paint_dab(obj.object_id, QPointF(20+index*4, 38), 18,
                               QColor(40+index*20, 90, 160-index*10), opacity=.7)
    modifier = KuwaharaModifier(variant="original", size=4)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    expected = render(canvas)
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert not chapter.objects[obj.object_id].modifier_ids
    assert np.allclose(render(canvas), expected, atol=1/255)
    canvas.command_stack.undo()
    assert canvas.chapter.objects[obj.object_id].modifier_ids == [modifier.modifier_id]
    assert np.array_equal(render(canvas), expected)
