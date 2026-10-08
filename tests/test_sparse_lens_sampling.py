"""Complete native Lens results and independently evaluated SciPy tap support."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QTransform
from scipy.ndimage import map_coordinates

from comic_editor.core.models import DistortModifier
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, working_image
from comic_editor.ui import distort_rendering as rendering


CONTRACTS = [LEGACY_PIXELS,replace(FLOAT_PIXELS,precision='float16'),FLOAT_PIXELS]
BOUNDS,TARGET = QRectF(-31.25,17111.375,91.,67.),QRectF(-111.,17021.,239.,237.)


def source(contract):
    yy,xx = np.mgrid[:67,:91]
    alpha = ((xx*7+yy*13)%193)/192.
    pixels = np.stack((xx/91,yy/67,(xx%11)/11,np.ones_like(alpha)),-1)*alpha[...,None]
    if contract.floating:
        pixels[23,24] = [np.nan,np.inf,-0.,.125]
    return working_image(pixels.astype(np.float32),contract)


def modifier(edge='transparent',effect='lens_distortion',interpolation='bilinear'):
    item=DistortModifier(modifier_type='distort_'+effect,
        frame=tuple(BOUNDS.getRect()),center=(12.125,17148.875))
    item.validate()
    item.parameters.update(amount=-63.125,interpolation=interpolation,edges=edge)
    return item


def bits(image):
    return bytes(image.constBits())


def assert_native(left,right):
    assert left.size()==right.size() and left.format()==right.format()
    assert left.bytesPerLine()==right.bytesPerLine()
    assert bits(left)==bits(right)


@pytest.mark.parametrize('contract',CONTRACTS)
@pytest.mark.parametrize('edge',['transparent','white'])
@pytest.mark.parametrize('projective',[False,True])
@pytest.mark.parametrize('prepared',[False,True])
def test_complete_native_lens_matches_original_sampling_route(monkeypatch,contract,edge,projective,prepared):
    mapping=(QTransform(1.1,.13,.000003,-.07,.9,-.000005,13.375,22.625,1.)
             if projective else QTransform())
    helper=rendering._sample_lens_bilinear
    with pixel_scope(contract):
        incoming=source(contract)
        original=bits(incoming)
        item=modifier(edge)
        # This reference executes the original unoptimized source sampler,
        # retaining the same complete kernel/map/native encoder.
        monkeypatch.setattr(rendering,'_sample_lens_bilinear',lambda sampler,xy:sampler(xy))
        expected=rendering.render_distort(incoming,BOUNDS,item,mapping,TARGET)
        calls=[]
        def observe(sampler,xy):
            calls.append(xy.shape)
            return helper(sampler,xy)
        monkeypatch.setattr(rendering,'_sample_lens_bilinear',observe)
        cache=rendering.PreparedDistortCache() if prepared else None
        actual=rendering.render_distort(incoming,BOUNDS,item,mapping,TARGET,preparation_cache=cache)
        assert_native(actual,expected)
        assert bits(incoming)==original
        assert bool(calls)==(contract==LEGACY_PIXELS)
        if cache is not None:
            repeated=rendering.render_distort(incoming,BOUNDS,item,mapping,TARGET,preparation_cache=cache)
            assert_native(repeated,expected)
            assert cache.hits and cache.bytes<=cache.budget


def scipy_reference(sampler,xy):
    x,y=xy[...,0],xy[...,1]
    valid=np.isfinite(x)&np.isfinite(y)&(np.abs(x)<1e15)&(np.abs(y)<1e15)
    coordinates=[np.where(valid,y+sampler.padding,-1e9),np.where(valid,x+sampler.padding,-1e9)]
    result=np.stack([map_coordinates(channel,coordinates,order=sampler.order,mode=sampler.mode,
        cval=sampler.fill,prefilter=False) for channel in sampler.filtered],-1)
    result[~valid]=sampler.fill
    return result


@pytest.mark.parametrize('edge',['transparent','white'])
@pytest.mark.parametrize('pattern',['partial-boundaries','all-outside','nonfinite'])
def test_exact_and_adjacent_support_boundaries_match_independent_scipy(monkeypatch,edge,pattern):
    native=np.arange(67*91*4,dtype=np.uint8).reshape(67,91,4)
    pixels=np.array(native,np.float32,copy=True)/np.float32(255)
    sampler=rendering._Sampler(pixels,'bilinear',edge)
    xy=np.array([[-1.,-.5],[np.nextafter(-1.,0.),.375],[0.,0.],[90.,66.],
        [np.nextafter(91.,0.),12.125],[91.,0.],[-99.,-44.],[220.,188.],[.125,-.25],[0.,67.]],np.float64)
    if pattern=='all-outside': xy=xy[[6,7]]
    if pattern=='nonfinite': xy=np.array([[np.nan,0.],[np.inf,1.],[1e15,3.]],np.float64)
    expected=scipy_reference(sampler,xy)
    full_calls=[]
    original=rendering._Sampler.__call__
    def observe(owner,coordinates):
        full_calls.append(coordinates.copy())
        return original(owner,coordinates)
    monkeypatch.setattr(rendering._Sampler,'__call__',observe)
    actual=rendering._sample_lens_bilinear(sampler,xy)
    assert actual.dtype==expected.dtype and actual.tobytes()==expected.tobytes()
    if pattern=='all-outside': assert not full_calls
    if pattern=='partial-boundaries':
        supported=(xy[:,0]>-1)&(xy[:,0]<91)&(xy[:,1]>-1)&(xy[:,1]<67)
        assert len(full_calls)==1
        assert full_calls[0].tobytes()==xy[supported].tobytes()
    if pattern=='nonfinite': assert len(full_calls)==1 and full_calls[0].tobytes()==xy.tobytes()


@pytest.mark.parametrize('interpolation,edge',[('nearest','white'),('bicubic','transparent'),
    ('bilinear','clamp'),('bilinear','mirror'),('bilinear','wrap')])
def test_unsupported_sampling_modes_keep_the_original_route(monkeypatch,interpolation,edge):
    with pixel_scope(LEGACY_PIXELS):
        incoming=source(LEGACY_PIXELS)
        item=modifier(edge,interpolation=interpolation)
        def forbidden(*args): pytest.fail('Unsupported mode entered sparse Lens sampling')
        monkeypatch.setattr(rendering,'_sample_lens_bilinear',forbidden)
        actual=rendering.render_distort(incoming,BOUNDS,item,output_bounds=TARGET)
        assert not actual.isNull()


@pytest.mark.parametrize('case',['draft','density-two','other-effect','native-wide','different-byte-format'])
def test_other_source_grids_and_native_formats_keep_original_sampling(monkeypatch,case):
    with pixel_scope(LEGACY_PIXELS):
        incoming=source(LEGACY_PIXELS)
        item,scale=modifier(),1.
        if case=='draft':scale=.5
        if case=='density-two':scale=2.
        if case=='other-effect':item=modifier(effect='twirl')
        if case=='native-wide':incoming=incoming.convertToFormat(QImage.Format_RGBA64_Premultiplied)
        if case=='different-byte-format':incoming=incoming.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        def forbidden(*args):pytest.fail('Unproved source/grid entered sparse Lens sampling')
        monkeypatch.setattr(rendering,'_sample_lens_bilinear',forbidden)
        assert not rendering.render_distort(incoming,BOUNDS,item,output_bounds=TARGET,pixel_scale=scale).isNull()


@pytest.mark.parametrize('contract',CONTRACTS)
def test_source_mutation_is_current_and_preserves_pinned_native_image(contract):
    with pixel_scope(contract):
        incoming=source(contract)
        pinned=QImage(incoming)
        original=bits(pinned)
        cache=rendering.PreparedDistortCache()
        item=modifier()
        before=rendering.render_distort(incoming,BOUNDS,item,output_bounds=TARGET,preparation_cache=cache)
        incoming.setPixelColor(24,23,QColor(251,19,83,173))
        after=rendering.render_distort(incoming,BOUNDS,item,output_bounds=TARGET,preparation_cache=cache)
        fresh=rendering.render_distort(incoming,BOUNDS,item,output_bounds=TARGET)
        unchanged=rendering.render_distort(pinned,BOUNDS,item,output_bounds=TARGET,preparation_cache=cache)
        assert_native(after,fresh)
        assert_native(unchanged,before)
        assert bits(incoming)!=original and bits(pinned)==original
        assert bits(after)!=bits(before)
        assert cache.bytes<=cache.budget


@pytest.mark.parametrize('kind',['point-limit','borrowed-array','nonfloat64'])
def test_bounded_or_unowned_coordinate_work_keeps_original_sampler(monkeypatch,kind):
    pixels=np.random.default_rng(11).random((7,9,4),dtype=np.float32)
    if kind=='borrowed-array':pixels=pixels[:,:,:]
    sampler=rendering._Sampler(pixels,'bilinear','transparent')
    xy=np.zeros((262145 if kind=='point-limit' else 12,2),np.float64)
    if kind=='nonfloat64':xy=xy.astype(np.float32)
    expected=scipy_reference(sampler,xy)
    full_calls=[]
    original=rendering._Sampler.__call__
    def observe(owner,coordinates):
        full_calls.append(coordinates.shape)
        return original(owner,coordinates)
    monkeypatch.setattr(rendering._Sampler,'__call__',observe)
    actual=rendering._sample_lens_bilinear(sampler,xy)
    assert full_calls==[xy.shape] and actual.tobytes()==expected.tobytes()


def test_cancellation_still_stops_between_native_output_strips():
    with pixel_scope(LEGACY_PIXELS):
        count=0
        def cancelled():
            nonlocal count
            count+=1
            return count==3
        result=rendering.render_distort(source(LEGACY_PIXELS),BOUNDS,modifier(),
            output_bounds=QRectF(-400.,16700.,900.,400.),cancelled=cancelled)
        assert result is None and count==3
