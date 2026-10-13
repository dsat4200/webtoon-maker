"""Actual dated artwork progresses under continuous geometry input."""
from dataclasses import replace
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.changes import ChangeSet, EntityChange
from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, OutlineModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.tools import ToolKind
from comic_editor.render.geometry_capture import GeometrySceneSnapshot
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService, RenderRequest, RenderQuality, RenderStatus
from comic_editor.ui.canvas import RasterCanvasWidget


def pixels(image):
    return bytes(image.constBits())


def pump(canvas, qapp):
    surface = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    canvas.render(surface)
    qapp.processEvents()
    time.sleep(.002)
    return surface


def wait(canvas, qapp, condition):
    deadline = time.monotonic()+10
    while time.monotonic()<deadline:
        surface = pump(canvas,qapp)
        assert not canvas._scene_controller.error,canvas._scene_controller.error
        if condition():
            return surface
    pytest.fail('No expected complete artwork/proven owner')


def exact(canvas,qapp):
    return wait(canvas,qapp,lambda: not canvas._projection_frame_pending
        and canvas._scene_controller.capture is None
        and not canvas._scene_controller.scheduler.busy)


def native_reference(canvas):
    document = canvas._render_document_state()
    assert not document.live_preview
    capture = canvas._scene_snapshot_compiler.capture(canvas,document)
    while not capture.advance(.004):
        pass
    backend = DetachedSceneBackend(replace(capture.result,cache_spec=None))
    backend.artwork_scale=1.
    service=DocumentRenderService(backend)
    service.projection.revision=document.revision
    try:
        result=service.render_region(document,RenderRequest((0.,0.,256.,256.),1.,(256,256),
            ('independent-native',),document.revision,quality=RenderQuality.EXACT))
        assert result.exact and result.status is RenderStatus.EXACT
        return QImage(result.image)
    finally:
        backend.close()


@pytest.fixture
def scene(qapp,monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda _:None)
    canvas=RasterCanvasWidget(EditorSettings(canvas_renderer='raster',snap_to_grid=False,
        grid_overlay_visible=False,predictive_ink=False))
    canvas.setUpdatesEnabled(False)
    canvas.setFixedSize(256,256)
    chapter=ChapterDocument(width=256,height=256,document_kind='asset',background='#00000000')
    page=chapter.add_page('Page',BoundGeometry.rectangle(0.,0.,256.,256.))
    page.fill_color,page.border_width=None,0
    shape=chapter.add_layer(page.layer_id,'Many children',bound=BoundGeometry.rectangle(32.,32.,150.,150.))
    shape.fill_color,shape.border_color,shape.border_width='#ffdd8844','#fffefefe',2.
    tiles=TileStore()
    for index in range(16):
        obj=chapter.add_object(shape.layer_id,RasterObject(x=45.+20*(index%4),y=45.+20*(index//4),
            interaction_rect=(0.,0.,16.,16.)))
        image=QImage(256,256,QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('transparent'))
        painter=QPainter(image)
        painter.fillRect(0,0,16,16,QColor('#ff44aaee'))
        painter.end()
        tiles.set_tile(obj.object_id,(0,0),image)
        chapter.add_modifier(OutlineModifier(thickness=1.),[('object',obj.object_id)])
    canvas.set_document(chapter,tiles,ImageStore())
    canvas.center_x=canvas.center_y=128.
    canvas.scale,canvas.rotation=1.,0.
    canvas.set_selection('layer',shape.layer_id,activate_default_tool=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._draw_selection=lambda _:None
    exact(canvas,qapp)
    yield canvas,shape
    canvas._scene_controller.reset()
    for scheduler in (canvas._scene_controller.scheduler,canvas._scene_controller.bootstrap_scheduler):
        scheduler.close()
        scheduler.executor.shutdown(wait=True)
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    for timer in canvas.findChildren(QTimer):
        timer.stop()
    canvas.close()
    canvas.deleteLater()


def begin(canvas,kind):
    if kind=='boundary':
        point=QPointF(182.,182.)
        canvas._tool_press(canvas.document_to_widget(point),1.)
        assert canvas._active_shape_control is not None
        move=lambda n:canvas._tool_move(canvas.document_to_widget(point+QPointF(22*n,12*n)),1.)
    else:
        canvas.set_tool(ToolKind.TRANSFORM)
        point=QPointF(72.,84.)
        canvas._tool_press(canvas.document_to_widget(point),1.)
        assert canvas._geometry_transform_target
        assert canvas._transform_drag_mode=='translate'
        move=lambda n:canvas._tool_move(canvas.document_to_widget(point+QPointF(22*n,12*n)),1.)
    move(1.)
    return move


@pytest.mark.parametrize('kind',['boundary','translate'])
def test_continuous_edit_presents_real_older_pixels_before_latest_worker_finishes(scene,qapp,monkeypatch,kind):
    canvas,shape=scene
    gui=get_ident()
    ready=[Event(),Event()]
    releases=[Event(),Event()]
    observed=[]
    ordinary=DocumentRenderService.render_region
    def render(service,document,request,*args,**kwargs):
        result=ordinary(service,document,request,*args,**kwargs)
        if isinstance(service.backend.snapshot,GeometrySceneSnapshot) and len(observed)<2:
            assert get_ident()!=gui and result.status is RenderStatus.PROVISIONAL and not result.exact
            assert service.backend.snapshot.cache_spec is None
            index=len(observed)
            observed.append((document,QImage(result.image),service.backend.snapshot))
            ready[index].set()
            assert releases[index].wait(10)
        return result
    monkeypatch.setattr(DocumentRenderService,'render_region',render)
    initial=native_reference(canvas)
    try:
        move=begin(canvas,kind)
        wait(canvas,qapp,ready[0].is_set)
        controller=canvas._scene_controller
        owner=controller._geometry_preview_owner
        active=controller.scheduler.active_demand
        token=controller.scheduler.cancelled
        move(2.)
        # The timer can arrive between input and its queued paint. It must
        # dispatch the newest owned pose without waiting for another paint.
        assert controller.desired[0].revision < canvas._document_projection.revision
        controller.advance()
        assert controller.scheduler.pending is not None
        assert (controller.scheduler.pending.snapshot.document.revision
            == canvas._document_projection.revision)
        wait(canvas,qapp,lambda:controller.scheduler.pending is not None)
        assert controller._geometry_preview_owner==owner
        assert controller.scheduler.active_demand is active and not token.is_set()
        assert controller.scheduler.pending is not None
        first=observed[0][0]
        assert first.revision<canvas._document_projection.revision
        releases[0].set()
        wait(canvas,qapp,lambda:ready[1].is_set() and controller.preview is not None
            and controller.preview[0]==first)
        shown=controller.preview[1].image
        assert pixels(shown)==pixels(observed[0][1])
        assert pixels(shown)!=pixels(initial)
        assert canvas._projection_provisional_visible and canvas._projection_frame_pending
        assert canvas._document_projection.revision>first.revision
        # A further move keeps ownership while the next actual immutable
        # worker is held. It cannot relabel old pixels or enter exact storage.
        move(3.)
        newest = canvas._document_projection.revision
        wait(canvas,qapp,lambda: controller.scheduler.pending is not None
            and controller.scheduler.pending.snapshot.document.revision == newest)
        assert controller.preview[0]==first and controller._geometry_preview_owner==owner
        pending = controller.scheduler.pending
        assert pending.snapshot.capture_lease.lease.scope is pending.interaction_preview_owner
        releases[1].set()
        canvas._tool_release()
        exact(canvas,qapp)
        assert controller._geometry_preview_owner is None
        settled=native_reference(canvas)
        assert pixels(settled)!=pixels(initial)
        assert canvas.command_stack.can_undo
        canvas.command_stack.undo()
        exact(canvas,qapp)
        assert pixels(native_reference(canvas))==pixels(initial)
        canvas.command_stack.redo()
        exact(canvas,qapp)
        assert pixels(native_reference(canvas))==pixels(settled)
    finally:
        for event in releases:
            event.set()


@pytest.mark.parametrize('retire',['view','selection','tool','history','foreign','source'])
def test_geometry_owner_retires_and_rejects_prior_epoch(scene,qapp,retire):
    canvas,shape=scene
    begin(canvas,'boundary')
    controller=canvas._scene_controller
    wait(canvas,qapp,lambda:controller.preview is not None and controller._geometry_preview_owner is not None)
    snapshot=controller._preview_snapshot
    document,addresses,phases,visible=controller.desired
    assert controller._current_geometry_snapshot(snapshot,document,addresses,phases,visible)
    if retire=='view':
        canvas.center_x+=8
    elif retire=='selection':
        canvas.set_selection('layer',canvas.chapter.root_page_ids[0])
    elif retire=='tool':
        canvas.set_tool(ToolKind.OBJECT_SELECT)
    elif retire=='history':
        canvas.command_stack.clear()
    elif retire=='source':
        identifier=next(iter(canvas.chapter.objects))
        canvas.tiles.tile(identifier,(0,0)).fill(QColor('red'))
    else:
        page=canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
        page.opacity=.6
        canvas._publish_change_set(ChangeSet((EntityChange(('layer',page.layer_id),frozenset({'opacity'})),),
            transient=True,label='Other edit'),action='transient')
    current=canvas._render_document_state()
    assert not controller._current_geometry_snapshot(snapshot,current,addresses,phases,visible)


@pytest.mark.parametrize('kind',['boundary','translate'])
def test_release_keeps_actual_dated_artwork_until_current_native_is_presented(scene,qapp,monkeypatch,kind):
    from comic_editor.render.scheduler import SceneScheduler
    canvas,shape=scene
    canvas.setUpdatesEnabled(True)
    canvas.show()
    exact(canvas,qapp)
    controller=canvas._scene_controller
    move=begin(canvas,kind)
    wait(canvas,qapp,lambda:controller.geometry_continuity.last is not None)
    shown=controller.geometry_continuity.last.preview
    before=pump(canvas,qapp)
    older=shown[0]
    assert older.live_preview
    held,release=Event(),Event()
    original=SceneScheduler._evaluate
    def evaluate(scheduler,demand,token):
        if scheduler is controller.scheduler and not demand.snapshot.document.live_preview:
            held.set()
            assert release.wait(10)
        return original(scheduler,demand,token)
    monkeypatch.setattr(SceneScheduler,'_evaluate',evaluate)
    try:
        canvas._tool_release()
        wait(canvas,qapp,held.is_set)
        ticket=controller.geometry_continuity.ticket
        assert ticket is not None and ticket.painted.preview is shown
        assert ticket.document!=older and not ticket.document.live_preview
        assert controller._geometry_preview_owner is None
        pending=pump(canvas,qapp)
        assert pixels(before)==pixels(pending)
        assert canvas._projection_provisional_visible and canvas._projection_frame_pending
        assert controller.geometry_continuity.for_request(controller.desired) is shown
        assert controller.preview is None
        release.set()
        exact(canvas,qapp)
        assert controller.geometry_continuity.ticket is None
        settled=native_reference(canvas)
        canvas.command_stack.undo()
        exact(canvas,qapp)
        assert pixels(native_reference(canvas))!=pixels(settled)
    finally:
        release.set()


@pytest.mark.parametrize('retire',['view','selection','tool','history','foreign','source'])
def test_pending_geometry_commit_cannot_present_after_context_changes(scene,qapp,monkeypatch,retire):
    from comic_editor.render.scheduler import SceneScheduler
    canvas,shape=scene
    canvas.setUpdatesEnabled(True)
    canvas.show()
    exact(canvas,qapp)
    controller=canvas._scene_controller
    begin(canvas,'boundary')
    wait(canvas,qapp,lambda:controller.geometry_continuity.last is not None)
    release=Event()
    original=SceneScheduler._evaluate
    def evaluate(scheduler,demand,token):
        if scheduler is controller.scheduler and not demand.snapshot.document.live_preview:
            assert release.wait(10)
        return original(scheduler,demand,token)
    monkeypatch.setattr(SceneScheduler,'_evaluate',evaluate)
    try:
        canvas._tool_release()
        pump(canvas,qapp)
        assert controller.geometry_continuity.ticket is not None
        if retire=='view':
            canvas.center_x+=8
        elif retire=='selection':
            canvas.set_selection('layer',canvas.chapter.root_page_ids[0])
        elif retire=='tool':
            canvas.set_tool(ToolKind.OBJECT_SELECT)
        elif retire=='history':
            canvas.command_stack.undo()
        elif retire=='source':
            identifier=next(iter(canvas.chapter.objects))
            canvas.tiles.tile(identifier,(0,0)).fill(QColor('red'))
        else:
            page=canvas.chapter.layers[canvas.chapter.root_page_ids[0]]
            page.opacity=.6
            canvas._publish_change_set(ChangeSet((EntityChange(('layer',page.layer_id),frozenset({'opacity'})),),
                label='Other edit'),action='commit')
        pump(canvas,qapp)
        assert controller.geometry_continuity.ticket is None
        assert controller.geometry_continuity.for_request(controller.desired) is None
    finally:
        release.set()
