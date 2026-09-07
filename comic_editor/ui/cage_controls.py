"""Identical cage controls for destructive tools and non-destructive modifiers."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QWidget, QVBoxLayout, QHBoxLayout, QLabel, QSlider, QSpinBox, QComboBox, QPushButton, QCheckBox
from comic_editor.ui.icons import iconoir


class CageSettingsControls(QWidget):
    def __init__(self, canvas, parent=None, modifier_id=None):
        super().__init__(parent)
        self.canvas = canvas
        self.modifier_id = modifier_id
        self._updating = False
        self.fields = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        for name, label, low, high in (("columns", "Horizontal points", 2, 16),
                ("rows", "Vertical points", 2, 16), ("smoothness", "Smoothness", 0, 100)):
            layout.addWidget(QLabel(label, self))
            row = QHBoxLayout()
            slider, value = QSlider(Qt.Horizontal, self), QSpinBox(self)
            slider.setRange(low, high)
            value.setRange(low, high)
            value.setKeyboardTracking(False)
            value.setObjectName("cage_"+name)
            slider.valueChanged.connect(value.setValue)
            value.valueChanged.connect(slider.setValue)
            value.valueChanged.connect(lambda v, n=name: self.change(n, v))
            row.addWidget(slider, 1)
            row.addWidget(value)
            layout.addLayout(row)
            self.fields[name] = (slider, value)
        layout.addWidget(QLabel("Image interpolation", self))
        self.interpolation = QComboBox(self)
        for value, label in (("nearest", "Nearest neighbor"), ("bilinear", "Bilinear"), ("bicubic", "Bicubic")):
            self.interpolation.addItem(label, value)
        self.interpolation.currentIndexChanged.connect(lambda _: self.change("interpolation", self.interpolation.currentData()))
        layout.addWidget(self.interpolation)
        self.uniform = QCheckBox("Uniform scaling", self)
        self.uniform.toggled.connect(lambda checked: self.change("uniform", checked))
        layout.addWidget(self.uniform)
        row = QHBoxLayout()
        for vertical, label, icon in ((False, "Horizontal", "align-horizontal-centers"), (True, "Vertical", "align-vertical-centers")):
            button = QPushButton(iconoir(icon), label, self)
            button.setToolTip("Flip " + label.lower() + " around the pivot")
            button.clicked.connect(lambda _, v=vertical: self.flip(v))
            row.addWidget(button)
        layout.addLayout(row)
        hint = QLabel("Shift/Ctrl-click points to add to the selection. Shift-drag a box to select several points. Drag the gold pivot to move it.", self)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        row = QHBoxLayout()
        for label, commit in (("OK", True), ("Cancel", False)):
            button = QPushButton(label, self)
            button.clicked.connect(lambda _, c=commit: self.canvas.finish_cage(c))
            row.addWidget(button)
        layout.addLayout(row)
        self.canvas.cageChanged.connect(self.refresh)
        self.refresh()

    def cage(self):
        if self.modifier_id and self.canvas.chapter:
            return self.canvas.chapter.modifiers.get(self.modifier_id)
        return self.canvas._active_cage()

    def activate(self):
        if self.modifier_id and self.canvas.modifier_mode:
            if self.canvas.active_modifier_id != self.modifier_id:
                self.canvas.finish_cage(True)
                self.canvas._remember_modifier(self.modifier_id)

    def change(self, name, value):
        if not self._updating:
            self.activate()
            self.canvas.set_cage_parameter(name, value)

    def flip(self, vertical):
        self.activate()
        self.canvas.flip_cage(vertical)

    def refresh(self):
        cage = self.cage()
        self.setEnabled(cage is not None)
        if cage is None:
            return
        self._updating = True
        for name, controls in self.fields.items():
            for control in controls:
                control.setValue(round(getattr(cage, name)))
        self.interpolation.setCurrentIndex(self.interpolation.findData(cage.interpolation))
        self.uniform.setChecked(cage.uniform)
        self._updating = False
