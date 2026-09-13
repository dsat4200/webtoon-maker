"""Gradient type controls and presets share one undoable model change."""
import copy

import pytest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientRampPreset, ColorGradientStop, SeriesDocument, ToneMask,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.gradient_tools import BUILTIN_PRIMARY_SECONDARY_ID
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def window(qapp, monkeypatch):
    widget = MainWindow()
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 400))
    chapter.add_object(page.layer_id, ColorFillGradientObject())
    widget.series = SeriesDocument()
    monkeypatch.setattr(widget, "_schedule_series_preferences_save", lambda **_kwargs: None)
    widget._set_chapter(chapter, TileStore())
    yield widget
    widget._dirty = False
    widget.canvas._effect_jobs.cancel()
    widget.close()
    widget.deleteLater()


def selected(window, mask):
    canvas = window.canvas
    if mask:
        owner = ToneMask(saved=True, gradient=ColorFillGradientObject(mask_only=True))
        window.chapter.masks[owner.mask_id] = owner
        window._enter_mask_mode(owner.mask_id)
        canvas.set_tool(ToolKind.GRADIENT)
        obj = canvas.active_mask_gradient()
    else:
        obj = next(iter(window.chapter.objects.values()))
        canvas.set_selection("object", obj.object_id)
    window._sync_contextual_ribbon()
    window.gradient_tools_controls.refresh()
    return obj


def change_shape(controls, shape):
    controls.gradient_shape.setCurrentIndex(controls.gradient_shape.findData(shape))


def test_gradient_type_available_before_creation_without_model_undo(window):
    canvas = window.canvas
    canvas.set_selection("layer", window.chapter.root_page_ids[0])
    canvas.set_tool(ToolKind.GRADIENT)
    window._sync_contextual_ribbon()
    controls = window.gradient_tools_controls
    assert not window.gradient_type_group.isHidden()
    assert controls.gradient_shape.isEnabled()
    assert [controls.gradient_shape.itemText(i) for i in range(2)] == ["Linear", "Circular"]
    before = window.chapter.to_dict()
    revision = canvas.command_stack.revision
    change_shape(controls, "circular")
    assert canvas._gradient_tool_shape == "circular"
    assert canvas.command_stack.revision == revision
    assert window.chapter.to_dict() == before


@pytest.mark.parametrize("mask", [False, True])
def test_selected_gradient_type_is_one_undo_and_preserves_other_options(window, mask):
    obj = selected(window, mask)
    obj.line_field.direction_mode = "parallel" if mask else "perpendicular"
    obj.line_field.perpendicular_distance = 73
    obj.line_field.reverse_direction = True
    controls = window.gradient_tools_controls
    controls.refresh()
    original = copy.deepcopy(obj.to_dict())
    revision = window.canvas.command_stack.revision
    owner_revision = window.chapter.masks[window.canvas.active_tone_mask_id].revision if mask else None
    change_shape(controls, "circular")
    assert obj.gradient_shape == "circular"
    assert obj.line_field.to_dict() == original["line_field"]
    assert obj.ramp.to_dict() == original["ramp"]
    assert controls.direction_row.isHidden()
    assert controls.distance_row.isHidden()
    assert window.canvas.command_stack.revision == revision+1
    if mask:
        assert window.chapter.masks[window.canvas.active_tone_mask_id].revision == owner_revision+1
    window.canvas.command_stack.undo()
    restored = controls.selected_gradient()
    assert restored.to_dict() == original
    controls.refresh()
    assert controls.gradient_shape.currentData() == "linear"
    window.canvas.command_stack.redo()
    assert controls.selected_gradient().gradient_shape == "circular"


@pytest.mark.parametrize("mask", [False, True])
def test_preset_load_restores_type_and_ramp_atomically(window, mask):
    obj = selected(window, mask)
    preset = ColorGradientRampPreset(name="Circular color", gradient_shape="circular",
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#FF00FF00"),
                                     ColorGradientStop(position=1, color="#400000FF")]))
    window.series.gradient_ramp_presets.append(preset)
    original = copy.deepcopy(obj.to_dict())
    revision = window.canvas.command_stack.revision
    owner_revision = window.chapter.masks[window.canvas.active_tone_mask_id].revision if mask else None
    window._load_gradient_preset(preset.preset_id)
    assert obj.gradient_shape == "circular"
    assert obj.ramp.to_dict() == preset.ramp.to_dict()
    assert window.gradient_tools_controls.gradient_shape.currentData() == "circular"
    assert window.canvas.command_stack.revision == revision+1
    if mask:
        assert window.chapter.masks[window.canvas.active_tone_mask_id].revision == owner_revision+1
        assert window.canvas._tone_mask_overlay_key is None
    window.canvas.command_stack.undo()
    assert window.gradient_tools_controls.selected_gradient().to_dict() == original
    window.canvas.command_stack.redo()
    restored = window.gradient_tools_controls.selected_gradient()
    assert restored.gradient_shape == "circular"
    restored.ramp.stops[0].color = "#FFFFFFFF"
    assert preset.ramp.stops[0].color == "#FF00FF00"


def test_new_and_saved_presets_capture_type_and_builtin_resets_linear(window):
    obj = selected(window, False)
    change_shape(window.gradient_tools_controls, "circular")
    window._add_gradient_preset()
    preset = window._gradient_preset_by_id(obj.loaded_preset_id)
    assert preset.gradient_shape == "circular"
    change_shape(window.gradient_tools_controls, "linear")
    window._save_gradient_preset(preset.preset_id)
    assert preset.gradient_shape == "linear"
    change_shape(window.gradient_tools_controls, "circular")
    window._load_gradient_preset(BUILTIN_PRIMARY_SECONDARY_ID)
    assert obj.gradient_shape == "linear"
    assert window.gradient_tools_controls.gradient_shape.currentData() == "linear"


def test_legacy_fields_remain_available_and_keep_dormant_circular_type(window):
    obj = selected(window, False)
    controls = window.gradient_tools_controls
    change_shape(controls, "circular")
    for kind in ("radial", "parent_shape"):
        controls.field_type.setCurrentIndex(controls.field_type.findData(kind))
        assert obj.field_type == kind
        assert controls.field_type.isEnabled()
        assert not controls.gradient_shape.isEnabled()
        assert obj.gradient_shape == "circular"
    controls.field_type.setCurrentIndex(controls.field_type.findData("line"))
    assert controls.gradient_shape.isEnabled()
    assert controls.gradient_shape.currentData() == "circular"
