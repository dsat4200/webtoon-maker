"""Drawing-only pencil pressure and preset manager."""
from __future__ import annotations

from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout,
    QInputDialog, QLabel, QMessageBox, QPushButton, QSlider, QVBoxLayout,
)

from comic_editor.core.pressure import BrushPreset, PressureCurve
from comic_editor.ui.pressure_curve_editor import PressureCurveEditor


class PencilSettingsDialog(QDialog):
    committedPresets = Signal(object, str)

    def __init__(self, presets: list[dict], active_name: str, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Pencil Settings – Pressure Curves")
        self.setMinimumSize(680, 520)
        self._presets = deepcopy(presets)
        self._active_name = active_name
        self._draft_dirty = False
        self._loading = False
        outer = QVBoxLayout(self)
        bar = QHBoxLayout()
        bar.addWidget(QLabel("Preset"))
        self.preset_combo = QComboBox()
        bar.addWidget(self.preset_combo, 1)
        self.save_button = QPushButton("Save Preset…")
        self.delete_button = QPushButton("Delete")
        bar.addWidget(self.save_button)
        bar.addWidget(self.delete_button)
        outer.addLayout(bar)

        toggles = QHBoxLayout()
        self.size_pressure = QCheckBox("Pressure controls size")
        self.opacity_pressure = QCheckBox("Pressure controls opacity")
        self.antialias = QCheckBox("Antialias")
        for control in (self.size_pressure, self.opacity_pressure, self.antialias):
            toggles.addWidget(control)
        outer.addLayout(toggles)
        editors = QHBoxLayout()
        self.size_editor = PressureCurveEditor("Size Curve", PressureCurve())
        self.opacity_editor = PressureCurveEditor("Opacity Curve", PressureCurve())
        editors.addWidget(self.size_editor)
        editors.addWidget(self.opacity_editor)
        outer.addLayout(editors, 1)

        sliders = QHBoxLayout()
        self.density, self.density_label = self._slider(
            sliders, "Density", 10, 300
        )
        self.start_taper, self.start_label = self._slider(
            sliders, "Start taper", 10, 100
        )
        self.end_taper, self.end_label = self._slider(
            sliders, "End taper", 10, 100
        )
        outer.addLayout(sliders)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self._close_requested)
        outer.addWidget(close)

        self.preset_combo.currentIndexChanged.connect(self._select_preset)
        self.save_button.clicked.connect(self._save)
        self.delete_button.clicked.connect(self._delete)
        self.size_editor.curveChanged.connect(self._mark_dirty)
        self.opacity_editor.curveChanged.connect(self._mark_dirty)
        for control in (
            self.size_pressure, self.opacity_pressure, self.antialias
        ):
            control.toggled.connect(self._mark_dirty)
        for control in (self.density, self.start_taper, self.end_taper):
            control.valueChanged.connect(self._mark_dirty)
        self.size_pressure.toggled.connect(self._sync_enabled)
        self.opacity_pressure.toggled.connect(self._sync_enabled)
        self.density.valueChanged.connect(
            lambda value: self.density_label.setText(f"{value / 100:.2f}×")
        )
        self.start_taper.valueChanged.connect(
            lambda value: self.start_label.setText(f"{value / 100:.2f}×")
        )
        self.end_taper.valueChanged.connect(
            lambda value: self.end_label.setText(f"{value / 100:.2f}×")
        )
        self._populate()
        self._load(self._active())

    @staticmethod
    def _slider(layout, label, low, high):
        column = QVBoxLayout()
        column.addWidget(QLabel(label))
        slider = QSlider(Qt.Horizontal)
        slider.setRange(low, high)
        value = QLabel()
        column.addWidget(slider)
        column.addWidget(value, alignment=Qt.AlignRight)
        layout.addLayout(column)
        return slider, value

    def _populate(self) -> None:
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        self.preset_combo.addItems([
            item.get("name", "Untitled") for item in self._presets
        ])
        self.preset_combo.setCurrentIndex(max(
            0, self.preset_combo.findText(self._active_name)
        ))
        self.preset_combo.blockSignals(False)
        self.delete_button.setEnabled(
            len(self._presets) > 1 and self._active_name != "Linear"
        )

    def _active(self) -> BrushPreset:
        return BrushPreset.from_dict(next(
            (item for item in self._presets
             if item.get("name") == self._active_name),
            self._presets[0],
        ))

    def _load(self, preset: BrushPreset) -> None:
        self._loading = True
        self.size_editor.set_curve(
            PressureCurve.from_dict(preset.size_curve.to_dict())
        )
        self.opacity_editor.set_curve(
            PressureCurve.from_dict(preset.opacity_curve.to_dict())
        )
        self.size_pressure.setChecked(preset.pressure_size)
        self.opacity_pressure.setChecked(preset.pressure_opacity)
        self.antialias.setChecked(preset.antialiasing)
        self.density.setValue(round(preset.density * 100))
        self.start_taper.setValue(round(preset.stroke_start_ratio * 100))
        self.end_taper.setValue(round(preset.stroke_end_ratio * 100))
        self._loading = False
        self._draft_dirty = False
        self._sync_enabled()

    def _draft(self, name=None) -> BrushPreset:
        return BrushPreset(
            name=name or self._active_name,
            size_curve=PressureCurve.from_dict(
                self.size_editor.curve().to_dict()
            ),
            opacity_curve=PressureCurve.from_dict(
                self.opacity_editor.curve().to_dict()
            ),
            pressure_size=self.size_pressure.isChecked(),
            pressure_opacity=self.opacity_pressure.isChecked(),
            density=self.density.value() / 100,
            stroke_start_ratio=self.start_taper.value() / 100,
            stroke_end_ratio=self.end_taper.value() / 100,
            antialiasing=self.antialias.isChecked(),
        )

    def _mark_dirty(self, *args) -> None:
        if not self._loading:
            self._draft_dirty = True

    def _sync_enabled(self, *args) -> None:
        self.size_editor.setEnabled(self.size_pressure.isChecked())
        self.opacity_editor.setEnabled(self.opacity_pressure.isChecked())

    def _resolve_dirty(self) -> bool:
        if not self._draft_dirty:
            return True
        answer = QMessageBox.warning(
            self, "Unsaved preset",
            f'Save changes to "{self._active_name}"?',
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel,
        )
        if answer == QMessageBox.Save:
            return self._save()
        if answer == QMessageBox.Discard:
            self._draft_dirty = False
            return True
        return False

    def _select_preset(self, index: int) -> None:
        if index < 0 or index >= len(self._presets):
            return
        previous = self.preset_combo.findText(self._active_name)
        if not self._resolve_dirty():
            self.preset_combo.blockSignals(True)
            self.preset_combo.setCurrentIndex(previous)
            self.preset_combo.blockSignals(False)
            return
        self._active_name = self._presets[index]["name"]
        self._load(BrushPreset.from_dict(self._presets[index]))
        self._populate()

    def _save(self) -> bool:
        name, accepted = QInputDialog.getText(
            self, "Save preset", "Preset name", text=self._active_name
        )
        if not accepted or not name.strip():
            return False
        name = name.strip()
        index = next((
            i for i, item in enumerate(self._presets)
            if item.get("name") == name
        ), -1)
        if index >= 0 and name != self._active_name:
            if QMessageBox.question(
                self, "Overwrite preset", f'Overwrite "{name}"?',
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return False
        preset = self._draft(name).to_dict()
        if index >= 0:
            self._presets[index] = preset
        else:
            self._presets.append(preset)
        self._active_name = name
        self._draft_dirty = False
        self._populate()
        self.committedPresets.emit(deepcopy(self._presets), name)
        return True

    def _delete(self) -> None:
        if self._active_name == "Linear" or len(self._presets) <= 1:
            return
        if not self._resolve_dirty():
            return
        if QMessageBox.question(
            self, "Delete preset", f'Delete "{self._active_name}"?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        self._presets = [
            item for item in self._presets
            if item.get("name") != self._active_name
        ]
        self._active_name = self._presets[0]["name"]
        self._populate()
        self._load(self._active())
        self.committedPresets.emit(
            deepcopy(self._presets), self._active_name
        )

    def _close_requested(self) -> None:
        if self._resolve_dirty():
            self.reject()

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._resolve_dirty():
            event.accept()
        else:
            event.ignore()
