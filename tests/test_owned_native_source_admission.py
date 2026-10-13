"""Owned source quanta coexist without borrowing an ordinary native ticket."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.render.admission import (NATIVE_SOURCE_WORKSPACE, WorkAdmission,
    WorkCancelled, current_ticket)
from comic_editor.render.input_sources import InputSourceSnapshot
from comic_editor.render.scene import SceneTileStore
from comic_editor.ui import scene_consumers, tile_input
from test_native_input_admission import source, methods, wait_for


def hold(admission, owner, size, entered, release, **options):
    with admission.reserve(owner, size, **options) as ticket:
        entered.set()
        assert release.wait(5)
        return ticket


def waiters(admission, predicate):
    with admission.condition:
        deadline = time.monotonic()+5
        while not predicate(admission.waiters) and time.monotonic() < deadline:
            admission.condition.wait(.01)
        assert predicate(admission.waiters)


def native_bits(image):
    return (image.size(), image.format(), image.bytesPerLine(), image.colorSpace(),
        image.devicePixelRatio(), bytes(image.constBits()))


def test_owned_quantum_gets_distinct_charged_ticket_before_oversized_scene_returns():
    admission = WorkAdmission(100)
    entered, release = Event(), Event()
    executor = ThreadPoolExecutor(max_workers=1)
    try:
        scene = executor.submit(hold, admission, 'canvas', 200, entered, release)
        assert entered.wait(5)
        with admission.reserve('source-sampling', 20, native_source=True) as source_ticket:
            assert current_ticket() is source_ticket and source_ticket.native_source
            ordinary = next(ticket for ticket in admission.running.values() if not ticket.native_source)
            assert source_ticket is not ordinary and ordinary.oversized
            assert admission._occupied() == admission.peak_bytes == 220
            assert not scene.done()
        assert admission._occupied() == 200
    finally:
        release.set()
        executor.shutdown(wait=True)
    assert not admission.running and not admission.waiters and current_ticket() is None


@pytest.mark.parametrize('options', [dict(size=0), dict(size=NATIVE_SOURCE_WORKSPACE+1),
    dict(size=20, priority=1), dict(size=20, copies=object())])
def test_source_lane_rejects_unbounded_or_copy_credited_requests(options):
    admission = WorkAdmission(100)
    with pytest.raises(ValueError):
        with admission.reserve('source-sampling', native_source=True, **options):
            pytest.fail('Invalid native lane entered')
    assert not admission.running and not admission.waiters


def test_native_lane_never_borrows_foreign_or_other_admission_ticket():
    admission, other = WorkAdmission(100), WorkAdmission(100)
    with admission.reserve('canvas', 20):
        with pytest.raises(ValueError):
            with admission.reserve('source-sampling', 20, native_source=True):
                pytest.fail('Foreign ordinary ticket borrowed')
    with admission.reserve('source-sampling', 20, native_source=True) as ticket:
        with admission.reserve('nested-source', 20, native_source=True) as nested:
            assert nested is ticket
        with pytest.raises(ValueError):
            with admission.reserve('larger-source', 21, native_source=True):
                pytest.fail('Larger nested workspace was not accounted')
        with pytest.raises(ValueError):
            with admission.reserve('nested-gpu', 21):
                pytest.fail('Generic nested workspace escaped its bounded source ticket')
        with pytest.raises(ValueError):
            with other.reserve('source-sampling', 20, native_source=True):
                pytest.fail('Foreign admission ticket borrowed')


def test_one_source_lane_cancellation_does_not_block_eligible_ordinary_waiter():
    admission = WorkAdmission(100)
    cancelled = Event()
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        with admission.reserve('source-sampling', 20, native_source=True):
            def second():
                with admission.reserve('source-sampling', 20, native_source=True,
                        cancelled=cancelled.is_set):
                    pytest.fail('Second source workspace ran concurrently')
            pending = executor.submit(second)
            waiters(admission, lambda items: any(value[2] for value in items.values()))
            def ordinary():
                with admission.reserve('navigator', 100, priority=2):
                    assert admission._occupied() == 120
            executor.submit(ordinary).result(timeout=5)
            cancelled.set()
            with pytest.raises(WorkCancelled):
                pending.result(timeout=5)
            assert len(admission.running) == 1 and not admission.waiters
    finally:
        cancelled.set()
        executor.shutdown(wait=True)


def test_input_overtakes_ineligible_ordinary_waiter_without_ordinary_overcommit():
    admission = WorkAdmission(100)
    entered, release = Event(), Event()
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        first = executor.submit(hold, admission, 'canvas', 80, entered, release)
        assert entered.wait(5)
        def ordinary():
            with admission.reserve('visible', 40):
                return admission._occupied()
        pending = executor.submit(ordinary)
        waiters(admission, lambda items: len(items) == 1)
        with admission.reserve('source-sampling', 20, native_source=True):
            assert not pending.done() and admission._occupied() == 100
        assert not pending.done()
        release.set()
        assert pending.result(timeout=5) == 40
        first.result(timeout=5)
    finally:
        release.set()
        executor.shutdown(wait=True)


def test_ordinary_parent_copy_borrowing_counts_source_and_still_rejects_foreign_copies():
    admission = WorkAdmission(100)
    entered, release = Event(), Event()
    source_executor = ThreadPoolExecutor(max_workers=1)
    try:
        with admission.reserve('canvas', 200) as parent:
            source_job = source_executor.submit(hold, admission, 'source-sampling', 20,
                entered, release, native_source=True)
            assert entered.wait(5)
            copies = admission.reserve_copies(30)
            assert copies is not None and admission._occupied() == admission.peak_bytes == 250
            with admission.reserve('nested-gpu', 30, copies=copies) as nested:
                assert nested is parent
            copies.release()
            release.set()
            source_job.result(timeout=5)
    finally:
        release.set()
        source_executor.shutdown(wait=True)
    foreign = admission.reserve_copies(10)
    assert foreign is not None
    try:
        with admission.reserve('canvas', 200):
            assert admission.reserve_copies(1) is None
    finally:
        foreign.release()
    with admission.reserve('source-sampling', 20, native_source=True):
        assert admission.reserve_copies(101) is None
    assert not admission.running and not admission.copies and not admission.copy_credits


def snapshot(format, side=256, count=2):
    tiles = SceneTileStore(cache_budget=4*1024*1024)
    originals = {}
    for x in range(count):
        image = QImage(side, side, format)
        image.fill(QColor.fromRgbF(.1234+x*.1, .4567, .8765, .5341))
        originals[(x, 0)] = native_bits(image)
        tiles.set_tile('native-source', (x, 0), image)
    return InputSourceSnapshot(tiles, ()), originals


@pytest.mark.parametrize('format', [QImage.Format_RGBA64_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied, QImage.Format_Grayscale16])
def test_exact_native_chunks_finish_before_ordinary_ticket_without_partial_result(format, monkeypatch):
    admission = WorkAdmission(4*1024*1024)
    captured, originals = snapshot(format, side=128)
    cancelled, stopped, entered, release = Event(), Event(), Event(), Event()
    source_entered, source_release = Event(), Event()
    calls = []
    gui = get_ident()
    original = tile_input.prepare_input_tiles
    def prepare(captured, identifier, keys):
        ticket = current_ticket()
        assert get_ident() != gui and ticket.native_source
        assert 0 < ticket.estimated_bytes <= NATIVE_SOURCE_WORKSPACE
        calls.append((keys, ticket))
        if len(calls) == 2:
            source_entered.set()
            assert source_release.wait(5)
        return original(captured, identifier, keys)
    prepare.working_bytes = original.working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', admission)
    executor = ThreadPoolExecutor(max_workers=2)
    try:
        ordinary = executor.submit(hold, admission, 'canvas', 6*1024*1024, entered, release)
        assert entered.wait(5)
        native = executor.submit(scene_consumers.compute_snapshot, prepare, captured,
            ('native-source', ((0, 0), (1, 0))), cancelled, stopped,
            native_source_budget=4*1024*1024)
        assert source_entered.wait(5)
        assert not native.done() and not ordinary.done()
        assert admission._occupied() == admission.peak_bytes == 6*1024*1024+calls[0][1].estimated_bytes
        source_release.set()
        result = native.result(timeout=5)
        assert not ordinary.done()
        assert [keys for keys,_ticket in calls] == [((0, 0),), ((1, 0),)]
        assert calls[0][1] is calls[1][1]
        assert {key: native_bits(image) for key,image,_bounds in result} == originals
        assert len(admission.running) == 1
    finally:
        source_release.set()
        release.set()
        executor.shutdown(wait=True)


def test_chunk_cancellation_never_returns_a_partially_prepared_packet(monkeypatch):
    admission = WorkAdmission(4*1024*1024)
    captured, originals = snapshot(QImage.Format_RGBA64_Premultiplied, side=128)
    cancelled, stopped, calls = Event(), Event(), []
    original = tile_input.prepare_input_tiles
    def prepare(*args):
        calls.append(args[2])
        result = original(*args)
        cancelled.set()
        return result
    prepare.working_bytes = original.working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', admission)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(scene_consumers.compute_snapshot, prepare, captured,
            ('native-source', ((0, 0), (1, 0))), cancelled, stopped,
            native_source_budget=4*1024*1024)
        with pytest.raises(WorkCancelled):
            result.result(timeout=5)
    assert calls == [((0, 0),)] and not admission.running and not admission.waiters
    assert {key: native_bits(captured.tiles.tile('native-source', key)) for key in originals} == originals


def test_handoff_preflight_keeps_large_packet_on_original_full_job_admission(monkeypatch):
    admission = WorkAdmission(4*1024*1024)
    captured, originals = snapshot(QImage.Format_RGBA64_Premultiplied)
    calls = []
    original = tile_input.prepare_input_tiles
    def prepare(*args):
        assert not current_ticket().native_source
        calls.append((args[2], current_ticket().estimated_bytes))
        return original(*args)
    prepare.working_bytes = original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', admission)
    with ThreadPoolExecutor(max_workers=1) as executor:
        result = executor.submit(scene_consumers.compute_snapshot, prepare, captured,
            ('native-source', ((0, 0), (1, 0))), Event(), Event(),
            native_source_budget=1024*1024).result(timeout=5)
    assert calls == [(((0, 0), (1, 0)), 4*1024*1024)]
    assert {key: native_bits(image) for key,image,_bounds in result} == originals


def test_retained_payload_plus_scratch_declines_lane_even_with_large_handoff_budget(monkeypatch):
    admission = WorkAdmission(4*1024*1024)
    captured, originals = snapshot(QImage.Format_RGBA32FPx4_Premultiplied)
    original = tile_input.prepare_input_tiles
    calls = []
    def prepare(*args):
        assert not current_ticket().native_source
        calls.append(args[2])
        return original(*args)
    prepare.working_bytes = original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', admission)
    result = scene_consumers.compute_snapshot(prepare, captured,
        ('native-source', ((0, 0), (1, 0))), Event(), Event(),
        native_source_budget=4*1024*1024)
    assert calls == [((0, 0), (1, 0))]
    assert {key: native_bits(image) for key,image,_bounds in result} == originals


def test_grayscale_conversion_and_retained_packet_use_ordinary_admission_near_native_ceiling(monkeypatch):
    captured, originals = snapshot(QImage.Format_Grayscale16, count=3)
    original = tile_input.prepare_input_tiles
    keys = tuple(originals)
    calls = []
    def prepare(*args):
        assert not current_ticket().native_source
        calls.append(args[2])
        return original(*args)
    prepare.working_bytes = original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', WorkAdmission(4*1024*1024))
    result = scene_consumers.compute_snapshot(prepare, captured,
        ('native-source', keys), Event(), Event(), native_source_budget=4*1024*1024)
    assert calls == [keys]
    assert {key: native_bits(image) for key,image,_bounds in result} == originals


@pytest.mark.parametrize('backing', ['resident', 'png', 'frozen'])
def test_noncanonical_original_declines_lane_before_decode_and_preserves_native_bits(tmp_path, monkeypatch, backing):
    from comic_editor.core.tile_backing import EditableTile
    from comic_editor.core.tile_history import TileHistoryCache
    image = QImage(512, 512, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.1234, .5678, .789, .4321))
    tiles = SceneTileStore(cache_budget=4*1024*1024)
    owner = tiles._object_tiles('native-source')
    raw_cache = None
    if backing == 'resident':
        tiles.set_tile('native-source', (0, 0), image)
        expected = native_bits(image)
    elif backing == 'png':
        path = tmp_path/'noncanonical.png'
        assert image.save(str(path))
        owner.register((0, 0), path)
        expected = native_bits(QImage(str(path)))
    else:
        raw_cache = TileHistoryCache(0)
        owner.entries[(0, 0)] = EditableTile(raw_cache.capture(image))
        expected = native_bits(image)
    captured = InputSourceSnapshot(tiles, ())
    assert captured.bounded_workspace('native-source', ((0, 0),),
        4*1024*1024, lambda: False) is None
    assert tiles.residency.decodes == 0 and (raw_cache is None or raw_cache.reads == 0)
    original = tile_input.prepare_input_tiles
    calls = []
    def prepare(*args):
        assert not current_ticket().native_source
        assert tiles.residency.decodes == 0 and (raw_cache is None or raw_cache.reads == 0)
        calls.append(args[2])
        return original(*args)
    prepare.working_bytes = original.working_bytes
    prepare.snapshot_working_bytes = original.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', WorkAdmission(4*1024*1024))
    result = scene_consumers.compute_snapshot(prepare, captured,
        ('native-source', ((0, 0),)), Event(), Event(), native_source_budget=4*1024*1024)
    assert calls == [((0, 0),)] and native_bits(result[0][1]) == expected


def test_oversized_packet_stops_header_preflight_before_unneeded_sources(tmp_path, monkeypatch):
    from comic_editor.render import input_sources
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.3145, .7654, .5432, .2345))
    path = tmp_path/'immutable-original.png'
    assert image.save(str(path))
    original = path.read_bytes()
    tiles = SceneTileStore()
    owner = tiles._object_tiles('native-source')
    keys = tuple((x, 0) for x in range(32))
    for key in keys:
        owner.register(key, path)
    headers = []
    reader = input_sources.QImageReader
    def inspect(path):
        headers.append(path)
        return reader(path)
    monkeypatch.setattr(input_sources, 'QImageReader', inspect)
    captured = InputSourceSnapshot(tiles, ())
    assert captured.bounded_workspace('native-source', keys,
        64*1024*1024, lambda: False) is None
    assert 0 < len(headers) <= 2 and tiles.residency.decodes == 0
    assert path.read_bytes() == original and tuple(owner) == keys


@pytest.mark.parametrize('exclusion', ['foreign-gate', 'closed', 'not-owned', 'not-ordered',
    'detached', 'wrong-lane', 'wrong-compute', 'stale-owner', 'stale-version', 'missing-snapshot'])
def test_gui_authorization_rejects_foreign_stale_or_generic_source_jobs(source, exclusion):
    canvas,obj,_mask,_original = source
    gate = tile_input.TileInputGate(canvas, obj.object_id)
    canvas._raster_tile_input = gate
    gate.pending = True
    jobs = gate.jobs
    try:
        owner = canvas.tiles._tiles[obj.object_id]
        versions = {key: owner.version(key) for key in ((0, 0),)}
        def valid():
            return (gate.current() and canvas.tiles._tiles.get(obj.object_id) is owner
                and all(key in owner and owner.version(key) == version
                    for key,version in versions.items()))
        jobs.request(('native-input', id(gate)), tile_input.prepare_input_tiles,
            (obj.object_id, ((0, 0),)), lambda *_args: None, ordered=True,
            valid=valid, owned_source=True)
        _key,job = jobs.pending.popitem()
        jobs.active = job
        capture = jobs._capture_job(job)
        while not capture.advance(.004):
            pass
        captured = capture.result
        assert jobs._native_source_budget(job, captured) > 0
        if exclusion == 'foreign-gate':
            canvas._raster_tile_input = None
        elif exclusion == 'closed':
            gate.closed = True
        elif exclusion == 'not-owned':
            job.owned_source = False
        elif exclusion == 'not-ordered':
            job.ordered = False
        elif exclusion == 'detached':
            job.detached = True
        elif exclusion == 'wrong-lane':
            job.lane = ('generic', id(gate))
        elif exclusion == 'wrong-compute':
            job.compute = tile_input.prepare_input_bounds
        elif exclusion == 'stale-owner':
            gate.tiles = object()
        elif exclusion == 'stale-version':
            owner.versions[(0, 0)] += 1
        else:
            captured = None
        assert jobs._native_source_budget(job, captured) == 0
    finally:
        gate.closed = False
        gate.tiles = canvas.tiles
        gate.cancel()
        jobs.shutdown()
        jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas._raster_tile_input = None


def test_production_gate_drains_accepted_release_before_ordinary_ticket_and_undoes_exactly(source, monkeypatch):
    from comic_editor.core.models import ChapterDocument
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.canvas import CanvasWidget
    canvas,obj,mask,original = source
    canvas.setUpdatesEnabled(False)
    canvas.tiles.residency.budget = 4*1024*1024
    begin,move,end,attribute,identifier = methods(canvas, mask, 'raster')
    warm_tiles = TileStore(cache_budget=4*1024*1024)
    for key in ((0, 0), (1, 0)):
        warm_tiles.set_tile(identifier, key, QImage(original))
    warm = CanvasWidget(canvas.settings)
    warm.setUpdatesEnabled(False)
    warm.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), warm_tiles)
    warm.set_selection('object', identifier)
    warm.primary_color = canvas.primary_color
    warm_begin,warm_move,warm_end,_attribute,_identifier = methods(
        warm, warm.chapter.masks[mask.mask_id], 'raster')
    warm_begin(QPointF(30, 70), .6)
    warm_move(QPointF(50, 86), .8)
    warm_end()
    expected = {key: native_bits(warm_tiles.tile(identifier, key)) for key in ((0, 0), (1, 0))}
    entered, release, second, finish = Event(), Event(), Event(), Event()
    admission = WorkAdmission(4*1024*1024)
    monkeypatch.setattr(scene_consumers, 'RENDER_ADMISSION', admission)
    prepare = tile_input.prepare_input_tiles
    calls = []
    def observed(*args):
        assert current_ticket().native_source
        calls.append(args[2])
        if len(calls) == 1:
            second.set()
            assert finish.wait(5)
        return prepare(*args)
    for name in ('source_capture', 'admission_priority', 'working_bytes', 'snapshot_working_bytes'):
        setattr(observed, name, getattr(prepare, name))
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', observed)
    history = canvas.command_stack.revision
    executor = ThreadPoolExecutor(max_workers=1)
    gate = None
    try:
        ordinary = executor.submit(hold, admission, 'canvas', 6*1024*1024, entered, release)
        assert entered.wait(5)
        begin(QPointF(30, 70), .6)
        gate = getattr(canvas, attribute)
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert second.wait(5)
        assert gate.pending and not gate.buffers and not ordinary.done()
        assert canvas.command_stack.revision == history and not canvas.tiles.dirty
        move(QPointF(50, 86), .8)
        end()
        assert gate.released and gate.pending
        finish.set()
        wait_for(lambda: not gate.busy and getattr(canvas, attribute) is None)
        assert not ordinary.done() and len(calls) == 1 and all(len(keys) == 1 for keys in calls)
        assert canvas.command_stack.revision == history+1
        changed = {key: native_bits(canvas.tiles.tile(identifier, key)) for key in ((0, 0), (1, 0))}
        assert changed == expected
        assert any(bits != native_bits(original) for bits in changed.values())
        canvas.command_stack.undo()
        assert all(native_bits(canvas.tiles.tile(identifier, key)) == native_bits(original)
                   for key in ((0, 0), (1, 0)))
        canvas.command_stack.redo()
        assert {key: native_bits(canvas.tiles.tile(identifier, key)) for key in changed} == changed
    finally:
        finish.set()
        release.set()
        executor.shutdown(wait=True)
        if gate is not None:
            gate.cancel()
            gate.jobs.shutdown()
            gate.jobs.executor.shutdown(wait=True, cancel_futures=True)
        warm._cancel_native_raster_stroke()
        warm._scene_controller.reset()
        warm.deleteLater()
