"""Portable brush selection and contour settings for Outline modifiers."""
from copy import deepcopy
import secrets

from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QLabel, QPushButton, QSpinBox,
    QVBoxLayout, QWidget,
)


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
        presets = list(getattr(owner.canvas.settings, "brush_presets", []))
        brush = QComboBox(details)
        brush.setObjectName("outlineBrush")
        saved_name = (modifier.brush or {}).get("name", "Round pen")
        brush.addItem(f"Saved: {saved_name}", -1)
        for index, preset in enumerate(presets):
            brush.addItem(preset.get("name", "Brush"), index)
        brush.setToolTip("A copy of the brush and its materials is saved with this modifier.")
        def select_brush():
            index = brush.currentData()
            if index >= 0:
                snapshot = deepcopy(presets[index])
                owner.set_parameter(modifier.modifier_id, "brush", snapshot, True)
                brush.setItemText(0, f"Saved: {snapshot.get('name', 'Brush')}")
        brush.currentIndexChanged.connect(select_brush)
        form.addRow("Brush", brush)
        current = QPushButton("Use current brush settings", details)
        current.setObjectName("outlineUseCurrentBrush")
        def use_current():
            snapshot = owner.canvas.settings.active_paint_brush().to_dict()
            owner.set_parameter(modifier.modifier_id, "brush", snapshot, True)
            brush.setItemText(0, f"Saved: {snapshot['name']}")
            brush.setCurrentIndex(0)
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
