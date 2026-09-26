"""Smudge point editing and project tool presets preserve existing gestures."""
import copy

import pytest
from PySide6.QtWidgets import QDialog, QInputDialog, QMessageBox, QSlider

from comic_editor.core.models import BoundGeometry, ChapterDocument, DistortModifier, RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.pressure import PressureCurve
from comic_editor.core.settings import EditorSettings
from comic_editor.core.smudge import default_tool_settings, fit_smudge_stroke
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierControls
from comic_editor.ui.modifier_presets import ModifierPresetController
from comic_editor.ui.pressure_curve_editor import PressureCurveEditor
from comic_editor.ui.smudge_controls import SmudgeControls


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    repository = SeriesRepository(tmp_path / 'smudge-project')
    series = repository.create('Smudge test')
    chapter = ChapterDocument(width=100, height=100, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 100, 100))
    obj = chapter.add_object(page.layer_id, RasterObject())
    stroke = fit_smudge_stroke([(10, 10, .2), (30, 28, .7), (70, 60, .9)], default_tool_settings())
    modifier = DistortModifier(modifier_type='distort_smudge', frame=(0, 0, 100, 100),
                              parameters={'strokes': [stroke]})
    chapter.add_modifier(modifier, [('object', obj.object_id)])
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection('object', obj.object_id)
    canvas.modifier_mode = True
    canvas.active_modifier_id = modifier.modifier_id
    canvas.smudge_selected_stroke_id = stroke['id']
    canvas.smudge_selected_point_index = 0
    owner = ModifierControls(canvas)
    context = [series, repository]
    ModifierPresetController(owner, lambda: tuple(context))
    owner.refresh()
    controls = owner.findChild(SmudgeControls)
    assert controls is not None
    errors = []
    monkeypatch.setattr(QMessageBox, 'warning', lambda _owner, _title, text: errors.append(text))
    yield canvas, owner, controls, modifier.modifier_id, context, errors
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()
    canvas.deleteLater()


def params(editor):
    canvas, _, _, identifier, _, _ = editor
    return canvas.chapter.modifiers[identifier].parameters


def test_point_drag_single_undo_and_other_endpoint_pressure_unchanged(editor):
    canvas, owner, controls, identifier, _, _ = editor
    before = copy.deepcopy(params(editor))
    slider = controls.findChild(QSlider, 'smudgeSlider_point_radius')
    slider.setSliderDown(True)
    slider.setValue(420)
    slider.setValue(850)
    assert canvas._smudge_parameter_drag_id == identifier
    slider.setSliderDown(False)
    assert canvas._smudge_parameter_drag_id is None
    after = params(editor)
    assert after['strokes'][0]['points'][0]['radius'] == 85
    assert after['strokes'][0]['points'][1] == before['strokes'][0]['points'][1]
    assert after['strokes'][0]['pressure_settings'] == before['strokes'][0]['pressure_settings']
    assert after['strokes'][0]['pressure'] == before['strokes'][0]['pressure']
    canvas.command_stack.undo()
    assert params(editor) == before
    canvas.command_stack.redo()
    assert params(editor)['strokes'][0]['points'][0]['radius'] == 85


def test_replacing_controls_finishes_drag_and_document_replacement_cancels(editor, monkeypatch):
    canvas, owner, controls, _, _, _ = editor
    before = canvas.chapter.to_dict()
    slider = controls.findChild(QSlider, 'smudgeSlider_point_flow')
    slider.setSliderDown(True)
    slider.setValue(230)
    owner.refresh()
    assert owner._parameter_before is None
    assert canvas._smudge_parameter_drag_id is None
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    controls = owner.findChild(SmudgeControls)
    # Locate the live card, since deleteLater retains the old widget until events run.
    controls = next(iter(owner._cards.values())).findChild(SmudgeControls)
    slider = controls.findChild(QSlider, 'smudgeSlider_point_flow')
    slider.setSliderDown(True)
    slider.setValue(300)
    monkeypatch.setattr(owner, '_push', lambda *_: pytest.fail('committed across chapter replacement'))
    canvas.replace_chapter(before)
    assert owner._parameter_before is None
    assert owner._smudge_parameter_chapter is None
    assert canvas._smudge_parameter_drag_id is None


def test_point_type_and_opacity_are_distinct_from_stroke_strength(editor):
    canvas, _, controls, _, _, _ = editor
    before = copy.deepcopy(params(editor))
    controls.type_combo.setCurrentIndex(controls.type_combo.findData('vector'))
    assert params(editor)['strokes'][0]['points'][0]['point_type'] == 'vector'
    controls.opacity.value.setValue(38)
    assert params(editor)['opacity'] == 38
    assert params(editor)['strokes'][0]['points'][0]['strength'] == before['strokes'][0]['points'][0]['strength']
    canvas.command_stack.undo()
    assert params(editor)['opacity'] == 100
    canvas.command_stack.undo()
    assert params(editor) == before


def test_tool_settings_pressure_channels_and_curves_do_not_change_strokes(editor, monkeypatch):
    canvas, _, controls, _, _, _ = editor
    strokes = copy.deepcopy(params(editor)['strokes'])
    controls.tool_numbers['radius'].value.setValue(91)
    controls.pressure_channels['flow'].setChecked(True)
    controls.pressure_channels['strength'].setChecked(False)
    original_curves = copy.deepcopy(params(editor)['tool_settings'])

    def edit(dialog):
        curve = dialog.findChild(PressureCurveEditor).curve()
        curve.minimum = .25
        curve.control_x = .7
        return QDialog.Accepted

    monkeypatch.setattr(QDialog, 'exec', edit)
    controls.edit_curve('flow')
    settings = params(editor)['tool_settings']
    assert PressureCurve.from_dict(settings['flow_curve']).minimum == .25
    assert settings['radius_curve'] == original_curves['radius_curve']
    assert settings['strength_curve'] == original_curves['strength_curve']
    assert settings['pressure_flow'] and not settings['pressure_strength']
    assert params(editor)['strokes'] == strokes
    canvas.command_stack.undo()
    assert params(editor)['tool_settings'] == original_curves


def test_tool_preset_roundtrip_load_only_settings_and_one_undo(editor):
    canvas, _, controls, _, (series, repository), errors = editor
    strokes = copy.deepcopy(params(editor)['strokes'])
    controls.set_tool_value('radius', 71)
    assert controls.presets.save('Wide gentle brush')
    preset = series.smudge_tool_presets[0]
    assert set(preset) == {'id', 'name', 'settings'}
    assert repository.load_series().smudge_tool_presets == series.smudge_tool_presets
    controls.set_tool_value('radius', 13)
    controls.set_opacity(43)
    before = copy.deepcopy(params(editor))
    assert controls.presets.load(preset['id'])
    assert params(editor)['tool_settings']['radius'] == 71
    assert params(editor)['strokes'] == strokes
    assert params(editor)['opacity'] == 43
    assert params(editor)['tool_settings'] is not preset['settings']
    canvas.command_stack.undo()
    assert params(editor) == before
    assert not errors


def test_preset_save_failure_rolls_back_and_project_switch_rejects(editor, monkeypatch):
    _, _, controls, _, context, errors = editor
    series, repository = context
    monkeypatch.setattr(repository, 'save_series', lambda *_: (_ for _ in ()).throw(OSError('disk test')))
    assert not controls.presets.save('Not saved')
    assert not series.smudge_tool_presets
    assert errors

    def switch(*_args, **_kwargs):
        context[0] = copy.deepcopy(series)
        return 'Wrong project', True

    monkeypatch.setattr(QInputDialog, 'getText', switch)
    assert not controls.presets.save()
    assert not context[0].smudge_tool_presets and not series.smudge_tool_presets


def test_properties_collapse_keeps_tool_settings_available(editor):
    _, _, controls, _, _, _ = editor
    controls.properties_toggle.setChecked(False)
    assert controls.properties.isHidden()
    assert not controls.tool_numbers['radius'].isHidden()
    assert not controls.pressure.isHidden()
    assert not controls.preset_combo.isHidden()


def test_real_selection_signal_updates_existing_widgets_and_delete_is_undoable(editor):
    canvas, owner, controls, identifier, _, _ = editor
    stroke = params(editor)['strokes'][0]
    before = copy.deepcopy(params(editor))
    controls.point_combo.setCurrentIndex(1)
    assert canvas.smudge_selected_point_index == 1
    assert controls.point_numbers['radius'].value.value() == stroke['points'][1]['radius']
    canvas.smudge_selected_point()['flow'] = 31
    canvas.smudgePointChanged.emit()
    assert owner._cards[identifier].findChild(SmudgeControls) is controls
    assert controls.point_numbers['flow'].value.value() == 31
    controls.delete_button.click()
    assert params(editor)['strokes'] == []
    assert not controls.delete_button.isEnabled()
    canvas.command_stack.undo()
    assert params(editor)['strokes'][0]['points'][1]['flow'] == 31
    assert params(editor)['strokes'][0]['points'][0] == before['strokes'][0]['points'][0]


def test_panel_delete_works_outside_modifier_mode_with_one_undo(editor):
    canvas, _, controls, _, _, _ = editor
    canvas.modifier_mode = False
    before = copy.deepcopy(params(editor))
    controls.delete_button.click()
    assert not params(editor)['strokes']
    assert not canvas.modifier_mode
    canvas.command_stack.undo()
    assert params(editor) == before
