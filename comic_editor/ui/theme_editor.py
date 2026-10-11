"""Transactional theme editor used by the application's Settings dialog."""
from __future__ import annotations

import copy
from pathlib import Path
import re

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QInputDialog, QLabel, QMessageBox,
    QPushButton, QScrollArea, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

from comic_editor.core.ui_themes import (
    PALETTE_ROLES, ThemeLibrary, export_theme, import_theme,
)
from comic_editor.ui.themes import apply_ui_theme


class ThemeEditorWidget(QWidget):
    def __init__(self, library: ThemeLibrary, parent=None):
        super().__init__(parent)
        self.library = copy.deepcopy(library)
        self.original_theme = library.get(library.active_id)
        self.draft = self.library.get(self.library.active_id)
        self._baseline = copy.deepcopy(self.draft)
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        heading = QLabel("Make the workspace your own", self)
        heading.setObjectName("themeEditorHeading")
        layout.addWidget(heading)
        row = QHBoxLayout()
        row.addWidget(QLabel("Theme", self))
        self.theme_combo = QComboBox(self)
        self.theme_combo.setObjectName("uiThemeSelector")
        row.addWidget(self.theme_combo, 1)
        self.save_button = QPushButton("Save", self)
        self.save_as_button = QPushButton("Save as…", self)
        self.rename_button = QPushButton("Rename…", self)
        for button in (self.save_button, self.save_as_button, self.rename_button):
            row.addWidget(button)
        layout.addLayout(row)
        actions = QHBoxLayout()
        self.preview_checkbox = QCheckBox("Preview across the app", self)
        self.preview_checkbox.setChecked(True)
        actions.addWidget(self.preview_checkbox, 1)
        self.import_button = QPushButton("Import…", self)
        self.export_button = QPushButton("Export…", self)
        self.delete_button = QPushButton("Delete", self)
        for button in (self.import_button, self.export_button, self.delete_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.description = QLabel(self)
        self.description.setObjectName("themeEditorDescription")
        self.description.setWordWrap(True)
        layout.addWidget(self.description)

        adjustments = QGroupBox("Adjust the UI palette", self)
        form = QFormLayout(adjustments)
        self.sliders = {}
        self.adjustment_spins = {}
        for key, label, minimum, maximum, suffix in (
            ("hue", "Hue", -180, 180, "°"),
            ("saturation", "Saturation", -100, 100, "%"),
            ("brightness", "Brightness", -100, 100, "%"),
            ("contrast", "Contrast", -100, 100, "%"),
        ):
            field = QWidget(adjustments)
            controls = QHBoxLayout(field)
            controls.setContentsMargins(0, 0, 0, 0)
            slider = QSlider(Qt.Horizontal, field)
            slider.setObjectName(f"theme{key.title()}Slider")
            slider.setRange(minimum, maximum)
            spin = QSpinBox(field)
            spin.setRange(minimum, maximum)
            spin.setSuffix(suffix)
            spin.setFixedWidth(85)
            slider.valueChanged.connect(spin.setValue)
            spin.valueChanged.connect(slider.setValue)
            spin.valueChanged.connect(lambda value, role=key: self._adjust(role, value))
            controls.addWidget(slider, 1)
            controls.addWidget(spin)
            form.addRow(label, field)
            self.sliders[key] = slider
            self.adjustment_spins[key] = spin
        self.reset_adjustments_button = QPushButton("Reset adjustments", adjustments)
        form.addRow(self.reset_adjustments_button)
        layout.addWidget(adjustments)

        palette_group = QGroupBox("Palette colors", self)
        palette_outer = QVBoxLayout(palette_group)
        note = QLabel("Edit the base colors below; sliders transform them together.", palette_group)
        note.setWordWrap(True)
        palette_outer.addWidget(note)
        scroll = QScrollArea(palette_group)
        scroll.setWidgetResizable(True)
        scroll.setMinimumHeight(180)
        scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget(scroll)
        grid = QGridLayout(content)
        grid.setContentsMargins(2, 2, 8, 2)
        self.color_buttons = {}
        for index, (role, label) in enumerate(PALETTE_ROLES.items()):
            column = (index % 2) * 2
            grid.addWidget(QLabel(label, content), index // 2, column)
            button = QPushButton(content)
            button.setObjectName(f"themeColor_{role}")
            button.setMinimumWidth(100)
            button.clicked.connect(lambda checked=False, key=role: self._choose_color(key))
            grid.addWidget(button, index // 2, column + 1)
            self.color_buttons[role] = button
        grid.setColumnStretch(1, 1)
        grid.setColumnStretch(3, 1)
        scroll.setWidget(content)
        palette_outer.addWidget(scroll, 1)
        layout.addWidget(palette_group, 1)
        self.status = QLabel("Changes are saved with OK. Cancel restores the previous theme.", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.theme_combo.currentIndexChanged.connect(self._select_theme)
        self.save_button.clicked.connect(self._save)
        self.save_as_button.clicked.connect(self._save_as)
        self.rename_button.clicked.connect(self._rename)
        self.delete_button.clicked.connect(self._delete)
        self.import_button.clicked.connect(self._import)
        self.export_button.clicked.connect(self._export)
        self.reset_adjustments_button.clicked.connect(self._reset_adjustments)
        self.preview_checkbox.toggled.connect(self._preview)
        self._refresh_choices(self.draft.id)
        self._load_controls()

    @property
    def dirty(self):
        return self.draft.to_dict() != self._baseline.to_dict()

    def _is_custom(self):
        return any(theme.id == self.draft.id for theme in self.library.custom_themes)

    def _refresh_choices(self, selected):
        with QSignalBlocker(self.theme_combo):
            self.theme_combo.clear()
            for theme in self.library.all_themes():
                self.theme_combo.addItem(theme.name, theme.id)
            self.theme_combo.setCurrentIndex(max(0, self.theme_combo.findData(selected)))

    def _load_controls(self):
        self._loading = True
        try:
            for key, spin in self.adjustment_spins.items():
                spin.setValue(self.draft.adjustments.get(key, 0))
            self._update_swatches()
            custom = self._is_custom()
            self.rename_button.setEnabled(custom)
            self.delete_button.setEnabled(custom)
            self.description.setText(
                "Your saved theme. Edit its palette or save a new variation."
                if custom else "Built-in palette. Save your edits as a custom theme."
            )
        finally:
            self._loading = False
        self._preview()

    def _update_swatches(self):
        for role, button in self.color_buttons.items():
            value = self.draft.palette[role]
            color = QColor(value)
            text = "#171717" if color.lightnessF() > .55 else "#ffffff"
            button.setText(value.upper())
            button.setToolTip(f"Choose {PALETTE_ROLES[role].lower()}")
            button.setStyleSheet(f"background: {value}; color: {text}; border: 1px solid {text};")

    def set_palette_color(self, role, color):
        parsed = QColor(color)
        if role not in PALETTE_ROLES or not parsed.isValid():
            raise ValueError("Choose a valid palette color")
        self.draft.palette[role] = parsed.name()
        self._update_swatches()
        self._preview()

    def _choose_color(self, role):
        color = QColorDialog.getColor(QColor(self.draft.palette[role]), self, PALETTE_ROLES[role])
        if color.isValid():
            self.set_palette_color(role, color.name())

    def _adjust(self, role, value):
        if not self._loading:
            self.draft.adjustments[role] = value
            self._preview()

    def _reset_adjustments(self):
        for spin in self.adjustment_spins.values():
            spin.setValue(0)

    def _preview(self, *args):
        if not self._loading:
            apply_ui_theme(self.draft if self.preview_checkbox.isChecked() else self.original_theme)

    def restore_preview(self):
        apply_ui_theme(self.original_theme)

    def _select_theme(self, index):
        selected = self.theme_combo.itemData(index)
        if not selected or selected == self.draft.id:
            return
        if not self._resolve_draft():
            self._refresh_choices(self.draft.id)
            return
        self.draft = self.library.get(selected)
        self.library.active_id = self.draft.id
        self._baseline = copy.deepcopy(self.draft)
        self._refresh_choices(self.draft.id)
        self._load_controls()

    def _resolve_draft(self):
        if not self.dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved palette", "Save this palette before switching themes?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        return answer != QMessageBox.Cancel and (answer != QMessageBox.Save or self._save())

    def _store_draft(self, name=None):
        if name is not None or not self._is_custom():
            self.draft = self.library.copy_theme(self.draft, name or f"{self.draft.name} Custom")
        else:
            self.draft = self.library.save_theme(self.draft)
        self.library.active_id = self.draft.id
        self._baseline = copy.deepcopy(self.draft)
        self._refresh_choices(self.draft.id)
        self._load_controls()
        return self.draft

    def _save(self, checked=False):
        if not self._is_custom():
            return self._save_as()
        self._store_draft()
        self.status.setText("Theme saved in this dialog. Click OK to keep it.")
        return True

    def _save_as(self, checked=False):
        name, accepted = QInputDialog.getText(self, "Save theme", "Theme name", text=f"{self.draft.name} Custom")
        if not accepted or not name.strip():
            return False
        try:
            self._store_draft(name.strip())
        except ValueError as error:
            QMessageBox.warning(self, "Theme name", str(error))
            return False
        self.status.setText("Theme saved in this dialog. Click OK to keep it.")
        return True

    def _rename(self):
        name, accepted = QInputDialog.getText(self, "Rename theme", "Theme name", text=self.draft.name)
        if not accepted or not name.strip():
            return
        try:
            self.library.rename_theme(self.draft.id, name.strip())
            self.draft.name = self.library.get(self.draft.id).name
            self._store_draft()
        except ValueError as error:
            QMessageBox.warning(self, "Theme name", str(error))

    def _delete(self):
        answer = QMessageBox.question(self, "Delete theme", f'Delete "{self.draft.name}"?', QMessageBox.Yes | QMessageBox.No)
        if answer != QMessageBox.Yes:
            return
        self.library.delete_theme(self.draft.id)
        self.draft = self.library.get("dark")
        self.library.active_id = self.draft.id
        self._baseline = copy.deepcopy(self.draft)
        self._refresh_choices(self.draft.id)
        self._load_controls()

    def import_file(self, path):
        theme = import_theme(Path(path))
        if not self._resolve_draft():
            return None
        imported = self.library.import_theme(theme)
        self.draft = imported
        self.library.active_id = imported.id
        self._baseline = copy.deepcopy(imported)
        self._refresh_choices(imported.id)
        self._load_controls()
        return imported

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import UI theme", "", "UI themes (*.json)")
        if path:
            try:
                self.import_file(path)
            except (OSError, ValueError, TypeError) as error:
                QMessageBox.warning(self, "Could not import theme", str(error))

    def _export(self):
        name = re.sub(r'[^\w .-]', "", self.draft.name).strip() or "theme"
        path, _ = QFileDialog.getSaveFileName(self, "Export UI theme", f"{name}.json", "UI themes (*.json)")
        if path:
            try:
                export_theme(self.draft, Path(path))
                self.status.setText(f"Exported {self.draft.name}.")
            except (OSError, ValueError) as error:
                QMessageBox.warning(self, "Could not export theme", str(error))

    def result_library(self):
        if self.dirty:
            self._store_draft()
        self.library.active_id = self.draft.id
        return copy.deepcopy(self.library)
