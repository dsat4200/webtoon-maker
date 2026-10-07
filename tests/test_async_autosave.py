"""Recovery saves keep editing responsive and persist detached revisions."""
from threading import Event, Timer, get_ident
import time

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QImage
from PySide6.QtGui import QPainterPath
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog, QInputDialog, QMessageBox

from comic_editor.core.assets import extract_asset
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject, SeriesDocument, ToneMask
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.ui.autosave import RecoverySnapshot
from comic_editor.ui.main_window import MainWindow


def wait_until(predicate, timeout=5000):
    deadline = time.monotonic()+timeout/1000
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate()


def wait_idle(window):
    wait_until(lambda: window._autosave_jobs.running is None and not window._autosave_jobs.pending and not window._autosave_jobs.captures)
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
    serialization_threads, serialize = [], ChapterDocument.to_dict
    def record_serialize(chapter):
        serialization_threads.append(get_ident())
        return serialize(chapter)

    def held(snapshot):
        worker_threads.append(get_ident())
        entered.set()
        assert release.wait(3), "GUI event loop did not release the worker"
        original(snapshot)

    monkeypatch.setattr(RecoverySnapshot, "write", held)
    monkeypatch.setattr(ChapterDocument, 'to_dict', record_serialize)
    window._mark_dirty(None)
    dirty_tiles = set(session.tiles.dirty)
    window._autosave()
    wait_until(entered.is_set)
    QTimer.singleShot(0, lambda: (heartbeat.set(), release.set()))
    wait_idle(window)
    assert heartbeat.is_set()
    assert worker_threads == [worker_threads[0]] and worker_threads[0] != get_ident()
    assert serialization_threads and all(thread != get_ident() for thread in serialization_threads)
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
        observed.append((snapshot.model.name if snapshot.model is not None else snapshot.chapter["name"],
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
        assert session.dirty  # Normal Save queues publication and remains live.
    finally:
        release.set()
        timer.join()
    wait_idle(window)
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


def test_manual_save_detaches_write_and_preserves_edits_and_shared_preferences(editor, monkeypatch):
    window, session = editor, editor.active_session
    entered, release, heartbeat = Event(), Event(), Event()
    original = RecoverySnapshot.write
    gui_thread, writer_threads = get_ident(), []
    original_serialize = ChapterDocument.to_dict
    serialization_threads = []
    def serialize(document):
        serialization_threads.append(get_ident())
        return original_serialize(document)
    def held(snapshot):
        if snapshot.manual:
            writer_threads.append(get_ident())
            entered.set()
            assert release.wait(3)
        original(snapshot)
    monkeypatch.setattr(RecoverySnapshot, "write", held)
    monkeypatch.setattr(ChapterDocument, "to_dict", serialize)
    session.chapter.name = "Captured revision"
    window._mark_dirty(None)
    assert window.save()
    wait_until(entered.is_set)
    QTimer.singleShot(0, heartbeat.set)
    session.chapter.name = "Later edits"
    session.tiles.paint_dab(window._test_object_id, QPointF(20, 20), 20, QColor("blue"))
    window._mark_dirty(None)
    session.context.series.primary_color = "#FF102030"
    window._schedule_series_preferences_save(immediate=True)
    release.set()
    wait_idle(window)
    assert heartbeat.is_set()
    assert writer_threads and all(thread != gui_thread for thread in writer_threads)
    assert serialization_threads and all(thread != gui_thread for thread in serialization_threads)
    assert session.dirty and session.tiles.dirty
    saved, tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.name == "Captured revision"
    assert tiles.tile(window._test_object_id, (0, 0)).pixelColor(20, 20) == QColor("red")
    assert session.context.repository.load_series().primary_color == "#FF102030"
    assert window.save()
    wait_idle(window)
    assert not session.dirty
    saved, tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.name == "Later edits"
    assert tiles.tile(window._test_object_id, (0, 0)).pixelColor(20, 20) == QColor("blue")


def test_manual_save_completion_follows_its_owner_after_tab_switch(editor, monkeypatch, tmp_path):
    window, first = editor, editor.active_session
    entered, release = Event(), Event()
    original = RecoverySnapshot.write
    def held(snapshot):
        if snapshot.manual:
            entered.set()
            assert release.wait(3)
        original(snapshot)
    monkeypatch.setattr(RecoverySnapshot, "write", held)
    first.chapter.name = "First saved"
    window._mark_dirty(None)
    assert window.save()
    wait_until(entered.is_set)
    repository, _ = make_project(tmp_path, "Manual second")
    assert window.open_series(repository.root)
    second = window.active_session
    window._mark_dirty(None)
    release.set()
    wait_idle(window)
    assert not first.dirty
    assert second.dirty and window._dirty
    saved, _ = first.context.repository.load_chapter(first.chapter.chapter_id)
    assert saved.name == "First saved"


def test_manual_save_series_capture_never_serializes_on_owner_thread(editor, monkeypatch):
    window, session = editor, editor.active_session
    owner_thread, threads = get_ident(), []
    original = SeriesDocument.to_dict
    def serialize(series):
        threads.append(get_ident())
        return original(series)
    monkeypatch.setattr(SeriesDocument, "to_dict", serialize)
    window._mark_dirty(None)
    assert window.save()
    session.context.series.secondary_color = "#FF203040"
    wait_idle(window)
    assert threads and all(thread != owner_thread for thread in threads)
    assert session.context.repository.load_series().secondary_color == "#FF203040"


def test_save_waits_for_released_async_fill_before_source_capture(editor, monkeypatch):
    window, session, entered, release = editor, editor.active_session, Event(), Event()
    canvas = window.canvas
    obj = session.chapter.objects[window._test_object_id]
    original = TileStore.advanced_fill
    def held(store, *args, **kwargs):
        if get_ident() != owner_thread:
            entered.set()
            assert release.wait(3)
        return original(store, *args, **kwargs)
    owner_thread = get_ident()
    monkeypatch.setattr(TileStore, 'advanced_fill', held)
    canvas.set_selection('object', obj.object_id)
    canvas._fill_operation_selection = canvas._drawing_selection_path
    profile = dict(canvas.settings.active_fill_profile(), close_gap=False, antialiasing=False)
    assert canvas._start_async_fill(obj, None, QRectF(0, 0, 300, 300), profile, None, QColor('blue'), 'area')
    wait_until(entered.is_set)
    window._mark_dirty(None)
    assert window.save()
    QTest.qWait(15)
    assert window._autosave_jobs.captures and window._autosave_jobs.running is None
    timer = Timer(.03, release.set)
    timer.start()
    try:
        assert window.save(wait=True)
    finally:
        release.set()
        timer.join()
    assert not session.dirty
    saved, tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert tiles.tile(obj.object_id, (0, 0)).pixelColor(200, 200) == QColor('blue')


def test_shared_preferences_reconcile_on_serial_writer_without_dirtying_art(editor, monkeypatch):
    window, session, entered, release = editor, editor.active_session, Event(), Event()
    owner_thread, writes = get_ident(), []
    original = SeriesRepository.save_series
    def held(repository, series):
        assert get_ident() != owner_thread
        writes.append(series.primary_color)
        if len(writes) == 1:
            entered.set()
            assert release.wait(3)
        return original(repository, series)
    monkeypatch.setattr(SeriesRepository, 'save_series', held)
    session.context.series.primary_color = '#FF112233'
    window._flush_series_preferences()
    wait_until(entered.is_set)
    session.context.series.primary_color = '#FF445566'
    window._flush_series_preferences()
    release.set()
    wait_idle(window)
    assert writes == ['#FF112233', '#FF445566']
    assert session.context.repository.load_series().primary_color == '#FF445566'
    assert not session.dirty and not window._dirty


def test_manual_capture_defers_a_new_source_contact_until_commit(editor):
    from comic_editor.ui.canvas import ToolKind
    window, session = editor, editor.active_session
    window._mark_dirty(None)
    assert window.save()
    canvas = window.canvas
    canvas.set_selection("object", window._test_object_id)
    canvas.set_tool(ToolKind.BRUSH)
    canvas._begin_paint_brush(QPointF(200, 200), 1)
    QTest.qWait(30)
    assert window._autosave_jobs.captures and window._autosave_jobs.running is None
    saved, tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert tiles.tile(window._test_object_id, (0, 0)).pixelColor(200, 200).alpha() == 0
    canvas._finish_paint_brush()
    wait_idle(window)
    saved, tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert tiles.tile(window._test_object_id, (0, 0)).pixelColor(200, 200).alpha() > 0


def test_failed_manual_save_keeps_dirty_owner_and_close_reports_failure(editor, monkeypatch):
    window, session = editor, editor.active_session
    errors = []
    def fail(snapshot):
        if snapshot.manual:
            raise OSError("Manual storage failure")
    monkeypatch.setattr(RecoverySnapshot, "write", fail)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Save)
    window._mark_dirty(None)
    assert window.save()
    wait_idle(window)
    assert session.dirty and window._dirty
    assert "Manual storage failure" in errors[-1]
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert window.sessions.get(session.key) is session
    assert session.dirty


def test_manual_asset_save_runs_thumbnail_off_thread_and_persists_source_pixels(editor, monkeypatch):
    from comic_editor.render import outputs
    window = editor
    raster = window.chapter.objects[window._test_object_id]
    manifest, tiles = extract_asset(window.chapter, window.canvas.tiles, "layer", raster.parent_layer_id, "Manual asset")
    repository = window.active_session.context.assets
    repository.create(manifest, tiles, None)
    window._open_asset(manifest.asset_id)
    session = window.active_session
    raster = next(obj for obj in session.chapter.objects.values() if isinstance(obj, RasterObject))
    session.tiles.paint_dab(raster.object_id, QPointF(20, 20), 20, QColor("blue"))
    threads, original = [], outputs.asset_thumbnail
    def thumbnail(*args, **kwargs):
        threads.append(get_ident())
        return original(*args, **kwargs)
    monkeypatch.setattr(outputs, "asset_thumbnail", thumbnail)
    window._mark_dirty(None)
    assert window.save()
    wait_until(lambda: window._autosave_jobs.running is None and not window._autosave_jobs.pending and not window._autosave_jobs.captures, timeout=20000)
    window.autosave_timer.stop()
    assert threads and all(thread != get_ident() for thread in threads)
    assert not session.dirty
    saved, saved_tiles = repository.load(manifest.asset_id)
    assert saved_tiles.tile(raster.object_id, (0, 0)).pixelColor(20, 20) == QColor("blue")
    assert repository.thumbnail_path(manifest.asset_id).is_file()
    assert tuple(saved.visual_bounds) == session.asset_manifest.visual_bounds


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


def test_save_then_immediate_close_waits_without_dirty_prompt(editor, monkeypatch):
    window, session = editor, editor.active_session
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: pytest.fail('Already requested Save prompted again'))
    window._mark_dirty(None)
    assert window.save()
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert session.key not in window.sessions
    saved, _tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.name == session.chapter.name


def test_save_then_window_close_waits_without_dirty_prompt(editor, monkeypatch):
    window, session = editor, editor.active_session
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: pytest.fail('Already requested Save prompted again'))
    window._mark_dirty(None)
    assert window.save()
    assert window.close()
    assert window._autosave_jobs.closed and not session.dirty
    saved, _tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.name == session.chapter.name


def test_save_then_new_edit_close_still_prompts(editor, monkeypatch):
    window, session = editor, editor.active_session
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: prompts.append(True) or QMessageBox.Cancel)
    window._mark_dirty(None)
    assert window.save()
    window.chapter.name = 'Later edit'
    window._mark_dirty(None)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert prompts == [True]
    assert window.sessions[session.key] is session


@pytest.mark.parametrize('action', ['cut', 'duplicate', 'mask', 'raster'])
@pytest.mark.parametrize('whole_window', [False, True])
def test_close_drains_accepted_cold_edit_before_dirty_prompt(editor, monkeypatch, action, whole_window):
    from comic_editor.render import input_capture
    from comic_editor.ui.canvas import ToolKind
    window, session = editor, editor.active_session
    obj = session.chapter.objects[window._test_object_id]
    if action == 'mask':
        mask = ToneMask(saved=True)
        session.chapter.masks[mask.mask_id] = mask
        source = session.tiles._tiles[obj.object_id].backing((0, 0))
        session.tiles._object_tiles(mask.mask_id).register((0, 0), source)
        session.tiles._alpha_bounds[mask.mask_id] = dict(session.tiles._alpha_bounds[obj.object_id])
        window._mark_dirty(None)
        assert window.save(wait=True)
        session.tiles.residency.clear(discard=True)
    entered, release = Event(), Event()
    if action in {'mask', 'raster'}:
        from comic_editor.ui import tile_input
        module, name = tile_input, 'prepare_input_tiles'
    else:
        module, name = input_capture, 'drawing_selection' if action == 'cut' else 'object_clipboard'
    original = getattr(module, name)
    def held(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    held.accepts_cancelled = action not in {'mask', 'raster'}
    monkeypatch.setattr(module, name, held)
    prompts = []
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: prompts.append(True) or QMessageBox.Save)
    window.canvas.set_selection('object', obj.object_id)
    if action == 'cut':
        window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        path = QPainterPath()
        path.addRect(QRectF(0, 0, 50, 50))
        window.canvas._drawing_selection_path = path
        assert window._cut_drawing_selection()
    elif action == 'duplicate':
        assert window._duplicate_outliner_object('object', obj.object_id)
    else:
        window.canvas.set_tool(ToolKind.RASTER_PENCIL)
        if action == 'mask':
            window.canvas.set_tone_mask_mode(mask.mask_id)
            window.canvas._begin_mask_stroke(QPointF(20, 20), .5)
            window.canvas._continue_mask_stroke(QPointF(35, 20), .9)
            window.canvas._end_mask_stroke()
        else:
            window.canvas._begin_stroke(QPointF(20, 20), .5)
            window.canvas._continue_stroke(QPointF(35, 20), .9)
            window.canvas._end_stroke()
    wait_until(entered.is_set)
    assert not session.dirty
    timer = Timer(.05, release.set)
    timer.start()
    try:
        if whole_window:
            assert window.close()
        else:
            window._close_project_tab(window._tab_index_for_key(session.key))
    finally:
        release.set()
        timer.join()
    assert prompts == [True]
    assert window._autosave_jobs.closed if whole_window else session.key not in window.sessions
    saved, saved_tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert len(session.canvas_state.command_stack._undo) == 1
    if action == 'cut':
        pixels = saved_tiles.tile(obj.object_id, (0, 0))
        assert pixels is None or pixels.pixelColor(20, 20).alpha() == 0
    elif action == 'duplicate':
        assert len(saved.objects) == 2
    else:
        identifier = mask.mask_id if action == 'mask' else obj.object_id
        assert saved_tiles.tile(identifier, (0, 0)).pixelColor(35, 20).alpha() > 0


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


@pytest.mark.parametrize('target', ['mask', 'raster'])
def test_manual_save_waits_for_released_native_input_before_source_capture(editor, target, monkeypatch):
    from comic_editor.ui import tile_input
    from comic_editor.ui.canvas import ToolKind
    window, session = editor, editor.active_session
    obj = session.chapter.objects[window._test_object_id]
    if target == 'mask':
        mask = ToneMask(saved=True)
        session.chapter.masks[mask.mask_id] = mask
        source = session.tiles._tiles[obj.object_id].backing((0, 0))
        session.tiles._object_tiles(mask.mask_id).register((0, 0), source)
        session.tiles._alpha_bounds[mask.mask_id] = dict(session.tiles._alpha_bounds[obj.object_id])
        window._mark_dirty(None)
        assert window.save(wait=True)
        session.tiles.residency.clear(discard=True)
        window.canvas.set_tone_mask_mode(mask.mask_id)
    window.canvas.set_selection('object', obj.object_id)
    window.canvas.set_tool(ToolKind.RASTER_PENCIL)
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles
    def held(*args):
        entered.set()
        assert release.wait(5)
        return original(*args)
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', held)
    begin = window.canvas._begin_mask_stroke if target == 'mask' else window.canvas._begin_stroke
    move = window.canvas._continue_mask_stroke if target == 'mask' else window.canvas._continue_stroke
    end = window.canvas._end_mask_stroke if target == 'mask' else window.canvas._end_stroke
    try:
        begin(QPointF(20, 20), .3)
        move(QPointF(40, 20), .8)
        end()
        wait_until(entered.is_set)
        assert window.save()
        assert window.canvas._drawing
        assert not session.dirty
        release.set()
        wait_idle(window)
        assert not window.canvas._drawing
        assert not session.dirty
        saved, saved_tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
        identifier = mask.mask_id if target == 'mask' else obj.object_id
        assert saved_tiles.tile(identifier, (0, 0)) == session.tiles.tile(identifier, (0, 0))
        assert saved_tiles.tile(identifier, (0, 0)).pixelColor(40, 20).alpha() > 0
        assert saved.to_dict() == session.chapter.to_dict()
    finally:
        release.set()


def test_manual_save_commits_pending_contributors_keeps_mask_mode_and_resets_history_baseline(editor):
    window, session = editor, editor.active_session
    obj = session.chapter.objects[window._test_object_id]
    mask = ToneMask(saved=True)
    session.chapter.masks[mask.mask_id] = mask
    window._mark_dirty(None)
    assert window.save(wait=True)
    window._enter_mask_mode(mask.mask_id)
    baseline = window.canvas.command_stack.revision
    assert window._toggle_mask_contributor('object', obj.object_id)
    supplier = window._save_capture_source(window._autosave_scope(session), session)
    assert not supplier()[-1]
    assert window.save(wait=True)
    assert window.canvas.active_tone_mask_id == mask.mask_id
    assert window._mask_original_contributors == [('object', obj.object_id)]
    assert window.canvas.command_stack.revision == baseline+1
    saved, _tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.masks[mask.mask_id].contributors == [('object', obj.object_id)]
    assert supplier()[-1]
    assert window._finish_mask_mode(True)
    assert window.canvas.command_stack.revision == baseline+1


def test_page_creation_temporary_height_pauses_recovery_and_manual_save_cancels_draft(editor, monkeypatch):
    monkeypatch.setattr(QMessageBox, 'question', lambda *_a: QMessageBox.Discard)
    monkeypatch.setattr(QMessageBox, 'critical', lambda *_a: None)
    window, session = editor, editor.active_session
    anchor = session.chapter.root_page_ids[-1]
    session.chapter.height = round(window.canvas.page_world_bounds(anchor).bottom())
    window._mark_dirty(None)
    assert window.save(wait=True)
    height = session.chapter.height
    assert window.canvas.begin_page_creation(anchor, 'rectangle')
    assert session.chapter.height > height
    supplier = window._save_capture_source(window._autosave_scope(session), session)
    assert not supplier()[-1]
    assert window.save(wait=True)
    assert not window.canvas._page_creation_anchor_id
    saved, _tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.height == session.chapter.height == height
    assert supplier()[-1]


def test_manual_save_finishes_pending_gradient_ramp_once_before_capture(editor):
    from comic_editor.core.models import ColorFillGradientObject, ColorGradientRamp, ColorGradientStop
    window, session = editor, editor.active_session
    obj = session.chapter.objects[window._test_object_id]
    gradient = session.chapter.add_object(obj.parent_layer_id, ColorFillGradientObject())
    window._mark_dirty(None)
    assert window.save(wait=True)
    window.canvas.set_selection('object', gradient.object_id, activate_default_tool=False)
    controls = window.gradient_tools_controls
    controls.refresh()
    before = gradient.ramp.to_dict()
    baseline = window.canvas.command_stack.revision
    controls._begin_ramp_edit()
    controls._preview_ramp(ColorGradientRamp(stops=[
        ColorGradientStop(position=0., color='#FFFF0000'),
        ColorGradientStop(position=1., color='#FF00FF00'),
    ]))
    supplier = window._save_capture_source(window._autosave_scope(session), session)
    assert not supplier()[-1]
    assert window.save(wait=True)
    assert controls._edit_before is None and not window.canvas._gradient_preview_active
    assert window.canvas.command_stack.revision == baseline+1
    saved, _tiles = session.context.repository.load_chapter(session.chapter.chapter_id)
    assert saved.objects[gradient.object_id].ramp.to_dict() == gradient.ramp.to_dict()
    controls._finish_ramp_edit()
    assert window.canvas.command_stack.revision == baseline+1
    window.canvas.command_stack.undo()
    assert gradient.ramp.to_dict() == before


@pytest.mark.parametrize('draft', ['_page_gap_draft', '_gradient_creation_before', '_model_before', '_mask_gradient_drag'])
def test_recovery_capture_rejects_active_authoring_transaction(editor, draft):
    window, session = editor, editor.active_session
    supplier = window._save_capture_source(window._autosave_scope(session), session)
    assert supplier()[-1]
    previous = getattr(window.canvas, draft)
    try:
        setattr(window.canvas, draft, {'active': True})
        assert not supplier()[-1]
    finally:
        setattr(window.canvas, draft, previous)
    assert supplier()[-1]


@pytest.mark.parametrize('preview', ['contributors', 'ramp', 'mask-gradient'])
def test_save_as_resolves_authoring_preview_once_before_publishing_clone(editor, tmp_path, monkeypatch, preview):
    from comic_editor.core.models import ColorFillGradientObject, ColorGradientRamp, ColorGradientStop

    window, session = editor, editor.active_session
    obj = session.chapter.objects[window._test_object_id]
    if preview == 'ramp':
        gradient = session.chapter.add_object(obj.parent_layer_id, ColorFillGradientObject())
    else:
        mask = ToneMask(saved=True)
        session.chapter.masks[mask.mask_id] = mask
    window._mark_dirty(None)
    assert window.save(wait=True)
    original_repository = session.context.repository

    if preview == 'ramp':
        window.canvas.set_selection('object', gradient.object_id, activate_default_tool=False)
        controls = window.gradient_tools_controls
        controls.refresh()
        original_value = gradient.ramp.to_dict()
    else:
        window._enter_mask_mode(mask.mask_id)
        original_value = list(mask.contributors) if preview == 'contributors' else None
    baseline = window.canvas.command_stack.revision
    if preview == 'contributors':
        assert window._toggle_mask_contributor('object', obj.object_id)
        expected = list(mask.contributors)
    elif preview == 'ramp':
        controls._begin_ramp_edit()
        controls._preview_ramp(ColorGradientRamp(stops=[
            ColorGradientStop(position=0., color='#FFFF0000'),
            ColorGradientStop(position=1., color='#FF00FF00'),
        ]))
        expected = gradient.ramp.to_dict()
    else:
        window.canvas._mask_gradient_press(QPointF(30, 40))
        window.canvas._mask_gradient_move(QPointF(150, 40))
        expected = mask.gradient.to_dict()
    assert window.canvas.command_stack.revision == baseline
    supplier = window._save_capture_source(window._autosave_scope(session), session)
    assert not supplier()[-1]

    monkeypatch.setattr(QFileDialog, 'getExistingDirectory', lambda *_a, **_k: str(tmp_path))
    monkeypatch.setattr(QInputDialog, 'getText', lambda *_a, **_k: ('Preview clone', True))
    errors = []
    monkeypatch.setattr(QMessageBox, 'critical', lambda _p, _t, message, *_a: errors.append(message))
    capture_source = window._save_capture_source
    polls = 0
    def bounded_source(*args, **kwargs):
        source = capture_source(*args, **kwargs)
        def checked():
            nonlocal polls
            polls += 1
            assert polls <= 3, 'Save As never resolved its pending authoring transaction'
            return source()
        return checked
    monkeypatch.setattr(window, '_save_capture_source', bounded_source)

    assert window._save_as(), errors
    assert not errors
    destination = (tmp_path/'Preview clone').resolve()
    assert window.repository.root == session.context.repository.root == destination
    assert not session.dirty and not window._dirty
    assert window.canvas.command_stack.revision == baseline+1
    cloned, _tiles = SeriesRepository(destination).load_chapter(session.chapter.chapter_id)
    original, _tiles = original_repository.load_chapter(session.chapter.chapter_id)
    if preview == 'contributors':
        assert cloned.masks[mask.mask_id].contributors == expected
        assert original.masks[mask.mask_id].contributors == original_value
        assert window._mask_original_contributors == expected
        assert window._mask_contributors_before.records['masks'][mask.mask_id]['contributors'] == expected
        assert window.canvas.active_tone_mask_id == mask.mask_id
        assert window._finish_mask_mode(True)
    elif preview == 'ramp':
        assert cloned.objects[gradient.object_id].ramp.to_dict() == expected
        assert original.objects[gradient.object_id].ramp.to_dict() == original_value
        assert controls._edit_before is None and not window.canvas._gradient_preview_active
        controls._finish_ramp_edit()
    else:
        assert cloned.masks[mask.mask_id].gradient.to_dict() == expected
        assert original.masks[mask.mask_id].gradient is None
        assert window.canvas._mask_gradient_drag is None
        assert window.canvas.active_tone_mask_id == mask.mask_id
        assert window._finish_mask_mode(True)
    assert window.canvas.command_stack.revision == baseline+1
    window.canvas.command_stack.undo()
    if preview == 'contributors':
        assert mask.contributors == original_value
    elif preview == 'ramp':
        assert gradient.ramp.to_dict() == original_value
    else:
        assert mask.gradient is None
    window.canvas.command_stack.redo()
    if preview == 'contributors':
        assert mask.contributors == expected
    elif preview == 'ramp':
        assert gradient.ramp.to_dict() == expected
    else:
        assert mask.gradient.to_dict() == expected
