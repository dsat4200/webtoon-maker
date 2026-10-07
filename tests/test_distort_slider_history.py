"""Real mouse groove presses snapshot before QSlider changes the model."""
import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QSlider

from test_distort_models_controls import editor


@pytest.mark.parametrize('name', ['distortSlider_angle', 'distortRadiusSlider'])
def test_groove_press_and_followup_drag_have_one_original_undo_snapshot(editor, qapp, name):
    canvas, controls, _layer, _objects = editor
    controls.add_modifier('distort_twirl')
    modifier = canvas.chapter.modifiers[canvas.active_modifier_id]
    before = modifier.to_dict()
    slider = controls._cards[modifier.modifier_id].findChild(QSlider, name)
    slider.resize(300, 24)
    commands = len(canvas.command_stack._undo)
    # A groove click can emit valueChanged while isSliderDown is still false.
    QTest.mousePress(slider, Qt.LeftButton, pos=QPoint(slider.width()-8, slider.height()//2))
    assert controls._parameter_before is not None
    assert len(canvas.command_stack._undo) == commands
    slider.setSliderDown(True)
    slider.setValue(slider.value()+slider.pageStep())
    QTest.mouseRelease(slider, Qt.LeftButton, pos=QPoint(slider.width()-30, slider.height()//2))
    assert controls._parameter_before is None
    assert canvas._modifier_parameter_drag_id is None
    assert len(canvas.command_stack._undo) == commands + 1
    assert modifier.to_dict() != before
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].to_dict() == before
