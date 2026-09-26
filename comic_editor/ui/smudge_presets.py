"""Project-local presets containing only settings for future smudge strokes."""
from __future__ import annotations

import copy
from uuid import uuid4

from PySide6.QtWidgets import QInputDialog, QMessageBox

from comic_editor.core.smudge import validate_tool_settings


class SmudgeToolPresets:
    def __init__(self, controls):
        self.controls = controls

    def context(self):
        controller = self.controls.owner.preset_controller
        return controller._context() if controller is not None else (None, None)

    def items(self):
        series, repository = self.context()
        if series is None or repository is None:
            return []
        return sorted(series.smudge_tool_presets, key=lambda item: item['name'].casefold())

    def save(self, name=None):
        series, repository = self.context()
        chapter = self.controls.owner.canvas.chapter
        if series is None or repository is None or self.controls.current_modifier() is None:
            return False
        if name is None:
            name, accepted = QInputDialog.getText(
                self.controls, 'Save smudge tool preset', 'Preset name:')
            if not accepted:
                return False
        # Dialogs process events: do not save to a project switched meanwhile.
        current_series, current_repository = self.context()
        if (current_series is not series or current_repository is not repository
                or self.controls.owner.canvas.chapter is not chapter):
            return False
        name = str(name).strip()
        if not name or any(item['name'].casefold() == name.casefold() for item in series.smudge_tool_presets):
            QMessageBox.warning(self.controls, 'Smudge tool presets', 'Enter a unique, non-empty preset name.')
            return False
        modifier = self.controls.current_modifier()
        if modifier is None:
            return False
        previous = copy.deepcopy(series.smudge_tool_presets)
        item = {'id': uuid4().hex, 'name': name,
                'settings': validate_tool_settings(modifier.parameters.get('tool_settings', {}))}
        series.smudge_tool_presets = [*previous, item]
        try:
            # SeriesRepository publishes its manifest atomically.
            repository.save_series(series)
        except (OSError, ValueError) as error:
            series.smudge_tool_presets = previous
            QMessageBox.warning(self.controls, 'Smudge tool presets', f'Could not save preset.\n{error}')
            return False
        self.controls.refresh_presets(item['id'])
        return True

    def load(self, identifier):
        preset = next((item for item in self.items() if item['id'] == identifier), None)
        if preset is None or self.controls.current_modifier() is None:
            return False
        self.controls.owner.finish_parameter_drag()
        self.controls.set_tool_settings(validate_tool_settings(preset['settings']))
        self.controls.refresh_values()
        return True
