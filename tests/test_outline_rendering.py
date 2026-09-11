"""Outline raster equivalence and cache-invalidation regression coverage."""
import numpy as np
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QColor, QFont, QImage, QPainter
from scipy.ndimage import distance_transform_edt

from comic_editor.core.models import (
    HueSaturationLightnessModifier, OutlineModifier, ParameterMaskBinding,
)
from comic_editor.ui.modifier_rendering import (
    OutlineDistanceCache, _parameter_field, _premultiplied_qimage,
    _qimage_premultiplied, apply_modifier_stack,
)


def text_image(font_family, width=420, height=180):
    image = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.setFont(QFont(font_family, 40))
    painter.setPen(QColor(210, 90, 35, 183))
    painter.drawText(image.rect(), Qt.AlignmentFlag.AlignCenter, "Outline O")
    painter.end()
    return image


def rgba8(image):
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    return np.frombuffer(converted.constBits(), dtype=np.uint8).reshape(
        converted.height(), converted.width(), 4
    ).copy()


def reference_outline(image, modifier, masks):
    """The original full-image float implementation, independent of fast paths."""
    return _premultiplied_qimage(reference_outline_array(
        _qimage_premultiplied(image), modifier, masks))


def reference_outline_array(original, modifier, masks):
    alpha = original[..., 3]
    distance = (distance_transform_edt(alpha <= 1e-6).astype(np.float32)
                if np.any(alpha > 1e-6) else np.full(alpha.shape, np.inf))
    fields = {
        name: _parameter_field(modifier, name, getattr(modifier, name), alpha.shape, masks)
        for name in ("thickness", "opacity", "intensity")
    }
    coverage = np.clip(fields["thickness"] + 0.5 - distance, 0, 1)
    coverage *= np.clip(1 - alpha, 0, 1)
    coverage *= np.clip(np.asarray(fields["opacity"], dtype=np.float32) / 100, 0, 1)
    color = modifier.color.lstrip("#")
    rgba = np.asarray([int(color[index:index + 2], 16)
                       for index in (2, 4, 6, 0)], dtype=np.float32) / 255
    outlined = np.zeros_like(original)
    outlined[..., 3] = coverage * rgba[3]
    outlined[..., :3] = rgba[:3] * outlined[..., 3:4]
    effect = original + outlined * (1 - original[..., 3:4])
    amount = np.asarray(fields["intensity"], dtype=np.float32) / 100
    if amount.ndim:
        amount = amount[..., None]
    return original * (1 - amount) + effect * amount


@pytest.mark.parametrize("masked", [False, True])
@pytest.mark.parametrize("thickness,color", [
    (0, "#FF000000"), (0.8, "#803366EE"), (3.25, "#BBD09020"), (25, "#FFFFFFFF"),
])
def test_fast_outline_matches_exact_outside_reference(text_outline_font_family, masked, thickness, color):
    image = text_image(text_outline_font_family)
    before = rgba8(image)
    modifier = OutlineModifier(thickness=thickness, color=color, opacity=63, intensity=71)
    masks = {}
    if masked:
        rng = np.random.default_rng(882)
        for name, black, white in (("thickness", 25, 0), ("opacity", 0, 100),
                                    ("intensity", 100, 0)):
            modifier.parameter_masks[name] = ParameterMaskBinding("mask", black, white)
            masks[(modifier.modifier_id, name)] = rng.random(
                (image.height(), image.width()), dtype=np.float32)
    modifier.validate()
    actual = apply_modifier_stack(image, [modifier], (0, 0), masks,
                                  outline_distance_cache=OutlineDistanceCache())
    expected = reference_outline(image, modifier, masks)
    # Direct RGBA8 arithmetic removes intermediate float round trips. Their
    # rounding can differ by one byte, but coverage and colors remain exact.
    np.testing.assert_allclose(rgba8(actual), rgba8(expected), atol=1)
    np.testing.assert_array_equal(rgba8(image), before)


def test_single_outline_and_mixed_stack_paths_match(text_outline_font_family):
    image = text_image(text_outline_font_family)
    modifier = OutlineModifier(thickness=7.2, color="#AAB123FF", intensity=53)
    direct = apply_modifier_stack(image, [modifier], (0, 0))
    generic = apply_modifier_stack(
        image, [HueSaturationLightnessModifier(intensity=0), modifier], (0, 0)
    )
    np.testing.assert_allclose(rgba8(direct), rgba8(generic), atol=1)


@pytest.mark.parametrize("image_format", [QImage.Format.Format_ARGB32_Premultiplied,
                                         QImage.Format.Format_RGBA8888,
                                         QImage.Format.Format_RGBA8888_Premultiplied])
def test_fast_outline_preserves_edge_pixels_and_image_formats(image_format):
    image = QImage(67, 43, image_format)
    image.fill(Qt.GlobalColor.transparent)
    for x, y in ((0, 0), (66, 0), (0, 42), (66, 42), (33, 21)):
        image.setPixelColor(x, y, QColor(170, 42, 225, 123))
    modifier = OutlineModifier(thickness=2.4, color="#BBA903FF", intensity=61)
    expected = reference_outline(image, modifier, {})
    actual = apply_modifier_stack(image, [modifier], (0, 0))
    np.testing.assert_allclose(rgba8(actual), rgba8(expected), atol=1)


def test_outline_stack_uses_each_preceding_silhouette(text_outline_font_family):
    image = text_image(text_outline_font_family)
    first = OutlineModifier(thickness=2, color="#FFFF0000")
    second = OutlineModifier(thickness=3, color="#FF0000FF", intensity=74)
    cache = OutlineDistanceCache()
    actual = apply_modifier_stack(image, [first, second], (0, 0),
                                  outline_distance_cache=cache)
    expected = reference_outline(reference_outline(image, first, {}), second, {})
    np.testing.assert_allclose(rgba8(actual), rgba8(expected), atol=2)
    assert cache.computations == 2


@pytest.mark.parametrize("masked", [False, True])
def test_stacked_outline_crop_preserves_fractional_alpha_and_masks(text_outline_font_family, masked):
    image = text_image(text_outline_font_family, 820, 450)
    modifiers = [OutlineModifier(thickness=1.8, color="#01FF0000", intensity=0.5),
                 OutlineModifier(thickness=4.2, color="#8800FF00", opacity=51),
                 OutlineModifier(thickness=6.1, color="#FF3300FF", intensity=87)]
    masks = {}
    if masked:
        rng = np.random.default_rng(340)
        for modifier in modifiers:
            for name, black, white in (("thickness", 25, 0), ("opacity", 100, 0),
                                        ("intensity", 0, 100)):
                modifier.parameter_masks[name] = ParameterMaskBinding("mask", black, white)
                masks[(modifier.modifier_id, name)] = rng.random(
                    (image.height(), image.width()), dtype=np.float32)
    expected = _qimage_premultiplied(image)
    for modifier in modifiers:
        expected = reference_outline_array(expected, modifier, masks)
    actual = apply_modifier_stack(image, modifiers, (20, -8), masks,
                                  outline_distance_cache=OutlineDistanceCache())
    np.testing.assert_allclose(rgba8(actual), rgba8(_premultiplied_qimage(expected)), atol=1)


def test_distance_cache_reuses_only_identical_silhouettes():
    cache = OutlineDistanceCache()
    alpha = np.zeros((90, 180), dtype=np.float32)
    alpha[40:50, 70:100] = 0.25
    first = cache.distance(alpha)
    alpha[40:50, 70:100] = 0.8
    assert cache.distance(alpha) is first
    assert cache.computations == 1
    alpha[40, 69] = 1
    changed = cache.distance(alpha)
    assert cache.computations == 2
    assert first[40, 69] == 1
    assert changed[40, 69] == 0


def test_bounded_distance_field_is_exact_and_omits_empty_canvas():
    alpha = np.zeros((700, 1200), dtype=np.uint8)
    alpha[340:370, 550:650] = 180
    alpha[350:360, 570:580] = 0
    cache = OutlineDistanceCache()
    distance, bounds, extent = cache.field(alpha, margin=32)
    left, top, right, bottom = extent
    expected = distance_transform_edt(alpha == 0).astype(np.float32)
    np.testing.assert_array_equal(distance, expected[top:bottom, left:right])
    assert bounds == (550, 340, 650, 370)
    assert cache.bytes == distance.nbytes < expected.nbytes / 20


def test_outline_distance_cache_evicts_and_clears_within_budget():
    alpha = np.zeros((12, 12), dtype=np.float32)
    alpha[5, 5] = 1
    cache = OutlineDistanceCache(budget=alpha.nbytes)
    cache.distance(alpha)
    moved = np.roll(alpha, 1, axis=0)
    cache.distance(moved)
    assert cache.bytes == alpha.nbytes
    assert cache.computations == 2
    cache.distance(alpha)
    assert cache.computations == 3
    cache.clear()
    assert cache.bytes == 0
    assert not cache._values


def test_transparent_and_zero_contribution_outlines_are_unchanged(text_outline_font_family):
    image = text_image(text_outline_font_family)
    for modifier in (OutlineModifier(opacity=0), OutlineModifier(intensity=0),
                     OutlineModifier(color="#00000000")):
        assert apply_modifier_stack(image, [modifier], (0, 0)) == image
    image.fill(Qt.GlobalColor.transparent)
    assert apply_modifier_stack(image, [OutlineModifier()], (0, 0)) == image
