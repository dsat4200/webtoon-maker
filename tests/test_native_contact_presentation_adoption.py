"""Warm contact adoption updates untouched backing pixels outside the ink rows."""
import math
import time
import pytest
from PySide6.QtCore import QEvent, QPointF
from PySide6.QtGui import QColor, QImage, QRegion

from comic_editor.core.models import BoundGeometry
from comic_editor.core.tools import ToolKind
from test_native_contact_partial_repaint import (
    assert_backing_equals_full_repaint, contact_canvas, packet, pump, snapshot,
)
from test_native_pointer_lifetime import hold_native_source
from test_painting_input_cursor import wait_for


@pytest.mark.parametrize('tool,empty_erase', [
    (ToolKind.RASTER_PENCIL, False), (ToolKind.RASTER_ERASER, False),
    (ToolKind.BRUSH, False), (ToolKind.RASTER_ERASER, True),
])
def test_warm_fractional_contact_adopts_whole_visible_presentation_without_forced_held_paint(
        contact_canvas, tool, empty_erase, qapp, wait_scene, monkeypatch):
    import comic_editor.ui.document_presentation as presentation
    canvas, selected = contact_canvas
    native_mosaic = presentation._native_raster_mosaic

    def idle_native_fallback(owner, *args, **kwargs):
        if owner is canvas and not canvas._drawing:
            # The optional mosaic can decline an otherwise valid ordinary
            # native batch (for example incomplete coverage or allocation).
            # Exercise that existing presentation path before native input
            # adopts a complete owned mosaic; no source/render data changes.
            presentation._discard_native_raster_mosaic(owner)
            return None
        return native_mosaic(owner, *args, **kwargs)

    monkeypatch.setattr(presentation, '_native_raster_mosaic', idle_native_fallback)
    canvas.scale = .57
    # Both native document edges are fractional and visible. Most of their
    # length is outside every authored/cursor dirty row in this contact.
    canvas.setFixedSize(640, 500)
    canvas.center_x, canvas.center_y = 731., 260.
    canvas.chapter.width = 1080  # Final visible native core is clipped to 56px.
    page = canvas.chapter.layers[selected.parent_layer_id]
    page.bound = BoundGeometry.rectangle(8, 8, 1064, 496)
    other = next(obj for obj in canvas.chapter.objects.values() if obj.object_id != selected.object_id)
    for y in range(2):
        background = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        background.fill(QColor('#2050a0'))
        canvas.tiles.set_tile(other.object_id, (3, y), background)
        canvas.tiles.set_tile(other.object_id, (4, y), background)
    selected.interaction_rect = (48., 48., 950., 384.)
    canvas.set_tool(tool)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    pump(qapp, .04)
    # The empty eraser covers transparent source; it still requires an admitted
    # presentation refresh. The positive tools also revisit the view after undo.
    for cycle in range(1 if empty_erase else 2):
        assert not canvas._drawing
        canvas.repaint()  # Establish only the untouched idle backing baseline.
        pump(qapp, .04)
        baseline = snapshot(canvas)
        assert canvas._native_raster_mosaic is None
        prepared = canvas._scene_controller.feedback
        assert prepared is not None, 'This regression requires a warm native scene'
        before_history = canvas.command_stack.revision
        start = QPointF(575. if empty_erase else 425., 180.)
        empty_source = canvas.tiles.tile(selected.object_id, (2, 0)) if empty_erase else None
        empty_before = QImage(empty_source) if empty_source is not None else None
        canvas.painted.clear()
        packet(canvas, QEvent.TabletPress, start, cycle*200)
        pump(qapp, .035)
        assert canvas._drawing
        assert any(region.boundingRect() == canvas.rect() for region in canvas.painted)
        canvas.painted.clear()
        began = time.perf_counter()
        for index in range(1, 129):
            fraction = index/128
            point = QPointF(start.x()+70*fraction, start.y()+12*math.sin(2*math.pi*fraction))
            packet(canvas, QEvent.TabletMove, point, cycle*200+index)
            due = began+index/240
            pump(qapp, max(.00001, min(.001, due-time.perf_counter())))
            while time.perf_counter() < due:
                pump(qapp, min(.001, due-time.perf_counter()))
        pump(qapp, .025)
        assert canvas._scene_controller.feedback is prepared
        if not empty_erase:
            assert canvas._native_raster_mosaic is not None, 'The held scene must adopt its complete native mosaic'
        assert canvas.command_stack.revision == before_history
        assert canvas.painted
        if not empty_erase:
            assert all(region.boundingRect() != canvas.rect() for region in canvas.painted)
        if empty_erase:
            # A missing source currently retains ordinary fallback presentation;
            # the initial admitted refresh must still be independent of dirt.
            assert canvas.tiles.tile(selected.object_id, (2, 0)) == empty_before
        assert bytes(snapshot(canvas).constBits()) != bytes(baseline.constBits())
        # The first held snapshot reads retained QWidget pixels. No grab,
        # render or repaint has refreshed the held canvas before this assertion.
        assert_backing_equals_full_repaint(canvas)
        packet(canvas, QEvent.TabletRelease, point, cycle*200+129)
        pump(qapp)
        wait_scene(canvas)
        canvas.command_stack.undo()
        wait_scene(canvas)
        pump(qapp, .04)


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_deferred_successor_adopts_presentation_after_its_received_repaint_is_consumed(
        contact_canvas, tool, qapp, tmp_path, monkeypatch):
    canvas, selected = contact_canvas
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    entered, release = hold_native_source(canvas, selected, tmp_path, monkeypatch)
    before_history = canvas.command_stack.revision
    first, successor = QPointF(180., 150.), QPointF(425., 180.)
    admitted, queued = [], []
    recording = False
    enqueue = canvas._queue_widget_dirty

    def record_damage(widget, **kwargs):
        if recording:
            queued.append(QRegion(widget))
        return enqueue(widget, **kwargs)

    monkeypatch.setattr(canvas, '_queue_widget_dirty', record_damage)
    if tool == ToolKind.BRUSH:
        apply_brush = canvas._apply_paint_brush_sample

        def record_brush(sample, *, begin=False):
            nonlocal recording
            # A uses Pencil; the only actual Brush begin belongs to received B.
            is_successor = begin
            recording = is_successor
            try:
                result = apply_brush(sample, begin=begin)
                if is_successor:
                    admitted.append(tuple(queued))
                return result
            finally:
                recording = False

        monkeypatch.setattr(canvas, '_apply_paint_brush_sample', record_brush)
    else:
        begin = canvas._begin_stroke_impl

        def record_native(point, pressure):
            nonlocal recording
            is_successor = point == successor
            recording = is_successor
            try:
                result = begin(point, pressure)
                if is_successor:
                    admitted.append(tuple(queued))
                return result
            finally:
                recording = False

        monkeypatch.setattr(canvas, '_begin_stroke_impl', record_native)
    try:
        packet(canvas, QEvent.TabletPress, first, 0)
        wait_for(entered.is_set)
        packet(canvas, QEvent.TabletRelease, first, 1)
        predecessor = canvas._native_input_predecessor()
        assert predecessor is not None and predecessor.released
        assert canvas.set_tool(tool)
        packet(canvas, QEvent.TabletPress, successor, 200)
        packet(canvas, QEvent.TabletMove, successor+QPointF(5., 0.), 201)
        assert canvas._native_deferred_activations
        assert canvas._native_input_predecessor() is predecessor
        # Let every received-contact repaint finish while A still owns source
        # admission. A full update requested only on B's receipt cannot satisfy
        # the independent presentation refresh required when B is later drawn.
        pump(qapp, .04)
        assert canvas.painted and not admitted
        assert canvas.command_stack.revision == before_history
        release.set()
        wait_for(lambda: bool(admitted))
        assert len(admitted) == 1
        assert QRegion(canvas.rect()) in admitted[0], 'Admitted B must independently refresh the complete presentation'
        assert canvas._drawing and canvas._pen_contact_active
        pump(qapp, .025)
        assert_backing_equals_full_repaint(canvas)
        packet(canvas, QEvent.TabletRelease, successor+QPointF(5., 0.), 202)
        wait_for(lambda: canvas._native_input_predecessor() is None
                 and not canvas._native_deferred_activations)
        assert canvas.command_stack.revision == before_history+2
    finally:
        release.set()
