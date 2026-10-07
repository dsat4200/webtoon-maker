"""Save capture must retain the live owner's cold recovery pixels."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident

import pytest

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import RasterObject, ToneMask
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tile_backing import PrefetchedPins, SnapshotBacking
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene import SceneSnapshotCompiler
from comic_editor.ui.autosave import RecoveryCapture
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def test_manual_capture_keeps_live_recovery_tiles_after_autosave_cleanup(tmp_path, monkeypatch):
    repository = SeriesRepository(tmp_path / 'project')
    series = repository.create('Recovery ownership')
    chapter, tiles = repository.create_chapter(series, 'Chapter')
    oid = next(obj.object_id for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    mask = ToneMask(name='Painted mask', saved=True)
    chapter.masks[mask.mask_id] = mask
    for identifier in (oid, mask.mask_id):
        tiles.paint_dab(identifier, QPointF(40, 50), 20, QColor('red'))
    repository.save_chapter(chapter, tiles, autosave=True)
    recovery = repository.chapter_root(chapter.chapter_id) / 'autosave'
    live = TileStore()
    live.load_directory(recovery / 'raster', {oid})
    live.load_directory(recovery / 'masks', {mask.mask_id}, clear=False)
    original_pin, pin_threads = SnapshotBacking.pin, []
    def pin(backing, path):
        pin_threads.append(get_ident())
        return original_pin(backing, path)
    monkeypatch.setattr(SnapshotBacking, 'pin', pin)
    capture = RecoveryCapture(repository.root, chapter, live, ImageStore(), manual=True)
    while not capture.advance():
        pass
    snapshot = capture.result
    for job in snapshot.pin_jobs:
        for identifier, key, _version, _path, pinned in job.result():
            owner = snapshot.tile_store._tiles[identifier]
            owner.entries[key], owner.snapshot_pins[key] = pinned.path, pinned
    assert pin_threads and all(thread != get_ident() for thread in pin_threads)
    assert live.residency.decodes == 0
    snapshot.write()
    assert not recovery.exists()
    # Other detached consumers also need to adopt the live owner's pending
    # pin rather than trying to reopen the deleted recovery filename.
    detached = live.detached_snapshot({oid})
    assert detached.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('red')
    assert live.residency.decodes == 0
    # A second cold capture must reuse surviving originals before a visible
    # read or any UI completion callback has adopted the captured pins.
    capture = RecoveryCapture(repository.root, chapter, live, ImageStore(), manual=True)
    while not capture.advance():
        pass
    assert not capture.result.pin_jobs
    capture.result.write()
    for identifier in (oid, mask.mask_id):
        assert live.tile(identifier, (0, 0)).pixelColor(40, 50) == QColor('red')
    # Saving an earlier snapshot cannot replace newer borrowed live pixels.
    borrowed = live.tile(oid, (0, 0))
    borrowed.setPixelColor(40, 50, QColor('blue'))
    capture.result.write()
    live._tiles[oid].version((0, 0))
    assert live.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('blue')
    assert live._tiles[oid].snapshot_pin((0, 0)) is None
    _, reopened = repository.load_chapter(chapter.chapter_id)
    assert reopened.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('red')


@pytest.fixture
def recovered_canvas(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    repository = SeriesRepository(tmp_path/'project')
    series = repository.create('Recovery source boundaries')
    chapter, tiles = repository.create_chapter(series, 'Chapter')
    oid = next(obj.object_id for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    for identifier in (oid, mask.mask_id):
        tiles.paint_dab(identifier, QPointF(40, 50), 20, QColor('red'))
    repository.save_chapter(chapter, tiles, autosave=True)
    recovery = repository.chapter_root(chapter.chapter_id)/'autosave'
    live = TileStore()
    live.load_directory(recovery/'raster', {oid})
    live.load_directory(recovery/'masks', {mask.mask_id}, clear=False)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', snap_to_grid=False,
        brush_size=8, predictive_ink=False))
    canvas.set_document(chapter, live)
    canvas.set_selection('object', oid)
    canvas.primary_color = '#FF0000FF'
    yield canvas, repository, oid, mask, recovery
    canvas._cancel_mask_stroke()
    canvas._cancel_native_raster_stroke()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def finish_capture(capture):
    while not capture.advance():
        pass
    assert not capture.stale
    return capture.result


def publish_manual_snapshot(snapshot):
    for job in snapshot.pin_jobs:
        for identifier, key, _version, _path, pinned in job.result():
            owner = snapshot.tile_store._tiles[identifier]
            owner.entries[key], owner.snapshot_pins[key] = pinned.path, pinned
    snapshot.write()


def test_fresh_scene_capture_reuses_live_recovery_pins_before_any_read_or_save_completion(recovered_canvas, monkeypatch):
    canvas, repository, oid, mask, recovery = recovered_canvas
    snapshot = finish_capture(RecoveryCapture(repository.root, canvas.chapter, canvas.tiles,
        canvas.images, manual=True))
    publish_manual_snapshot(snapshot)
    assert not recovery.exists()
    assert canvas.tiles.residency.decodes == 0
    assert all(not owner.snapshot_pins and owner.pending_snapshot_pins
               for owner in canvas.tiles._tiles.values())
    owner_thread, original = get_ident(), SnapshotBacking.pin
    def no_gui_pin(backing, path):
        assert get_ident() != owner_thread, 'Scene capture performed pin IO on the GUI thread'
        return original(backing, path)
    monkeypatch.setattr(SnapshotBacking, 'pin', no_gui_pin)

    compiler = SceneSnapshotCompiler()
    fresh = finish_capture(compiler.capture(canvas, canvas._render_document_state()))
    # The ready original owner is shared, rather than reopening a recovery
    # filename that the writer removed or repinning it under another key.
    assert not fresh.pin_jobs
    for identifier in (oid, mask.mask_id):
        assert fresh.tiles._tiles[identifier].snapshot_pins[(0, 0)] is canvas.tiles._tiles[identifier].snapshot_pins[(0, 0)]
    def read_sources():
        fresh.finish_sources()
        return [QImage(fresh.tiles.tile(identifier, (0, 0))) for identifier in (oid, mask.mask_id)]
    with ThreadPoolExecutor(max_workers=1) as workers:
        images = workers.submit(read_sources).result(timeout=5)
    assert all(image.pixelColor(40, 50) == QColor('red') for image in images)
    assert canvas.tiles.residency.decodes == 0


@pytest.mark.parametrize('target', ['raster', 'mask'])
def test_resident_native_contact_never_waits_for_pending_recovery_pin(recovered_canvas, monkeypatch, target):
    canvas, repository, oid, mask, _recovery = recovered_canvas
    identifier = oid if target == 'raster' else mask.mask_id
    before = QImage(canvas.tiles.tile(identifier, (0, 0)))
    if target == 'mask':
        canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    begin, move, end = ((canvas._begin_stroke, canvas._continue_stroke, canvas._end_stroke)
        if target == 'raster' else (canvas._begin_mask_stroke, canvas._continue_mask_stroke, canvas._end_mask_stroke))
    owner_thread, entered, release = get_ident(), Event(), Event()
    original_pin, original_lookup = SnapshotBacking.pin, PrefetchedPins.pin
    source_path = canvas.tiles._tiles[identifier].entries[(0, 0)]
    def held_pin(backing, path):
        assert get_ident() != owner_thread, 'Resident input performed filesystem pinning'
        if path == source_path:
            entered.set()
            assert release.wait(5), 'Resident input waited for its pending pin'
        return original_pin(backing, path)
    def no_gui_lookup(job, object_id, key):
        assert get_ident() != owner_thread, 'Resident input waited for recovery pin IO'
        return original_lookup(job, object_id, key)
    original_prepare = canvas.tiles.residency.prepare
    def no_resident_prepare(owner, key):
        assert (owner, key) not in canvas.tiles.residency.entries, 'Resident input entered a backing preparation hook'
        return original_prepare(owner, key)
    monkeypatch.setattr(SnapshotBacking, 'pin', held_pin)
    monkeypatch.setattr(PrefetchedPins, 'pin', no_gui_lookup)
    monkeypatch.setattr(canvas.tiles.residency, 'prepare', no_resident_prepare)
    snapshot = finish_capture(RecoveryCapture(repository.root, canvas.chapter, canvas.tiles,
        canvas.images, manual=True))
    try:
        assert entered.wait(2)
        assert canvas.tiles._tiles[identifier].pending_snapshot_pins
        baseline = canvas.command_stack.revision
        begin(QPointF(40, 50), .7)
        assert not getattr(canvas, '_native_input_error', None)
        move(QPointF(48, 54), .3)
        end()
        assert not release.is_set()
        assert not getattr(canvas, '_native_input_error', None)
        assert canvas.command_stack.revision == baseline+1
        after = QImage(canvas.tiles.tile(identifier, (0, 0)))
        assert after != before and after.pixelColor(40, 50).blue() > before.pixelColor(40, 50).blue()
        canvas.command_stack.undo()
        assert canvas.tiles.tile(identifier, (0, 0)) == before
        canvas.command_stack.redo()
        assert canvas.tiles.tile(identifier, (0, 0)) == after
        release.set()
        publish_manual_snapshot(snapshot)
        # The older writer's original pixels cannot replace the accepted ink.
        assert canvas.tiles.tile(identifier, (0, 0)) == after
        _, saved = repository.load_chapter(canvas.chapter.chapter_id)
        with ThreadPoolExecutor(max_workers=1) as workers:
            assert workers.submit(saved.tile, identifier, (0, 0)).result(timeout=5) == before
    finally:
        release.set()
        for job in snapshot.pin_jobs:
            job.result()


def test_failed_pin_batch_does_not_make_later_scene_capture_pin_on_gui(recovered_canvas, monkeypatch):
    canvas, _repository, oid, _mask, recovery = recovered_canvas
    source = canvas.tiles._tiles[oid]
    image = QImage(canvas.tiles.tile(oid, (0, 0)))
    version, path = source.version((0, 0)), source.entries[(0, 0)]
    job = PrefetchedPins(SnapshotBacking(), (
        (oid, (-1, -1), (0, 0), recovery/'missing.png'),
        (oid, (0, 0), version, path),
    ))
    canvas.tiles.retain_snapshot_pins(job)
    with pytest.raises(OSError):
        job.result()
    # The failed first address prevented the batch from visiting this valid
    # resident source. Batch completion alone does not make its pin ready.
    assert (oid, (0, 0)) not in job.values
    assert source.pending_snapshot_pins[(0, 0)][2] is job
    owner_thread, original = get_ident(), SnapshotBacking.pin
    pin_threads = []
    def no_gui_pin(backing, file_path):
        pin_threads.append(get_ident())
        assert get_ident() != owner_thread, 'Failed batch caused GUI pin IO'
        return original(backing, file_path)
    monkeypatch.setattr(SnapshotBacking, 'pin', no_gui_pin)
    fresh = finish_capture(SceneSnapshotCompiler().capture(canvas, canvas._render_document_state()))
    assert (oid, (0, 0)) not in job.values
    assert fresh.pin_jobs
    def read_source():
        fresh.finish_sources()
        return QImage(fresh.tiles.tile(oid, (0, 0)))
    with ThreadPoolExecutor(max_workers=1) as workers:
        assert workers.submit(read_source).result(timeout=5) == image
    assert pin_threads and all(thread != owner_thread for thread in pin_threads)
    assert canvas.tiles.tile(oid, (0, 0)) == image
