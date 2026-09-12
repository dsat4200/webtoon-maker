"""Compact editable hotkey map for the standalone editor."""
from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout,
    QMessageBox, QPushButton, QScrollArea, QVBoxLayout, QWidget,
)

from comic_editor.core.settings import default_hotkey_hold, default_hotkeys
from comic_editor.ui.hotkeys import ChordCaptureEdit, normalize_chord


LABELS = {
    "raster_pencil": "Pencil",
    "raster_eraser": "Eraser",
    "fill": "Fill",
    "gradient": "Gradient",
    "object_select": "Object Select",
    "transform": "Transform",
    "eyedropper": "Eyedropper",
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
    "reset_rotation": "Reset Rotation",
    "toggle_grid": "Toggle Grid",
    "select_all": "Select All",
    "deselect": "Deselect",
    "cut": "Cut",
    "copy": "Copy",
    "paste": "Paste",
    "paste_as_new": "Paste as New Object",
    "clipboard_image_history": "Clipboard Image History",
    "delete_selected": "Delete Selected",
    "clear_canvas": "Clear Canvas",
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
        form_widget = QWidget()
        form = QFormLayout(form_widget)
        self.editors: dict[str, ChordCaptureEdit] = {}
        self.hold_checks: dict[str, QCheckBox] = {}
        hold_bindings = hold_bindings or {}
        for action_id, label in LABELS.items():
            editor = ChordCaptureEdit(bindings.get(action_id, ""))
            self.editors[action_id] = editor
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            clear = QPushButton("×")
            clear.setFixedSize(20, 20)
            clear.setToolTip("Clear hotkey")
            clear.clicked.connect(lambda checked=False, e=editor: (e.setChord(""), e.chordChanged.emit("")))
            row_layout.addWidget(clear)
            row_layout.addWidget(editor, 1)
            if action_id in self.TOOL_ACTIONS:
                hold = QCheckBox("Hold")
                hold.setChecked(bool(hold_bindings.get(action_id, False)))
                self.hold_checks[action_id] = hold
                row_layout.addWidget(hold)
            form.addRow(label, row)
        self.scroll_area = QScrollArea()
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setWidget(form_widget)
        layout.addWidget(self.scroll_area, 1)
        reset = QPushButton("Reset defaults")
        reset.clicked.connect(self._reset)
        layout.addWidget(reset)
        self.button_box = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        self.button_box.accepted.connect(self.accept)
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        available = self.screen().availableGeometry()
        self.resize(
            min(560, max(1, available.width() - 80)),
            min(650, max(360, available.height() - 160)),
        )

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
