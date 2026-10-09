"""Cold native contacts do not enqueue competing whole-scene evaluations."""
import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.tools import ToolKind
from test_native_input_admission import source, prepare_gated, wait_for
from test_raster_contact_feedback import ready_paint
from test_covered_native_contact_deferral import trace_scene_work


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('transition', ['ready', 'release', 'cancel'])
def test_cold_contact_defers_scene_until_input_ready_or_contact_released(
        source, tool, transition, monkeypatch, qapp):
    canvas, _obj, _mask, _original = source
    canvas.set_tool(tool)
    from comic_editor.ui import tile_input
    source_capture = tile_input.prepare_input_tiles.source_capture
    entered, release, _threads = prepare_gated(monkeypatch)
    tile_input.prepare_input_tiles.source_capture = source_capture
    calls = trace_scene_work(canvas, monkeypatch)
    controller = canvas._scene_controller
    try:
        if tool == ToolKind.BRUSH:
            canvas._begin_paint_brush(QPointF(30, 70), .7, _definition=BrushDefinition(size=14))
        else:
            canvas._begin_stroke(QPointF(30, 70), .7)
        for _ in range(4):
            ready_paint(canvas)
            controller.advance()
            qapp.processEvents()
        wait_for(entered.is_set)
        gate = getattr(canvas, '_paint_brush_tile_input' if tool == ToolKind.BRUSH else '_raster_tile_input')
        assert gate.current() and gate.busy and not gate.released
        assert controller._contact_reuse_gate is gate
        assert controller.capture is None and controller.dispatched is None
        assert not controller.timer.isActive()
        assert not calls['capture'] and not calls['submit']
        if transition == 'ready':
            release.set()
            wait_for(lambda: not gate.busy)
        elif transition == 'release':
            (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
            assert gate.released and gate.busy
        else:
            gate.cancel()
        ready_paint(canvas)
        controller.advance()
        assert controller._contact_reuse_gate is None
        assert calls['capture'], 'Losing the cold input guard must resume scene capture'
    finally:
        release.set()
        if canvas._drawing:
            (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
        wait_for(lambda: gate.jobs.active is None)
