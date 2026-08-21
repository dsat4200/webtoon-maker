from __future__ import annotations

import json

from comic_editor.core import settings as settings_module
from comic_editor.core.settings import (
    default_hotkey_hold, default_hotkeys, load_settings, save_settings,
)
from comic_editor.ui.main_window import MainWindow


def _use_settings_file(monkeypatch, path):
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)


def test_missing_settings_file_has_complete_hotkeys(monkeypatch, tmp_path):
    _use_settings_file(monkeypatch, tmp_path / "missing" / "settings.json")
    loaded = load_settings()
    assert loaded.hotkeys == default_hotkeys()
    assert loaded.recent_series == []
    assert loaded.blender_bridge_host == "127.0.0.1"
    assert loaded.blender_bridge_port == 47837
    assert loaded.blender_bridge_token == ""


def test_settings_v21_adds_clamps_and_persists_grid_defaults(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 20,
        "grid_overlay_visible": False,
        "grid_size_px": 9000,
        "grid_divisions": 0,
        "grid_color": "not-a-color",
        "grid_opacity": 4,
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 21
    assert loaded.grid_overlay_visible is False
    assert loaded.grid_size_px == 1080
    assert loaded.grid_divisions == 1
    assert loaded.grid_color == "#5d7d9c"
    assert loaded.grid_opacity == 1.0
    save_settings(loaded)
    restored = load_settings()
    assert restored.grid_size_px == 1080
    assert restored.grid_divisions == 1
    assert restored.grid_color == "#5d7d9c"
    assert restored.grid_opacity == 1.0

    path.write_text(json.dumps({
        "settings_version": 21,
        "grid_size_px": "invalid",
        "grid_divisions": None,
        "grid_opacity": "invalid",
    }), encoding="utf-8")
    malformed = load_settings()
    assert malformed.grid_size_px == 120
    assert malformed.grid_divisions == 4
    assert malformed.grid_opacity == 0.25


def test_blender_bridge_endpoint_is_clamped_and_persisted(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 14,
        "blender_bridge_host": " localhost ",
        "blender_bridge_port": 999999,
        "blender_bridge_token": " panel-token ",
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 21
    assert loaded.blender_bridge_host == "localhost"
    assert loaded.blender_bridge_port == 65535
    assert loaded.blender_bridge_token == "panel-token"
    save_settings(loaded)
    restored = load_settings()
    assert restored.blender_bridge_host == "localhost"
    assert restored.blender_bridge_port == 65535
    assert restored.blender_bridge_token == "panel-token"


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
    assert loaded.settings_version == 21
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
    assert loaded.settings_version == 21
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

    assert loaded.settings_version == 21
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


def test_settings_v16_adds_sampling_transform_and_point_defaults(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({"settings_version": 15}), encoding="utf-8")
    loaded = load_settings()
    assert loaded.settings_version == 21
    assert loaded.hotkeys["eyedropper"] == "I"
    assert loaded.hotkeys["reset_rotation"] == "Ctrl+Shift+0"
    assert loaded.hotkey_hold["eyedropper"] is True
    assert loaded.pencil_transform_handles_visible is False
    assert loaded.eraser_transform_handles_visible is False
    assert loaded.vector_point_icons_visible is False
    assert loaded.vector_point_icon_size == 80
    assert loaded.vector_point_icon_opacity == 100


def test_settings_v17_adds_and_clamps_raster_fill_tolerance(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 16,
        "raster_fill_tolerance": 999,
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 21
    assert loaded.raster_fill_tolerance == 255

    path.write_text(json.dumps({"settings_version": 16}), encoding="utf-8")
    assert load_settings().raster_fill_tolerance == 16


def test_settings_v18_adds_and_clamps_mask_pencil_alpha(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 17,
        "mask_pencil_pressure_sensitive": False,
        "mask_pencil_from_alpha": -4,
        "mask_pencil_to_alpha": 3,
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 21
    assert loaded.mask_pencil_pressure_sensitive is False
    assert loaded.mask_pencil_from_alpha == 0.0
    assert loaded.mask_pencil_to_alpha == 1.0


def test_settings_v20_removes_obsolete_fill_fields_and_clamps_real_ranges(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "settings_version": 19,
        "active_fill_subtool": "editing_layer",
        "fill_profiles": {
            "editing_layer": {
                "color_source": "specified",
                "specified_color": "#FF123456",
                "target_color_mode": "black_only",
                "exclude_text": False,
                "gap_threshold": 999,
                "area_amount": -999,
            },
        },
    }), encoding="utf-8")
    _use_settings_file(monkeypatch, path)

    loaded = load_settings()
    profile = loaded.active_fill_profile()

    assert loaded.settings_version == 21
    assert profile["gap_threshold"] == 16
    assert profile["area_amount"] == -64
    assert not {
        "color_source", "specified_color", "target_color_mode", "exclude_text",
    } & profile.keys()

    path.write_text(json.dumps({"settings_version": 17}), encoding="utf-8")
    migrated = load_settings()
    assert migrated.mask_pencil_pressure_sensitive is True
    assert migrated.mask_pencil_from_alpha == 0.0
    assert migrated.mask_pencil_to_alpha == 1.0


def test_settings_v12_adds_font_preview_delete_and_integer_text_sizes(
    monkeypatch, tmp_path,
):
    path = tmp_path / "settings.json"
    _use_settings_file(monkeypatch, path)
    path.write_text(json.dumps({
        "settings_version": 11,
        "text_presets": [
            {"name": "Default", "font_size": 32.6},
            {"name": "Large", "font_size": 999},
        ],
        "hotkeys": {"save": "Ctrl+S"},
    }), encoding="utf-8")

    loaded = load_settings()

    assert loaded.settings_version == 21
    assert loaded.navigator_expanded is False
    assert loaded.preview_font_names is False
    assert loaded.hotkeys["delete_selected"] == "Delete"
    assert loaded.hotkeys["paste_image"] == "Ctrl+V"
    assert [item["font_size"] for item in loaded.text_presets] == [33, 250]

    loaded.preview_font_names = True
    save_settings(loaded)
    assert load_settings().preview_font_names is True
