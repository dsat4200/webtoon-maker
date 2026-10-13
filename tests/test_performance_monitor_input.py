"""Native input preparation carries only its monitored owner's frozen token."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
import json
import threading
import time
from types import SimpleNamespace
from functools import wraps

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.tools import ToolKind
from comic_editor.render.input_sources import InputSourceCapture
from comic_editor.ui import scene_consumers
from test_performance_monitor_integration import editor, controller, _phases


def _owned_job(canvas, identifier, compute):
    jobs = scene_consumers.SceneConsumers(canvas)
    gate = SimpleNamespace(canvas=canvas, chapter=canvas.chapter, tiles=canvas.tiles,
        identifier=identifier, queue=deque(), pending=True, released=False, jobs=jobs)
    job = SimpleNamespace(lane=('native-input', id(gate)), document=SimpleNamespace(revision=11),
        arguments=(identifier, ((0, 0),)), owned_source=True, detached=False,
        cancelled=threading.Event(), compute=compute, captured_arguments=None, future=None,
        capture=None, discard=None)
    return gate, job


def test_feedback_receipt_reports_only_bounded_known_model_blockers(editor, controller):
    canvas, layer, obj = editor[0].canvas, editor[1], editor[2]
    canvas.set_selection('object', obj.object_id)
    obj.modifier_ids = ['private modifier identity']
    obj.show_on_top = True
    layer.modifier_ids = ['private ancestor modifier identity']
    details = controller._details('_begin_stroke_impl', (), {})
    assert details['feedback_known_blockers'] == [
        'object.modifiers', 'object.show_on_top', 'ancestor.modifiers']
    assert 'private' not in json.dumps(details)


def test_input_capture_requires_current_gate_and_exact_active_capture(editor, controller, monkeypatch):
    canvas = editor[0].canvas
    gate, job = _owned_job(canvas, editor[2].object_id, lambda *_args: None)
    capture = SimpleNamespace(owner=canvas, document=job.document)
    job.capture = capture
    gate.jobs.active = job
    monkeypatch.setattr(canvas, '_raster_tile_input', gate, raising=False)
    controller.start()
    try:
        details = controller._input_capture_scope((capture,), {})
        assert details['scope'] == 'gui_input_capture_slice'
        assert details['source_id'] == editor[2].object_id and details['gate_id'] == id(gate)
        assert details['requested_tiles'] == 1 and details['document_revision'] == 11
        foreign = SimpleNamespace(owner=canvas, document=job.document)
        assert controller._input_capture_scope((foreign,), {}) is None
        chapter, tiles = gate.chapter, gate.tiles
        gate.chapter = object()
        assert controller._input_capture_scope((capture,), {}) is None
        gate.chapter, gate.tiles = chapter, object()
        assert controller._input_capture_scope((capture,), {}) is None
        gate.tiles = tiles
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(controller._input_capture_scope, (capture,), {}).result() is None
    finally:
        gate.jobs.shutdown()
        gate.jobs.executor.shutdown(wait=True)
        canvas._raster_tile_input = None
        controller.stop()


def test_submit_registers_input_worker_before_start_and_foreign_jobs_are_excluded(
        editor, controller, monkeypatch):
    canvas = editor[0].canvas
    entered, release = threading.Event(), threading.Event()
    scopes = []
    gui = threading.get_ident()
    def compute(_snapshot, _identifier, _keys):
        token = controller._current_scope()
        scopes.append(token.details() if token else None)
        assert threading.get_ident() != gui
        entered.set()
        assert release.wait(10)
        return 'native result'
    gate, job = _owned_job(canvas, editor[2].object_id, compute)
    monkeypatch.setattr(canvas, '_raster_tile_input', gate, raising=False)
    controller.start()
    try:
        gate.jobs._submit(job, None)
        assert entered.wait(10)
        assert scopes[0]['scope'] == 'detached_input_worker'
        release.set()
        assert job.future.result(timeout=10) == 'native result'
        rows = [row for row in controller.snapshot()['critical_events']
            if row['name'].startswith('input.worker_envelope')]
        assert len(rows) == 2
        assert all(row['details']['gate_id'] == id(gate)
            and row['details']['source_id'] == editor[2].object_id
            and row['details']['requested_tiles'] == 1
            and row['details']['thread'] == 'worker'
            and row['details']['thread_id'] != gui for row in rows)
        count = _phases(controller)['input.worker_envelope']['count']
        # Same canvas is insufficient: its current gate must own these jobs.
        foreign, other = _owned_job(canvas, editor[2].object_id, compute)
        try:
            foreign.jobs._submit(other, None)
            assert other.future.result(timeout=10) == 'native result'
            assert scopes[-1] is None
            assert _phases(controller)['input.worker_envelope']['count'] == count
        finally:
            foreign.jobs.shutdown()
            foreign.jobs.executor.shutdown(wait=True)
    finally:
        release.set()
        gate.jobs.shutdown()
        gate.jobs.executor.shutdown(wait=True)
        canvas._raster_tile_input = None
        controller.stop()


@pytest.mark.parametrize('boundary', ['restart', 'clear'])
def test_old_input_worker_never_enters_a_new_capture_epoch(editor, controller, monkeypatch, boundary):
    canvas = editor[0].canvas
    entered, release = threading.Event(), threading.Event()
    def compute(_snapshot, _identifier, _keys):
        entered.set()
        assert release.wait(10)
        return 17
    gate, job = _owned_job(canvas, editor[2].object_id, compute)
    monkeypatch.setattr(canvas, '_raster_tile_input', gate, raising=False)
    controller.start()
    try:
        gate.jobs._submit(job, None)
        assert entered.wait(10)
        if boundary == 'restart':
            controller.stop()
            controller.start()
        else:
            controller.recorder.clear()
        release.set()
        assert job.future.result(timeout=10) == 17
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(scene_consumers.compute_snapshot, compute, None,
                job.arguments, job.cancelled, gate.jobs.stopped).result(timeout=10) == 17
        assert not any(row['name'].startswith('input.worker_envelope')
            for row in controller.snapshot()['timeline'])
    finally:
        release.set()
        gate.jobs.shutdown()
        gate.jobs.executor.shutdown(wait=True)
        canvas._raster_tile_input = None
        controller.stop()


def test_real_cold_source_is_prepared_and_admitted_while_held_without_pixel_changes_from_monitor(
        editor, controller, qapp, tmp_path, monkeypatch):
    canvas, identifier = editor[0].canvas, editor[2].object_id
    canvas.setUpdatesEnabled(False)
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    original = bytes(image.constBits())
    path = tmp_path / 'private original source.png'
    assert image.save(str(path))
    owner = canvas.tiles._object_tiles(identifier)
    owner.register((0, 0), path)
    canvas.set_selection('object', identifier)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.primary_color = '#0000ff'
    scopes = []
    original_advance = InputSourceCapture.advance
    @wraps(original_advance)
    def capture_advance(capture, *args, **kwargs):
        token = controller._current_scope()
        scopes.append(token.details() if token is not None else None)
        return original_advance(capture, *args, **kwargs)
    monkeypatch.setattr(InputSourceCapture, 'advance', capture_advance)
    controller.start()
    gate = None
    try:
        canvas._begin_stroke(QPointF(32, 32), 1.)
        gate = canvas._raster_tile_input
        assert gate is not None and gate.pending
        deadline = time.monotonic() + 10
        while not canvas._stroke_before and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.001)
        assert canvas._drawing and canvas._stroke_before and not gate.released
        assert scopes and all(scope['scope'] == 'gui_input_capture_slice'
            and scope['source_id'] == identifier and scope['gate_id'] == id(gate)
            for scope in scopes)
        changed = canvas.tiles.tile(identifier, (0, 0))
        assert bytes(changed.constBits()) != original
        assert changed.pixelColor(220, 220) == image.pixelColor(220, 220)
        canvas._end_stroke()
        while not gate.closed and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(.001)
        assert gate.closed
        phases = _phases(controller)
        assert {'input.gate_advance', 'input.capture_job', 'input.submit', 'input.worker_envelope',
            'input.prepare_input_tiles', 'input.accept', 'input.gate_publish',
            'canvas.begin_stroke_admitted', 'canvas.end_stroke_admitted',
            'canvas.begin_stroke_impl', 'canvas.end_stroke_impl'} <= set(phases)
        critical = controller.snapshot()['critical_events']
        worker = [row for row in critical if row['name'] == 'input.prepare_input_tiles']
        assert worker and all(row['details']['scope'] == 'detached_input_worker'
            and row['details']['source_id'] == identifier
            and row['details']['thread'] == 'worker' for row in worker)
        assert str(path) not in json.dumps(critical)
        assert not canvas._drawing and canvas.command_stack.can_undo
        actual = [row for row in critical if row['name'] == 'canvas.begin_stroke_impl']
        assert len(actual) == 1 and actual[0]['details']['feedback_known_blockers'] == []
        assert any(row['name'] == 'canvas.begin_stroke_impl.thread_cpu' for row in critical)
        # Instrumentation must preserve the source transaction and its journal.
        canvas.command_stack.undo()
        assert bytes(canvas.tiles.tile(identifier, (0, 0)).constBits()) == original
    finally:
        if gate is not None:
            canvas._cancel_native_raster_stroke()
            gate.jobs.shutdown()
            gate.jobs.executor.shutdown(wait=True)
        controller.stop()
