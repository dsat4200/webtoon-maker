"""Outline-only Gaussian blur preserves source pixels and all rendering routes."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from scipy.ndimage import distance_transform_edt, gaussian_filter

from comic_editor.core.effect_geometry import effect_bounds, outline_blur_padding
from comic_editor.core.models import (
    HueSaturationLightnessModifier, OutlineModifier, ParameterMaskBinding, modifier_from_dict,
)
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.ui.modifier_rendering import (
    _premultiplied_qimage, _qimage_premultiplied, apply_modifier_stack,
)
from comic_editor.ui.thumbnail_effects import scaled_modifiers


def source_image():
    image = QImage(170, 130, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setPen(Qt.NoPen)
    painter.setBrush(QColor(17, 210, 43, 255))
    painter.drawEllipse(QRectF(55.25, 40.5, 38, 36))
    painter.setBrush(QColor(221, 32, 51, 112))
    painter.drawRect(QRectF(83, 61, 17, 15))
    painter.end()
    return image


def bytes_rgba(image):
    image = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()


def reference(original, modifier, radius=None, strength=None):
    """Construct the independent outline, blur it, then source-over under ink."""
    alpha = original[..., 3]
    distance = distance_transform_edt(alpha <= 1e-6)
    coverage = (np.clip(modifier.thickness + .5 - distance, 0, 1) if modifier.antialiasing
                else (distance <= modifier.thickness).astype(float))
    color = QColor(modifier.color)
    outline = np.zeros_like(original)
    outline[..., 3] = coverage * (1 - alpha) * modifier.opacity / 100 * color.alphaF()
    outline[..., :3] = outline[..., 3:4] * np.array([color.redF(), color.greenF(), color.blueF()])
    radius = modifier.blur_radius if radius is None else radius
    strength = modifier.blur_strength if strength is None else strength
    if np.ndim(radius):
        wet = np.zeros_like(outline)
        for value in np.unique(radius):
            level = gaussian_filter(outline, (value, value, 0), mode="constant", truncate=3)
            wet[radius == value] = level[radius == value]
    else:
        wet = gaussian_filter(outline, (radius, radius, 0), mode="constant", truncate=3)
    blend = np.asarray(strength) / 100
    if blend.ndim:
        blend = blend[..., None]
    outline = outline * (1 - blend) + wet * blend
    return original + outline * (1 - alpha[..., None]) * modifier.intensity / 100


@pytest.mark.parametrize("antialiasing", [False, True])
@pytest.mark.parametrize("radius,strength", [(1, 100), (4, 25), (8, 100), (100, 100)])
def test_blur_matches_separate_outline_reference_and_preserves_source(antialiasing, radius, strength):
    image = source_image()
    original = _qimage_premultiplied(image)
    modifier = OutlineModifier(thickness=5.5, opacity=73, intensity=81, color="#AB9849EB",
                               blur_radius=radius, blur_strength=strength, antialiasing=antialiasing)
    expected = bytes_rgba(_premultiplied_qimage(reference(original, modifier)))
    actual = bytes_rgba(apply_modifier_stack(image, [modifier], (0, 0)))
    np.testing.assert_allclose(actual, expected, atol=1)
    opaque = bytes_rgba(image)[..., 3] == 255
    np.testing.assert_array_equal(actual[opaque], bytes_rgba(image)[opaque])
    np.testing.assert_array_equal(_qimage_premultiplied(image), original)


@pytest.mark.parametrize("radius,strength", [(100, 0), (0, 100), (0, 0)])
def test_disabled_blur_is_byte_identical_to_original_outline(radius, strength):
    image = source_image()
    legacy = OutlineModifier(thickness=4.25, intensity=67, opacity=59, color="#BC5921AA")
    modifier = deepcopy(legacy)
    modifier.blur_radius, modifier.blur_strength = radius, strength
    assert apply_modifier_stack(image, [modifier], (0, 0)) == apply_modifier_stack(image, [legacy], (0, 0))


@pytest.mark.parametrize("masked_parameter", ["blur_radius", "blur_strength"])
def test_blur_parameter_masks_follow_spatial_endpoints(masked_parameter):
    image = source_image()
    original = _qimage_premultiplied(image)
    modifier = OutlineModifier(thickness=6, blur_radius=4, blur_strength=100)
    mask = np.zeros(original.shape[:2], np.float32)
    mask[:, 75:] = 1
    white = 4 if masked_parameter == "blur_radius" else 100
    modifier.parameter_masks[masked_parameter] = ParameterMaskBinding("mask", 0, white)
    fields = {(modifier.modifier_id, masked_parameter): mask}
    expected = reference(original, modifier, **{
        "radius" if masked_parameter == "blur_radius" else "strength": mask * white})
    actual = apply_modifier_stack(image, [modifier], (0, 0), fields)
    np.testing.assert_allclose(bytes_rgba(actual), bytes_rgba(_premultiplied_qimage(expected)), atol=1)


def test_single_stacked_and_mixed_outline_routes_match():
    image = source_image()
    first = OutlineModifier(thickness=5, blur_radius=3, blur_strength=70, color="#BE2030CC")
    second = OutlineModifier(thickness=3, blur_radius=2, blur_strength=40, color="#FF992211")
    single = apply_modifier_stack(image, [first], (0, 0))
    generic = apply_modifier_stack(image, [HueSaturationLightnessModifier(intensity=0), first], (0, 0))
    np.testing.assert_allclose(bytes_rgba(single), bytes_rgba(generic), atol=1)
    expected = reference(reference(_qimage_premultiplied(image), first), second)
    actual = apply_modifier_stack(image, [first, second], (0, 0))
    np.testing.assert_allclose(bytes_rgba(actual), bytes_rgba(_premultiplied_qimage(expected)), atol=1)


def test_radius_mask_interpolation_does_not_depend_on_capture_extent():
    image = source_image()
    modifier = OutlineModifier(thickness=5, blur_radius=0, blur_strength=100)
    modifier.parameter_masks["blur_radius"] = ParameterMaskBinding("radius", 0, 6.3)
    mask = np.tile(np.linspace(0, 1, image.width(), dtype=np.float32), (image.height(), 1))
    key = (modifier.modifier_id, "blur_radius")
    full = apply_modifier_stack(image, [modifier], (0, 0), {key: mask})
    cropped = apply_modifier_stack(image.copy(0, 0, 140, 130), [modifier], (0, 0), {key: mask[:, :140]})
    np.testing.assert_array_equal(bytes_rgba(full)[:, :130], bytes_rgba(cropped)[:, :130])


def test_uniform_intermediate_mask_radius_retains_upper_kernel_halo():
    image = QImage(180, 150, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    for y in range(72, 76):
        for x in range(84, 88):
            image.setPixelColor(x, y, QColor("white"))
    modifier = OutlineModifier(thickness=24, blur_strength=100)
    modifier.parameter_masks["blur_radius"] = ParameterMaskBinding("radius", 0, 6.3)
    original = _qimage_premultiplied(image)
    lower = reference(original, modifier, radius=4)
    upper = reference(original, modifier, radius=6.3)
    expected = lower + (upper - lower) * ((4.2 - 4) / (6.3 - 4))
    mask = np.full((150, 180), 4.2 / 6.3, np.float32)
    actual = apply_modifier_stack(image, [modifier], (0, 0), {
        (modifier.modifier_id, "blur_radius"): mask,
    })
    np.testing.assert_allclose(bytes_rgba(actual), bytes_rgba(_premultiplied_qimage(expected)), atol=1)
    # Both the internal cropped work region and the document bounds use the
    # largest interpolation kernel, even though this entire mask is only 4.2.
    from comic_editor.ui.modifier_rendering import _outline_blur_fringe
    assert _outline_blur_fringe({"blur_radius": mask * 6.3, "blur_radius_limit": 6.3,
                                "blur_strength": 100}) == 19


def test_blur_bounds_masks_thumbnail_and_serialization():
    modifier = OutlineModifier(thickness=4, blur_radius=7, blur_strength=0)
    assert outline_blur_padding(modifier) == 0
    modifier.parameter_masks = {
        "blur_radius": ParameterMaskBinding("radius", 2, 12),
        "blur_strength": ParameterMaskBinding("strength", 0, 65),
    }
    assert effect_bounds(QRectF(10, 20, 30, 40), [modifier]) == QRectF(-30, -20, 110, 120)
    restored = modifier_from_dict(modifier.to_dict())
    assert restored.to_dict() == modifier.to_dict()
    small = scaled_modifiers([modifier], .25)[0]
    assert (small.thickness, small.blur_radius, small.blur_strength) == (1, 1.75, 0)
    assert small.parameter_masks["blur_radius"].white_value == 3
    assert small.parameter_masks["blur_strength"].white_value == 65
    assert modifier.parameter_masks["blur_radius"].white_value == 12
    preset = preset_from_modifier("Soft outline", OutlineModifier(blur_radius=14, blur_strength=72))
    loaded = apply_modifier_preset(modifier, preset)
    assert (loaded.blur_radius, loaded.blur_strength) == (14, 72)
    assert loaded.parameter_masks == modifier.parameter_masks


def test_legacy_blur_defaults_and_validation():
    old = modifier_from_dict({"type": "outline", "thickness": 9})
    assert (old.blur_radius, old.blur_strength) == (0, 0)
    clamped = OutlineModifier(blur_radius=101, blur_strength=-1)
    clamped.validate()
    assert (clamped.blur_radius, clamped.blur_strength) == (100, 0)
    for name in ("blur_radius", "blur_strength"):
        modifier = OutlineModifier(**{name: float("nan")})
        with pytest.raises(ValueError, match="finite"):
            modifier.validate()
