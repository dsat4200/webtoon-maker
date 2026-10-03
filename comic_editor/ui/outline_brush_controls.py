"""Portable brush selection and contour settings for Outline modifiers."""
from copy import deepcopy
import secrets
from types import SimpleNamespace

from PySide6.QtCore import QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget, QSizePolicy,
)
from comic_editor.core.brushes import BrushDefinition
from comic_editor.ui.brush_controls import BrushPresetCombo


class OutlineBrushCombo(BrushPresetCombo):
    """The standard thumbnail picker, including the document's saved brush."""
    def __init__(self, modifier, settings, parent=None):
        self.modifier = modifier
        self.library_settings = settings
        self.saved_id = f"outline-saved-{modifier.modifier_id}"
        super().__init__(SimpleNamespace(brush_presets=[]), parent)
        self.setMinimumContentsLength(6)
        self.setMinimumWidth(0)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.refresh_choices()

    def refresh_choices(self):
        saved = dict(self.modifier.brush or BrushDefinition().to_dict())
        saved["id"] = self.saved_id
        self.settings.brush_presets = [saved, *self.library_settings.brush_presets]
        blocker = QSignalBlocker(self)
        self.clear()
        self.addItem(f"Saved: {saved.get('name', 'Round pen')}", self.saved_id)
        for preset in self.library_settings.brush_presets:
            self.addItem(preset.get("name", "Brush"), preset["id"])
        self.setCurrentIndex(0)
        self.refresh_thumbnails()

    def showPopup(self):
        self.refresh_choices()
        super().showPopup()


class OutlineBrushControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        style = QComboBox(self)
        style.setObjectName("outlineStyle")
        style.addItem("Solid outline", "solid")
        style.addItem("Brush outline", "brush")
        style.setCurrentIndex(style.findData(modifier.style))
        layout.addWidget(style)
        details = QWidget(self)
        form = QFormLayout(details)
        form.setContentsMargins(0, 0, 0, 0)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        brush = OutlineBrushCombo(modifier, owner.canvas.settings, details)
        brush.setObjectName("outlineBrush")
        brush.setToolTip("A copy of the brush and its materials is saved with this modifier.")
        def select_brush():
            identifier = brush.currentData()
            preset = next((record for record in owner.canvas.settings.brush_presets
                           if record.get("id") == identifier), None)
            if preset is not None and identifier != brush.saved_id:
                snapshot = deepcopy(preset)
                owner.set_parameter(modifier.modifier_id, "brush", snapshot, True)
                brush.refresh_choices()
        brush.currentIndexChanged.connect(select_brush)
        form.addRow("Brush", brush)
        current = QPushButton("Use current brush settings", details)
        current.setObjectName("outlineUseCurrentBrush")
        def use_current():
            snapshot = owner.canvas.settings.active_paint_brush().to_dict()
            owner.set_parameter(modifier.modifier_id, "brush", snapshot, True)
            brush.refresh_choices()
        current.clicked.connect(use_current)
        form.addRow(current)
        for label, field, low, high, suffix in (
                ("Spacing", "brush_spacing", 10, 400, "%"),
                ("Angle offset", "brush_angle", -180, 180, "°")):
            control = QSpinBox(details)
            control.setObjectName(field)
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setValue(round(getattr(modifier, field)))
            control.setKeyboardTracking(False)
            control.valueChanged.connect(lambda value, name=field: owner.set_parameter(
                modifier.modifier_id, name, value, True))
            form.addRow(label, control)
        follow = QCheckBox("Follow contour direction", details)
        follow.setChecked(modifier.brush_follow_contour)
        follow.toggled.connect(lambda checked: owner.set_parameter(
            modifier.modifier_id, "brush_follow_contour", checked, True))
        form.addRow(follow)
        randomize = QPushButton("Randomize brush pattern", details)
        randomize.clicked.connect(lambda: owner.set_parameter(
            modifier.modifier_id, "brush_seed", secrets.randbelow(2**32), True))
        form.addRow(randomize)
        note = QLabel("Thickness masks vary brush size. Brush outlines omit pen taper, "
                      "wet mixing and erasing.", details)
        note.setWordWrap(True)
        note.setMinimumHeight(note.fontMetrics().lineSpacing() * 4)
        form.addRow(note)
        details.setVisible(modifier.style == "brush")
        def change_style():
            value = style.currentData()
            owner.set_parameter(modifier.modifier_id, "style", value, True)
            details.setVisible(value == "brush")
        style.currentIndexChanged.connect(change_style)
        layout.addWidget(details)
