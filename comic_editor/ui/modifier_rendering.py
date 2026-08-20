"""Pixel processing for nondestructive document modifiers."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
    OutlineModifier,
)


def _qimage_rgba(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
    width, height = converted.width(), converted.height()
    view = np.frombuffer(
        converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes()
    ).reshape(height, converted.bytesPerLine())
    return view[:, :width * 4].reshape(height, width, 4).copy()


def _rgba_qimage(array: np.ndarray) -> QImage:
    array = np.ascontiguousarray(np.clip(array, 0, 255).astype(np.uint8))
    height, width = array.shape[:2]
    return QImage(
        array.data, width, height, width * 4,
        QImage.Format.Format_RGBA8888,
    ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _premultiplied(array: np.ndarray) -> np.ndarray:
    result = array.astype(np.float32) / 255.0
    result[..., :3] *= result[..., 3:4]
    return result


def _straight(premultiplied: np.ndarray) -> np.ndarray:
    alpha = premultiplied[..., 3:4]
    rgb = np.divide(
        premultiplied[..., :3], alpha,
        out=np.zeros_like(premultiplied[..., :3]), where=alpha > 1e-6,
    )
    return np.concatenate((rgb, alpha), axis=2)


def _hsl_effect(
    original: np.ndarray, hue_delta, saturation_delta, lightness_delta,
) -> np.ndarray:
    straight = _straight(original)
    rgb = straight[..., :3]
    maximum = np.max(rgb, axis=2)
    minimum = np.min(rgb, axis=2)
    delta = maximum - minimum
    light = (maximum + minimum) * 0.5
    saturation = np.divide(
        delta, 1.0 - np.abs(2.0 * light - 1.0),
        out=np.zeros_like(delta),
        where=(delta > 1e-7) & (np.abs(2.0 * light - 1.0) < 1.0),
    )
    hue = np.zeros_like(maximum)
    nonzero = delta > 1e-7
    red = nonzero & (maximum == rgb[..., 0])
    green = nonzero & (maximum == rgb[..., 1])
    blue = nonzero & (maximum == rgb[..., 2])
    hue[red] = np.mod(
        (rgb[..., 1][red] - rgb[..., 2][red]) / delta[red], 6.0
    )
    hue[green] = (
        (rgb[..., 2][green] - rgb[..., 0][green]) / delta[green] + 2.0
    )
    hue[blue] = (
        (rgb[..., 0][blue] - rgb[..., 1][blue]) / delta[blue] + 4.0
    )
    hue = np.mod(hue / 6.0 + hue_delta / 360.0, 1.0)
    sat_delta = np.asarray(saturation_delta, dtype=np.float32) / 100.0
    saturation = np.where(
        sat_delta >= 0,
        saturation + (1.0 - saturation) * sat_delta,
        saturation * (1.0 + sat_delta),
    )
    light_delta = np.asarray(lightness_delta, dtype=np.float32) / 100.0
    light = np.where(
        light_delta >= 0,
        light + (1.0 - light) * light_delta,
        light * (1.0 + light_delta),
    )
    saturation = np.clip(saturation, 0.0, 1.0)
    light = np.clip(light, 0.0, 1.0)
    chroma = (1.0 - np.abs(2.0 * light - 1.0)) * saturation
    sector = hue * 6.0
    x = chroma * (1.0 - np.abs(np.mod(sector, 2.0) - 1.0))
    zero = np.zeros_like(chroma)
    choices = (
        np.stack((chroma, x, zero), axis=2),
        np.stack((x, chroma, zero), axis=2),
        np.stack((zero, chroma, x), axis=2),
        np.stack((zero, x, chroma), axis=2),
        np.stack((x, zero, chroma), axis=2),
        np.stack((chroma, zero, x), axis=2),
    )
    sector_index = np.floor(sector).astype(np.int32) % 6
    output = np.zeros_like(rgb)
    for index, choice in enumerate(choices):
        mask = sector_index == index
        output[mask] = choice[mask]
    output += (light - chroma * 0.5)[..., None]
    output *= straight[..., 3:4]
    return np.concatenate((output, original[..., 3:4]), axis=2)


def _blur_effect(original: np.ndarray, strength: float) -> np.ndarray:
    pixels = np.clip(original * 255.0, 0, 255).astype(np.uint8)
    blurred = Image.fromarray(pixels, "RGBA").filter(
        ImageFilter.GaussianBlur(radius=max(0.0, float(strength)))
    )
    return np.asarray(blurred, dtype=np.float32) / 255.0


def _parameter_field(
    modifier: ModifierInstance, attribute: str, fallback: float,
    shape: tuple[int, int], mask_fields: dict[tuple[str, str], np.ndarray],
) -> np.ndarray | float:
    binding = modifier.parameter_masks.get(attribute)
    field = mask_fields.get((modifier.modifier_id, attribute))
    if binding is None or field is None:
        return float(fallback)
    normalized = np.clip(np.asarray(field, dtype=np.float32), 0.0, 1.0)
    if normalized.shape != shape:
        return float(fallback)
    return (
        binding.black_value
        + normalized * (binding.white_value - binding.black_value)
    )


def _variable_blur(original: np.ndarray, strength) -> np.ndarray:
    radii = np.broadcast_to(
        np.asarray(strength, dtype=np.float32), original.shape[:2]
    )
    minimum = max(0.0, float(np.min(radii)))
    maximum = max(0.0, float(np.max(radii)))
    if maximum - minimum < 1e-4:
        return _blur_effect(original, maximum)
    levels = np.linspace(minimum, maximum, 16, dtype=np.float32)
    images = np.stack([
        _blur_effect(original, float(radius)) for radius in levels
    ], axis=0)
    position = np.clip(
        (radii - minimum) / (maximum - minimum) * 15.0, 0.0, 15.0
    )
    lower = np.floor(position).astype(np.int32)
    upper = np.minimum(15, lower + 1)
    blend = (position - lower)[..., None]
    rows, columns = np.indices(radii.shape)
    return (
        images[lower, rows, columns] * (1.0 - blend)
        + images[upper, rows, columns] * blend
    )


def _edt_1d(values: np.ndarray) -> np.ndarray:
    """Exact squared Euclidean distance transform for one scanline."""
    size = len(values)
    locations = np.empty(size, dtype=np.int32)
    boundaries = np.empty(size + 1, dtype=np.float64)
    result = np.empty(size, dtype=np.float32)
    count = 0
    locations[0] = 0
    boundaries[0] = -np.inf
    boundaries[1] = np.inf
    for point in range(1, size):
        while True:
            previous = locations[count]
            crossing = (
                (values[point] + point * point)
                - (values[previous] + previous * previous)
            ) / (2.0 * (point - previous))
            if crossing > boundaries[count] or count == 0:
                break
            count -= 1
        if crossing <= boundaries[count] and count == 0:
            count = -1
        count += 1
        locations[count] = point
        boundaries[count] = crossing
        boundaries[count + 1] = np.inf
    count = 0
    for point in range(size):
        while boundaries[count + 1] < point:
            count += 1
        delta = point - locations[count]
        result[point] = delta * delta + values[locations[count]]
    return result


def _outside_distance(alpha: np.ndarray) -> np.ndarray:
    if not np.any(alpha > 1e-6):
        return np.full(alpha.shape, np.inf, dtype=np.float32)
    large = float(alpha.shape[0] ** 2 + alpha.shape[1] ** 2 + 1)
    field = np.where(alpha > 1e-6, 0.0, large).astype(np.float32)
    horizontal = np.empty_like(field)
    for row in range(field.shape[0]):
        horizontal[row] = _edt_1d(field[row])
    vertical = np.empty_like(field)
    for column in range(field.shape[1]):
        vertical[:, column] = _edt_1d(horizontal[:, column])
    return np.sqrt(np.maximum(0.0, vertical))


def _outline_effect(
    original: np.ndarray, thickness, opacity, color: str,
) -> np.ndarray:
    alpha = original[..., 3]
    distance = _outside_distance(alpha)
    thickness_field = np.broadcast_to(
        np.asarray(thickness, dtype=np.float32), alpha.shape
    )
    opacity_field = np.broadcast_to(
        np.asarray(opacity, dtype=np.float32), alpha.shape
    ) / 100.0
    coverage = np.clip(thickness_field + 0.5 - distance, 0.0, 1.0)
    coverage *= np.clip(1.0 - alpha, 0.0, 1.0)
    coverage *= np.clip(opacity_field, 0.0, 1.0)
    raw = color.lstrip("#")
    if len(raw) == 8:
        color_alpha = int(raw[0:2], 16) / 255.0
        rgb = np.array([
            int(raw[2:4], 16), int(raw[4:6], 16), int(raw[6:8], 16),
        ], dtype=np.float32) / 255.0
    else:
        color_alpha = 1.0
        rgb = np.zeros(3, dtype=np.float32)
    outline_alpha = coverage * color_alpha
    outline = np.zeros_like(original)
    outline[..., :3] = rgb * outline_alpha[..., None]
    outline[..., 3] = outline_alpha
    return original + outline * (1.0 - original[..., 3:4])


def apply_modifier_stack(
    image: QImage, modifiers: list[ModifierInstance],
    world_origin: tuple[float, float],
    mask_fields: dict[tuple[str, str], np.ndarray] | None = None,
) -> QImage:
    if image.isNull() or not modifiers:
        return image
    current = _premultiplied(_qimage_rgba(image))
    height, width = current.shape[:2]
    mask_fields = mask_fields or {}
    for modifier in modifiers:
        modifier.validate()
        amount = _parameter_field(
            modifier, "intensity", modifier.intensity,
            (height, width), mask_fields,
        )
        amount = np.asarray(amount, dtype=np.float32) / 100.0
        if np.max(amount) <= 0.0:
            continue
        if isinstance(modifier, HueSaturationLightnessModifier):
            effect = _hsl_effect(
                current,
                _parameter_field(
                    modifier, "hue", modifier.hue,
                    (height, width), mask_fields,
                ),
                _parameter_field(
                    modifier, "saturation", modifier.saturation,
                    (height, width), mask_fields,
                ),
                _parameter_field(
                    modifier, "lightness", modifier.lightness,
                    (height, width), mask_fields,
                ),
            )
            mask = amount
        elif isinstance(modifier, BlurModifier):
            effect = _variable_blur(current, _parameter_field(
                modifier, "strength", modifier.strength,
                (height, width), mask_fields,
            ))
            if modifier.mode == "focal":
                x = np.arange(width, dtype=np.float32) + world_origin[0] + 0.5
                y = np.arange(height, dtype=np.float32) + world_origin[1] + 0.5
                grid_x, grid_y = np.meshgrid(x, y)
                distance = np.hypot(
                    grid_x - modifier.focal_center[0],
                    grid_y - modifier.focal_center[1],
                )
                inner = modifier.focal_radius * modifier.focal_ramp
                denominator = max(1e-6, modifier.focal_radius - inner)
                mask = np.clip((distance - inner) / denominator, 0.0, 1.0)
                mask = mask[..., None] * amount
            else:
                mask = amount
        elif isinstance(modifier, OutlineModifier):
            effect = _outline_effect(
                current,
                _parameter_field(
                    modifier, "thickness", modifier.thickness,
                    (height, width), mask_fields,
                ),
                _parameter_field(
                    modifier, "opacity", modifier.opacity,
                    (height, width), mask_fields,
                ),
                modifier.color,
            )
            mask = amount
        else:
            continue
        if np.ndim(mask) == 2:
            mask = mask[..., None]
        current = current * (1.0 - mask) + effect * mask
    return _rgba_qimage(_straight(np.clip(current, 0.0, 1.0)) * 255.0)


def apply_opacity_mask(
    image: QImage, mask: np.ndarray, black_value: float,
    white_value: float,
) -> QImage:
    """Apply a spatial opacity map to an already isolated render pass."""
    if image.isNull():
        return image
    current = _premultiplied(_qimage_rgba(image))
    normalized = np.asarray(mask, dtype=np.float32)
    if normalized.shape != current.shape[:2]:
        return image
    opacity = np.clip(
        float(black_value)
        + np.clip(normalized, 0.0, 1.0)
        * (float(white_value) - float(black_value)),
        0.0, 1.0,
    )[..., None]
    current[..., :3] *= opacity
    current[..., 3:4] *= opacity
    return _rgba_qimage(_straight(np.clip(current, 0.0, 1.0)) * 255.0)
