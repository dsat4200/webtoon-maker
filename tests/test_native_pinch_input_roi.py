"""Native Pinch ROI contracts against a fixed portable pre-change oracle."""
from functools import lru_cache
import hashlib
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import DistortModifier, HueSaturationLightnessModifier, ParameterMaskBinding, OutlineModifier
from comic_editor.core.pixel_contract import PixelContract
from comic_editor.render.pixels import LEGACY_PIXELS, pixel_scope, working_image
from comic_editor.ui import distort_rendering as kernel
from comic_editor.render.effect_pipeline import render_stages
from comic_editor.render.tile_effects import tile_output
from test_effect_regions import scene, register


@lru_cache(maxsize=1)
def original_kernel():
    # This is an independently pinned immutable pre-change implementation,
    # never a candidate helper used to derive its own expected output.
    path=Path(__file__).resolve().parent/'references/pinch_native_r11.py'
    assert hashlib.sha256(path.read_bytes()).hexdigest()=='083f17ef8f8737daa325cb659ae9ecb239747e6d8bd287eb05c1f1fc9e5152cb'
    name='pinch_roi_independent_original_kernel'
    spec=importlib.util.spec_from_file_location(name,path)
    module=importlib.util.module_from_spec(spec)
    sys.modules[name]=module
    spec.loader.exec_module(module)
    return module


def native_source(width=768,height=768):
    image=QImage(width,height,QImage.Format_ARGB32_Premultiplied)
    data=np.frombuffer(image.bits(),np.uint8).reshape(height,image.bytesPerLine())
    pixels=data[:,:width*4].reshape(height,width,4)
    yy,xx=np.mgrid[:height,:width]
    alpha=(xx*7+yy*11+17)%256
    pixels[...,3]=alpha
    for channel,prime in enumerate((13,19,23)):
        pixels[...,channel]=((xx*prime+yy*(prime+6)+31)%256)*alpha//255
    return image


def effect(bounds,amount,edges='transparent',intensity=100.):
    value=DistortModifier(modifier_type='distort_pinch_punch',intensity=intensity,
        frame=bounds.getRect(),center=bounds.center().toTuple(),radius=min(bounds.width(),bounds.height())*.35)
    value.validate()
    value.parameters.update(amount=amount,interpolation='bilinear',edges=edges)
    return value


def assert_native_equal(actual,expected):
    assert actual.size()==expected.size()
    assert actual.format()==expected.format()
    assert actual.bytesPerLine()==expected.bytesPerLine()
    assert actual.devicePixelRatio()==expected.devicePixelRatio()==1.
    assert bytes(actual.constBits())==bytes(expected.constBits())


def mix(source,source_bounds,warped,output,intensity):
    from comic_editor.render.effect_pipeline import empty_image
    from comic_editor.render.modifier_rendering import _qimage_premultiplied,_premultiplied_qimage
    base=empty_image(output)
    painter=QPainter(base)
    try:painter.drawImage(source_bounds.topLeft()-output.topLeft(),source)
    finally:painter.end()
    amount=np.array(intensity,dtype=np.float32,copy=True)
    amount/=100.
    return warped if float(amount)==1. else _premultiplied_qimage(_qimage_premultiplied(base)*(1.-amount)+_qimage_premultiplied(warped)*amount)


@pytest.mark.parametrize('kind',['distort_pinch_punch','distort_spherical'])
@pytest.mark.parametrize('amount',[-100.,65.])
@pytest.mark.parametrize('edges',['transparent','white'])
def test_default_full_input_keeps_held_native_kernel_bytes(kind,amount,edges):
    source=native_source(191,173);bounds=QRectF(-13,8192,191,173)
    output=QRectF(-37,8179,227,211)
    modifier=effect(bounds,amount,edges)
    modifier.modifier_type=kind;modifier.validate()
    modifier.parameters.update(amount=amount,interpolation='bilinear',edges=edges)
    actual=kernel.render_distort(source,bounds,modifier,QTransform(),output)
    expected=original_kernel().render_distort(source,bounds,modifier,QTransform(),output)
    assert_native_equal(actual,expected)


@pytest.mark.parametrize('origin',[(0,0),(-513,8192)])
@pytest.mark.parametrize('edges',['transparent','white'])
@pytest.mark.parametrize('amount',[-100.,-23.5,65.])
@pytest.mark.parametrize('placement',['identity','affine','projective'])
def test_crop_retains_original_global_native_kernel_and_partial_mix(origin,edges,amount,placement):
    source=native_source()
    bounds=QRectF(*origin,768,768)
    mapping=(QTransform() if placement=='identity' else
        QTransform().translate(13.375,22.625).rotate(19.25).scale(1.25,.73) if placement=='affine' else
        QTransform(1.1,.13,.000015,-.07,.9,-.000021,13.375,22.625,1.))
    modifier=effect(bounds,amount,edges)
    # Author the same original world-space rig for both independent kernels.
    modifier.frame=mapping.mapRect(bounds).getRect()
    modifier.center=mapping.map(bounds.center()).toTuple()
    output=QRectF(origin[0]+301,origin[1]+290,128,96)
    region=kernel.native_pinch_input_region(bounds,output,modifier,mapping)
    assert region is not None and region.width()*region.height()<bounds.width()*bounds.height()
    rect=QRectF(region).translated(-bounds.topLeft()).toAlignedRect()
    incoming=source.copy(rect)
    expected=original_kernel().render_distort(source,bounds,modifier,mapping,output)
    actual=kernel.render_distort(incoming,bounds,modifier,mapping,output,native_input_bounds=region)
    assert_native_equal(actual,expected)
    for intensity in (0.,100.,37.5):
        assert_native_equal(mix(incoming,region,actual,output,intensity),mix(source,bounds,expected,output,intensity))


@pytest.mark.parametrize('edges',['transparent','white'])
@pytest.mark.parametrize('which',['left','right'])
def test_partial_border_and_outside_global_frame_match_original(edges,which):
    source=native_source()
    bounds=QRectF(-513,-769,768,768)
    modifier=effect(bounds,-100.,edges)
    output=QRectF(-557,-477,96,128) if which=='left' else QRectF(211,-477,96,128)
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    actual=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,native_input_bounds=region)
    expected=original_kernel().render_distort(source,bounds,modifier,QTransform(),output)
    assert_native_equal(actual,expected)
    # The world rectangle still includes pixels beyond the ORIGINAL frame.
    assert not bounds.contains(output)


@pytest.mark.parametrize('edges',['transparent','white'])
def test_internal_crop_edge_is_not_an_original_frame_edge(edges):
    source=native_source()
    bounds=QRectF(-513,8192,768,768)
    output=QRectF(-213,8492,128,128)
    modifier=effect(bounds,-100.,edges)
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None and bounds.contains(region) and region!=bounds
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    # The valid omitted native pixels are opaque. The demanded taps remain
    # unchanged, so replacing every omitted pixel cannot alter this output.
    modified=QImage(source)
    painter=QPainter(modified)
    try:
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        painter.fillRect(modified.rect(),QColor('red'))
        painter.drawImage(region.topLeft()-bounds.topLeft(),incoming)
    finally:painter.end()
    expected=original_kernel().render_distort(source,bounds,modifier,QTransform(),output)
    assert_native_equal(original_kernel().render_distort(modified,bounds,modifier,QTransform(),output),expected)
    actual=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,native_input_bounds=region)
    assert_native_equal(actual,expected)
    # A crop stretched across the complete frame without its native offset
    # has a different sampling scale and must not satisfy this oracle.
    rephased=original_kernel().render_distort(incoming,bounds,modifier,QTransform(),output)
    assert bytes(rephased.constBits())!=bytes(expected.constBits())


@pytest.mark.parametrize('edges',['transparent','white'])
def test_empty_no_native_taps_retains_full_input_fallback(edges):
    bounds=QRectF(-513,-769,768,768)
    modifier=effect(bounds,-100.,edges)
    output=QRectF(4096,4096,128,128)
    assert kernel.native_pinch_input_region(bounds,output,modifier,QTransform()) is None
    source=native_source()
    assert_native_equal(kernel.render_distort(source,bounds,modifier,QTransform(),output),
        original_kernel().render_distort(source,bounds,modifier,QTransform(),output))


def test_fraction_uncertainty_and_small_budget_keep_original_full_requirement(monkeypatch):
    bounds=QRectF(-513,8192,768,768)
    modifier=effect(bounds,-100.)
    output=QRectF(-213,8492,128,128)
    assert kernel.native_pinch_input_region(bounds,output,modifier,QTransform(),budget=1) is None
    monkeypatch.setattr(kernel._StripBilinearSampler,'_same_native_coefficients',staticmethod(lambda *_:False))
    assert kernel.native_pinch_input_region(bounds,output,modifier,QTransform()) is None


@pytest.mark.parametrize('policy',['masked','cubic','repeat','mirror','clamp','fractional','nonfinite','large','singular'])
def test_unsupported_dependencies_keep_full_frame(policy):
    bounds=QRectF(-513,8192,768,768)
    modifier=effect(bounds,-100.)
    output=QRectF(-213,8492,128,128)
    mapping=QTransform()
    if policy=='masked':modifier.parameter_masks['intensity']=ParameterMaskBinding('mask',20,80)
    elif policy in ('cubic','repeat','mirror','clamp'):
        modifier.parameters['interpolation' if policy=='cubic' else 'edges']=policy
    elif policy=='fractional':bounds.translate(.25,0)
    elif policy=='nonfinite':modifier.parameters['amount']=float('nan')
    elif policy=='large':output=QRectF(-213,8492,1024,1024)
    else:mapping=QTransform(0,0,0,0,0,0,0,0,0)
    assert kernel.native_pinch_input_region(bounds,output,modifier,mapping) is None


@pytest.mark.parametrize('precision',['float16','float32'])
def test_native_float_paths_stay_on_unchanged_complete_source(precision):
    contract=PixelContract(version=2,working_space='linear_srgb',precision=precision)
    bounds=QRectF(-13,19,64,48)
    output=QRectF(0,32,32,24)
    modifier=effect(bounds,-73.5)
    with pixel_scope(contract):
        yy,xx=np.mgrid[:48,:64]
        alpha=((xx+yy)%17+1).astype(np.float32)/10000
        pixels=np.stack((alpha*2.5,alpha*.33,alpha*1.25,alpha),axis=-1)
        source=working_image(pixels)
        assert kernel.native_pinch_input_region(bounds,output,modifier,QTransform()) is None
        actual=kernel.render_distort(source,bounds,modifier,QTransform(),output)
        expected=original_kernel().render_distort(source,bounds,modifier,QTransform(),output)
        assert_native_equal(actual,expected)


@pytest.mark.parametrize('intensity',[100.,37.5])
@pytest.mark.parametrize('edges',['transparent','white'])
def test_actual_graph_prunes_upstream_lens_and_preserves_exact_pixels(scene,monkeypatch,intensity,edges):
    source=native_source(1536,1536)
    bounds=QRectF(-37,-61,1536,1536)
    lens=DistortModifier(modifier_type='distort_lens_distortion',frame=bounds.getRect(),
        center=bounds.center().toTuple())
    lens.validate();lens.parameters.update(amount=100.,interpolation='bilinear',edges=edges)
    pinch=effect(bounds,-100.,edges,intensity)
    modifiers=[lens,pinch,HueSaturationLightnessModifier(hue=37.25,lightness=-8.5),OutlineModifier(thickness=3)]
    register(scene,modifiers)
    scene._interactive_render=True;scene._effect_region_requests=True;scene._projection_exact=True
    scene._projection_defer_effects=False
    required=QRectF(100,140,300,280)
    kwargs=dict(required=required,request_scope=('object','native-pinch-roi','canvas'),source_identity=('native-pinch-same-pixels',source.cacheKey()))
    original=kernel.render_distort;calls=[]
    def counted(image,full,*args,**kwargs):
        modifier=args[0]
        if modifier.modifier_type=='distort_lens_distortion':calls.append(QRectF(args[2]))
        return original(image,full,*args,**kwargs)
    monkeypatch.setattr(kernel,'render_distort',counted)
    with monkeypatch.context() as patch:
        patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:None)
        def independent(image,full,*args,**kwargs):
            if args[0].modifier_type=='distort_lens_distortion':calls.append(QRectF(args[2]))
            return original_kernel().render_distort(image,full,*args,**kwargs)
        patch.setattr(kernel,'render_distort',independent)
        expected=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert expected is not None and calls
    old_count=len(calls)
    scene._modifier_render_cache.clear();scene._modifier_render_cache_bytes=0
    scene._modifier_source_cache.clear();scene._modifier_source_cache_bytes=0
    scene._effect_jobs.cancel()
    scene._distort_preparation_cache.clear()
    calls.clear()
    actual=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert actual is not None and calls
    assert actual[1]==expected[1]
    assert_native_equal(actual[0],expected[0])
    assert len(calls)<old_count  # Real original upstream kernel evaluations.
    assert scene._distort_preparation_cache.bytes<=scene._distort_preparation_cache.budget
    # A completed exact tile/output remains reusable under its ordinary key.
    calls.clear()
    again=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert_native_equal(again[0],actual[0])
    assert not calls


@pytest.mark.parametrize('intensity',[100.,37.5])
@pytest.mark.parametrize('edges',['transparent','white'])
def test_small_frame_full_base_coverage_requires_all_original_upstream_tiles(scene,monkeypatch,intensity,edges):
    source=native_source()
    bounds=QRectF(-37,-61,768,768)
    lens=DistortModifier(modifier_type='distort_lens_distortion',frame=bounds.getRect(),
        center=bounds.center().toTuple())
    lens.validate();lens.parameters.update(amount=100.,interpolation='bilinear',edges=edges)
    pinch=effect(bounds,-100.,edges,intensity)
    modifiers=[lens,pinch,HueSaturationLightnessModifier(hue=37.25,lightness=-8.5),OutlineModifier(thickness=3)]
    register(scene,modifiers)
    scene._interactive_render=True;scene._effect_region_requests=True;scene._projection_exact=True
    scene._projection_defer_effects=False
    required=QRectF(100,140,300,280)
    kwargs=dict(required=required,request_scope=('object','native-pinch-roi','canvas'),source_identity=('native-pinch-same-pixels',source.cacheKey()))
    original=kernel.render_distort;calls=[]
    def counted(image,full,*args,**kwargs):
        modifier=args[0]
        if modifier.modifier_type=='distort_lens_distortion':calls.append(QRectF(args[2]))
        return original(image,full,*args,**kwargs)
    monkeypatch.setattr(kernel,'render_distort',counted)
    with monkeypatch.context() as patch:
        patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:None)
        def independent(image,full,*args,**kwargs):
            if args[0].modifier_type=='distort_lens_distortion':calls.append(QRectF(args[2]))
            return original_kernel().render_distort(image,full,*args,**kwargs)
        patch.setattr(kernel,'render_distort',independent)
        expected=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert expected is not None and calls
    old_count=len(calls)
    scene._modifier_render_cache.clear();scene._modifier_render_cache_bytes=0
    scene._modifier_source_cache.clear();scene._modifier_source_cache_bytes=0
    scene._effect_jobs.cancel()
    scene._distort_preparation_cache.clear()
    calls.clear()
    actual=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert actual is not None and calls
    assert actual[1]==expected[1]
    assert_native_equal(actual[0],expected[0])
    assert len(calls)==old_count==16  # Every native source tile is genuinely required.
    assert scene._distort_preparation_cache.bytes<=scene._distort_preparation_cache.budget
    # A completed exact tile/output remains reusable under its ordinary key.
    calls.clear()
    again=tile_output(scene,source,bounds,modifiers,QTransform(),**kwargs)
    assert_native_equal(again[0],actual[0])
    assert not calls


@pytest.mark.parametrize('intensity',[0.,.0001,37.5,100.,-15.,115.])
@pytest.mark.parametrize('edges',['transparent','white'])
def test_original_stage_helper_owns_validated_intensity_mix(scene,monkeypatch,intensity,edges):
    from comic_editor.render.effect_pipeline import empty_image
    from comic_editor.ui.distort_pipeline import render_distort_stage
    source=native_source();bounds=QRectF(-513,8192,768,768)
    output=QRectF(-213,8492,128,128)
    modifier=effect(bounds,-100.,edges,intensity)
    assert modifier.intensity==min(100.,max(0.,intensity))
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    base=empty_image(output)
    painter=QPainter(base)
    try:painter.drawImage(bounds.topLeft()-output.topLeft(),source)
    finally:painter.end()
    scene._projection_exact=True;scene._projection_defer_effects=False
    with monkeypatch.context() as patch:
        patch.setattr(kernel,'render_distort',original_kernel().render_distort)
        expected,provisional=render_distort_stage(scene,source,base,bounds,output,
            modifier,QTransform(),{},('independent-original-full-frame',),None,False,False)
    assert not provisional
    warped=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,native_input_bounds=region)
    assert_native_equal(mix(incoming,region,warped,output,modifier.intensity),expected)


@pytest.mark.parametrize('intensity',[float('nan'),float('inf'),-float('inf')])
def test_nonfinite_intensity_is_rejected_by_the_existing_model(intensity):
    value=DistortModifier(modifier_type='distort_pinch_punch',intensity=intensity)
    with pytest.raises(ValueError,match='finite'):value.validate()


@pytest.mark.parametrize('cancel_at',[1,2,3])
def test_crop_kernel_keeps_original_cancellation_result(cancel_at):
    source=native_source();bounds=QRectF(-513,8192,768,768)
    output=QRectF(-213,8400,128,280)
    modifier=effect(bounds,-100.)
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    def cancellation():
        count=0
        def cancelled():
            nonlocal count
            count+=1
            return count>=cancel_at
        return cancelled
    actual=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,
        cancellation(),native_input_bounds=region)
    expected=original_kernel().render_distort(source,bounds,modifier,QTransform(),output,cancellation())
    assert actual is expected is None


def clear_native_memory(scene):
    scene._modifier_render_cache.clear();scene._modifier_render_cache_bytes=0
    scene._modifier_source_cache.clear();scene._modifier_source_cache_bytes=0
    scene._effect_jobs.cancel()
    scene._distort_preparation_cache.clear()


def test_old_exact_disk_key_reuse_and_changed_upstream_dependency(scene,monkeypatch,tmp_path):
    from comic_editor.render.cache import PersistentRenderCache
    source=native_source();bounds=QRectF(-37,-61,768,768)
    lens=DistortModifier(modifier_type='distort_lens_distortion',frame=bounds.getRect(),
        center=bounds.center().toTuple())
    lens.validate();lens.parameters.update(amount=85.,interpolation='bilinear',edges='white')
    pinch=effect(bounds,-100.,'white',37.5)
    modifiers=[lens,pinch,HueSaturationLightnessModifier(hue=37.25),OutlineModifier(thickness=3)]
    register(scene,modifiers)
    scene._interactive_render=True;scene._effect_region_requests=True
    scene._projection_exact=True;scene._projection_defer_effects=False
    args=dict(required=QRectF(100,140,300,280),request_scope=('object','same-native-disk','canvas'),
        source_identity=('original-native-source',hashlib.sha256(bytes(source.constBits())).hexdigest()))
    backing=PersistentRenderCache(tmp_path/'native-cache')
    scene._persistent_render_cache=backing
    try:
        with monkeypatch.context() as patch:
            patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:None)
            patch.setattr(kernel,'render_distort',original_kernel().render_distort)
            with backing.record():expected=tile_output(scene,source,bounds,modifiers,QTransform(),**args)
        backing.drain()
        assert expected is not None and backing.entries
        # The original synchronous render can fill the bounded write queue
        # before its final output. Establish that precise entry explicitly:
        # drain outside rendering, then visit the already completed original
        # memory result through ordinary retained_get/cache_put recording.
        output_scope=('tile-graph',args['request_scope'],
            ('output',tuple(expected[1].getRect())))
        output_entry=scene._effect_jobs.retained.get(output_scope)
        assert output_entry is not None
        output_key=output_entry[0]
        assert output_key[0]=='tile-output'
        with monkeypatch.context() as patch:
            patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:
                pytest.fail('Original completed memory output was unavailable for recording'))
            patch.setattr(kernel,'render_distort',lambda *_a,**_k:
                pytest.fail('Original completed memory output recomputed during recording'))
            with backing.record():
                # Incoming InlineResults RAM hits do not re-record an image.
                # Feed this independently completed original checkpoint into
                # its existing admission/writer API, retaining the same state.
                assert scene._effect_jobs.retained_put(output_scope,output_key,
                    output_entry[1],output_entry[2])
                recorded=tile_output(scene,source,bounds,modifiers,QTransform(),**args)
        assert recorded[1]==expected[1]
        assert_native_equal(recorded[0],expected[0])
        backing.drain()
        assert backing.has('retained',('retained',output_scope,output_key),verify=False)
        stored=set(backing.entries)
        clear_native_memory(scene)
        backing.close()
        backing=PersistentRenderCache(tmp_path/'native-cache',contract=backing.contract,environment=backing.environment)
        scene._persistent_render_cache=backing
        assert set(backing.entries)==stored
        with monkeypatch.context() as patch:
            patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:pytest.fail('Old completed semantic output was not reused'))
            patch.setattr(kernel,'render_distort',lambda *_a,**_k:pytest.fail('Old exact native output recomputed'))
            actual=tile_output(scene,source,bounds,modifiers,QTransform(),**args)
        assert actual[1]==expected[1];assert_native_equal(actual[0],expected[0])
        # The existing full prefix signature still covers every Lens setting;
        # a smaller demanded crop never makes the native dependency narrower.
        lens.parameters['amount']=-65.
        changed=tile_output(scene,source,bounds,modifiers,QTransform(),**args)
        assert bytes(changed[0].constBits())!=bytes(expected[0].constBits())
        clear_native_memory(scene)
        with monkeypatch.context() as patch:
            patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:None)
            patch.setattr(kernel,'render_distort',original_kernel().render_distort)
            fresh=tile_output(scene,source,bounds,modifiers,QTransform(),**args)
        assert_native_equal(changed[0],fresh[0])
        assert set(backing.entries)==stored  # Read-only reuse, no recording scope.
    finally:
        scene._persistent_render_cache=None
        backing.close()


def test_original_crop_ownership_and_source_mutation_change_current_native():
    source=native_source();old=QImage(source)
    bounds=QRectF(-513,8192,768,768);output=QRectF(-213,8492,128,128)
    modifier=effect(bounds,-100.)
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    cache=kernel.PreparedDistortCache()
    source.fill(QColor('blue'))
    actual=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,
        preparation_cache=cache,native_input_bounds=region)
    expected=original_kernel().render_distort(old,bounds,modifier,QTransform(),output)
    assert_native_equal(actual,expected)
    current=kernel.render_distort(source,bounds,modifier,QTransform(),output)
    assert bytes(current.constBits())!=bytes(actual.constBits())
    assert 0<cache.bytes<=cache.budget


@pytest.mark.parametrize('change',['source','mapping','center'])
def test_current_graph_dependencies_change_native_and_match_fresh_original(scene,monkeypatch,change):
    source=native_source();bounds=QRectF(-37,-61,768,768)
    pinch=effect(bounds,-100.,'transparent',37.5)
    modifiers=[pinch,HueSaturationLightnessModifier(hue=37.25),OutlineModifier(thickness=3)]
    register(scene,modifiers)
    scene._interactive_render=True;scene._effect_region_requests=True
    scene._projection_exact=True;scene._projection_defer_effects=False
    mapping=QTransform()
    def capture():
        return tile_output(scene,source,bounds,modifiers,mapping,required=QRectF(100,140,300,280),
            request_scope=('object','current-native-dependencies','canvas'),
            source_identity=('original-native-source',hashlib.sha256(bytes(source.constBits())).hexdigest()))
    before=capture()
    assert before is not None
    if change=='source':source.fill(QColor('blue'))
    elif change=='mapping':mapping.translate(23.375,-19.625)
    else:pinch.center=(pinch.center[0]+29.375,pinch.center[1]-17.625)
    current=capture()
    assert current is not None and bytes(current[0].constBits())!=bytes(before[0].constBits())
    clear_native_memory(scene)
    with monkeypatch.context() as patch:
        patch.setattr(kernel,'native_pinch_input_region',lambda *_a,**_k:None)
        patch.setattr(kernel,'render_distort',original_kernel().render_distort)
        fresh=capture()
    assert current[1]==fresh[1];assert_native_equal(current[0],fresh[0])


@pytest.mark.parametrize('edges',['transparent','white'])
def test_original_oversized_native_strip_source_matches_small_demand(edges,monkeypatch):
    width,height=4103,4105
    source=QImage(width,height,QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(160,40,130,90))
    painter=QPainter(source)
    try:painter.drawImage(1750,1750,native_source())
    finally:painter.end()
    bounds=QRectF(-2160,15611,width,height)
    output=QRectF(-140,17591,128,96)
    modifier=effect(bounds,-100.,edges)
    region=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert region is not None and region.width()*region.height()<width*height
    incoming=source.copy(QRectF(region).translated(-bounds.topLeft()).toAlignedRect())
    reference=original_kernel();observed=[]
    prepare=reference._oversized_bilinear_sampler
    def source_sampler(*args,**kwargs):
        sampler=prepare(*args,**kwargs)
        observed.append(type(sampler))
        return sampler
    with monkeypatch.context() as patch:
        patch.setattr(reference,'_oversized_bilinear_sampler',source_sampler)
        expected=reference.render_distort(source,bounds,modifier,QTransform(),output,
            preparation_cache=reference.PreparedDistortCache())
    assert observed==[reference._StripBilinearSampler]
    cache=kernel.PreparedDistortCache()
    actual=kernel.render_distort(incoming,bounds,modifier,QTransform(),output,
        preparation_cache=cache,native_input_bounds=region)
    assert_native_equal(actual,expected)
    assert 0<cache.bytes<=cache.budget


@pytest.mark.parametrize('edges',['transparent','white'])
def test_explicit_bad_crop_cannot_turn_omitted_native_pixels_into_edge_fill(edges):
    source=native_source();bounds=QRectF(-513,8192,768,768)
    output=QRectF(-213,8492,128,128)
    modifier=effect(bounds,-100.,edges)
    complete=kernel.native_pinch_input_region(bounds,output,modifier,QTransform())
    assert complete is not None and complete.width()>4 and complete.height()>4
    incomplete=QRectF(complete.x(),complete.y(),1,1)
    incoming=source.copy(QRectF(incomplete).translated(-bounds.topLeft()).toAlignedRect())
    with pytest.raises(ValueError,match='omitted original native taps'):
        kernel.render_distort(incoming,bounds,modifier,QTransform(),output,native_input_bounds=incomplete)


@pytest.mark.parametrize('policy',['large_output','fractional_output'])
def test_explicit_crop_cannot_bypass_bounded_native_output_admission(policy):
    source=native_source();bounds=QRectF(-513,8192,768,768)
    output=(QRectF(-213,8492,513,513) if policy=='large_output' else
        QRectF(-213.25,8492,128,128))
    with pytest.raises(ValueError,match='Unsupported native Pinch input crop'):
        kernel.render_distort(source,bounds,effect(bounds,-100.),QTransform(),output,native_input_bounds=bounds)
