"""A later native Qt paint can retain its first current coverage metadata."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtGui import QColor

from comic_editor.core.tools import ToolKind
from comic_editor.ui.tile_input import TileInputGate
from test_performance_monitor_integration import editor, controller


def fixture(editor, monkeypatch):
    canvas, obj = editor[0].canvas, editor[2]
    canvas.set_selection('object', obj.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    gate = TileInputGate(canvas, obj.object_id, valid=lambda: True)
    monkeypatch.setattr(canvas, '_raster_tile_input', gate, raising=False)
    monkeypatch.setattr(canvas, '_begin_stroke', lambda *_args: None)
    monkeypatch.setattr(canvas, '_begin_stroke_impl', lambda *_args: None)
    scene = canvas._scene_controller
    state = SimpleNamespace(revision=22, completed_revision=22, generations={})
    monkeypatch.setattr(scene, '_filtered_state', state)
    frame = {'covered': False, 'count': 3, 'observe_batch': True, 'backend': 'gpu'}
    monkeypatch.setattr(scene, 'filtered_batch', lambda _document, _requests, batch, **_kwargs:
        (batch, frame['count']))
    monkeypatch.setattr(canvas, '_projection_frame_pending', True)
    monkeypatch.setattr(canvas, '_projection_presented_revision', 21, raising=False)
    monkeypatch.setattr(canvas, '_gpu_presentation_pending', False, raising=False)
    def draw(painter, _started):
        painter.fillRect(canvas.rect(), QColor('red'))
        if frame['observe_batch']:
            scene.filtered_batch(SimpleNamespace(revision=22), (), ())
        canvas._document_presentation_stats = SimpleNamespace(backend=frame['backend'],
            tiles=1, uploads=0, texture_bytes=4)
        canvas._raster_feedback_contact_covered = frame['covered']
    monkeypatch.setattr(canvas, '_paint_canvas_frame', draw)
    return canvas, gate, state, frame


def close_gate(canvas, gate):
    canvas._scene_controller._filtered_state = None
    gate.cancel()
    canvas._raster_tile_input = None
    gate.jobs.shutdown()
    gate.jobs.executor.shutdown(wait=True, cancel_futures=True)


def covered(controller):
    return [row for row in controller.snapshot()['critical_events']
        if row['name'].startswith('canvas.contact_frame.first_covered')]


def test_late_real_qt_paint_requires_same_paint_current_filtered_result_and_survives_flood(
        editor, controller, monkeypatch):
    canvas, gate, state, frame = fixture(editor, monkeypatch)
    controller.start()
    try:
        # An old true flag and physical receipt are never an actual native begin.
        frame.update(covered=True, observe_batch=False)
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        assert not canvas.grab().isNull() and not covered(controller)
        canvas._begin_stroke_impl(None, 1.)
        canvas.grab()
        assert not covered(controller)
        frame.update(covered=False, observe_batch=True)
        canvas.grab()  # The original one-shot boundary has now been consumed.
        frame.update(covered=True, backend='gpu-pending')
        canvas.grab()
        frame.update(backend='gpu', count=0)
        canvas.grab()
        frame['count'], state.completed_revision = 3, 21
        canvas.grab()
        state.completed_revision, state.generations = 22, {('source', 0, 0): 1}
        canvas.grab()
        assert not covered(controller)
        state.generations = {}
        canvas.grab()
        rows = covered(controller)
        assert {row['name'] for row in rows} == {'canvas.contact_frame.first_covered',
            'canvas.contact_frame.first_covered.thread_cpu'}
        assert all(row['duration_ms'] >= 0 for row in rows)
        metadata = rows[0]['details']
        assert metadata['gate_id'] == id(gate) and metadata['source_id'] == editor[2].object_id
        assert metadata['requested_revision'] == metadata['filtered_completed_revision'] == 22
        assert metadata['presented_revision'] == 21 and metadata['frame_pending']
        assert metadata['filtered_count'] == 3 and metadata['feedback_contact_covered']
        assert not metadata['filtered_pending_generations']
        assert 'contact_frame_boundary' not in metadata
        canvas.grab()
        for _ in range(4000):
            controller.recorder.record_event('unrelated leaf', .01)
        assert len(covered(controller)) == 2
        assert len(controller.snapshot()['critical_events']) <= 512
    finally:
        controller.stop()
        close_gate(canvas, gate)


@pytest.mark.parametrize('boundary', ['clear', 'restart', 'selection', 'document', 'cancel', 'foreign_gate'])
def test_coverage_wait_never_crosses_capture_or_native_owner_boundary(
        editor, controller, monkeypatch, boundary):
    canvas, gate, _state, frame = fixture(editor, monkeypatch)
    controller.start()
    try:
        canvas._begin_stroke_impl(None, 1.)
        canvas.grab()
        assert controller._feedback_contact_wait is not None
        if boundary == 'clear':
            controller.recorder.clear()
        elif boundary == 'restart':
            controller.stop()
            controller.start()
        elif boundary == 'selection':
            canvas.selected_id = 'foreign source'
        elif boundary == 'document':
            gate.chapter = object()
        elif boundary == 'cancel':
            gate.closed = True
        else:
            canvas._raster_tile_input = None
        frame['covered'] = True
        canvas.grab()
        assert not covered(controller) and controller._feedback_contact_wait is None
    finally:
        controller.stop()
        close_gate(canvas, gate)


def test_actual_deferred_begin_cannot_rearm_the_previous_released_gate(
        editor, controller, monkeypatch):
    from PySide6.QtCore import QPointF
    canvas = editor[0].canvas
    original_begin = canvas._begin_stroke
    canvas, gate, _state, frame = fixture(editor, monkeypatch)
    monkeypatch.setattr(canvas, '_begin_stroke', original_begin)
    controller.start()
    try:
        canvas._begin_stroke_impl(QPointF(2, 2), 1.)
        assert controller._feedback_contact_wait['gate']() is gate
        gate.released = True
        # The production outer path queues a second contact while this prior
        # released gate is still the predecessor; its actual impl has not run.
        canvas._begin_stroke(QPointF(10, 10), .5)
        assert canvas._raster_deferred_strokes and canvas._raster_tile_input is gate
        assert controller._feedback_contact_wait is None
        frame['covered'] = True
        canvas.grab()
        assert not covered(controller)
    finally:
        canvas._raster_deferred_strokes.clear()
        canvas._native_deferred_activations.clear()
        canvas._raster_contact_active = False
        controller.stop()
        close_gate(canvas, gate)
