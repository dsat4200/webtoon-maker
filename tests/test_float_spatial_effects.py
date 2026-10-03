"""Spatial stages preserve high precision, HDR, coverage, and region agreement."""
from dataclasses import replace
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRect, QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.cage import CageGrid
from comic_editor.core.models import DistortModifier, HalftoneModifier
from comic_editor.render.pixels import (
    FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, working_image, premultiplied_pixels,
)
from comic_editor.ui.cage_rendering import warp_image
from comic_editor.ui.distort_rendering import PreparedDistortCache, render_distort
from comic_editor.ui.modifier_rendering import apply_opacity_mask
from comic_editor.ui.pattern_rendering import apply_pattern_effect, halftone_region


def hdr_source(width=64, height=48):
    x, y = np.meshgrid(np.arange(width), np.arange(height))
    alpha = np.full((height, width), .31739, np.float32)
    return np.stack((1.12345 + x*.001, -.07321-y*.0001,
                     np.full_like(alpha, .00071), alpha), axis=-1).astype(np.float32)


@pytest.mark.parametrize('precision,tolerance', [('float32', 2e-6), ('float16', .001)])
@pytest.mark.parametrize('effect', ['affine', 'mesh_warp', 'perspective', 'equations'])
def test_neutral_distortions_keep_hdr_negative_color_and_subbyte_coverage(precision, tolerance, effect):
    contract = replace(FLOAT_PIXELS, precision=precision)
    values = hdr_source()
    bounds = QRectF(0, 0, 64, 48)
    modifier = DistortModifier(modifier_type='distort_'+effect,
                              frame=bounds.getRect(), parameters={'interpolation': 'bilinear'})
    modifier.validate()
    with pixel_scope(contract):
        image = working_image(values)
        result = render_distort(image, bounds, modifier, output_bounds=bounds)
        assert result.format() == contract.image_format
        np.testing.assert_allclose(premultiplied_pixels(result), values, atol=tolerance, rtol=0)
        np.testing.assert_allclose(premultiplied_pixels(image), values, atol=tolerance, rtol=0)


def test_active_fractional_warp_is_linear_and_regions_agree_with_complete_result():
    values = hdr_source()
    other = values.copy()
    other[..., :3] *= .37
    modifier = DistortModifier(modifier_type='distort_equations', frame=(0, 0, 64, 48),
        parameters={'x_expression': 'x+0.3*sin(y/6)', 'y_expression': 'y+0.23',
                    'interpolation': 'bilinear', 'edges': 'transparent'})
    modifier.validate()
    bounds, region = QRectF(0, 0, 64, 48), QRectF(7, 9, 23, 19)
    with pixel_scope(FLOAT_PIXELS):
        cache = PreparedDistortCache()
        image = working_image(values)
        full = render_distort(image, bounds, modifier, output_bounds=bounds, preparation_cache=cache)
        crop = render_distort(image, bounds, modifier, output_bounds=region, preparation_cache=cache)
        np.testing.assert_allclose(premultiplied_pixels(crop), premultiplied_pixels(full)[9:28, 7:30], atol=2e-6, rtol=0)
        second = render_distort(working_image(other), bounds, modifier, output_bounds=bounds)
        mixed = render_distort(working_image(values*.61+other*.39), bounds, modifier, output_bounds=bounds)
        np.testing.assert_allclose(premultiplied_pixels(mixed),
            premultiplied_pixels(full)*.61+premultiplied_pixels(second)*.39, atol=3e-7, rtol=0)


def test_preparation_cache_does_not_reuse_legacy_decoding_in_float_scope():
    values = hdr_source()
    image = working_image(values, FLOAT_PIXELS)
    cache = PreparedDistortCache()
    with pixel_scope(LEGACY_PIXELS):
        legacy = cache.source(image)
    with pixel_scope(FLOAT_PIXELS):
        high = cache.source(image)
        assert high is cache.source(image)
    assert high is not legacy
    np.testing.assert_array_equal(high.pixels, values)
    assert legacy.pixels[..., 0].max() <= 1.


@pytest.mark.parametrize('interpolation', ['nearest', 'bilinear', 'bicubic'])
def test_cage_translation_and_flip_preserve_hdr_pixels(interpolation):
    values = hdr_source(32, 24)
    bounds = QRectF(0, 0, 32, 24)
    grid = CageGrid(frame=bounds.getRect(), interpolation=interpolation)
    grid.validate_grid()
    grid.points = [(x+13, y-7) for x, y in grid.rest_points()]
    with pixel_scope(FLOAT_PIXELS):
        image = working_image(values)
        translated, actual_bounds = warp_image(image, bounds, grid)
        assert actual_bounds == QRectF(13, -7, 32, 24)
        np.testing.assert_allclose(premultiplied_pixels(translated), values, atol=2e-6, rtol=0)
        grid.points = [(32-x, y) for x, y in grid.rest_points()]
        flipped, _ = warp_image(image, bounds, grid)
        np.testing.assert_allclose(premultiplied_pixels(flipped), values[:, ::-1], atol=2e-6, rtol=0)


def test_smudge_checkpoints_and_opacity_never_round_through_display_bytes(qapp):
    from test_smudge_rendering import stroke, modifier
    values = hdr_source(96, 48)
    bounds, region = QRectF(0, 0, 96, 48), QRectF(19, 12, 43, 19)
    effect = modifier(stroke(start=(19, 24), end=(72, 24), radius=9))
    cache = PreparedDistortCache()
    with pixel_scope(FLOAT_PIXELS):
        image = working_image(values)
        full = render_distort(image, bounds, effect, output_bounds=bounds, preparation_cache=cache)
        crop = render_distort(image, bounds, effect, output_bounds=region, preparation_cache=cache)
        np.testing.assert_array_equal(premultiplied_pixels(crop), premultiplied_pixels(full)[12:31, 19:62])
        effect.parameters['opacity'] = 37.
        mixed = render_distort(image, bounds, effect, output_bounds=bounds, preparation_cache=cache)
        np.testing.assert_allclose(premultiplied_pixels(mixed),
            values+(premultiplied_pixels(full)-values)*.37, atol=2e-7, rtol=0)
        assert not any(key[1:2] == ('pixels',) for key in cache._entries)
        np.testing.assert_array_equal(premultiplied_pixels(image), values)
        assert premultiplied_pixels(mixed)[0, 0, 0] > 1


def test_float_mask_preserves_subbyte_coverage_and_does_not_mutate_inputs():
    values = hdr_source(31, 19)
    mask = np.linspace(-.25, 1.25, 31*19, dtype=np.float32).reshape(19, 31)
    original_mask = mask.copy()
    with pixel_scope(FLOAT_PIXELS):
        image = working_image(values)
        result = apply_opacity_mask(image, mask, .17, .81)
        expected = values * (np.clip(mask, 0, 1)*(.81-.17)+.17)[..., None]
        np.testing.assert_allclose(premultiplied_pixels(result), expected, atol=2e-7, rtol=0)
        np.testing.assert_array_equal(premultiplied_pixels(image), values)
    np.testing.assert_array_equal(mask, original_mask)


def test_float_halftone_regions_preserve_coverage_without_byte_rounding(qapp):
    values = hdr_source(193, 173)
    values[..., :3] = .15*values[..., 3:4]
    modifier = HalftoneModifier(spacing=.7, transparent_background=False)
    modifier.validate()
    region = QRect(35, 29, 97, 85)
    with pixel_scope(FLOAT_PIXELS):
        image = working_image(values)
        full = apply_pattern_effect(image, modifier)
        crop = halftone_region(image, modifier, region, tile_size=31)
        assert crop.format() == FLOAT_PIXELS.image_format
        np.testing.assert_allclose(premultiplied_pixels(crop), premultiplied_pixels(full)[29:114, 35:132], atol=2e-6, rtol=0)
        np.testing.assert_array_equal(premultiplied_pixels(full)[..., 3], values[..., 3])


def test_float_halftone_buffers_use_color_fill_overload(qapp, monkeypatch):
    from PySide6.QtGui import QImage
    from comic_editor.ui.pattern_rendering import _halftone_strips

    original_fill = QImage.fill
    fills = []

    def checked_fill(image, color):
        if image.depth() > 32:
            # Qt documents the integer-pixel overload as undefined at this depth.
            assert not isinstance(color, int)
            fills.append(image.size())
        return original_fill(image, color)

    monkeypatch.setattr(QImage, 'fill', checked_fill)
    modifier = HalftoneModifier(spacing=.3, transparent_background=False)
    modifier.validate()
    values = hdr_source(129, 113)
    values[..., :3] = .15*values[..., 3:4]
    with pixel_scope(FLOAT_PIXELS):
        image = working_image(values)
        full = apply_pattern_effect(image, modifier)
        strips = _halftone_strips(image, modifier, None, None,
                                 strip_height=31, tile_width=37)
        assert strips is not None
        region = halftone_region(image, modifier, QRect(9, 11, 79, 67), tile_size=23)
        np.testing.assert_allclose(premultiplied_pixels(strips),
                                   premultiplied_pixels(full), atol=2e-6, rtol=0)
        np.testing.assert_allclose(premultiplied_pixels(region),
            premultiplied_pixels(full)[11:78, 9:88], atol=2e-6, rtol=0)
    assert len(fills) == 2


@pytest.mark.parametrize('module', ['gpu_pattern_effects', 'gpu_textures', 'gpu_object_blending'])
def test_byte_gpu_backends_decline_float_work_without_creating_a_context(module, monkeypatch):
    import importlib
    renderer = importlib.import_module('comic_editor.ui.'+module)
    canvas = SimpleNamespace(settings=SimpleNamespace(canvas_renderer='gpu'))
    with pixel_scope(FLOAT_PIXELS):
        assert renderer.renderer_for(canvas) is None
    assert vars(canvas).keys() == {'settings'}


@pytest.mark.parametrize('mode', ['multiply', 'color', 'luma_modulate', 'height_modulate'])
def test_custom_float_blends_keep_hdr_backdrop_under_transparent_source(mode):
    from comic_editor.ui.object_blending import _custom_composite
    values = hdr_source(31, 19)
    halo = 1 if mode == 'height_modulate' else 0
    source = np.zeros((19+halo*2, 31+halo*2, 4), np.float32)
    with pixel_scope(FLOAT_PIXELS):
        result = _custom_composite(working_image(values), working_image(source), mode,
                                   QRect(halo, halo, 31, 19), 1.)
        np.testing.assert_array_equal(premultiplied_pixels(result), values)
