"""Complete series-local modifier preset actions and persistence."""
import copy

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMenu, QMessageBox, QToolButton

from comic_editor.core.models import (BoundGeometry, ChapterDocument, HalftoneModifier,
    PixelateModifier, PosterizeModifier, ScreamModifier, RasterObject)
from comic_editor.core.modifier_presets import preset_from_modifier
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_controls import ModifierCard, ModifierControls, ModifierTitleBar
from comic_editor.ui.modifier_presets import ModifierPresetController


@pytest.fixture
def preset_editor(qapp, tmp_path, monkeypatch):
    repository = SeriesRepository(tmp_path / "Series A")
    series = repository.create("Series A")
    chapter = ChapterDocument(width=100, height=100, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 100, 100))
    obj = chapter.add_object(page.layer_id, RasterObject())
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", obj.object_id)
    controls = ModifierControls(canvas)
    context = [series, repository]
    controller = ModifierPresetController(controls, lambda: tuple(context))
    errors = []
    monkeypatch.setattr(QMessageBox, "warning", lambda _parent, title, text: errors.append((title, text)))
    yield canvas, controls, controller, context, obj, errors
    for _, dialog in list(controller.managers):
        dialog.close()
    canvas._effect_jobs.cancel()
    controls.deleteLater()
    canvas.deleteLater()


def attach(editor, modifier):
    canvas, controls, _, _, obj, _ = editor
    canvas.chapter.add_modifier(modifier, [("object", obj.object_id)])
    controls.refresh()
    return modifier.modifier_id


def named(monkeypatch, name):
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: (name, True))


def test_save_save_as_and_overwrite_are_distinct_and_persist(preset_editor, monkeypatch):
    canvas, _, controller, (series, repository), _, errors = preset_editor
    identifier = attach(preset_editor, HalftoneModifier(spacing=17))
    named(monkeypatch, "Fine dots")
    assert controller.save(identifier)
    first = series.modifier_presets[0]
    assert canvas.chapter.modifier_preset_ids[identifier] == first.preset_id
    assert repository.load_series().modifier_presets[0].settings["spacing"] == 17
    canvas.chapter.modifiers[identifier].spacing = 29
    monkeypatch.setattr(QInputDialog, "getText", lambda *_args, **_kwargs: pytest.fail("Save must overwrite without asking for a name"))
    assert controller.save(identifier)
    assert len(series.modifier_presets) == 1
    assert series.modifier_presets[0].preset_id == first.preset_id
    assert repository.load_series().modifier_presets[0].settings["spacing"] == 29
    named(monkeypatch, "Large dots")
    canvas.chapter.modifiers[identifier].spacing = 45
    assert controller.save(identifier, save_as=True)
    assert len(series.modifier_presets) == 2
    assert canvas.chapter.modifier_preset_ids[identifier] != first.preset_id
    assert {item.name: item.settings["spacing"] for item in repository.load_series().modifier_presets} == {"Fine dots": 29, "Large dots": 45}
    assert not errors


def test_load_retains_targets_and_local_bindings_and_undo_restores_association(preset_editor):
    canvas, controls, controller, (series, _), obj, _ = preset_editor
    modifier = HalftoneModifier(name="My halftone", spacing=11, target_layer_id=obj.parent_layer_id,
                               expanded=False, muted=True)
    identifier = attach(preset_editor, modifier)
    second = canvas.chapter.add_object(obj.parent_layer_id, RasterObject())
    canvas.chapter.set_modifier_targets(identifier, [("object", obj.object_id), ("object", second.object_id)])
    preset = preset_from_modifier("Print", HalftoneModifier(spacing=38, color_mode="target_layer", target_hue=72))
    series.modifier_presets.append(preset)
    before = canvas.chapter.to_dict()
    assert controller.load(identifier, preset.preset_id)
    loaded = canvas.chapter.modifiers[identifier]
    assert (loaded.spacing, loaded.target_hue) == (38, 72)
    assert (loaded.name, loaded.expanded, loaded.muted, loaded.target_layer_id) == ("My halftone", False, True, obj.parent_layer_id)
    assert canvas.chapter.modifier_target_ids(identifier) == [("object", obj.object_id), ("object", second.object_id)]
    assert controls.common_ids() == [identifier]
    assert ChapterDocument.from_dict(canvas.chapter.to_dict()).modifier_preset_ids[identifier] == preset.preset_id
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.modifiers[identifier].spacing == 38
    assert canvas.chapter.modifier_preset_ids[identifier] == preset.preset_id


@pytest.mark.parametrize("factory", [HalftoneModifier, PosterizeModifier, ScreamModifier])
def test_dropdown_is_immediately_right_of_collapse_for_each_title_layout(preset_editor, factory):
    # Construct directly so the stroke card's special second action row is checked.
    _, controls, _, _, _, _ = preset_editor
    card = ModifierCard(factory(), controls)
    title = card.findChild(ModifierTitleBar)
    collapse = card.findChild(QToolButton, "modifierCollapseButton")
    assert title.layout().itemAt(0).widget() is collapse
    assert title.layout().itemAt(1).widget() is card.preset_button
    assert card.preset_button.popupMode() == QToolButton.InstantPopup
    assert title.layout().itemAt(2).widget() is title.label
    card.deleteLater()


def test_load_submenu_filters_type_and_save_actions_use_current_modifier(preset_editor, monkeypatch):
    _, controls, controller, (series, _), _, _ = preset_editor
    identifier = attach(preset_editor, PixelateModifier(pixel_size=6))
    pixel = preset_from_modifier("Chunky", PixelateModifier(pixel_size=24))
    series.modifier_presets = [preset_from_modifier("Print", HalftoneModifier()), pixel]
    menu = QMenu(controls)
    controller.populate_menu(menu, identifier)
    load = menu.actions()[0].menu()
    assert [action.text() for action in load.actions()] == ["Chunky"]
    load.actions()[0].trigger()
    assert controls.canvas.chapter.modifiers[identifier].pixel_size == 24
    assert [action.text() for action in menu.actions() if not action.isSeparator()] == [
        "Load modifier preset", "Save modifier preset", "Save as modifier preset…", "Manage modifier presets…"]
    named(monkeypatch, "Chunkier")
    menu.actions()[2].trigger()
    assert {preset.name for preset in series.modifier_presets} == {"Print", "Chunky", "Chunkier"}


def test_manager_rename_and_delete_update_disk_without_changing_applied_modifier(preset_editor, monkeypatch):
    canvas, _, controller, (series, repository), _, _ = preset_editor
    identifier = attach(preset_editor, PixelateModifier(pixel_size=18))
    named(monkeypatch, "Pixel look")
    assert controller.save(identifier)
    preset_id = series.modifier_presets[0].preset_id
    dialog = controller.manage(identifier)
    named(monkeypatch, "Retro")
    QTest.mouseClick(dialog.rename_button, Qt.LeftButton)
    assert dialog.preset_list.item(0).text() == "Retro"
    assert repository.load_series().modifier_presets[0].name == "Retro"
    before = canvas.chapter.modifiers[identifier].to_dict()
    QTest.mouseClick(dialog.delete_button, Qt.LeftButton)
    assert not series.modifier_presets and not repository.load_series().modifier_presets
    assert dialog.preset_list.count() == 0
    assert canvas.chapter.modifiers[identifier].to_dict() == before
    assert canvas.chapter.modifier_preset_ids[identifier] == preset_id
    named(monkeypatch, "Saved again")
    assert controller.save(identifier)
    assert series.modifier_presets[0].name == "Saved again"
    assert series.modifier_presets[0].preset_id != preset_id


def test_series_switch_never_exposes_or_modifies_other_series_presets(preset_editor, tmp_path, monkeypatch):
    canvas, controls, controller, context, _, _ = preset_editor
    series_a, repository_a = context
    identifier = attach(preset_editor, PixelateModifier())
    named(monkeypatch, "Series A look")
    assert controller.save(identifier)
    stale_menu = QMenu(controls)
    controller.populate_menu(stale_menu, identifier)
    dialog = controller.manage(identifier)
    repository_b = SeriesRepository(tmp_path / "Series B")
    series_b = repository_b.create("Series B")
    context[:] = [series_b, repository_b]
    controller.refresh_managers()
    assert dialog.isHidden()
    stale_menu.actions()[1].trigger()
    assert not series_b.modifier_presets
    fresh_menu = QMenu(controls)
    controller.populate_menu(fresh_menu, identifier)
    assert fresh_menu.actions()[0].menu().actions()[0].text() == "No presets for this modifier"
    assert not controller.load(identifier, series_a.modifier_presets[0].preset_id)
    assert not controller.delete(series_a, series_a.modifier_presets[0].preset_id)
    assert len(repository_a.load_series().modifier_presets) == 1


@pytest.mark.parametrize("operation", ["save", "rename", "delete"])
def test_failed_series_write_rolls_back_library_and_association(preset_editor, monkeypatch, operation):
    canvas, _, controller, (series, repository), _, errors = preset_editor
    identifier = attach(preset_editor, PixelateModifier())
    existing = preset_from_modifier("Existing", PixelateModifier(pixel_size=7))
    series.modifier_presets.append(existing)
    repository.save_series(series)
    old_library, old_chapter = copy.deepcopy(series.to_dict()), canvas.chapter.to_dict()
    monkeypatch.setattr(repository, "save_series", lambda _series: (_ for _ in ()).throw(OSError("Read only")))
    named(monkeypatch, "New")
    if operation == "save":
        assert not controller.save(identifier)
    elif operation == "rename":
        assert not controller.rename(series, existing.preset_id, "Renamed")
    else:
        assert not controller.delete(series, existing.preset_id)
    assert series.to_dict() == old_library
    assert canvas.chapter.to_dict() == old_chapter
    assert errors and "Could not save" in errors[-1][1]


def test_empty_and_duplicate_names_do_not_overwrite_existing_presets(preset_editor, monkeypatch):
    _, _, controller, (series, _), _, errors = preset_editor
    identifier = attach(preset_editor, PixelateModifier())
    series.modifier_presets.append(preset_from_modifier("Chunky", PixelateModifier(pixel_size=12)))
    for name in (" ", "cHUnky"):
        named(monkeypatch, name)
        assert not controller.save(identifier, save_as=True)
    assert len(errors) == 2 and len(series.modifier_presets) == 1
    assert series.modifier_presets[0].settings["pixel_size"] == 12


def test_presets_available_in_another_chapter_after_reopening_series(preset_editor, monkeypatch):
    canvas, _, controller, context, obj, _ = preset_editor
    series, repository = context
    identifier = attach(preset_editor, PixelateModifier(pixel_size=31))
    named(monkeypatch, "Shared look")
    assert controller.save(identifier)
    context[0] = repository.load_series()
    other = ChapterDocument(width=100, height=100, document_kind="asset")
    page = other.add_page("Second", BoundGeometry.rectangle(0, 0, 100, 100))
    other_obj = other.add_object(page.layer_id, RasterObject())
    modifier = PixelateModifier()
    other.add_modifier(modifier, [("object", other_obj.object_id)])
    canvas.set_document(other, TileStore())
    canvas.set_selection("object", other_obj.object_id)
    assert controller.load(modifier.modifier_id, context[0].modifier_presets[0].preset_id)
    assert other.modifiers[modifier.modifier_id].pixel_size == 31


def test_save_rereads_replaced_modifier_after_naming_dialog(preset_editor, monkeypatch):
    canvas, _, controller, (series, repository), _, _ = preset_editor
    identifier = attach(preset_editor, PixelateModifier(pixel_size=7))

    def replace_while_naming(*_args, **_kwargs):
        canvas.chapter.modifiers[identifier] = PixelateModifier(modifier_id=identifier, pixel_size=39)
        return "Latest settings", True

    monkeypatch.setattr(QInputDialog, "getText", replace_while_naming)
    assert controller.save(identifier)
    assert series.modifier_presets[0].settings["pixel_size"] == 39
    assert repository.load_series().modifier_presets[0].settings["pixel_size"] == 39


def test_save_rechecks_duplicate_names_after_naming_dialog(preset_editor, monkeypatch):
    _, _, controller, (series, repository), _, errors = preset_editor
    identifier = attach(preset_editor, PixelateModifier(pixel_size=7))

    def add_while_naming(*_args, **_kwargs):
        series.modifier_presets.append(preset_from_modifier("Concurrent preset", PixelateModifier(pixel_size=26)))
        repository.save_series(series)
        return "Concurrent preset", True

    monkeypatch.setattr(QInputDialog, "getText", add_while_naming)
    assert not controller.save(identifier)
    assert len(series.modifier_presets) == 1
    assert repository.load_series().modifier_presets[0].settings["pixel_size"] == 26
    assert errors and "unique" in errors[-1][1]


def test_main_window_connects_presets_to_the_active_series(qapp, tmp_path, monkeypatch):
    from comic_editor.ui.main_window import MainWindow
    repository = SeriesRepository(tmp_path / "Connected series")
    series = repository.create("Connected series")
    repository.create_chapter(series, "Chapter")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        page = window.chapter.root_page_ids[0]
        obj = window.chapter.add_object(page, RasterObject())
        window.canvas.set_selection("object", obj.object_id)
        window.modifier_controls.add_modifier("pixelate")
        named(monkeypatch, "Connected preset")
        assert window.modifier_presets.save(obj.modifier_ids[0])
        assert repository.load_series().modifier_presets[0].name == "Connected preset"
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.canvas._effect_jobs.cancel()
        window.deleteLater()


def test_preset_popup_keeps_submenu_alive_and_refreshes_on_reopen(preset_editor, qapp):
    canvas, controls, _, (series, _), _, _ = preset_editor
    modifier = PixelateModifier(pixel_size=6)
    identifier = attach(preset_editor, modifier)
    preset = preset_from_modifier("Chunky", PixelateModifier(pixel_size=24))
    series.modifier_presets.append(preset)
    card = ModifierCard(modifier, controls)
    menu = card.preset_button.menu()
    try:
        # popup() emits the real aboutToShow signal wired by ModifierCard.
        menu.popup(controls.mapToGlobal(controls.rect().topLeft()))
        qapp.processEvents()
        load = menu.actions()[0].menu()
        assert [action.text() for action in load.actions()] == ["Chunky"]
        QTest.keyClick(menu, Qt.Key_Down)
        QTest.keyClick(menu, Qt.Key_Right)
        qapp.processEvents()
        assert load.isVisible()
        load.close()
        menu.close()

        series.modifier_presets.append(preset_from_modifier("Fine", PixelateModifier(pixel_size=3)))
        menu.popup(controls.mapToGlobal(controls.rect().topLeft()))
        qapp.processEvents()
        reopened = menu.actions()[0].menu()
        assert reopened is load
        assert [action.text() for action in reopened.actions()] == ["Chunky", "Fine"]
        assert len(menu.findChildren(QMenu)) == 1
        QTest.keyClick(menu, Qt.Key_Down)
        QTest.keyClick(menu, Qt.Key_Right)
        qapp.processEvents()
        QTest.mouseClick(reopened, Qt.LeftButton, pos=reopened.actionGeometry(reopened.actions()[0]).center())
        assert canvas.chapter.modifiers[identifier].pixel_size == 24
        assert canvas.chapter.modifier_preset_ids[identifier] == preset.preset_id
    finally:
        menu.close()
        card.deleteLater()
