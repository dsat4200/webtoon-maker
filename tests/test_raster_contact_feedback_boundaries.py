"""Held native feedback across transformed source and world tile boundaries."""
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import BoundGeometry, ChildRef
from comic_editor.core.tools import ToolKind
from comic_editor.render.raster_feedback import _feedback_source_keys
from comic_editor.render.scheduler import SceneDemand
from comic_editor.render.service import RenderRequest, RenderQuality
from comic_editor.ui.document_presentation import draw_document_border
from test_raster_contact_feedback import scene, pixels, ready_paint


def native_decorated(canvas):
    """Independent native artwork plus the presenter's mandatory document UI."""
    document = canvas._render_document_state()
    width, height = canvas.width(), canvas.height()
    request = RenderRequest((0.,0.,float(width),float(height)),1.,(width,height),
        ('native-contact-boundary-oracle',),document.revision,quality=RenderQuality.INTERACTIVE)
    result = canvas._render_service.render_region(document,request)
    assert not result.image.isNull(),result
    image = QImage(width,height,QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#242428'))
    painter=QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing,True)
    try:
        painter.drawImage(0,0,result.image)
        draw_document_border(painter,document.bounds,canvas.camera_transform(),canvas.size(),owner=canvas)
    finally:
        painter.end()
    return image


def block_existing_scene_demand(canvas, monkeypatch):
    """Block an already valid ordinary scene job before starting the contact."""
    scheduler=canvas._scene_controller.scheduler
    gate,entered=Event(),Event()
    original=scheduler._evaluate_admitted
    def blocked(demand,token):
        entered.set()
        assert gate.wait(20),'test barrier was not released'
        return original(demand,token)
    monkeypatch.setattr(scheduler,'_evaluate_admitted',blocked)
    snapshot=canvas._scene_controller.snapshot
    assert snapshot is not None and not snapshot.document.live_preview
    visible=canvas.visible_document_rect().intersected(snapshot.document.bounds)
    requests=tuple(canvas._document_projection.requests(visible,1.))
    scheduler.submit(SceneDemand(canvas._scene_controller.serial,snapshot,requests,(None,),
        (visible.center().x(),visible.center().y()),tuple(visible.getRect())))
    assert entered.wait(3.) and scheduler.busy
    return gate


def boundary_scene(canvas,selected,front,format,opacity):
    canvas.chapter.width,canvas.chapter.height=768,70000
    canvas.setFixedSize(768,128)
    canvas.center_x,canvas.center_y,canvas.scale=384.,64.,1.
    page=canvas.chapter.layers[selected.parent_layer_id]
    page.bound=BoundGeometry.rectangle(8.25,8.5,751.5,110.)
    page.children=[ref for ref in page.children if ref.entity_id!=selected.object_id]
    layer=canvas.chapter.add_layer(page.layer_id,'Native seam',BoundGeometry.rectangle(0,0,768,128),index=1)
    layer.fill_color,layer.border_width=None,0
    layer.transform_frame=(0.,0.,768.,128.)
    layer.transform_quad=[(-13.5,3.7),(850.5,3.7),(850.5,118.9),(-13.5,118.9)]
    layer.children=[ChildRef('object',selected.object_id)]
    selected.parent_layer_id=layer.layer_id
    selected.x,selected.y,selected.opacity=.25,-.125,opacity
    selected.transform_frame=(0.,0.,768.,256.)
    selected.transform_quad=[(14.75,10.5),(690.59,10.5),(690.59,189.7),(14.75,189.7)]
    back=canvas.chapter.objects[page.children[-1].entity_id]
    for x in range(3):
        image=QImage(256,256,QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('blue'))
        canvas.tiles.set_tile(back.object_id,(x,0),image)
    image=QImage(256,256,QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter=QPainter(image)
    painter.fillRect(24,20,16,90,QColor('green'))
    painter.end()
    # The foreground crosses the drawn line in world tile 1.
    canvas.tiles.set_tile(front.object_id,(0,0),QImage(256,256,QImage.Format_ARGB32_Premultiplied))
    canvas.tiles.tile(front.object_id,(0,0)).fill(Qt.transparent)
    canvas.tiles.set_tile(front.object_id,(1,0),image)
    colors=(QColor.fromRgbF(.0713,.2817,.7891,.3527),
            QColor.fromRgbF(.7831,.3173,.0719,.7243),
            QColor.fromRgbF(.0371,.7139,.8293,.4527))
    for x,color in enumerate(colors):
        image=QImage(256,256,format)
        image.fill(color)
        canvas.tiles.set_tile(selected.object_id,(x,0),image)
    canvas._invalidate_scene_cache()


def begin(canvas,tool,point):
    if tool==ToolKind.BRUSH:
        canvas._begin_paint_brush(point,1.,_definition=BrushDefinition(size=16,antialiasing=0,spacing=.08))
    else:
        canvas._begin_stroke(point,1.)


def finish(canvas,tool):
    (canvas._finish_paint_brush if tool==ToolKind.BRUSH else canvas._end_stroke)()


@pytest.mark.parametrize('tool',[ToolKind.RASTER_PENCIL,ToolKind.BRUSH,ToolKind.RASTER_ERASER])
@pytest.mark.parametrize('format',[QImage.Format_ARGB32_Premultiplied,QImage.Format_RGBA64_Premultiplied])
@pytest.mark.parametrize('opacity',[1.,.43])
def test_transformed_adjacent_native_source_tiles_match_every_held_full_buffer(
        scene,tool,format,opacity,wait_scene,monkeypatch):
    canvas,selected,front=scene
    boundary_scene(canvas,selected,front,format,opacity)
    canvas.set_tool(tool)
    wait_scene(canvas)
    prepared=canvas._scene_controller.feedback
    assert prepared is not None and prepared.source_transform is not None
    assert len(prepared.tiles)>=3
    seam=canvas._raster_world_point(selected,QPointF(256,80))
    assert 256.<seam.x()<258.,'source seam must sample the world patch gutter'
    before=ready_paint(canvas)
    np.testing.assert_array_equal(pixels(before),pixels(native_decorated(canvas)))
    sources_before={key:QImage(image) for key,image in canvas.tiles.object_tiles(selected.object_id).items()}
    revision=canvas.command_stack.revision
    gate=block_existing_scene_demand(canvas,monkeypatch)
    try:
        previous=before
        # Warm contact must not traverse a chapter or decode resident sources.
        with monkeypatch.context() as guarded:
            guarded.setattr(canvas._scene_snapshot_compiler,'capture',
                lambda *_a,**_k:pytest.fail('Warm native contact recaptured the chapter'))
            for index,x in enumerate((240,272,328,520)):
                point=QPointF(x,64)
                if index==0:
                    begin(canvas,tool,point)
                else:
                    (canvas._continue_paint_brush if tool==ToolKind.BRUSH else canvas._continue_stroke)(point,1.)
                actual=ready_paint(canvas)
                assert canvas._drawing and canvas._raster_contact_active and canvas._scene_controller.scheduler.busy
                assert canvas._scene_controller.feedback is prepared and not gate.is_set()
                assert actual.pixelColor(x,64)!=previous.pixelColor(x,64)
                assert actual.pixelColor(288,64)==QColor('green'),'foreground order must remain native'
                assert canvas._raster_feedback_contact_covered and not canvas._raster_feedback_pending_visible
                np.testing.assert_array_equal(pixels(actual),pixels(native_decorated(canvas)))
                for key,image in canvas.tiles.object_tiles(selected.object_id).items():
                    assert image.format()==format
                    baseline=sources_before[key]
                    # This bottom half never intersects native input coverage.
                    assert bytes(image.constBits())[128*image.bytesPerLine():]==bytes(baseline.constBits())[128*baseline.bytesPerLine():]
                previous=actual
                with monkeypatch.context() as presentation:
                    presentation.setattr(canvas,'_render_scene_layers',lambda *_a,**_k:pytest.fail('GUI scene evaluation'))
                    presentation.setattr(canvas.tiles.residency,'get',lambda *_a,**_k:pytest.fail('GUI source decode'))
                    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(actual))
        finish(canvas,tool)
        np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(previous))
        assert canvas.command_stack.revision==revision+1
    finally:
        if canvas._drawing:
            finish(canvas,tool)
        gate.set()
    wait_scene(canvas)
    np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_decorated(canvas)))
    canvas.command_stack.undo()
    assert sources_before==canvas.tiles.object_tiles(selected.object_id)


@pytest.mark.parametrize('format',[QImage.Format_ARGB32_Premultiplied,QImage.Format_RGBA64_Premultiplied])
def test_transformed_pruned_source_tile_keeps_adjacent_native_source_and_world_gutter(
        scene,format,wait_scene,monkeypatch):
    canvas,selected,front=scene
    boundary_scene(canvas,selected,front,format,1.)
    image=QImage(256,256,format)
    image.fill(Qt.transparent)
    painter=QPainter(image)
    painter.fillRect(232,73,8,8,QColor('red'))
    painter.end()
    canvas.tiles.set_tile(selected.object_id,(0,0),image)
    adjacent={key:QImage(image) for key,image in canvas.tiles.object_tiles(selected.object_id).items() if key!=(0,0)}
    canvas.set_tool(ToolKind.RASTER_ERASER)
    wait_scene(canvas)
    gate=block_existing_scene_demand(canvas,monkeypatch)
    try:
        point=canvas._raster_world_point(selected,QPointF(236,77))
        begin(canvas,ToolKind.RASTER_ERASER,point)
        canvas._end_stroke()
        assert (0,0) not in canvas.tiles._tiles[selected.object_id].entries
        assert adjacent=={key:image for key,image in canvas.tiles.object_tiles(selected.object_id).items() if key!=(0,0)}
        np.testing.assert_array_equal(pixels(ready_paint(canvas)),pixels(native_decorated(canvas)))
    finally:
        if canvas._drawing:
            canvas._end_stroke()
        gate.set()


def test_inverse_source_key_cap_declines_before_a_large_dependency_set_is_constructed():
    inverse=QTransform(1e6,0.,0.,1e6,0.,0.)
    assert _feedback_source_keys((0.,0.,260.,260.),inverse,256) is None
    assert len(_feedback_source_keys((-2.,-2.,260.,260.),QTransform(),256))==9
