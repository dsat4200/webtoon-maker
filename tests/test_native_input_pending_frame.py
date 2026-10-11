"""A pending presentation frame cannot defer or replace native pointer samples."""
import time

import pytest
from PySide6.QtCore import Qt

from comic_editor.ui.canvas import ToolKind
from test_native_pointer_lifetime import native_source, pointer
from test_painting_input_cursor import painting_canvas, wait_for


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('native', [False, True])
def test_received_pen_burst_updates_originals_while_presentation_timer_is_pending(
        painting_canvas, tool, native, monkeypatch):
    canvas, raster = painting_canvas
    before = native_source(canvas, raster, tool, native=native)
    assert canvas.set_tool(tool)
    send = pointer(canvas, 'pen')
    revision = canvas.command_stack.revision
    send('press', (20, 40), .6)
    wait_for(lambda: canvas._native_input_predecessor() is not None and bool(canvas._stroke_before))

    applied = []
    if tool == ToolKind.BRUSH:
        begin_time = canvas._paint_brush_sample.time
        apply = canvas._apply_paint_brush_sample
        def record(sample, *, begin=False):
            applied.append(((sample.x, sample.y), sample.pressure, sample.time-begin_time,
                            sample.tilt_x, sample.tilt_y, sample.rotation))
            return apply(sample, begin=begin)
        monkeypatch.setattr(canvas, '_apply_paint_brush_sample', record)
    else:
        apply = canvas._continue_stroke_impl
        def record(point, pressure):
            applied.append(((point.x(), point.y()), pressure))
            return apply(point, pressure)
        monkeypatch.setattr(canvas, '_continue_stroke_impl', record)

    # Stop setup's pending frame. Every following call is a real Qt event;
    # without event-loop pumping, even a due timer cannot run between packets.
    # This establishes ordering without relying on CPU speed or timer jitter.
    canvas._visual_frame_timer.stop()
    canvas._flush_visual_dirty()
    canvas._last_painting_frame_start = time.perf_counter()
    canvas._queue_widget_dirty(canvas.rect())
    assert canvas._visual_frame_timer.isActive()
    frames = []
    canvas._visual_frame_timer.timeout.connect(lambda: frames.append(True))
    samples = [((24+index*2, 40+index%3), .2+(index%6)/10) for index in range(36)]
    samples[9] = (samples[9][0], 0.)
    for index, (position, pressure) in enumerate(samples, 1):
        send('move', position, pressure, timestamp=1000+index*8)
        assert len(applied) == index
        assert canvas._visual_frame_timer.isActive()
        assert not frames
        assert canvas.command_stack.revision == revision
        assert canvas.cursor().shape() == Qt.BlankCursor

    assert [(sample[0], sample[1]) for sample in applied] == samples
    if tool == ToolKind.BRUSH:
        assert [sample[2] for sample in applied] == pytest.approx([index*.008 for index in range(1, 37)])
        assert all(sample[3:] == pytest.approx((12., -7., 33.)) for sample in applied)
    last = samples[-1][0]
    held = canvas.tiles.tile(raster.object_id, (0, 0))
    assert held.format() == before.format()
    assert held.pixelColor(*last) != before.pixelColor(*last)
    assert canvas._visual_frame_timer.isActive() and not frames

    # A lift carrying a new endpoint must also reach the native owner before
    # any presentation timeout, using the last received positive pressure.
    send('release', (108, 40), timestamp=1000+37*8)
    assert applied[-1][:2] == ((108., 40.), samples[-1][1])
    assert not frames
    wait_for(lambda: canvas._native_input_predecessor() is None)
    assert canvas.command_stack.revision == revision+1
    assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(108, 40) != before.pixelColor(108, 40)
    canvas.command_stack.undo()
    assert canvas.tiles.tile(raster.object_id, (0, 0)) == before
