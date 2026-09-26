"""Wet ink restrictions remain visible without losing stored brush choices."""
import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics
from comic_editor.ui.brush_controls import BrushSettingsDialog


@pytest.mark.parametrize("mixing", ["blend", "running"])
def test_wet_modes_disable_incompatible_controls_without_changing_values(qapp, mixing):
    brush = BrushDefinition(mixing_mode=mixing, blending_mode="multiply",
                            dynamics={"opacity": BrushDynamics(pressure=True, minimum=.37)})
    dialog = BrushSettingsDialog(brush)
    assert not dialog.controls["blending_mode"].isEnabled()
    assert not dialog.opacity_dynamics_group.isEnabled()
    assert not dialog.dynamic_controls["opacity"]["minimum"].isEnabled()
    assert not dialog.ink_compatibility_note.isHidden()
    assert dialog.result_definition().to_dict() == brush.to_dict()
    mode = dialog.controls["mixing_mode"]
    mode.setCurrentIndex(mode.findData("smear"))
    assert dialog.controls["blending_mode"].isEnabled()
    assert dialog.opacity_dynamics_group.isEnabled()
    assert dialog.ink_compatibility_note.isHidden()
    assert dialog.result_definition().blending_mode == "multiply"
    assert dialog.result_definition().dynamics["opacity"] == brush.dynamics["opacity"]
    dialog.close()


def test_second_brush_blending_stays_owned_by_main_after_ink_mode_change(qapp):
    dialog = BrushSettingsDialog(BrushDefinition(mixing_mode="blend"), allow_dual=False)
    mode = dialog.controls["mixing_mode"]
    mode.setCurrentIndex(mode.findData("none"))
    assert not dialog.controls["blending_mode"].isEnabled()
    assert dialog.opacity_dynamics_group.isEnabled()
    dialog.close()


@pytest.mark.parametrize("allow_dual", [True, False])
def test_edge_blur_requires_after_stroke_without_losing_saved_width(qapp, allow_dual):
    brush = BrushDefinition(watercolor_edge=4, watercolor_blur=2.3456789,
                            watercolor_after=False)
    dialog = BrushSettingsDialog(brush, allow_dual=allow_dual)
    blur = dialog.controls["watercolor_blur"]
    assert not blur.isEnabled()
    assert ("after stroke" if allow_dual else "main brush") in blur.toolTip()
    assert dialog.result_definition().to_dict() == brush.to_dict()
    dialog.controls["watercolor_after"].setChecked(True)
    assert blur.isEnabled() is allow_dual
    assert dialog.result_definition().watercolor_blur == brush.watercolor_blur
    assert dialog.preview._definition.watercolor_blur == brush.watercolor_blur
    dialog.controls["watercolor_after"].setChecked(False)
    assert not blur.isEnabled()
    assert dialog.result_definition().to_dict() == brush.to_dict()
    dialog.close()
