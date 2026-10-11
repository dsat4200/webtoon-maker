"""Held filtered contacts use the actual controller and normal presentation."""
from dataclasses import replace
from threading import Event
import time

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tools import ToolKind
from comic_editor.render.outputs import capture_document
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, TileBatchPolicy
from comic_editor.render.filtered_raster_feedback import FilteredRasterFeedback
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.document_presentation import PresentedTile, draw_document_tiles, draw_document_border
from test_filtered_native_recipe import fixture as native_fixture, native, packet as native_packet
from test_raster_contact_feedback import ready_paint,pixels


@pytest.fixture
def filtered_scene(qapp,monkeypatch,request):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda *_:None)
    format,transparent,rotate = getattr(request,'param',(QImage.Format_ARGB32_Premultiplied,True,True))
    chapter,tiles,images,obj,_baseline,_request,_recipe = native_fixture(format,transparent,rotate)
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster',predictive_ink=False,
        grid_overlay_visible=False,snap_to_grid=False))
    canvas.setFixedSize(512,512)
    canvas.set_document(chapter,tiles,images)
    canvas.center_x = canvas.center_y = 256.
    canvas.scale = 1.
    canvas.primary_color = '#ffffff00'
    canvas.settings.pencil_size_px = dict.fromkeys(('small','medium','large'),16)
    canvas.settings.eraser_size_px = dict.fromkeys(('small','medium','large'),16)
    canvas.set_selection('object',obj.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    yield canvas,obj
    worker = None if canvas._scene_controller._filtered_state is None else canvas._scene_controller._filtered_state.worker
    canvas._scene_controller.reset()
    if worker is not None:
        worker.executor.shutdown(wait=True)
    canvas._scene_controller.scheduler.close()
    canvas._scene_controller.scheduler.executor.shutdown(wait=True,cancel_futures=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    canvas.deleteLater()


def source_pixels(canvas,obj):
    return {key:native(image) for key,image in canvas.tiles.object_tiles(obj.object_id).items()}


def world_point(canvas,obj,point):
    transform = (QTransform.fromTranslate(obj.x,obj.y)*canvas._drawing_object_transform(obj)
        *canvas.layer_world_transform(obj.parent_layer_id))
    return transform.map(point)


def begin(canvas,point,tool):
    if tool == ToolKind.BRUSH:
        canvas._begin_paint_brush(point,1.,_definition=BrushDefinition(size=16,antialiasing=0,spacing=.08))
    else:
        canvas._begin_stroke(point,1.)


def move(canvas,point,tool):
    if tool == ToolKind.BRUSH:
        canvas._continue_paint_brush(point,1.)
    else:
        canvas._continue_stroke(point,1.)


def finish(canvas,tool):
    canvas._finish_paint_brush() if tool == ToolKind.BRUSH else canvas._end_stroke()


def wait_feedback(canvas,qapp,timeout=5.):
    deadline = time.monotonic()+timeout
    while time.monotonic()<deadline:
        actual = ready_paint(canvas)
        qapp.processEvents()
        state = canvas._scene_controller._filtered_state
        assert state is not None,'Filtered feedback lost its prepared ownership'
        assert not state.error,state.error
        if (state.completed_revision == canvas._document_projection.revision
                and not state.generations and not state.worker.busy):
            actual = ready_paint(canvas)
            assert canvas._raster_feedback_contact_covered
            return actual
        time.sleep(.001)
    pytest.fail('Current filtered native contact did not arrive')


def native_presentation(canvas):
    snapshot = capture_document(canvas.chapter,canvas.tiles,canvas.images)
    requests = list(canvas._scene_controller._filtered_requests)
    backend = DetachedSceneBackend(snapshot)
    service = DocumentRenderService(backend)
    service.projection.revision = snapshot.document.revision
    try:
        center = canvas.visible_document_rect().center()
        result = service.render_tiles(snapshot.document,requests,TileBatchPolicy(center.toTuple()))
        assert not result.error and all(exact for _image,exact in result.tiles.values())
        batch = [PresentedTile((None,request.address),result.tiles[request.address][0],
            request.world_rect,request.source_rect,snapshot.document.pixel_contract,snapshot.pixel_environment)
            for request in requests]
        image = QImage(canvas.size(),QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing,True)
        try:
            draw_document_tiles(painter,batch,canvas.camera_transform(),canvas.size(),owner=canvas)
            draw_document_border(painter,snapshot.document.bounds,canvas.camera_transform(),canvas.size(),owner=canvas)
        finally:
            painter.end()
        return image
    finally:
        backend.close()


@pytest.mark.parametrize('filtered_scene',[(format,transparent,rotate)
    for format in (QImage.Format_ARGB32_Premultiplied,QImage.Format_RGBA64_Premultiplied)
    for transparent in (False,True) for rotate in (False,True)],indirect=True)
@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
def test_held_filtered_tools_replace_native_tiles_and_release_undo_redo_exactly(
        filtered_scene,tool,wait_scene,qapp):
    canvas,obj = filtered_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    assert isinstance(controller.feedback,FilteredRasterFeedback)
    assert controller._filtered_state is not None
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))

    original = source_pixels(canvas,obj)
    history = canvas.command_stack.revision
    before = pixels(ready_paint(canvas))
    submitted = controller.scheduler.submitted
    for index,local in enumerate((QPointF(137.,113.),QPointF(173.,244.),QPointF(253.,128.))):
        point = world_point(canvas,obj,local)
        begin(canvas,point,tool) if index==0 else move(canvas,point,tool)
        actual = wait_feedback(canvas,qapp)
        assert controller.scheduler.submitted == submitted
        assert canvas._projection_provisional_visible
        np.testing.assert_array_equal(pixels(actual),pixels(native_presentation(canvas)))
        assert not np.array_equal(before,pixels(actual))
        before = pixels(actual)
    finish(canvas,tool)
    wait_scene(canvas)
    assert canvas.command_stack.revision == history+1
    assert not canvas._projection_provisional_visible
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    changed = source_pixels(canvas,obj)
    assert changed != original
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj)==original
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    canvas.command_stack.redo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj)==changed
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
def test_cancel_restores_filtered_source_and_exact_presentation(filtered_scene,tool,wait_scene,qapp):
    canvas,obj = filtered_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    original = source_pixels(canvas,obj)
    before = pixels(ready_paint(canvas))
    history = canvas.command_stack.revision
    begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),tool)
    assert not np.array_equal(before,pixels(wait_feedback(canvas,qapp)))
    if tool == ToolKind.BRUSH:
        canvas._cancel_paint_brush()
    else:
        assert canvas._cancel_native_raster_stroke()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == original
    assert canvas.command_stack.revision == history
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),before)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


@pytest.mark.parametrize('reason',['reset','identity','configuration','target'])
def test_queued_filtered_worker_retires_before_a_new_owner_can_capture_sources(
        filtered_scene,reason,wait_scene,qapp,monkeypatch):
    canvas,obj = filtered_scene
    wait_scene(canvas)
    controller = canvas._scene_controller
    state = controller._filtered_state
    worker = state.worker
    entered,release = Event(),Event()
    evaluate = worker._evaluate
    def held(packet):
        entered.set()
        assert release.wait(5.)
        return evaluate(packet)
    monkeypatch.setattr(worker,'_evaluate',held)
    try:
        begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),ToolKind.RASTER_PENCIL)
        ready_paint(canvas)
        deadline = time.monotonic()+1.
        while not entered.is_set() and time.monotonic()<deadline:
            qapp.processEvents()
            time.sleep(.001)
        assert entered.is_set()
        document = canvas._render_document_state()
        requests = controller._filtered_requests
        visible = canvas.visible_document_rect()
        if reason == 'reset':
            controller.reset()
        else:
            if reason == 'identity':
                document = replace(document,identity=('replacement-document',))
            elif reason == 'configuration':
                document = replace(document,configuration=('replacement-display',))
            else:
                canvas.selected_id = next(identifier for identifier in canvas.chapter.objects
                    if identifier != obj.object_id)
            # Retirement must precede polling or capturing a changed source.
            monkeypatch.setattr(state,'advance',lambda *_a,**_k:
                pytest.fail('Retired feedback was allowed to capture sources'))
            controller.request(document,requests,(None,),visible)
        assert controller._filtered_state is None
        assert controller._filtered_lifetime['state'] is None
        assert worker.stopped.is_set()
    finally:
        release.set()
        worker.close()
        worker.executor.shutdown(wait=True)
        canvas._cancel_native_raster_stroke()
    assert worker.owner is None and worker.backend is None and not worker.progress
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


@pytest.mark.parametrize('invalid',['no_base','old_base','missing_tile','coarse_tile','small_image','phase'])
def test_filtered_coverage_requires_the_complete_native_previous_view(
        filtered_scene,invalid,wait_scene):
    canvas,_obj = filtered_scene
    wait_scene(canvas)
    controller = canvas._scene_controller
    state = controller._filtered_state
    document = canvas._render_document_state()
    requests = controller._filtered_requests
    base = canvas._projection_completed_view
    assert state.covers(document,requests,base)
    if invalid == 'no_base':
        base = None
    elif invalid == 'old_base':
        base = (base[0],base[1],state.prepared.document.revision-1)
    else:
        tiles = list(base[1][0][1])
        if invalid == 'missing_tile':
            tiles.pop()
        elif invalid == 'coarse_tile':
            address = ProjectionAddress(1,0,0)
            tiles[0] = replace(tiles[0],key=(None,address))
        elif invalid == 'small_image':
            tiles[0] = replace(tiles[0],image=tiles[0].image.copy(0,0,16,16))
        base = (base[0],[('base' if invalid == 'phase' else None,tiles)],base[2])
    assert not state.covers(document,requests,base)


def test_filtered_coverage_checks_dirty_tile_gutters_outside_the_contact_region(filtered_scene,wait_scene):
    canvas,_obj = filtered_scene
    wait_scene(canvas)
    state = canvas._scene_controller._filtered_state
    requests = canvas._scene_controller._filtered_requests
    left = min(requests,key=lambda request:request.address.x+request.address.y*100)
    recipe = state.prepared.recipes[0]
    restricted = replace(recipe,request=replace(recipe.request,
        requested_region=tuple(left.capture_rect.getRect())))
    state.prepared = replace(state.prepared,recipes=(restricted,))
    state.dirty = QRectF(253.,100.,6.,10.)
    # The pointer is in the prepared left tile, while its finite influence
    # also touches the next tile's native gutter. Holding that base is unsafe.
    assert restricted.request.requested.contains(QPointF(254.,105.))
    assert not state.covers(canvas._render_document_state(),requests,canvas._projection_completed_view)


@pytest.mark.parametrize('scale,rotation',[(.45,0.),(1.37,17.),(3.25,-29.)])
def test_camera_changes_present_held_filtered_pixels_on_the_native_grid(
        filtered_scene,scale,rotation,wait_scene,qapp):
    canvas,obj = filtered_scene
    wait_scene(canvas)
    begin(canvas,world_point(canvas,obj,QPointF(137.,113.)),ToolKind.RASTER_PENCIL)
    wait_feedback(canvas,qapp)
    canvas.scale,canvas.rotation = scale,rotation
    actual = wait_feedback(canvas,qapp)
    np.testing.assert_array_equal(pixels(actual),pixels(native_presentation(canvas)))
    finish(canvas,ToolKind.RASTER_PENCIL)
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


def test_erasing_the_last_filtered_source_retires_feedback_and_converges_exactly(filtered_scene,wait_scene,qapp):
    canvas,obj = filtered_scene
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas.settings.eraser_size_px = dict.fromkeys(('small','medium','large'),1600)
    wait_scene(canvas)
    original = source_pixels(canvas,obj)
    worker = canvas._scene_controller._filtered_state.worker
    begin(canvas,world_point(canvas,obj,QPointF(128.,128.)),ToolKind.RASTER_ERASER)
    ready_paint(canvas)
    finish(canvas,ToolKind.RASTER_ERASER)
    wait_scene(canvas)
    assert not canvas.tiles.object_tiles(obj.object_id)
    assert worker.stopped.is_set()
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == original
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.RASTER_ERASER,ToolKind.BRUSH])
@pytest.mark.parametrize('reason',['gutter','crop_budget'])
def test_live_filter_coverage_or_crop_failure_resumes_normal_work_and_preserves_history(
        filtered_scene,tool,reason,wait_scene,qapp):
    canvas,obj = filtered_scene
    canvas.set_tool(tool)
    wait_scene(canvas)
    controller = canvas._scene_controller
    state = controller._filtered_state
    before = source_pixels(canvas,obj)
    history = canvas.command_stack.revision
    submitted = controller.scheduler.submitted
    if reason == 'gutter':
        if tool == ToolKind.RASTER_ERASER:
            canvas.settings.eraser_size_px = dict.fromkeys(('small','medium','large'),64)
        left = min(controller._filtered_requests,key=lambda request:request.address.x+request.address.y*100)
        recipe = state.prepared.recipes[0]
        state.prepared = replace(state.prepared,recipes=(replace(recipe,request=replace(recipe.request,
            requested_region=tuple(left.capture_rect.getRect()))),))
        point = world_point(canvas,obj,QPointF(253.,113.))
    else:
        state.crop_bytes = 32*1024*1024
        point = world_point(canvas,obj,QPointF(137.,113.))
    begin(canvas,point,tool)
    # Exercise the production batch and its ordinary fallback while held.
    wait_scene(canvas)
    assert controller.scheduler.submitted > submitted
    assert controller._contact_reuse_gate is None
    assert not canvas._raster_feedback_contact_covered
    finish(canvas,tool)
    wait_scene(canvas)
    assert canvas.command_stack.revision == history+1
    changed = source_pixels(canvas,obj)
    assert changed != before
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    canvas.command_stack.undo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == before
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))
    canvas.command_stack.redo()
    wait_scene(canvas)
    assert source_pixels(canvas,obj) == changed
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_presentation(canvas)))


def test_destroying_qt_consumer_retires_an_active_filtered_worker_without_waiting(
        filtered_scene,wait_scene,qapp,monkeypatch):
    from comic_editor.ui.scene_controller import SceneController
    from comic_editor.ui.filtered_raster_feedback import FilteredRasterFeedbackState
    canvas,obj = filtered_scene
    wait_scene(canvas)
    consumer = SceneController(canvas)
    state = FilteredRasterFeedbackState(canvas,canvas._scene_controller.feedback,lambda:None)
    consumer._filtered_state = state
    consumer._filtered_lifetime['state'] = state
    worker = state.worker
    scheduler = consumer.scheduler
    entered,release = Event(),Event()
    evaluate = worker._evaluate
    def held(packet):
        entered.set()
        assert release.wait(5.)
        return evaluate(packet)
    monkeypatch.setattr(worker,'_evaluate',held)
    try:
        canvas.tiles.paint_dab(obj.object_id,QPointF(137.,113.),16.,QColor('#ffffff00'))
        assert worker.submit(native_packet(canvas.tiles,obj,1,1))
        deadline = time.monotonic()+2.
        while not entered.is_set() and time.monotonic()<deadline:
            worker.poll()
            time.sleep(.001)
        assert entered.is_set()
        worker.notify = consumer.filteredFinished.emit
        consumer.deleteLater()
        QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete)
        assert worker.stopped.is_set() and worker.pending is None
        assert not release.is_set()  # Destruction did not wait for evaluation.
    finally:
        release.set()
        worker.close()
        worker.executor.shutdown(wait=True)
        scheduler.close()
        scheduler.executor.shutdown(wait=True,cancel_futures=True)
    qapp.processEvents()
    assert worker.owner is None and worker.backend is None and not worker.progress
