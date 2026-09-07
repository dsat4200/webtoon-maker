"""Numeric controls for the regular tile crop."""
from PySide6.QtWidgets import QWidget, QFormLayout, QComboBox, QDoubleSpinBox
from PySide6.QtCore import QSignalBlocker


class TilingSettingsControls(QWidget):
    def __init__(self, owner, modifier, parent=None):
        super().__init__(parent)
        self.owner, self.modifier_id = owner, modifier.modifier_id
        self.controls = {}
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        shape = QComboBox(self)
        self.shape_control = shape
        for label, value in (("Square", "square"), ("Hexagon", "hexagon"), ("Triangle", "triangle")):
            shape.addItem(label, value)
        shape.setCurrentIndex(shape.findData(modifier.shape))
        shape.currentIndexChanged.connect(lambda: owner.set_parameter(modifier.modifier_id, "shape", shape.currentData(), True))
        layout.addRow("Tile", shape)
        for label, attribute, value, minimum, maximum in (
            ("Center X", "x", modifier.center[0], -1e7, 1e7),
            ("Center Y", "y", modifier.center[1], -1e7, 1e7),
            ("Side length", "side", modifier.side, 1., 1e7),
            ("Rotation", "rotation", modifier.rotation, -360., 360.),
        ):
            control = QDoubleSpinBox(self)
            self.controls[attribute] = control
            control.setObjectName("tiling_"+attribute)
            control.setRange(minimum, maximum)
            control.setDecimals(2)
            control.setValue(value)
            control.setKeyboardTracking(False)
            control.setSuffix("°" if attribute == "rotation" else " px")
            def changed(value, attribute=attribute):
                field = attribute
                if attribute in {"x", "y"}:
                    current = owner.canvas.chapter.modifiers.get(self.modifier_id)
                    if current is None:
                        return
                    center = list(current.center)
                    center[0 if attribute == "x" else 1] = value
                    field, value = "center", tuple(center)
                owner.set_parameter(modifier.modifier_id, field, value, True)
            control.valueChanged.connect(changed)
            layout.addRow(label, control)
        owner.canvas.interactionFinished.connect(self.sync_values)

    def sync_values(self):
        chapter = self.owner.canvas.chapter
        modifier = chapter.modifiers.get(self.modifier_id) if chapter else None
        if modifier is None:
            return
        for attribute, control in self.controls.items():
            blocker = QSignalBlocker(control)
            control.setValue(modifier.center[0 if attribute == "x" else 1] if attribute in {"x", "y"} else getattr(modifier, attribute))
            del blocker
        blocker = QSignalBlocker(self.shape_control)
        self.shape_control.setCurrentIndex(self.shape_control.findData(modifier.shape))
