"""Pixel processing for nondestructive document modifiers."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import zlib

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage
from scipy.ndimage import distance_transform_edt

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
    OutlineModifier,
)


def _qimage_premultiplied(image: QImage) -> np.ndarray:
    converted = image.convertToFormat(
        QImage.Format.Format_RGBA8888_Premultiplied
    )
    width, height = converted.width(), converted.height()
    view = np.frombuffer(
        converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes()
    ).reshape(height, converted.bytesPerLine())
    return (
        view[:, :width * 4].reshape(height, width, 4).astype(np.float32)
        / 255.0
    )


def _premultiplied_qimage(array: np.ndarray) -> QImage:
    array = np.ascontiguousarray(
        np.clip(array * 255.0, 0, 255).astype(np.uint8)
    )
    height, width = array.shape[:2]
    return QImage(
        array.data, width, height, width * 4,
        QImage.Format.Format_RGBA8888_Premultiplied,
    ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


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


BLUR_PYRAMID_RADII = np.asarray(
    (0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 63.0, 127.0),
    dtype=np.float32,
)


class BlurPyramidCache:
    """Byte-budgeted LRU of reduced premultiplied RGBA8 blur levels."""

    def __init__(self, budget: int = 64 * 1024 * 1024):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self._values: OrderedDict[tuple, tuple[np.ndarray, ...]] = (
            OrderedDict()
        )
        self.builds = 0

    @staticmethod
    def _pixels(original: np.ndarray) -> np.ndarray:
        return np.ascontiguousarray(
            np.clip(original * 255.0, 0.0, 255.0).astype(np.uint8)
        )

    @staticmethod
    def _key(pixels: np.ndarray) -> tuple:
        digest = hashlib.blake2b(
            memoryview(pixels).cast("B"), digest_size=16
        ).digest()
        return pixels.shape, digest

    @staticmethod
    def _build(pixels: np.ndarray) -> tuple[np.ndarray, ...]:
        levels = [pixels]
        current = Image.fromarray(pixels, "RGBA")
        for _radius in BLUR_PYRAMID_RADII[1:]:
            width = max(1, (current.width + 1) // 2)
            height = max(1, (current.height + 1) // 2)
            current = current.resize(
                (width, height), Image.Resampling.BILINEAR
            )
            levels.append(np.asarray(current, dtype=np.uint8).copy())
        return tuple(levels)

    def pyramid(self, original: np.ndarray) -> tuple[np.ndarray, ...]:
        pixels = self._pixels(original)
        key = self._key(pixels)
        cached = self._values.pop(key, None)
        if cached is not None:
            self._values[key] = cached
            return cached
        result = self._build(pixels)
        self.builds += 1
        size = sum(int(level.nbytes) for level in result)
        if 0 < size <= self.budget:
            self._values[key] = result
            self.bytes += size
            while self._values and self.bytes > self.budget:
                _old_key, old = self._values.popitem(last=False)
                self.bytes -= sum(int(level.nbytes) for level in old)
        return result

    def clear(self) -> None:
        self._values.clear()
        self.bytes = 0


def _upscaled_blur_image(
    levels: tuple[np.ndarray, ...], index: int,
    shape: tuple[int, int],
) -> Image.Image:
    height, width = shape
    image = Image.fromarray(levels[index], "RGBA")
    if image.size != (width, height):
        image = image.resize((width, height), Image.Resampling.BILINEAR)
    return image


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


def _variable_blur(
    original: np.ndarray, strength,
    cache: BlurPyramidCache | None = None,
) -> np.ndarray:
    radii = np.clip(
        np.broadcast_to(
            np.asarray(strength, dtype=np.float32), original.shape[:2]
        ),
        0.0, 100.0,
    )
    if float(np.max(radii)) <= 1e-6:
        return original.copy()
    cache = cache or BlurPyramidCache(0)
    levels = cache.pyramid(original)
    lower = np.searchsorted(
        BLUR_PYRAMID_RADII, radii, side="right"
    ) - 1
    lower = np.clip(lower, 0, len(BLUR_PYRAMID_RADII) - 2)
    scalar = np.ndim(strength) == 0
    if scalar:
        index = int(lower.flat[0])
        low_radius = float(BLUR_PYRAMID_RADII[index])
        high_radius = float(BLUR_PYRAMID_RADII[index + 1])
        blend = (float(radii.flat[0]) - low_radius) / max(
            1e-6, high_radius - low_radius
        )
        low_image = _upscaled_blur_image(
            levels, index, original.shape[:2]
        )
        if blend <= 1e-6:
            return np.asarray(low_image, dtype=np.float32) / 255.0
        high_image = _upscaled_blur_image(
            levels, index + 1, original.shape[:2]
        )
        return np.asarray(
            Image.blend(low_image, high_image, blend), dtype=np.float32
        ) / 255.0

    low_radius = BLUR_PYRAMID_RADII[lower]
    high_radius = BLUR_PYRAMID_RADII[lower + 1]
    blend = np.clip(
        (radii - low_radius) / np.maximum(1e-6, high_radius - low_radius),
        0.0, 1.0,
    )
    height, width = original.shape[:2]
    result = Image.new("RGBA", (width, height))
    prior_index = -1
    prior_high: Image.Image | None = None
    for raw_index in np.unique(lower):
        index = int(raw_index)
        selected = lower == index
        if prior_index + 1 == index and prior_high is not None:
            low_image = prior_high
        else:
            low_image = _upscaled_blur_image(
                levels, index, original.shape[:2]
            )
        high_image = _upscaled_blur_image(
            levels, index + 1, original.shape[:2]
        )
        alpha = np.zeros(radii.shape, dtype=np.uint8)
        alpha[selected] = np.rint(blend[selected] * 255.0).astype(np.uint8)
        mixed = Image.composite(
            high_image, low_image, Image.fromarray(alpha, "L")
        )
        selection = np.zeros(radii.shape, dtype=np.uint8)
        selection[selected] = 255
        result.paste(mixed, (0, 0), Image.fromarray(selection, "L"))
        prior_index, prior_high = index, high_image
    return np.asarray(result, dtype=np.float32) / 255.0


class OutlineDistanceCache:
    """Byte-budgeted cache of exact source-alpha distance fields."""

    def __init__(self, budget: int = 64 * 1024 * 1024):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self._values: OrderedDict[tuple, np.ndarray] = OrderedDict()
        self.computations = 0

    @staticmethod
    def _key(alpha: np.ndarray) -> tuple:
        contiguous = np.ascontiguousarray(alpha, dtype=np.float32)
        return (
            contiguous.shape,
            zlib.crc32(memoryview(contiguous).cast("B")),
        )

    def distance(self, alpha: np.ndarray) -> np.ndarray:
        key = self._key(alpha)
        cached = self._values.pop(key, None)
        if cached is not None:
            self._values[key] = cached
            return cached
        result = _outside_distance(alpha)
        self.computations += 1
        size = int(result.nbytes)
        if 0 < size <= self.budget:
            self._values[key] = result
            self.bytes += size
            while self._values and self.bytes > self.budget:
                _old_key, old = self._values.popitem(last=False)
                self.bytes -= int(old.nbytes)
        return result

    def clear(self) -> None:
        self._values.clear()
        self.bytes = 0


def _outside_distance(alpha: np.ndarray) -> np.ndarray:
    if not np.any(alpha > 1e-6):
        return np.full(alpha.shape, np.inf, dtype=np.float32)
    # distance_transform_edt measures nonzero pixels to the nearest zero.
    # Transparent pixels are therefore the foreground and opaque pixels the
    # zero-valued targets.
    return distance_transform_edt(
        np.asarray(alpha <= 1e-6, dtype=np.uint8)
    ).astype(np.float32, copy=False)


def _outline_effect(
    original: np.ndarray, thickness, opacity, color: str,
    distance_cache: OutlineDistanceCache | None = None,
) -> np.ndarray:
    alpha = original[..., 3]
    distance = (
        distance_cache.distance(alpha)
        if distance_cache is not None else _outside_distance(alpha)
    )
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
    *, outline_distance_cache: OutlineDistanceCache | None = None,
    blur_pyramid_cache: BlurPyramidCache | None = None,
) -> QImage:
    if image.isNull() or not modifiers:
        return image
    current = _qimage_premultiplied(image)
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
            effect = _variable_blur(
                current,
                _parameter_field(
                    modifier, "strength", modifier.strength,
                    (height, width), mask_fields,
                ),
                blur_pyramid_cache,
            )
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
                outline_distance_cache,
            )
            mask = amount
        else:
            continue
        if np.ndim(mask) == 2:
            mask = mask[..., None]
        current = current * (1.0 - mask) + effect * mask
    return _premultiplied_qimage(np.clip(current, 0.0, 1.0))


def apply_opacity_mask(
    image: QImage, mask: np.ndarray, black_value: float,
    white_value: float,
) -> QImage:
    """Apply a spatial opacity map to an already isolated render pass."""
    if image.isNull():
        return image
    current = _qimage_premultiplied(image)
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
    return _premultiplied_qimage(np.clip(current, 0.0, 1.0))
