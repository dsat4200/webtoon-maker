from __future__ import annotations

import json

from comic_editor.core import settings as settings_module
from comic_editor.core.settings import default_hotkeys, load_settings
from comic_editor.ui.main_window import MainWindow


def _use_settings_file(monkeypatch, path):
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)


def test_missing_settings_file_has_complete_hotkeys(monkeypatch, tmp_path):
    _use_settings_file(monkeypatch, tmp_path / "missing" / "settings.json")
    loaded = load_settings()
    assert loaded.hotkeys == default_hotkeys()
    assert loaded.recent_series == []


def test_partial_settings_without_hotkeys_uses_defaults(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({"brush_size": 42}), encoding="utf-8")
    _use_settings_file(monkeypatch, path)
    loaded = load_settings()
    assert loaded.brush_size == 42
    assert loaded.hotkeys == default_hotkeys()


def test_null_and_partial_hotkeys_are_merged(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)

    path.write_text(json.dumps({"hotkeys": None}), encoding="utf-8")
    assert load_settings().hotkeys == default_hotkeys()

    path.write_text(
        json.dumps({"hotkeys": {"raster_pencil": "Ctrl+P"}}),
        encoding="utf-8",
    )
    loaded = load_settings()
    assert loaded.hotkeys["raster_pencil"] == "Ctrl+P"
    assert loaded.hotkeys["save"] == default_hotkeys()["save"]
    assert set(loaded.hotkeys) == set(default_hotkeys())


def test_main_window_starts_with_clean_configuration(qapp, monkeypatch, tmp_path):
    _use_settings_file(monkeypatch, tmp_path / "clean" / "settings.json")
    window = MainWindow()
    try:
        assert window.settings.hotkeys == default_hotkeys()
        assert len(window._shortcuts) >= len(default_hotkeys())
    finally:
        window.deleteLater()


def test_settings_v5_migration_preserves_explicit_selection_choices(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "settings_version": 1,
        "page_scope_select": False,
        "transform_mode": "uniform",
        "transform_snap_to_grid": False,
    }), encoding="utf-8")
    _use_settings_file(monkeypatch, path)
    loaded = load_settings()
    assert loaded.settings_version == 5
    assert loaded.page_scope_select is False
    assert loaded.transform_mode == "uniform"
    assert loaded.transform_snap_to_grid is False
    assert loaded.rectangle_edit_mode == "normal"


def test_rectangle_edit_mode_preserves_valid_value_and_clamps_invalid(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 5,
        "rectangle_edit_mode": "free",
    }), encoding="utf-8")
    assert load_settings().rectangle_edit_mode == "free"
    path.write_text(json.dumps({
        "settings_version": 5,
        "rectangle_edit_mode": "diagonal",
    }), encoding="utf-8")
    assert load_settings().rectangle_edit_mode == "normal"


def test_default_text_preset_is_protected_and_formatting_only():
    loaded = settings_module.EditorSettings(text_presets=[{
        "name": "default", "font_size": 48, "bold": True,
    }])
    assert len(loaded.text_presets) == 1
    assert loaded.text_presets[0]["name"] == "Default"
    assert loaded.text_presets[0]["font_size"] == 48
    assert "text" not in loaded.text_presets[0]
