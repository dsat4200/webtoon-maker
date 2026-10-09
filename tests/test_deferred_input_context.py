"""Accepted contacts retain their authoring context across later UI changes."""
from threading import Event

import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.models import RasterObject, ToneMask
from comic_editor.ui.canvas import ToolKind
from test_brush_input_admission import close, contact, make_canvas, pixels, wait_for


def pencil(canvas):
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_stroke(QPointF(40, 60), .8)
    canvas._continue_stroke(QPointF(300, 60), .5)
    canvas._end_stroke()


@pytest.mark.parametrize('tool', [ToolKind.BRUSH, ToolKind.LASSO_BRUSH])
@pytest.mark.parametrize('editor_change', ['mask', 'multiple-selection'])
def test_released_deferred_contact_keeps_pixels_and_history_after_mode_change(
        qapp, tmp_path, monkeypatch, tool, editor_change):
    from comic_editor.ui import tile_input
    warm, warm_raster = make_canvas(tmp_path, cold=False, tool=tool, native=True)
    canvas, raster = make_canvas(tmp_path, cold=True, tool=tool, native=True)
    entered, release = Event(), Event()
    original = tile_input.prepare_input_tiles
    try:
        initial = pixels(warm, warm_raster)
        pencil(warm)
        wait_for(lambda: warm._native_input_predecessor() is None)
        after_pencil = pixels(warm, warm_raster)
        assert warm.set_tool(tool)
        contact(warm, tool)
        wait_for(lambda: warm._native_input_predecessor() is None)
        expected = pixels(warm, warm_raster)
        labels = [command.label for command in warm.command_stack._undo]

        def prepare(*args):
            entered.set()
            assert release.wait(10)
            return original(*args)
        for name in ('working_bytes', 'snapshot_working_bytes', 'admission_priority', 'source_capture'):
            setattr(prepare, name, getattr(original, name))
        monkeypatch.setattr(tile_input, 'prepare_input_tiles', prepare)
        pencil(canvas)
        wait_for(entered.is_set)
        assert canvas.set_tool(tool)
        contact(canvas, tool)
        assert len(canvas._native_deferred_activations) == 1
        if editor_change == 'mask':
            mask = ToneMask(saved=True)
            canvas.chapter.masks[mask.mask_id] = mask
            # Follow the same mode/tool sequence as MainWindow._enter_mask_mode.
            canvas.set_tone_mask_mode(mask.mask_id)
            assert canvas.set_tool(ToolKind.RASTER_PENCIL)
        else:
            other = canvas.chapter.add_object(raster.parent_layer_id, RasterObject())
            assert canvas.set_selection_set([('object', raster.object_id), ('object', other.object_id)])
        ui = (canvas.active_tone_mask_id, tuple(canvas.selected_entities), canvas.tool)
        assert not canvas.command_stack.can_undo, 'Changing UI mode invented a mask transaction'
        release.set()
        wait_for(lambda: canvas._native_input_predecessor() is None
                 and not canvas._native_deferred_activations)
        assert [command.label for command in canvas.command_stack._undo] == labels
        assert pixels(canvas, raster) == expected
        assert (canvas.active_tone_mask_id, tuple(canvas.selected_entities), canvas.tool) == ui
        if editor_change == 'mask':
            assert not canvas.tiles.object_tiles(mask.mask_id)
            assert mask.revision == 0
        canvas.command_stack.undo()
        assert pixels(canvas, raster) == after_pencil
        canvas.command_stack.undo()
        assert pixels(canvas, raster) == initial
        canvas.command_stack.redo()
        canvas.command_stack.redo()
        assert pixels(canvas, raster) == expected
    finally:
        release.set()
        canvas._cancel_native_raster_stroke()
        close(canvas)
        close(warm)
