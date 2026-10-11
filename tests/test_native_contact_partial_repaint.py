"""Real QWidget backing pixels stay current under bounded held-contact damage."""
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QInputDevice, QPainter, QPointingDevice, QTabletEvent

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.tools import ToolKind
from comic_editor.ui.canvas import RasterCanvasWidget


class TracedCanvas(RasterCanvasWidget):
    def paintEvent(self, event):
        self.painted.append(event.region())
        super().paintEvent(event)


def snapshot(canvas):
    # QWidget.grab()/render() force a new paint and would hide stale backing
    # pixels. The screen reads the existing native backing store without one.
    image = canvas.screen().grabWindow(canvas.winId()).toImage()
    assert not image.isNull()
    return image.convertToFormat(QImage.Format_ARGB32_Premultiplied)


def pump(qapp, seconds=.018):
    deadline = time.monotonic()+seconds
    while time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.001)


def packet(canvas, kind, world, index):
    point = canvas.camera_transform().map(world)
    event = QTabletEvent(kind, canvas.test_pen, point, point,
        0. if kind == QEvent.TabletRelease else .8, 18., -11., 0., 0., 0., Qt.NoModifier,
        Qt.NoButton if kind == QEvent.TabletMove else Qt.LeftButton,
        Qt.NoButton if kind == QEvent.TabletRelease else Qt.LeftButton)
    event.setTimestamp(1000+index*16)
    QCoreApplication.sendEvent(canvas, event)
    assert event.isAccepted()


def assert_backing_equals_full_repaint(canvas):
    # No queued events or model mutation run between the snapshots. Both frames
    # observe exactly the same current gesture and complete editor overlays.
    actual = snapshot(canvas)
    canvas.repaint()
    expected = snapshot(canvas)
    assert actual.size() == expected.size()
    a = np.frombuffer(actual.constBits(), np.uint8).reshape(actual.height(), actual.width(), 4)
    b = np.frombuffer(expected.constBits(), np.uint8).reshape(expected.height(), expected.width(), 4)
    difference = np.abs(a.astype(np.int16)-b.astype(np.int16))
    rows, columns = np.where(np.any(difference != 0, axis=2))
    assert not len(rows), dict(pixels=len(rows), max_difference=int(difference.max()),
        bounds=[int(columns.min()), int(rows.min()), int(columns.max()), int(rows.max())],
        positions=list(zip(columns[:12].tolist(), rows[:12].tolist())),
        painted=[region.boundingRect().getRect() for region in canvas.painted])


@pytest.fixture
def contact_canvas(qapp, monkeypatch, wait_scene):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    canvas = TracedCanvas(EditorSettings(canvas_renderer='raster', predictive_ink=True,
        grid_overlay_visible=False, snap_to_grid=False,
        pencil_size_px=dict.fromkeys(('small', 'medium', 'large'), 16),
        eraser_size_px=dict.fromkeys(('small', 'medium', 'large'), 16)))
    canvas.painted = []
    canvas.setFixedSize(512, 384)
    chapter = ChapterDocument(width=768, height=512, document_kind='asset', background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(8, 8, 752, 496))
    page.fill_color, page.border_width = None, 0
    back = chapter.add_object(page.layer_id, RasterObject())
    selected = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(48., 48., 512., 384.)))
    page.children.reverse()
    tiles = TileStore()
    for y in range(2):
        for x in range(3):
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(QColor('#2050a0'))
            tiles.set_tile(back.object_id, (x, y), image)
            image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.fillRect(QRectF(80.-x*256, 80.-y*256, 400., 250.), QColor('#c04f40'))
            painter.end()
            tiles.set_tile(selected.object_id, (x, y), image)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y = 300., 230.
    canvas.primary_color = '#ff10e020'
    canvas.settings.active_paint_brush = lambda: BrushDefinition(size=16., antialiasing=0, spacing=.8)
    canvas.set_selection('object', selected.object_id)
    canvas.test_pen = QPointingDevice('Partial repaint pen', 981139, QInputDevice.DeviceType.Stylus,
        QPointingDevice.PointerType.Pen, QInputDevice.Capability.Position|QInputDevice.Capability.Pressure, 1, 3)
    canvas.setAttribute(Qt.WA_ShowWithoutActivating)
    canvas.show()
    wait_scene(canvas)
    pump(qapp)
    yield canvas, selected
    if canvas._drawing:
        canvas._interrupt_native_pointer_contact()
        canvas._interrupt_paint_brush()
    canvas._scene_controller.reset()
    for scheduler in (canvas._scene_controller.scheduler, canvas._scene_controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True, cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.close()
    canvas.deleteLater()


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('zoom', [.57, 1.])
@pytest.mark.parametrize('column', ['near', 'far'])
def test_held_contact_clears_old_hover_and_reverse_prediction_with_bounded_paints(
        contact_canvas, tool, zoom, column, qapp, wait_scene):
    canvas, selected = contact_canvas
    offset = QPointF()
    if column == 'far':
        selected.interaction_rect = (48., 48., 700., 384.)
        canvas.center_x = 450.
        canvas._invalidate_scene_cache()
        offset = QPointF(300., 0.)
    canvas.scale = zoom
    canvas.set_tool(tool)
    wait_scene(canvas)
    pump(qapp)
    start = QPointF(180., 150.)+offset
    packet(canvas, QEvent.TabletPress, start, 0)
    pump(qapp)
    assert canvas._drawing
    assert_backing_equals_full_repaint(canvas)
    stable_frame = tuple(selected.interaction_rect)
    for index, point in enumerate((QPointF(280., 150.), QPointF(190., 210.), QPointF(215., 200.)), 1):
        point += offset
        canvas.painted.clear()
        packet(canvas, QEvent.TabletMove, point, index)
        pump(qapp)
        assert tuple(selected.interaction_rect) == stable_frame
        assert canvas.painted
        assert any(region != canvas.rect() for region in canvas.painted)
        assert all(region.boundingRect() != canvas.rect() for region in canvas.painted), canvas.painted
        assert_backing_equals_full_repaint(canvas)
    packet(canvas, QEvent.TabletRelease, point, 5)
    pump(qapp)
    wait_scene(canvas)
    assert_backing_equals_full_repaint(canvas)


def test_frame_growth_repaints_previous_selection_and_transform_controls(
        contact_canvas, qapp, wait_scene):
    canvas, selected = contact_canvas
    canvas.scale = .57
    canvas.settings.pencil_transform_handles_visible = True
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    selected.interaction_rect = (80., 80., 260., 220.)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    pump(qapp)
    packet(canvas, QEvent.TabletPress, QPointF(160., 140.), 0)
    pump(qapp)
    assert canvas._drawing
    old_frame = tuple(selected.interaction_rect)
    canvas.painted.clear()
    packet(canvas, QEvent.TabletMove, QPointF(530., 340.), 1)
    pump(qapp)
    assert tuple(selected.interaction_rect) != old_frame
    assert any(region.boundingRect() == canvas.rect() for region in canvas.painted), canvas.painted
    assert_backing_equals_full_repaint(canvas)
