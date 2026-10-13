"""Latency includes a received press waiting behind a previous transaction."""
import time
import pytest

from PySide6.QtCore import QEvent, QPointF
from comic_editor.ui.main_window import MainWindow
from test_performance_monitor_feedback_coverage import fixture, close_gate
from test_performance_monitor_integration import editor, controller


def latency(controller):
    return [row for row in controller.snapshot()['critical_events']
        if row['name'] == 'canvas.contact_frame.receipt_to_first_covered']


def test_receipt_latency_includes_wait_before_first_covered_paint(editor, controller, monkeypatch):
    canvas, gate, _state, frame = fixture(editor, monkeypatch)
    monkeypatch.setattr(canvas, '_begin_stroke', lambda *_a: canvas._begin_stroke_impl(None, 1.))
    controller.start()
    try:
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        canvas._begin_stroke(None, 1.)
        time.sleep(.03)
        frame.update(covered=True, backend='gpu-pending')
        canvas.grab()
        assert not latency(controller)
        frame['backend'] = 'gpu'
        canvas.grab()
        rows = latency(controller)
        assert len(rows) == 1 and rows[0]['duration_ms'] >= 30.
        assert rows[0]['details']['input_kind'] == 'mouse'
        assert rows[0]['details']['receipt_serial'] > 0
        paints = [row for row in controller.snapshot()['critical_events']
            if row['name'] == 'canvas.contact_frame.first_covered']
        assert rows[0]['duration_ms'] > paints[0]['duration_ms'] + 20.
    finally:
        controller.stop()
        close_gate(canvas, gate)


def test_released_deferred_contact_keeps_original_receipt(editor, controller, monkeypatch):
    canvas = editor[0].canvas
    original_begin = canvas._begin_stroke
    canvas, old_gate, _state, frame = fixture(editor, monkeypatch)
    monkeypatch.setattr(canvas, '_begin_stroke', original_begin)
    canvas._raster_native_context = canvas._raster_input_context()
    old_gate.released = True
    controller.start()
    new_gate = None
    try:
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        canvas._begin_stroke(QPointF(10, 10), .5)
        assert canvas._raster_deferred_strokes
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonRelease))
        assert not latency(controller)
        time.sleep(.03)
        old_gate.closed = True
        canvas._raster_tile_input = None
        canvas._start_deferred_raster_stroke()
        new_gate = canvas._raster_tile_input
        assert new_gate is not None and new_gate is not old_gate
        frame['covered'] = True
        canvas.grab()
        rows = latency(controller)
        assert len(rows) == 1 and rows[0]['duration_ms'] >= 30.
        assert rows[0]['details']['gate_id'] == id(new_gate)
    finally:
        controller.stop()
        if new_gate is not None:
            close_gate(canvas, new_gate)
        close_gate(canvas, old_gate)


def test_idle_canvas_tablet_bypasses_global_popup_lookup(editor, monkeypatch):
    from PySide6.QtWidgets import QApplication
    window = editor[0]
    monkeypatch.setattr(QApplication, 'activePopupWidget', lambda: None)
    monkeypatch.setattr(QApplication, 'activeModalWidget', lambda: None)
    monkeypatch.setattr(QApplication, 'widgetAt', lambda *_a: (_ for _ in ()).throw(
        AssertionError('Ordinary pen hover performed a global widget lookup')))
    assert not MainWindow._forward_popup_tablet_event(window, window.canvas, QEvent(QEvent.TabletMove))


def test_actual_qt_tablet_dispatch_records_entire_handler_and_cursor(editor, controller):
    from PySide6.QtCore import QCoreApplication, Qt
    from PySide6.QtGui import QTabletEvent, QPointingDevice, QInputDevice
    canvas = editor[0].canvas
    pen = QPointingDevice('Latency test pen', 123, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position, 1, 3)
    event = QTabletEvent(QEvent.TabletMove, pen, QPointF(40, 40), QPointF(40, 40),
        0., 0., 0., 0., 0., 0., Qt.NoModifier, Qt.NoButton, Qt.NoButton)
    controller.start()
    try:
        QCoreApplication.sendEvent(canvas, event)
        names = {row['name'] for row in controller.snapshot()['summary']}
        assert {'canvas.tabletEvent', 'canvas.update_interaction_cursor', 'window.popup_tablet'} <= names
    finally:
        controller.stop()


@pytest.mark.parametrize('kind', ['popup', 'modal'])
def test_popup_and_modal_contacts_keep_forwarded_release_target(editor, monkeypatch, kind):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QTabletEvent, QPointingDevice, QInputDevice
    from PySide6.QtWidgets import QApplication, QDialog, QMenu
    window = editor[0]
    target = QMenu(window) if kind == 'popup' else QDialog(window)
    if kind == 'modal':
        target.setModal(True)
    active = {'popup': target if kind == 'popup' else None,
              'modal': target if kind == 'modal' else None, 'hit': target}
    hits = []
    monkeypatch.setattr(QApplication, 'activePopupWidget', lambda: active['popup'])
    monkeypatch.setattr(QApplication, 'activeModalWidget', lambda: active['modal'])
    monkeypatch.setattr(QApplication, 'widgetAt', lambda *_a: (hits.append(1), active['hit'])[1])
    pen = QPointingDevice('Popup test pen', 124, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position, 1, 3)
    def packet(type, pressure, buttons):
        return QTabletEvent(type, pen, QPointF(10, 10), QPointF(10, 10),
            pressure, 0., 0., 0., 0., 0., Qt.NoModifier, Qt.LeftButton, buttons)
    assert window._forward_popup_tablet_event(window.canvas,
        packet(QEvent.TabletPress, 1., Qt.LeftButton))
    assert window._tablet_popup_pressed is target
    active.update(popup=None, modal=None, hit=window.canvas)
    assert window._forward_popup_tablet_event(window.canvas,
        packet(QEvent.TabletRelease, 0., Qt.NoButton))
    assert window._tablet_popup_pressed is None and len(hits) == 2


def test_view_change_reports_cancelled_receipt_without_a_false_latency(editor, controller, monkeypatch):
    canvas, gate, _state, frame = fixture(editor, monkeypatch)
    monkeypatch.setattr(canvas, '_begin_stroke', lambda *_a: canvas._begin_stroke_impl(None, 1.))
    controller.start()
    try:
        controller.eventFilter(canvas, QEvent(QEvent.MouseButtonPress))
        canvas._begin_stroke(None, 1.)
        canvas.cameraChanged.emit()
        frame['covered'] = True
        canvas.grab()
        assert not latency(controller)
        cancelled = [e for e in controller.snapshot()['critical_events']
            if e['name'] == 'canvas.contact_frame.cancelled']
        assert len(cancelled) == 1 and cancelled[0]['details']['reason'] == 'cameraChanged'
    finally:
        controller.stop()
        close_gate(canvas, gate)
