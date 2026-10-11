"""Live ownership survives ready exact tiles retained through history/selection."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF

from comic_editor.core.tools import ToolKind
from test_neighbor_filtered_contact import neighboring_scene
from test_filtered_raster_contact_feedback import (
    begin, move, finish, wait_feedback, source_pixels, native_presentation,
    ready_paint, pixels,
)


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_undo_then_crossing_keeps_both_visible_native_regions_owned(
        neighboring_scene, tool, wait_scene, qapp):
    canvas, obj = neighboring_scene
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    wait_scene(canvas, timeout=60)
    controller = canvas._scene_controller
    initial = controller._filtered_state
    assert initial is not None and len(initial.prepared.recipes) == 2
    expected_blocks = {recipe.request.key for recipe in initial.prepared.recipes}
    original = source_pixels(canvas, obj)
    undo_count = len(canvas.command_stack._undo)
    begin(canvas, QPointF(520., 1080.), ToolKind.RASTER_PENCIL)
    move(canvas, QPointF(534., 1084.), ToolKind.RASTER_PENCIL)
    wait_feedback(canvas, qapp, timeout=30)
    finish(canvas, ToolKind.RASTER_PENCIL)
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) != original
    assert len(canvas.command_stack._undo) == undo_count + 1
    canvas.command_stack.undo()
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == original
    assert len(canvas.command_stack._undo) == undo_count
    canvas.set_tool(tool)
    wait_scene(canvas, timeout=60)
    state = controller._filtered_state
    assert state is initial, "Eligible source-only undo must keep its native owner"
    assert {recipe.request.key for recipe in state.prepared.recipes} == expected_blocks
    assert state.prepared.byte_count > 64 * 1024 * 1024
    submitted, revision = controller.scheduler.submitted, canvas.command_stack.revision
    for index, point in enumerate((QPointF(520., 1052.), QPointF(523., 1024.), QPointF(516., 996.))):
        begin(canvas, point, tool) if index == 0 else move(canvas, point, tool)
        actual = wait_feedback(canvas, qapp, timeout=30)
        assert controller._contact_reuse_gate is not None
        assert controller.scheduler.submitted == submitted
        np.testing.assert_array_equal(pixels(actual), pixels(native_presentation(canvas)))
    assert len(state.updates) == 2
    held = source_pixels(canvas, obj)
    finish(canvas, tool)
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == held and held != original
    assert canvas.command_stack.revision == revision + 1
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == original
    assert {recipe.request.key for recipe in controller._filtered_state.prepared.recipes} == expected_blocks
    canvas.command_stack.redo()
    wait_scene(canvas, timeout=60)
    assert source_pixels(canvas, obj) == held
    assert {recipe.request.key for recipe in controller._filtered_state.prepared.recipes} == expected_blocks


def test_reselection_prepares_complete_live_ownership_without_repainting_ready_tiles(
        neighboring_scene, wait_scene, monkeypatch):
    canvas, obj = neighboring_scene
    wait_scene(canvas, timeout=60)
    controller = canvas._scene_controller
    initial = controller._filtered_state
    assert initial is not None and len(initial.prepared.recipes) == 2
    expected_blocks = {recipe.request.key for recipe in initial.prepared.recipes}
    original = source_pixels(canvas, obj)
    history = canvas.command_stack.revision
    demands = []
    submit = controller.scheduler.submit
    def observe(demand):
        demands.append(demand)
        return submit(demand)
    monkeypatch.setattr(controller.scheduler, 'submit', observe)
    canvas.set_selection('layer', obj.parent_layer_id, activate_default_tool=False)
    wait_scene(canvas, timeout=60)
    assert controller.feedback is None
    canvas.set_selection('object', obj.object_id, activate_default_tool=False)
    wait_scene(canvas, timeout=60)
    prepared = controller._filtered_state
    assert prepared is not None
    assert {recipe.request.key for recipe in prepared.prepared.recipes} == expected_blocks
    target_demands = [demand for demand in demands if demand.feedback_target == obj.object_id]
    assert target_demands and all(not demand.requests for demand in target_demands)
    assert source_pixels(canvas, obj) == original and canvas.command_stack.revision == history
    np.testing.assert_array_equal(pixels(ready_paint(canvas)), pixels(native_presentation(canvas)))


@pytest.mark.parametrize('tool', [ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH])
def test_undo_does_not_present_an_older_inflight_ink_result(
        neighboring_scene, tool, wait_scene, qapp, monkeypatch):
    from threading import Event
    import time
    canvas, obj = neighboring_scene
    # Keep the dense native source and finite point effect. Expose the
    # selected leaf so this stroke must visibly change the native field.
    from comic_editor.core.models import OutlineModifier
    for identifier in tuple(obj.modifier_ids):
        if isinstance(canvas.chapter.modifiers[identifier], OutlineModifier):
            obj.modifier_ids.remove(identifier)
            del canvas.chapter.modifiers[identifier]
    from comic_editor.core.models import ChildRef
    canvas.chapter.layers[obj.parent_layer_id].children = [ChildRef('object', obj.object_id)]
    canvas.chapter.objects = {obj.object_id: obj}
    # Match the existing release framing rule before capture, so this is
    # a source-only restore and not a source-frame eligibility failure.
    obj.interaction_rect = (-24., -24., 1072., 2096.)
    canvas.set_document(canvas.chapter, canvas.tiles, canvas.images)
    canvas.center_x, canvas.center_y, canvas.scale = 512., 1024., 1.
    canvas.set_selection('object', obj.object_id)
    # Pending-contact rings/text are intentional editing UI. Compare only
    # artwork while the native restore and ordinary scene jobs are blocked.
    monkeypatch.setattr('comic_editor.ui.raster_feedback.present_pending_raster_gesture',
        lambda *_args: False)
    def actual_paint():
        from PySide6.QtGui import QImage, QPainter, QColor
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing)
        try:
            canvas._paint_ready_document_projection(painter,
                live_ink=canvas._show_on_top_live_ink())
        finally:
            painter.end()
        return image
    canvas.set_tool(tool)
    wait_scene(canvas, timeout=60)
    controller = canvas._scene_controller
    state = controller._filtered_state
    assert state is not None
    original_source = source_pixels(canvas, obj)
    original_frame = pixels(actual_paint())
    undo_count = len(canvas.command_stack._undo)
    first_entered, restore_entered = Event(), Event()
    release_first, release_restore = Event(), Event()
    evaluate = state.worker._evaluate
    calls = []
    def held(packet):
        calls.append(packet.revision)
        if len(calls) == 1:
            first_entered.set()
            assert release_first.wait(15.)
        elif len(calls) == 2:
            restore_entered.set()
            assert release_restore.wait(15.)
        return evaluate(packet)
    monkeypatch.setattr(state.worker, '_evaluate', held)
    scene_hold, release_scene = Event(), Event()
    scene_evaluate = controller.scheduler._evaluate
    def held_scene(demand, token):
        if scene_hold.is_set():
            assert release_scene.wait(15.)
        return scene_evaluate(demand, token)
    monkeypatch.setattr(controller.scheduler, '_evaluate', held_scene)
    try:
        begin(canvas, QPointF(520., 1080.), tool)
        move(canvas, QPointF(534., 1084.), tool)
        # Capture the latest committed source revision as the first held job.
        finish(canvas, tool)
        assert len(canvas.command_stack._undo) == undo_count + 1
        assert source_pixels(canvas, obj) != original_source
        assert not np.array_equal(pixels(native_presentation(canvas)), original_frame)
        scene_hold.set()
        deadline = time.monotonic() + 5.
        while not first_entered.is_set() and time.monotonic() < deadline:
            actual_paint()
            qapp.processEvents()
            time.sleep(.001)
        assert first_entered.is_set()
        scene_hold.set()
        canvas.command_stack.undo()
        assert source_pixels(canvas, obj) == original_source
        assert len(canvas.command_stack._undo) == undo_count
        assert controller._filtered_state is state and not state.worker.stopped.is_set()
        # Capture the restored source packet before letting old ink complete.
        actual_paint()
        qapp.processEvents()
        release_first.set()
        deadline = time.monotonic() + 5.
        while not restore_entered.is_set() and time.monotonic() < deadline:
            actual_paint()
            qapp.processEvents()
            time.sleep(.001)
        assert restore_entered.is_set(), (state.error,state.worker.error,calls,dict(state.generations),controller.error)
        # Old publication is allowed to finish its owner work, but it may not
        # resurrect the stroke while restored pixels are still being prepared.
        for _ in range(3):
            actual = pixels(actual_paint())
            qapp.processEvents()
            np.testing.assert_array_equal(actual, original_frame)
        # Resuming an owned contact admits the native basis even when the
        # ordinary view is a viewport preview. A pre-undo completion may not
        # then resurrect the old stroke while restore work remains blocked.
        begin(canvas, QPointF(390., 980.), tool)
        actual = pixels(actual_paint())
        qapp.processEvents()
        assert controller._contact_reuse_gate is not None
        np.testing.assert_array_equal(actual, original_frame)
        if tool == ToolKind.BRUSH:
            canvas._cancel_paint_brush()
        else:
            assert canvas._cancel_native_raster_stroke()
        assert source_pixels(canvas, obj) == original_source
        assert len(canvas.command_stack._undo) == undo_count
        release_restore.set()
        release_scene.set()
        wait_scene(canvas, timeout=60)
        assert source_pixels(canvas, obj) == original_source
        np.testing.assert_array_equal(pixels(actual_paint()), pixels(native_presentation(canvas)))
        canvas.command_stack.redo()
        wait_scene(canvas, timeout=60)
        assert source_pixels(canvas, obj) != original_source
        np.testing.assert_array_equal(pixels(actual_paint()), pixels(native_presentation(canvas)))
    finally:
        release_first.set()
        release_restore.set()
        release_scene.set()
