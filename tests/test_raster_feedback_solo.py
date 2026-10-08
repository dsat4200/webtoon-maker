"""Prepared solo feedback matches the ordinary cold native scene exactly."""
from concurrent.futures import ThreadPoolExecutor
import copy

import pytest
from PySide6.QtCore import QRectF,Qt
from PySide6.QtGui import QColor,QImage,QPainter

from comic_editor.core.models import BoundGeometry,BlurModifier,ParameterMaskBinding,ToneMask
from comic_editor.render.raster_feedback import prepare_raster_feedback
from comic_editor.render.scene import SceneSnapshotCompiler,DetachedSceneBackend
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.render.service import DocumentRenderService,RenderRequest,RenderQuality
from comic_editor.ui.raster_feedback import _compose_tile
from test_raster_contact_feedback import scene


def buffers(image):
    return image.size(),image.format(),image.bytesPerLine(),bytes(image.constBits())


def freeze(canvas):
    capture=SceneSnapshotCompiler().capture(canvas,canvas._render_document_state())
    while not capture.advance(.001):pass
    assert not capture.stale and capture.result is not None
    return capture.result


def flat(image):
    result=QImage(image.size(),QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor('#242428'));painter=QPainter(result)
    try:painter.drawImage(0,0,image)
    finally:painter.end()
    return result


def native(snapshot):
    backend=DetachedSceneBackend(snapshot)
    try:
        service=DocumentRenderService(backend);service.projection.revision=snapshot.document.revision
        request=RenderRequest((0.,0.,128.,128.),1.,(128,128),('solo-feedback-native-oracle',),
            snapshot.document.revision,quality=RenderQuality.EXACT)
        result=service.render_region(snapshot.document,request)
        assert result.exact,result
        return flat(result.image)
    finally:backend.close()


def nested(canvas,selected):
    page=canvas.chapter.layers[selected.parent_layer_id]
    children=list(page.children);page.children=[]
    layer=canvas.chapter.add_layer(page.layer_id,'Solo ink clip',BoundGeometry.rectangle(16,16,96,96))
    layer.fill_color='#663366';layer.border_width=3
    layer.translate_x,layer.translate_y=3.,5.
    layer.children=children
    for ref in children:canvas.chapter.objects[ref.entity_id].parent_layer_id=layer.layer_id
    selected.x,selected.y=7.,11.
    source=QImage(256,256,QImage.Format_ARGB32_Premultiplied);source.fill(Qt.transparent)
    painter=QPainter(source)
    try:painter.fillRect(16,24,80,32,QColor('red'))
    finally:painter.end()
    canvas.tiles.set_tile(selected.object_id,(0,0),source)
    canvas._invalidate_scene_cache()
    return page,layer


def compose(canvas,prepared):
    owner=canvas.tiles._tiles[prepared.identifier]
    resident={key:entry[0] for (source,key),entry in owner.residency.entries.items() if source is owner}
    result=QImage(128,128,QImage.Format_ARGB32_Premultiplied);result.fill(QColor('#242428'))
    painter=QPainter(result)
    try:
        for tile in prepared.tiles:
            patch=_compose_tile(canvas,prepared,tile,resident)
            side,gutter=prepared.tile_size,prepared.gutter
            world=QRectF(tile.bounds[0]+gutter,tile.bounds[1]+gutter,side,side)
            painter.drawImage(world,patch,QRectF(gutter,gutter,side,side))
    finally:painter.end()
    return result


@pytest.mark.parametrize('mode',['object','layer','multiple','page','suspended'])
def test_solo_prepared_native_feedback_matches_cold_shared_scene_and_hides_excluded_siblings(
        scene,monkeypatch,mode):
    canvas,selected,front=scene
    page,layer=nested(canvas,selected)
    entries=({'object':{('object',selected.object_id)},'layer':{('layer',layer.layer_id)},
        'multiple':{('object',selected.object_id),('object',front.object_id)},
        'page':{('layer',page.layer_id)},'suspended':{('object',front.object_id)}}[mode])
    canvas.set_solo_entities(entries)
    if mode=='suspended':canvas._solo_suspended=True
    before_model=copy.deepcopy(canvas.chapter.to_dict());snapshot=freeze(canvas)
    old_source=buffers(snapshot.tiles.tile(selected.object_id,(0,0)))
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared=worker.submit(prepare_raster_feedback,snapshot,selected.object_id,(0.,0.,128.,128.)).result(timeout=20)
    assert prepared is not None and prepared.origin==(10,16)
    assert prepared.document.configuration==snapshot.document.configuration
    # Replace only current resident source pixels after immutable preparation.
    current=canvas.tiles.tile(selected.object_id,(0,0)).copy()
    painter=QPainter(current)
    try:painter.fillRect(16,24,80,32,QColor('yellow'))
    finally:painter.end()
    canvas.tiles.set_tile(selected.object_id,(0,0),current)
    canvas._invalidate_scene_cache()
    newer=freeze(canvas)
    with ThreadPoolExecutor(max_workers=1) as worker:expected=worker.submit(native,newer).result(timeout=20)
    with monkeypatch.context() as patch:
        patch.setattr(canvas,'_render_scene_layers',lambda *_a,**_k:pytest.fail('Feedback evaluated GUI scene'))
        patch.setattr(canvas.tiles.residency,'get',lambda *_a,**_k:pytest.fail('Feedback decoded GUI source'))
        actual=compose(canvas,prepared)
    assert buffers(actual)==buffers(expected)
    assert actual.pixelColor(42,64)==QColor('yellow')
    assert actual.pixelColor(4,64)==QColor('#242428')
    if mode=='object':
        assert actual.pixelColor(58,64)==QColor('yellow')
        assert actual.pixelColor(32,32)==QColor('#242428')
    else:assert actual.pixelColor(58,64)==QColor('green')
    assert buffers(snapshot.tiles.tile(selected.object_id,(0,0)))==old_source
    assert canvas.chapter.to_dict()==before_model


def test_selected_raster_excluded_by_solo_never_draws_raw_feedback_source(scene,monkeypatch):
    canvas,selected,front=scene
    nested(canvas,selected);canvas.set_solo_entities({('object',front.object_id)})
    snapshot=freeze(canvas);calls=[];original=SceneKernels._render_raster_content
    def observed(self,painter,obj,*args,**kwargs):
        calls.append(obj.object_id);return original(self,painter,obj,*args,**kwargs)
    monkeypatch.setattr(SceneKernels,'_render_raster_content',observed)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared=worker.submit(prepare_raster_feedback,snapshot,selected.object_id,(0.,0.,128.,128.)).result(timeout=20)
    assert prepared is None and not calls
    with ThreadPoolExecutor(max_workers=1) as worker:expected=worker.submit(native,snapshot).result(timeout=20)
    assert expected.pixelColor(42,64)==QColor('#242428')
    assert expected.pixelColor(58,64)==QColor('green')


@pytest.mark.parametrize('dependency',['ancestor_effect','ancestor_opacity_mask','ancestor_compound'])
def test_solo_nonseparable_ancestor_uses_ordinary_mask_effect_clip_pipeline(scene,dependency):
    canvas,selected,_front=scene
    _page,layer=nested(canvas,selected)
    if dependency=='ancestor_effect':canvas.chapter.add_modifier(BlurModifier(strength=2),[('layer',layer.layer_id)])
    elif dependency=='ancestor_compound':layer.compound_enabled=True
    else:
        mask=ToneMask();canvas.chapter.masks[mask.mask_id]=mask
        tile=QImage(256,256,QImage.Format_ARGB32_Premultiplied);tile.fill(QColor(255,255,255,180))
        canvas.tiles.set_tile(mask.mask_id,(0,0),tile)
        layer.opacity_mask=ParameterMaskBinding(mask.mask_id,0.,1.)
    canvas.set_solo_entities({('object',selected.object_id)})
    before=copy.deepcopy(canvas.chapter.to_dict());snapshot=freeze(canvas)
    with ThreadPoolExecutor(max_workers=1) as worker:
        assert worker.submit(prepare_raster_feedback,snapshot,selected.object_id,(0.,0.,128.,128.)).result(timeout=20) is None
        expected=worker.submit(native,snapshot).result(timeout=20)
    # The original renderer remains the fallback with its current solo masks/effects.
    document=canvas._render_document_state()
    request=RenderRequest((0.,0.,128.,128.),1.,(128,128),('solo-ancestor-live-control',),
        document.revision,quality=RenderQuality.EXACT)
    ordinary=canvas._render_service.render_region(document,request)
    assert ordinary.exact and buffers(flat(ordinary.image))==buffers(expected)
    assert canvas.chapter.to_dict()==before
