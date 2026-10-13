"""Committed smudge journals refresh existing controls without copying journals."""
import copy

import pytest

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import DistortModifier
from comic_editor.core.smudge import default_tool_settings, fit_smudge_stroke
from comic_editor.ui.modifier_controls import ModifierCard
from comic_editor.ui.smudge_controls import SmudgeControls
from test_smudge_controls import editor, params


def test_true_smudge_release_and_undo_redo_keep_controls_and_exact_journal(editor, monkeypatch):
    canvas, owner, controls, identifier, _, _ = editor
    card = owner._cards[identifier]
    controls.properties_toggle.setChecked(False)
    before = canvas.chapter.to_dict()
    original_strokes = copy.deepcopy(params(editor)['strokes'])
    samples = [(13., 17., .2), (35., 26., .7), (55., 48., .5), (76., 63., .9)]
    tool_settings = default_tool_settings()
    expected = fit_smudge_stroke(samples, tool_settings)
    clears = []
    clear = controls.stroke_combo.clear
    def clear_list():
        clears.append(True)
        clear()
    def unexpected(*_args, **_kwargs):
        pytest.fail('A smudge parameter edit must update the existing controls')
    monkeypatch.setattr(controls.stroke_combo, 'clear', clear_list)
    monkeypatch.setattr(ModifierCard, '__init__', unexpected)
    monkeypatch.setattr(SmudgeControls, '__init__', unexpected)
    monkeypatch.setattr(owner, 'set_parameter', unexpected)
    canvas._modifier_handle_drag = {
        'smudge': True, 'distort': identifier, 'mode': 'draw', 'before': before,
        'samples': samples, 'tool_settings': tool_settings,
    }
    assert canvas._finish_smudge_handle()
    after = canvas.chapter.to_dict()
    stroke = params(editor)['strokes'][-1]
    expected['id'] = stroke['id']
    assert stroke == expected
    assert params(editor)['strokes'][:-1] == original_strokes
    assert controls.stroke_combo.currentData() == stroke['id']
    assert controls.point_combo.currentData() == 1
    assert controls.point_numbers['radius'].value.value() == pytest.approx(stroke['points'][1]['radius'])
    assert len(clears) == 1 and len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    assert controls.stroke_combo.count() == 2 and not controls.delete_button.isEnabled()
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after
    assert controls.stroke_combo.currentData() == stroke['id']
    assert len(clears) == 3
    assert owner._cards[identifier] is card
    assert card.modifier is canvas.chapter.modifiers[identifier]
    assert card.findChild(SmudgeControls) is controls
    assert controls.properties.isHidden() and not controls.properties_toggle.isChecked()


def test_read_only_control_refresh_does_not_copy_or_mutate_pressure_journals(editor, monkeypatch):
    _, owner, controls, _, _, _ = editor
    parameters = params(editor)
    before = copy.deepcopy(parameters)
    class RetainedJournal(list):
        def __deepcopy__(self, _memo):
            pytest.fail('Displaying scalar controls copied the retained pressure journal')
    journal = parameters['strokes'][0]['pressure'] = RetainedJournal(parameters['strokes'][0]['pressure'])
    monkeypatch.setattr(owner, 'set_parameter',
                        lambda *_a: pytest.fail('Refreshing controls emitted a model edit'))
    monkeypatch.setattr(controls.stroke_combo, 'clear',
                        lambda: pytest.fail('An unchanged stroke list was rebuilt'))
    for _ in range(3):
        controls.refresh_values()
        controls.refresh_selection()
        controls.sync_from_modifier()
    assert parameters == before and parameters['strokes'][0]['pressure'] is journal
    assert controls.point_numbers['flow'].value.value() == before['strokes'][0]['points'][0]['flow']


def test_parameter_slider_retains_capture_and_history_synchronizes_existing_widgets(editor, monkeypatch):
    canvas, owner, controls, identifier, _, _ = editor
    card = owner._cards[identifier]
    before = copy.deepcopy(params(editor))
    monkeypatch.setattr(controls.stroke_combo, 'clear',
                        lambda: pytest.fail('Scalar parameter editing rebuilt the stroke list'))
    slider = controls.point_numbers['flow'].slider
    slider.setSliderDown(True)
    for value in (230, 310, 450):
        slider.setValue(value)
        assert slider.isSliderDown()
        assert owner._cards[identifier] is card
        assert card.findChild(SmudgeControls) is controls
    slider.setSliderDown(False)
    assert len(canvas.command_stack._undo) == 1
    assert controls.point_numbers['flow'].value.value() == 45
    canvas.command_stack.undo()
    assert params(editor) == before
    assert controls.point_numbers['flow'].value.value() == before['strokes'][0]['points'][0]['flow']
    canvas.command_stack.redo()
    assert controls.point_numbers['flow'].value.value() == 45
    assert owner._cards[identifier] is card


def test_smudge_tool_fields_and_deleted_stroke_sync_without_replacing_controls(editor):
    canvas, owner, controls, identifier, _, _ = editor
    card = owner._cards[identifier]
    before = copy.deepcopy(params(editor))
    controls.tool_numbers['radius'].value.setValue(91)
    controls.pressure.setChecked(False)
    controls.opacity.value.setValue(38)
    assert controls.tool_numbers['radius'].value.value() == 91
    assert not controls.curve_buttons['radius'].isEnabled()
    assert params(editor)['strokes'] == before['strokes']
    controls.delete_stroke()
    assert not params(editor)['strokes'] and controls.stroke_combo.count() == 1
    canvas.command_stack.undo()
    assert params(editor)['strokes'] == before['strokes'] and controls.stroke_combo.count() == 2
    canvas.command_stack.undo()
    assert controls.opacity.value.value() == before['opacity']
    canvas.command_stack.undo()
    assert controls.pressure.isChecked() == before['tool_settings']['pressure_enabled']
    canvas.command_stack.undo()
    assert controls.tool_numbers['radius'].value.value() == before['tool_settings']['radius']
    assert params(editor) == before and owner._cards[identifier] is card


def test_modifier_type_change_keeps_ordinary_control_replacement(editor):
    canvas, owner, controls, identifier, _, _ = editor
    original = canvas.chapter.modifiers[identifier]
    card = owner._cards[identifier]
    canvas.chapter.modifiers[identifier] = DistortModifier(
        modifier_id=identifier, modifier_type='distort_mesh_warp', frame=(0, 0, 100, 100))
    change = ChangeSet((EntityChange(('modifier', identifier),
                                    frozenset({'modifier_type', 'parameters'})),))
    owner.apply_change(change)
    replacement = owner._cards[identifier]
    assert replacement is not card and replacement.findChild(SmudgeControls) is None
    # Even a parameters-only packet cannot adapt a different type of controls.
    canvas.chapter.modifiers[identifier] = original
    owner.apply_change(ChangeSet((EntityChange(('modifier', identifier), frozenset({'parameters'})),)))
    assert owner._cards[identifier] is not replacement
    assert owner._cards[identifier].findChild(SmudgeControls) is not controls

