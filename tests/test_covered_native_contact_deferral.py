"""Covered native feedback pauses scene work only while its owner stays valid."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.tools import ToolKind
from test_raster_contact_feedback import scene, pixels, ready_paint
from test_raster_contact_feedback_boundaries import (
    begin, finish, block_existing_scene_demand, native_decorated,
)


def trace_scene_work(canvas, monkeypatch):
    controller = canvas._scene_controller
    calls = {'capture': [], 'submit': [], 'cancel': []}
    original_capture = canvas._scene_snapshot_compiler.capture
    original_submit = controller.scheduler.submit
    original_cancel = controller.scheduler.cancel
    def capture(*args, **kwargs):
        calls['capture'].append(args)
        return original_capture(*args, **kwargs)
    def submit(demand):
        calls['submit'].append(demand)
        return original_submit(demand)
    def cancel():
        calls['cancel'].append(controller.scheduler.busy)
        return original_cancel()
    monkeypatch.setattr(canvas._scene_snapshot_compiler, 'capture', capture)
    monkeypatch.setattr(controller.scheduler, 'submit', submit)
    monkeypatch.setattr(controller.scheduler, 'cancel', cancel)
    return calls


def move(canvas, tool, point):
    (canvas._continue_paint_brush if tool == ToolKind.BRUSH else canvas._continue_stroke)(point, 1.)


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
@pytest.mark.parametrize('solo', [False, True])
def test_held_covered_native_contact_has_no_capture_or_submit_then_release_is_exact(
        scene, tool, solo, wait_scene, monkeypatch, qapp):
    canvas, selected, _front = scene
    canvas.chapter.height = 70000
    if solo:
        canvas.set_solo_entities({('object', selected.object_id)})
    if tool == ToolKind.RASTER_ERASER:
        image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('red'))
        canvas.tiles.set_tile(selected.object_id, (0, 0), image)
    canvas.set_tool(tool)
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    controller = canvas._scene_controller
    prepared, snapshot, preview = controller.feedback, controller.snapshot, controller.preview
    backdrop = getattr(canvas, '_projection_completed_view', None)
    assert prepared is not None and not snapshot.document.live_preview
    history = canvas.command_stack.revision
    blocker = block_existing_scene_demand(canvas, monkeypatch)
    calls = trace_scene_work(canvas, monkeypatch)
    try:
        begin(canvas, tool, QPointF(32, 64))
        for index, x in enumerate((32, 80, 104)):
            if index:
                move(canvas, tool, QPointF(x, 64))
            actual = ready_paint(canvas)
            qapp.processEvents()
            actual = ready_paint(canvas)
            controller.advance()
            document = canvas._render_document_state()
            gate = getattr(canvas, '_paint_brush_tile_input' if tool == ToolKind.BRUSH
                           else '_raster_tile_input')
            assert document.live_preview and canvas._drawing and canvas._raster_contact_active
            assert gate.current() and not gate.closed and not gate.released and gate.error is None
            assert controller._contact_reuse_gate is gate
            assert controller.feedback is prepared and controller.snapshot is snapshot
            assert controller.preview is preview
            assert getattr(canvas, '_projection_completed_view', None) is backdrop
            assert controller.capture is None and controller.dispatched is None
            assert not controller.timer.isActive()
            assert not calls['capture'] and not calls['submit'] and calls['cancel'] == [True]
            assert controller.scheduler.busy and controller.scheduler.cancelled.is_set()
            expected = native_decorated(canvas)
            np.testing.assert_array_equal(pixels(actual), pixels(expected))
            expected_color = (QColor('#242428') if solo else QColor('blue')) if tool == ToolKind.RASTER_ERASER else QColor('red')
            assert actual.pixelColor(x, 64) == expected_color
        assert canvas.command_stack.revision == history
        finish(canvas, tool)
        blocker.set()
        wait_scene(canvas)
        document = canvas._render_document_state()
        assert not document.live_preview and not canvas._drawing
        assert controller._contact_reuse_gate is None
        assert calls['capture'] and calls['submit']
        assert controller.snapshot.document == document and not controller.snapshot.document.live_preview
        assert not canvas._projection_frame_pending and not controller.scheduler.busy
        np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_decorated(canvas)))
        assert canvas.command_stack.revision == history + 1
    finally:
        blocker.set()
        if canvas._drawing:
            finish(canvas, tool)


@pytest.mark.parametrize('transition', ['release', 'view', 'configuration', 'unrelated', 'new_owner', 'other_preview'])
def test_contact_eligibility_exit_immediately_resumes_ordinary_scene_capture(
        scene, transition, wait_scene, monkeypatch):
    canvas, selected, front = scene
    canvas.chapter.width, canvas.chapter.height = 768, 70000
    canvas.set_solo_entities({('object', selected.object_id)})
    canvas._invalidate_scene_cache()
    wait_scene(canvas)
    controller = canvas._scene_controller
    blocker = block_existing_scene_demand(canvas, monkeypatch)
    calls = trace_scene_work(canvas, monkeypatch)
    original_object = selected
    try:
        begin(canvas, ToolKind.RASTER_PENCIL, QPointF(32, 64))
        ready_paint(canvas)
        gate = canvas._raster_tile_input
        assert controller._contact_reuse_gate is gate and not calls['capture'] and not calls['submit']
        if transition == 'release':
            finish(canvas, ToolKind.RASTER_PENCIL)
        elif transition == 'view':
            canvas.center_x = 640.
            visible = tuple(canvas.visible_document_rect().getRect())
            assert not controller._feedback_covers(canvas._render_document_state(), visible)
        elif transition == 'configuration':
            canvas.chapter.background = '#191929'
            canvas._publish_change_set(ChangeSet(document_fields=frozenset({'background'})))
        elif transition == 'unrelated':
            front.opacity = .5
            canvas._publish_change_set(ChangeSet((EntityChange(('object', front.object_id), frozenset({'opacity'})),)))
            assert controller.feedback is None
        elif transition == 'new_owner':
            canvas.chapter.objects[selected.object_id] = copy.deepcopy(selected)
            assert not gate.current()
        else:
            canvas._transform_preview_quad = [(1., 1.), (127., 1.), (127., 127.), (1., 127.)]
            assert canvas._projection_has_live_preview(include_ink=False)
        ready_paint(canvas)
        assert controller._contact_reuse_gate is None
        assert calls['capture'], 'The ordinary compiler must really be invoked after eligibility ends'
        assert controller.capture is not None
        assert controller.dispatched is None and controller.timer.isActive()
    finally:
        blocker.set()
        canvas.chapter.objects[selected.object_id] = original_object
        canvas._transform_preview_quad = None
        if canvas._drawing:
            finish(canvas, ToolKind.RASTER_PENCIL)
