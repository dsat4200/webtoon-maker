"""Ctrl pen-down selects through the mouse picker without creating artwork."""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import (QGuiApplication, QImage, QInputDevice, QMouseEvent,
                          QPointingDevice, QTabletEvent)

from comic_editor.core.models import BoundGeometry, RasterObject
from comic_editor.ui.canvas import ToolKind
from test_native_pointer_lifetime import hold_native_source, native_source
from test_painting_input_cursor import painting_canvas, wait_for
from test_tablet_button_transitions import held_packets


def _pointer(canvas, input_kind, modifiers):
    device = QPointingDevice('Ctrl selection pen', 21933, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen,
        QInputDevice.Capability.Position | QInputDevice.Capability.Pressure, 1, 3)

    def send(kind, point, pressure=.5, *, button=None, buttons=None, index=0):
        point = QPointF(*point)
        button = (Qt.LeftButton if kind in {'press', 'release'} else Qt.NoButton
                  ) if button is None else button
        buttons = (Qt.NoButton if kind == 'release' else Qt.LeftButton
                   ) if buttons is None else buttons
        if input_kind == 'mouse':
            event = QMouseEvent({'press': QEvent.MouseButtonPress,
                'move': QEvent.MouseMove, 'release': QEvent.MouseButtonRelease}[kind],
                point, point, button, buttons, modifiers if kind == 'press' else Qt.NoModifier)
        else:
            event = QTabletEvent({'press': QEvent.TabletPress,
                'move': QEvent.TabletMove, 'release': QEvent.TabletRelease}[kind],
                device, point, point, pressure, 0., 0., 0., 0., 0.,
                modifiers if kind == 'press' else Qt.NoModifier, button, buttons)
        event.setTimestamp(1000 + index * 8)
        QCoreApplication.sendEvent(canvas, event)
        assert event.isAccepted()
    return send


def _layers(canvas, raster, rectangle=(4, 4, 100, 100)):
    return [canvas.chapter.add_layer(raster.parent_layer_id, name,
        BoundGeometry.rectangle(*rectangle)).layer_id for name in ('First', 'Second')]


@pytest.mark.parametrize('input_kind,packet_ctrl,keyboard_ctrl', [
    ('mouse', True, False), ('pen', True, False), ('pen', False, True)])
@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('cold', [False, True])
def test_ctrl_native_pen_picker_selects_layer_without_stroke_or_source_reads(
        painting_canvas, input_kind, packet_ctrl, keyboard_ctrl, tool, cold,
        tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    original = native_source(canvas, raster, tool, native=True)
    layers = _layers(canvas, raster)
    assert canvas.set_tool(tool)
    entered, unblock = (hold_native_source(canvas, raster, tmp_path, monkeypatch)
                        if cold else (None, None))
    if cold:
        # The real encoded owner decodes to straight native RGBA64. Compare
        # with that pen-down baseline without warming the canvas residency.
        original = QImage(str(tmp_path / 'native-contact.png'))
    owner = canvas.tiles._tiles[raster.object_id]
    source_owner = owner.entries[(0, 0)]
    decodes, revision = canvas.tiles.residency.decodes, canvas.command_stack.revision
    document, requested = canvas.chapter.to_dict(), []
    keyboard = [Qt.ControlModifier if keyboard_ctrl else Qt.NoModifier]
    monkeypatch.setattr(QGuiApplication, 'keyboardModifiers', lambda: keyboard[0])

    def choose(hits, _position):
        requested.append(hits)
        assert all({'kind': 'layer', 'id': layer} in hits for layer in layers)
        canvas.set_selection('layer', layers[1])
    canvas.selectionCandidatesRequested.connect(choose)
    send = _pointer(canvas, input_kind,
        Qt.ControlModifier if packet_ctrl else Qt.NoModifier)
    try:
        send('press', (8, 8))
        assert len(requested) == 1
        assert (canvas.selected_kind, canvas.selected_id) == ('layer', layers[1])
        assert not canvas._drawing and canvas._native_input_predecessor() is None
        # Neither releasing Ctrl nor moving a pressed tip may turn the picker
        # into native paint or a delayed source admission.
        keyboard[0] = Qt.NoModifier
        send('move', (40, 40), index=1)
        send('release', (40, 40), pressure=0., index=2)
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        assert len(requested) == 1 and not canvas._drawing
        assert canvas._native_input_predecessor() is None
        assert canvas.command_stack.revision == revision
        assert canvas.chapter.to_dict() == document
        assert owner.entries[(0, 0)] is source_owner
        assert canvas.tiles.residency.decodes == decodes
        if entered is not None:
            assert not entered.is_set(), 'Selection queued a native source worker'
        assert canvas.tiles.tile(raster.object_id, (0, 0)) == original
    finally:
        if unblock is not None:
            unblock.set()


@pytest.mark.parametrize('foreign_objects', [False, True])
def test_shape_picker_uses_ctrl_in_tablet_packet_when_keyboard_has_none(
        painting_canvas, foreign_objects, monkeypatch):
    canvas, raster = painting_canvas
    layers = _layers(canvas, raster)
    away = canvas.chapter.add_layer(raster.parent_layer_id, 'Selected elsewhere',
        BoundGeometry.rectangle(300, 300, 40, 40))
    if foreign_objects:
        other = canvas.chapter.add_object(raster.parent_layer_id,
            RasterObject(interaction_rect=(0, 0, 128, 128)))
        expected = [{'kind': 'object', 'id': raster.object_id},
                    {'kind': 'object', 'id': other.object_id}]
    else:
        raster.visible = False
        expected = [{'kind': 'layer', 'id': layer} for layer in layers]
    canvas.set_selection('layer', away.layer_id)
    assert canvas.set_tool(ToolKind.SHAPE_EDIT)
    monkeypatch.setattr(QGuiApplication, 'keyboardModifiers', lambda: Qt.NoModifier)
    requested = []
    canvas.selectionCandidatesRequested.connect(lambda hits, _position: requested.append(hits))
    send = _pointer(canvas, 'pen', Qt.ControlModifier)
    send('press', (8, 8))
    send('release', (8, 8), pressure=0.)
    assert len(requested) == 1
    assert all(item in requested[0] for item in expected)
    assert canvas.selected_id == away.layer_id


def test_deferred_native_tap_uses_down_snapshot_over_ctrl_pressed_before_lift(
        painting_canvas, monkeypatch):
    canvas, raster = painting_canvas
    raster.interaction_rect = raster.transform_frame = (0., 0., 20., 20.)
    raster.transform_quad = [(0., 0.), (20., 0.), (20., 20.), (0., 20.)]
    _layers(canvas, raster, rectangle=(54, 54, 60, 60))
    canvas.settings.pencil_transform_handles_visible = False
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    keyboard, requested = [Qt.NoModifier], []
    monkeypatch.setattr(QGuiApplication, 'keyboardModifiers', lambda: keyboard[0])
    canvas.selectionCandidatesRequested.connect(lambda *_args: requested.append(True))
    send = _pointer(canvas, 'pen', Qt.NoModifier)
    send('press', (60, 60))
    assert canvas._pending_raster_press is not None
    keyboard[0] = Qt.ControlModifier
    send('release', (60, 60), pressure=0.)
    assert not requested, 'A later Ctrl key changed an earlier unmodified tap'
    assert canvas.selected_kind == 'layer'
    assert canvas._pending_raster_press is None and not canvas._drawing


def test_deferred_transform_tap_keeps_tablet_ctrl_after_keyboard_release(
        painting_canvas, monkeypatch):
    canvas, raster = painting_canvas
    raster.interaction_rect = raster.transform_frame = (0., 0., 20., 20.)
    raster.transform_quad = [(0., 0.), (20., 0.), (20., 20.), (0., 20.)]
    layers = _layers(canvas, raster, rectangle=(30, 4, 80, 80))
    assert canvas.set_tool(ToolKind.TRANSFORM)
    monkeypatch.setattr(QGuiApplication, 'keyboardModifiers', lambda: Qt.NoModifier)
    requested = []
    canvas.selectionCandidatesRequested.connect(lambda hits, _position: requested.append(hits))
    send = _pointer(canvas, 'pen', Qt.ControlModifier)
    send('press', (36, 10))
    assert canvas._pending_raster_transform_press is not None
    assert not requested
    send('release', (36, 10), pressure=0.)
    assert len(requested) == 1
    assert all({'kind': 'layer', 'id': layer} in requested[0] for layer in layers)
    assert canvas._pending_raster_transform_press is None


@pytest.mark.parametrize('cold', [False, True])
def test_ctrl_during_held_barrel_transitions_keeps_every_native_sample(
        painting_canvas, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, ToolKind.RASTER_PENCIL)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    entered, unblock = (hold_native_source(canvas, raster, tmp_path, monkeypatch)
                        if cold else (None, None))
    keyboard, admitted, requested = [Qt.NoModifier], [], []
    monkeypatch.setattr(QGuiApplication, 'keyboardModifiers', lambda: keyboard[0])
    canvas.selectionCandidatesRequested.connect(lambda *_args: requested.append(True))
    begin, move = canvas._begin_stroke_impl, canvas._continue_stroke_impl
    def record_begin(point, pressure):
        admitted.append((point.toTuple(), pressure))
        return begin(point, pressure)
    def record_move(point, pressure):
        admitted.append((point.toTuple(), pressure))
        return move(point, pressure)
    monkeypatch.setattr(canvas, '_begin_stroke_impl', record_begin)
    monkeypatch.setattr(canvas, '_continue_stroke_impl', record_move)
    send, packets = _pointer(canvas, 'pen', Qt.NoModifier), held_packets('replacement')
    expected, revision = [], canvas.command_stack.revision
    try:
        for index, (kind, button, buttons, pressure) in enumerate(packets):
            if index:
                keyboard[0] = Qt.ControlModifier
            point = (20 + index * 12, 40)
            send({QEvent.TabletPress: 'press', QEvent.TabletMove: 'move',
                QEvent.TabletRelease: 'release'}[kind], point, pressure,
                button=button, buttons=buttons, index=index)
            expected.append((point, pressure))
            assert canvas._drawing and canvas._pen_contact_active
            assert not requested
            if index == 0 and cold:
                wait_for(entered.is_set)
        send('release', (104, 40), pressure=0., index=len(packets))
        expected.append(((104., 40.), packets[-1][3]))
        if unblock is not None:
            unblock.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        assert admitted == expected
        assert not requested and canvas.selected_object_id == raster.object_id
        assert canvas.command_stack.revision == revision + 1
    finally:
        if unblock is not None:
            unblock.set()
