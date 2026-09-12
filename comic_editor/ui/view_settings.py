"""Permanent View Settings tab next to selection settings and masks."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
                               QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget)


class ViewSettingsPanel(QWidget):
    def __init__(self, canvas, tablet_mode, reset_view_button, fullscreen_action, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._updating = False
        self._overflow_before = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        form = QFormLayout()
        layout.addLayout(form)
        self.overflow = QSlider(Qt.Orientation.Horizontal)
        self.overflow.setRange(0, 100)
        self.overflow.setToolTip("Opacity of content outside the rendered page area")
        self.overflow_value = QDoubleSpinBox()
        self.overflow_value.setRange(0, 1)
        self.overflow_value.setSingleStep(0.05)
        self.overflow_value.setDecimals(2)
        row = QHBoxLayout()
        row.addWidget(self.overflow, 1)
        row.addWidget(self.overflow_value)
        form.addRow("Overflow", row)
        self.export_rect_enabled = QCheckBox("Enable export rect")
        form.addRow(self.export_rect_enabled)
        self.edit_export_rect = QPushButton("Edit export rect")
        form.addRow(self.edit_export_rect)
        form.addRow(tablet_mode)
        form.addRow(reset_view_button)
        self.fullscreen = QToolButton()
        self.fullscreen.setDefaultAction(fullscreen_action)
        self.fullscreen.setToolButtonStyle(Qt.ToolButtonStyle.ToolButtonTextOnly)
        form.addRow(self.fullscreen)
        layout.addStretch(1)
        self.overflow.sliderPressed.connect(self._begin_overflow)
        self.overflow.sliderReleased.connect(self._finish_overflow)
        self.overflow.valueChanged.connect(self._overflow_changed)
        self.overflow_value.valueChanged.connect(lambda value: self.overflow.setValue(round(value * 100)))
        self.export_rect_enabled.toggled.connect(self._toggle_export_rect)
        self.edit_export_rect.clicked.connect(self._edit_export_rect)
        canvas.viewSettingsChanged.connect(self.refresh)
        canvas.documentChanged.connect(self.refresh)
        self.refresh()

    def _begin_overflow(self):
        if self.canvas.chapter is not None:
            self._overflow_before = self.canvas.chapter.view_overflow

    def _finish_overflow(self):
        if self._overflow_before is not None and self.canvas.chapter is not None:
            self.canvas._record_view_change("view_overflow", self._overflow_before,
                                            self.canvas.chapter.view_overflow, "Change overflow")
        self._overflow_before = None

    def _overflow_changed(self, value):
        if self._updating:
            return
        self.overflow_value.blockSignals(True)
        self.overflow_value.setValue(value / 100)
        self.overflow_value.blockSignals(False)
        self.canvas.set_view_overflow(value / 100, record=self._overflow_before is None)

    def _toggle_export_rect(self, enabled):
        if not self._updating:
            self.canvas.set_export_rect_enabled(enabled)

    def _edit_export_rect(self):
        self.canvas.set_export_rect_editing(not self.canvas.export_rect_editing)
        self.refresh()

    def refresh(self, *_args):
        self._updating = True
        try:
            chapter = self.canvas.chapter
            active = chapter is not None
            self.overflow.setEnabled(active)
            self.overflow_value.setEnabled(active)
            self.export_rect_enabled.setEnabled(active)
            self.edit_export_rect.setEnabled(active)
            value = chapter.view_overflow if active else 0
            self.overflow.setValue(round(value * 100))
            self.overflow_value.setValue(value)
            self.export_rect_enabled.setChecked(bool(active and chapter.export_rect_enabled))
            self.edit_export_rect.setText("Save export rect" if self.canvas.export_rect_editing
                                         else "Edit export rect")
        finally:
            self._updating = False
