from __future__ import annotations

from copy import deepcopy
import json

import pytest

from comic_editor.core.ui_themes import (
    DEFAULT_PALETTE, PALETTE_ROLES, ThemeLibrary, UITheme,
    export_theme, import_theme, load_theme_library, save_theme_library,
)


def _theme(name="My theme", theme_id="custom"):
    return UITheme(theme_id, name, DEFAULT_PALETTE.copy())


def test_theme_library_round_trip_keeps_base_palette_and_adjustments(tmp_path):
    library = ThemeLibrary()
    theme = library.copy_theme(library.get("light"), "Paper")
    theme.palette["accent"] = "#123abc"
    theme.adjustments.update(hue=35, saturation=-20, brightness=10, contrast=15)
    library.save_theme(theme)
    library.active_id = theme.id
    path = tmp_path / "themes.json"

    save_theme_library(library, path)
    restored = load_theme_library(path)

    assert restored.active_id == theme.id
    assert restored.get(theme.id).to_dict() == theme.to_dict()
    assert restored.get(theme.id).effective_palette() == theme.effective_palette()


def test_default_theme_preferences_are_siblings_of_editor_settings(tmp_path, monkeypatch):
    from comic_editor.core import settings
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings.json")
    library = ThemeLibrary()
    custom = library.copy_theme(library.get("dark"), "Saved locally")
    library.active_id = custom.id

    save_theme_library(library)

    assert (tmp_path / "themes.json").is_file()
    assert not (tmp_path / "settings.json").exists()
    assert load_theme_library().active_id == custom.id


def test_builtin_palettes_and_custom_records_are_isolated_snapshots():
    library = ThemeLibrary()
    original = library.get("dark").to_dict()
    draft = library.get("dark")
    draft.palette["accent"] = "#ff0000"
    draft.adjustments["hue"] = 120
    assert library.get("dark").to_dict() == original

    custom = library.copy_theme(draft, "My red")
    custom.palette["accent"] = "#00ff00"
    assert library.get(custom.id).palette["accent"] == "#ff0000"
    listed = library.all_themes()
    listed[-1].palette["accent"] = "#0000ff"
    assert library.get(custom.id).palette["accent"] == "#ff0000"


def test_builtin_records_cannot_be_overwritten_renamed_or_deleted():
    library = ThemeLibrary()
    with pytest.raises(ValueError, match="Copy"):
        library.save_theme(library.get("dark"))
    with pytest.raises(ValueError, match="Copy"):
        library.rename_theme("light", "Something else")
    with pytest.raises(ValueError, match="cannot be deleted"):
        library.delete_theme("dark")


def test_portable_import_preserves_sliders_and_never_overwrites(tmp_path):
    library = ThemeLibrary()
    custom = library.copy_theme(library.get("dark"), "Ink")
    custom.adjustments.update(hue=-180, saturation=15, brightness=-10, contrast=25)
    library.save_theme(custom)
    path = tmp_path / "ink.json"
    export_theme(custom, path)

    portable = import_theme(path)
    imported = library.import_theme(portable)
    again = library.import_theme(portable)

    assert portable.to_dict() == custom.to_dict()
    assert imported.id != custom.id
    assert again.id not in {custom.id, imported.id}
    assert imported.name == "Ink (2)"
    assert again.name == "Ink (3)"
    assert library.get(custom.id).to_dict() == custom.to_dict()
    assert imported.palette == custom.palette
    assert imported.adjustments == custom.adjustments


def test_rename_resolves_case_insensitive_collisions_and_delete_resets_active():
    library = ThemeLibrary()
    first = library.copy_theme(library.get("dark"), "DARK")
    second = library.copy_theme(library.get("dark"), "Ocean")
    assert first.name == "DARK (2)"
    renamed = library.rename_theme(second.id, "dark")
    assert renamed.name == "dark (3)"
    assert library.rename_theme(second.id, "dark (3)").name == "dark (3)"
    library.active_id = second.id
    assert library.delete_theme(second.id)
    assert library.active_id == "dark"
    assert not library.delete_theme(second.id)


@pytest.mark.parametrize("change", [
    {"id": "../unsafe"}, {"name": " "}, {"name": "Bad\nname"},
    {"palette": {**DEFAULT_PALETTE, "accent": "red"}},
    {"palette": {**DEFAULT_PALETTE, "accent": "#ff001188"}},
    {"palette": {"accent": "#ff0000"}},
    {"adjustments": {"hue": 181}}, {"adjustments": {"contrast": -101}},
    {"adjustments": {"saturation": True}}, {"adjustments": {"brightness": 0.1}},
    {"adjustments": {"brightness": float("nan")}},
    {"adjustments": {"unknown": 0}}, {"unknown": "field"},
])
def test_imported_theme_values_are_strictly_validated(change):
    raw = _theme().to_dict()
    raw.update(change)
    with pytest.raises(ValueError):
        UITheme.from_dict(raw)


def test_import_rejects_unknown_format_and_future_version(tmp_path):
    path = tmp_path / "unsupported.json"
    for raw in (
        _theme().to_dict(),
        {"format": "webtoon-maker-ui-theme", "version": 2, "theme": _theme().to_dict()},
        {"format": "webtoon-maker-ui-theme", "version": True, "theme": _theme().to_dict()},
    ):
        path.write_text(json.dumps(raw), encoding="utf-8")
        with pytest.raises(ValueError, match="format version 1"):
            import_theme(path)


def test_corrupt_library_skips_bad_records_without_rewriting(tmp_path):
    path = tmp_path / "themes.json"
    valid = _theme().to_dict()
    bad = deepcopy(valid)
    bad["id"] = "invalid"
    bad["palette"]["text"] = "not-a-color"
    raw = {"format": "webtoon-maker-ui-theme-library", "version": 1,
           "active_id": "invalid", "custom_themes": [bad, valid, valid]}
    text = json.dumps(raw)
    path.write_text(text, encoding="utf-8")

    library = load_theme_library(path)

    assert [theme.id for theme in library.custom_themes] == ["custom"]
    assert library.active_id == "dark"
    assert path.read_text(encoding="utf-8") == text
    path.write_text("unfinished JSON", encoding="utf-8")
    assert load_theme_library(path).active_id == "dark"
    assert path.read_text(encoding="utf-8") == "unfinished JSON"


def test_missing_library_does_not_create_preferences(tmp_path):
    path = tmp_path / "missing.json"
    library = load_theme_library(path)
    assert library.get("missing").id == "dark"
    assert {"dark", "light"}.issubset({theme.id for theme in library.all_themes()})
    assert not path.exists()


def test_failed_atomic_publication_preserves_previous_theme_file(tmp_path, monkeypatch):
    from comic_editor.core import brush_storage
    path = tmp_path / "themes.json"
    library = ThemeLibrary()
    save_theme_library(library, path)
    original = path.read_bytes()
    library.copy_theme(library.get("dark"), "New theme")

    def fail_replace(*args):
        raise OSError("Could not publish")

    monkeypatch.setattr(brush_storage.os, "replace", fail_replace)
    with pytest.raises(OSError, match="Could not publish"):
        save_theme_library(library, path)

    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_adjustments_always_derive_from_original_palette():
    theme = _theme()
    original = theme.palette.copy()
    assert theme.effective_palette() == original
    theme.adjustments.update(hue=90, saturation=-25, brightness=10, contrast=20)
    first = theme.effective_palette()
    assert theme.effective_palette() == first
    assert theme.palette == original
    theme.adjustments.update(hue=0, saturation=0, brightness=0, contrast=0)
    assert theme.effective_palette() == original


def test_hue_saturation_brightness_and_contrast_have_expected_endpoints():
    palette = dict.fromkeys(PALETTE_ROLES, "#ff0000")
    theme = UITheme("example", "Example", palette)
    theme.adjustments["hue"] = 120
    assert set(theme.effective_palette().values()) == {"#00ff00"}
    theme.adjustments.update(hue=0, saturation=-100)
    assert set(theme.effective_palette().values()) == {"#ffffff"}
    theme.adjustments.update(saturation=0, brightness=-100)
    assert set(theme.effective_palette().values()) == {"#000000"}
    theme.adjustments.update(brightness=0, contrast=-100)
    assert set(theme.effective_palette().values()) == {"#808080"}


def test_mutated_invalid_draft_cannot_be_exported(tmp_path):
    theme = _theme()
    theme.palette["accent"] = "malformed"
    path = tmp_path / "bad.json"
    with pytest.raises(ValueError):
        export_theme(theme, path)
    assert not path.exists()
