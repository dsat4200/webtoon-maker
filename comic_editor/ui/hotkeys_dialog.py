"""Compact editable hotkey map for the standalone editor."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QMessageBox, QPushButton, QVBoxLayout, QWidget,
)

from comic_editor.core.settings import default_hotkey_hold, default_hotkeys
from comic_editor.ui.hotkeys import ChordCaptureEdit, normalize_chord


LABELS = {
    "raster_pencil": "Pencil",
    "raster_eraser": "Eraser",
    "fill": "Fill",
    "object_select": "Object Select",
    "transform": "Transform",
    "shape_edit": "Shape Edit",
    "vector_redraw": "Redraw Vector Thickness / Opacity",
    "vector_connect": "Connect Vector Line",
    "vector_simplify": "Simplify Vector Line",
    "draw_select_rect": "Rectangle Drawing Select",
    "draw_select_lasso": "Lasso Drawing Select",
    "draw_select_stroke": "Stroke Drawing Select",
    "insert_page_gap": "Insert Page Gap",
    "save": "Save",
    "undo": "Undo",
    "redo": "Redo",
    "reset_view": "Reset View",
    "toggle_grid": "Toggle Grid",
    "select_all": "Select All",
    "delete_selected": "Delete Selected",
}


class HotkeysDialog(QDialog):
    TOOL_ACTIONS = set(default_hotkey_hold())

    def __init__(
        self, bindings: dict[str, str],
        hold_bindings: dict[str, bool] | None = None, parent=None,
    ):
        super().__init__(parent)
        self.setWindowTitle("Hotkeys")
        layout = QVBoxLayout(self)
        form = QFormLayout()
        self.editors: dict[str, ChordCaptureEdit] = {}
        self.hold_checks: dict[str, QCheckBox] = {}
        hold_bindings = hold_bindings or {}
        for action_id, label in LABELS.items():
            editor = ChordCaptureEdit(bindings.get(action_id, ""))
            self.editors[action_id] = editor
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(editor, 1)
            if action_id in self.TOOL_ACTIONS:
                hold = QCheckBox("Hold")
                hold.setChecked(bool(hold_bindings.get(action_id, False)))
                self.hold_checks[action_id] = hold
                row_layout.addWidget(hold)
            form.addRow(label, row)
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
            action_id: normalize_chord(editor.chord())
            for action_id, editor in self.editors.items()
        }
        used = [sequence for sequence in result.values() if sequence]
        if len(used) != len(set(used)):
            raise ValueError("Each action must have a unique hotkey")
        return result

    def hold_bindings(self) -> dict[str, bool]:
        return {
            action_id: check.isChecked()
            for action_id, check in self.hold_checks.items()
        }

    def accept(self) -> None:
        for editor in self.editors.values():
            editor.commitCapture()
        try:
            self.bindings()
        except ValueError as error:
            QMessageBox.warning(self, "Duplicate hotkey", str(error))
            return
        super().accept()

    def _reset(self) -> None:
        for action_id, sequence in default_hotkeys().items():
            self.editors[action_id].setChord(sequence)
        for action_id, value in default_hotkey_hold().items():
            self.hold_checks[action_id].setChecked(value)
