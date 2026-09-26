"""Contrast reduces texture differences without implicitly enabling inversion."""
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest
from PIL import Image
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushTexture
from comic_editor.core import brush_raster as raster
from comic_editor.core import sut_import
from comic_editor.core.sut_import import import_sut
from comic_editor.core.tiles import TileStore
from test_brush_raster import png


def gradient_texture(**kwargs):
    pixels = np.empty((1, 256, 4), np.uint8)
    pixels[..., :3] = np.arange(256, dtype=np.uint8)[None, :, None]
    pixels[..., 3] = 255
    return BrushTexture(png=png(pixels), **kwargs)


def response(texture):
    stroke = raster.RasterBrushStroke(TileStore(16), "paint", BrushDefinition(), QColor("black"), {})
    xx = np.arange(256, dtype=np.float32)[None, :]+.5
    yy = np.full_like(xx, .5)
    return stroke._texture(np.ones_like(xx), texture, xx, yy)[0]


@pytest.mark.parametrize("contrast", [-1, -.999, -.55, -.34, -.01, 0])
def test_negative_contrast_preserves_luminance_order_and_shrinks_range(contrast):
    values = response(gradient_texture(contrast=contrast))
    assert np.isfinite(values).all()
    assert np.all(np.diff(values) >= -1e-7)
    assert values[-1]-values[0] == pytest.approx(1+contrast, abs=1e-6)
    if contrast == -1:
        np.testing.assert_array_equal(values, np.full(256, .5, np.float32))


@pytest.mark.parametrize("brightness", [-1, -.3, 0, .3, 1])
def test_minimum_contrast_is_uniform_even_with_brightness(brightness):
    values = response(gradient_texture(contrast=-1, brightness=brightness))
    np.testing.assert_allclose(values, np.clip(.5+brightness, 0, 1), atol=1e-7)


@pytest.mark.parametrize("emphasize", [False, True])
@pytest.mark.parametrize("brightness", [-.3, 0, .3])
def test_invert_alone_reverses_negative_contrast_after_brightness(emphasize, brightness):
    texture = gradient_texture(contrast=-.55, brightness=brightness,
                               emphasize_density=emphasize)
    normal = response(texture)
    inverted = response(replace(texture, invert=True))
    assert np.all(np.diff(normal) >= -1e-7)
    assert np.all(np.diff(inverted) <= 1e-7)
    np.testing.assert_allclose(normal+inverted, 1, atol=2e-7)
    assert normal[-1] > normal[0] and inverted[-1] < inverted[0]


@pytest.mark.parametrize("contrast", [0, .1, .55, 1])
@pytest.mark.parametrize("brightness", [-.3, 0, .3])
def test_nonnegative_contrast_keeps_previous_equation_exactly(contrast, brightness):
    texture = gradient_texture(contrast=contrast, brightness=brightness)
    gray = raster._material_cache.get(texture.png, gray=True)
    xx = np.arange(256, dtype=np.float32)[None, :]
    values = raster._sample(gray, xx, np.zeros_like(xx), wrap=True)
    previous = np.clip((values-.5)*(1+contrast*3)+.5+brightness, 0, 1)[0]
    # Include the unchanged density-composition step: its float32 subtraction
    # and addition can round by one ULP even when density is exactly one.
    previous = np.clip(1+(previous-1), 0, 1)
    np.testing.assert_array_equal(response(texture), previous)


def test_hazy3_source_contrast_scale_and_invert_are_retained_without_preset_tuning(monkeypatch):
    path = Path('.artifacts/brush-investigation/installed/additional-request-reconstructed/2308021-03.registered.reconstructed.sut')
    if not path.exists():
        pytest.skip('Local read-only brush fixture is unavailable')
    before = path.read_bytes()
    # This checks the real source parameters, not material decoding (covered
    # separately). Avoid allocating the 1967x1962 texture for scalar assertions.
    monkeypatch.setattr(sut_import, '_material_file',
                        lambda *args, **kwargs: (Image.new('RGBA', (1, 1), 'gray'), {}))
    brush = import_sut(path)
    assert brush.texture.contrast == -.55
    assert brush.texture.scale == .35
    assert brush.texture.invert
    assert brush.source['variant']['TextureContrast'] == -55
    assert brush.source['variant']['TextureScale2'] == 35
    assert brush.source['variant']['TextureReverseDensity'] == 1
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    assert path.read_bytes() == before
