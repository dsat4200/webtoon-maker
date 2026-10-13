"""Real mouse and pen contacts retain current input and the brush outline."""
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import (QColor, QImage, QInputDevice, QMouseEvent, QPainter,
                          QPointingDevice, QTabletEvent)

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def wait_for(predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        QCoreApplication.processEvents()
        time.sleep(.001)
    assert predicate()


def overlay(canvas):
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        canvas._draw_tablet_hover(painter)
    finally:
        painter.end()
    return image


@pytest.fixture
def painting_canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 128, 128))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    raster.transform_frame = (0., 0., 128., 128.)
    raster.transform_quad = [(0.,0.), (128.,0.), (128.,128.), (0.,128.)]
    brush = BrushDefinition(size=12, antialiasing=0, spacing=.2)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', predictive_ink=False,
        snap_to_grid=False, grid_overlay_visible=False, brush_presets=[brush.to_dict()],
        active_brush_id=brush.id, brush_size_px=12,
        pencil_size_px={'small':12,'medium':12,'large':12},
        eraser_size_px={'small':12,'medium':12,'large':12}))
    canvas.setFixedSize(128, 128)
    canvas.set_document(chapter, TileStore())
    canvas.center_x = canvas.center_y = 64
    canvas.scale = 1
    canvas.primary_color = '#ffff0000'
    canvas.set_selection('object', raster.object_id)
    yield canvas, raster
    canvas._cancel_native_raster_stroke()
    canvas._cancel_paint_brush()
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True, cancel_futures=False)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('input_kind', ['mouse', 'pen'])
def test_received_contact_keeps_native_changes_and_custom_cursor_until_release(
        painting_canvas, tool, input_kind):
    canvas, raster = painting_canvas
    if tool == ToolKind.RASTER_ERASER:
        original = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        original.fill(QColor('red'))
        canvas.tiles.set_tile(raster.object_id, (0,0), original)
    assert canvas.set_tool(tool)
    history = canvas.command_stack.revision
    device = QPointingDevice('Cursor test pen', 71, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen,
        QInputDevice.Capability.Position | QInputDevice.Capability.Pressure, 1, 3)

    def send(kind, point):
        if input_kind == 'mouse':
            event_type = {'hover':QEvent.MouseMove,'press':QEvent.MouseButtonPress,
                          'move':QEvent.MouseMove,'release':QEvent.MouseButtonRelease}[kind]
            event = QMouseEvent(event_type, point, point,
                Qt.LeftButton if kind in {'press','release'} else Qt.NoButton,
                Qt.LeftButton if kind in {'press','move'} else Qt.NoButton, Qt.NoModifier)
        else:
            event_type = {'hover':QEvent.TabletMove,'press':QEvent.TabletPress,
                          'move':QEvent.TabletMove,'release':QEvent.TabletRelease}[kind]
            event = QTabletEvent(event_type, device, point, point,
                1. if kind in {'press','move'} else 0., 0., 0., 0., 0., 0., Qt.NoModifier,
                Qt.LeftButton if kind in {'press','release'} else Qt.NoButton,
                Qt.LeftButton if kind in {'press','move'} else Qt.NoButton)
        QCoreApplication.sendEvent(canvas, event)

    first, second = QPointF(32,40), QPointF(90,40)
    send('hover', first)
    assert canvas.cursor().shape() == Qt.BlankCursor
    assert overlay(canvas).pixelColor(32,40).alpha() > 0
    send('press', first)
    wait_for(lambda: canvas._drawing and (canvas._paint_brush_stroke is not None
             if tool == ToolKind.BRUSH else bool(canvas._stroke_before)))
    assert canvas.cursor().shape() == Qt.BlankCursor
    assert overlay(canvas).pixelColor(32,40).alpha() > 0
    assert canvas._tablet_tool_active == (input_kind == 'pen')
    assert canvas.command_stack.revision == history
    send('move', second)
    wait_for(lambda: canvas.tiles.tile(raster.object_id,(0,0)).pixelColor(90,40).alpha()
             == (0 if tool == ToolKind.RASTER_ERASER else 255))
    assert canvas.cursor().shape() == Qt.BlankCursor
    current = overlay(canvas)
    assert current.pixelColor(90,40).alpha() > 0
    assert current.pixelColor(32,40).alpha() == 0, 'Cursor stayed at the old contact position'
    assert canvas.command_stack.revision == history, 'Input was committed before release'
    send('release', second)
    wait_for(lambda: canvas._native_input_predecessor() is None)
    assert not canvas._drawing and not canvas._tablet_tool_active
    assert canvas.command_stack.revision == history + 1
    assert canvas.cursor().shape() == Qt.BlankCursor
    assert overlay(canvas).pixelColor(90,40).alpha() > 0
    QCoreApplication.sendEvent(canvas, QEvent(QEvent.Leave))
    cleared = overlay(canvas)
    assert not np.any(np.frombuffer(cleared.constBits(), np.uint8))


def test_brush_hover_keeps_transform_and_navigation_cursor_affordances(painting_canvas):
    canvas, _raster = painting_canvas
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.settings.pencil_transform_handles_visible = True
    canvas._update_interaction_cursor(QPointF(0,0))
    assert canvas.cursor().shape() == Qt.CrossCursor
    canvas._nav_mode = 'pan'
    canvas._update_interaction_cursor(QPointF(32,40))
    assert canvas.cursor().shape() != Qt.BlankCursor
    hidden = overlay(canvas)
    assert not np.any(np.frombuffer(hidden.constBits(), np.uint8))
    canvas._nav_mode = None
    assert canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    canvas._update_interaction_cursor(QPointF(32,40))
    assert canvas.cursor().shape() == Qt.CrossCursor


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_mouse_hover_does_not_reuse_an_earlier_pen_position(painting_canvas, tool):
    canvas, _raster = painting_canvas
    assert canvas.set_tool(tool)
    device = QPointingDevice('Cursor hover pen', 72, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position, 1, 3)
    first, second = QPointF(32,40), QPointF(90,40)
    QCoreApplication.sendEvent(canvas, QTabletEvent(QEvent.TabletMove, device, first, first,
        0., 0., 0., 0., 0., 0., Qt.NoModifier, Qt.NoButton, Qt.NoButton))
    QCoreApplication.sendEvent(canvas, QMouseEvent(QEvent.MouseMove, second, second,
        Qt.NoButton, Qt.NoButton, Qt.NoModifier))
    image = overlay(canvas)
    assert image.pixelColor(90,40).alpha() > 0
    assert image.pixelColor(32,40).alpha() == 0


def test_repeated_hover_does_not_repeat_cursor_or_tooltip_native_changes(painting_canvas, monkeypatch):
    canvas, _raster = painting_canvas
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._update_interaction_cursor(QPointF(32, 40))
    calls = []
    original_cursor, original_tooltip = canvas.setCursor, canvas.setToolTip
    monkeypatch.setattr(canvas, 'setCursor', lambda value: (calls.append('cursor'), original_cursor(value)))
    monkeypatch.setattr(canvas, 'setToolTip', lambda value: (calls.append('tooltip'), original_tooltip(value)))
    for _ in range(20):
        canvas._update_interaction_cursor(QPointF(32, 40))
    assert calls == []
    canvas.settings.pencil_transform_handles_visible = True
    canvas._update_interaction_cursor(QPointF(0, 0))
    assert calls == ['cursor']
    assert canvas.cursor().shape() == Qt.CrossCursor


def test_rotated_solo_overlay_only_schedules_full_guides_on_readiness_changes(painting_canvas, monkeypatch):
    from types import SimpleNamespace
    canvas, raster = painting_canvas
    canvas.rotation = -.663008407605119
    canvas.set_solo_entities({('object', raster.object_id)})
    canvas._raster_contact_active = True
    gate = SimpleNamespace(busy=False)
    monkeypatch.setattr(canvas, '_raster_tile_input', gate, raising=False)
    damage = []
    monkeypatch.setattr(canvas, '_queue_widget_dirty', lambda value, **_kw: damage.append(value))
    monkeypatch.setattr(canvas, 'update', lambda *_a: pytest.fail('Overlay bypassed damage scheduling'))
    for _ in range(10):
        canvas._queue_painting_overlay_damage()
    assert canvas.rect() not in damage
    gate.busy = True
    for _ in range(10):
        canvas._queue_painting_overlay_damage()
    assert damage.count(canvas.rect()) == 1
    gate.busy = False
    canvas._queue_painting_overlay_damage()
    assert damage.count(canvas.rect()) == 2
    canvas._raster_tile_input = None
    canvas._raster_contact_active = False


def test_hover_repaint_deadline_is_retained_and_press_promotes_it(painting_canvas):
    canvas, _raster = painting_canvas
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._visual_frame_timer.stop()
    canvas._pointer_hover_widget = QPointF(32, 40)
    canvas.grab()
    assert canvas._last_painting_frame_start is not None
    canvas._queue_painting_overlay_damage()
    remaining = canvas._visual_frame_timer.remainingTime()
    assert 0 < remaining <= 16
    canvas._queue_painting_overlay_damage()
    assert 0 <= canvas._visual_frame_timer.remainingTime() <= remaining
    canvas._queue_painting_contact_adoption()
    assert canvas._visual_frame_timer.remainingTime() == 0
