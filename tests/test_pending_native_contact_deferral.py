"""Cold native contacts do not enqueue competing whole-scene evaluations."""
import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.tools import ToolKind
from test_native_input_admission import source, prepare_gated, wait_for
from test_raster_contact_feedback import ready_paint
from test_covered_native_contact_deferral import trace_scene_work, assert_current_native_reuse


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
    history = canvas.command_stack.revision
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
        if transition == 'ready':
            document, visible, _bootstrap_gate, _covered_gate = assert_current_native_reuse(canvas, controller, gate)
            assert canvas.command_stack.revision == history, 'Ready held packets must not commit history'
            assert not calls['capture'] and not calls['submit']
            # The real gated native preparation and first packet have completed.
            # Read only a buffer whose residency was admitted by that gate.
            owner = canvas.tiles._tiles[_obj.object_id]
            assert (owner, (0, 0)) in owner.residency.entries
            edited = canvas.tiles.tile(_obj.object_id, (0, 0))
            assert edited.depth() == _original.depth() == 64
            assert edited.pixelColor(30, 70) != _original.pixelColor(30, 70), 'Real first native packet was not applied'
            def current_feedback_ready():
                ready_paint(canvas)
                controller.advance()
                current = canvas._render_document_state()
                return controller._covered_contact_gate(current, visible) is gate
            wait_for(current_feedback_ready)
            prepared = controller.feedback
            current = canvas._render_document_state()
            assert prepared is not None and prepared.identifier == _obj.object_id
            assert prepared.document.identity == current.identity
            assert prepared.document.configuration == current.configuration
            assert prepared.document.pixel_contract == current.pixel_contract
            assert not calls['capture'] and not calls['submit']
            assert canvas.command_stack.revision == history
            (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
            wait_for(lambda: not canvas._drawing and gate.closed)
            ready_paint(canvas)
            controller.advance()
            assert controller._contact_reuse_gate is None and controller._bootstrap is None
            assert calls['capture'], 'Release must really resume ordinary source capture'
            assert canvas.command_stack.revision == history + 1
        else:
            assert controller._contact_reuse_gate is None
            assert calls['capture'], 'Losing the cold input guard must resume scene capture'
    finally:
        release.set()
        if canvas._drawing:
            (canvas._finish_paint_brush if tool == ToolKind.BRUSH else canvas._end_stroke)()
        wait_for(lambda: gate.jobs.active is None)
