"""Prospective bit-level SciPy/native oracles; no tolerance or pixel rebasing."""
import hashlib
import importlib.util
from pathlib import Path
from dataclasses import replace
from threading import Event

import numpy as np
import pytest
from PySide6.QtGui import QTransform
from scipy.ndimage import map_coordinates
from test_effect_regions import scene

from comic_editor.render.pixels import LEGACY_PIXELS, FLOAT_PIXELS, pixel_scope, working_image
from comic_editor.ui.modifier_rendering import _qimage_premultiplied

HERE=Path(__file__).resolve().parent


def load(name, filename, expected):
    path=HERE/filename
    assert hashlib.sha256(path.read_bytes()).hexdigest()==expected
    spec=importlib.util.spec_from_file_location(name,path)
    value=importlib.util.module_from_spec(spec);spec.loader.exec_module(value)
    assert Path(value.__file__).resolve()==path.resolve()
    return value


@pytest.fixture(scope='module')
def candidate():
    import comic_editor
    from comic_editor.ui import radial_blur
    assert Path(radial_blur.__file__).resolve().is_relative_to(Path(comic_editor.__file__).resolve().parent)
    return radial_blur


@pytest.fixture(scope='module')
def reference():
    return load('_shared_rgba_original_r12','radial_blur_native_reference.py',
        '3c6a4ea010b0630c092bba3ca0be9e39ceae4fd8fe6221a6d3f92d40af57f9e3')


CONTRACTS=[LEGACY_PIXELS,replace(FLOAT_PIXELS,precision='float16'),FLOAT_PIXELS]


@pytest.fixture
def owned_component(candidate):
    from scipy import __version__
    if candidate._sampler_library() is None or __version__ != '1.18.0':
        assert not candidate._shared_rgba_input(pixels())
        pytest.skip('Optional verified Windows AMD64/SciPy1.18 component unavailable; ordinary SciPy lane remains tested')



def pixels(width=67,height=55):
    rng=np.random.default_rng(420317)
    values=rng.random((height,width,4),dtype=np.float32)
    values[...,:3]*=values[...,3:4]
    values[0,0]=0.;values[0,0,0]=np.float32(-0.)
    values[1,1,3]=np.float32(2**-23);values[1,1,:3]=values[1,1,3]*np.array([.1,.5,1.],np.float32)
    if min(height,width)>2:
        values[2,2,:3]=values[2,2,3]*np.array([-.25,2.5,.4],dtype=np.float32)
    return values


def raw(array):
    assert array.dtype==np.float32
    return np.ascontiguousarray(array).view(np.uint32)


def scipy_rgba(source,coords):
    return np.stack([map_coordinates(source[...,channel],coords,order=1,
        mode='grid-constant',cval=0.,prefilter=False) for channel in range(4)],axis=-1)


@pytest.mark.parametrize('contract',CONTRACTS)
@pytest.mark.parametrize('size',[(1,1),(1,67),(55,1),(55,67)])
@pytest.mark.parametrize('coordinates',['boundary','random'])
def test_shared_sample_all_float_bits_and_native_input_unchanged(candidate,contract,size,coordinates):
    height,width=size
    initial=pixels(max(2,width),max(2,height))[:height,:width].copy()
    if height==width==1:
        initial[0,0]=np.array([.125,.25,.375,.5],dtype=np.float32)
    with pixel_scope(contract):
        native=working_image(initial,contract)
        source=_qimage_premultiplied(native)
    before=source.tobytes();eligible=candidate._shared_rgba_input(source)
    from scipy import __version__
    assert eligible == (candidate._sampler_library() is not None and __version__ == '1.18.0')
    if coordinates=='random':
        rng=np.random.default_rng(8391)
        coords=np.stack((rng.uniform(-3,height+3,(31,29)),rng.uniform(-3,width+3,(31,29))))
    else:
        def edges(length):
            basic=np.array([-2.,-1.,-.5,-0.,0.,.5,length-1.,length-.5,float(length),length+1.])
            return np.concatenate((basic,np.nextafter(basic,-np.inf),np.nextafter(basic,np.inf)))
        yy,xx=np.meshgrid(edges(height),edges(width),indexing='ij');coords=np.stack((yy,xx))
    actual=candidate._rgba_linear_sample(source,coords,eligible)
    expected=scipy_rgba(source,coords)
    np.testing.assert_array_equal(raw(actual),raw(expected))
    assert source.tobytes()==before


@pytest.mark.parametrize('reason',['nan-coordinate','positive-inf-coordinate','negative-inf-coordinate',
    'huge-coordinate','oversized-coordinate','nonfinite-source','float64-source','strided-source','unknown-version'])
def test_unsupported_sample_preserves_original_calls_and_raw_results(candidate,monkeypatch,reason):
    import scipy
    source=pixels();coords=np.zeros((2,17,19),dtype=np.float64)
    if reason=='nan-coordinate':coords[0,2,3]=np.nan
    elif reason=='positive-inf-coordinate':coords[0,2,3]=np.inf
    elif reason=='negative-inf-coordinate':coords[1,2,3]=-np.inf
    elif reason=='huge-coordinate':coords[1,2,3]=2**53
    elif reason=='oversized-coordinate':coords=np.zeros((2,97,97),dtype=np.float64)
    elif reason=='nonfinite-source':
        source[2,3,0]=np.nan;coords[:,2,3]=[2.,3.]
    elif reason=='float64-source':source=source.astype(np.float64)
    elif reason=='strided-source':source=source[:,::-1]
    elif reason=='unknown-version':monkeypatch.setattr(scipy,'__version__','unknown-untested')
    calls=[];original=candidate.map_coordinates
    def ordinary(*args,**kwargs):
        calls.append(1);return original(*args,**kwargs)
    monkeypatch.setattr(candidate,'map_coordinates',ordinary)
    expected=scipy_rgba(source,coords)
    actual=candidate._rgba_linear_sample(source,coords,candidate._shared_rgba_input(source))
    assert calls==[1,1,1,1]
    assert actual.dtype==expected.dtype and actual.shape==expected.shape
    assert actual.tobytes()==expected.tobytes()


@pytest.mark.parametrize('contract',CONTRACTS)
@pytest.mark.parametrize('mapping_kind',['identity','affine','negative-origin','projective'])
@pytest.mark.parametrize('field',[False,True])
def test_complete_native_global_blocks_angles_and_conversion_equal_original(candidate,reference,contract,mapping_kind,field):
    with pixel_scope(contract):
        native=working_image(pixels(),contract)
        source=_qimage_premultiplied(native)
        original_pixels=source.tobytes()
        origin=(-17.,-11.) if mapping_kind=='negative-origin' else (0.,0.)
        mapping={'identity':QTransform(),'affine':QTransform(1.,.13,-.17,1.,-2.25,3.75),
            'negative-origin':QTransform.fromTranslate(-7.,-5.),
            'projective':QTransform(1.,.03,.0004,-.04,1.,-.0003,2.5,-3.25,1.)}[mapping_kind]
        shape=(103,137)
        angle=np.broadcast_to(np.linspace(0.,9.,shape[1],dtype=np.float32),shape).copy() if field else 5.
        common=dict(output_shape=shape,output_origin=origin)
        expected=reference.radial_blur(source,(27.,19.),angle,mapping,**common)
        actual=candidate.radial_blur(source,(27.,19.),angle,mapping,**common)
        np.testing.assert_array_equal(raw(actual),raw(expected))
        actual_image=working_image(actual,contract);expected_image=working_image(expected,contract)
        assert actual_image.format()==expected_image.format()==contract.image_format
        assert bytes(actual_image.constBits())==bytes(expected_image.constBits())
        assert actual_image.devicePixelRatio()==expected_image.devicePixelRatio()==1.
        assert bytes(actual_image.colorSpace().iccProfile())==bytes(expected_image.colorSpace().iccProfile())
        assert source.tobytes()==original_pixels


@pytest.mark.parametrize('stop_at',[0,1])
def test_existing_cancellation_retains_no_partial_return(candidate,reference,stop_at):
    source=pixels();before=source.tobytes();outcomes=[]
    for module in (reference,candidate):
        observed=[0]
        def cancelled():
            value=observed[0]>=stop_at;observed[0]+=1;return value
        with pytest.raises(module.RadialRenderCancelled):
            module.radial_blur(source,(27.,19.),20.,QTransform(),cancelled=cancelled,
                              output_shape=(103,137),output_origin=(-11.,-7.))
        outcomes.append(observed[0])
    assert outcomes==[stop_at+1,stop_at+1]
    assert source.tobytes()==before


def test_real_sample_supersession_stops_at_unchanged_check_without_return(candidate,reference,monkeypatch):
    source=pixels();before=source.tobytes();token=Event();calls=[]
    ordinary=candidate._rgba_linear_sample
    def sample(*args,**kwargs):
        result=ordinary(*args,**kwargs);calls.append(1);token.set();return result
    monkeypatch.setattr(candidate,'_rgba_linear_sample',sample)
    with pytest.raises(candidate.RadialRenderCancelled):
        candidate.radial_blur(source,(150.,110.),40.,QTransform(),cancelled=token.is_set,
                              output_shape=(103,137),output_origin=(-11.,-7.))
    assert 1<=len(calls)<=16
    token.clear();old_calls=[];old_sample=reference.map_coordinates
    def old(*args,**kwargs):
        result=old_sample(*args,**kwargs);old_calls.append(1)
        if len(old_calls)==4:token.set()
        return result
    monkeypatch.setattr(reference,'map_coordinates',old)
    with pytest.raises(reference.RadialRenderCancelled):
        reference.radial_blur(source,(150.,110.),40.,QTransform(),cancelled=token.is_set,
                              output_shape=(103,137),output_origin=(-11.,-7.))
    assert len(old_calls)==4*len(calls) and source.tobytes()==before


def test_complement_weight_roundoff_matches_scipy_not_naive_fraction(candidate):
    source=np.zeros((1,2,4),dtype=np.float32)
    source[0,1]=1.
    coords=np.zeros((2,1,3),dtype=np.float64)
    coords[1,0]=[2**-54,.5,1.]
    actual=candidate._rgba_linear_sample(source,coords,True)
    expected=scipy_rgba(source,coords)
    np.testing.assert_array_equal(raw(actual),raw(expected))
    assert not np.any(actual[0,0])
    assert np.all(actual[0,1]==np.float32(.5)) and np.all(actual[0,2]==np.float32(1.))


@pytest.mark.parametrize('contract',CONTRACTS)
@pytest.mark.parametrize('intensity',[0.,43.75,100.])
def test_actual_pipeline_native_mask_mix_and_source_bits_equal_original(candidate,reference,scene,monkeypatch,contract,intensity):
    from comic_editor.core.models import ParameterMaskBinding,RadialBlurModifier,ToneMask
    from comic_editor.ui import radial_blur as kernel_module
    from comic_editor.ui.radial_pipeline import render_radial_stage
    from PySide6.QtGui import QColorSpace
    scene.chapter.pixel_contract=contract
    modifier=RadialBlurModifier(center=(27.,19.),angle=18.,intensity=intensity)
    mask=ToneMask();scene.chapter.masks[mask.mask_id]=mask
    modifier.parameter_masks['intensity']=ParameterMaskBinding(mask.mask_id,0.,intensity)
    scene.chapter.modifiers[modifier.modifier_id]=modifier
    fields={(modifier.modifier_id,'intensity'):np.broadcast_to(np.linspace(0.,1.,67,dtype=np.float32),(55,67))}
    calls=[];actual_kernel=candidate.radial_blur
    def operation(function):
        def call(*args,**kwargs):
            calls.append(function is actual_kernel)
            return function(*args,**kwargs)
        return call
    with pixel_scope(contract):
        native=working_image(pixels(),contract)
        native.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
        before=(bytes(native.constBits()),bytes(native.colorSpace().iccProfile()),native.cacheKey())
        mapping=QTransform(1.,.03,-.04,1.,-7.,2.)
        kwargs=dict(scope=('shared-sample-native-mask',),asynchronous=False,deferred=False,
                    provisional=False,navigator=False,exact=True)
        monkeypatch.setattr(kernel_module,'radial_blur',operation(reference.radial_blur))
        expected,old_pending=render_radial_stage(scene,native,native,modifier,fields,mapping,(-3.,-5.),
            source_key=('original-independent-shared-sample',),**kwargs)
        monkeypatch.setattr(kernel_module,'radial_blur',operation(actual_kernel))
        actual,pending=render_radial_stage(scene,native,native,modifier,fields,mapping,(-3.,-5.),
            source_key=('candidate-current-shared-sample',),**kwargs)
        assert not pending and not old_pending and calls==[False,True]
        assert actual.format()==expected.format()==contract.image_format
        assert (actual.width(),actual.height(),actual.bytesPerLine())==(expected.width(),expected.height(),expected.bytesPerLine())
        assert bytes(actual.constBits())==bytes(expected.constBits())
        assert bytes(actual.colorSpace().iccProfile())==bytes(expected.colorSpace().iccProfile())
        assert actual.devicePixelRatio()==expected.devicePixelRatio()==1.
        assert before==(bytes(native.constBits()),bytes(native.colorSpace().iccProfile()),native.cacheKey())



@pytest.mark.parametrize('contract',CONTRACTS)
def test_verified_owned_abi_supported_sample_cannot_silently_use_scipy(candidate,contract,monkeypatch,owned_component):
    with pixel_scope(contract):
        source=_qimage_premultiplied(working_image(pixels(),contract))
    assert candidate._shared_rgba_input(source)
    coords=np.stack(np.meshgrid(np.linspace(-1.,53.,31),np.linspace(-1.,66.,29),indexing='ij'))
    expected=scipy_rgba(source,coords)
    monkeypatch.setattr(candidate,'map_coordinates',lambda *_a,**_k:pytest.fail('Supported C sample silently fell back'))
    actual=candidate._rgba_linear_sample(source,coords,True)
    np.testing.assert_array_equal(raw(actual),raw(expected))


@pytest.mark.parametrize('reason',['height-zero','width-zero','points-zero','points-over-cap',
    'null-source','null-x','nan-coordinate','unsafe-coordinate'])
def test_owned_c_abi_rejects_complete_batch_before_output_write(candidate,reason,owned_component):
    import ctypes
    library=candidate._sampler_library()
    assert library is not None and library.radial_rgba_abi()==candidate._C_ABI
    source=pixels();coordinates=np.zeros((2,3,4),dtype=np.float64)
    output=np.full((3,4,4),-.3125,dtype=np.float32);before=output.tobytes();source_before=source.tobytes()
    source_pointer=source.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
    y_pointer=coordinates.ctypes.data_as(ctypes.POINTER(ctypes.c_double))
    x_pointer=ctypes.cast(coordinates.ctypes.data+12*8,ctypes.POINTER(ctypes.c_double))
    height,width,points=source.shape[0],source.shape[1],12
    if reason=='height-zero':height=0
    elif reason=='width-zero':width=0
    elif reason=='points-zero':points=0
    elif reason=='points-over-cap':points=96*96+1
    elif reason=='null-source':source_pointer=ctypes.POINTER(ctypes.c_float)()
    elif reason=='null-x':x_pointer=ctypes.POINTER(ctypes.c_double)()
    elif reason=='nan-coordinate':coordinates[0,2,3]=np.nan
    elif reason=='unsafe-coordinate':coordinates[1,2,3]=2**52
    code=library.radial_rgba_linear(source_pointer,height,width,y_pointer,x_pointer,points,
        output.ctypes.data_as(ctypes.POINTER(ctypes.c_float)))
    assert code!=0 and output.tobytes()==before and source.tobytes()==source_before


@pytest.mark.parametrize('reason',['missing-build-record','wrong-source-proof'])
def test_optional_delivery_failure_preserves_original_scipy(candidate,reason,tmp_path,monkeypatch):
    import json
    monkeypatch.setattr(candidate,'__file__',str(tmp_path/'private_candidate.py'))
    monkeypatch.setattr(candidate,'_C_LIBRARY',candidate._C_UNTRIED)
    if reason=='wrong-source-proof':
        (tmp_path/'radial_rgba_build.json').write_text(json.dumps({'abi':hex(candidate._C_ABI),
            'platform':'win-amd64','build_contract':candidate._C_BUILD_CONTRACT,
            'source_sha256':'wrong','binary_sha256':'wrong'}))
    assert candidate._sampler_library() is None
    source=pixels();coords=np.zeros((2,3,4),dtype=np.float64);calls=[]
    ordinary=candidate.map_coordinates
    def operation(*args,**kwargs):calls.append(1);return ordinary(*args,**kwargs)
    monkeypatch.setattr(candidate,'map_coordinates',operation)
    expected=scipy_rgba(source,coords)
    actual=candidate._rgba_linear_sample(source,coords,candidate._shared_rgba_input(source))
    assert calls==[1,1,1,1] and actual.tobytes()==expected.tobytes()
