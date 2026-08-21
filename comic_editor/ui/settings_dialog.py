"""Application settings dialog with room for additional categories."""
from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QDialog, QDialogButtonBox, QFormLayout,
    QGroupBox, QHBoxLayout, QPushButton, QSlider, QSpinBox, QTabWidget,
    QVBoxLayout, QWidget,
)

from comic_editor.core.models import ChapterDocument, GridSettings
from comic_editor.core.settings import EditorSettings


class SettingsDialog(QDialog):
    """Edit user defaults and the optional current-document grid override."""

    def __init__(
        self,
        settings: EditorSettings,
        document: ChapterDocument | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.setWindowTitle("Settings")
        self.setMinimumWidth(430)

        layout = QVBoxLayout(self)
        self.tabs = QTabWidget(self)
        layout.addWidget(self.tabs)
        self.grid_tab = QWidget(self.tabs)
        self.tabs.addTab(self.grid_tab, "Grid")
        grid_layout = QVBoxLayout(self.grid_tab)

        user_group = QGroupBox("User grid defaults", self.grid_tab)
        user_form = QFormLayout(user_group)
        self.grid_visible = QCheckBox("Show grid overlay", user_group)
        self.grid_visible.setChecked(bool(settings.grid_overlay_visible))
        user_form.addRow(self.grid_visible)
        self.grid_size = self._size_spin(settings.grid_size_px, user_group)
        user_form.addRow("Grid box size", self.grid_size)
        self.grid_divisions = self._division_spin(
            settings.grid_divisions, user_group
        )
        user_form.addRow("Divisions per box", self.grid_divisions)
        self.grid_color = self._color_button(settings.grid_color, user_group)
        user_form.addRow("Color", self.grid_color)
        self.grid_opacity, opacity_row = self._opacity_control(
            round(settings.grid_opacity * 100), user_group
        )
        user_form.addRow("Opacity", opacity_row)
        grid_layout.addWidget(user_group)

        document_group = QGroupBox("Current document", self.grid_tab)
        document_form = QFormLayout(document_group)
        self.document_override = QCheckBox(
            "Override user grid defaults", document_group
        )
        self.document_override.setEnabled(document is not None)
        self.document_override.setChecked(bool(
            document is not None and document.grid_override_enabled
        ))
        document_form.addRow(self.document_override)

        inherited = GridSettings(
            size=settings.grid_size_px,
            divisions=settings.grid_divisions,
            color=settings.grid_color,
            opacity=settings.grid_opacity,
        )
        source = (
            document.grid
            if document is not None and document.grid_override_enabled
            else inherited
        )
        self.document_grid_size = self._size_spin(source.size, document_group)
        document_form.addRow("Grid box size", self.document_grid_size)
        self.document_grid_divisions = self._division_spin(
            source.divisions, document_group
        )
        document_form.addRow(
            "Divisions per box", self.document_grid_divisions
        )
        self.document_grid_color = self._color_button(
            source.color, document_group
        )
        document_form.addRow("Color", self.document_grid_color)
        self.document_grid_opacity, document_opacity_row = (
            self._opacity_control(round(source.opacity * 100), document_group)
        )
        document_form.addRow("Opacity", document_opacity_row)
        self._document_controls = (
            self.document_grid_size, self.document_grid_divisions,
            self.document_grid_color, self.document_grid_opacity,
            document_opacity_row,
        )
        self.document_override.toggled.connect(
            self._sync_document_controls
        )
        self._sync_document_controls(self.document_override.isChecked())
        grid_layout.addWidget(document_group)
        grid_layout.addStretch(1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok
            | QDialogButtonBox.StandardButton.Cancel,
            parent=self,
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _size_spin(value: int, parent: QWidget) -> QSpinBox:
        control = QSpinBox(parent)
        control.setRange(8, 1080)
        control.setSuffix(" px")
        control.setValue(int(value))
        return control

    @staticmethod
    def _division_spin(value: int, parent: QWidget) -> QSpinBox:
        control = QSpinBox(parent)
        control.setRange(1, 16)
        control.setValue(int(value))
        return control

    def _color_button(self, value: str, parent: QWidget) -> QPushButton:
        button = QPushButton(parent)
        self._set_button_color(button, value)
        button.clicked.connect(lambda: self._choose_color(button))
        return button

    @staticmethod
    def _set_button_color(button: QPushButton, value: str) -> None:
        color = QColor(str(value))
        if not color.isValid():
            color = QColor("#5d7d9c")
        value = color.name(QColor.NameFormat.HexRgb)
        button.setText(value)
        button.setProperty("color", value)
        button.setStyleSheet(
            f"background-color: {value}; color: "
            f"{'#111111' if color.lightnessF() > 0.6 else '#ffffff'}"
        )

    def _choose_color(self, button: QPushButton) -> None:
        selected = QColorDialog.getColor(
            QColor(str(button.property("color"))), self, "Choose grid color"
        )
        if selected.isValid():
            self._set_button_color(button, selected.name())

    @staticmethod
    def _opacity_control(
        value: int, parent: QWidget,
    ) -> tuple[QSpinBox, QWidget]:
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal, row)
        slider.setRange(0, 100)
        spin = QSpinBox(row)
        spin.setRange(0, 100)
        spin.setSuffix("%")
        slider.setValue(int(value))
        spin.setValue(int(value))
        slider.valueChanged.connect(spin.setValue)
        spin.valueChanged.connect(slider.setValue)
        layout.addWidget(slider, 1)
        layout.addWidget(spin)
        return spin, row

    def _sync_document_controls(self, enabled: bool) -> None:
        enabled = bool(enabled and self.document_override.isEnabled())
        for control in self._document_controls:
            control.setEnabled(enabled)

    def apply_user_settings(self, settings: EditorSettings) -> None:
        settings.grid_overlay_visible = self.grid_visible.isChecked()
        settings.grid_size_px = self.grid_size.value()
        settings.grid_divisions = self.grid_divisions.value()
        settings.grid_color = str(self.grid_color.property("color"))
        settings.grid_opacity = self.grid_opacity.value() / 100.0
        settings.clamp()

    def document_grid(self) -> GridSettings:
        grid = GridSettings(
            enabled=True,
            size=self.document_grid_size.value(),
            divisions=self.document_grid_divisions.value(),
            color=str(self.document_grid_color.property("color")),
            opacity=self.document_grid_opacity.value() / 100.0,
        )
        grid.validate()
        return grid
