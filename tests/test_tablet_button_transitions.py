"""A pen's button changes cannot terminate or replace its held drawing owner.

These are ordinary Qt event sequences, including Windows' replacement of the
tip's LeftButton by RightButton while the barrel is held. They test application
admission, not whether a particular physical driver emits the sequence.
"""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QInputDevice, QPointingDevice, QTabletEvent

from comic_editor.ui.canvas import ToolKind
from test_native_pointer_lifetime import hold_native_source, native_source
from test_painting_input_cursor import painting_canvas, wait_for


def held_packets(transition):
    left, right = Qt.LeftButton, Qt.RightButton
    if transition == 'replacement':
        # Qt's Windows pointer backend represents an in-contact barrel as
        # RightButton instead of LeftButton. QGuiApplication infers event type
        # from the changing button mask and reports its lowest changed bit.
        middle = [(QEvent.TabletPress, left, right, .45),
                  (QEvent.TabletMove, Qt.NoButton, right, .6),
                  (QEvent.TabletRelease, left, left, .3),
                  (QEvent.TabletMove, Qt.NoButton, left, .8)]
    else:
        # A secondary-button release can also arrive while the tip's LeftButton
        # remains down; neither that release nor the following press is a lift.
        middle = [(QEvent.TabletRelease, right, left, .45),
                  (QEvent.TabletPress, right, left | right, .6),
                  (QEvent.TabletMove, Qt.NoButton, left | right, .3),
                  (QEvent.TabletRelease, right, left, .8)]
    return [(QEvent.TabletPress, left, left, .2),
            (QEvent.TabletMove, Qt.NoButton, left, .7), *middle,
            (QEvent.TabletMove, Qt.NoButton, left, .55)]


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('transition', ['remaining-tip', 'replacement'])
def test_held_button_changes_keep_samples_owner_cursor_and_one_native_history(
        painting_canvas, tool, cold, transition, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    before = native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    history = canvas.command_stack.revision
    entered, unblock = (hold_native_source(canvas, raster, tmp_path, monkeypatch)
                        if cold else (None, None))
    if cold:
        # Undo restores the registered native PNG owner, whose decoded bytes
        # are the real pre-stroke baseline, rather than an in-memory substitute.
        before = QImage(str(tmp_path / 'native-contact.png')).convertToFormat(
            QImage.Format_ARGB32_Premultiplied)

    admitted = []
    if tool == ToolKind.BRUSH:
        apply = canvas._apply_paint_brush_sample

        def record(sample, *, begin=False):
            admitted.append(dict(point=(sample.x, sample.y), pressure=sample.pressure,
                axes=(sample.tilt_x, sample.tilt_y, sample.rotation), time=sample.time,
                begin=begin, owner=canvas._paint_brush_object))
            return apply(sample, begin=begin)

        monkeypatch.setattr(canvas, '_apply_paint_brush_sample', record)
    else:
        begin, update = canvas._begin_stroke_impl, canvas._continue_stroke_impl

        def record_begin(point, pressure):
            admitted.append(dict(point=point.toTuple(), pressure=pressure, begin=True,
                owner=canvas.chapter.objects.get(raster.object_id)))
            return begin(point, pressure)

        def record_update(point, pressure):
            admitted.append(dict(point=point.toTuple(), pressure=pressure, begin=False,
                owner=canvas.chapter.objects.get(raster.object_id)))
            return update(point, pressure)

        monkeypatch.setattr(canvas, '_begin_stroke_impl', record_begin)
        monkeypatch.setattr(canvas, '_continue_stroke_impl', record_update)

    device = QPointingDevice('Button transition pen', 21931, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position
        | QInputDevice.Capability.Pressure | QInputDevice.Capability.XTilt
        | QInputDevice.Capability.YTilt | QInputDevice.Capability.Rotation, 1, 3)
    packets = held_packets(transition)
    expected = []

    def send(index, kind, button, buttons, pressure):
        point = QPointF(20 + index * 12, 40)
        axes = (12. + index, -7. + index, 33. + index)
        event = QTabletEvent(kind, device, point, QPointF(canvas.mapToGlobal(point.toPoint())),
            pressure, axes[0], axes[1], 0., axes[2], 0., Qt.NoModifier, button, buttons)
        event.setTimestamp(1000 + index * 8)
        QCoreApplication.sendEvent(canvas, event)
        assert event.isAccepted()
        expected.append((point.toTuple(), pressure, axes, index * .008))

    try:
        send(0, *packets[0])
        gate = canvas._native_input_predecessor()
        assert gate is not None
        if cold:
            wait_for(entered.is_set)
        else:
            wait_for(lambda: bool(admitted) and not gate.busy)
        for index, packet in enumerate(packets[1:], 1):
            send(index, *packet)
            state = dict(tool=tool.name, cold=cold, transition=transition, packet=index,
                pen_contact=canvas._pen_contact_active, drawing=canvas._drawing,
                gate_released=gate.released, gate_closed=gate.closed,
                history=canvas.command_stack.revision, admitted=len(admitted),
                deferred=len(canvas._native_deferred_activations))
            assert canvas._pen_contact_active and canvas._tablet_tool_active, state
            assert canvas._drawing and not gate.released and not gate.closed, state
            assert canvas._native_input_predecessor() is gate, state
            assert not canvas._native_deferred_activations, state
            assert canvas.chapter.objects[raster.object_id] is raster
            assert canvas.command_stack.revision == history, state
            assert canvas.cursor().shape() == Qt.BlankCursor, state
            if cold:
                assert not admitted, 'Cold button transitions must retain every queued packet'
            else:
                assert len(admitted) == index + 1, state

        # The real lift carries a new endpoint without an intervening move.
        final_index = len(packets)
        send(final_index, QEvent.TabletRelease, Qt.LeftButton, Qt.NoButton, 0.)
        expected[-1] = (expected[-1][0], packets[-1][3], expected[-1][2], expected[-1][3])
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        if unblock is not None:
            unblock.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
                 and not canvas._native_deferred_activations)

        assert [(row['point'], row['pressure']) for row in admitted] == [
            (point, pressure) for point, pressure, _axes, _time in expected]
        assert [row['begin'] for row in admitted] == [True] + [False] * (len(expected) - 1)
        assert all(row['owner'] is raster for row in admitted)
        if tool == ToolKind.BRUSH:
            for row, (_point, _pressure, axes, elapsed) in zip(admitted, expected):
                assert row['axes'] == pytest.approx(axes)
                assert row['time'] - admitted[0]['time'] == pytest.approx(elapsed)
        assert not canvas._drawing
        assert canvas.command_stack.revision == history + 1
        after = QImage(canvas.tiles.tile(raster.object_id, (0, 0)))
        assert after != before
        assert after.format() == before.format()
        assert after.pixelColor(104, 40) != before.pixelColor(104, 40)
        canvas.command_stack.undo()
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == before
        canvas.command_stack.redo()
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == after
        assert canvas.chapter.objects[raster.object_id] is raster
    finally:
        if unblock is not None:
            unblock.set()


def button_test_device():
    return QPointingDevice('Button boundary pen', 21932, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position
        | QInputDevice.Capability.Pressure | QInputDevice.Capability.XTilt
        | QInputDevice.Capability.YTilt | QInputDevice.Capability.Rotation, 1, 3)


def send_boundary_packet(canvas, device, kind, x, pressure, button, buttons, index):
    point = QPointF(x, 40)
    axes = (12. + index, -7. + index, 33. + index)
    event = QTabletEvent(kind, device, point, QPointF(canvas.mapToGlobal(point.toPoint())),
        pressure, axes[0], axes[1], 0., axes[2], 0., Qt.NoModifier, button, buttons)
    event.setTimestamp(2000 + index * 16)
    QCoreApplication.sendEvent(canvas, event)
    assert event.isAccepted()
    return axes


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('cold', [False, True])
@pytest.mark.parametrize('release', ['stale-positive-no-buttons', 'zero-pressure-barrel'])
def test_real_lift_keeps_last_positive_pressure_and_terminal_endpoint(
        painting_canvas, tool, cold, release, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    before = native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    history = canvas.command_stack.revision
    entered, unblock = (hold_native_source(canvas, raster, tmp_path, monkeypatch)
                        if cold else (None, None))
    if cold:
        before = QImage(str(tmp_path / 'native-contact.png')).convertToFormat(
            QImage.Format_ARGB32_Premultiplied)
    admitted = []
    if tool == ToolKind.BRUSH:
        original = canvas._apply_paint_brush_sample

        def record(sample, *, begin=False):
            admitted.append((sample.x, sample.y, sample.pressure, begin,
                (sample.tilt_x, sample.tilt_y, sample.rotation), sample.time))
            return original(sample, begin=begin)

        monkeypatch.setattr(canvas, '_apply_paint_brush_sample', record)
    else:
        original_begin, original_move = canvas._begin_stroke_impl, canvas._continue_stroke_impl

        def record_begin(point, pressure):
            admitted.append((point.x(), point.y(), pressure, True))
            return original_begin(point, pressure)

        def record_move(point, pressure):
            admitted.append((point.x(), point.y(), pressure, False))
            return original_move(point, pressure)

        monkeypatch.setattr(canvas, '_begin_stroke_impl', record_begin)
        monkeypatch.setattr(canvas, '_continue_stroke_impl', record_move)

    device = button_test_device()
    try:
        send_boundary_packet(canvas, device, QEvent.TabletPress, 20, .25,
            Qt.LeftButton, Qt.LeftButton, 0)
        gate = canvas._native_input_predecessor()
        assert gate is not None
        if cold:
            wait_for(entered.is_set)
        else:
            wait_for(lambda: bool(admitted) and not gate.busy)
        send_boundary_packet(canvas, device, QEvent.TabletMove, 52, .7,
            Qt.NoButton, Qt.LeftButton, 1)
        assert canvas._pen_contact_active and canvas._tablet_tool_active
        assert canvas.cursor().shape() == Qt.BlankCursor
        pressure, buttons = ((.91, Qt.NoButton) if release == 'stale-positive-no-buttons'
                             else (0., Qt.RightButton))
        axes = send_boundary_packet(canvas, device, QEvent.TabletRelease, 88,
            pressure, Qt.LeftButton, buttons, 2)
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        assert gate.released or gate.closed
        assert not canvas._native_deferred_activations
        if unblock is not None:
            unblock.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        assert [row[:4] for row in admitted] == [
            (20., 40., .25, True), (52., 40., .7, False), (88., 40., .7, False)]
        if tool == ToolKind.BRUSH:
            assert admitted[-1][4] == pytest.approx(axes)
            assert admitted[-1][5] - admitted[0][5] == pytest.approx(.032)
        assert not canvas._drawing
        assert canvas.command_stack.revision == history + 1
        after = QImage(canvas.tiles.tile(raster.object_id, (0, 0)))
        assert after.pixelColor(88, 40) != before.pixelColor(88, 40)
        assert after.format() == before.format()
        canvas.command_stack.undo()
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == before
        canvas.command_stack.redo()
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == after
        assert canvas.chapter.objects[raster.object_id] is raster
    finally:
        if unblock is not None:
            unblock.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_zero_pressure_hover_barrel_does_not_begin_native_painting(painting_canvas, tool):
    canvas, raster = painting_canvas
    before = native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    history = canvas.command_stack.revision
    device = button_test_device()
    for index, (kind, button, buttons) in enumerate([
            (QEvent.TabletPress, Qt.RightButton, Qt.RightButton),
            (QEvent.TabletMove, Qt.NoButton, Qt.RightButton),
            (QEvent.TabletRelease, Qt.RightButton, Qt.NoButton)]):
        send_boundary_packet(canvas, device, kind, 20 + index * 24, 0., button, buttons, index)
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        assert not canvas._drawing
        assert canvas._native_input_predecessor() is None
        assert not canvas._native_deferred_activations
        assert canvas.command_stack.revision == history
        assert canvas.chapter.objects[raster.object_id] is raster
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == before
