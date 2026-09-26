"""Clock availability follows the effective main/secondary raster stroke."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_correction import resolved_taper
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_controls import BrushSettingsDialog
from comic_editor.ui.canvas import ToolKind
from test_brush_ui import make_canvas, pixels


@pytest.mark.parametrize("post_correction",[0,.6])
def test_secondary_only_continuous_timer_uses_effective_scheduler_and_stops_at_finish(qapp,monkeypatch,post_correction):
    import comic_editor.ui.brush_features as features
    clock = [10.]
    monkeypatch.setattr(features.time,"monotonic",lambda:clock[0])
    second = BrushDefinition(size=10,density=.05,continuous=True,continuous_rate=10,
                             post_correction=.8)  # stored child correction is dormant
    brush = BrushDefinition(size=10,density=.1,continuous=False,dual=second,
                            dual_mode="add",post_correction=post_correction)
    original = brush.to_dict()
    canvas,_chapter,raster = make_canvas()
    try:
        canvas.settings.brush_presets = [brush.to_dict()]
        canvas.settings.active_brush_id = brush.id
        canvas.settings.brush_size_px = 10
        canvas.set_tool(ToolKind.BRUSH)
        canvas._begin_paint_brush(QPointF(30,30),1)
        stroke = canvas._paint_brush_stroke
        assert not stroke.scheduler.continuous_enabled
        assert stroke.dual_scheduler.continuous_enabled == (post_correction == 0)
        assert canvas._paint_brush_timer.isActive() == (post_correction == 0)
        before = stroke.dual_scheduler.index
        clock[0] += .3
        canvas._tick_paint_brush()
        assert stroke.dual_scheduler.index == before + (3 if post_correction == 0 else 0)
        canvas._finish_paint_brush()
        assert not canvas._paint_brush_timer.isActive()
        assert canvas._paint_brush_stroke is None
        assert len(canvas.command_stack._undo) == 1
        final = pixels(canvas.tiles,raster.object_id)
        clock[0] += 1
        canvas._tick_paint_brush()
        assert pixels(canvas.tiles,raster.object_id) == final
        assert canvas.settings.brush_presets[0] == original
        canvas.command_stack.undo()
        assert pixels(canvas.tiles,raster.object_id) == {}
        canvas.command_stack.redo()
        assert pixels(canvas.tiles,raster.object_id) == final
    finally:
        canvas.close()


def test_post_correction_disables_both_clocks_during_live_and_final_replay(qapp):
    second = BrushDefinition(size=8,density=.2,continuous=True,continuous_rate=19)
    brush = BrushDefinition(size=10,density=.2,continuous=True,continuous_rate=23,
                            post_correction=.8,dual=second,dual_mode="add")
    samples = [BrushInput(8,16,time=0),BrushInput(16,22,time=.3),
               BrushInput(24,16,time=.6),BrushInput(24,16,time=.9)]
    def rendered(definition):
        tiles = TileStore(32)
        stroke = RasterBrushStroke(tiles,"paint",definition,QColor("black"),{})
        stroke.begin(samples[0])
        for sample in samples[1:]:
            stroke.add(sample)
        live = pixels(tiles,"paint")
        stroke.finish()
        return live,pixels(tiles,"paint")
    assert rendered(brush) == rendered(replace(brush,continuous=False,
                                               dual=replace(second,continuous=False)))
    effective = resolved_taper(brush,30)
    assert not effective.continuous and not effective.dual.continuous
    assert brush.continuous and brush.dual.continuous


def test_taper_replay_keeps_secondary_clock_when_only_its_ignored_correction_is_set():
    child = BrushDefinition(continuous=True,post_correction=.8)
    brush = BrushDefinition(continuous=True,dual=child,taper_mode="percentage",taper_end=20)
    effective = resolved_taper(brush,50)
    assert effective.continuous and effective.dual.continuous
    assert effective.dual.post_correction == 0


def test_continuous_dialog_explains_precedence_and_preserves_dormant_choices(qapp):
    brush = BrushDefinition(continuous=True,continuous_rate=47,post_correction=.456789)
    dialog = BrushSettingsDialog(brush)
    try:
        assert not dialog.controls["continuous"].isEnabled()
        assert not dialog.controls["continuous_rate"].isEnabled()
        assert "Post-correction" in dialog.continuous_note.text()
        assert dialog.result_definition().to_dict() == brush.to_dict()
        dialog.controls["post_correction"].setValue(0)
        assert dialog.controls["continuous"].isEnabled()
        assert dialog.controls["continuous_rate"].isEnabled()
        assert dialog.controls["continuous"].isChecked()
        assert dialog.result_definition().continuous_rate == 47
        dialog.controls["ribbon"].setChecked(True)
        assert not dialog.controls["continuous"].isEnabled()
        assert "not yet supported" in dialog.continuous_note.text()
    finally:
        dialog.close()


def test_moving_continuous_brush_accumulates_more_raster_coverage_when_drawn_slowly(qapp):
    brush = BrushDefinition(size=12,spacing=.8,density=.05,continuous=True,continuous_rate=30)
    def coverage(duration):
        tiles = TileStore(32)
        stroke = RasterBrushStroke(tiles,"paint",brush,QColor("black"),{},defer_flush=True)
        stroke.begin(BrushInput(10,16))
        stroke.add(BrushInput(110,16,time=duration))
        stroke.finish()
        return sum(image.pixelColor(x,y).alpha()
                   for image in tiles.object_tiles("paint").values()
                   for y in range(image.height()) for x in range(image.width()))
    assert coverage(1) > 2*coverage(.1)


@pytest.mark.parametrize("post_correction",[0,.6])
def test_cooperative_preview_retains_continuous_gate_through_final_path_replay(qapp,post_correction):
    from comic_editor.core.brush_preview import render_brush_preview
    from test_brush_preview_queue import reference_preview
    brush = BrushDefinition(size=8,spacing=.7,continuous=True,continuous_rate=10,
        taper_mode="percentage",taper_end=30,post_correction=post_correction,
        dual=BrushDefinition(size=5,continuous=True,continuous_rate=17))
    expected = reference_preview(brush)
    actual = render_brush_preview(brush,100,44,color="red")
    assert bytes(actual.constBits()) == bytes(expected.constBits())
