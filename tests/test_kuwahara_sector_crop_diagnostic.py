"""Root-only diagnostic of exact full/cropped sector samples and matmul moments.

No renderer acceptance, formula replacement, source mutation or old-test edits.
The read-only weight view delegates one ordinary ndarray matmul; both instrumented
outputs must remain byte-exact to their uninstrumented actual pipeline controls.
"""
import copy
import hashlib
import inspect
import json
import os
from pathlib import Path
import threading

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage,QTransform

from comic_editor.core import kuwahara
from comic_editor.core.models import BrightnessContrastModifier,HueSaturationLightnessModifier,KuwaharaModifier
from comic_editor.render.effect_pipeline import render_stages
from comic_editor.render.modifier_rendering import apply_modifier_stack
from comic_editor.render.tile_effects import tile_output
from test_effect_regions import scene,image,crop,register
from test_tile_graph_effects import enable

MAX_PROBES=12
MAX_RECORDS=128
MAX_REPORT_BYTES=2*1024*1024

def buffers(value):
    return value.size(),value.format(),value.bytesPerLine(),bytes(value.constBits())

def differing_pixels(actual,expected):
    assert actual.size()==expected.size() and actual.format()==expected.format()
    assert actual.format()==QImage.Format_ARGB32_Premultiplied
    a=np.ndarray((actual.height(),actual.bytesPerLine()),np.uint8,buffer=actual.constBits())[:,:actual.width()*4].reshape(actual.height(),actual.width(),4)
    b=np.ndarray((expected.height(),expected.bytesPerLine()),np.uint8,buffer=expected.constBits())[:,:expected.width()*4].reshape(expected.height(),expected.width(),4)
    positions=np.argwhere(np.any(a!=b,axis=2))
    return positions,int(np.count_nonzero(a!=b)),int(np.max(np.abs(a.astype(np.int16)-b.astype(np.int16))))

@pytest.mark.parametrize('mode',['byte_generalized','byte_anisotropic','float_generalized'])
def test_actual_sector_samples_distinguish_matmul_shape_from_halo_or_field(
        scene,monkeypatch,tmp_path,record_property,mode):
    sector=KuwaharaModifier(variant='anisotropic' if mode=='byte_anisotropic' else 'generalized',size=3)
    modifiers=[BrightnessContrastModifier(brightness=12),sector,HueSaturationLightnessModifier(hue=30)]
    register(scene,modifiers)
    source,bounds=image(),QRectF(-37,-61,530,403)
    float_pipeline=mode=='float_generalized'
    mapping=(QTransform().translate(13,22).rotate(19).scale(1.5,.5) if float_pipeline else QTransform())
    requested=(QRectF(-25,-45,280,310) if float_pipeline else QRectF(-50,-35,280,199))
    before_model=copy.deepcopy(scene.chapter.to_dict());before_source=buffers(source)
    def render_full(serial):
        if float_pipeline:
            return apply_modifier_stack(source,modifiers,mapping.map(bounds.topLeft()).toTuple()),bounds
        return render_stages(scene,source,bounds,modifiers,mapping,tile_evaluation=False,
            source_key=('sector-causal-diagnostic',serial),request_scope=('object',str(serial),'canvas'))
    def render_region(serial):
        if float_pipeline:
            result=tile_output(scene,source,bounds,modifiers,mapping,required=requested,
                source_identity=('sector-causal-diagnostic',serial),request_scope=('object',str(serial),'canvas'),float_pipeline=True)
            assert result is not None
            return result
        return render_stages(scene,source,bounds,modifiers,mapping,required=requested,
            source_key=('sector-causal-diagnostic',serial),request_scope=('object',str(serial),'canvas'))
    # A fresh ordinary core result cache each phase ensures the actual kernel is
    # observed, without changing input values, sample grids or formulas.
    with monkeypatch.context() as patch:
        patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
        full,frame=render_full('plain-full')
    enable(scene)
    with monkeypatch.context() as patch:
        patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
        regional,region=render_region('plain')
    expected,expected_region=crop(full,frame,requested)
    assert region==expected_region
    positions,channels,max_byte=differing_pixels(regional,expected)
    probes=[]
    if len(positions):
        indices=np.linspace(0,len(positions)-1,min(MAX_PROBES,len(positions)),dtype=int)
        probes=[(int(positions[i,1]+region.x()-bounds.x()),int(positions[i,0]+region.y()-bounds.y())) for i in indices]
    else:
        probes=[(int(region.center().x()-bounds.x()),int(region.center().y()-bounds.y()))]
    records=[];state=threading.local();phase=['unset'];dropped=[0]
    original_kernel=kuwahara._sector_kernel;original_sectors=kuwahara._sectors
    class ObservedWeights(np.ndarray):
        def __matmul__(self,other):
            result=np.asarray(self) @ other
            caller=inspect.currentframe().f_back
            assert caller.f_code is original_sectors.__code__
            local=caller.f_locals;ctx=state.context
            top,left,bottom,right=(local[n] for n in ('top','left','bottom','right'))
            h,w=bottom-top,right-left
            samples=other.reshape(local['sx'].size,h,w,7)
            moments=result.reshape(8,h,w,7)
            for px,py in probes:
                ix,iy=int(px-ctx['origin'][0]-left),int(py-ctx['origin'][1]-top)
                if not (0<=ix<w and 0<=iy<h):continue
                if len(records)>=MAX_RECORDS:
                    dropped[0]+=1;continue
                sample=np.ascontiguousarray(samples[:,iy,ix]);moment=np.ascontiguousarray(moments[:,iy,ix])
                target=ctx['output_rect']
                used=(target is None or (target[0]<=bounds.x()+px<target[0]+target[2]
                    and target[1]<=bounds.y()+py<target[1]+target[3]))
                records.append(dict(phase=phase[0],probe=[px,py],applied_to_output=used,
                    actual_sector_output_rect=target,rgba_shape=list(ctx['shape']),origin=list(ctx['origin']),
                    block=[left,top,w,h],matmul_shape=[list(self.shape),list(other.shape)],
                    weights_sha256=hashlib.sha256(np.asarray(self).tobytes()).hexdigest(),
                    samples_sha256=hashlib.sha256(sample.tobytes()).hexdigest(),samples_hex=sample.tobytes().hex(),
                    moments_sha256=hashlib.sha256(moment.tobytes()).hexdigest(),moments_hex=moment.tobytes().hex()))
            return result
    def observed_kernel(*args):
        sx,sy,weights=original_kernel(*args)
        result=weights.view(ObservedWeights)
        assert not result.flags.writeable and np.shares_memory(result,weights)
        return sx,sy,result
    def observed_sectors(rgba,size,modifier,cancelled=None,origin=(0,0)):
        previous=getattr(state,'context',None)
        output_rect=None;caller=inspect.currentframe().f_back
        for _ in range(12):
            if caller is None:break
            if (caller.f_globals.get('__name__')=='comic_editor.render.tile_effects'
                and caller.f_code.co_name=='compute' and 'output' in caller.f_locals):
                output_rect=list(caller.f_locals['output'].getRect());break
            caller=caller.f_back
        state.context=dict(shape=rgba.shape,origin=tuple(origin),output_rect=output_rect)
        try:return original_sectors(rgba,size,modifier,cancelled,origin)
        finally:state.context=previous
    with monkeypatch.context() as patch:
        patch.setattr(kuwahara,'_sector_kernel',observed_kernel)
        patch.setattr(kuwahara,'_sectors',observed_sectors)
        phase[0]='full'
        patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
        spied_full,spied_frame=render_full('spied-full')
        phase[0]='regional'
        patch.setattr(kuwahara,'_cache',kuwahara.KuwaharaCache())
        spied_region,spied_region_bounds=render_region('spied')
    assert spied_frame==frame and buffers(spied_full)==buffers(full)
    assert spied_region_bounds==region and buffers(spied_region)==buffers(regional)
    assert scene.chapter.to_dict()==before_model and buffers(source)==before_source
    pairs=[]
    for probe in probes:
        a=[r for r in records if r['phase']=='full' and tuple(r['probe'])==probe]
        b=[r for r in records if r['phase']=='regional' and tuple(r['probe'])==probe and r['applied_to_output']]
        assert a and b,('Missing actual kernel evidence',probe)
        for reference in a:
            for candidate in b:
                pairs.append(dict(probe=list(probe),full_block=reference['block'],regional_block=candidate['block'],
                    same_weights=reference['weights_sha256']==candidate['weights_sha256'],
                    same_samples=reference['samples_sha256']==candidate['samples_sha256'],
                    same_moments=reference['moments_sha256']==candidate['moments_sha256'],
                    full_matmul_shape=reference['matmul_shape'],regional_matmul_shape=candidate['matmul_shape']))
    report=dict(diagnostic_only=True,renderer_acceptance=False,mode=mode,variant=sector.variant,
        original_pixel_mismatch_count=len(positions),original_differing_channels=channels,original_max_byte_difference=max_byte,
        original_buffer_assertions_untouched=True,spied_full_and_regional_exactly_equal_plain_controls=True,
        source_and_model_unchanged=True,mask_bindings=sector.parameter_masks,probes=probes,records=records,pairs=pairs,
        records_dropped=dropped[0],bounded=dict(probes=MAX_PROBES,records=MAX_RECORDS,report_bytes=MAX_REPORT_BYTES),
        thread_environment={key:os.environ.get(key) for key in ('OPENBLAS_NUM_THREADS','OMP_NUM_THREADS','MKL_NUM_THREADS')},
        native_density={'canvas':scene.devicePixelRatioF()},
        interpretation='Identical weights/sampled vectors with different moments and matrix column shape supports BLAS numerical partitioning; different samples keep frame/halo/interpolation investigation open. Pairs include only regional computations whose actual output rectangle paints the native mismatching probe; raw halo records are separately labeled.')
    raw=json.dumps(report,indent=2).encode();assert len(raw)<=MAX_REPORT_BYTES and not dropped[0]
    path=tmp_path/f'kuwahara-sector-{mode}.json'
    with path.open('xb') as stream:stream.write(raw)
    record_property('diagnostic_only','true');record_property('sector_sample_report',str(path.resolve()))
    record_property('sector_sample_report_sha256',hashlib.sha256(raw).hexdigest())
