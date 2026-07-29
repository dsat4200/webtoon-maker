"""Compact editable hotkey map for the standalone editor."""
from __future__ import annotations

from PySide6.QtGui import QKeySequence
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QFormLayout, QKeySequenceEdit, QPushButton,
    QVBoxLayout,
)

from comic_editor.core.settings import default_hotkeys


LABELS = {
    "raster_pencil": "Raster Pencil",
    "raster_eraser": "Raster Eraser",
    "object_select": "Object Select",
    "transform": "Transform",
    "shape_edit": "Shape Edit",
    "save": "Save",
    "undo": "Undo",
    "redo": "Redo",
    "reset_view": "Reset View",
    "toggle_grid": "Toggle Grid",
}


class HotkeysDialog(QDialog):
    def __init__(self, bindings: dict[str, str], parent=None):
        super().__init__(parent)
        self.setWindowTitle("Hotkeys")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.editors: dict[str, QKeySequenceEdit] = {}
        for action_id, label in LABELS.items():
            editor = QKeySequenceEdit(QKeySequence(bindings.get(action_id, "")))
            self.editors[action_id] = editor
            form.addRow(label, editor)
        layout.addLayout(form)
        reset = QPushButton("Reset defaults")
        reset.clicked.connect(self._reset)
        layout.addWidget(reset)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def bindings(self) -> dict[str, str]:
        result = {
            action_id: editor.keySequence().toString(QKeySequence.PortableText)
            for action_id, editor in self.editors.items()
        }
        used = [sequence for sequence in result.values() if sequence]
        if len(used) != len(set(used)):
            raise ValueError("Each action must have a unique hotkey")
        return result

    def _reset(self) -> None:
        for action_id, sequence in default_hotkeys().items():
            self.editors[action_id].setKeySequence(QKeySequence(sequence))
