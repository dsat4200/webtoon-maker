"""Controls for editable smudge paths and defaults for future strokes."""
from __future__ import annotations

import copy

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QHBoxLayout, QLabel, QPushButton, QSlider, QToolButton, QVBoxLayout,
    QWidget, QSizePolicy,
)

from comic_editor.core.pressure import PressureCurve
from comic_editor.core.smudge import validate_tool_settings
from comic_editor.ui.pressure_curve_editor import PressureCurveEditor
from comic_editor.ui.smudge_presets import SmudgeToolPresets


def _set(widget, method, value):
    previous = widget.blockSignals(True)
    try:
        getattr(widget, method)(value)
    finally:
        widget.blockSignals(previous)


def _combo(parent, name):
    combo = QComboBox(parent)
    combo.setObjectName(name)
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(8)
    combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return combo


class SmudgeNumber(QWidget):
    def __init__(self, label, key, value, minimum, maximum, controls, setter):
        super().__init__(controls)
        self.setObjectName('smudgeRow_' + key)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        row = QHBoxLayout()
        row.addWidget(QLabel(label, self), 1)
        self.value = QDoubleSpinBox(self)
        self.value.setObjectName('smudgeValue_' + key)
        self.value.setAccessibleName(label)
        self.value.setRange(minimum, maximum)
        self.value.setDecimals(1)
        self.value.setKeyboardTracking(False)
        self.value.setSuffix(' px' if 'radius' in key else '%')
        row.addWidget(self.value)
        layout.addLayout(row)
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setObjectName('smudgeSlider_' + key)
        self.slider.setAccessibleName(label)
        self.slider.setRange(round(minimum * 10), round(maximum * 10))
        layout.addWidget(self.slider)
        self.set_value(value)
        self.slider.sliderPressed.connect(controls.owner.begin_parameter_drag)
        self.slider.sliderReleased.connect(controls.owner.finish_parameter_drag)

        def slider_changed(position):
            _set(self.value, 'setValue', position / 10)
            setter(position / 10, not self.slider.isSliderDown())

        def value_changed(number):
            _set(self.slider, 'setValue', round(number * 10))
            setter(number, True)

        self.slider.valueChanged.connect(slider_changed)
        self.value.valueChanged.connect(value_changed)

    def set_value(self, value):
        _set(self.value, 'setValue', float(value))
        _set(self.slider, 'setValue', round(float(value) * 10))


class SmudgeControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.owner = owner
        self.modifier_id = modifier.modifier_id
        self.setObjectName('smudgeControls')
        form = QVBoxLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(5)
        self.opacity = SmudgeNumber('Opacity', 'opacity', modifier.parameters.get('opacity', 100),
                                   0, 100, self, self.set_opacity)
        self.opacity.setToolTip('Blend the smudged image with the original. This does not change stroke strength.')
        form.addWidget(self.opacity)

        self.properties_toggle = QToolButton(self)
        self.properties_toggle.setObjectName('smudgeBezierPropertiesToggle')
        self.properties_toggle.setText('Bézier properties')
        self.properties_toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.properties_toggle.setCheckable(True)
        self.properties_toggle.setChecked(True)
        self.properties_toggle.setArrowType(Qt.DownArrow)
        form.addWidget(self.properties_toggle)
        self.properties = QWidget(self)
        props = QVBoxLayout(self.properties)
        props.setContentsMargins(0, 0, 0, 0)
        form.addWidget(self.properties)
        self.properties_toggle.toggled.connect(self._toggle_properties)
        self.stroke_combo = _combo(self, 'smudgeSelectedStroke')
        self.stroke_combo.setAccessibleName('Selected stroke')
        props.addWidget(self.stroke_combo)
        self.point_combo = _combo(self, 'smudgeSelectedPoint')
        self.point_combo.setAccessibleName('Selected point')
        self.point_combo.addItem('Start point', 0)
        self.point_combo.addItem('End point', 1)
        props.addWidget(self.point_combo)
        self.type_combo = _combo(self, 'smudgePointType')
        self.type_combo.setAccessibleName('Point type')
        self.type_combo.addItem('Bézier', 'bezier')
        self.type_combo.addItem('Vector', 'vector')
        props.addWidget(self.type_combo)
        self.point_numbers = {}
        for key, label, minimum, maximum in [('radius', 'Radius', .1, 4096),
                                            ('flow', 'Flow', 0, 100), ('strength', 'Strength', 0, 100)]:
            widget = SmudgeNumber(label, 'point_' + key, minimum, minimum, maximum, self,
                                 lambda value, commit, key=key: self.set_point_value(key, value, commit))
            self.point_numbers[key] = widget
            props.addWidget(widget)
        self.delete_button = QPushButton('Delete selected stroke', self)
        self.delete_button.setObjectName('smudgeDeleteStroke')
        props.addWidget(self.delete_button)
        self.stroke_combo.currentIndexChanged.connect(self.select_stroke)
        self.point_combo.currentIndexChanged.connect(self.select_point)
        self.type_combo.currentIndexChanged.connect(
            lambda _index: self.set_point_value('point_type', self.type_combo.currentData()))
        self.delete_button.clicked.connect(self.delete_stroke)

        form.addWidget(QLabel('Tool settings', self))
        explanation = QLabel('Defaults for new strokes. Existing strokes keep their settings.', self)
        explanation.setWordWrap(True)
        form.addWidget(explanation)
        self.tool_numbers = {}
        for key, label, minimum, maximum in [('radius', 'Radius', .1, 4096),
                                            ('flow', 'Flow', 0, 100), ('strength', 'Strength', 0, 100)]:
            widget = SmudgeNumber(label, 'tool_' + key, minimum, minimum, maximum, self,
                                 lambda value, commit, key=key: self.set_tool_value(key, value, commit))
            self.tool_numbers[key] = widget
            form.addWidget(widget)
        self.pressure = QCheckBox('Enable pressure', self)
        self.pressure.setObjectName('smudgePressureEnabled')
        self.pressure.toggled.connect(lambda enabled: self.set_tool_value('pressure_enabled', enabled))
        form.addWidget(self.pressure)
        self.pressure_channels = {}
        self.curve_buttons = {}
        for key in ('radius', 'flow', 'strength'):
            row = QHBoxLayout()
            check = QCheckBox(key.title(), self)
            check.setObjectName('smudgePressure_' + key)
            check.toggled.connect(lambda enabled, key=key: self.set_tool_value('pressure_' + key, enabled))
            row.addWidget(check, 1)
            button = QPushButton('Edit curve…', self)
            button.setObjectName('smudgeCurve_' + key)
            button.setAccessibleName(key.title() + ' pressure curve')
            button.clicked.connect(lambda _checked=False, key=key: self.edit_curve(key))
            row.addWidget(button)
            form.addLayout(row)
            self.pressure_channels[key], self.curve_buttons[key] = check, button

        form.addWidget(QLabel('Tool preset', self))
        self.presets = SmudgeToolPresets(self)
        self.preset_combo = _combo(self, 'smudgeToolPreset')
        form.addWidget(self.preset_combo)
        row = QHBoxLayout()
        self.load_preset = QPushButton('Load', self)
        self.load_preset.setObjectName('smudgeLoadToolPreset')
        self.save_preset = QPushButton('Save as…', self)
        self.save_preset.setObjectName('smudgeSaveToolPreset')
        row.addWidget(self.load_preset)
        row.addWidget(self.save_preset)
        form.addLayout(row)
        self.load_preset.clicked.connect(lambda: self.presets.load(self.preset_combo.currentData()))
        self.save_preset.clicked.connect(lambda: self.presets.save())
        self.refresh_values()
        self.refresh_presets()
        self.refresh_selection()
        for name in ('smudgeSelectedChanged', 'smudgePointChanged'):
            signal = getattr(owner.canvas, name, None)
            if signal is not None:
                signal.connect(self.refresh_selection)

    def current_modifier(self):
        chapter = self.owner.canvas.chapter
        modifier = chapter.modifiers.get(self.modifier_id) if chapter else None
        return modifier if modifier is not None and modifier.modifier_type == 'distort_smudge' else None

    def _parameters(self):
        modifier = self.current_modifier()
        return copy.deepcopy(modifier.parameters) if modifier is not None else None

    def _write(self, parameters, commit=True):
        if parameters is not None:
            self.owner.set_parameter(self.modifier_id, 'parameters', parameters, commit)

    def set_opacity(self, value, commit=True):
        parameters = self._parameters()
        if parameters is not None:
            parameters['opacity'] = value
            self._write(parameters, commit)

    def set_tool_settings(self, settings, commit=True):
        parameters = self._parameters()
        if parameters is not None:
            parameters['tool_settings'] = validate_tool_settings(settings)
            self._write(parameters, commit)

    def set_tool_value(self, key, value, commit=True):
        parameters = self._parameters()
        if parameters is None:
            return
        settings = validate_tool_settings(parameters.get('tool_settings', {}))
        settings[key] = copy.deepcopy(value)
        self.set_tool_settings(settings, commit)
        self.refresh_values()

    def selected(self, parameters=None):
        parameters = self._parameters() if parameters is None else parameters
        canvas = self.owner.canvas
        identifier = getattr(canvas, 'smudge_selected_stroke_id', '')
        index = getattr(canvas, 'smudge_selected_point_index', 0)
        for stroke in (parameters or {}).get('strokes', []):
            if stroke['id'] == identifier and 0 <= index < len(stroke['points']):
                return stroke, index
        return None, 0

    def set_point_value(self, key, value, commit=True):
        parameters = self._parameters()
        stroke, index = self.selected(parameters)
        if stroke is None:
            return
        stroke['points'][index][key] = value
        self._write(parameters, commit)
        self.refresh_selection()

    def select_stroke(self, _index):
        identifier = self.stroke_combo.currentData()
        if identifier:
            self.owner.activate_modifier(self.modifier_id)
            self.owner.canvas.smudge_select(identifier, 0)

    def select_point(self, _index):
        identifier = self.stroke_combo.currentData()
        if identifier:
            self.owner.activate_modifier(self.modifier_id)
            self.owner.canvas.smudge_select(identifier, self.point_combo.currentData())

    def delete_stroke(self):
        parameters = self._parameters()
        stroke, _ = self.selected(parameters)
        if stroke is not None:
            self.owner.finish_parameter_drag()
            parameters['strokes'] = [item for item in parameters['strokes'] if item['id'] != stroke['id']]
            self._write(parameters)
            self.owner.canvas.smudge_select('', 0)
            self.refresh_selection()

    def refresh_selection(self):
        parameters = self._parameters() or {}
        stroke, index = self.selected(parameters)
        previous = self.stroke_combo.blockSignals(True)
        self.stroke_combo.clear()
        self.stroke_combo.addItem('Select a stroke', '')
        for number, item in enumerate(parameters.get('strokes', []), 1):
            self.stroke_combo.addItem(f'Stroke {number}', item['id'])
        self.stroke_combo.setCurrentIndex(self.stroke_combo.findData(stroke['id']) if stroke else 0)
        self.stroke_combo.blockSignals(previous)
        enabled = stroke is not None
        for widget in [self.point_combo, self.type_combo, self.delete_button, *self.point_numbers.values()]:
            widget.setEnabled(enabled)
        if stroke is not None:
            point = stroke['points'][index]
            _set(self.point_combo, 'setCurrentIndex', index)
            _set(self.type_combo, 'setCurrentIndex', self.type_combo.findData(point['point_type']))
            for key, widget in self.point_numbers.items():
                widget.set_value(point[key])

    def refresh_values(self):
        parameters = self._parameters() or {}
        settings = validate_tool_settings(parameters.get('tool_settings', {}))
        self.opacity.set_value(parameters.get('opacity', 100))
        for key, widget in self.tool_numbers.items():
            widget.set_value(settings[key])
        _set(self.pressure, 'setChecked', settings['pressure_enabled'])
        for key, widget in self.pressure_channels.items():
            _set(widget, 'setChecked', settings['pressure_' + key])
            widget.setEnabled(settings['pressure_enabled'])
            self.curve_buttons[key].setEnabled(settings['pressure_enabled'] and settings['pressure_' + key])

    def edit_curve(self, key):
        modifier = self.current_modifier()
        if modifier is None:
            return
        chapter = self.owner.canvas.chapter
        settings = validate_tool_settings(modifier.parameters.get('tool_settings', {}))
        dialog = QDialog(self)
        dialog.setWindowTitle(key.title() + ' pressure curve')
        layout = QVBoxLayout(dialog)
        editor = PressureCurveEditor(key.title() + ' pressure curve',
                                     PressureCurve.from_dict(settings[key + '_curve']), dialog)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() == QDialog.Accepted and self.owner.canvas.chapter is chapter and self.current_modifier() is modifier:
            self.set_tool_value(key + '_curve', editor.curve().to_dict())

    def refresh_presets(self, selected=None):
        selected = selected or self.preset_combo.currentData()
        self.preset_combo.clear()
        for item in self.presets.items():
            self.preset_combo.addItem(item['name'], item['id'])
        if selected:
            self.preset_combo.setCurrentIndex(self.preset_combo.findData(selected))
        self.load_preset.setEnabled(self.preset_combo.count() > 0)
        series, repository = self.presets.context()
        self.save_preset.setEnabled(series is not None and repository is not None)
        self.preset_combo.setEnabled(self.preset_combo.count() > 0)

    def _toggle_properties(self, expanded):
        self.properties.setVisible(expanded)
        self.properties_toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
