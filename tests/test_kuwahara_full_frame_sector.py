"""Full original sector kernel is the native oracle for every cropped consumer."""
import copy
import hashlib
import json
import time

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage,QTransform

from comic_editor.core import kuwahara
from comic_editor.core.models import (BoundGeometry,BrightnessContrastModifier,ChapterDocument,
    HueSaturationLightnessModifier,KuwaharaModifier,ParameterMaskBinding,ToneMask)
from comic_editor.core.pixel_contract import LEGACY_PIXELS,FLOAT_PIXELS,PixelContract
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render import cache as cache_storage
from comic_editor.render.async_projection import ProjectionPending
from comic_editor.render.effect_pipeline import render_stages
from comic_editor.render.modifier_rendering import apply_modifier_stack,_premultiplied_qimage
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.tile_effects import tile_output
from comic_editor.ui.canvas import CanvasWidget

F16=PixelContract(version=2,precision='float16')
ROUTES=[('byte_stages',LEGACY_PIXELS,False),('byte_generic',LEGACY_PIXELS,True),
        ('float16_generic',F16,True),('float32_generic',FLOAT_PIXELS,True)]
OLD_RENDERER='native-artwork-20261008-refactor-full-frame-translation-1'

@pytest.fixture
def scene(qapp,monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager',lambda *_:None)
    owner=CanvasWidget(EditorSettings(canvas_renderer='raster',grid_overlay_visible=False))
    owner.setUpdatesEnabled(False)
    chapter=ChapterDocument(width=600,height=500,document_kind='asset')
    chapter.add_page('Sector oracle',BoundGeometry.rectangle(0,0,600,500))
    owner.set_document(chapter,TileStore())
    yield owner
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._scene_controller.scheduler.executor.shutdown(wait=True,cancel_futures=False)
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True,cancel_futures=True)
    owner.close();owner.deleteLater()

def buffers(image):
    return image.size(),image.format(),image.bytesPerLine(),image.colorSpace(),bytes(image.constBits())

def patterned(width=305,height=263):
    yy,xx=np.mgrid[:height,:width]
    alpha=((xx+yy)%193).astype(np.float32)/192.
    rgba=np.stack((xx/width,yy/height,(xx%7)/7,np.ones_like(xx)),axis=-1).astype(np.float32)
    return _premultiplied_qimage(rgba*alpha[...,None])

def crop(image,bounds,requested):
    region=QRectF(bounds.intersected(requested).toAlignedRect()).intersected(bounds)
    rect=QRectF(region);rect.translate(-bounds.topLeft())
    return image.copy(rect.toAlignedRect()),region

def modifiers(scene,variant,masked):
    sector=KuwaharaModifier(variant=variant,size=3)
    effects=[BrightnessContrastModifier(brightness=12),sector,HueSaturationLightnessModifier(hue=30)]
    if masked:
        mask=ToneMask();scene.chapter.masks[mask.mask_id]=mask
        # Real chapter-local painted coverage, not a replacement mask evaluator.
        for key,alpha in [((-1,-1),.27),((-1,0),.54),((0,-1),.71),((0,0),.93),((1,-1),.11),((1,0),.63)]:
            rgba=np.full((256,256,4),alpha,np.float32)
            scene.tiles.set_tile(mask.mask_id,key,_premultiplied_qimage(rgba))
        sector.parameter_masks['size']=ParameterMaskBinding(mask.mask_id,0,7)
        sector.parameter_masks['strength']=ParameterMaskBinding(mask.mask_id,37,100)
        sector.parameter_masks['intensity']=ParameterMaskBinding(mask.mask_id,29,100)
    for effect in effects:
        effect.validate();scene.chapter.modifiers[effect.modifier_id]=effect
    return effects

def original_full_oracle(scene,source,bounds,effects,mapping,generic):
    work=scene._world_to_image_transform(mapping,bounds,source.width(),source.height())
    fields=scene._modifier_mask_fields(effects,source.width(),source.height(),work,mapping.mapRect(bounds))
    origin=mapping.map(bounds.topLeft()).toTuple()
    if generic:
        return apply_modifier_stack(source,effects,origin,fields,world_to_image=work,_point_lut=False)
    # Compatibility stages quantize each whole original stage independently.
    value=source
    for effect in effects:
        value=apply_modifier_stack(value,[effect],origin,fields,world_to_image=work,_point_lut=False)
    return value

@pytest.mark.parametrize('route',ROUTES,ids=[r[0] for r in ROUTES])
@pytest.mark.parametrize('variant',['generalized','anisotropic'])
@pytest.mark.parametrize('masked',[False,True])
def test_sector_roi_whole_warm_pressure_and_durable_match_full_original_kernel(
        scene,monkeypatch,tmp_path,record_property,route,variant,masked):
    name,contract,generic=route
    scene.chapter.pixel_contract=contract
    with pixel_scope(contract):
        effects=modifiers(scene,variant,masked);source=patterned();bounds=QRectF(-37,-61,305,263)
        mapping=QTransform().translate(13,22).rotate(19).scale(1.5,.5) if generic else QTransform()
        source_before=buffers(source);model_before=copy.deepcopy(scene.chapter.to_dict())
        with monkeypatch.context() as patch:
            patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
            oracle=original_full_oracle(scene,source,bounds,effects,mapping,generic)
        trace=[];original=kuwahara._sectors
        def observed(rgba,size,effect,cancelled=None,origin=(0,0)):
            trace.append((rgba.shape,tuple(origin)))
            return original(rgba,size,effect,cancelled,origin)
        scene._interactive_render=True;scene._effect_region_requests=True;scene._projection_exact=True
        scope=('object','sector-native-oracle','canvas');identity=('unchanged-sector-source',name,variant,masked)
        def render(requested,*,provided=True):
            if generic:
                return tile_output(scene,source if provided else None,bounds,effects,mapping,
                    required=requested,request_scope=scope,source_identity=identity,float_pipeline=True)
            return render_stages(scene,source if provided else None,bounds,effects,mapping,
                required=requested,request_scope=scope,source_key=identity)
        regions=[QRectF(-25,-45,96,91),QRectF(128,12,140,190),QRectF(-500,-500,1200,1200)]
        with monkeypatch.context() as patch:
            patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
            patch.setattr(kuwahara,'_sectors',observed)
            for requested in regions:
                actual=render(requested);assert actual is not None
                expected,region=crop(oracle,bounds,requested)
                assert actual[1]==region and buffers(actual[0])==buffers(expected)
            assert trace==[((263,305,4),(0,0))],trace
            # The whole-frame sector is shared across separate warm ROI demands.
            warm_before=len(trace)
            for requested in reversed(regions):
                actual=render(requested);expected,region=crop(oracle,bounds,requested)
                assert actual is not None and actual[1]==region and buffers(actual[0])==buffers(expected)
            assert len(trace)==warm_before
            if generic:
                actual=render(regions[1],provided=False);expected,region=crop(oracle,bounds,regions[1])
                assert actual is not None and actual[1]==region and buffers(actual[0])==buffers(expected)
            # Persist an actual sector frame key/value produced by this graph.
            key=next(key for key in scene._modifier_render_cache if key[0]=='effect-tile'
                and key[-1]==tuple(bounds.getRect()) and len(key[1][2])==2)
            value=QImage(scene._modifier_render_cache[key])
            backing=cache_storage.PersistentRenderCache(tmp_path/'native-sector',contract=contract.signature)
            try:
                with backing.record():backing.retain('effect',key,value)
                backing.drain()
            finally:backing.close()
            reopened=cache_storage.PersistentRenderCache(tmp_path/'native-sector',contract=contract.signature)
            try:assert buffers(reopened.lookup('effect',key,wait=True))==buffers(value)
            finally:reopened.close()
            # Ordinary low byte budgets and a newer admission evict actual caches.
            scene._modifier_render_cache_budget=1024;scene._effect_jobs.retained_budget=1024
            pressure=QImage(16,16,QImage.Format_ARGB32_Premultiplied);pressure.fill(0xff234567)
            scene._modifier_cache_put(('native-sector-pressure',),pressure)
            scene._effect_jobs.retained_put(('native-sector-pressure',),('native-sector-pressure',),pressure,force=True)
            assert key not in scene._modifier_render_cache
            assert not any(entry[0]==key for entry in scene._effect_jobs.retained.values())
            patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache(budget=1))
            actual=render(regions[2]);expected,region=crop(oracle,bounds,regions[2])
            assert actual is not None and actual[1]==region and buffers(actual[0])==buffers(expected)
            assert trace==[((263,305,4),(0,0)),((263,305,4),(0,0))],trace
        assert buffers(source)==source_before and scene.chapter.to_dict()==model_before
    record_property('original_full_kernel_calls_cold_then_pressure',str(len(trace)))
    record_property('warm_additional_sector_calls','0')
    record_property('native_pixel_tolerance','exact complete buffers')

@pytest.mark.parametrize('variant',['generalized','anisotropic'])
def test_deferred_float_sector_frame_keeps_shared_owner_and_original_intermediates(scene,monkeypatch,variant):
    with pixel_scope(LEGACY_PIXELS):
        effects=modifiers(scene,variant,False);source=patterned();bounds=QRectF(-37,-61,305,263)
        mapping=QTransform().translate(13,22).rotate(19).scale(1.5,.5)
        oracle=original_full_oracle(scene,source,bounds,effects,mapping,True)
        scene._interactive_render=True;scene._effect_region_requests=True
        scene._projection_exact=True;scene._projection_defer_effects=True
        observed=[];original=scene._effect_jobs.request
        def request(scope,key,compute,*args,**kwargs):
            if key[0]=='effect-tile' and len(key[1][2])==2:observed.append((scope,key))
            return original(scope,key,compute,*args,**kwargs)
        monkeypatch.setattr(scene._effect_jobs,'request',request)
        monkeypatch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
        scope=('object','deferred-sector-native','canvas');required=QRectF(128,12,140,190)
        def render():return tile_output(scene,source,bounds,effects,mapping,required=required,
            request_scope=scope,source_identity=('deferred-native-source',variant),float_pipeline=True)
        with pytest.raises(ProjectionPending):render()
        deadline=time.monotonic()+30
        while time.monotonic()<deadline:
            for work in scene._effect_jobs.running_jobs:work[3].result(timeout=10)
            scene._effect_jobs.poll()
            try:actual=render();break
            except ProjectionPending:pass
        else:pytest.fail('Shared full-frame sector did not finish')
        expected,region=crop(oracle,bounds,required)
        assert actual is not None and actual[1]==region and buffers(actual[0])==buffers(expected)
        assert len(observed)==1 and observed[0][0]==('tile-graph',scope,(2,'frame'))
        assert observed[0][1][-1]==tuple(bounds.getRect())

@pytest.mark.parametrize('contract',[LEGACY_PIXELS,F16,FLOAT_PIXELS],ids=['byte','float16','float32'])
@pytest.mark.parametrize('kind',['effect','retained','projection'])
def test_shared_renderer_epoch_rejects_old_sector_pixels_without_deleting_sources_or_cache_files(
        tmp_path,monkeypatch,contract,kind):
    with pixel_scope(contract):
        source=patterned(17,13);before=buffers(source)
        reference=apply_modifier_stack(source,[KuwaharaModifier(variant='generalized',size=3)],(0,0),_point_lut=False)
    key=('native-sector-original-reference',kind,contract.signature)
    with monkeypatch.context() as patch:
        patch.setattr(cache_storage,'RENDERER_VERSION',OLD_RENDERER)
        old=cache_storage.PersistentRenderCache(tmp_path,contract=contract.signature)
        old_identity=old.descriptor(kind,key).identity
        try:
            with old.record():old.retain(kind,key,source)
            old.drain();assert buffers(old.lookup(kind,key,wait=True))==before
        finally:old.close()
    files={p.relative_to(tmp_path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
        for p in tmp_path.rglob('*') if p.is_file()}
    current=cache_storage.PersistentRenderCache(tmp_path,contract=contract.signature)
    try:
        assert cache_storage.RENDERER_VERSION!=OLD_RENDERER
        assert current.descriptor(kind,key).identity!=old_identity
        assert not current.entries and current.lookup(kind,key,wait=True) is None
        assert {p.relative_to(tmp_path).as_posix():hashlib.sha256(p.read_bytes()).hexdigest()
            for p in tmp_path.rglob('*') if p.is_file()}==files
        with current.record():current.retain(kind,key,reference)
        current.drain();assert buffers(current.lookup(kind,key,wait=True))==buffers(reference)
        assert json.loads((tmp_path/'index.json').read_text())['renderer']==cache_storage.RENDERER_VERSION
    finally:current.close()
    assert buffers(source)==before
