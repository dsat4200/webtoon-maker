"""Allocation shortcuts retain original coordinate and working-pixel bits."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core import kuwahara
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.render.pixels import pixel_scope
from comic_editor.ui import distort_rendering
from references.kuwahara_original_native_20261009 import _original as original_kuwahara
from test_distort_mesh_acceleration import mesh, reference as original_mesh_mapping


@pytest.mark.parametrize('dtype', [np.float32, np.float64])
@pytest.mark.parametrize('shape', [(9, 11), (192, 192), (193, 197), (256, 260)])
@pytest.mark.parametrize('radius', [0., 1., 6., 6.25, 30.5, 'field'])
def test_kuwahara_matches_original_float_bits(dtype, shape, radius):
    rng = np.random.default_rng(819)
    pixels = rng.random((*shape, 4)).astype(dtype)
    pixels[..., :3] *= pixels[..., 3:4]
    pixels[::9, ::7] = 0
    size = rng.uniform(0, 7, shape).astype(np.float32) if radius == 'field' else radius
    actual = kuwahara._original(pixels, size)
    expected = original_kuwahara(pixels, size)
    assert actual.dtype == expected.dtype and actual.shape == expected.shape
    assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize('query_dtype', [np.float32, np.float64])
@pytest.mark.parametrize('shape', [(128, 256), (99, 121), (128, 260), (1, 17)])
@pytest.mark.parametrize('projective', [False, True])
def test_mesh_preserves_mixed_precision_coordinate_bits(query_dtype, shape, projective):
    _, (source, destination, faces) = mesh()
    # Original face order decides overlapping coverage; degenerate faces skip.
    faces = np.concatenate((faces, faces[:20, ::-1], [[0, 0, 0]]))
    xx, yy = np.meshgrid(np.linspace(-12, 212, shape[1]), np.linspace(-20, 230, shape[0]))
    query = np.stack((xx, yy), axis=-1).astype(query_dtype)
    if projective:
        query = distort_rendering._transform(QTransform(1, .1, .0003, -.07, 1, -.0002, 8, -4, 1), query)
    actual = distort_rendering._triangle_map(query, source, destination, faces)
    expected = original_mesh_mapping(query, source, destination, faces)
    assert actual.dtype == expected.dtype
    assert actual.tobytes() == expected.tobytes()


@pytest.mark.parametrize('contract', [LEGACY_PIXELS,
    PixelContract(version=2, precision='float16'), PixelContract(version=2, precision='float32')])
@pytest.mark.parametrize('sampling', ['nearest', 'bilinear', 'bicubic'])
@pytest.mark.parametrize('scale,projective', [(1., False), (.4, False), (1., True)])
def test_mesh_full_native_buffers_match_original_mapping(contract, sampling, scale, projective, monkeypatch):
    rng = np.random.default_rng(432)
    pixels = rng.random((48, 64, 4), dtype=np.float32)
    pixels[..., :3] *= pixels[..., 3:4]
    if contract.floating:
        dtype = np.float16 if contract.precision == 'float16' else np.float32
        pixels = np.ascontiguousarray(pixels.astype(dtype))
        source = QImage(pixels.data, 64, 48, 64 * 4 * dtype().itemsize, contract.image_format).copy()
    else:
        pixels = np.ascontiguousarray(np.rint(pixels * 255).astype(np.uint8))
        source = QImage(pixels.data, 64, 48, 256, QImage.Format_RGBA8888_Premultiplied).copy()
    modifier, _ = mesh()
    modifier.points[6] = (-.1, .6)  # Folded geometry retains stable face order.
    modifier.parameters.update(interpolation=sampling, edges='transparent')
    placement = QTransform(1, .1, .0003, -.07, 1, -.0002, 8, -4, 1) if projective else QTransform()
    with pixel_scope(contract):
        for bounds in (QRectF(-4., -3., 260., 190.), QRectF(12.25, -3.75, 60.5, 71.5)):
            actual = distort_rendering.render_distort(source, QRectF(0, 0, 64, 48), modifier, placement, bounds,
                pixel_scale=scale, preparation_cache=distort_rendering.PreparedDistortCache())
            with monkeypatch.context() as patch:
                patch.setattr(distort_rendering, '_triangle_map', original_mesh_mapping)
                expected = distort_rendering.render_distort(source, QRectF(0, 0, 64, 48), modifier, placement, bounds,
                    pixel_scale=scale, preparation_cache=distort_rendering.PreparedDistortCache())
            assert actual.format() == expected.format() and actual.size() == expected.size()
            assert actual.bytesPerLine() == expected.bytesPerLine()
            assert actual.devicePixelRatio() == expected.devicePixelRatio()
            assert actual.colorSpace() == expected.colorSpace()
            assert bytes(actual.constBits()) == bytes(expected.constBits())
