"""Actual Qt packets retain endpoints and resolve interrupted native contacts."""
from threading import Event

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QInputDevice, QKeyEvent, QMouseEvent, QPointingDevice, QTabletEvent

from comic_editor.ui.canvas import ToolKind
from comic_editor.core.models import ToneMask, VectorDrawingObject, VectorStroke, VectorStrokePoint
from test_painting_input_cursor import painting_canvas, wait_for


def pointer(canvas, input_kind):
    device = QPointingDevice('Lifetime pen', 173, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen,
        QInputDevice.Capability.Position | QInputDevice.Capability.Pressure, 1, 3)

    def send(kind, point, pressure=1., timestamp=1000):
        point = QPointF(*point)
        if input_kind == 'mouse':
            event_type = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
                          'release': QEvent.MouseButtonRelease}[kind]
            event = QMouseEvent(event_type, point, point,
                Qt.LeftButton if kind in {'press', 'release'} else Qt.NoButton,
                Qt.LeftButton if kind in {'press', 'move'} else Qt.NoButton, Qt.NoModifier)
        else:
            event_type = {'press': QEvent.TabletPress, 'move': QEvent.TabletMove,
                          'release': QEvent.TabletRelease}[kind]
            event = QTabletEvent(event_type, device, point, point,
                pressure if kind != 'release' else 0., 12., -7., 0., 33., 0., Qt.NoModifier,
                Qt.LeftButton if kind in {'press', 'release'} else Qt.NoButton,
                Qt.LeftButton if kind in {'press', 'move'} else Qt.NoButton)
        event.setTimestamp(timestamp)
        QCoreApplication.sendEvent(canvas, event)
    return send


def native_source(canvas, raster, tool, *, native=False):
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied if native
                   else QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red') if tool == ToolKind.RASTER_ERASER else QColor('#80336299'))
    canvas.tiles.set_tile(raster.object_id, (0, 0), image)
    return QImage(image)


def hold_native_source(canvas, raster, tmp_path, monkeypatch):
    from comic_editor.ui import tile_input
    # Register a fresh encoded owner after setup to guarantee the pointer's
    # first source is cold, without touching an on-disk user project.
    original = QImage(canvas.tiles.tile(raster.object_id, (0, 0)))
    path = tmp_path / 'native-contact.png'
    assert original.save(str(path), 'PNG')
    owner = canvas.tiles._tiles[raster.object_id]
    owner.register((0, 0), path)
    owner.residency.evict(owner, (0, 0))
    entered, release = Event(), Event()
    prepare = tile_input.prepare_input_tiles

    def held(*args):
        entered.set()
        assert release.wait(10)
        return prepare(*args)
    held.admission_priority = prepare.admission_priority
    held.working_bytes = prepare.working_bytes
    held.snapshot_working_bytes = prepare.snapshot_working_bytes
    monkeypatch.setattr(tile_input, 'prepare_input_tiles', held)
    return entered, release


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('input_kind', ['mouse', 'pen'])
@pytest.mark.parametrize('cold', [False, True])
def test_release_at_new_position_keeps_final_native_segment_and_one_history(
        painting_canvas, tool, input_kind, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    send = pointer(canvas, input_kind)
    baseline = canvas.command_stack.revision
    try:
        send('press', (32, 40))
        if cold:
            wait_for(entered.is_set)
        # Qt can deliver the final position in the release packet without an
        # intervening MouseMove/TabletMove; it is still an accepted endpoint.
        send('release', (90, 40), timestamp=1016)
        if cold:
            assert canvas.command_stack.revision == baseline
            release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        image = canvas.tiles.tile(raster.object_id, (0, 0))
        expected_alpha = 0 if tool == ToolKind.RASTER_ERASER else 255
        assert image.pixelColor(80, 40).alpha() == expected_alpha, 'Final native segment was dropped'
        assert image.pixelColor(90, 40).alpha() == expected_alpha
        assert canvas.command_stack.revision == baseline + 1
        # A duplicate terminal event must not create a second transaction.
        send('release', (90, 40), timestamp=1017)
        QCoreApplication.processEvents()
        assert canvas.command_stack.revision == baseline + 1
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('input_kind', ['mouse', 'pen'])
@pytest.mark.parametrize('interruption', [QEvent.UngrabMouse, QEvent.WindowDeactivate])
@pytest.mark.parametrize('cold', [False, True])
def test_lost_capture_finishes_contact_and_next_mouse_contact_is_accepted(
        painting_canvas, tool, input_kind, interruption, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    baseline = canvas.command_stack.revision
    send = pointer(canvas, input_kind)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    try:
        send('press', (32, 40))
        if cold:
            wait_for(entered.is_set)
        send('move', (48, 40), timestamp=1016)
        QCoreApplication.sendEvent(canvas, QEvent(interruption))
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        if not cold:
            wait_for(lambda: canvas._native_input_predecessor() is None)
            assert not canvas._drawing
            assert canvas.command_stack.revision == baseline + 1
        mouse = pointer(canvas, 'mouse')
        mouse('press', (80, 90), timestamp=2000)
        mouse('move', (96, 90), timestamp=2016)
        mouse('release', (96, 90), timestamp=2032)
        if cold:
            assert canvas.command_stack.revision == baseline
            release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
                 and not getattr(canvas, '_native_deferred_activations', None))
        assert not canvas._drawing
        expected_alpha = 0 if tool == ToolKind.RASTER_ERASER else 255
        assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(96, 90).alpha() == expected_alpha
        assert canvas.command_stack.revision == baseline + 2
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_pen_stream_does_not_coalesce_pressure_while_native_source_is_pending(
        painting_canvas, tool, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool, native=True)
    assert canvas.set_tool(tool)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch)
    send = pointer(canvas, 'pen')
    baseline = canvas.command_stack.revision
    packets = [((32 + index, 40 + index % 5), .1 + (index % 7) / 10)
               for index in range(1, 41)]
    delivered = []
    if tool == ToolKind.BRUSH:
        apply = canvas._apply_paint_brush_sample
        def record(sample, *, begin=False):
            delivered.append(((sample.x, sample.y), sample.pressure))
            return apply(sample, begin=begin)
        monkeypatch.setattr(canvas, '_apply_paint_brush_sample', record)
    else:
        apply = canvas._continue_stroke_impl
        def record(point, pressure):
            delivered.append(((point.x(), point.y()), pressure))
            return apply(point, pressure)
        monkeypatch.setattr(canvas, '_continue_stroke_impl', record)
    try:
        send('press', (32, 40), .3)
        wait_for(entered.is_set)
        for index, (point, pressure) in enumerate(packets, 1):
            send('move', point, pressure, timestamp=1000 + index * 8)
        send('release', packets[-1][0], timestamp=1330)
        assert not canvas.command_stack.can_undo and not delivered
        release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        if tool == ToolKind.BRUSH:
            # Brush uses its last positive pressure when a zero-pressure lift
            # appends the terminal sample at the same position.
            assert delivered == [((32., 40.), .3)] + packets + [packets[-1]]
        else:
            assert delivered == packets
        # PNG stores straight alpha; the input kernel must preserve the loaded
        # original's 16-bit format and precision contract.
        assert canvas.tiles.tile(raster.object_id, (0, 0)).format() == QImage.Format_RGBA64
        assert canvas.command_stack.revision == baseline + 1
    finally:
        release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('input_kind', ['mouse', 'pen'])
@pytest.mark.parametrize('cold', [False, True])
def test_owned_native_moves_skip_transform_text_and_modifier_hit_testing(
        painting_canvas, tool, input_kind, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    send = pointer(canvas, input_kind)
    try:
        send('press', (32, 40))
        if cold:
            wait_for(entered.is_set)
        with monkeypatch.context() as held:
            for name in ('object_world_quad', '_selected_object_transform_hit',
                         '_move_cage_handle', '_move_modifier_handle',
                         '_text_placement_move', '_queue_free_text_drag'):
                held.setattr(canvas, name, lambda *_a, **_k: pytest.fail('Held native input visited a gizmo hit test'))
            for index in range(1, 21):
                send('move', (32 + index * 2, 40), .2 + index / 30,
                     timestamp=1000 + index * 8)
        send('release', (72, 40), timestamp=1170)
        if release is not None:
            release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        assert len(canvas.command_stack._undo) == 1
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('cold', [False, True])
def test_escape_cancels_pen_dispatch_and_the_next_mouse_contact_can_start(
        painting_canvas, tool, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    send = pointer(canvas, 'pen')
    baseline = canvas.command_stack.revision
    try:
        send('press', (32, 40))
        if cold:
            wait_for(entered.is_set)
        send('move', (48, 40), timestamp=1016)
        QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
        assert not canvas._pen_contact_active and not canvas._tablet_tool_active
        assert not canvas._drawing
        assert canvas.command_stack.revision == baseline
        if release is not None:
            release.set()
        mouse = pointer(canvas, 'mouse')
        mouse('press', (80, 90), timestamp=2000)
        mouse('move', (96, 90), timestamp=2016)
        mouse('release', (96, 90), timestamp=2032)
        wait_for(lambda: canvas._native_input_predecessor() is None)
        expected_alpha = 0 if tool == ToolKind.RASTER_ERASER else 255
        assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(96, 90).alpha() == expected_alpha
        assert canvas.command_stack.revision == baseline + 1
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('cold', [False, True])
def test_release_only_endpoint_uses_last_received_pen_pressure_while_admission_lags(
        painting_canvas, tool, cold, tmp_path, monkeypatch):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    entered, release = hold_native_source(canvas, raster, tmp_path, monkeypatch) if cold else (None, None)
    delivered = []
    apply = canvas._continue_stroke_impl
    def record(point, pressure):
        delivered.append(((point.x(), point.y()), pressure))
        return apply(point, pressure)
    monkeypatch.setattr(canvas, '_continue_stroke_impl', record)
    send = pointer(canvas, 'pen')
    try:
        send('press', (32, 40), .2)
        if cold:
            wait_for(entered.is_set)
        send('move', (48, 40), .7, timestamp=1016)
        send('release', (90, 40), timestamp=1032)
        if release is not None:
            release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None)
        assert delivered == [((48., 40.), .7), ((90., 40.), .7)]
        assert len(canvas.command_stack._undo) == 1
    finally:
        if release is not None:
            release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('input_kind', ['mouse', 'pen'])
def test_same_position_release_does_not_repaint_a_translucent_native_dot(
        painting_canvas, tool, input_kind):
    canvas, raster = painting_canvas
    native_source(canvas, raster, tool)
    assert canvas.set_tool(tool)
    canvas.primary_color = '#40ff0000'
    canvas._preset.stroke_end_ratio = 1.
    send = pointer(canvas, input_kind)
    send('press', (40, 40))
    before_release = QImage(canvas.tiles.tile(raster.object_id, (0, 0)))
    send('release', (40, 40), timestamp=1016)
    wait_for(lambda: canvas._native_input_predecessor() is None)
    assert canvas.tiles.tile(raster.object_id, (0, 0)) == before_release
    assert len(canvas.command_stack._undo) == 1


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
def test_shared_vector_pencil_eraser_release_does_not_enter_native_raster_hooks(
        painting_canvas, tool, monkeypatch):
    canvas, raster = painting_canvas
    drawing = canvas.chapter.add_object(raster.parent_layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=16., y=40., width=12., opacity=1.),
                             VectorStrokePoint(x=104., y=40., width=12., opacity=1.)])]))
    canvas.set_selection('object', drawing.object_id)
    assert canvas.set_tool(tool)
    monkeypatch.setattr(canvas, '_continue_stroke', lambda *_: pytest.fail('Vector contact reached native input'))
    monkeypatch.setattr(canvas, '_end_stroke', lambda *_: pytest.fail('Vector contact reached native completion'))
    send = pointer(canvas, 'pen')
    send('press', (32, 40))
    send('move', (48, 40), timestamp=1016)
    send('release', (90, 40), timestamp=1032)
    assert not canvas._drawing and canvas._vector_gesture_mode is None
    assert not canvas._pen_contact_active and not canvas._tablet_tool_active
    assert len(canvas.command_stack._undo) == 1


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('terminal', ['release', 'focus', 'escape'])
def test_mask_contact_uses_native_endpoint_and_interruption_ownership(
        painting_canvas, tool, terminal):
    canvas, _raster = painting_canvas
    mask = ToneMask(saved=True)
    canvas.chapter.masks[mask.mask_id] = mask
    image = QImage(256, 256, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor('red') if tool == ToolKind.RASTER_ERASER else QColor('#20336299'))
    canvas.tiles.set_tile(mask.mask_id, (0, 0), image)
    canvas.set_tone_mask_mode(mask.mask_id)
    assert canvas.set_tool(tool)
    baseline = canvas.command_stack.revision
    send = pointer(canvas, 'pen')
    send('press', (32, 40))
    send('move', (48, 40), timestamp=1016)
    if terminal == 'release':
        send('release', (90, 40), timestamp=1032)
    elif terminal == 'focus':
        QCoreApplication.sendEvent(canvas, QEvent(QEvent.WindowDeactivate))
    else:
        QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    wait_for(lambda: canvas._native_input_predecessor() is None)
    assert not canvas._drawing and not canvas._pen_contact_active and not canvas._tablet_tool_active
    assert canvas.command_stack.revision == baseline + (terminal != 'escape')
    if terminal == 'release':
        expected_alpha = 0 if tool == ToolKind.RASTER_ERASER else 255
        assert canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(90, 40).alpha() == expected_alpha
