"""Completed work must not outrun the newest owned source waiting on the GUI."""
from threading import Event
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.tools import ToolKind
from test_filtered_raster_contact_feedback import (filtered_scene, begin, move,
    world_point, wait_feedback, native_presentation)
from test_raster_contact_feedback import pixels, ready_paint


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
def test_finished_job_waits_for_newest_native_packet_before_launching_pending(
        filtered_scene,tool,wait_scene,qapp,monkeypatch):
    canvas,obj = filtered_scene
    wait_scene(canvas)
    assert canvas.set_tool(tool)
    wait_scene(canvas)
    state = canvas._scene_controller._filtered_state
    worker = state.worker
    entered,release = Event(),Event()
    evaluated = []
    evaluate = worker._evaluate
    def held(packet):
        evaluated.append(packet.revision)
        if len(evaluated)==1:
            entered.set()
            assert release.wait(5.)
        return evaluate(packet)
    monkeypatch.setattr(worker,'_evaluate',held)
    try:
        begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),tool)
        ready_paint(canvas)
        deadline = time.monotonic()+2.
        while not entered.is_set() and time.monotonic()<deadline:
            qapp.processEvents()
            time.sleep(.001)
        assert entered.is_set()
        first = evaluated[0]
        for point in (QPointF(173.,144.),QPointF(183.,155.)):
            move(canvas,world_point(canvas,obj,point),tool)
            assert state.advance(canvas._render_document_state())
        assert worker.pending is not None
        older_pending = worker.pending.revision
        release.set()
        deadline = time.monotonic()+2.
        while not worker.future.done() and time.monotonic()<deadline:
            time.sleep(.001)  # Keep the completion wake queued on Qt.
        assert worker.future.done()
        move(canvas,world_point(canvas,obj,QPointF(194.,166.)),tool)
        newest = canvas._document_projection.revision
        assert newest>older_pending and state.generations
        # A timer/completion poll publishes ready work but must not launch the
        # older source while a newer, uncaptured native generation is waiting.
        assert state.advance(canvas._render_document_state(),capture=False)
        assert worker.future is None and worker.pending is not None
        assert state.advance(canvas._render_document_state())
        assert not state.generations
        wait_feedback(canvas,qapp)
        assert evaluated==[first,newest]
        np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    finally:
        release.set()
        canvas._cancel_paint_brush() if tool==ToolKind.BRUSH else canvas._cancel_native_raster_stroke()
