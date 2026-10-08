"""Exact output-work crop against the unchanged, portable R12 kernel.

This is not a reduced source/grid renderer. Full blur, native coordinates and
cell-table eligibility precede output slicing. Timing acceptance is separate.
"""
from pathlib import Path
import hashlib
import importlib.util

import numpy as np
import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.models import HalftoneModifier
from comic_editor.core.pixel_contract import PixelContract
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.ui import pattern_rendering as current
from comic_editor.ui.radial_blur import RadialRenderCancelled


REFERENCE_SHA = '4b0636f5025fc042e367178663be826bf59162a76e70ce337dd67394fcd1fa1e'
REFERENCE_PATH = Path(__file__).resolve().parent/'references'/'halftone_native_r12.py'


@pytest.fixture(scope='module')
def original():
    assert hashlib.sha256(REFERENCE_PATH.read_bytes()).hexdigest() == REFERENCE_SHA
    spec = importlib.util.spec_from_file_location('_halftone_native_r12_reference', REFERENCE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    assert Path(module.__file__).resolve() == REFERENCE_PATH.resolve()
    return module


def _contract(precision='float32'):
    return PixelContract() if precision == 'uint8' else PixelContract(version=2, precision=precision)


def _pixels(width=73, height=61):
    yy, xx = np.indices((height, width), dtype=np.float32)
    alpha = ((xx*7+yy*11) % 29)/np.float32(28)
    alpha[0, 0], alpha[1, 1] = np.float32(0), np.float32(2**-13)
    rgb = np.stack(((xx % 17)/16, (yy % 13)/12, ((xx+yy) % 19)/18), axis=-1)
    result = np.concatenate((rgb*alpha[..., None], alpha[..., None]), axis=-1).astype(np.float32)
    result[0, 0, 0] = np.float32(-0.)
    return result


def _modifier(grid='square', style='circle', color_mode='two', fine=False):
    modifier = HalftoneModifier(grid_type=grid, dot_style=style,
        base_resolution=4000 if fine else 149, spacing=2 if fine else 12,
        rotation=27., dot_rotation=-19., link_rotation=False, size=1.7,
        scale_factor=.63, blur=1.9, color_mode=color_mode,
        transparent_background=True, foreground='#A03C719A', background='#7091BFA2',
        target_hue=33., target_saturation=17., target_lightness=-12.,
        gradient_stops=[[0., '#8033519A'], [.43, '#C08FA829'], [1., '#FFE1C674']])
    modifier.validate()
    return modifier


def _bits(array):
    assert array.dtype == np.float32
    return np.ascontiguousarray(array).view(np.uint32)


def _native(image):
    # The bytes detach before the caller can release a temporary QImage.
    return (image.width(), image.height(), image.format(), image.bytesPerLine(),
            bytes(image.constBits()), bytes(image.colorSpace().iccProfile()),
            image.devicePixelRatio(), image.dotsPerMeterX(), image.dotsPerMeterY())


@pytest.mark.parametrize('grid', ['square', 'hexagonal'])
@pytest.mark.parametrize('style', ['circle', 'incircle'])
@pytest.mark.parametrize('fine', [False, True])
@pytest.mark.parametrize('mode', ['two', 'gradient', 'source', 'target_layer'])
def test_cropped_float_bits_equal_original_full_global_frame(original, grid, style, fine, mode):
    source, colors = _pixels(), _pixels()[::-1, ::-1].copy()
    before, color_before = source.tobytes(), colors.tobytes()
    modifier = _modifier(grid, style, mode, fine)
    frame, origin = (149, 173), (31, 47)
    with pixel_scope(_contract()):
        expected = original._halftone(source, modifier, colors, frame_size=frame, origin=origin)
        default = current._halftone(source, modifier, colors, frame_size=frame, origin=origin)
        np.testing.assert_array_equal(_bits(default), _bits(expected))
        for left, top, width, height in ((0, 0, 73, 61), (0, 0, 1, 1),
                                        (72, 60, 1, 1), (11, 17, 29, 23)):
            cropped = current._halftone(source, modifier, colors,
                frame_size=frame, origin=origin, output_region=(left, top, width, height))
            assert cropped.shape == (height, width, 4)
            np.testing.assert_array_equal(_bits(cropped), _bits(expected[top:top+height, left:left+width]))
    assert source.tobytes() == before and colors.tobytes() == color_before
    assert np.count_nonzero(expected[..., 3]) > 0


@pytest.mark.parametrize('fine', [False, True])
def test_full_cell_table_decision_and_blur_precede_output_work(original, monkeypatch, fine):
    source = _pixels()
    modifier = _modifier(fine=fine)
    record = {'blur': [], 'table': [], 'dot': []}
    blur, table, dot = current._blur, current._cell_sample_table, current._dot_coverage
    def observed_blur(value, amount):
        record['blur'].append(value.shape)
        return blur(value, amount)
    def observed_table(prepared, colors, cell_x, cell_y, *args, **kwargs):
        result = table(prepared, colors, cell_x, cell_y, *args, **kwargs)
        record['table'].append((prepared.shape, cell_x.shape, result is not None))
        return result
    def observed_dot(x, y, *args, **kwargs):
        record['dot'].append(x.shape)
        return dot(x, y, *args, **kwargs)
    monkeypatch.setattr(current, '_blur', observed_blur)
    monkeypatch.setattr(current, '_cell_sample_table', observed_table)
    monkeypatch.setattr(current, '_dot_coverage', observed_dot)
    with pixel_scope(_contract()):
        result = current._halftone(source, modifier, frame_size=(149, 173), origin=(31, 47), output_region=(19, 23, 1, 1))
        expected = original._halftone(source, modifier, frame_size=(149, 173), origin=(31, 47))
    assert record['blur'] == [source.shape]
    assert record['table'] == [(source.shape, source.shape[:2], not fine)]
    # The naturally absent cell table retains whole arithmetic.
    expected_work_shape = source.shape[:2] if fine else (1, 1)
    assert record['dot'] and set(record['dot']) == {expected_work_shape}
    np.testing.assert_array_equal(_bits(result), _bits(expected[23:24, 19:20]))


@pytest.mark.parametrize('precision', ['uint8', 'float16', 'float32', 'rgba64', 'linear-float32'])
@pytest.mark.parametrize('grid', ['square', 'hexagonal'])
@pytest.mark.parametrize('style', ['circle', 'incircle'])
def test_native_qimage_region_equals_original_whole_crop_and_preserves_source(original, precision, grid, style):
    contract = (PixelContract(version=2, precision='float32', working_space='linear_srgb')
                if precision == 'linear-float32' else _contract('float32' if precision == 'rgba64' else precision))
    source = working_image(_pixels(67, 53), contract)
    if precision == 'rgba64':
        source = source.convertToFormat(QImage.Format_RGBA64_Premultiplied)
    source.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.SRgb))
    source.setDevicePixelRatio(1.)
    source.setDotsPerMeterX(3780); source.setDotsPerMeterY(3780)
    before, key = _native(source), source.cacheKey()
    modifier = _modifier(grid, style, 'gradient')
    requested = QRect(7, 9, 43, 31)
    with pixel_scope(contract):
        full = original.halftone_region(source, modifier, QRect(source.rect()))
        expected = full.copy(requested)
        for tile_size in (1536, 17):
            old_region = original.halftone_region(source, modifier, requested, tile_size=tile_size)
            result = current.halftone_region(source, modifier, requested, tile_size=tile_size)
            assert _native(result) == _native(old_region) == _native(expected)
            assert result.format() == contract.image_format
            assert result.devicePixelRatio() == 1.
    assert _native(source) == before and source.cacheKey() == key
    assert before[5] and source.size() == QImage(67, 53, source.format()).size()


FALLBACKS = [('line', 'circle'), ('ring', 'incircle'), ('radial', 'circle'),
             ('stippling', 'circle'), ('square', 'blob'), ('hexagonal', 'liquid'),
             ('square', 'polygon'), ('square', 'triangle'), ('square', 'delaunay')]


@pytest.mark.parametrize('grid,style', FALLBACKS)
def test_unsupported_default_full_frame_unchanged_and_optional_crop_rejected(original, grid, style):
    source = _pixels(47, 43)
    modifier = _modifier(grid, style)
    modifier.spacing, modifier.base_resolution, modifier.blur = 18., 100, 0.
    with pixel_scope(_contract()):
        expected = original._halftone(source, modifier)
        actual = current._halftone(source, modifier)
        np.testing.assert_array_equal(_bits(actual), _bits(expected))
        with pytest.raises(ValueError, match='complete evaluation frame'):
            current._halftone(source, modifier, output_region=(2, 3, 17, 13))
    assert np.count_nonzero(source[..., 3]) > 0


@pytest.mark.parametrize('region', [(-1, 0, 1, 1), (0, -1, 1, 1), (0, 0, 0, 1),
    (0, 0, 1, -1), (72, 60, 2, 1), (72, 60, 1, 2), (0., 0, 1, 1),
    (0, 0, 1.5, 1), (0, 0, 1), (0, 0, 1, 1, 1)])
def test_invalid_region_cannot_change_source_or_publish_result(region):
    source = _pixels()
    before = source.tobytes()
    with pixel_scope(_contract()), pytest.raises((ValueError, TypeError)):
        current._halftone(source, _modifier(), output_region=region)
    assert source.tobytes() == before


@pytest.mark.parametrize('cancel_at', [1, 2, 4, 8])
def test_original_cancellation_exception_and_source_invariance(original, cancel_at):
    source = _pixels()
    before = source.tobytes()
    modifier = _modifier('hexagonal')
    counts = []
    for module in (original, current):
        seen = [0]
        def cancelled():
            seen[0] += 1
            return seen[0] >= cancel_at
        with pixel_scope(_contract()), pytest.raises(RadialRenderCancelled):
            kwargs = {'output_region': (9, 11, 23, 17)} if module is current else {}
            module._halftone(source, modifier, cancelled=cancelled, **kwargs)
        counts.append(seen[0])
    assert counts == [cancel_at, cancel_at]
    assert source.tobytes() == before


@pytest.mark.parametrize('grid', ['line', 'ring', 'radial', 'stippling'])
def test_regional_unsupported_grid_keeps_original_fallback(original, grid):
    source = working_image(_pixels(47, 43), _contract())
    modifier = _modifier(grid)
    with pixel_scope(_contract()):
        for module in (current, original):
            with pytest.raises(ValueError, match='complete frame'):
                module.halftone_region(source, modifier, QRect(1, 2, 19, 17))


@pytest.mark.parametrize('rotation,origin', [(0., (0., 0.)), (89., (31.25, 47.5)),
                                          (-180., (1048576., -32.))])
def test_native_global_float_coordinates_and_hdr_source_bits_remain_unchanged(original, rotation, origin):
    source = _pixels()
    source[2, 2, :3] = source[2, 2, 3]*np.array([-.25, 2.5, .4], dtype=np.float32)
    before = source.tobytes()
    modifier = _modifier('hexagonal', 'incircle', 'source')
    modifier.rotation = rotation
    with pixel_scope(_contract()):
        full = original._halftone(source, modifier, frame_size=(1537, 19654), origin=origin)
        result = current._halftone(source, modifier, frame_size=(1537, 19654), origin=origin,
                                   output_region=(7, 9, 31, 23))
    np.testing.assert_array_equal(_bits(result), _bits(full[9:32, 7:38]))
    assert source.tobytes() == before and source[2, 2, 1] > source[2, 2, 3]



@pytest.mark.parametrize('grid', ['square', 'hexagonal'])
@pytest.mark.parametrize('mode', ['two', 'gradient', 'source', 'target_layer'])
def test_natural_table_none_keeps_full_arithmetic_then_crops_once(original, monkeypatch, grid, mode):
    source, colors = _pixels(), _pixels()[::-1, ::-1].copy()
    before, color_before = source.tobytes(), colors.tobytes()
    modifier = _modifier(grid, 'circle', mode, fine=True)
    record = {'blur': [], 'table': [], 'dot': []}
    blur, table, dot = current._blur, current._cell_sample_table, current._dot_coverage
    def observed_blur(value, amount):
        record['blur'].append(value.shape)
        return blur(value, amount)
    def observed_table(*args, **kwargs):
        answer = table(*args, **kwargs)
        record['table'].append(answer is None)
        return answer
    def observed_dot(x, y, *args, **kwargs):
        record['dot'].append(x.shape)
        return dot(x, y, *args, **kwargs)
    monkeypatch.setattr(current, '_blur', observed_blur)
    monkeypatch.setattr(current, '_cell_sample_table', observed_table)
    monkeypatch.setattr(current, '_dot_coverage', observed_dot)
    with pixel_scope(_contract()):
        expected = original._halftone(source, modifier, colors, frame_size=(149, 173), origin=(31, 47))
        answer = current._halftone(source, modifier, colors, frame_size=(149, 173),
            origin=(31, 47), output_region=(0, 0, 1, 1))
    assert record['blur'] == [source.shape]
    assert record['table'] == [True]
    assert record['dot'] and set(record['dot']) == {source.shape[:2]}
    assert answer.shape == (1, 1, 4)
    np.testing.assert_array_equal(_bits(answer), _bits(expected[:1, :1]))
    assert source.tobytes() == before and colors.tobytes() == color_before
    assert np.count_nonzero(expected[..., 3]) > 0
