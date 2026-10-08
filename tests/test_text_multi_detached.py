"""Proposed artifact-only regressions; no Qt import occurs during preparation.

Run only through the pinned isolated/original source runner after exclusive GO.
Every positive gesture uses ordinary press/move/release, never a private begin,
preview-update or direct commit shortcut. Source pixels are fixed fixture input.
"""
from copy import deepcopy
from dataclasses import asdict
import math

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPolygonF
from PySide6.QtWidgets import QApplication

from comic_editor.core.commands import CallbackCommand
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (BoundGeometry, ChapterDocument, ImageObject,
    MirrorModifier, OutlineModifier, RadialBlurModifier, RasterObject, TextObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.service import RenderQuality, RenderRequest, RenderStatus
from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.service import DocumentRenderService
from comic_editor.ui.canvas import RasterCanvasWidget, ToolKind


@pytest.fixture(scope='session',autouse=True)
def isolated_settings(tmp_path_factory):
    from comic_editor.core import settings
    path=tmp_path_factory.mktemp('text-multi-settings')/'settings.json'
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(settings,'settings_path',lambda:path)
        yield


@pytest.fixture(scope='session')
def qapp():
    return QApplication.instance() or QApplication([])


def close(canvas):
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    consumers = getattr(canvas, '_scene_consumers', None)
    if consumers is not None:
        consumers.shutdown()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    for timer in canvas.findChildren(QTimer): timer.stop()
    for name in ('_gpu_texture_renderer','_gpu_pattern_renderer','_gpu_cage_renderer'):
        renderer=getattr(canvas,name,None)
        if renderer:renderer.close()
    canvas.close()
    canvas.deleteLater()


def png(image):
    data=QByteArray()
    buffer=QBuffer(data)
    assert buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer,'PNG')
    buffer.close()
    return bytes(data)


def scene(composition,ancestor,projective,primary_second):
    settings=EditorSettings(snap_to_grid=False,grid_overlay_visible=False)
    canvas=RasterCanvasWidget(settings)
    canvas.setUpdatesEnabled(False)
    canvas.resize(700,550)
    chapter=ChapterDocument(width=1080,height=1600)
    page=chapter.add_page('Page',BoundGeometry.rectangle(0,0,1080,1600))
    parent=chapter.add_layer(page.layer_id,'Free Text',layer_kind='text_container')
    parent.transform_frame=(0.,0.,400.,300.)
    parent.transform_quad=([(35.,55.),(435.,35.),(415.,365.),(25.,345.)] if projective
        else [(35.,55.),(435.,55.),(435.,355.),(35.,355.)])
    a=chapter.add_object(parent.layer_id,TextObject(text='Paying sixty dollars\nper hour',
        width=145,height=175,font_family='Segoe UI',font_size=25,
        horizontal_alignment='left',vertical_alignment='top',margin=4,
        layout_mode='free',transform_behavior='bounds',
        transform_quad=[(20.,10.),(165.,10.),(165.,185.),(20.,185.)]))
    other_parent=chapter.add_layer(page.layer_id,'Other input',
        BoundGeometry.rectangle(0,0,1080,1600))
    other_parent.fill_color=None
    other_parent.border_width=0
    other_parent.translate_x,other_parent.translate_y=65.,45.
    canvas.set_document(chapter,TileStore(),ImageStore())

    def image(owner,x,y,color):
        obj=chapter.add_object(owner,ImageObject(pixel_width=95,pixel_height=100,
            source_filename='fixture.png',source_mime_type='image/png',
            transform_frame=(0,0,95,100),transform_quad=[(x,y),(x+95,y),(x+95,y+100),(x,y+100)]))
        input_image=QImage(95,100,QImage.Format_ARGB32_Premultiplied)
        input_image.fill(QColor(color))
        painter=QPainter(input_image)
        painter.fillRect(12,17,42,52,QColor('#ffeedd'))
        painter.end()
        canvas.images.put(obj.object_id,'fixture.png',png(input_image),'image/png')
        return obj

    def raster(owner,x,y,color):
        obj=chapter.add_object(owner,RasterObject(x=x,y=y,interaction_rect=(0,0,95,100)))
        canvas.tiles.paint_dab(obj.object_id,QPointF(40,45),28,QColor(color),antialias=False,square=True)
        return obj

    if composition=='text-text':
        b=chapter.add_object(parent.layer_id,TextObject(text='Group two\nkeeps its layout',
            width=155,height=145,font_family='Segoe UI',font_size=24,
            horizontal_alignment='left',vertical_alignment='top',margin=4,
            layout_mode='free',transform_behavior='stretch',
            transform_quad=[(195.,20.),(350.,20.),(350.,165.),(195.,165.)]))
    elif composition=='text-image':
        b=image(other_parent.layer_id,285.,25.,'#bb3366')
    elif composition=='image-image':
        a=image(other_parent.layer_id,20.,15.,'#4466bb')
        b=image(other_parent.layer_id,200.,25.,'#bb3366')
    elif composition=='raster-raster':
        a=raster(other_parent.layer_id,20.,15.,'#4466bb')
        b=raster(other_parent.layer_id,200.,25.,'#bb3366')
    else:raise AssertionError('Unknown fixture composition')
    if composition.startswith('text'):
        chapter.add_modifier(OutlineModifier(thickness=5,color='#ffffff'),[('layer',parent.layer_id)])
        chapter.add_modifier(OutlineModifier(thickness=3),[('layer',parent.layer_id)])
        if ancestor=='mirror':
            chapter.add_modifier(MirrorModifier(axis_start=(440,0),axis_end=(440,500)),[('layer',parent.layer_id)])
        elif ancestor=='radial':
            chapter.add_modifier(RadialBlurModifier(center=(230,185),angle=20),[('layer',parent.layer_id)])
    chapter.validate()
    canvas.center_x,canvas.center_y,canvas.scale,canvas.rotation=350.,275.,1.,0.
    refs=[('object',a.object_id),('object',b.object_id)]
    assert canvas.set_selection_set(refs,primary=refs[1 if primary_second else 0])
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.command_stack.push(CallbackCommand('Prior fixture command',lambda:None,lambda:None),already_done=True)
    return canvas,refs


def press_for(canvas,mode):
    cage=canvas._multi_selection_cage()
    assert cage is not None
    if mode!='translate':return QPointF(*cage[2])
    handles,rotate,pivot=canvas._transform_control_points(cage,canvas._transform_pivot)
    controls=[*handles,rotate.toTuple(),pivot.toTuple()]
    tolerance=14/max(canvas.scale,.05)
    for u,v in ((.25,.25),(.75,.25),(.25,.75),(.75,.75)):
        weights=((1-u)*(1-v),u*(1-v),u*v,(1-u)*v)
        point=QPointF(sum(w*p[0] for w,p in zip(weights,cage)),sum(w*p[1] for w,p in zip(weights,cage)))
        if min(math.dist(point.toTuple(),p) for p in controls)>tolerance and canvas._transform_control_hit(cage,point)==('translate',None):
            return point
    raise AssertionError('Fixture cage has no real translation affordance')


def capture(canvas):
    document=canvas._render_document_state()
    request=RenderRequest((0.,0.,700.,550.),1.,(700,550),('text-multi-current-proof',),
        document.revision,quality=RenderQuality.INTERACTIVE,defer_effects=False)
    # This unit oracle uses the incoming detached evaluator. Real widget
    # publication latency/currentness remains a separate native acceptance.
    capture=canvas._scene_snapshot_compiler.capture(canvas,document)
    while not capture.advance(.004):
        pass
    assert not capture.stale and capture.result.document==document
    backend=DetachedSceneBackend(capture.result)
    backend.native_preview=True
    backend.artwork_scale=1.
    service=DocumentRenderService(backend)
    service.projection.revision=document.revision
    try:
        result=service.render_region(document,request)
        assert result.status in (RenderStatus.EXACT,RenderStatus.PROVISIONAL)
        assert not result.image.isNull()
        assert service.current(document,request)
        return QImage(result.image)
    finally:
        backend.close()


def pixels(image):
    return np.frombuffer(image.constBits(),np.uint8).reshape(image.height(),image.bytesPerLine()).copy()


def committed_reference(canvas,before,refs,press,move):
    store=canvas.images.clone()
    assert not store._decoded
    for identifier in canvas.images._sources:
        if identifier in canvas.images._decoded or ('native',identifier) in canvas.images._decoded:
            canvas.images.copy_source_to(identifier,store,identifier)
    reference=RasterCanvasWidget(type(canvas.settings)(**asdict(canvas.settings)))
    reference.setUpdatesEnabled(False)
    reference.resize(700,550)
    reference.set_document(ChapterDocument.from_dict(deepcopy(before)),canvas.tiles,store)
    reference.center_x,reference.center_y,reference.scale,reference.rotation=350.,275.,1.,0.
    assert reference.set_selection_set(refs,primary=(canvas.selected_kind,canvas.selected_id))
    reference.set_tool(ToolKind.TRANSFORM)
    assert not reference._modifier_render_cache and not reference._modifier_source_cache
    try:
        reference._tool_press(reference.document_to_widget(press),1.)
        assert reference._geometry_transform_target==('multi','')
        reference._tool_move(reference.document_to_widget(move),1.)
        expected=deepcopy(reference._multi_transform_preview_quads)
        reference._tool_release()
        assert reference._transform_preview_quad is None
        assert len(reference.command_stack._undo)==1
        for _kind,identifier in refs:
            assert reference.chapter.objects[identifier].transform_quad==expected[identifier]
        model=deepcopy(reference.chapter.to_dict())
        result=capture(reference)
        assert reference.chapter.to_dict()==model
        return result,model
    finally:close(reference)


@pytest.mark.parametrize('mode',['translate','scale','warp'])
@pytest.mark.parametrize('composition',['text-text','text-image','image-image','raster-raster'])
@pytest.mark.parametrize('primary_second',[False,True])
def test_actual_multi_release_stores_every_selected_destination(qapp,mode,composition,primary_second):
    """Bounded old/new causal replay, before introducing any ROI oracle."""
    canvas,refs=scene(composition,'outline',False,primary_second)
    try:
        canvas.settings.transform_mode='free' if mode=='warp' else 'uniform'
        before=deepcopy(canvas.chapter.to_dict())
        prior=tuple(canvas.command_stack._undo)
        revision=canvas.command_stack.revision
        press=press_for(canvas,mode)
        canvas._tool_press(canvas.document_to_widget(press),1.)
        assert canvas._geometry_transform_target==('multi','')
        assert isinstance(canvas._model_before,RecordSnapshot)
        assert set(canvas._model_before.records['objects'])=={identifier for _,identifier in refs}
        assert canvas._transform_drag_mode==('translate' if mode=='translate' else 'handle')
        canvas._tool_move(canvas.document_to_widget(press+QPointF(31,23)),1.)
        expected=deepcopy(canvas._multi_transform_preview_quads)
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
        assert set(expected)=={identifier for _,identifier in refs}
        canvas._tool_release()
        stored={identifier:canvas.chapter.objects[identifier].transform_quad for _,identifier in refs}
        assert stored==expected,{'composition':composition,'primary_second':primary_second,
            'mode':mode,'expected':expected,'stored':stored}
        assert canvas._geometry_transform_target is None and not canvas._multi_transform_preview_quads
        own=canvas.command_stack.top_undo_command
        assert own is not None and own.label=='Transform objects'
        assert len(canvas.command_stack._undo)==len(prior)+1
        assert all(a is b for a,b in zip(prior,canvas.command_stack._undo[:-1]))
        assert canvas.command_stack.revision==revision+1
        after=deepcopy(canvas.chapter.to_dict())
        assert after!=before
        for _,identifier in refs:
            obj=canvas.chapter.objects[identifier]
            parent=canvas.layer_world_transform(obj.parent_layer_id)
            assert canvas.object_world_quad(identifier)==[parent.map(QPointF(*point)).toTuple()
                for point in expected[identifier]]
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict()==after and canvas.command_stack.top_undo_command is own
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before
    finally:close(canvas)


@pytest.mark.parametrize('mode',['translate','scale','warp'])
@pytest.mark.parametrize('composition',['text-text','text-image'])
@pytest.mark.parametrize('ancestor',['outline','mirror','radial'])
@pytest.mark.parametrize('projective',[False,True])
@pytest.mark.parametrize('primary_second',[False,True])
def test_actual_multi_free_text_is_current_and_one_undoable_commit(qapp,mode,composition,ancestor,projective,primary_second):
    canvas,refs=scene(composition,ancestor,projective,primary_second)
    try:
        canvas.settings.transform_mode='free' if mode=='warp' else 'uniform'
        before=deepcopy(canvas.chapter.to_dict())
        before_records={identifier:deepcopy(canvas.chapter.objects[identifier].to_dict()) for _,identifier in refs}
        history=tuple(canvas.command_stack._undo)
        revision=canvas.command_stack.revision
        before_image=capture(canvas)
        press=press_for(canvas,mode)
        move=press+(QPointF(0,-35) if mode=='translate' else QPointF(42,25))
        canvas._tool_press(canvas.document_to_widget(press),1.)
        assert canvas._geometry_transform_target==('multi','') and canvas._free_text_drag is None
        assert canvas._transform_drag_mode==('translate' if mode=='translate' else 'handle')
        canvas._tool_move(canvas.document_to_widget(move),1.)
        intended=deepcopy(canvas._multi_transform_preview_quads)
        assert set(intended)=={identifier for _,identifier in refs}
        assert canvas.chapter.to_dict()==before
        assert tuple(canvas.command_stack._undo)==history and canvas.command_stack.revision==revision
        live=capture(canvas)
        assert np.count_nonzero(pixels(live)!=pixels(before_image))>0
        fresh,fresh_model=committed_reference(canvas,before,refs,press,move)
        assert np.array_equal(pixels(live),pixels(fresh))
        canvas._tool_release()
        after=deepcopy(canvas.chapter.to_dict())
        assert after!=before and after==fresh_model
        assert canvas._transform_preview_quad is None and not canvas._multi_transform_preview_quads
        for _,identifier in refs:
            obj=canvas.chapter.objects[identifier]
            assert obj.transform_quad==intended[identifier]
            if isinstance(obj,TextObject):
                assert [obj.width,obj.height]==before_records[identifier]['size']
                assert obj.x==obj.y==0
                assert all(obj.to_dict()[key]==value for key,value in before_records[identifier].items()
                    if key not in {'position','transform_quad'})
        own=canvas.command_stack.top_undo_command
        assert own is not None and own.label=='Transform objects'
        assert len(canvas.command_stack._undo)==len(history)+1 and all(a is b for a,b in zip(history,canvas.command_stack._undo[:-1]))
        assert canvas.command_stack.revision==revision+1
        released=capture(canvas)
        assert np.array_equal(pixels(released),pixels(fresh))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==history
        assert canvas.command_stack._redo[-1] is own
        canvas.command_stack.redo()
        assert canvas.chapter.to_dict()==after and canvas.command_stack.top_undo_command is own
        assert np.array_equal(pixels(capture(canvas)),pixels(fresh))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before
    finally:close(canvas)


@pytest.mark.parametrize('mode',['translate','scale','warp'])
@pytest.mark.parametrize('composition',['image-image','raster-raster'])
@pytest.mark.parametrize('primary_second',[False,True])
def test_actual_existing_multi_image_raster_release_is_group_commit(qapp,mode,composition,primary_second):
    canvas,refs=scene(composition,'outline',False,primary_second)
    try:
        canvas.settings.transform_mode='free' if mode=='warp' else 'uniform'
        before=deepcopy(canvas.chapter.to_dict())
        prior=tuple(canvas.command_stack._undo)
        press=press_for(canvas,mode)
        canvas._tool_press(canvas.document_to_widget(press),1.)
        assert canvas._geometry_transform_target==('multi','')
        assert isinstance(canvas._model_before,RecordSnapshot)
        assert set(canvas._model_before.records['objects'])=={identifier for _,identifier in refs}
        canvas._tool_move(canvas.document_to_widget(press+QPointF(31,23)),1.)
        expected=deepcopy(canvas._multi_transform_preview_quads)
        assert canvas.chapter.to_dict()==before
        canvas._tool_release()
        assert all(canvas.chapter.objects[identifier].transform_quad==expected[identifier] for _,identifier in refs)
        assert canvas.command_stack.top_undo_command.label=='Transform objects'
        assert len(canvas.command_stack._undo)==len(prior)+1
        assert all(a is b for a,b in zip(prior,canvas.command_stack._undo[:-1]))
        canvas.command_stack.undo()
        assert canvas.chapter.to_dict()==before and tuple(canvas.command_stack._undo)==prior
    finally:close(canvas)


@pytest.mark.parametrize('primary_strict',[False,True])
@pytest.mark.parametrize('other_kind',['text-text','text-image','raster-raster'])
def test_strict_text_multi_does_not_partially_transform_and_keeps_selection_dispatch(qapp,primary_strict,other_kind):
    canvas,refs=scene(other_kind,'outline',False,False)
    try:
        page=canvas.chapter.root_page_ids[0]
        parent=canvas.chapter.add_layer(page,'Strict owner',BoundGeometry.rectangle(800,600,180,190))
        strict=canvas.chapter.add_object(parent.layer_id,TextObject(text='Strict',layout_mode='strict'))
        canvas.chapter.validate()
        target=refs[0]
        if other_kind=='raster-raster':
            # This unselected initial Text otherwise overlaps the Raster
            # center and legitimately wins the unchanged Text-first selector.
            for obj in canvas.chapter.objects.values():
                if isinstance(obj,TextObject) and obj is not strict:
                    obj.transform_quad=[(780.,700.),(925.,700.),(925.,875.),(780.,875.)]
        # Stabilize the normal remembered-raster selection before the strict
        # group, so the actual selector need not change unrelated metadata.
        canvas.set_selection(*target)
        selected=[target,('object',strict.object_id)]
        assert canvas.set_selection_set(selected,primary=selected[1 if primary_strict else 0])
        canvas.set_tool(ToolKind.TRANSFORM)
        assert canvas._multi_selection_cage() is None
        before=deepcopy(canvas.chapter.to_dict())
        commands=tuple(canvas.command_stack._undo)
        revision=canvas.command_stack.revision
        # The ordinary existing selector receives the click. No forced
        # fallback begin or patched return value substitutes for selection.
        quad=canvas.object_world_quad(target[1])
        point=QPointF(sum(x for x,y in quad)/4,sum(y for x,y in quad)/4)
        hits=canvas.hit_test_entities(point)
        hits.sort(key=lambda hit:not(hit['kind']=='object'
            and isinstance(canvas.chapter.objects.get(hit['id']),TextObject)))
        assert hits and (hits[0]['kind'],hits[0]['id'])==target
        calls=[]
        selector=canvas._request_object_selection
        def observed_selector(*a,**kw):
            calls.append(True)
            return selector(*a,**kw)
        canvas._request_object_selection=observed_selector
        canvas._tool_press(canvas.document_to_widget(point),1.)
        assert calls==[True]
        canvas._tool_release()
        assert canvas.chapter.to_dict()==before
        assert tuple(canvas.command_stack._undo)==commands and canvas.command_stack.revision==revision
        assert canvas._geometry_transform_target is None and canvas._transform_preview_quad is None
        assert canvas._free_text_drag is None
        import time
        deadline=time.monotonic()+10.
        while canvas.selected_entities!=[target] and time.monotonic()<deadline:
            qapp.processEvents()
            time.sleep(.002)
        assert canvas.selected_entities==[target]
    finally:close(canvas)
