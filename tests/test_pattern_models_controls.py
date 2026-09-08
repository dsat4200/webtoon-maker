import copy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDoubleSpinBox

from comic_editor.core.models import (
    HALFTONE_DOT_STYLES, HALFTONE_GRIDS, BoundGeometry, ChapterDocument,
    HalftoneModifier, ParameterMaskBinding, PixelateModifier, RasterObject,
    ToneMask, modifier_from_dict, sanitize_halftone_svg,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.mask_controls import MaskButton
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.pattern_controls import HalftoneControls, PixelateControls


SVG = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10"><path fill="#f04" d="M0 0H10V10H0Z"/></svg>'


@pytest.fixture
def pattern_scene(qapp):
    chapter = ChapterDocument(height=360)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 480, 360))
    layer = chapter.add_layer(page.layer_id, "Layer", BoundGeometry.rectangle(0, 0, 300, 300))
    obj = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 300, 300)))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", obj.object_id)
    controls = ModifierControls(canvas)
    yield canvas, controls, layer, obj
    canvas._effect_jobs.cancel()
    controls.deleteLater()
    canvas.deleteLater()


def test_pattern_round_trip_embeds_settings_and_preserves_shared_targets(pattern_scene):
    canvas, controls, layer, obj = pattern_scene
    chapter = canvas.chapter
    mask = ToneMask(name="Intensity", saved=True)
    chapter.masks[mask.mask_id] = mask
    halftone = HalftoneModifier(
        grid_type="stippling", dot_style="custom", custom_svg=SVG,
        custom_render_mode="original", color_mode="gradient", gradient_interpolation="oklch",
        gradient_stops=[[1, "#FFFFFF"], [.35, "#80FF0000"], [0, "#000"]],
        gamma=1.25, muted=True, expanded=False, smoothing_iterations=123,
        parameter_masks={"intensity": ParameterMaskBinding(mask.mask_id, 10., 80.)})
    chapter.add_modifier(halftone, [("object", obj.object_id), ("layer", layer.layer_id)])
    pixelate = PixelateModifier(pixel_size=17, brightness=150, saturation=200, blur=3.5)
    chapter.add_modifier(pixelate, [("object", obj.object_id)])
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.modifiers[halftone.modifier_id].to_dict() == halftone.to_dict()
    assert restored.modifiers[pixelate.modifier_id].to_dict() == pixelate.to_dict()
    assert restored.layers[layer.layer_id].modifier_ids == [halftone.modifier_id]
    assert restored.objects[obj.object_id].modifier_ids == [halftone.modifier_id, pixelate.modifier_id]
    payload = halftone.to_dict()
    payload["gradient_stops"][0][1] = "#FF123456"
    assert halftone.gradient_stops[0][1] == "#FF000000"


@pytest.mark.parametrize("kind,factory", [("halftone", HalftoneModifier), ("pixelate", PixelateModifier)])
def test_pattern_default_loading_and_finite_validation(kind, factory):
    restored = modifier_from_dict({"type": kind})
    expected = factory()
    assert type(restored) is factory
    assert restored.name == expected.name
    for attribute in restored.numeric_ranges():
        candidate = factory()
        setattr(candidate, attribute, float("nan"))
        with pytest.raises(ValueError, match="finite"):
            candidate.validate()
    candidate = factory(parameter_masks={
        "intensity": ParameterMaskBinding("mask", -99, 900),
        "blur": ParameterMaskBinding("mask", 0, 50),
    })
    candidate.validate()
    assert list(candidate.parameter_masks) == ["intensity"]
    assert candidate.parameter_masks["intensity"].black_value == 0
    assert candidate.parameter_masks["intensity"].white_value == 100


def test_halftone_validation_bounds_enums_and_gradient():
    gamma_zero = HalftoneModifier(gamma=0)
    gamma_zero.validate()
    assert gamma_zero.gamma == 0
    modifier = HalftoneModifier(clamp_min=.9, clamp_max=.2, level_min=2, level_max=-1,
                                sides=100, spacing=-4, base_resolution=99999,
                                gradient_stops=[[2, "#fff"], [-1, "#F00"]])
    modifier.validate()
    assert (modifier.clamp_min, modifier.clamp_max) == (.2, .9)
    assert (modifier.level_min, modifier.level_max) == (0., 1.)
    assert modifier.sides == 16 and modifier.spacing == 2 and modifier.base_resolution == 4000
    assert modifier.gradient_stops == [[0., "#FFFF0000"], [1., "#FFFFFFFF"]]
    for grid in HALFTONE_GRIDS:
        for style in HALFTONE_DOT_STYLES:
            HalftoneModifier(grid_type=grid, dot_style=style).validate()
    with pytest.raises(ValueError, match="Unknown halftone"):
        HalftoneModifier(grid_type="unknown").validate()
    with pytest.raises(ValueError, match="2 to 32"):
        HalftoneModifier(gradient_stops=[[0, "#000"]]).validate()


def test_custom_svg_is_embedded_static_and_bounded():
    unsafe = '''<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 10 10" onload="alert(1)">
      <script>bad()</script><image href="https://example.com/private"/>
      <path d="M0 0H10V10Z" style="fill:url(https://example.com/paint)"/>
      <use href="file:///private.svg"/>
    </svg>'''
    safe = sanitize_halftone_svg(unsafe)
    assert all(value not in safe for value in ("script", "image", "onload", "https://", "file:///"))
    assert "path" in safe
    assert sanitize_halftone_svg(safe) == safe
    with pytest.raises(ValueError, match="document types"):
        sanitize_halftone_svg('<!DOCTYPE svg [<!ENTITY secret SYSTEM "file:///private">]><svg/>')
    with pytest.raises(ValueError, match="256 KB"):
        sanitize_halftone_svg(" " * (256 * 1024 + 1) + "<svg/>")


def test_add_controls_decimal_keyboard_edit_undo_and_mask_scope(pattern_scene, qapp):
    canvas, controls, _, obj = pattern_scene
    controls.add_modifier("halftone")
    identifier = obj.modifier_ids[-1]
    modifier = canvas.chapter.modifiers[identifier]
    panel = controls.findChild(HalftoneControls)
    assert panel is not None
    assert len(controls._cards[identifier].findChildren(MaskButton)) == 1
    controls.show()
    panel.sections["Image sampling"].toggle.setChecked(True)
    qapp.processEvents()
    value = panel.findChild(QDoubleSpinBox, "patternValue_gamma")
    value.setFocus()
    QTest.keyClick(value, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClicks(value, "1.27")
    QTest.keyClick(value, Qt.Key_Return)
    assert modifier.gamma == pytest.approx(1.27)
    assert panel.numbers["gamma"].slider.value() == 127
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[identifier].gamma == 1
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[identifier].gamma == pytest.approx(1.27)
    controls.add_modifier("pixelate")
    pixelate = canvas.chapter.modifiers[canvas.chapter.objects[obj.object_id].modifier_ids[-1]]
    panel = controls.findChild(PixelateControls)
    panel.numbers["brightness"].value.setValue(150)
    panel.numbers["blur"].value.setValue(4.5)
    assert pixelate.brightness == 150 and pixelate.blur == 4.5
    assert len(controls._cards[pixelate.modifier_id].findChildren(MaskButton)) == 1
    panel.reset_all()
    assert pixelate.brightness == 0 and pixelate.blur == 0 and pixelate.pixel_size == 8
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[pixelate.modifier_id].brightness == 150
    assert canvas.chapter.modifiers[pixelate.modifier_id].blur == 4.5
    controls.close()


def test_halftone_all_modes_svg_gradient_edit_reset_undo(pattern_scene):
    canvas, controls, _, obj = pattern_scene
    controls.add_modifier("halftone")
    modifier = canvas.chapter.modifiers[obj.modifier_ids[-1]]
    identifier = modifier.modifier_id
    panel = controls.findChild(HalftoneControls)
    for grid in HALFTONE_GRIDS:
        combo = panel.combos["grid_type"]
        combo.setCurrentIndex(combo.findData(grid))
        assert modifier.grid_type == grid
    for style in HALFTONE_DOT_STYLES:
        combo = panel.combos["dot_style"]
        combo.setCurrentIndex(combo.findData(style))
        assert modifier.dot_style == style
    panel.set_custom_svg(SVG)
    assert "path" in modifier.custom_svg
    gradient = panel.gradient
    gradient.add_stop()
    assert len(modifier.gradient_stops) == 3
    gradient.select(0)
    first_color = modifier.gradient_stops[0][1]
    gradient.move_color(1)
    assert modifier.gradient_stops[1][1] == first_color
    gradient.move_color(-1)
    assert modifier.gradient_stops[0][1] == first_color
    gradient.move_stop(gradient.preview.selected, .7)
    assert any(stop[0] == .7 for stop in modifier.gradient_stops)
    gradient.reverse_stops()
    assert any(stop[0] == pytest.approx(.3) for stop in modifier.gradient_stops)
    gradient.distribute_stops()
    assert [stop[0] for stop in modifier.gradient_stops] == [0, .5, 1]
    gradient.remove_stop()
    assert len(modifier.gradient_stops) == 2
    original_colors = modifier.foreground, modifier.background
    panel.swap_color_values()
    assert (modifier.foreground, modifier.background) == original_colors[::-1]
    before = copy.deepcopy(modifier.to_dict())
    panel.reset_section("Dots and lines")
    assert modifier.dot_style == "circle" and modifier.custom_svg == ""
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[identifier].to_dict() == before
