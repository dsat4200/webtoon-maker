"""Recovery saves keep editing responsive and persist detached revisions."""
from threading import Event, Timer, get_ident
import time

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from comic_editor.core.assets import extract_asset
from comic_editor.core.models import ImageObject, RasterObject, ToneMask
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.autosave import RecoverySnapshot
from comic_editor.ui.main_window import MainWindow


def wait_until(predicate, timeout=5000):
    deadline = time.monotonic()+timeout/1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate()


def wait_idle(window):
    wait_until(lambda: window._autosave_jobs.running is None and not window._autosave_jobs.pending)
    window.autosave_timer.stop()


def make_project(path, name):
    repository = SeriesRepository(path/name)
    series = repository.create(name)
    chapter, tiles = repository.create_chapter(series, name)
    obj = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    tiles.paint_dab(obj.object_id, QPointF(20, 20), 20, QColor("red"))
    repository.save_chapter(chapter, tiles)
    return repository, obj.object_id


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    repository, object_id = make_project(tmp_path, "First")
    window = MainWindow()
    assert window.open_series(repository.root)
    window._test_object_id = object_id
    window.autosave_timer.stop()
    yield window
    window.autosave_timer.stop()
    window._autosave_jobs.shutdown()
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.close()
    window.deleteLater()


def test_autosave_worker_leaves_gui_live_and_skips_unchanged_revision(editor, monkeypatch):
    window = editor
    session = window.active_session
    entered, release, heartbeat = Event(), Event(), Event()
    original = RecoverySnapshot.write
    worker_threads = []

    def held(snapshot):
        worker_threads.append(get_ident())
        entered.set()
        assert release.wait(3), "GUI event loop did not release the worker"
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    window._mark_dirty(None)
    dirty_tiles = set(session.tiles.dirty)
    window._autosave()
    wait_until(entered.is_set)
    QTimer.singleShot(0, lambda: (heartbeat.set(), release.set()))
    wait_idle(window)
    assert heartbeat.is_set()
    assert worker_threads == [worker_threads[0]] and worker_threads[0] != get_ident()
    assert session.recovery_revision == session.edit_revision
    assert session.last_autosave > 0
    assert session.dirty  # Manual-save status is independent of recovery status.
    assert session.tiles.dirty == dirty_tiles
    assert session.context.repository.has_recovery(session.chapter.chapter_id)
    submitted = window._autosave_jobs.submitted
    session.last_autosave = 0
    window._autosave()
    assert window._autosave_jobs.submitted == submitted


def test_autosave_snapshot_survives_new_edits_and_stale_completion(editor, monkeypatch):
    window, session = editor, editor.active_session
    old_entered, new_entered = Event(), Event()
    old_release, new_release = Event(), Event()
    original = RecoverySnapshot.write
    observed = []
    object_id = window._test_object_id

    def held(snapshot):
        index = len(observed)
        observed.append((snapshot.chapter["name"],
                         snapshot.tiles[object_id][(0, 0)].pixelColor(20, 20).name()))
        (old_entered if index == 0 else new_entered).set()
        assert (old_release if index == 0 else new_release).wait(3)
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    session.chapter.name = "Old snapshot"
    window._mark_dirty(None)
    window._autosave()
    wait_until(old_entered.is_set)
    session.chapter.name = "New edits"
    session.tiles.paint_dab(object_id, QPointF(20, 20), 20, QColor("blue"))
    window._mark_dirty(None)
    revision = session.edit_revision
    window._autosave()
    old_release.set()
    wait_until(new_entered.is_set)
    assert session.recovery_revision != revision
    assert session.last_autosave == 0
    assert observed == [("Old snapshot", "#ff0000"), ("New edits", "#0000ff")]
    new_release.set()
    wait_idle(window)
    chapter, tiles = session.context.repository.load_chapter(session.chapter.chapter_id, recover=True)
    assert chapter.name == "New edits"
    assert tiles.tile(object_id, (0, 0)).pixelColor(20, 20) == QColor("blue")
    assert session.recovery_revision == revision


def test_autosave_completion_follows_owner_when_switching_projects(editor, monkeypatch, tmp_path):
    window, first = editor, editor.active_session
    entered, release = Event(), Event()
    original = RecoverySnapshot.write

    def held(snapshot):
        entered.set()
        assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    first.chapter.name = "First changed"
    window._mark_dirty(None)
    window._autosave()
    wait_until(entered.is_set)
    repository, _ = make_project(tmp_path, "Second")
    assert window.open_series(repository.root)
    second = window.active_session
    second.chapter.name = "Second changed"
    window._mark_dirty(None)
    window._autosave()
    release.set()
    wait_idle(window)
    assert window.active_session is second
    assert first.recovery_revision == first.edit_revision
    assert second.recovery_revision == second.edit_revision
    for session, name in [(first, "First changed"), (second, "Second changed")]:
        recovered, _ = session.context.repository.load_chapter(session.chapter.chapter_id, recover=True)
        assert recovered.name == name


def test_manual_save_finishes_conflicting_autosave_before_clearing_recovery(editor, monkeypatch):
    window, session = editor, editor.active_session
    entered, release = Event(), Event()
    original = RecoverySnapshot.write

    def held(snapshot):
        entered.set()
        assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    window._mark_dirty(None)
    window._autosave()
    wait_until(entered.is_set)
    session.chapter.name = "Explicit latest save"
    window._mark_dirty(None)
    timer = Timer(.05, release.set)
    timer.start()
    try:
        assert window.save()
    finally:
        release.set()
        timer.join()
    assert window._autosave_jobs.running is None
    assert not session.dirty
    assert not session.context.repository.has_recovery(session.chapter.chapter_id)
    saved, _ = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.name == "Explicit latest save"
    window._autosave()
    assert window._autosave_jobs.running is None


def test_failed_autosave_does_not_mark_revision_and_can_retry(editor, monkeypatch):
    window, session = editor, editor.active_session
    original = RecoverySnapshot.write

    def fail(snapshot):
        raise OSError("Test storage failure")

    monkeypatch.setattr(RecoverySnapshot, "write", fail)
    window._mark_dirty(None)
    window._autosave()
    wait_idle(window)
    assert session.recovery_revision != session.edit_revision
    assert session.last_autosave == 0
    assert "Test storage failure" in window.statusBar().currentMessage()
    monkeypatch.setattr(RecoverySnapshot, "write", original)
    window._autosave()
    wait_idle(window)
    assert session.recovery_revision == session.edit_revision


def png(color):
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(data)


def test_asset_autosave_preserves_image_mask_snapshots_and_removed_tiles(editor):
    window = editor
    raster = window.chapter.objects[window._test_object_id]
    manifest, tiles = extract_asset(window.chapter, window.canvas.tiles, "layer",
                                   raster.parent_layer_id, "Autosaved asset")
    repository = window.active_session.context.assets
    repository.create(manifest, tiles, None)
    window._open_asset(manifest.asset_id)
    session = window.active_session
    raster = next(obj for obj in session.chapter.objects.values() if isinstance(obj, RasterObject))
    mask = ToneMask(name="Recovery mask", saved=True)
    session.chapter.masks[mask.mask_id] = mask
    image = session.chapter.add_object(raster.parent_layer_id, ImageObject(
        source_filename="original.png", source_mime_type="image/png",
        pixel_width=8, pixel_height=8,
    ))
    session.images.put(image.object_id, "original.png", png("red"), "image/png")
    for identifier in (raster.object_id, mask.mask_id):
        session.tiles.paint_dab(identifier, QPointF(280, 20), 15, QColor("red"))
    window._mark_dirty(None)
    window._autosave()
    wait_idle(window)
    for identifier in (raster.object_id, mask.mask_id):
        session.tiles.set_tile(identifier, (1, 0), None)
        session.tiles.paint_dab(identifier, QPointF(20, 20), 15, QColor("blue"))
    session.images.put(image.object_id, "original.png", png("blue"), "image/png")
    dirty_images = set(session.images.dirty)
    window._mark_dirty(None)
    session.last_autosave = 0
    window._autosave()
    wait_idle(window)
    recovered, saved_tiles, saved_images = repository.load(manifest.asset_id, recover=True, include_images=True)
    assert mask.mask_id in recovered.document.masks
    assert saved_images.source(image.object_id).data == png("blue")
    assert session.images.dirty == dirty_images
    for identifier in (raster.object_id, mask.mask_id):
        assert (1, 0) not in saved_tiles.object_tiles(identifier)
        assert saved_tiles.tile(identifier, (0, 0)).pixelColor(20, 20) == QColor("blue")


def test_renaming_dirty_asset_refreshes_recovery_after_newer_manual_manifest(editor, monkeypatch):
    window = editor
    raster = window.chapter.objects[window._test_object_id]
    manifest, tiles = extract_asset(window.chapter, window.canvas.tiles, "layer",
                                   raster.parent_layer_id, "Original asset")
    repository = window.active_session.context.assets
    repository.create(manifest, tiles, None)
    window._open_asset(manifest.asset_id)
    session = window.active_session
    session.chapter.name = "Unsaved content"
    window._mark_dirty(None)
    window._autosave()
    wait_idle(window)
    revision = session.recovery_revision
    monkeypatch.setattr(QInputDialog, "getText", lambda *args, **kwargs: ("Renamed asset", True))
    window._rename_asset(manifest.asset_id)
    assert session.edit_revision > revision
    assert session.last_autosave == window._last_autosave == 0
    window._autosave()
    wait_idle(window)
    assert repository.has_recovery(manifest.asset_id)
    recovered, _ = repository.load(manifest.asset_id, recover=True)
    assert recovered.name == "Renamed asset"
    assert recovered.document.name == "Unsaved content"
    assert session.recovery_revision == session.edit_revision


def test_closing_project_drains_its_writer_before_removing_session(editor, monkeypatch):
    window, session = editor, editor.active_session
    entered, release = Event(), Event()
    original = RecoverySnapshot.write

    def held(snapshot):
        entered.set()
        assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Discard)
    window._mark_dirty(None)
    window._autosave()
    wait_until(entered.is_set)
    timer = Timer(.05, release.set)
    timer.start()
    try:
        window._close_project_tab(window.project_tabs.currentIndex())
    finally:
        release.set()
        timer.join()
    assert session.key not in window.sessions
    assert window._autosave_jobs.running is None
    assert window.chapter is None
    window._autosave_jobs.poll()


def test_window_shutdown_finishes_current_write_and_rejects_new_jobs(editor, monkeypatch):
    window = editor
    entered, release = Event(), Event()
    original = RecoverySnapshot.write

    def held(snapshot):
        entered.set()
        assert release.wait(3)
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Discard)
    window._mark_dirty(None)
    window._autosave()
    wait_until(entered.is_set)
    timer = Timer(.05, release.set)
    timer.start()
    try:
        window.close()
    finally:
        release.set()
        timer.join()
    assert window._autosave_jobs.closed
    assert window._autosave_jobs.running is None
    submitted = window._autosave_jobs.submitted
    window._autosave()
    assert window._autosave_jobs.submitted == submitted
