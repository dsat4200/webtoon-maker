"""Shared pencil/eraser preset management and per-preset pressure settings."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QInputDialog, QLabel, QListWidget, QMessageBox, QPushButton, QSlider,
    QVBoxLayout,
)

from comic_editor.core.pressure import BrushPreset, PressureCurve
from comic_editor.ui.pressure_curve_editor import PressureCurveEditor


class PencilSettingsDialog(QDialog):
    """Manage either tool's independent library, publishing accepted edits."""

    committedPresets = Signal(object, str)

    def __init__(self, presets: list[dict], active_name: str, parent=None, *, tool="Pencil"):
        super().__init__(parent)
        self.setWindowTitle(f"Manage {tool.lower()} presets")
        self.setMinimumSize(380, 360)
        self._presets = deepcopy(presets)
        self._active_name = active_name
        self._tool = tool
        layout = QVBoxLayout(self)
        self.presets_list = QListWidget(self)
        layout.addWidget(self.presets_list, 1)
        row = QHBoxLayout()
        self.create_button = QPushButton("Create…", self)
        self.rename_button = QPushButton("Rename…", self)
        self.delete_button = QPushButton("Delete", self)
        for button in (self.create_button, self.rename_button, self.delete_button):
            row.addWidget(button)
        layout.addLayout(row)
        buttons = QDialogButtonBox(QDialogButtonBox.Close, self)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.presets_list.currentTextChanged.connect(self._selected)
        self.create_button.clicked.connect(self._create)
        self.rename_button.clicked.connect(self._rename)
        self.delete_button.clicked.connect(self._delete)
        self._populate()

    def _populate(self):
        self.presets_list.blockSignals(True)
        self.presets_list.clear()
        self.presets_list.addItems([item["name"] for item in self._presets])
        index = next((i for i, item in enumerate(self._presets)
                      if item["name"] == self._active_name), 0)
        self.presets_list.setCurrentRow(index)
        self.presets_list.blockSignals(False)
        self.delete_button.setEnabled(len(self._presets) > 1)

    def _selected(self, name):
        if name:
            self._active_name = name
            self.committedPresets.emit(deepcopy(self._presets), name)

    def _name(self, title, initial):
        name, accepted = QInputDialog.getText(self, title, "Preset name", text=initial)
        if not accepted or not name.strip():
            return None
        name = name.strip()
        if any(item["name"].casefold() == name.casefold() for item in self._presets
               if title == "Create preset" or item["name"] != self._active_name):
            QMessageBox.warning(self, "Preset name in use", "Choose a different preset name.")
            return None
        return name

    def _publish(self):
        self._populate()
        self.committedPresets.emit(deepcopy(self._presets), self._active_name)

    def _create(self):
        name = self._name("Create preset", "New preset")
        if name is None:
            return
        preset = deepcopy(next(item for item in self._presets if item["name"] == self._active_name))
        preset["name"] = name
        self._presets.append(preset)
        self._active_name = name
        self._publish()

    def _rename(self):
        name = self._name("Rename preset", self._active_name)
        if name is None:
            return
        next(item for item in self._presets if item["name"] == self._active_name)["name"] = name
        self._active_name = name
        self._publish()

    def _delete(self):
        if len(self._presets) <= 1:
            return
        if QMessageBox.question(self, "Delete preset", f'Delete "{self._active_name}"?',
                                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        index = next(i for i, item in enumerate(self._presets) if item["name"] == self._active_name)
        self._presets.pop(index)
        self._active_name = self._presets[min(index, len(self._presets) - 1)]["name"]
        self._publish()


class DrawingPresetSettingsDialog(QDialog):
    """The same shape and pressure options for one pencil or eraser preset."""

    def __init__(self, preset: dict, parent=None, *, tool="Pencil", show_handles=False):
        super().__init__(parent)
        self._preset = BrushPreset.from_dict(preset)
        self.setWindowTitle(f"{tool} settings — {self._preset.name}")
        self.setMinimumSize(680, 520)
        outer = QVBoxLayout(self)
        form = QFormLayout()
        self.shape = QComboBox(self)
        self.shape.addItem("Circle", "circle")
        self.shape.addItem("Square", "square")
        self.shape.setToolTip("Shape of raster pencil and eraser marks.")
        self.shape.setCurrentIndex(max(0, self.shape.findData(self._preset.shape)))
        form.addRow("Shape", self.shape)
        self.show_handles = QCheckBox("Show transform handles", self)
        self.show_handles.setChecked(show_handles)
        form.addRow(self.show_handles)
        outer.addLayout(form)

        toggles = QHBoxLayout()
        self.size_pressure = QCheckBox("Pressure controls size", self)
        self.opacity_pressure = QCheckBox("Pressure controls opacity", self)
        self.antialias = QCheckBox("Antialias", self)
        self.size_pressure.setChecked(self._preset.pressure_size)
        self.opacity_pressure.setChecked(self._preset.pressure_opacity)
        self.antialias.setChecked(self._preset.antialiasing)
        for control in (self.size_pressure, self.opacity_pressure, self.antialias):
            toggles.addWidget(control)
        outer.addLayout(toggles)
        editors = QHBoxLayout()
        self.size_editor = PressureCurveEditor("Size Curve", self._preset.size_curve)
        self.opacity_editor = PressureCurveEditor("Opacity Curve", self._preset.opacity_curve)
        editors.addWidget(self.size_editor)
        editors.addWidget(self.opacity_editor)
        outer.addLayout(editors, 1)
        sliders = QHBoxLayout()
        self.density, self.density_label = self._slider(sliders, "Density", 10, 300, self._preset.density)
        self.start_taper, self.start_label = self._slider(sliders, "Start taper", 10, 100, self._preset.stroke_start_ratio)
        self.end_taper, self.end_label = self._slider(sliders, "End taper", 10, 100, self._preset.stroke_end_ratio)
        outer.addLayout(sliders)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.size_pressure.toggled.connect(self._sync_enabled)
        self.opacity_pressure.toggled.connect(self._sync_enabled)
        self._sync_enabled()

    @staticmethod
    def _slider(layout, label, low, high, initial):
        column = QVBoxLayout()
        column.addWidget(QLabel(label))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        slider.setValue(round(initial * 100))
        value = QLabel(f"{slider.value() / 100:.2f}×")
        slider.valueChanged.connect(lambda number: value.setText(f"{number / 100:.2f}×"))
        column.addWidget(slider)
        column.addWidget(value, alignment=Qt.AlignRight)
        layout.addLayout(column)
        return slider, value

    def _sync_enabled(self, *args):
        self.size_editor.setEnabled(self.size_pressure.isChecked())
        self.opacity_editor.setEnabled(self.opacity_pressure.isChecked())

    def result_preset(self) -> dict:
        return BrushPreset(
            name=self._preset.name,
            size_curve=PressureCurve.from_dict(self.size_editor.curve().to_dict()),
            opacity_curve=PressureCurve.from_dict(self.opacity_editor.curve().to_dict()),
            pressure_size=self.size_pressure.isChecked(),
            pressure_opacity=self.opacity_pressure.isChecked(),
            antialiasing=self.antialias.isChecked(),
            density=self.density.value() / 100,
            stroke_start_ratio=self.start_taper.value() / 100,
            stroke_end_ratio=self.end_taper.value() / 100,
            shape=str(self.shape.currentData()),
        ).to_dict()
