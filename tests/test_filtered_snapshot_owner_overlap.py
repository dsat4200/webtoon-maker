"""Observe real source-owner boundaries; never bypass render admission.

Blur is excluded from finite feedback. This concerns a retired finite filter,
selection of another Raster, and its first cold contact. The companion control
qualifies the heavy admission that already protects ordinary cancellation.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread, get_ident
import time

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (BoundGeometry, ChapterDocument,
    HueSaturationLightnessModifier, OutlineModifier, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.tile_backing import DiskTileMap
from comic_editor.core.images import ImageStore
from comic_editor.render.admission import RENDER_ADMISSION
from comic_editor.render.filtered_raster_feedback import FilteredRasterFeedback
from comic_editor.render.service import RenderRequest
from comic_editor.ui.canvas import CanvasWidget
from test_filtered_native_recipe import native, ordinary


def _wait(predicate, timeout=15.):
    # Do not pump Qt: a cold contact must remain unadmitted throughout the
    # source handoff, and unrelated queued widget paints are not this oracle.
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline, 'Owner boundary did not arrive'
        time.sleep(.001)


def _reference(snapshot):
    request = RenderRequest((0., 0., 512., 256.), 1., (512, 256),
        ('independent-owner-overlap-reference',), snapshot.document.revision)
    with ThreadPoolExecutor(1) as pool:
        return native(pool.submit(ordinary, snapshot, request).result(30.))


def _fixture(tmp_path, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False))
    try:
        chapter = ChapterDocument(width=512, height=256, document_kind='asset', background='#00000000')
        page = chapter.add_page('Page', BoundGeometry.rectangle(0., 0., 512., 256.))
        page.shape_style.outline_thickness = 0
        filtered = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0., 0., 512., 256.)))
        plain = chapter.add_object(page.layer_id, RasterObject(x=256., interaction_rect=(0., 0., 256., 256.)))
        tiles, images = TileStore(), ImageStore()
        for key, color in (((0, 0), '#a0603377'), ((1, 0), '#b0297766')):
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor(color))
            tiles.set_tile(filtered.object_id, key, image)
        cold_image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        cold_image.fill(QColor('#80778833'))
        tiles.set_tile(plain.object_id, (0, 0), cold_image)
        cold_path = tmp_path / 'cold-plain-original.png'
        assert cold_image.save(str(cold_path), 'PNG')
        cold_source = tiles._tiles[plain.object_id]
        cold_source.register((0, 0), cold_path)
        tiles.residency.remove(cold_source, (0, 0))
        for modifier in (HueSaturationLightnessModifier(hue=37.), OutlineModifier(thickness=8.)):
            chapter.add_modifier(modifier, [('object', filtered.object_id)])
        canvas.set_document(chapter, tiles, images)
        canvas.set_selection('object', filtered.object_id)
        canvas.resize(512, 256)
        canvas.center_x, canvas.center_y, canvas.scale = 256., 128., 1.
        # Ordinary projection binding clears the old document and increments
        # its revision. Establish it before capture, just as a first canvas
        # presentation must settle before an immutable worker demand is usable.
        configuration = canvas._projection_configuration()
        canvas._document_projection.configure((*configuration, None), document=configuration[:3])
        document = canvas._render_document_state()
        capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        while not capture.advance(.001):
            pass
        snapshot = capture.result
        assert snapshot is not None
        frozen_source = snapshot.tiles._tiles[filtered.object_id]
        assert all(frozen_source.entries[key].frozen is None for key in ((0, 0), (1, 0)))
        original = _reference(snapshot)
        original_model = snapshot.chapter.to_dict()
        authored_bytes = cold_path.read_bytes()
        return canvas, filtered, plain, snapshot, original, original_model, cold_path, authored_bytes
    except BaseException:
        # Construction/reference assertions happen before the caller's finally.
        # Retire the actual input/controller owners and join their executors.
        _finish(canvas, (), ())
        raise


def _dispatch(canvas, filtered, snapshot):
    controller = canvas._scene_controller
    visible, phases = QRectF(0., 0., 512., 256.), (None,)
    requests = tuple(canvas._document_projection.requests(visible, 1.))
    assert requests and all(request.address.level == 0 for request in requests)
    assert canvas._render_document_state() == snapshot.document
    assert canvas._document_projection._document == snapshot.document.identity
    controller.snapshot = snapshot
    controller.request(snapshot.document, requests, phases, visible)
    controller.advance()
    assert controller.feedback_target == filtered.object_id
    assert controller.desired[0] == snapshot.document
    assert controller.dispatched == controller.desired and controller.capture is None
    assert controller.scheduler.future is not None
    return controller.scheduler.future, requests, phases, visible


def _finish(canvas, workers, releases):
    for release in releases:
        release.set()
    canvas._clear_detached_input_state()
    for worker in workers:
        worker.close()
        worker.executor.shutdown(wait=True)
    for scheduler in (canvas._scene_controller.scheduler, canvas._scene_controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas._visual_frame_timer.stop()
    canvas.deleteLater()


def test_heavy_admission_serializes_cancelled_predecessor_and_eager_filtered_fork(qapp, tmp_path, monkeypatch):
    canvas, filtered, _plain, snapshot, original, model, path, authored = _fixture(tmp_path, monkeypatch)
    scheduler = canvas._scene_controller.scheduler
    published, publication_release = Event(), Event()
    capture_started, capture_release, fork_started = Event(), Event(), Event()
    scheduler_threads, fork_threads, workers = set(), set(), []
    original_detach = TileStore.detached_snapshot
    original_version = DiskTileMap.version

    def detached(store, identifiers):
        if store is snapshot.tiles and current_thread().name.startswith('native-filtered-contact'):
            fork_threads.add(get_ident())
            fork_started.set()
        return original_detach(store, identifiers)

    monkeypatch.setattr(TileStore, 'detached_snapshot', detached)

    def version(owner, key):
        if (owner.residency is snapshot.tiles.residency and get_ident() in scheduler_threads
                and scheduler.cancelled.is_set() and not capture_started.is_set()):
            # ReadyOriginals.capture has fetched the first actual LRU entry
            # before asking its owner for this native version witness.
            capture_started.set()
            assert capture_release.wait(15.), 'Cancellation capture was not released'
        return original_version(owner, key)

    monkeypatch.setattr(DiskTileMap, 'version', version)

    def published_callback():
        # _publish invokes the public wake callback on its producer after
        # queuing feedback, while that producer still owns its heavy ticket.
        with scheduler.condition:
            ready = any(isinstance(item.feedback, FilteredRasterFeedback) for item in scheduler.ready)
        if ready:
            scheduler_threads.add(get_ident())
            published.set()
            assert publication_release.wait(15.), 'Publication boundary was not released'

    scheduler.ready_callback = published_callback
    try:
        evaluation, _requests, _phases, _visible = _dispatch(canvas, filtered, snapshot)
        assert published.wait(30.), 'Real scheduler produced no finite feedback'
        canvas._scene_controller.advance()
        state = canvas._scene_controller._filtered_state
        assert state is not None and state.prepared.snapshot is snapshot
        worker = state.worker
        workers.append(worker)
        preparation = worker.preparation
        assert preparation is not None
        _wait(lambda: bool(worker.worker_threads))
        assert not fork_started.is_set()
        scheduler.cancel()
        publication_release.set()
        assert capture_started.wait(15.)
        with RENDER_ADMISSION.condition:
            assert any(ticket.owner == 'canvas' and ticket.estimated_bytes >= 16 * 1024 * 1024
                for ticket in RENDER_ADMISSION.running.values())
        assert not fork_started.is_set(), 'Filtered fork bypassed predecessor admission'
        capture_release.set()
        evaluation.result(30.)
        preparation.result(30.)
        assert fork_started.is_set()
        assert scheduler_threads and fork_threads and scheduler_threads.isdisjoint(fork_threads)
        assert worker.owner is not None and worker.owner.tiles is not snapshot.tiles
        assert _reference(snapshot) == original
        assert snapshot.chapter.to_dict() == model and path.read_bytes() == authored
    finally:
        _finish(canvas, workers, (publication_release, capture_release))


def test_retired_finite_filter_fork_keeps_new_cold_contact_source_handoff_safe(qapp, tmp_path, monkeypatch):
    canvas, filtered, plain, snapshot, original, model, path, authored = _fixture(tmp_path, monkeypatch)
    scheduler = canvas._scene_controller.scheduler
    fork_started, fork_release, fork_copied, capture_started, armed = (Event() for _ in range(5))
    fork_threads, capture_threads, moves, workers = set(), set(), [], []
    original_detach = TileStore.detached_snapshot
    original_get = type(snapshot.tiles.residency).get
    original_version = DiskTileMap.version

    def detached(store, identifiers):
        if store is snapshot.tiles and current_thread().name.startswith('native-filtered-contact'):
            fork_threads.add(get_ident())
            fork_started.set()
            assert fork_release.wait(15.), 'Retired fork was not released'
            try:
                return original_detach(store, identifiers)
            finally:
                fork_copied.set()
        return original_detach(store, identifiers)

    def resident_get(residency, owner, key, value):
        observed = residency is snapshot.tiles.residency and get_ident() in fork_threads
        before = tuple(residency.entries) if observed else None
        result = original_get(residency, owner, key, value)
        if observed and tuple(residency.entries) != before:
            moves.append((owner.object_id, key))
        return result

    monkeypatch.setattr(TileStore, 'detached_snapshot', detached)
    monkeypatch.setattr(type(snapshot.tiles.residency), 'get', resident_get)

    def version(owner, key):
        if (owner.residency is snapshot.tiles.residency and armed.is_set()
                and current_thread().name.startswith('scene-evaluate') and not capture_started.is_set()):
            # Pause inside the real capture's iterator body. Keep the native
            # OrderedDict.items() implementation intact: copying that bounded
            # metadata tuple before this body must remain a valid repair.
            capture_threads.add(get_ident())
            capture_started.set()
            assert fork_copied.wait(15.), 'Actual source copy did not finish'
        return original_version(owner, key)

    monkeypatch.setattr(DiskTileMap, 'version', version)
    try:
        plain_owner, plain_key = snapshot.tiles._tiles[plain.object_id], (0, 0)
        expected_identity = (snapshot.document.identity, snapshot.document.pixel_contract.signature,
                             getattr(snapshot.pixel_environment, 'signature', None))
        expected_pin = plain_owner.snapshot_pins[plain_key]
        expected_version = plain_owner.version(plain_key)
        # Observe the decoded snapshot borrower without moving its LRU or
        # warming the live canvas's deliberately cold original.
        expected_pixels = native(snapshot.tiles.residency.entries[(plain_owner, plain_key)][0])
        evaluation, requests, phases, visible = _dispatch(canvas, filtered, snapshot)
        evaluation.result(30.)
        canvas._scene_controller.advance()
        state = canvas._scene_controller._filtered_state
        assert state is not None and state.prepared.snapshot is snapshot
        worker = state.worker
        workers.append(worker)
        preparation = worker.preparation
        assert preparation is not None
        assert fork_started.wait(15.)
        assert scheduler._backend is not None and scheduler._backend.snapshot is snapshot
        canvas.set_selection('object', plain.object_id)
        # Selection uses its ordinary history command. The still-unadmitted
        # native gesture must not create a pixel command beyond that baseline.
        history_before_contact = canvas.command_stack.revision
        canvas._begin_stroke(QPointF(300., 64.), .7)
        gate = canvas._raster_tile_input
        assert gate is not None and gate.busy and gate.current() and not gate.released
        armed.set()
        document = canvas._render_document_state()
        canvas._scene_controller.request(document, requests, phases, visible)
        assert worker.stopped.is_set(), 'Selection did not retire the old finite filter'
        assert canvas._scene_controller._filtered_state is None
        handoff = canvas._scene_controller._bootstrap_originals
        assert handoff is not None, 'Actual cold contact did not request predecessor originals'
        assert capture_started.wait(15.)
        assert fork_threads and capture_threads and fork_threads.isdisjoint(capture_threads)
        fork_release.set()
        # The source-only handoff must remain usable. A native unmodified
        # source cannot become an evaluator failure because its old fork moves
        # the shared residency LRU during this capture.
        ready, handoff_error = None, None
        try:
            ready = handoff.result(30.)
        except Exception as error:
            handoff_error = error
        preparation.result(30.)
        assert moves, 'Fixture did not exercise unsaved resident source borrowing'
        assert _reference(snapshot) == original
        assert snapshot.chapter.to_dict() == model and path.read_bytes() == authored
        assert gate.busy and not gate.released and canvas.command_stack.revision == history_before_contact
        assert handoff_error is None, repr(handoff_error)
        assert ready is not None and ready.identity == expected_identity
        records = [record for record in ready.tiles
                   if record.identifier == plain.object_id and record.key == plain_key]
        assert len(records) == 1, 'The cold Raster has no uniquely usable source handoff'
        record = records[0]
        assert record.pin[0] == 'file' and record.pin[1] is expected_pin
        assert record.version == expected_version
        assert native(record.image) == expected_pixels
    finally:
        _finish(canvas, workers, (fork_release,))
