"""Original native bilinear coefficients survive bounded source preparation."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import DistortModifier
from comic_editor.core.pixel_contract import PixelContract
from comic_editor.render.pixels import pixel_scope
from comic_editor.ui import distort_rendering as kernel


def source():
    image = QImage(512, 384, QImage.Format_ARGB32_Premultiplied)
    rows = np.frombuffer(image.bits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    pixels = rows[:, :image.width()*4].reshape(image.height(), image.width(), 4)
    yy, xx = np.mgrid[:384, :512]
    alpha = (xx*7 + yy*13 + 31) % 256
    pixels[..., 3] = alpha
    for channel, prime in enumerate((11, 17, 23)):
        pixels[..., channel] = ((xx*prime + yy*(prime+4) + 31) % 256)*alpha//255
    return image


def modifier(kind, edges):
    effect = DistortModifier(modifier_type='distort_'+kind,
        frame=(-37.25, -61.375, 512., 384.), center=(218.75, 130.625), radius=183.5)
    effect.validate()
    effect.parameters.update(interpolation='bilinear', edges=edges)
    if kind == 'twirl':
        effect.parameters['angle'] = 73.5
    elif kind in {'deform', 'mesh_warp'}:
        effect.points = [(x + (.025 if index % 2 else -.04), y+.03)
                         for index, (x, y) in enumerate(effect.points)]
    else:
        effect.parameters['amount'] = -52.75 if kind == 'lens_distortion' else -86.25
    return effect


@pytest.mark.parametrize('kind', ['twirl', 'deform', 'mesh_warp', 'lens_distortion', 'pinch_punch'])
@pytest.mark.parametrize('edges', ['transparent', 'white'])
@pytest.mark.parametrize('projective', [False, True])
@pytest.mark.parametrize('output', [(-11.5, -19.25, 129., 113.),
                                   (-82., -90., 113., 97.), (397., 261., 111., 113.)])
def test_native_stage_matches_full_preparation(monkeypatch, kind, edges, projective, output):
    image = source()
    bounds = QRectF(-37.25, -61.375, 512., 384.)
    mapping = (QTransform(1.1, .13, .00015, -.07, .9, -.00021, 13.375, 22.625, 1.)
               if projective else QTransform().translate(13.375, 22.625).rotate(19.25).scale(1.25, .73))
    arguments = (image, bounds, modifier(kind, edges), mapping, QRectF(*output))
    expected = kernel.render_distort(*arguments, preparation_cache=kernel.PreparedDistortCache())
    # Exercise the real branch without making every correctness test allocate
    # a 29MP source. The cache budget and the source sampling grid stay intact.
    monkeypatch.setattr(kernel, 'PREPARED_DISTORT_CACHE_BUDGET', 1)
    assert isinstance(kernel._oversized_bilinear_sampler(image, 'bilinear', edges,
        kind, 1., kernel.PreparedDistortCache()), kernel._StripBilinearSampler)
    actual = kernel.render_distort(*arguments, preparation_cache=kernel.PreparedDistortCache())
    assert actual.size() == expected.size() and actual.format() == expected.format()
    assert actual.bytesPerLine() == expected.bytesPerLine()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


@pytest.mark.parametrize('edges', ['transparent', 'white'])
def test_native_fraction_boundary_and_invalid_coordinates_match_full_sampler(edges):
    image = source()
    full = kernel._Sampler(kernel._rgba(image), 'bilinear', edges)
    cache = kernel.PreparedDistortCache()
    sliced = kernel._StripBilinearSampler(image, edges, cache)
    yy, xx = np.meshgrid(np.linspace(71.125, 74.875, 11),
                         np.linspace(40.125, 43.875, 13), indexing='ij')
    requests = [np.stack((xx, yy), axis=-1),
        np.array([[[0., -0.], [np.nextafter(1., 0.), np.nextafter(1., 2.)]],
                  [[-1., -.25], [511.75, 383.25]]], np.float64),
        np.array([[[np.nan, 0.], [np.inf, 2.], [1e15, 1.]]], np.float64)]
    for coordinates in requests:
        expected, actual = full(coordinates), sliced(coordinates)
        assert actual.dtype == expected.dtype
        assert actual.tobytes() == expected.tobytes()
    assert sliced.full is not None
    assert 0 < cache.bytes <= cache.budget
    assert all(not entry[0].pixels.flags.writeable for entry in cache._entries.values())


def test_slice_input_is_immutable_and_new_source_cannot_alias_old_preparation():
    image = source()
    old = QImage(image)
    cache = kernel.PreparedDistortCache(budget=2048, entry_limit=2)
    coordinates = np.array([[[71.125, 42.375], [74.875, 44.25]]], np.float64)
    sliced = kernel._StripBilinearSampler(image, 'transparent', cache)
    expected = kernel._Sampler(kernel._rgba(old))(coordinates)
    assert sliced(coordinates).tobytes() == expected.tobytes()
    image.fill(0)
    assert sliced(coordinates).tobytes() == expected.tobytes()
    new = kernel._StripBilinearSampler(image, 'transparent', cache)
    assert new(coordinates).tobytes() != expected.tobytes()
    for offset in range(5):
        sliced(coordinates + offset*10.)
        assert cache.bytes <= cache.budget and len(cache._entries) <= cache.entry_limit
    assert cache.evictions > 0


@pytest.mark.parametrize('interpolation,edges,kind,scale', [
    ('bicubic', 'transparent', 'twirl', 1.), ('nearest', 'transparent', 'twirl', 1.),
    ('bilinear', 'wrap', 'twirl', 1.), ('bilinear', 'mirror', 'twirl', 1.),
    ('bilinear', 'clamp', 'twirl', 1.), ('bilinear', 'transparent', 'displace', 1.),
    ('bilinear', 'transparent', 'pixelate', 1.), ('bilinear', 'transparent', 'smudge', 1.),
    ('bilinear', 'transparent', 'twirl', .5)])
def test_unsupported_sampling_keeps_complete_source(monkeypatch, interpolation, edges, kind, scale):
    monkeypatch.setattr(kernel, 'PREPARED_DISTORT_CACHE_BUDGET', 1)
    assert kernel._oversized_bilinear_sampler(source(), interpolation, edges, kind, scale, None) is None


@pytest.mark.parametrize('precision', ['float16', 'float32'])
def test_float_source_never_uses_integer_preparation(monkeypatch, precision):
    monkeypatch.setattr(kernel, 'PREPARED_DISTORT_CACHE_BUDGET', 1)
    with pixel_scope(PixelContract(version=2, precision=precision, working_space='linear_srgb')):
        assert kernel._oversized_bilinear_sampler(source(), 'bilinear', 'transparent', 'twirl', 1., None) is None


def test_small_and_foreign_native_formats_keep_original_preparation(monkeypatch):
    image = source()
    assert kernel._oversized_bilinear_sampler(image, 'bilinear', 'transparent', 'twirl', 1., None) is None
    monkeypatch.setattr(kernel, 'PREPARED_DISTORT_CACHE_BUDGET', 1)
    foreign = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    assert kernel._oversized_bilinear_sampler(foreign, 'bilinear', 'transparent', 'twirl', 1., None) is None


def test_cancellation_returns_before_any_source_preparation(monkeypatch):
    def forbidden(*args):
        pytest.fail('cancelled warp allocated a source sampler')
    monkeypatch.setattr(kernel, '_oversized_bilinear_sampler', forbidden)
    assert kernel.render_distort(source(), QRectF(-37.25, -61.375, 512., 384.),
                                 modifier('twirl', 'transparent'), cancelled=lambda: True) is None
