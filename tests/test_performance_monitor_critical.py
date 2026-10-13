"""Contact diagnostics survive scene-render floods without widening timelines."""
from types import SimpleNamespace

from PySide6.QtCore import QEvent

from test_performance_monitor_integration import editor, controller


def test_contact_boundaries_and_first_pending_frame_survive_generic_flood(
        editor, controller, monkeypatch):
    canvas = editor[0].canvas
    monkeypatch.setattr(canvas, '_begin_stroke', lambda _position: 17)
    monkeypatch.setattr(canvas, '_end_stroke', lambda: 23)
    monkeypatch.setattr(canvas, 'paintEvent', lambda _event: 29)
    monkeypatch.setattr(canvas, '_document_presentation_stats', SimpleNamespace(
        backend='gpu-pending', tiles=1, uploads=0, texture_bytes=4))
    monkeypatch.setattr(canvas, '_projection_frame_pending', True)
    monkeypatch.setattr(canvas, '_raster_feedback_contact_covered', False)
    controller.start()
    try:
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        assert canvas._begin_stroke(None) == 17
        assert canvas.paintEvent(QEvent(QEvent.Paint)) == 29
        assert canvas.paintEvent(QEvent(QEvent.Paint)) == 29
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonRelease))
        assert canvas._end_stroke() == 23
        assert canvas.paintEvent(QEvent(QEvent.Paint)) == 29
        for _ in range(4000):
            controller.recorder.record_event('generic source leaf', .01)
        snapshot = controller.snapshot()
        assert not any(row['name'] == 'mouse.press' for row in snapshot['timeline'])
        critical = snapshot['critical_events']
        assert {'mouse.press', 'mouse.release', 'canvas.begin_stroke', 'canvas.end_stroke'} <= {
            row['name'] for row in critical}
        paints = [row for row in critical if row['name'] == 'canvas.paintEvent']
        assert len(paints) == 2  # Only the first frame after each boundary.
        assert paints[0]['details']['contact_frame_boundary'] == ['after_begin', 'after_press']
        assert paints[1]['details']['contact_frame_boundary'] == ['after_release']
        completed = [row for row in critical if row['name'] == 'canvas.contact_frame.completed']
        assert len(completed) == 2
        assert all(row['details']['presentation_backend'] == 'gpu-pending'
            and row['details']['frame_pending']
            and not row['details']['feedback_contact_covered'] for row in completed)
        assert len(snapshot['timeline']) <= 3000
        assert len(critical) <= 512
    finally:
        controller.stop()


def test_pending_first_frame_marker_cannot_cross_recorder_epoch_or_foreign_owner(
        editor, controller, monkeypatch):
    canvas = editor[0].canvas
    monkeypatch.setattr(canvas, 'paintEvent', lambda _event: 29)
    controller.start()
    try:
        controller.eventFilter(editor[0], QEvent(QEvent.MouseButtonPress))
        canvas.paintEvent(QEvent(QEvent.Paint))
        assert not controller.snapshot()['critical_events']
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        controller.recorder.clear()
        canvas.paintEvent(QEvent(QEvent.Paint))
        assert not controller.snapshot()['critical_events']
        assert not any('contact_frame_boundary' in row.get('details', {})
            for row in controller.snapshot()['timeline'])
    finally:
        controller.stop()
