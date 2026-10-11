"""Real held pointer input can warm its native planes while normal work stalls."""
from threading import Event
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from test_raster_contact_feedback import ready_paint, native_reference, pixels, _queue_blocked_native_scene


def wait_for(predicate, canvas=None, timeout=5.):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if canvas is not None:
            ready_paint(canvas)
        QCoreApplication.processEvents()
        if predicate():
            return
        time.sleep(.001)
    assert predicate(), 'Held contact did not reach the required current state'


def send(canvas, kind, x, y=64.):
    point = QPointF(x, y)
    event_type = {'press': QEvent.MouseButtonPress, 'move': QEvent.MouseMove,
        'release': QEvent.MouseButtonRelease}[kind]
    event = QMouseEvent(event_type, point, point,
        Qt.LeftButton if kind in {'press', 'release'} else Qt.NoButton,
        Qt.LeftButton if kind in {'press', 'move'} else Qt.NoButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


@pytest.fixture
def cold_canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    brush = BrushDefinition(size=12, antialiasing=0, spacing=.2)
    settings = EditorSettings(canvas_renderer='raster', predictive_ink=False,
        snap_to_grid=False, grid_overlay_visible=False, brush_presets=[brush.to_dict()],
        active_brush_id=brush.id, brush_size_px=12,
        pencil_size_px={'small':12,'medium':12,'large':12},
        eraser_size_px={'small':12,'medium':12,'large':12})
    canvas = CanvasWidget(settings)
    canvas.setFixedSize(128, 128)
    chapter = ChapterDocument(width=128, height=128, document_kind='asset', background='#ffffffff')
    page = chapter.add_page('Page', BoundGeometry.rectangle(8, 8, 112, 112))
    page.fill_color, page.border_width = None, 0
    back = chapter.add_object(page.layer_id, RasterObject())
    selected = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    front = chapter.add_object(page.layer_id, RasterObject())
    page.children.reverse()
    tiles = TileStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('blue'))
    tiles.set_tile(back.object_id, (0, 0), tile)
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(Qt.transparent)
    painter = QPainter(tile)
    painter.fillRect(48, 20, 16, 90, QColor('green'))
    painter.end()
    tiles.set_tile(front.object_id, (0, 0), tile)
    canvas.set_document(chapter, tiles)
    canvas.center_x = canvas.center_y = 64.
    canvas.scale = 1.
    canvas.primary_color = '#ffff0000'
    canvas.set_selection('layer', page.layer_id, activate_default_tool=False)
    yield canvas, selected, front
    canvas._cancel_native_raster_stroke()
    canvas._cancel_paint_brush()
    controller = canvas._scene_controller
    controller.reset()
    for scheduler in (controller.scheduler, controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=False)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def blocked_worker(monkeypatch, scheduler, *, before_admission=False):
    release, entered = Event(), Event()
    entry = '_evaluate' if before_admission else '_evaluate_admitted'
    original = getattr(scheduler, entry)
    def blocked(demand, token):
        entered.set()
        assert release.wait(10.)
        return original(demand, token)
    monkeypatch.setattr(scheduler, entry, blocked)
    return release, entered


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_actual_cold_mouse_stream_changes_visible_artwork_before_release_while_ordinary_worker_is_blocked(
        cold_canvas, tool, wait_scene, monkeypatch):
    canvas, selected, _front = cold_canvas
    if tool == ToolKind.RASTER_ERASER:
        red = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        red.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), red)
        canvas._invalidate_scene_cache()
    wait_scene(canvas)
    before = ready_paint(canvas)
    controller = canvas._scene_controller
    assert controller.feedback is None
    # Keep the ordinary evaluator stalled before its shared memory ticket.
    # A ticket already occupied by a deliberately cancellation-blind test
    # worker remains exclusive under the unchanged admission contract.
    ordinary_release, ordinary_entered = blocked_worker(monkeypatch, controller.scheduler, before_admission=True)
    bootstrap_release, bootstrap_entered = blocked_worker(monkeypatch, controller.bootstrap_scheduler)
    _queue_blocked_native_scene(canvas)
    wait_for(ordinary_entered.is_set)
    canvas.set_selection('object', selected.object_id, activate_default_tool=False)
    canvas.set_tool(tool)
    revision = canvas.command_stack.revision
    try:
        for index, x in enumerate((24., 32., 40., 72., 80., 88., 100.)):
            send(canvas, 'press' if index == 0 else 'move', x)
            wait_for(lambda: bool(canvas._stroke_before), canvas)
            assert canvas._drawing and canvas.command_stack.revision == revision
            assert canvas.cursor().shape() == Qt.BlankCursor
        wait_for(bootstrap_entered.is_set, canvas)
        assert controller._bootstrap is not None
        owner = controller._bootstrap
        assert owner.dirty == {(0, 0)}
        assert controller.feedback is None and controller.scheduler.busy
        bootstrap_release.set()
        wait_for(lambda: controller.feedback is not None, canvas)
        held = ready_paint(canvas)
        assert held.pixelColor(80, 64) != before.pixelColor(80, 64)
        assert held.pixelColor(80, 64) == QColor('blue' if tool == ToolKind.RASTER_ERASER else 'red')
        assert held.pixelColor(56, 64) == QColor('green')
        assert canvas._drawing and canvas.command_stack.revision == revision
        assert not ordinary_release.is_set() and controller.scheduler.busy
        assert controller.bootstrap_scheduler.submitted == 1
        assert controller.feedback_dirty == {(0, 0)}
        np.testing.assert_array_equal(pixels(held)[2:126, 8:504],
            pixels(native_reference(canvas))[2:126, 8:504])
        # The same held contact keeps accepting and displaying input after the
        # cold owner is adopted; it does not require a release or second press.
        send(canvas, 'move', 100., 90.)
        wait_for(lambda: ready_paint(canvas).pixelColor(100, 88) != before.pixelColor(100, 88), canvas)
        next_held = ready_paint(canvas)
        np.testing.assert_array_equal(pixels(next_held)[2:126, 8:504],
            pixels(native_reference(canvas))[2:126, 8:504])
        assert canvas._drawing and canvas.command_stack.revision == revision
        send(canvas, 'release', 100., 90.)
        wait_for(lambda: canvas.command_stack.revision == revision + 1)
    finally:
        bootstrap_release.set()
        ordinary_release.set()


@pytest.mark.parametrize('transition', ['release', 'cancel', 'selection', 'configuration', 'unrelated'])
def test_inflight_cold_owner_cannot_publish_after_loss_of_contact_ownership(
        cold_canvas, transition, wait_scene, monkeypatch):
    canvas, selected, front = cold_canvas
    wait_scene(canvas)
    controller = canvas._scene_controller
    release, entered = blocked_worker(monkeypatch, controller.bootstrap_scheduler)
    canvas.set_selection('object', selected.object_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    history = canvas.command_stack.revision
    try:
        send(canvas, 'press', 32.)
        send(canvas, 'move', 80.)
        wait_for(entered.is_set, canvas)
        owner = controller._bootstrap
        assert owner is not None and controller.feedback is None
        if transition == 'release':
            send(canvas, 'release', 80.)
        elif transition == 'cancel':
            canvas._cancel_native_raster_stroke()
        elif transition == 'selection':
            canvas.set_selection('object', front.object_id, activate_default_tool=False)
        elif transition == 'configuration':
            canvas.chapter.background = '#ff121212'
            canvas._publish_change_set(ChangeSet(document_fields=frozenset({'background'})))
        else:
            front.opacity = .5
            canvas._publish_change_set(ChangeSet(entities=(EntityChange(('object', front.object_id),
                frozenset({'opacity'})),)))
        ready_paint(canvas)
        controller._advance_bootstrap()
        assert owner.retired
        assert controller.feedback is None
        release.set()
        # Observe the same confirmed worker until it finishes; no new ordinary
        # exact completion is allowed to masquerade as the old bootstrap result.
        deadline = time.monotonic() + 5.
        while controller.bootstrap_scheduler.busy and time.monotonic() < deadline:
            controller.bootstrap_scheduler.poll()
            time.sleep(.001)
        assert not controller.bootstrap_scheduler.busy
        assert controller.feedback is None
        if transition == 'cancel':
            assert canvas.command_stack.revision == history
            assert canvas.tiles.tile(selected.object_id, (0, 0)) is None
    finally:
        release.set()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_bootstrap_created_after_first_source_packet_still_presents_the_held_first_dot(
        cold_canvas, tool, wait_scene, monkeypatch):
    canvas, selected, _front = cold_canvas
    if tool == ToolKind.RASTER_ERASER:
        red = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        red.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), red)
        canvas._invalidate_scene_cache()
    wait_scene(canvas)
    before = ready_paint(canvas)
    controller = canvas._scene_controller
    release, entered = blocked_worker(monkeypatch, controller.scheduler, before_admission=True)
    _queue_blocked_native_scene(canvas)
    wait_for(entered.is_set)
    canvas.set_selection('object', selected.object_id, activate_default_tool=False)
    canvas.set_tool(tool)
    history = canvas.command_stack.revision
    try:
        with monkeypatch.context() as delayed_creation:
            delayed_creation.setattr(controller, '_ensure_bootstrap', lambda *_: None)
            send(canvas, 'press', 32.)
            wait_for(lambda: (0, 0) in canvas._stroke_before, canvas)
            assert controller._bootstrap is None and controller.feedback is None
            source = canvas.tiles.tile(selected.object_id, (0, 0))
            assert source is not None
            assert source.pixelColor(32, 64) == QColor(Qt.transparent if tool == ToolKind.RASTER_ERASER else 'red')
        wait_for(lambda: controller.feedback is not None, canvas)
        actual = ready_paint(canvas)
        assert actual.pixelColor(32, 64) != before.pixelColor(32, 64)
        assert actual.pixelColor(32, 64) == QColor('blue' if tool == ToolKind.RASTER_ERASER else 'red')
        assert controller.feedback_dirty == {(0, 0)}
        np.testing.assert_array_equal(pixels(actual)[2:126, 8:504],
            pixels(native_reference(canvas))[2:126, 8:504])
        assert canvas._drawing and canvas.command_stack.revision == history
        send(canvas, 'release', 32.)
        wait_for(lambda: canvas.command_stack.revision == history + 1)
    finally:
        release.set()
