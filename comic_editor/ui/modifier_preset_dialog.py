"""Small nonmodal editor for presets belonging to one modifier type."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QInputDialog, QLabel,
    QListWidget, QListWidgetItem, QPushButton, QVBoxLayout,
)


class ManageModifierPresetsDialog(QDialog):
    renameRequested = Signal(str, str)
    deleteRequested = Signal(str)

    def __init__(self, presets, modifier_type, parent=None):
        super().__init__(parent)
        self.modifier_type = modifier_type
        self._presets = {}
        self.setObjectName("manageModifierPresetsDialog")
        self.setWindowTitle("Manage modifier presets")
        self.setModal(False)
        self.setMinimumWidth(300)
        self.resize(320, 290)

        layout = QVBoxLayout(self)
        self.preset_list = QListWidget(self)
        self.preset_list.setObjectName("modifierPresetList")
        self.preset_list.setAccessibleName("Saved modifier presets")
        self.preset_list.setMinimumHeight(140)
        layout.addWidget(self.preset_list, 1)

        self.empty_label = QLabel("No presets saved for this modifier.", self)
        self.empty_label.setObjectName("modifierPresetEmptyLabel")
        self.empty_label.setWordWrap(True)
        layout.addWidget(self.empty_label)

        actions = QHBoxLayout()
        self.rename_button = QPushButton("Rename…", self)
        self.rename_button.setObjectName("modifierPresetRenameButton")
        self.delete_button = QPushButton("Delete", self)
        self.delete_button.setObjectName("modifierPresetDeleteButton")
        actions.addWidget(self.rename_button)
        actions.addWidget(self.delete_button)
        actions.addStretch(1)
        layout.addLayout(actions)

        self.error_label = QLabel(self)
        self.error_label.setObjectName("modifierPresetErrorLabel")
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color: #d94b4b;")
        layout.addWidget(self.error_label)

        self.button_box = QDialogButtonBox(QDialogButtonBox.Close, self)
        self.button_box.setObjectName("modifierPresetCloseButtons")
        self.button_box.rejected.connect(self.reject)
        layout.addWidget(self.button_box)

        self.rename_button.clicked.connect(self._rename)
        self.delete_button.clicked.connect(self._delete)
        self.preset_list.itemDoubleClicked.connect(lambda _item: self._rename())
        self.preset_list.currentItemChanged.connect(self._selection_changed)
        self.set_presets(presets)

    def _selected_id(self):
        item = self.preset_list.currentItem()
        return item.data(Qt.UserRole) if item is not None else None

    def set_presets(self, presets):
        """Refresh persisted values while keeping the selected preset by ID."""
        selected_id, previous_row = self._selected_id(), self.preset_list.currentRow()
        matching = [preset for preset in presets if preset.modifier_type == self.modifier_type]
        self._presets = {preset.preset_id: preset for preset in matching}
        self.preset_list.blockSignals(True)
        try:
            self.preset_list.clear()
            selected_row = None
            for row, preset in enumerate(matching):
                item = QListWidgetItem(preset.name)
                item.setData(Qt.UserRole, preset.preset_id)
                self.preset_list.addItem(item)
                if preset.preset_id == selected_id:
                    selected_row = row
            if matching:
                self.preset_list.setCurrentRow(selected_row if selected_row is not None
                                               else min(max(previous_row, 0), len(matching) - 1))
        finally:
            self.preset_list.blockSignals(False)
        self.empty_label.setVisible(not matching)
        self._selection_changed()

    def _selection_changed(self, *_args):
        selected = self._selected_id() in self._presets
        self.rename_button.setEnabled(selected)
        self.delete_button.setEnabled(selected)
        self.error_label.clear()
        self.error_label.hide()

    def _show_error(self, message):
        self.error_label.setText(message)
        self.error_label.show()

    def _rename(self):
        preset = self._presets.get(self._selected_id())
        if preset is None:
            return
        self.error_label.clear()
        self.error_label.hide()
        name, accepted = QInputDialog.getText(self, "Rename preset", "Preset name", text=preset.name)
        if not accepted:
            return
        name = name.strip()
        if not name:
            self._show_error("Enter a name for the preset.")
            return
        if any(other.preset_id != preset.preset_id and other.name.strip().casefold() == name.casefold()
               for other in self._presets.values()):
            self._show_error("A preset with this name already exists for this modifier.")
            return
        if name != preset.name:
            self.renameRequested.emit(preset.preset_id, name)

    def _delete(self):
        preset_id = self._selected_id()
        if preset_id in self._presets:
            self.deleteRequested.emit(preset_id)
