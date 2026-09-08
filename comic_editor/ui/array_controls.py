"""Array settings; spatial parameters are edited by the canvas rig."""
from PySide6.QtWidgets import QWidget, QFormLayout, QSpinBox, QDoubleSpinBox, QComboBox, QLabel


class ArraySettingsControls(QWidget):
    def __init__(self, owner, modifier, parent=None):
        super().__init__(parent)
        layout = QFormLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for attribute, label, low, high, suffix, tip in (
            ("count", "Count", 0, 100, "", "Number of added copies; the original is always kept."),
            ("angle_offset", "Angle offset", -360, 360, "°", "Rotation added at each signed step around the blue pivot."),
            ("scale_offset", "Scale offset", -90, 100, "%", "Scale change per step: +10% gives 110%, 121%, 133.1%… Backward steps use the inverse."),
        ):
            control = QSpinBox(self) if attribute == "count" else QDoubleSpinBox(self)
            control.setObjectName("array_" + attribute)
            if isinstance(control, QDoubleSpinBox):
                control.setDecimals(1)
            control.setRange(low, high)
            control.setSuffix(suffix)
            control.setValue(getattr(modifier, attribute))
            control.setKeyboardTracking(False)
            control.setToolTip(tip)
            # Capture before the first edit, then group each editing gesture.
            control.valueChanged.connect(lambda _value: owner.begin_parameter_drag())
            control.valueChanged.connect(lambda value, attr=attribute:
                owner.set_parameter(modifier.modifier_id, attr, value, False))
            control.editingFinished.connect(owner.finish_parameter_drag)
            layout.addRow(label, control)
        repeat = QComboBox(self)
        repeat.setObjectName("array_repeat_type")
        for value, label in (("center", "Center — both directions"),
                             ("first", "First — forward"), ("last", "Last — backward")):
            repeat.addItem(label, value)
        repeat.setCurrentIndex(repeat.findData(modifier.repeat_type))
        repeat.setToolTip("Position of the original in the sequence. An odd centered count adds the extra copy forward.")
        repeat.currentIndexChanged.connect(lambda _i:
            owner.set_parameter(modifier.modifier_id, "repeat_type", repeat.currentData(), True))
        layout.addRow("Repeat type", repeat)
        hint = QLabel("Drag the orange dots for spacing and direction. Drag the blue crosshair for the rotation and scale center.", self)
        hint.setWordWrap(True)
        layout.addRow(hint)
