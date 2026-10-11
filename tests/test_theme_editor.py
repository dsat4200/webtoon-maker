from __future__ import annotations

from copy import deepcopy

import pytest
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from comic_editor.core.ui_themes import PALETTE_ROLES, ThemeLibrary, export_theme, import_theme
from comic_editor.ui.theme_editor import ThemeEditorWidget


@pytest.fixture
def editor(qapp):
    original_palette = QPalette(qapp.palette())
    original_stylesheet = qapp.styleSheet()
    original_colors = qapp.property("ui_theme_palette")
    original_accent = qapp.property("ui_theme_accent")
    library = ThemeLibrary()
    widget = ThemeEditorWidget(library)
    try:
        yield widget, library
    finally:
        widget.deleteLater()
        qapp.processEvents()
        qapp.setPalette(original_palette)
        qapp.setStyleSheet(original_stylesheet)
        qapp.setProperty("ui_theme_palette", original_colors)
        qapp.setProperty("ui_theme_accent", original_accent)


def test_live_palette_preview_and_cancel_restore_do_not_mutate_library(editor, qapp):
    widget, library = editor
    original = library.get(library.active_id).to_dict()

    widget.set_palette_color("accent", "#127fe9")

    assert widget.dirty
    assert qapp.property("ui_theme_palette")["accent"] == "#127fe9"
    assert qapp.palette().color(QPalette.Link) == QColor("#127fe9")
    assert library.get(library.active_id).to_dict() == original
    assert library.custom_themes == []
    widget.restore_preview()
    assert qapp.property("ui_theme_palette") == library.get(library.active_id).effective_palette()
    assert library.custom_themes == []


def test_unchanged_ok_preserves_builtin_without_creating_custom_theme(editor):
    widget, library = editor
    assert not widget.dirty

    accepted = widget.result_library()

    assert accepted.active_id == library.active_id
    assert accepted.custom_themes == []
    assert not widget.dirty


def test_builtin_palette_edits_become_custom_on_ok(editor):
    widget, original_library = editor
    builtin = original_library.get("dark").to_dict()
    widget.set_palette_color("panel", "#292320")

    accepted = widget.result_library()

    assert len(accepted.custom_themes) == 1
    assert accepted.active_id == accepted.custom_themes[0].id
    assert accepted.active_id != "dark"
    assert accepted.get(accepted.active_id).palette["panel"] == "#292320"
    assert accepted.get("dark").to_dict() == builtin
    assert original_library.custom_themes == []
    assert widget.rename_button.isEnabled()
    assert widget.delete_button.isEnabled()
    assert not widget.dirty


def test_save_rename_export_and_import_preserve_palette_without_collision(editor, tmp_path, monkeypatch):
    widget, original_library = editor
    widget.set_palette_color("accent", "#05bca2")
    widget.sliders["hue"].setValue(25)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Sea glass", True))

    widget.save_button.click()

    saved_id = widget.draft.id
    assert saved_id != "dark"
    assert widget.draft.name == "Sea glass"
    assert widget.library.get(saved_id).palette["accent"] == "#05bca2"
    assert widget.library.get(saved_id).adjustments["hue"] == 25
    assert not widget.dirty
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Mint", True))
    widget.rename_button.click()
    assert widget.library.get(saved_id).name == "Mint"
    path = tmp_path / "mint.json"
    monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args, **kwargs: (str(path), ""))
    widget.export_button.click()
    portable = import_theme(path)
    assert portable.to_dict() == widget.draft.to_dict()

    imported = widget.import_file(path)
    again = widget.import_file(path)

    assert imported.id != saved_id
    assert imported.name == "Mint (2)"
    assert again.name == "Mint (3)"
    assert again.id not in {saved_id, imported.id}
    assert widget.library.get(saved_id).to_dict() == portable.to_dict()
    assert widget.library.active_id == again.id
    assert original_library.custom_themes == []


def test_save_updates_existing_custom_theme_and_save_as_creates_variation(editor, monkeypatch):
    widget, _library = editor
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Custom", True))
    assert widget._save_as()
    saved_id = widget.draft.id
    widget.set_palette_color("button", "#13579b")

    widget.save_button.click()

    assert len(widget.library.custom_themes) == 1
    assert widget.draft.id == saved_id
    assert widget.library.get(saved_id).palette["button"] == "#13579b"
    assert not widget.dirty
    assert widget._save_as()
    assert widget.draft.id != saved_id
    assert widget.draft.name == "Custom (2)"
    assert len(widget.library.custom_themes) == 2


def test_slider_changes_keep_base_palette_and_preview_derived_colors(editor, qapp):
    widget, _library = editor
    base_palette = deepcopy(widget.draft.palette)
    changes = {"hue": 50, "saturation": -30, "brightness": 12, "contrast": 18}

    for role, value in changes.items():
        widget.sliders[role].setValue(value)

    assert widget.draft.palette == base_palette
    assert widget.draft.adjustments == changes
    assert qapp.property("ui_theme_palette") == widget.draft.effective_palette()
    assert all(widget.adjustment_spins[role].value() == value for role, value in changes.items())
    changed = qapp.property("ui_theme_palette")
    assert changed != base_palette
    widget.preview_checkbox.setChecked(False)
    assert qapp.property("ui_theme_palette") == widget.original_theme.effective_palette()
    widget.preview_checkbox.setChecked(True)
    assert qapp.property("ui_theme_palette") == changed
    widget.reset_adjustments_button.click()
    assert widget.draft.palette == base_palette
    assert not any(widget.draft.adjustments.values())
    assert qapp.property("ui_theme_palette") == base_palette


def test_theme_switch_cancel_retains_current_draft(editor, monkeypatch):
    widget, _library = editor
    widget.set_palette_color("accent", "#abcdef")
    before = widget.draft.to_dict()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Cancel)

    widget.theme_combo.setCurrentIndex(widget.theme_combo.findData("light"))

    assert widget.draft.to_dict() == before
    assert widget.theme_combo.currentData() == "dark"
    assert widget.dirty


def test_theme_switch_discard_loads_selected_builtin(editor, qapp, monkeypatch):
    widget, _library = editor
    widget.set_palette_color("accent", "#abcdef")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Discard)

    widget.theme_combo.setCurrentIndex(widget.theme_combo.findData("light"))

    assert widget.draft.id == "light"
    assert widget.library.active_id == "light"
    assert widget.library.custom_themes == []
    assert not widget.dirty
    assert qapp.property("ui_theme_palette") == widget.library.get("light").effective_palette()
    assert not widget.rename_button.isEnabled()
    assert not widget.delete_button.isEnabled()


def test_save_prompt_cancel_keeps_unsaved_builtin_draft(editor, monkeypatch):
    widget, _library = editor
    widget.set_palette_color("accent", "#abcdef")
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("", False))

    assert not widget._save()

    assert widget.draft.id == "dark"
    assert widget.library.custom_themes == []
    assert widget.dirty


def test_delete_custom_theme_returns_to_dark(editor, monkeypatch):
    widget, _library = editor
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Delete me", True))
    assert widget._save_as()
    deleted_id = widget.draft.id
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes)

    widget.delete_button.click()

    assert widget.draft.id == "dark"
    assert widget.library.active_id == "dark"
    assert not any(theme.id == deleted_id for theme in widget.library.custom_themes)
    assert not widget.dirty


@pytest.mark.parametrize("name", ["x" * 201, "Bad\nname"])
def test_invalid_save_name_warns_and_preserves_draft(editor, monkeypatch, name):
    widget, _library = editor
    widget.set_palette_color("accent", "#abcdef")
    before = widget.draft.to_dict()
    warnings = []
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: (name, True))
    monkeypatch.setattr(QMessageBox, "warning", lambda *args, **kwargs: warnings.append(args[1:]))

    assert not widget._save_as()

    assert len(warnings) == 1
    assert warnings[0][0] == "Theme name"
    assert "200" in warnings[0][1]
    assert widget.draft.to_dict() == before
    assert widget.library.custom_themes == []
    assert widget.dirty


def test_import_cancel_preserves_dirty_palette(editor, tmp_path, monkeypatch):
    widget, _library = editor
    path = tmp_path / "light.json"
    export_theme(widget.library.get("light"), path)
    widget.set_palette_color("accent", "#abcdef")
    before = widget.draft.to_dict()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Cancel)

    assert widget.import_file(path) is None

    assert widget.draft.to_dict() == before
    assert widget.library.active_id == "dark"
    assert widget.library.custom_themes == []
    assert widget.dirty


@pytest.mark.parametrize("answer", [QMessageBox.Discard, QMessageBox.Save])
def test_import_resolves_dirty_palette_with_discard_or_save(editor, tmp_path, monkeypatch, answer):
    widget, _library = editor
    path = tmp_path / "light.json"
    export_theme(widget.library.get("light"), path)
    widget.set_palette_color("accent", "#abcdef")
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: answer)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Before import", True))

    imported = widget.import_file(path)

    assert imported is not None
    assert imported.name == "Light (2)"
    assert imported.palette == widget.library.get("light").palette
    assert widget.library.active_id == imported.id
    assert not widget.dirty
    if answer == QMessageBox.Save:
        assert len(widget.library.custom_themes) == 2
        saved = next(theme for theme in widget.library.custom_themes if theme.name == "Before import")
        assert saved.palette["accent"] == "#abcdef"
    else:
        assert len(widget.library.custom_themes) == 1


def test_import_save_prompt_cancel_preserves_dirty_palette(editor, tmp_path, monkeypatch):
    widget, _library = editor
    path = tmp_path / "light.json"
    export_theme(widget.library.get("light"), path)
    widget.set_palette_color("accent", "#abcdef")
    before = widget.draft.to_dict()
    monkeypatch.setattr(QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Save)
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("", False))

    assert widget.import_file(path) is None

    assert widget.draft.to_dict() == before
    assert widget.library.custom_themes == []
    assert widget.dirty


def test_invalid_import_does_not_discard_or_prompt_for_dirty_palette(editor, tmp_path, monkeypatch):
    widget, _library = editor
    path = tmp_path / "broken.json"
    path.write_text("not JSON", encoding="utf-8")
    widget.set_palette_color("accent", "#abcdef")
    before = widget.draft.to_dict()

    def unexpected_prompt(*args, **kwargs):
        raise AssertionError("Validate imports before asking to change the draft")

    monkeypatch.setattr(QMessageBox, "question", unexpected_prompt)
    with pytest.raises(ValueError, match="Could not read"):
        widget.import_file(path)

    assert widget.draft.to_dict() == before
    assert widget.library.custom_themes == []


def test_settings_themes_rejection_restores_original_theme(editor, qapp):
    from comic_editor.core.settings import EditorSettings
    from comic_editor.ui.settings_dialog import SettingsDialog

    _widget, library = editor
    library.active_id = "light"
    original = library.get("light").effective_palette()
    dialog = SettingsDialog(EditorSettings(), theme_library=library)
    try:
        assert dialog.tabs.tabText(dialog.tabs.indexOf(dialog.theme_editor)) == "Themes"
        dialog.theme_editor.set_palette_color("accent", "#abcdef")
        dialog.theme_editor.sliders["hue"].setValue(45)
        assert qapp.property("ui_theme_palette") != original

        dialog.reject()

        assert qapp.property("ui_theme_palette") == original
        assert library.active_id == "light"
        assert library.custom_themes == []
    finally:
        dialog.deleteLater()
        qapp.processEvents()


def test_bundled_registry_includes_controller_themes_and_blender_botanesque(editor):
    widget, _library = editor
    themes = {theme.id: theme for theme in widget.library.all_themes()}
    assert set(themes) == {
        "dark", "light", "blender", "crona", "kaito", "len", "miku_light",
        "miku_dark", "shadow", "unit01", "botanesque",
    }
    assert all(set(theme.palette) == set(PALETTE_ROLES) for theme in themes.values())
    assert themes["dark"].palette["accent"] == "#50fa7b"
    assert themes["blender"].palette["accent"] == "#ff8f00"
    assert themes["botanesque"].palette["accent"] == "#b4b245"
    assert widget.theme_combo.count() == 11
