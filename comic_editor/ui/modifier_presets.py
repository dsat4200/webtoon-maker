"""Series-scoped preset actions for the contextual modifier stack."""
from __future__ import annotations

import copy

from PySide6.QtCore import QObject
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox

from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier


class ModifierPresetController(QObject):
    def __init__(self, owner, context_provider):
        super().__init__(owner)
        self.owner = owner
        self.context_provider = context_provider
        self.managers = []
        owner.preset_controller = self
        owner.canvas.hierarchyChanged.connect(self.refresh_managers)
        owner.canvas.chapterReplaced.connect(self.refresh_managers)

    def _context(self):
        return self.context_provider()

    def _preset(self, series, preset_id, modifier_type):
        return next((preset for preset in series.modifier_presets
                     if preset.preset_id == preset_id and preset.modifier_type == modifier_type), None)

    def populate_menu(self, menu, modifier_id):
        menu.clear()
        series, repository = self._context()
        chapter = self.owner.canvas.chapter
        modifier = chapter.modifiers.get(modifier_id) if chapter else None
        available = series is not None and repository is not None and modifier is not None
        load = getattr(menu, "_preset_load_menu", None)
        if load is None:
            # Explicit parent ownership avoids PySide deleting a submenu made
            # by addMenu(str) when its temporary menuAction wrapper is released.
            load = QMenu("Load modifier preset", menu)
            menu._preset_load_menu = load
        else:
            load.clear()
        menu.addMenu(load)
        load.setObjectName("loadModifierPresetMenu")
        load.setEnabled(available)
        if available:
            presets = sorted((item for item in series.modifier_presets
                              if item.modifier_type == modifier.modifier_type),
                             key=lambda item: item.name.casefold())
            for preset in presets:
                action = load.addAction(preset.name)
                action.setData(preset.preset_id)
                action.setCheckable(True)
                action.setChecked(chapter.modifier_preset_ids.get(modifier_id) == preset.preset_id)
                action.triggered.connect(lambda _checked=False, identifier=preset.preset_id:
                    self.load(modifier_id, identifier, series, chapter))
        else:
            presets = []
        if not presets:
            load.addAction("No presets for this modifier").setEnabled(False)
        save = menu.addAction("Save modifier preset")
        save.setObjectName("saveModifierPresetAction")
        save.setEnabled(available)
        save.triggered.connect(lambda: self.save(modifier_id, expected_series=series, expected_chapter=chapter))
        save_as = menu.addAction("Save as modifier preset…")
        save_as.setObjectName("saveAsModifierPresetAction")
        save_as.setEnabled(available)
        save_as.triggered.connect(lambda: self.save(modifier_id, save_as=True,
            expected_series=series, expected_chapter=chapter))
        menu.addSeparator()
        manage = menu.addAction("Manage modifier presets…")
        manage.setObjectName("manageModifierPresetsAction")
        manage.setEnabled(available)
        manage.triggered.connect(lambda: self.manage(modifier_id, series, chapter))
        if not available:
            menu.addAction("Open a series to use presets").setEnabled(False)

    def _target(self, modifier_id, expected_series=None, expected_chapter=None):
        series, repository = self._context()
        chapter = self.owner.canvas.chapter
        if (series is None or repository is None or chapter is None
                or expected_series is not None and series is not expected_series
                or expected_chapter is not None and chapter is not expected_chapter):
            return None
        modifier = chapter.modifiers.get(modifier_id)
        return (series, repository, chapter, modifier) if modifier is not None else None

    def _write(self, series, repository, presets):
        previous = series.modifier_presets
        series.modifier_presets = presets
        try:
            repository.save_series(series)
        except (OSError, ValueError) as error:
            series.modifier_presets = previous
            QMessageBox.warning(self.owner, "Modifier presets", f"Could not save modifier presets.\n{error}")
            return False
        self.refresh_managers()
        return True

    def save(self, modifier_id, save_as=False, *, expected_series=None, expected_chapter=None):
        target = self._target(modifier_id, expected_series, expected_chapter)
        if target is None:
            return False
        series, repository, chapter, modifier = target
        modifier_type = modifier.modifier_type
        self.owner.finish_parameter_drag()
        loaded = self._preset(series, chapter.modifier_preset_ids.get(modifier_id), modifier.modifier_type)
        if loaded is not None and not save_as:
            name = loaded.name
        else:
            used = {preset.name.casefold() for preset in series.modifier_presets
                    if preset.modifier_type == modifier.modifier_type}
            base = loaded.name if loaded else modifier.name
            suggestion, number = base, 2
            while suggestion.casefold() in used:
                suggestion, number = f"{base} {number}", number + 1
            name, accepted = QInputDialog.getText(self.owner, "Save modifier preset", "Preset name:", text=suggestion)
            if not accepted:
                return False
            name = name.strip()
        # A modal prompt can run the application's event loop. Recheck the
        # active document and reread values which may have changed meanwhile.
        target = self._target(modifier_id, series, chapter)
        if target is None or target[3].modifier_type != modifier_type:
            return False
        series, repository, chapter, modifier = target
        if loaded is None or save_as:
            used = {preset.name.casefold() for preset in series.modifier_presets
                    if preset.modifier_type == modifier_type}
            if not name or name.casefold() in used:
                QMessageBox.warning(self.owner, "Modifier presets", "Enter a unique, non-empty preset name for this modifier.")
                return False
        try:
            preset = preset_from_modifier(name, modifier)
            if loaded is not None and not save_as:
                preset.preset_id = loaded.preset_id
            presets = [item for item in series.modifier_presets if item.preset_id != preset.preset_id]
            presets.append(preset)
            before = chapter.to_dict()
        except ValueError as error:
            QMessageBox.warning(self.owner, "Modifier presets", str(error))
            return False
        if not self._write(series, repository, presets):
            return False
        chapter.modifier_preset_ids[modifier_id] = preset.preset_id
        self.owner._push(before, "Save modifier preset")
        self.owner.refresh()
        return True

    def load(self, modifier_id, preset_id, expected_series=None, expected_chapter=None):
        target = self._target(modifier_id, expected_series, expected_chapter)
        if target is None:
            return False
        series, _, chapter, modifier = target
        preset = self._preset(series, preset_id, modifier.modifier_type)
        if preset is None:
            return False
        self.owner.finish_parameter_drag()
        if self.owner.canvas._cage_edit_before is not None:
            self.owner.canvas.finish_cage(True)
        self.owner.cancel_link_mode()
        self.owner.cancel_target_layer_pick()
        try:
            replacement = apply_modifier_preset(modifier, preset)
            targets = chapter.modifier_target_ids(modifier_id)
            incompatible = chapter.incompatible_modifier_targets(replacement, targets)
            if incompatible:
                raise ValueError(chapter.modifier_compatibility_message(replacement, incompatible))
            before = chapter.to_dict()
        except ValueError as error:
            QMessageBox.warning(self.owner, "Load modifier preset", str(error))
            return False
        chapter.modifiers[modifier_id] = replacement
        chapter.modifier_preset_ids[modifier_id] = preset_id
        self.owner._changed()
        self.owner._push(before, "Load modifier preset")
        self.owner.refresh()
        return True

    def manage(self, modifier_id, expected_series=None, expected_chapter=None):
        target = self._target(modifier_id, expected_series, expected_chapter)
        if target is None:
            return None
        series, _, _, modifier = target
        from comic_editor.ui.modifier_preset_dialog import ManageModifierPresetsDialog
        dialog = ManageModifierPresetsDialog(series.modifier_presets, modifier.modifier_type, self.owner)
        dialog.renameRequested.connect(lambda identifier, name: self.rename(series, identifier, name))
        dialog.deleteRequested.connect(lambda identifier: self.delete(series, identifier))
        self.managers.append((series, dialog))
        dialog.finished.connect(lambda _result: self._forget_manager(dialog))
        dialog.show()
        return dialog

    def _forget_manager(self, dialog):
        self.managers = [(series, item) for series, item in self.managers if item is not dialog]
        dialog.deleteLater()

    def refresh_managers(self, *_):
        current, _ = self._context()
        for series, dialog in list(self.managers):
            if series is not current:
                dialog.close()
            else:
                dialog.set_presets(series.modifier_presets)

    def rename(self, series, preset_id, name):
        current, repository = self._context()
        if series is not current or repository is None:
            self.refresh_managers()
            return False
        presets = copy.deepcopy(series.modifier_presets)
        preset = next((item for item in presets if item.preset_id == preset_id), None)
        if preset is None:
            return False
        candidate = name.strip()
        if not candidate or any(item.preset_id != preset_id and item.modifier_type == preset.modifier_type
                                and item.name.casefold() == candidate.casefold() for item in presets):
            QMessageBox.warning(self.owner, "Modifier presets", "Enter a unique, non-empty preset name for this modifier.")
            return False
        preset.name = candidate
        return self._write(series, repository, presets)

    def delete(self, series, preset_id):
        current, repository = self._context()
        if series is not current or repository is None:
            self.refresh_managers()
            return False
        presets = [item for item in series.modifier_presets if item.preset_id != preset_id]
        if len(presets) == len(series.modifier_presets):
            return False
        # Existing modifiers keep their appearance. Missing associations fall
        # back to Save As, including in chapters which are not currently open.
        return self._write(series, repository, presets)
