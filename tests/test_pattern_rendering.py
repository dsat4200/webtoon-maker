"""Pattern fallbacks stay useful when a GPU context is unavailable."""
from __future__ import annotations

from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QImage

from comic_editor.core.models import HALFTONE_DOT_STYLES, HALFTONE_GRIDS, HalftoneModifier, PixelateModifier
from comic_editor.ui.pattern_rendering import (
    apply_pattern_effect, custom_stamp, delaunay_triangles, gradient_lut,
    halftone_points, halftone_unit,
)


def image_from_rgba(array):
    array = np.ascontiguousarray(array, dtype=np.uint8)
    height, width = array.shape[:2]
    return QImage(array.data, width, height, width * 4, QImage.Format.Format_RGBA8888).copy()


def rgba_from_image(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888)
    return np.frombuffer(image.constBits(), dtype=np.uint8, count=image.sizeInBytes()).reshape(
        image.height(), image.bytesPerLine())[:, :image.width() * 4].reshape(image.height(), image.width(), 4).copy()


def solid(color=(96, 96, 96, 255), width=100, height=100):
    return image_from_rgba(np.tile(np.array(color, dtype=np.uint8), (height, width, 1)))


def halftone(**kwargs):
    return HalftoneModifier(base_resolution=100, spacing=16, blur=0, **kwargs)


@pytest.mark.parametrize("grid", HALFTONE_GRIDS)
def test_all_grids_preserve_source_dimensions_and_silhouette(grid):
    data = np.full((80, 100, 4), 96, dtype=np.uint8)
    data[..., 3] = np.arange(100, dtype=np.uint8)[None, :] * 2
    data[:, :15, 3] = 0
    result = rgba_from_image(apply_pattern_effect(image_from_rgba(data), halftone(grid_type=grid)))
    assert result.shape == data.shape
    np.testing.assert_array_equal(result[..., 3], data[..., 3])
    assert not result[:, :15].any()
    assert result[:, 20:, 0].max() > result[:, 20:, 0].min() + 80


@pytest.mark.parametrize("style", HALFTONE_DOT_STYLES)
def test_every_dot_style_renders_visible_geometry(style):
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><path d="M10 0 L20 20 L0 20Z" fill="red"/></svg>'
    result = rgba_from_image(apply_pattern_effect(solid(), halftone(dot_style=style, custom_svg=svg)))
    assert result[..., 0].min() < 70
    assert result[..., 0].max() > 150
    assert np.all(result[..., 3] == 255)


def test_transparent_background_punches_holes_and_preserves_ink_alpha():
    data = rgba_from_image(apply_pattern_effect(solid((96, 96, 96, 128)),
                                               halftone(transparent_background=True)))
    assert data[..., 3].max() == 128
    assert data[..., 3].min() == 0
    assert np.all(data[..., :3] == 0)


def test_source_color_uses_rgb_instead_of_luminance():
    result = rgba_from_image(apply_pattern_effect(solid((190, 30, 70, 255)),
                                                 halftone(color_mode="source", transparent_background=True)))
    ink = result[..., 3] > 250
    assert np.count_nonzero(ink) > 100
    np.testing.assert_allclose(result[ink, :3], np.tile([190, 30, 70], (ink.sum(), 1)), atol=1)


def test_inversion_and_level_mask_control_visible_ink():
    modifier = halftone()
    white = solid((255, 255, 255, 255))
    assert np.all(rgba_from_image(apply_pattern_effect(white, modifier))[..., :3] == 255)
    inverted = rgba_from_image(apply_pattern_effect(white, replace(modifier, invert=True)))
    assert np.mean(inverted[..., :3]) < 10
    excluded = rgba_from_image(apply_pattern_effect(solid(), replace(modifier, level_max=.2)))
    assert np.all(excluded[..., :3] == 255)


def test_zero_gamma_and_equal_clamp_endpoints_are_finite():
    result = rgba_from_image(apply_pattern_effect(solid(), halftone(gamma=0, clamp_min=.5, clamp_max=.5)))
    assert result.shape == (100, 100, 4)
    assert np.all(result[..., 3] == 255)


def test_fallback_returns_full_effect_without_applying_intensity_twice():
    source = solid((200, 40, 50, 255))
    full = rgba_from_image(apply_pattern_effect(source, halftone()))
    zero = rgba_from_image(apply_pattern_effect(source, halftone(intensity=0)))
    np.testing.assert_array_equal(full, zero)


def test_gradient_lookup_includes_alpha_and_all_stops():
    stops = [[0., "#FFFF0000"], [.5, "#8000FF00"], [1., "#FF0000FF"]]
    lookup = gradient_lut(stops, "rgb", 3)
    np.testing.assert_allclose(lookup[0], [1., 0., 0., 1.], atol=1 / 255)
    np.testing.assert_allclose(lookup[1], [0., 1., 0., 128 / 255], atol=1 / 255)
    np.testing.assert_allclose(lookup[2], [0., 0., 1., 1.], atol=1 / 255)
    assert lookup is gradient_lut(stops, "rgb", 3)
    assert not lookup.flags.writeable


def test_default_gradient_keeps_dark_source_as_dark_ink():
    result = rgba_from_image(apply_pattern_effect(solid((0, 0, 0, 255)),
                                                 halftone(color_mode="gradient")))
    assert np.mean(result[..., :3]) < 10


def test_oklch_is_perceptual_and_handles_achromatic_endpoints():
    stops = [[0., "#FFFF0000"], [1., "#FF0000FF"]]
    rgb, perceptual = gradient_lut(stops, "rgb", 11), gradient_lut(stops, "oklch", 11)
    np.testing.assert_allclose(rgb[[0, -1]], perceptual[[0, -1]], atol=1e-5)
    assert np.linalg.norm(rgb[5, :3] - perceptual[5, :3]) > .1
    neutral = gradient_lut([[0., "#FF000000"], [1., "#FFFFFFFF"]], "oklch", 11)
    assert np.isfinite(neutral).all()
    np.testing.assert_allclose(neutral[:, 0], neutral[:, 1], atol=1e-5)


def test_custom_svg_shares_cached_rgba_stamp_and_native_color_mode():
    svg = '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 20 20"><circle cx="10" cy="10" r="9" fill="#00ff00"/></svg>'
    stamp = custom_stamp(svg)
    assert stamp is custom_stamp(svg)
    center = rgba_from_image(stamp)[128, 128]
    np.testing.assert_array_equal(center, [0, 255, 0, 255])
    native = rgba_from_image(apply_pattern_effect(solid(), halftone(
        dot_style="custom", custom_svg=svg, custom_render_mode="original", transparent_background=True)))
    ink = native[..., 3] == 255
    assert np.any(ink)
    np.testing.assert_array_equal(native[ink, :3], np.tile([0, 255, 0], (ink.sum(), 1)))


def test_geometry_helpers_cache_and_apply_fit_rotation_and_seed():
    modifier = halftone()
    assert halftone_unit(100, 200, modifier) == 1.
    assert halftone_unit(100, 200, replace(modifier, fit_mode="long")) == 2.
    triangles = delaunay_triangles(100, 200, modifier)
    assert triangles.ndim == 3 and triangles.shape[1:] == (3, 2)
    assert len(triangles) > 100
    assert triangles is delaunay_triangles(100, 200, modifier)
    stipple = replace(modifier, grid_type="stippling")
    a, b = halftone_points(100, 200, stipple), halftone_points(100, 200, replace(stipple, stipple_seed=2))
    assert not np.array_equal(a, b)


def test_advanced_style_controls_change_their_geometry():
    source = solid((153, 153, 153, 255))
    blob = halftone(dot_style="blob")
    original = rgba_from_image(apply_pattern_effect(source, blob))
    for attribute, value in (("max_necks", 0), ("merge_strength", 3), ("min_neck_width", 0)):
        changed = rgba_from_image(apply_pattern_effect(source, replace(blob, **{attribute: value})))
        assert np.any(original != changed), attribute
    liquid = replace(blob, dot_style="liquid")
    sharp = rgba_from_image(apply_pattern_effect(source, liquid))
    rounded = rgba_from_image(apply_pattern_effect(source, replace(liquid, corner_rounding=1)))
    assert np.mean(np.abs(sharp.astype(float) - rounded.astype(float))) > 1
    data = rgba_from_image(source)
    data[..., :3] = np.linspace(20, 230, source.width(), dtype=np.uint8)[None, :, None]
    source = image_from_rgba(data)
    varied = rgba_from_image(apply_pattern_effect(source, liquid))
    even = rgba_from_image(apply_pattern_effect(source, replace(liquid, even_merge_tone=True)))
    assert np.any(varied != even)


def test_delaunay_independent_rotation_changes_faces():
    source = solid()
    modifier = halftone(dot_style="delaunay", link_rotation=False)
    normal = rgba_from_image(apply_pattern_effect(source, modifier))
    rotated = rgba_from_image(apply_pattern_effect(source, replace(modifier, dot_rotation=60)))
    assert np.mean(np.abs(normal.astype(float) - rotated.astype(float))) > 5


def test_thin_rotated_delaunay_geometry_has_bounded_allocation():
    faces = delaunay_triangles(8192, 1, HalftoneModifier(rotation=45))
    assert 0 < len(faces) < 200000
    assert np.isfinite(faces).all()


def test_pixelate_groups_pixels_and_honors_render_scale():
    data = np.zeros((8, 8, 4), dtype=np.uint8)
    data[..., 0] = np.arange(8)[None, :] * 30
    data[..., 3] = 255
    source = image_from_rgba(data)
    small = rgba_from_image(apply_pattern_effect(source, PixelateModifier(pixel_size=2)))
    large = rgba_from_image(apply_pattern_effect(source, PixelateModifier(pixel_size=2), scale=2))
    assert len(np.unique(small[0, :, 0])) == 4
    assert len(np.unique(large[0, :, 0])) == 2
    assert np.all(large[:, :4, 0] == large[0, 0, 0])


def test_pixelate_preblur_ignores_hidden_transparent_colors():
    data = np.zeros((16, 16, 4), dtype=np.uint8)
    data[:, :8] = [255, 0, 0, 255]
    data[:, 8:] = [0, 0, 255, 0]
    modifier = PixelateModifier(pixel_size=4, blur=3)
    result = rgba_from_image(apply_pattern_effect(image_from_rgba(data), modifier))
    assert np.any((result[..., 3] > 0) & (result[..., 3] < 255))
    assert np.all(result[..., 2] == 0)
    assert np.all(result[result[..., 3] > 0, 0] == 255)


def test_pixelate_brightness_contrast_and_saturation_apply():
    source = solid((200, 50, 20, 255), width=8, height=8)
    desaturated = rgba_from_image(apply_pattern_effect(source, PixelateModifier(saturation=-100)))
    assert np.all(desaturated[..., 0] == desaturated[..., 1])
    assert np.all(desaturated[..., 1] == desaturated[..., 2])
    white = rgba_from_image(apply_pattern_effect(source, PixelateModifier(brightness=100)))
    assert np.all(white[..., :3] == 255)
    gray = rgba_from_image(apply_pattern_effect(source, PixelateModifier(contrast=-100)))
    assert np.all(gray[..., :3] == 128)


def test_pixelate_blur_changes_sampling_before_blocks_are_formed():
    data = np.zeros((16, 16, 4), dtype=np.uint8)
    data[..., 3] = 255
    data[:, 6:8, :3] = 255
    source = image_from_rgba(data)
    crisp = rgba_from_image(apply_pattern_effect(source, PixelateModifier(pixel_size=8)))
    blurred = rgba_from_image(apply_pattern_effect(source, PixelateModifier(pixel_size=8, blur=3)))
    assert np.mean(blurred[..., :3]) > np.mean(crisp[..., :3])
    assert np.all(blurred[:, :8, 0] == blurred[0, 0, 0])


def test_null_images_are_safe():
    assert apply_pattern_effect(QImage(), halftone()).isNull()
    assert apply_pattern_effect(QImage(), PixelateModifier()).isNull()
