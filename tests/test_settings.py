from __future__ import annotations

import json

from comic_editor.core import settings as settings_module
from comic_editor.core.settings import (
    default_hotkey_hold, default_hotkeys, load_settings,
)
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
        assert set(window._hotkey_bindings) == {
            key for key, value in default_hotkeys().items() if value
        }
    finally:
        window.deleteLater()


def test_settings_v8_migration_removes_transform_snap_and_disables_hold(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "settings_version": 1,
        "page_scope_select": False,
        "transform_mode": "uniform",
        "snap_to_grid": False,
        "transform_snap_to_grid": False,
        "text_presets": [{"name": "Legacy", "transform_snap": True}],
    }), encoding="utf-8")
    _use_settings_file(monkeypatch, path)
    loaded = load_settings()
    assert loaded.settings_version == 11
    assert loaded.page_scope_select is False
    assert loaded.transform_mode == "uniform"
    assert loaded.snap_to_grid is False
    assert not hasattr(loaded, "transform_snap_to_grid")
    assert "transform_snap" not in loaded.text_presets[1]
    assert loaded.rectangle_edit_mode == "normal"
    assert loaded.hotkey_hold == default_hotkey_hold()


def test_vector_and_fill_settings_are_normalized(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 7,
        "vector_eraser_mode": "unknown",
        "vector_simplify_amount": 140,
        "vector_redraw_opacity_max": -1,
        "fill_gap_threshold": -4,
        "fill_area_mode": "triangle",
        "fill_mode": "other",
    }), encoding="utf-8")
    loaded = load_settings()
    assert loaded.settings_version == 11
    assert loaded.vector_eraser_mode == "stroke"
    assert loaded.vector_simplify_amount == 100
    assert loaded.vector_redraw_opacity_max == 0
    assert loaded.fill_gap_threshold == 0
    assert loaded.fill_area_mode == "round"
    assert loaded.fill_mode == "normal"


def test_settings_v9_normalizes_splitter_sizes(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 8,
        "ui_splitter_sizes": {
            "sidebar_workspace": [260, 1100],
            "tools_colors": [-20, 440],
            "ribbon_canvas": ["180", "720"],
            "unknown": [1, 2],
        },
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 11
    assert loaded.ui_splitter_sizes == {
        "sidebar_workspace": [260, 1100],
        "tools_colors": [0, 440],
        "ribbon_canvas": [180, 720],
    }
    assert {"fill", "vector_redraw", "vector_connect", "vector_simplify"} <= (
        loaded.hotkeys.keys()
    )
    assert loaded.hotkeys["insert_page_gap"] == ""
    assert loaded.hotkey_hold["insert_page_gap"] is False


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
