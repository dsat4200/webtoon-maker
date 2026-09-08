"""Preset-manager actions request immediate persistence without altering models."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QDialogButtonBox

from comic_editor.core.models import ModifierPreset
from comic_editor.ui.modifier_preset_dialog import ManageModifierPresetsDialog


def preset(preset_id, name, modifier_type="blur"):
    return ModifierPreset(preset_id=preset_id, name=name, modifier_type=modifier_type)


@pytest.fixture
def manager(qapp):
    presets = [preset("soft", "Soft"), preset("strong", "Strong"),
               preset("dots", "Dots", "halftone")]
    dialog = ManageModifierPresetsDialog(presets, "blur")
    dialog.show()
    qapp.processEvents()
    yield dialog, presets
    dialog.close()
    dialog.deleteLater()


def enter_name(monkeypatch, name, accepted=True):
    requests = []
    def answer(*args, **kwargs):
        requests.append((args, kwargs))
        return name, accepted
    monkeypatch.setattr("comic_editor.ui.modifier_preset_dialog.QInputDialog.getText", answer)
    return requests


def test_manager_filters_current_modifier_type_and_has_enabled_selection(manager):
    dialog, _ = manager
    assert dialog.isModal() is False
    assert 300 <= dialog.width() <= 400
    assert [dialog.preset_list.item(row).text() for row in range(dialog.preset_list.count())] == ["Soft", "Strong"]
    assert dialog.preset_list.currentItem().data(Qt.UserRole) == "soft"
    assert dialog.rename_button.isEnabled() and dialog.delete_button.isEnabled()
    assert dialog.empty_label.isHidden()
    assert dialog.error_label.isHidden()


def test_rename_button_prefills_current_name_and_emits_trimmed_request(manager, monkeypatch):
    dialog, presets = manager
    requests = enter_name(monkeypatch, "  Gentle blur  ")
    renamed = []
    dialog.renameRequested.connect(lambda preset_id, name: renamed.append((preset_id, name)))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert requests[0][1]["text"] == "Soft"
    assert renamed == [("soft", "Gentle blur")]
    assert presets[0].name == "Soft"
    assert dialog.isVisible()


@pytest.mark.parametrize("name,message", [("   ", "Enter a name"), ("  sTrOnG ", "already exists")])
def test_invalid_rename_shows_inline_error_and_does_not_emit(manager, monkeypatch, name, message):
    dialog, presets = manager
    enter_name(monkeypatch, name)
    renamed = []
    dialog.renameRequested.connect(lambda *values: renamed.append(values))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert renamed == []
    assert message in dialog.error_label.text()
    assert dialog.error_label.isVisible()
    assert presets[0].name == "Soft"
    dialog.preset_list.setCurrentRow(1)
    assert dialog.error_label.isHidden()


def test_duplicate_validation_uses_unicode_casefold(manager, monkeypatch):
    dialog, _ = manager
    dialog.set_presets([preset("first", "Soft"), preset("second", "Straße")])
    dialog.preset_list.setCurrentRow(0)
    enter_name(monkeypatch, "STRASSE")
    renamed = []
    dialog.renameRequested.connect(lambda *values: renamed.append(values))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert renamed == []
    assert "already exists" in dialog.error_label.text()


def test_other_modifier_type_name_is_not_a_duplicate(manager, monkeypatch):
    dialog, _ = manager
    enter_name(monkeypatch, "Dots")
    renamed = []
    dialog.renameRequested.connect(lambda *values: renamed.append(values))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert renamed == [("soft", "Dots")]
    assert dialog.error_label.isHidden()


def test_current_preset_can_change_name_case(manager, monkeypatch):
    dialog, _ = manager
    enter_name(monkeypatch, "SOFT")
    renamed = []
    dialog.renameRequested.connect(lambda *values: renamed.append(values))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert renamed == [("soft", "SOFT")]


@pytest.mark.parametrize("name,accepted", [("Replacement", False), (" Soft ", True)])
def test_cancelled_or_unchanged_rename_does_nothing(manager, monkeypatch, name, accepted):
    dialog, _ = manager
    enter_name(monkeypatch, name, accepted)
    renamed = []
    dialog.renameRequested.connect(lambda *values: renamed.append(values))
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert renamed == []
    assert dialog.error_label.isHidden()


def test_delete_button_emits_selected_id_without_confirmation_and_allows_refresh(manager, monkeypatch):
    dialog, presets = manager
    def unexpected_confirmation(*_args, **_kwargs):
        raise AssertionError("The Delete button already expresses the user's action")
    monkeypatch.setattr("PySide6.QtWidgets.QMessageBox.question", unexpected_confirmation)
    deleted = []
    def persist(preset_id):
        deleted.append(preset_id)
        dialog.set_presets([item for item in presets if item.preset_id != preset_id])
    dialog.deleteRequested.connect(persist)
    dialog.preset_list.setCurrentRow(1)
    QTest.mouseClick(dialog.delete_button, Qt.LeftButton)
    assert deleted == ["strong"]
    assert dialog.preset_list.count() == 1
    assert dialog.preset_list.currentItem().data(Qt.UserRole) == "soft"
    assert dialog.isVisible()


def test_refresh_preserves_selected_id_after_rename_and_reordering(manager):
    dialog, _ = manager
    dialog.preset_list.setCurrentRow(1)
    dialog.set_presets([preset("strong", "Renamed"), preset("soft", "Soft"),
                        preset("other", "Other", "pixelate")])
    assert dialog.preset_list.currentRow() == 0
    assert dialog.preset_list.currentItem().data(Qt.UserRole) == "strong"
    assert dialog.preset_list.currentItem().text() == "Renamed"


def test_empty_list_and_missing_selection_disable_actions(manager):
    dialog, _ = manager
    dialog.preset_list.setCurrentRow(-1)
    assert not dialog.rename_button.isEnabled() and not dialog.delete_button.isEnabled()
    dialog.set_presets([preset("other", "Other", "pixelate")])
    assert dialog.preset_list.count() == 0
    assert dialog.preset_list.currentItem() is None
    assert dialog.empty_label.isVisible()
    assert not dialog.rename_button.isEnabled() and not dialog.delete_button.isEnabled()
    dialog.set_presets([preset("new", "New")])
    assert dialog.preset_list.currentItem().data(Qt.UserRole) == "new"
    assert dialog.rename_button.isEnabled() and dialog.delete_button.isEnabled()
    assert dialog.empty_label.isHidden()


def test_close_button_closes_nonmodal_manager(manager):
    dialog, _ = manager
    QTest.mouseClick(dialog.button_box.button(QDialogButtonBox.Close), Qt.LeftButton)
    assert not dialog.isVisible()
