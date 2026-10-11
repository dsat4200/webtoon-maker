"""A prior viewport preview cannot masquerade as native contact tiles."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor,QImage

from comic_editor.core.tools import ToolKind
from comic_editor.ui.document_presentation import PresentedTile
from test_filtered_raster_contact_feedback import (
    filtered_scene,begin,move,finish,wait_feedback,world_point,source_pixels,
    native_presentation,ready_paint,pixels,
)


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
def test_prior_viewport_preview_keeps_owned_contact_native_and_history_exact(
        filtered_scene,tool,wait_scene,qapp):
    canvas,obj = filtered_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    original = source_pixels(canvas,obj)
    history = canvas.command_stack.revision
    submitted = controller.scheduler.submitted
    document = canvas._render_document_state()
    marker = QImage(canvas.size(),QImage.Format_ARGB32_Premultiplied)
    marker.fill(QColor('cyan'))
    controller.preview = (replace(document,live_preview=True),PresentedTile(('preview',147),
        marker,canvas.visible_document_rect(),pixel_contract=document.pixel_contract))
    for index,local in enumerate((QPointF(137.,113.),QPointF(173.,244.))):
        point = world_point(canvas,obj,local)
        begin(canvas,point,tool) if index==0 else move(canvas,point,tool)
        actual = wait_feedback(canvas,qapp)
        assert controller._contact_reuse_gate is not None
        assert controller.scheduler.submitted == submitted
        np.testing.assert_array_equal(pixels(actual),pixels(native_presentation(canvas)))
    finish(canvas,tool)
    wait_scene(canvas)
    assert canvas.command_stack.revision == history+1
    changed = source_pixels(canvas,obj)
    assert changed != original
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == original
    canvas.command_stack.redo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == changed
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


@pytest.mark.parametrize('kind',['preview','overview','unknown'])
def test_unowned_non_native_batch_remains_with_ordinary_renderer(
        filtered_scene,kind,wait_scene,qapp):
    canvas,obj = filtered_scene
    wait_scene(canvas)
    begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),ToolKind.RASTER_PENCIL)
    wait_feedback(canvas,qapp)
    controller = canvas._scene_controller
    state = controller._filtered_state
    document = canvas._render_document_state()
    base = canvas._projection_completed_view
    assert state.covers(document,controller._filtered_requests,base)
    assert state.updates
    image = QImage(5,5,QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('cyan'))
    batch = [(None,[PresentedTile((kind,147),image,canvas.visible_document_rect())])]
    result,count = state.replace_batch(document,controller._filtered_requests,base,batch)
    assert result is batch and count == 0
    saved_gate = controller._contact_reuse_gate
    try:
        controller._contact_reuse_gate = None
        result,count = controller.filtered_batch(document,controller._filtered_requests,batch)
        assert result is batch and count == 0
    finally:
        controller._contact_reuse_gate = saved_gate
    finish(canvas,ToolKind.RASTER_PENCIL)
    wait_scene(canvas)
