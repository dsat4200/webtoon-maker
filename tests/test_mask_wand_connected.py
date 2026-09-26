"""Connected mask selection is persistent and can include separated colors."""
import json

import numpy as np
import pytest
from PySide6.QtCore import QPoint, QRectF, Qt
from PySide6.QtGui import QFont, QTransform

from comic_editor.core import settings as settings_module
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.ui.tool_ribbon_pages import ToolSettingsControls
from test_mask_selection import canvas, _field, _wand_artwork, _wand_click


@pytest.fixture(autouse=True)
def disconnected_canvas(monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)


def test_connected_defaults_on_for_existing_settings_and_round_trips(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)
    assert load_settings().mask_wand_connected is True
    assert load_settings().mask_wand_ignore_other_layers is False
    path.write_text(json.dumps({"settings_version": 22, "mask_wand_tolerance": 42}), encoding="utf8")
    settings = load_settings()
    assert settings.mask_wand_connected is True and settings.mask_wand_tolerance == 42
    assert settings.mask_wand_ignore_other_layers is False
    settings.mask_wand_connected = False
    settings.mask_wand_ignore_other_layers = True
    save_settings(settings)
    assert load_settings().mask_wand_connected is False
    assert load_settings().mask_wand_ignore_other_layers is True
    settings.mask_wand_connected = True
    settings.mask_wand_ignore_other_layers = False
    save_settings(settings)
    assert load_settings().mask_wand_connected is True
    assert load_settings().mask_wand_ignore_other_layers is False


def test_connected_checkbox_updates_settings_and_refresh_is_silent(qapp):
    settings = EditorSettings()
    controls = ToolSettingsControls(settings)
    changes = []
    controls.settingsChanged.connect(lambda: changes.append(1))
    assert controls.mask_wand_connected.text() == "Connected"
    assert controls.mask_wand_connected.isChecked()
    assert controls.mask_wand_ignore_other_layers.text() == "Ignore other layers"
    assert not controls.mask_wand_ignore_other_layers.isChecked()
    controls.mask_wand_connected.click()
    assert settings.mask_wand_connected is False and changes == [1]
    settings.mask_wand_connected = True
    controls.refresh()
    assert controls.mask_wand_connected.isChecked() and changes == [1]
    controls.mask_wand_ignore_other_layers.click()
    assert settings.mask_wand_ignore_other_layers is True and changes == [1, 1]
    settings.mask_wand_ignore_other_layers = False
    controls.refresh()
    assert not controls.mask_wand_ignore_other_layers.isChecked() and changes == [1, 1]
    controls.deleteLater()


def test_wand_checkboxes_fit_160px_sidebar(qapp, text_outline_font_family):
    controls = ToolSettingsControls(EditorSettings())
    controls.setFont(QFont(text_outline_font_family, 9))
    controls.set_context("mask_wand", False, mask_active=True)
    controls.setFixedWidth(160)
    controls.resize(160, 600)
    controls.show()
    qapp.processEvents()
    for checkbox in (controls.mask_wand_connected, controls.mask_wand_ignore_other_layers):
        left = checkbox.mapTo(controls, QPoint()).x()
        assert 0 <= left and left + checkbox.width() <= 160
        assert checkbox.width() >= checkbox.sizeHint().width()
    controls.close()
    controls.deleteLater()


@pytest.mark.parametrize("connected", [True, False])
def test_wand_connected_toggle_changes_only_disconnected_color_regions(canvas, connected):
    _wand_artwork(canvas)
    canvas.settings.mask_wand_tolerance = 0
    canvas.settings.mask_wand_connected = connected
    _wand_click(canvas, (220, 100))
    selected = _field(canvas)
    assert selected[100, 220] == 1
    assert selected[300, 250] == (0 if connected else 1)
    assert selected[100, 350] == 0  # Nearby but outside color tolerance.
    assert selected[200, 220] == 0  # Space separating the matching regions.
    canvas.command_stack.undo()
    assert not _field(canvas).any()
    canvas.command_stack.redo()
    np.testing.assert_array_equal(_field(canvas), selected)
    _wand_click(canvas, (220, 100), Qt.ControlModifier)
    assert not _field(canvas).any()


def test_nonconnected_selects_only_within_finite_document(canvas):
    canvas.chapter.width, canvas.chapter.height = 500, 600
    canvas.settings.mask_wand_connected = False
    canvas.settings.mask_wand_tolerance = 255
    _wand_click(canvas, (220, 100))
    result = canvas.render_tone_mask_field(canvas.active_tone_mask_id, 550, 650,
        QTransform(), QRectF(0, 0, 550, 650))
    assert result[:600, :500].all()
    assert not result[600:, :].any() and not result[:, 500:].any()
