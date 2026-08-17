from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox, QToolButton

from comic_editor.core.assets import AssetRepository, extract_asset
from comic_editor.core.models import BoundGeometry, RasterObject
from comic_editor.core.persistence import PENDING_FILE, SeriesRepository
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.main_window import MainWindow


def _repository_with_asset(root: Path):
    repository = SeriesRepository(root)
    series = repository.create("Original Display Name")
    chapter, tiles = repository.create_chapter(series, "Chapter 1")
    layer = next(
        item for item in chapter.layers.values() if not item.is_page
    )
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", layer.layer_id, "Hero"
    )
    thumbnail = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    thumbnail.fill(Qt.GlobalColor.transparent)
    AssetRepository(root).create(manifest, asset_tiles, thumbnail)
    return repository, series, chapter, manifest


def _dispose(window: MainWindow) -> None:
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.hide()
    window.deleteLater()


def test_file_menu_replaces_project_file_controls_and_handles_stale_recent(
    qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    warnings: list[str] = []
    monkeypatch.setattr(
        QMessageBox, "warning",
        lambda _parent, _title, message, *args, **kwargs: warnings.append(message),
    )
    window = MainWindow()
    try:
        actions = window.file_menu.actions()
        assert [action.text() for action in actions] == [
            "New Series", "Open Series", "Open Recent", "Import Images…", "", "Save", "Save As", "Export PNG",
        ]
        toolbar_labels = [
            action.text() for action in window.file_toolbar.actions()
        ]
        assert "New Series" not in toolbar_labels
        assert "Open Series" not in toolbar_labels
        assert "Save" not in toolbar_labels
        assert "Undo" in toolbar_labels and "Redo" in toolbar_labels
        assert "New Chapter" in toolbar_labels
        assert window.file_button.menu() is window.file_menu
        assert (
            window.file_button.popupMode()
            == QToolButton.ToolButtonPopupMode.InstantPopup
        )
        toolbar_widgets = [
            window.file_toolbar.widgetForAction(action)
            for action in window.file_toolbar.actions()
        ]
        assert toolbar_widgets[0] is window.file_button
        assert not any(
            action.menu() is window.file_menu
            for action in window.menuBar().actions()
        )

        stale = tmp_path / "Missing Series"
        window.settings.recent_series = [str(stale)]
        window._rebuild_recent_menu()
        recent_action = window.open_recent_menu.actions()[0]
        assert recent_action.text() == "Missing Series"
        recent_action.trigger()
        qapp.processEvents()
        assert warnings and str(stale) in warnings[-1]
        assert window.settings.recent_series == []
        assert window.open_recent_menu.actions()[0].text() == "No Recent Series"
    finally:
        _dispose(window)


def test_export_png_uses_full_chapter_size_and_collision_suffix(
    qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Export")
    chapter, _tiles = repository.create_chapter(series, "Bad:/Name")

    class FixedDateTime:
        @classmethod
        def now(cls):
            return datetime(2026, 8, 16, 12, 34, 56)

    monkeypatch.setattr(main_window_module, "datetime", FixedDateTime)
    window = MainWindow()
    assert window.open_series(repository.root)
    try:
        window._export_png()
        window._export_png()
        exports = sorted((repository.root / "exports").glob("*.png"))
        assert [path.name for path in exports] == [
            "Bad-Name-20260816-123456-2.png",
            "Bad-Name-20260816-123456.png",
        ]
        image = QImage(str(exports[0]))
        assert image.size().width() == chapter.width
        assert image.size().height() == chapter.height
        assert not list((repository.root / "exports").glob("*.tmp.png"))
    finally:
        _dispose(window)


def test_undo_redo_resolve_the_active_stack_after_series_tab_switches(
    qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    first_repository, _series, _chapter, _manifest = _repository_with_asset(
        tmp_path / "First"
    )
    second_repository = SeriesRepository(tmp_path / "Second")
    second_series = second_repository.create("Second")
    second_repository.create_chapter(second_series, "Chapter")
    window = MainWindow()
    assert window.open_series(first_repository.root)
    try:
        canvas = window.canvas
        page_id = canvas.chapter.root_page_ids[0]
        canvas.set_selection("layer", page_id, False)
        created = canvas._create_layer_from_world_bound(
            BoundGeometry.rectangle(100, 100, 180, 120)
        )
        assert created is not None
        created_id = created.layer_id

        window.undo_action.trigger()
        assert created_id not in canvas.chapter.layers
        window.redo_action.trigger()
        assert created_id in canvas.chapter.layers
        window._command_hotkey_actions["undo"]()
        assert created_id not in canvas.chapter.layers
        window._command_hotkey_actions["redo"]()
        assert created_id in canvas.chapter.layers

        first_key = window.active_session.key
        assert window.open_series(second_repository.root)
        first_index = window._tab_index_for_key(first_key)
        window.project_tabs.setCurrentIndex(first_index)
        qapp.processEvents()
        assert window.active_session.key == first_key
        window.undo_action.trigger()
        assert created_id not in window.canvas.chapter.layers
        window.redo_action.trigger()
        assert created_id in window.canvas.chapter.layers
    finally:
        _dispose(window)


def test_save_as_clones_open_state_rebinds_project_and_excludes_recovery(
    qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    source, original_series, chapter, manifest = _repository_with_asset(
        tmp_path / "Source"
    )
    extra = source.root / "extras" / "notes.bin"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"project-extra")
    chapter_root = source.chapter_root(chapter.chapter_id)

    unrelated = SeriesRepository(tmp_path / "Unrelated")
    unrelated_series = unrelated.create("Unrelated")
    unrelated.create_chapter(unrelated_series, "Other")

    window = MainWindow()
    assert window.open_series(source.root)
    window.show()
    try:
        errors: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda _parent, _title, message, *args, **kwargs: errors.append(message),
        )
        (chapter_root / "autosave").mkdir()
        (chapter_root / "autosave" / "recovery.json").write_text("{}")
        (chapter_root / "last_good").mkdir()
        (chapter_root / "last_good" / "old.txt").write_text("old")
        (chapter_root / PENDING_FILE).write_text("{}")
        (source.root / ".scratch.tmp").write_text("temporary")
        series_session = window.active_session
        series_session.chapter.name = "Dirty Chapter"
        raster = next(
            obj for obj in series_session.chapter.objects.values()
            if isinstance(obj, RasterObject)
        )
        series_session.tiles.paint_dab(
            raster.object_id, QPointF(30, 40), 16, QColor("#ff5533")
        )
        window._mark_dirty(None)
        series_stack = window.canvas.command_stack

        window._open_asset(manifest.asset_id)
        qapp.processEvents()
        asset_session = window.active_session
        asset_session.chapter.name = "Dirty Asset"
        window._mark_dirty(None)
        asset_stack = window.canvas.command_stack
        original_asset_key = asset_session.key

        assert window.open_series(unrelated.root)
        unrelated_session = window.active_session
        unrelated_key = unrelated_session.key
        unrelated_context = unrelated_session.context
        asset_index = window._tab_index_for_key(original_asset_key)
        window.project_tabs.setCurrentIndex(asset_index)
        qapp.processEvents()
        assert window.active_session is asset_session

        destination = tmp_path / "Clone"
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *args, **kwargs: str(tmp_path),
        )
        monkeypatch.setattr(
            QInputDialog, "getText",
            lambda *args, **kwargs: ("Clone", True),
        )

        assert window._save_as(), errors
        assert destination.is_dir()
        clone_repository = SeriesRepository(destination)
        cloned_series = clone_repository.load_series()
        assert cloned_series.name == original_series.name
        assert cloned_series.series_id != original_series.series_id
        cloned_chapter, cloned_tiles = clone_repository.load_chapter(
            chapter.chapter_id
        )
        assert cloned_chapter.name == "Dirty Chapter"
        assert cloned_tiles.content_bounds(raster.object_id) is not None
        cloned_manifest, _asset_tiles = AssetRepository(destination).load(
            manifest.asset_id
        )
        assert cloned_manifest.document.name == "Dirty Asset"
        assert AssetRepository(destination).thumbnail_path(
            manifest.asset_id
        ).is_file()
        assert (destination / "extras" / "notes.bin").read_bytes() == b"project-extra"
        assert not list(destination.rglob("autosave"))
        assert not list(destination.rglob("last_good"))
        assert not list(destination.rglob(PENDING_FILE))
        assert not list(destination.rglob("*.tmp"))

        assert source.load_series().series_id == original_series.series_id
        original_payload = json.loads(
            (chapter_root / "chapter.json").read_text(encoding="utf-8")
        )
        assert original_payload["name"] == "Chapter 1"
        assert (chapter_root / "autosave").is_dir()

        assert window.repository.root == destination.resolve()
        assert window.active_session.context.repository.root == destination.resolve()
        assert all(
            not session.dirty
            for session in (series_session, asset_session)
        )
        assert asset_session.key != original_asset_key
        assert window.canvas.command_stack is asset_stack
        assert series_session.canvas_state.command_stack is series_stack
        assert window.settings.recent_series[0] == str(destination.resolve())

        assert window.sessions[unrelated_key] is unrelated_session
        assert unrelated_session.context is unrelated_context
        assert unrelated_session.context.repository.root == unrelated.root
    finally:
        _dispose(window)


def test_save_as_failure_removes_only_staging_and_keeps_original_bindings(
    qapp, tmp_path, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    source, _series, _chapter, _manifest = _repository_with_asset(
        tmp_path / "Source"
    )
    window = MainWindow()
    assert window.open_series(source.root)
    window.show()
    try:
        session = window.active_session
        session.chapter.name = "Unsaved"
        window._mark_dirty(None)
        original_context = session.context
        original_key = session.key
        original_stack = window.canvas.command_stack
        destination = tmp_path / "Failed Clone"
        monkeypatch.setattr(
            QFileDialog, "getExistingDirectory",
            lambda *args, **kwargs: str(tmp_path),
        )
        monkeypatch.setattr(
            QInputDialog, "getText",
            lambda *args, **kwargs: ("Failed Clone", True),
        )
        errors: list[str] = []
        monkeypatch.setattr(
            QMessageBox, "critical",
            lambda _parent, _title, message, *args, **kwargs: errors.append(message),
        )
        monkeypatch.setattr(
            window, "_write_session_to_clone",
            lambda *args, **kwargs: (_ for _ in ()).throw(
                RuntimeError("injected clone failure")
            ),
        )

        assert not window._save_as()
        assert errors == ["injected clone failure"]
        assert not destination.exists()
        assert not list(tmp_path.glob(".Failed Clone.clone-*.tmpdir"))
        assert session.context is original_context
        assert session.key == original_key
        assert session.dirty and window._dirty
        assert window.repository.root == source.root
        assert window.canvas.command_stack is original_stack
    finally:
        _dispose(window)
