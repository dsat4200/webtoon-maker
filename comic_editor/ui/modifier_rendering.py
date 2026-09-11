"""Pixel processing for nondestructive document modifiers."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
import sys

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage, QPainter, QTransform
from PySide6.QtCore import Qt
from scipy.ndimage import distance_transform_edt

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
    OutlineModifier, MirrorModifier, RadialBlurModifier, PosterizeModifier, PosterizeValueModifier,
    HalftoneModifier, PixelateModifier,
)
from comic_editor.core.effect_geometry import reflection_transform


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
    hue_only = (np.ndim(saturation_delta) == 0 and float(saturation_delta) == 0.0
                and np.ndim(lightness_delta) == 0 and float(lightness_delta) == 0.0)
    if hue_only:
        # A hue drag preserves chroma and the RGB minimum exactly; there is
        # no need to derive and reconstruct unchanged saturation/lightness.
        chroma, base = delta, minimum
    else:
        light = (maximum + minimum) * 0.5
        saturation = np.divide(
            delta, 1.0 - np.abs(2.0 * light - 1.0),
            out=np.zeros_like(delta),
            where=(delta > 1e-7) & (np.abs(2.0 * light - 1.0) < 1.0),
        )
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
        base = light - chroma * 0.5
    sector = hue * 6.0
    output = np.empty_like(rgb)
    # Each channel is a shifted, clipped triangle wave around the hue wheel.
    # This is the same six-sector HSL interpolation without allocating six
    # complete RGB candidate images and copying their boolean selections.
    for channel, phase in enumerate((0.0, 4.0, 2.0)):
        value = np.mod(sector + phase, 6.0)
        value -= 3.0
        np.abs(value, out=value)
        value -= 1.0
        np.clip(value, 0.0, 1.0, out=value)
        value *= chroma
        value += base
        value *= straight[..., 3]
        output[..., channel] = value
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
    def _build(pixels: np.ndarray, algorithm="normal") -> tuple[np.ndarray, ...]:
        levels = [pixels]
        current = Image.fromarray(pixels, "RGBA" if algorithm == "legacy" else "RGBa")
        for _radius in BLUR_PYRAMID_RADII[1:]:
            width = max(1, (current.width + 1) // 2)
            height = max(1, (current.height + 1) // 2)
            current = current.resize(
                (width, height), Image.Resampling.BILINEAR
            )
            levels.append(np.asarray(current, dtype=np.uint8).copy())
        return tuple(levels)

    def pyramid(self, original: np.ndarray, algorithm="normal") -> tuple[np.ndarray, ...]:
        pixels = self._pixels(original)
        key = (algorithm, *self._key(pixels))
        cached = self._values.pop(key, None)
        if cached is not None:
            self._values[key] = cached
            return cached
        result = self._build(pixels, algorithm)
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
    algorithm="normal",
) -> Image.Image:
    height, width = shape
    image = Image.fromarray(levels[index], "RGBA" if algorithm == "legacy" else "RGBa")
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
    algorithm="normal",
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
    levels = cache.pyramid(original, algorithm)
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
            levels, index, original.shape[:2], algorithm
        )
        if blend <= 1e-6:
            return np.asarray(low_image, dtype=np.float32) / 255.0
        high_image = _upscaled_blur_image(
            levels, index + 1, original.shape[:2], algorithm
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
    result = Image.new("RGBA" if algorithm == "legacy" else "RGBa", (width, height))
    prior_index = -1
    prior_high: Image.Image | None = None
    for raw_index in np.unique(lower):
        index = int(raw_index)
        selected = lower == index
        if prior_index + 1 == index and prior_high is not None:
            low_image = prior_high
        else:
            low_image = _upscaled_blur_image(
                levels, index, original.shape[:2], algorithm
            )
        high_image = _upscaled_blur_image(
            levels, index + 1, original.shape[:2], algorithm
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
    """Byte-budgeted cache of exact silhouette distances and occupied bounds.

    Distances depend on occupied pixels, not their alpha values or colors.
    Packing that silhouette makes warmed lookups inexpensive and also lets
    changes to text color/opacity reuse the same exact distance transform.
    """

    def __init__(self, budget: int = 64 * 1024 * 1024):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self._values: OrderedDict[
            tuple, tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int, int, int]]
        ] = OrderedDict()
        self.computations = 0

    @staticmethod
    def _key(alpha: np.ndarray) -> tuple:
        packed = np.packbits(alpha > 1e-6)
        return (
            alpha.shape,
            hashlib.blake2b(memoryview(packed), digest_size=16).digest(),
        )

    def distance(self, alpha: np.ndarray) -> np.ndarray:
        return self.field(alpha)[0]

    def field(
        self, alpha: np.ndarray, margin: int | None = None,
    ) -> tuple[np.ndarray, tuple[int, int, int, int], tuple[int, int, int, int]]:
        """Return distances, occupied bounds, and the distance field's bounds.

        A bounded margin avoids running the transform across empty canvas.
        Every occupied pixel remains inside the region, so distances within
        that region are identical to a full-image transform.
        """
        key = (margin, *self._key(alpha))
        cached = self._values.pop(key, None)
        if cached is not None:
            self._values[key] = cached
            return cached
        occupied = alpha > 1e-6
        rows = np.flatnonzero(np.any(occupied, axis=1))
        columns = np.flatnonzero(np.any(occupied, axis=0))
        bounds = (
            (int(columns[0]), int(rows[0]), int(columns[-1]) + 1, int(rows[-1]) + 1)
            if rows.size else (0, 0, 0, 0)
        )
        if margin is None:
            extent = (0, 0, alpha.shape[1], alpha.shape[0])
        elif rows.size:
            extent = (max(0, bounds[0] - margin), max(0, bounds[1] - margin),
                      min(alpha.shape[1], bounds[2] + margin),
                      min(alpha.shape[0], bounds[3] + margin))
        else:
            extent = (0, 0, 0, 0)
        left, top, right, bottom = extent
        result = (_outside_distance(alpha[top:bottom, left:right]), bounds, extent)
        self.computations += 1
        size = int(result[0].nbytes)
        if 0 < size <= self.budget:
            self._values[key] = result
            self.bytes += size
            while self._values and self.bytes > self.budget:
                _old_key, old = self._values.popitem(last=False)
                self.bytes -= int(old[0].nbytes)
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
    amount=1.0,
) -> np.ndarray:
    alpha = original[..., 3]
    distance = (
        distance_cache.distance(alpha)
        if distance_cache is not None else _outside_distance(alpha)
    )
    rgba = _outline_color(color)
    coverage = _outline_coverage(alpha, distance, thickness, opacity, rgba[3], amount)
    result = original.copy()
    for channel in range(3):
        result[..., channel] += coverage * rgba[channel]
    result[..., 3] += coverage
    return result


def _outline_color(color: str) -> tuple[float, float, float, float]:
    raw = color.lstrip("#")
    if len(raw) == 8:
        return tuple(int(raw[index:index + 2], 16) / 255.0 for index in (2, 4, 6, 0))
    return 0.0, 0.0, 0.0, 1.0


def _outline_coverage(alpha, distance, thickness, opacity, color_alpha, amount=1.0):
    """One-channel premultiplied contribution, including intensity blending."""
    coverage = np.asarray(thickness, dtype=np.float32) + 0.5 - distance
    np.clip(coverage, 0.0, 1.0, out=coverage)
    transparent = np.clip(1.0 - alpha, 0.0, 1.0)
    # Preserve the existing outside-only coverage and source-over treatment
    # of partially transparent antialiased edges.
    coverage *= transparent
    coverage *= np.clip(np.asarray(opacity, dtype=np.float32) / 100.0, 0.0, 1.0)
    coverage *= color_alpha
    coverage *= transparent
    coverage *= amount
    return coverage


def _outline_qimage(
    image: QImage, modifier: OutlineModifier,
    mask_fields: dict[tuple[str, str], np.ndarray],
    distance_cache: OutlineDistanceCache | None,
) -> QImage:
    """Apply a single outline without converting the full RGBA image to floats.

    Only the occupied rectangle and its outline fringe need arithmetic. The
    remaining pixels are copied directly, so a small caption on a large layer
    does not incur several full-canvas float buffers on every slider movement.
    """
    modifier.validate()
    height, width = image.height(), image.width()
    shape = (height, width)
    fields = {
        name: _parameter_field(modifier, name, getattr(modifier, name), shape, mask_fields)
        for name in ("thickness", "opacity", "intensity")
    }
    rgba = _outline_color(modifier.color)
    if (rgba[3] <= 0.0 or np.max(fields["opacity"]) <= 0.0
            or np.max(fields["intensity"]) <= 0.0):
        return image
    source = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    pixels = np.frombuffer(source.constBits(), dtype=np.uint8).reshape(
        height, source.bytesPerLine()
    )[:, :width * 4].reshape(height, width, 4)
    channels = (2, 1, 0, 3) if sys.byteorder == "little" else (1, 2, 3, 0)
    alpha = pixels[..., channels[3]]
    padding = max(0, math.ceil(float(np.max(fields["thickness"])) + 0.5))
    # Quantized padding keeps the expensive distance field reusable throughout
    # the full legal 0..25px thickness range, including thickness masks.
    cache_margin = max(32, math.ceil(padding / 32) * 32)
    distance, bounds, extent = (distance_cache or OutlineDistanceCache(0)).field(
        alpha, margin=cache_margin
    )
    if bounds[0] == bounds[2]:
        return image
    left, top, right, bottom = bounds
    left, top, right, bottom = (max(0, left - padding), max(0, top - padding),
                               min(width, right + padding), min(height, bottom + padding))
    region = np.s_[top:bottom, left:right]
    distance = distance[top - extent[1]:bottom - extent[1], left - extent[0]:right - extent[0]]
    for name, field in fields.items():
        if np.ndim(field):
            fields[name] = field[region]
    coverage = _outline_coverage(
        alpha[region].astype(np.float32) / 255.0, distance,
        fields["thickness"], fields["opacity"], rgba[3],
        np.asarray(fields["intensity"], dtype=np.float32) / 100.0,
    )
    coverage *= 255.0
    result = source.copy()
    output = np.frombuffer(result.bits(), dtype=np.uint8).reshape(
        height, result.bytesPerLine()
    )[:, :width * 4].reshape(height, width, 4)[region]
    for channel, coefficient in zip(channels, (*rgba[:3], 1.0)):
        if coefficient == 0.0:
            continue
        values = output[..., channel] + coverage * coefficient
        np.minimum(values, 255.0, out=values)
        output[..., channel] = values.astype(np.uint8)
    return result


def _outline_stack_qimage(
    image: QImage, modifiers: list[OutlineModifier],
    mask_fields: dict[tuple[str, str], np.ndarray],
    distance_cache: OutlineDistanceCache | None,
) -> QImage:
    """Keep float precision between outlines, limited to their combined bounds."""
    shape = (image.height(), image.width())
    parameters = []
    padding = 0
    for modifier in modifiers:
        modifier.validate()
        fields = {
            name: _parameter_field(modifier, name, getattr(modifier, name), shape, mask_fields)
            for name in ("thickness", "opacity", "intensity")
        }
        parameters.append(fields)
        fringe = max(0, math.ceil(float(np.max(fields["thickness"])) + 0.5))
        padding += max(32, math.ceil(fringe / 32) * 32)
    source = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    pixels = np.frombuffer(source.constBits(), dtype=np.uint8).reshape(
        shape[0], source.bytesPerLine()
    )[:, :shape[1] * 4].reshape(*shape, 4)
    alpha = pixels[..., 3 if sys.byteorder == "little" else 0]
    rows = np.flatnonzero(np.any(alpha, axis=1))
    if not rows.size:
        return image
    columns = np.flatnonzero(np.any(alpha, axis=0))
    left, top = max(0, int(columns[0]) - padding), max(0, int(rows[0]) - padding)
    right = min(shape[1], int(columns[-1]) + 1 + padding)
    bottom = min(shape[0], int(rows[-1]) + 1 + padding)
    region = np.s_[top:bottom, left:right]
    current = _qimage_premultiplied(source.copy(left, top, right - left, bottom - top))
    for modifier, fields in zip(modifiers, parameters):
        for name, field in fields.items():
            if np.ndim(field):
                fields[name] = field[region]
        amount = np.asarray(fields["intensity"], dtype=np.float32) / 100.0
        if np.max(amount) <= 0.0:
            continue
        current = _outline_effect(current, fields["thickness"], fields["opacity"],
                                  modifier.color, distance_cache, amount)
    result = source.copy()
    painter = QPainter(result)
    painter.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
    painter.drawImage(left, top, _premultiplied_qimage(current))
    painter.end()
    return result


def apply_pattern_modifier(image, modifier, mask_fields=None, renderer=None,
                           color_source: QImage | None = None, *,
                           allow_cpu_fallback=True, cancelled=None):
    """Render a pattern once, blending intensity in premultiplied space."""
    if image.isNull() or modifier.muted:
        return image
    modifier.validate()
    amount = _parameter_field(modifier, "intensity", modifier.intensity,
                              (image.height(), image.width()), mask_fields or {})
    amount = np.asarray(amount, dtype=np.float32) / 100.0
    if np.max(amount) <= 0.0:
        return image
    if renderer is not None:
        result = renderer.render(image, modifier,
                                 intensity_mask=amount if amount.ndim else None,
                                 color_source=color_source)
        if result is not None:
            return result
    if not allow_cpu_fallback:
        return None
    from comic_editor.ui.pattern_rendering import apply_pattern_effect
    result = apply_pattern_effect(image, modifier, color_source=color_source,
                                  cancelled=cancelled)
    if amount.ndim == 0 and float(amount) >= 1.0:
        return result
    if amount.ndim == 2:
        amount = amount[..., None]
    return _premultiplied_qimage(_qimage_premultiplied(image) * (1.0 - amount)
                                + _qimage_premultiplied(result) * amount)


def apply_modifier_stack(
    image: QImage, modifiers: list[ModifierInstance],
    world_origin: tuple[float, float],
    mask_fields: dict[tuple[str, str], np.ndarray] | None = None,
    *, outline_distance_cache: OutlineDistanceCache | None = None,
    blur_pyramid_cache: BlurPyramidCache | None = None,
    world_to_image: QTransform | None = None,
    nearest: bool = False,
) -> QImage:
    active_modifiers = [modifier for modifier in modifiers if not modifier.muted]
    if image.isNull() or not active_modifiers:
        return image
    if len(active_modifiers) == 1 and isinstance(active_modifiers[0], OutlineModifier):
        return _outline_qimage(image, active_modifiers[0], mask_fields or {}, outline_distance_cache)
    if all(isinstance(modifier, OutlineModifier) for modifier in active_modifiers):
        return _outline_stack_qimage(image, active_modifiers, mask_fields or {}, outline_distance_cache)
    current = _qimage_premultiplied(image)
    height, width = current.shape[:2]
    mask_fields = mask_fields or {}
    for modifier in active_modifiers:
        modifier.validate()
        amount = _parameter_field(
            modifier, "intensity", modifier.intensity,
            (height, width), mask_fields,
        )
        amount = np.asarray(amount, dtype=np.float32) / 100.0
        if np.max(amount) <= 0.0:
            continue
        if isinstance(modifier, (HalftoneModifier, PixelateModifier)):
            from comic_editor.ui.pattern_rendering import apply_pattern_effect
            effect = _qimage_premultiplied(apply_pattern_effect(
                _premultiplied_qimage(current), modifier))
            mask = amount
        elif isinstance(modifier, HueSaturationLightnessModifier):
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
        elif isinstance(modifier, PosterizeModifier):
            from comic_editor.core.color_smoothing import simplify_colors
            from comic_editor.core.posterize import rgb_hues, range_indices, rgb_values, value_range_indices, grayscale_source
            from PySide6.QtGui import QColor
            straight = _straight(current)
            if isinstance(modifier, PosterizeValueModifier):
                straight = grayscale_source(straight)
            straight = simplify_colors(straight, modifier)
            indices = (value_range_indices(rgb_values(straight[..., :3]), modifier.ranges)
                       if isinstance(modifier, PosterizeValueModifier)
                       else range_indices(rgb_hues(straight[..., :3]), modifier.ranges))
            palette = np.array([QColor(item.color).getRgbF() for item in modifier.ranges], dtype=np.float32)
            effect = palette[indices].copy()
            effect[..., 3:4] *= current[..., 3:4]
            effect[..., :3] *= effect[..., 3:4]
            mask = amount
        elif isinstance(modifier, BlurModifier):
            effect = _variable_blur(
                current,
                _parameter_field(
                    modifier, "strength", modifier.strength,
                    (height, width), mask_fields,
                ),
                blur_pyramid_cache,
                modifier.algorithm,
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
        elif isinstance(modifier, RadialBlurModifier):
            from comic_editor.ui.radial_blur import radial_blur
            effect = radial_blur(current, modifier.center,
                _parameter_field(modifier, "angle", modifier.angle, (height, width), mask_fields),
                world_to_image or QTransform.fromTranslate(-world_origin[0], -world_origin[1]))
            mask = amount
        elif isinstance(modifier, MirrorModifier):
            mapping = world_to_image or QTransform.fromTranslate(-world_origin[0], -world_origin[1])
            inverse, valid = mapping.inverted()
            if not valid:
                continue
            source = _premultiplied_qimage(current)
            reflected = QImage(source.size(), source.format())
            reflected.fill(Qt.transparent)
            painter = QPainter(reflected)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
            painter.setRenderHint(QPainter.Antialiasing, not nearest)
            painter.setTransform(inverse * reflection_transform(modifier) * mapping)
            painter.drawImage(0, 0, source)
            painter.end()
            reflection = _qimage_premultiplied(reflected)
            effect = current + reflection * (1.0 - current[..., 3:4])
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
                amount,
            )
            current = effect
            continue
        else:
            continue
        if np.ndim(mask) == 2:
            mask = mask[..., None]
        if np.ndim(mask) == 0 and float(mask) == 1.0:
            current = effect
        else:
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
