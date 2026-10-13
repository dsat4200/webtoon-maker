"""One initial native source quantum avoids a redundant Qt timer turn."""
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import ChapterDocument
from comic_editor.core.tile_backing import TileResidency
from comic_editor.core.tiles import TileStore
from comic_editor.render.input_sources import InputSourceCapture
from comic_editor.ui import tile_input
from comic_editor.ui.canvas import CanvasWidget
from test_native_input_admission import source, methods, wait_for


def native_bytes(image):
    return (image.size(), image.format(), image.bytesPerLine(), image.colorSpace(),
            image.devicePixelRatio(), bytes(image.constBits()))


def close_jobs(gate):
    gate.cancel()
    gate.jobs.shutdown()
    gate.jobs.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('target', ['raster', 'mask'])
@pytest.mark.parametrize('erasing', [False, True])
def test_first_quantum_submits_without_timer_then_held_pixels_release_and_undo_are_exact(
        source, target, erasing, monkeypatch):
    canvas, _obj, mask, original = source
    canvas.setUpdatesEnabled(False)
    begin, move, end, attribute, identifier = methods(canvas, mask, target, erasing)
    warm_tiles = TileStore()
    for key in ((0, 0), (1, 0)):
        warm_tiles.set_tile(identifier, key, QImage(original))
    warm = CanvasWidget(canvas.settings)
    warm.setUpdatesEnabled(False)
    warm.set_document(ChapterDocument.from_dict(canvas.chapter.to_dict()), warm_tiles)
    warm.set_selection('object', canvas.selected_id)
    warm.primary_color = canvas.primary_color
    warm_begin, warm_move, warm_end, _, _ = methods(
        warm, warm.chapter.masks[mask.mask_id], target, erasing)
    warm_begin(QPointF(30, 70), .2)
    expected_held = native_bytes(warm_tiles.tile(identifier, (0, 0)))
    warm_move(QPointF(90, 86), .8)
    warm_end()
    expected_final = native_bytes(warm_tiles.tile(identifier, (0, 0)))
    entered, release = Event(), Event()
    prepare = tile_input.prepare_input_tiles
    prepared = []
    gui = get_ident()
    get = TileResidency.get
    cold_threads = []

    def blocked_prepare(*args):
        assert get_ident() != gui
        entered.set()
        assert release.wait(5)
        result = prepare(*args)
        prepared.extend(native_bytes(image) for _key, image, _bounds in result)
        return result

    for name in ('source_capture', 'admission_priority', 'working_bytes', 'snapshot_working_bytes'):
        setattr(blocked_prepare, name, getattr(prepare, name))

    def checked_get(residency, owner, key, path):
        if (owner, key) not in residency.entries:
            assert get_ident() != gui, 'native pointer/admission decoded a cold GUI tile'
            cold_threads.append(get_ident())
        return get(residency, owner, key, path)

    advance = InputSourceCapture.advance
    quanta = []
    def observed_advance(capture, seconds):
        quanta.append(seconds)
        return advance(capture, seconds)

    monkeypatch.setattr(tile_input, 'prepare_input_tiles', blocked_prepare)
    monkeypatch.setattr(TileResidency, 'get', checked_get)
    monkeypatch.setattr(InputSourceCapture, 'advance', observed_advance)
    gate = None
    baseline = canvas.command_stack.revision
    try:
        begin(QPointF(30, 70), .2)
        gate = getattr(canvas, attribute)
        assert gate.pending and gate.jobs.active is None
        # No Qt events run here. The old path required a second timer turn.
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert gate.jobs.active.future is not None and quanta == [.004]
        assert entered.wait(5) and not gate.jobs.active.future.done()
        assert canvas.command_stack.revision == baseline
        release.set()
        gate.jobs.active.future.result(timeout=5)
        gate.jobs.advance()
        wait_for(lambda: not gate.busy)
        assert cold_threads and prepared == [native_bytes(original)]
        assert canvas._drawing and canvas.command_stack.revision == baseline
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == expected_held
        assert expected_held != native_bytes(original)
        move(QPointF(90, 86), .8)
        end()
        wait_for(lambda: getattr(canvas, attribute) is None)
        assert canvas.command_stack.revision == baseline + 1
        # Explicit post-contact/history inspection may load a frozen undo tile.
        monkeypatch.setattr(TileResidency, 'get', get)
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == expected_final
        canvas.command_stack.undo()
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == native_bytes(original)
        canvas.command_stack.redo()
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == expected_final
    finally:
        release.set()
        if gate is not None:
            close_jobs(gate)
        warm._cancel_mask_stroke()
        warm._cancel_native_raster_stroke()
        warm._scene_controller.reset()
        warm._scene_controller.scheduler.close()
        warm._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=True)
        warm._effect_jobs.cancel()
        warm._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        warm.deleteLater()


@pytest.mark.parametrize('exclusion', ['foreign_gate', 'wrong_lane', 'unordered', 'argument_capture'])
def test_noncurrent_or_nonpacket_source_job_keeps_staged_capture(source, monkeypatch, exclusion):
    canvas, _obj, mask, _original = source
    begin, _move, _end, attribute, _identifier = methods(canvas, mask, 'raster')
    begin(QPointF(30, 70), .5)
    gate = getattr(canvas, attribute)
    job = next(iter(gate.jobs.pending.values()))
    if exclusion == 'foreign_gate':
        monkeypatch.setattr(canvas, '_native_input_gates', lambda: ())
    elif exclusion == 'wrong_lane':
        job.lane = ('clipboard', id(gate))
    elif exclusion == 'unordered':
        job.ordered = False
    else:
        job.argument_capture = iter([None])
    calls = []
    monkeypatch.setattr(InputSourceCapture, 'advance', lambda *_args: calls.append(True))
    try:
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert gate.jobs.active is job and job.capture is not None
        assert job.future is None and not calls
    finally:
        close_jobs(gate)


def test_larger_initial_capture_takes_one_quantum_then_yields(source, monkeypatch):
    canvas, _obj, mask, _original = source
    begin, _move, _end, attribute, _identifier = methods(canvas, mask, 'raster')
    begin(QPointF(256, 70), .5)
    gate = getattr(canvas, attribute)
    baseline = canvas.command_stack.revision
    advance, calls = InputSourceCapture.advance, []
    def one_key(capture, seconds):
        calls.append(seconds)
        return advance(capture, 0.)
    monkeypatch.setattr(InputSourceCapture, 'advance', one_key)
    try:
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert calls == [.004]
        assert gate.jobs.active.capture is not None and gate.jobs.active.future is None
        assert canvas.command_stack.revision == baseline
    finally:
        close_jobs(gate)


@pytest.mark.parametrize('retirement', ['cancel', 'source_version', 'foreign_gate'])
def test_reentrant_retirement_during_first_quantum_never_submits_or_paints(source, monkeypatch, retirement):
    canvas, _obj, mask, original = source
    begin, _move, _end, attribute, identifier = methods(canvas, mask, 'raster')
    begin(QPointF(30, 70), .5)
    gate = getattr(canvas, attribute)
    job = next(iter(gate.jobs.pending.values()))
    baseline = canvas.command_stack.revision
    advance = InputSourceCapture.advance
    replacement = QImage(original)
    replacement.fill(QColor('yellow'))
    def retire(capture, seconds):
        finished = advance(capture, seconds)
        if retirement == 'cancel':
            gate.cancel()
        elif retirement == 'source_version':
            canvas.tiles._tiles[identifier][(0, 0)] = QImage(replacement)
        else:
            monkeypatch.setattr(canvas, '_native_input_gates', lambda: ())
        return finished
    monkeypatch.setattr(InputSourceCapture, 'advance', retire)
    try:
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert job.future is None and gate.jobs.active is None and job.cancelled.is_set()
        assert gate.closed and not gate.queue and not gate.buffers
        assert canvas.command_stack.revision == baseline
        expected = replacement if retirement == 'source_version' else original
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == native_bytes(expected)
    finally:
        close_jobs(gate)


def test_stale_first_quantum_retries_capture_without_advancing_twice(source, monkeypatch):
    canvas, _obj, mask, _original = source
    begin, _move, _end, attribute, _identifier = methods(canvas, mask, 'raster')
    begin(QPointF(30, 70), .5)
    gate = getattr(canvas, attribute)
    job = next(iter(gate.jobs.pending.values()))
    advance, captures = InputSourceCapture.advance, []

    def stale(capture, seconds):
        captures.append(capture)
        finished = advance(capture, seconds)
        capture.stale = True
        return finished

    monkeypatch.setattr(InputSourceCapture, 'advance', stale)
    try:
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert len(captures) == 1
        assert gate.jobs.active is job and job.future is None
        assert type(job.capture) is InputSourceCapture and job.capture is not captures[0]
        assert not job.capture.stale and not gate.closed
        assert job.document == canvas._render_document_state()
    finally:
        close_jobs(gate)


def test_current_first_quantum_capture_failure_uses_existing_input_error_path(source, monkeypatch):
    canvas, _obj, mask, original = source
    begin, _move, _end, attribute, identifier = methods(canvas, mask, 'raster')
    begin(QPointF(30, 70), .5)
    gate = getattr(canvas, attribute)
    job = next(iter(gate.jobs.pending.values()))
    baseline = canvas.command_stack.revision
    failure, errors = RuntimeError('capture failed'), []
    canvas.operationError.connect(lambda *args: errors.append(args))

    def fail(*_args):
        raise failure

    monkeypatch.setattr(InputSourceCapture, 'advance', fail)
    try:
        gate.jobs.advance()
        gate.jobs.timer.stop()
        assert gate.jobs.active is None and job.future is None
        assert gate.error is failure and canvas._native_input_error is failure
        assert gate.closed and not gate.queue and not gate.buffers
        assert errors == [('Drawing failed', str(failure))]
        assert getattr(canvas, attribute) is None and not canvas._drawing
        assert canvas.command_stack.revision == baseline
        assert native_bytes(canvas.tiles.tile(identifier, (0, 0))) == native_bytes(original)
    finally:
        close_jobs(gate)
