"""Brightness/contrast controls, transparent color processing, and stack integration."""
import copy
import json

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform
from PySide6.QtWidgets import QLabel, QSlider

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    ArrayModifier, BoundGeometry, BrightnessContrastModifier, ChapterDocument,
    ImageObject, ModifierPreset, ParameterMaskBinding, RasterObject, TextObject,
    ToneMask, modifier_from_dict,
)
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied


def image(colors, alpha=255):
    result = QImage(len(colors), 1, QImage.Format_ARGB32_Premultiplied)
    for x, value in enumerate(colors):
        result.setPixelColor(x, 0, QColor(value, value, value, alpha))
    return result


def pixels(value):
    return np.frombuffer(value.constBits(), np.uint8).copy()


def test_model_defaults_and_round_trip_with_all_parameter_masks():
    default = modifier_from_dict({"type": "brightness_contrast"})
    assert isinstance(default, BrightnessContrastModifier)
    assert (default.brightness, default.contrast, default.intensity) == (0, 0, 100)
    source = BrightnessContrastModifier(brightness=35, contrast=-27, intensity=61,
        expanded=False, muted=True, parameter_masks={
            "brightness": ParameterMaskBinding("mask", -40, 70),
            "contrast": ParameterMaskBinding("mask", 90, -80),
            "intensity": ParameterMaskBinding("mask", 10, 83)})
    restored = modifier_from_dict(json.loads(json.dumps(source.to_dict())))
    assert restored == source
    restored.brightness, restored.contrast, restored.intensity = 400, -400, 500
    restored.validate()
    assert (restored.brightness, restored.contrast, restored.intensity) == (100, -100, 100)


@pytest.mark.parametrize("attribute", ["brightness", "contrast", "intensity"])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), -float("inf")])
def test_nonfinite_values_are_rejected(attribute, value):
    modifier = BrightnessContrastModifier()
    setattr(modifier, attribute, value)
    with pytest.raises(ValueError, match="finite"):
        modifier.to_dict()


@pytest.mark.parametrize("modifier", [BrightnessContrastModifier(),
    BrightnessContrastModifier(brightness=70, contrast=80, muted=True),
    BrightnessContrastModifier(brightness=70, contrast=80, intensity=0)])
def test_neutral_muted_and_zero_intensity_preserve_every_byte(modifier):
    source = image(list(range(256)), 173)
    assert np.array_equal(pixels(source), pixels(apply_modifier_stack(source, [modifier], (0, 0))))


@pytest.mark.parametrize("brightness,contrast,expected", [
    (100, 0, [255, 255, 255]), (-100, 0, [0, 0, 0]),
    (0, -100, [127, 127, 127]), (0, 100, [0, 128, 255]),
    (20, 50, [83, 179, 275]),
])
def test_brightness_offset_and_contrast_about_midgray(brightness, contrast, expected):
    source = image([64, 128, 192])
    result = apply_modifier_stack(source, [BrightnessContrastModifier(
        brightness=brightness, contrast=contrast)], (0, 0))
    actual = [result.pixelColor(x, 0).red() for x in range(3)]
    assert np.allclose(actual, np.clip(expected, 0, 255), atol=1)


@pytest.mark.parametrize("intensity", [1, 33, 77, 100])
def test_adjustment_preserves_all_alpha_values_and_transparent_pixels(intensity):
    source = QImage(256, 1, QImage.Format_ARGB32_Premultiplied)
    for alpha in range(256):
        source.setPixelColor(alpha, 0, QColor(66, 125, 190, alpha))
    result = apply_modifier_stack(source, [BrightnessContrastModifier(
        brightness=32, contrast=43, intensity=intensity)], (0, 0))
    assert [result.pixelColor(x, 0).alpha() for x in range(256)] == list(range(256))
    assert result.pixel(0, 0) == 0
    # Straight colors stay similar across opaque and semitransparent coverage.
    assert abs(result.pixelColor(128, 0).green() - result.pixelColor(255, 0).green()) <= 2


@pytest.mark.parametrize("parameter", ["brightness", "contrast", "intensity"])
def test_each_parameter_uses_its_mask_endpoints(parameter):
    source = image([64] * 4, 153)
    modifier = BrightnessContrastModifier(brightness=25, contrast=50, intensity=80)
    endpoints = (0, 100) if parameter == "intensity" else (-100, 100)
    modifier.parameter_masks[parameter] = ParameterMaskBinding("mask", *endpoints)
    field = np.array([[0, .25, .75, 1]], dtype=np.float32)
    result = apply_modifier_stack(source, [modifier], (0, 0), {(modifier.modifier_id, parameter): field})
    # Compare independent single-pixel settings at the four mask positions.
    for x, amount in enumerate(field[0]):
        reference = copy.deepcopy(modifier)
        reference.parameter_masks.clear()
        setattr(reference, parameter, endpoints[0] + float(amount) * (endpoints[1]-endpoints[0]))
        expected = apply_modifier_stack(source.copy(x, 0, 1, 1), [reference], (0, 0))
        assert result.pixel(x, 0) == expected.pixel(0, 0)


def test_modifier_order_changes_result_and_preset_preserves_local_bindings():
    source = image([80, 120, 160])
    brightness = BrightnessContrastModifier(brightness=15)
    contrast = BrightnessContrastModifier(contrast=80)
    forward = apply_modifier_stack(source, [brightness, contrast], (0, 0))
    reverse = apply_modifier_stack(source, [contrast, brightness], (0, 0))
    assert not np.array_equal(pixels(forward), pixels(reverse))
    preset = preset_from_modifier("Lift", brightness)
    preset = ModifierPreset.from_dict(json.loads(json.dumps(preset.to_dict())))
    contrast.parameter_masks["contrast"] = ParameterMaskBinding("local-mask", -50, 70)
    loaded = apply_modifier_preset(contrast, preset)
    assert loaded.modifier_id == contrast.modifier_id
    assert loaded.parameter_masks == contrast.parameter_masks
    assert (loaded.brightness, loaded.contrast) == (15, 0)


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=160, height=160, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 160, 160))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore(), ImageStore())
    yield canvas, chapter, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    output = QImage(160, 160, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(output)
    return output


def owner(scene, kind, font="Arial"):
    canvas, chapter, page = scene
    if kind == "layer":
        obj = chapter.add_layer(page.layer_id, "Panel", BoundGeometry.rectangle(20, 20, 80, 80))
        obj.fill_color, obj.border_width = "#FF606060", 0
        return "layer", obj.layer_id
    if kind == "image":
        obj = chapter.add_object(page.layer_id, ImageObject(x=20, y=20, pixel_width=60, pixel_height=60))
        source = QImage(60, 60, QImage.Format_ARGB32_Premultiplied)
        source.fill(QColor(96, 96, 96, 180))
        canvas.images.put_decoded(obj.object_id, "source.png", b"", source)
    elif kind == "text":
        obj = chapter.add_object(page.layer_id, TextObject(x=20, y=20, width=100, height=60,
            text="Lift", text_color="#FF606060", font_family=font, font_size=32, margin=0, layout_mode="free"))
    else:
        obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 120, 120)))
        canvas.tiles.paint_dab(obj.object_id, QPointF(50, 50), 50, QColor(96, 96, 96), opacity=180/255)
    return "object", obj.object_id


@pytest.mark.parametrize("kind", ["raster", "image", "text", "layer"])
def test_scene_export_and_saved_targets_support_adjustment(scene, text_outline_font_family, kind):
    canvas, chapter, _page = scene
    target = owner(scene, kind, text_outline_font_family)
    before = _qimage_premultiplied(render(canvas))
    modifier = BrightnessContrastModifier(brightness=35, contrast=20)
    chapter.add_modifier(modifier, [target])
    after = _qimage_premultiplied(render(canvas))
    assert np.array_equal(before[:, :, 3], after[:, :, 3])
    assert np.any(after[:, :, :3] > before[:, :, :3])
    restored = ChapterDocument.from_dict(json.loads(json.dumps(chapter.to_dict())))
    assert restored.modifier_target(*target).modifier_ids == [modifier.modifier_id]
    canvas.set_document(restored, canvas.tiles, canvas.images)
    assert np.array_equal(_qimage_premultiplied(render(canvas)), after)


def test_ui_menu_slider_order_one_undo_per_drag_and_parameter_mask(scene):
    canvas, chapter, _page = scene
    target = owner(scene, "raster")
    canvas.set_selection(*target)
    controls = ModifierControls(canvas)
    try:
        action = next(a for a in controls.add_button.menu().actions() if a.text() == "Brightness / Contrast")
        action.trigger()
        modifier_id = controls.common_ids()[0]
        card = controls._cards[modifier_id]
        assert [label.text() for label in card.findChildren(QLabel)
                if label.text() in {"Intensity", "Brightness", "Contrast"}] == ["Intensity", "Brightness", "Contrast"]
        sliders = card.findChildren(QSlider)
        assert [(s.minimum(), s.maximum()) for s in sliders] == [(0, 100), (-100, 100), (-100, 100)]
        revision = canvas.command_stack.revision
        sliders[1].sliderPressed.emit()
        for value in (12, 28, 43):
            sliders[1].setValue(value)
        assert canvas.command_stack.revision == revision
        sliders[1].sliderReleased.emit()
        assert canvas.command_stack.revision == revision + 1
        assert chapter.modifiers[modifier_id].brightness == 43
        canvas.command_stack.undo()
        assert canvas.chapter.modifiers[modifier_id].brightness == 0
        canvas.command_stack.redo()
        assert canvas.chapter.modifiers[modifier_id].brightness == 43
        mask = ToneMask()
        canvas.chapter.masks[mask.mask_id] = mask
        canvas.chapter.modifiers[modifier_id].parameter_masks["brightness"] = ParameterMaskBinding(mask.mask_id, -50, 70)
        controls.refresh()
        controls.set_mask_endpoints(modifier_id, "brightness", -30, 90, True)
        assert canvas.chapter.modifiers[modifier_id].parameter_masks["brightness"].white_value == 90
    finally:
        controls.deleteLater()


def test_raster_bake_prefix_preserves_later_adjustment_and_undo(scene):
    canvas, chapter, _page = scene
    target = owner(scene, "raster")
    canvas.set_selection(*target)
    first = BrightnessContrastModifier(brightness=20)
    second = BrightnessContrastModifier(contrast=45)
    for modifier in (first, second):
        chapter.add_modifier(modifier, [target])
    before = _qimage_premultiplied(render(canvas))
    apply_raster_modifiers(canvas, first.modifier_id)
    assert chapter.modifier_target(*target).modifier_ids == [second.modifier_id]
    # Applying a prefix stores its intermediate in 8-bit tiles. The later
    # contrast can amplify that one-level rounding at antialiased edges.
    baked = _qimage_premultiplied(render(canvas))
    np.testing.assert_allclose(baked, before, atol=2/255)
    assert np.array_equal(baked[:, :, 3], before[:, :, 3])
    canvas.command_stack.undo()
    assert canvas.chapter.modifier_target(*target).modifier_ids == [first.modifier_id, second.modifier_id]
    assert np.array_equal(_qimage_premultiplied(render(canvas)), before)


def test_canvas_parameter_mask_uses_document_coordinates_and_survives_reload(scene):
    canvas, chapter, page = scene
    target = owner(scene, "raster")
    contributor = chapter.add_object(page.layer_id, RasterObject(mask_only=True))
    canvas.tiles.paint_dab(contributor.object_id, QPointF(62, 50), 22,
                           QColor("white"), square=True, antialias=False)
    mask = ToneMask(contributors=[("object", contributor.object_id)], saved=True)
    chapter.masks[mask.mask_id] = mask
    modifier = BrightnessContrastModifier(parameter_masks={
        "brightness": ParameterMaskBinding(mask.mask_id, -100, 100)})
    chapter.add_modifier(modifier, [target])
    result = render(canvas)
    assert result.pixelColor(38, 50) == QColor(0, 0, 0, 180)
    assert result.pixelColor(62, 50) == QColor(255, 255, 255, 180)
    restored = ChapterDocument.from_dict(chapter.to_dict())
    canvas.set_document(restored, canvas.tiles, canvas.images)
    assert np.array_equal(pixels(render(canvas)), pixels(result))


def test_text_menu_adds_editable_adjustment_and_navigator_remains_bounded(scene, text_outline_font_family):
    canvas, chapter, _page = scene
    target = owner(scene, "text", text_outline_font_family)
    canvas.set_selection(*target)
    controls = ModifierControls(canvas)
    try:
        controls.refresh()
        action = next(a for a in controls.add_button.menu().actions() if a.text() == "Brightness / Contrast")
        assert action.isVisible()
        action.trigger()
        modifier = chapter.modifiers[controls.common_ids()[0]]
        assert isinstance(modifier, BrightnessContrastModifier)
        assert chapter.modifier_target(*target).text == "Lift"
        from comic_editor.ui.thumbnail_effects import capture_scale, scaled_modifiers
        modifier.brightness, modifier.contrast = 20, -45
        modifier.parameter_masks["contrast"] = ParameterMaskBinding("mask", -80, 60)
        canvas._interactive_render = True
        canvas._effect_preview_channel = "navigator"
        scale = capture_scale(canvas, QRectF(0, 0, 1080, 20000), [modifier])
        assert 0 < scale < .02
        assert scaled_modifiers([modifier], scale)[0] == modifier
    finally:
        controls.deleteLater()


def test_spatial_stage_and_background_snapshot_use_color_adjustment(scene):
    canvas, chapter, _page = scene
    source = QImage(20, 20, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(80, 80, 80))
    repeat = ArrayModifier(axis_start=(0, 0), axis_end=(30, 0), count=1)
    color = BrightnessContrastModifier(brightness=25, contrast=30)
    for modifier in (repeat, color):
        chapter.modifiers[modifier.modifier_id] = modifier
    result, bounds = render_stages(canvas, source, QRectF(0, 0, 20, 20),
                                   [repeat, color], QTransform())
    expected = apply_modifier_stack(source, [color], (0, 0)).pixelColor(10, 10)
    assert result.pixelColor(10, 10) == result.pixelColor(40, 10) == expected
    assert bounds.width() >= 50
    from comic_editor.ui.interactive_effects import render_interactive_stack
    large = source.scaled(256, 256)
    pending = []
    canvas._effect_jobs.request = lambda scope, key, compute, *args, **kwargs: (pending.append(compute), True)[1]
    canvas._interactive_render = True
    _, provisional = render_interactive_stack(canvas, large, [color], (0, 0),
                                              cache_key=("brightness",), scope=("test",))
    assert provisional and len(pending) == 1
    color.brightness = -100
    assert pending[0](lambda: False).pixelColor(10, 10) == expected
