"""Pixel processing for nondestructive document modifiers."""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
import sys
from threading import RLock

import numpy as np
from PIL import Image
from PySide6.QtGui import QImage, QPainter, QTransform
from PySide6.QtCore import Qt, QRectF
from scipy.ndimage import distance_transform_edt, gaussian_filter

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, BrightnessContrastModifier, CurvesModifier, ModifierInstance,
    OutlineModifier, MirrorModifier, RadialBlurModifier, PosterizeModifier, PosterizeValueModifier,
    HalftoneModifier, PixelateModifier, DistortModifier,
    TextureModifier, SolidColorOverlayModifier,
)
from comic_editor.core.effect_geometry import reflection_transform
from comic_editor.core.curves import apply_curves, curves_is_neutral
from comic_editor.core.models import KuwaharaModifier
from comic_editor.core.models import DitheringModifier, SharpnessModifier
from comic_editor.render.pixels import current_contract, premultiplied_pixels, working_image
from comic_editor.core.pixel_arrays import normalized_bytes, truncated_bytes


def modifier_render_settings(modifier):
    """Serializable pixel dependencies, excluding modifier-card presentation."""
    settings = modifier.to_dict()
    settings.pop("name", None)
    settings.pop("expanded", None)
    if isinstance(modifier, TextureModifier):
        from comic_editor.core.texture_library import texture_digest
        settings["texture_data"] = texture_digest(modifier.texture_data)
        settings.pop("texture_name", None)
        settings.pop("texture_category", None)
        settings.pop("transform_mode", None)
    if isinstance(modifier, CurvesModifier):
        # Selecting a graph channel does not enable or disable its curve.
        settings.pop("channel", None)
    return settings


def _qimage_premultiplied(image: QImage) -> np.ndarray:
    if current_contract().floating:
        return premultiplied_pixels(image)
    converted = image.convertToFormat(
        QImage.Format.Format_RGBA8888_Premultiplied
    )
    width, height = converted.width(), converted.height()
    view = np.frombuffer(
        converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes()
    ).reshape(height, converted.bytesPerLine())
    return normalized_bytes(view[:, :width * 4].reshape(height, width, 4))


def _premultiplied_qimage(array: np.ndarray) -> QImage:
    if current_contract().floating:
        return working_image(array)
    array = truncated_bytes(array)
    height, width = array.shape[:2]
    return QImage(
        array.data, width, height, width * 4,
        QImage.Format.Format_RGBA8888_Premultiplied,
    ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _straight(premultiplied: np.ndarray) -> np.ndarray:
    alpha = premultiplied[..., 3:4]
    # Divide contiguous RGBA together for the vectorized path, then restore
    # coverage. A strided RGB output is substantially slower on large layers.
    result = np.zeros_like(premultiplied)
    np.divide(
        premultiplied, alpha,
        out=result, where=alpha > 1e-6,
    )
    result[..., 3:4] = alpha
    return result


def _brightness_contrast_effect(original, brightness, contrast):
    """Adjust straight RGB around midgray, preserving premultiplied alpha."""
    straight = _straight(original)
    rgb = straight[..., :3]
    gain = 1.0 + np.asarray(contrast, dtype=np.float32) / 100.0
    offset = np.asarray(brightness, dtype=np.float32) / 100.0
    if gain.ndim:
        gain = gain[..., None]
    if offset.ndim:
        offset = offset[..., None]
    rgb -= .5
    rgb *= gain
    rgb += .5 + offset
    np.clip(rgb, 0.0, 1.0, out=rgb)
    rgb *= original[..., 3:4]
    return straight


def _hsl_effect(
    original: np.ndarray, hue_delta, saturation_delta, lightness_delta,
) -> np.ndarray:
    # Large painted/raster layers often have transparent padding. The dense
    # kernel only needs visible colors; zero-alpha pixels always output black.
    if (original.shape[0] * original.shape[1] >= 65536
            and all(np.ndim(value) == 0 for value in
                    (hue_delta, saturation_delta, lightness_delta))):
        visible = original[..., 3] != 0
        visible_count = np.count_nonzero(visible)
        if visible_count * 5 < visible.size * 4:
            result = original.copy()
            result[~visible, :3] = 0
            if visible_count:
                selected = original[visible].reshape(-1, 1, 4)
                result[visible] = _hsl_effect_dense(
                    selected, hue_delta, saturation_delta, lightness_delta,
                ).reshape(-1, 4)
            return result
    return _hsl_effect_dense(original, hue_delta, saturation_delta, lightness_delta)


def _hsl_effect_dense(
    original: np.ndarray, hue_delta, saturation_delta, lightness_delta,
) -> np.ndarray:
    straight = _straight(original)
    rgb = straight[..., :3]
    maximum = np.maximum(rgb[..., 0], rgb[..., 1])
    np.maximum(maximum, rgb[..., 2], out=maximum)
    minimum = np.minimum(rgb[..., 0], rgb[..., 1])
    np.minimum(minimum, rgb[..., 2], out=minimum)
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
    output = np.empty_like(original)
    output[..., 3:4] = original[..., 3:4]
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
    return output


BLUR_PYRAMID_RADII = np.asarray(
    (0.0, 1.0, 3.0, 7.0, 15.0, 31.0, 63.0, 127.0),
    dtype=np.float32,
)


class BlurPyramidCache:
    """Byte-budgeted LRU of reduced, policy-typed premultiplied blur levels."""

    def __init__(self, budget: int = 64 * 1024 * 1024):
        self.budget = max(0, int(budget))
        self.bytes = 0
        self._values: OrderedDict[tuple, tuple[np.ndarray, ...]] = (
            OrderedDict()
        )
        self.builds = 0
        self.extensions = 0
        self._lock = RLock()

    @staticmethod
    def _pixels(original: np.ndarray) -> np.ndarray:
        if current_contract().floating:
            return np.array(original, dtype=np.float32, order='C', copy=True)
        return truncated_bytes(original)

    @staticmethod
    def _key(pixels: np.ndarray) -> tuple:
        digest = hashlib.blake2b(
            memoryview(pixels).cast("B"), digest_size=16
        ).digest()
        return pixels.shape, digest

    @staticmethod
    def _build(pixels: np.ndarray, algorithm="normal") -> tuple[np.ndarray, ...]:
        return BlurPyramidCache._extend(pixels, algorithm, (), len(BLUR_PYRAMID_RADII)-1)

    @staticmethod
    def _extend(pixels, algorithm, previous, maximum):
        levels = list(previous) if previous else [pixels]
        if np.issubdtype(pixels.dtype, np.floating):
            from comic_editor.render.float_resize import resize_rgba
            for _index in range(len(levels), maximum + 1):
                height, width = levels[-1].shape[:2]
                levels.append(resize_rgba(levels[-1], (max(1, (width+1)//2), max(1, (height+1)//2))))
            return tuple(levels)
        current = Image.fromarray(levels[-1], "RGBA" if algorithm == "legacy" else "RGBa")
        for _index in range(len(levels), maximum + 1):
            width = max(1, (current.width + 1) // 2)
            height = max(1, (current.height + 1) // 2)
            current = current.resize(
                (width, height), Image.Resampling.BILINEAR
            )
            levels.append(np.asarray(current, dtype=np.uint8).copy())
        return tuple(levels)

    def pyramid(self, original: np.ndarray, algorithm="normal", *, max_level=None) -> tuple[np.ndarray, ...]:
        maximum = (len(BLUR_PYRAMID_RADII)-1 if max_level is None
                   else max(0, min(len(BLUR_PYRAMID_RADII)-1, int(max_level))))
        pixels = self._pixels(original)
        key = (algorithm, pixels.dtype.str, *self._key(pixels))
        with self._lock:
            cached = self._values.pop(key, None)
            if cached is not None:
                self._values[key] = cached
                if len(cached) > maximum:
                    return cached
        extending = cached is not None
        result = (self._build(pixels, algorithm) if max_level is None and cached is None
                  else self._extend(pixels, algorithm, cached or (), maximum))
        for level in result:
            level.setflags(write=False)
        size = sum(int(level.nbytes) for level in result)
        with self._lock:
            if extending:
                self.extensions += 1
            else:
                self.builds += 1
            cached = self._values.pop(key, None)
            if cached is not None:
                if len(cached) >= len(result) or size > self.budget:
                    self._values[key] = cached
                    return cached if len(cached) >= len(result) else result
                self.bytes -= sum(int(level.nbytes) for level in cached)
            if 0 < size <= self.budget:
                self._values[key] = result
                self.bytes += size
                while self._values and self.bytes > self.budget:
                    _old_key, old = self._values.popitem(last=False)
                    self.bytes -= sum(int(level.nbytes) for level in old)
        return result

    def clear(self) -> None:
        with self._lock:
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
    values = np.asarray(field, dtype=np.float32)
    if values.shape != shape:
        return float(fallback)
    normalized = np.empty_like(values)
    np.clip(values, 0.0, 1.0, out=normalized)
    np.multiply(normalized, binding.white_value - binding.black_value,
                out=normalized)
    np.add(normalized, binding.black_value, out=normalized)
    return normalized


def _variable_blur(
    original: np.ndarray, strength,
    cache: BlurPyramidCache | None = None,
    algorithm="normal",
) -> np.ndarray:
    scalar = np.ndim(strength) == 0
    values = np.asarray(strength, dtype=np.float32)
    radii = np.clip(values if scalar else np.broadcast_to(values, original.shape[:2]), 0.0, 100.0)
    if float(np.max(radii)) <= 1e-6:
        return original.copy()
    if current_contract().floating:
        return _variable_float_blur(original, radii, cache, algorithm)
    if scalar:
        from comic_editor.ui.gpu_effects import scalar_blur
        accelerated = scalar_blur(original, strength, algorithm)
        if accelerated is not None:
            return accelerated
    cache = cache or BlurPyramidCache(0)
    lower = np.searchsorted(
        BLUR_PYRAMID_RADII, radii, side="right"
    ) - 1
    lower = np.clip(lower, 0, len(BLUR_PYRAMID_RADII) - 2)
    levels = cache.pyramid(original, algorithm, max_level=int(np.max(lower)) + 1)
    if scalar:
        index = int(lower)
        low_radius = float(BLUR_PYRAMID_RADII[index])
        high_radius = float(BLUR_PYRAMID_RADII[index + 1])
        blend = (float(radii) - low_radius) / max(
            1e-6, high_radius - low_radius
        )
        low_image = _upscaled_blur_image(
            levels, index, original.shape[:2], algorithm
        )
        if blend <= 1e-6:
            return normalized_bytes(low_image)
        high_image = _upscaled_blur_image(
            levels, index + 1, original.shape[:2], algorithm
        )
        return normalized_bytes(Image.blend(low_image, high_image, blend))

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
    return normalized_bytes(result)


def _variable_float_blur(original, radii, cache, algorithm):
    """Version-two pyramid interpolation keeps premultiplied float samples.

    The legacy straight/premultiplied resize distinction only exists because
    of intermediate byte rounding. Both float modes resample premultiplied
    channels and coverage together, preserving HDR and negative RGB values.
    """
    from comic_editor.render.float_resize import resize_rgba
    lower = np.clip(np.searchsorted(BLUR_PYRAMID_RADII, radii, side='right') - 1,
                    0, len(BLUR_PYRAMID_RADII)-2)
    levels = (cache or BlurPyramidCache(0)).pyramid(original, algorithm,
                                                   max_level=int(np.max(lower))+1)
    low_radius, high_radius = BLUR_PYRAMID_RADII[lower], BLUR_PYRAMID_RADII[lower+1]
    blend = np.clip((radii-low_radius)/(high_radius-low_radius), 0., 1.)
    size = original.shape[1], original.shape[0]
    if np.ndim(radii) == 0:
        low = resize_rgba(levels[int(lower)], size)
        if float(blend) <= 1e-6:
            return low
        high = resize_rgba(levels[int(lower)+1], size)
        return low + (high-low)*blend
    result = np.empty_like(original)
    previous_index, previous_high = -1, None
    for raw_index in np.unique(lower):
        index = int(raw_index)
        low = previous_high if previous_index+1 == index and previous_high is not None else resize_rgba(levels[index], size)
        high = resize_rgba(levels[index+1], size)
        selected = lower == index
        values = low[selected]
        result[selected] = values + (high[selected]-values)*blend[selected, None]
        previous_index, previous_high = index, high
    return result


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
        self._lock = RLock()

    @staticmethod
    def _key(alpha: np.ndarray, occupied=None) -> tuple:
        packed = np.packbits(alpha > 1e-6 if occupied is None else occupied)
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
        Distances that can contribute to an outline within that margin are
        identical to a full-image transform; large fields omit farther values.
        """
        occupied = alpha > 1e-6
        key = (margin, *self._key(alpha, occupied))
        with self._lock:
            cached = self._values.pop(key, None)
            if cached is not None:
                self._values[key] = cached
                return cached
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
        region = alpha[top:bottom, left:right]
        distance = (_outside_distance_bounded(region, margin)
                    if margin is not None and region.size > 1_000_000 and margin <= 256
                    else _outside_distance(region))
        result = (distance, bounds, extent)
        distance.setflags(write=False)
        size = int(result[0].nbytes)
        with self._lock:
            self.computations += 1
            cached = self._values.pop(key, None)
            if cached is not None:
                self._values[key] = cached
                return cached
            if 0 < size <= self.budget:
                self._values[key] = result
                self.bytes += size
                while self._values and self.bytes > self.budget:
                    _old_key, old = self._values.popitem(last=False)
                    self.bytes -= int(old[0].nbytes)
        return result

    def clear(self) -> None:
        with self._lock:
            self._values.clear()
            self.bytes = 0


def _outside_distance(alpha: np.ndarray) -> np.ndarray:
    if not np.any(alpha > 1e-6):
        return np.full(alpha.shape, np.inf, dtype=np.float32)
    # distance_transform_edt measures nonzero pixels to the nearest zero.
    # Transparent pixels are therefore the foreground and opaque pixels the
    # zero-valued targets.
    return distance_transform_edt(alpha <= 1e-6).astype(np.float32, copy=False)


def _outside_distance_bounded(alpha: np.ndarray, margin: int) -> np.ndarray:
    """Compute exact distances within the outline's finite support in small blocks."""
    height, width = alpha.shape
    result = np.full((height, width), np.inf, dtype=np.float32)
    halo = math.ceil(margin) + 1
    for top in range(0, height, 256):
        bottom = min(height, top + 256)
        source_top, source_bottom = max(0, top - halo), min(height, bottom + halo)
        for left in range(0, width, 256):
            right = min(width, left + 256)
            source_left, source_right = max(0, left - halo), min(width, right + halo)
            transparent = alpha[source_top:source_bottom, source_left:source_right] <= 1e-6
            if np.all(transparent):
                continue
            distances = distance_transform_edt(transparent)
            core = distances[top - source_top:bottom - source_top,
                             left - source_left:right - source_left]
            destination = result[top:bottom, left:right]
            np.copyto(destination, core, where=core <= margin)
    return result


def _outline_effect(
    original: np.ndarray, thickness, opacity, color: str,
    distance_cache: OutlineDistanceCache | None = None,
    amount=1.0,
    *, antialiasing: bool = True, blur_radius=0.0, blur_strength=0.0, blur_radius_limit=None,
) -> np.ndarray:
    alpha = original[..., 3]
    distance = (
        distance_cache.distance(alpha)
        if distance_cache is not None else _outside_distance(alpha)
    )
    rgba = _outline_color(color)
    coverage = _outline_coverage(alpha, distance, thickness, opacity, rgba[3], amount,
                                 antialiasing=antialiasing, blur_radius=blur_radius,
                                 blur_strength=blur_strength, blur_radius_limit=blur_radius_limit)
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


def _brush_outline_effect(original, modifier, thickness, opacity, amount,
                          blur_radius, blur_strength, distance_cache=None):
    """Paint real material strokes along source contours, then place below it."""
    from comic_editor.core.brush_outline import render_brush_outline
    alpha = original[..., 3]
    if float(np.max(thickness)) <= 0:
        return original
    ink = render_brush_outline(alpha, thickness, modifier)
    distance = distance_cache.distance(alpha) if distance_cache else _outside_distance(alpha)
    # The thickness field controls both each brush dab and its outside envelope.
    # This makes an exact zero mask truly zero and bounds unusual/spray tips.
    coverage = (np.clip(np.asarray(thickness)+.5-distance, 0., 1.)
                if modifier.antialiasing else np.asarray(distance <= thickness, np.float32))
    coverage *= np.asarray(thickness) > 0
    coverage *= (1.-alpha) * np.clip(np.asarray(opacity)/100., 0., 1.)
    ink *= coverage[..., None]
    if np.max(blur_radius) > 0 and np.max(blur_strength) > 0:
        blend = np.clip(np.asarray(blur_strength)/100., 0., 1.)
        for channel in range(4):
            wet = _outline_blurred_alpha(ink[..., channel], blur_radius, _outline_radius_limit(modifier))
            ink[..., channel] += (wet-ink[..., channel])*blend
    ink *= ((1.-alpha)*amount)[..., None]
    return original+ink


def _outline_blurred_alpha(alpha, radius, radius_limit=None):
    """Blur constant-color premultiplied outline alpha without blurring its source.

    A uniform radius uses an exact Gaussian. Radius masks interpolate Gaussian
    levels, just as a variable blur pyramid does, without allocating RGBA levels.
    Every level has finite three-sigma support.
    """
    radius = np.clip(np.asarray(radius, dtype=np.float32), 0.0, 100.0)
    minimum, maximum = float(np.min(radius)), float(np.max(radius))
    if radius.ndim == 0:
        return (gaussian_filter(alpha, maximum, mode="constant", truncate=3.0)
                if maximum > 0.0 else alpha.copy())
    # Use the modifier's mask endpoints, never this capture's observed range:
    # panning or cropping must not change the interpolated kernel at a pixel.
    limit = maximum if radius_limit is None else max(maximum, float(radius_limit))
    levels = sorted({0.0, limit, *(r for r in (.5, 1., 2., 4., 8., 16., 32., 64.)
                                  if r < limit)})
    result = np.zeros_like(alpha)
    for index, level in enumerate(levels):
        lower = levels[max(0, index - 1)]
        upper = levels[min(len(levels) - 1, index + 1)]
        if upper < minimum or lower > maximum:
            continue
        weight = np.ones_like(radius)
        if index:
            np.minimum(weight, (radius - lower) / (level - lower), out=weight)
        if index + 1 < len(levels):
            np.minimum(weight, (upper - radius) / (upper - level), out=weight)
        np.clip(weight, 0.0, 1.0, out=weight)
        if np.any(weight):
            blurred = (gaussian_filter(alpha, level, mode="constant", truncate=3.0)
                       if level else alpha)
            result += blurred * weight
    return result


def _outline_blur_fringe(fields):
    return (math.ceil(3.0 * fields["blur_radius_limit"])
            if np.max(fields["blur_strength"]) > 0.0 else 0)


def _outline_radius_limit(modifier):
    binding = modifier.parameter_masks.get("blur_radius")
    return (max(modifier.blur_radius, binding.black_value, binding.white_value)
            if binding else modifier.blur_radius)


def _outline_coverage(alpha, distance, thickness, opacity, color_alpha, amount=1.0,
                      *, antialiasing: bool = True, blur_radius=0.0, blur_strength=0.0,
                      blur_radius_limit=None):
    """One-channel premultiplied contribution, including intensity blending."""
    thickness = np.asarray(thickness, dtype=np.float32)
    if antialiasing:
        coverage = thickness + 0.5 - distance
        np.clip(coverage, 0.0, 1.0, out=coverage)
    else:
        # Threshold only geometric coverage; opacity and masks still blend normally.
        coverage = np.asarray(distance <= thickness, dtype=np.float32)
    transparent = np.clip(1.0 - alpha, 0.0, 1.0)
    # Preserve the existing outside-only coverage and source-over treatment
    # of partially transparent antialiased edges.
    coverage *= transparent
    coverage *= np.clip(np.asarray(opacity, dtype=np.float32) / 100.0, 0.0, 1.0)
    coverage *= color_alpha
    # Blur the separate outline component before compositing it under the
    # unchanged source. Constant RGB makes blurring alpha premultiplied-safe.
    wet = None
    if np.max(blur_radius) > 0.0 and np.max(blur_strength) > 0.0:
        wet = _outline_blurred_alpha(coverage, blur_radius, blur_radius_limit)
        wet *= transparent
        wet *= amount
    coverage *= transparent
    coverage *= amount
    if wet is not None:
        strength = np.clip(np.asarray(blur_strength, dtype=np.float32) / 100.0, 0.0, 1.0)
        coverage += (wet - coverage) * strength
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
        for name in ("thickness", "opacity", "intensity", "blur_radius", "blur_strength")
    }
    fields["blur_radius_limit"] = _outline_radius_limit(modifier)
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
    padding += _outline_blur_fringe(fields)
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
        antialiasing=modifier.antialiasing,
        blur_radius=fields["blur_radius"], blur_strength=fields["blur_strength"],
        blur_radius_limit=fields["blur_radius_limit"],
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
            for name in ("thickness", "opacity", "intensity", "blur_radius", "blur_strength")
        }
        fields["blur_radius_limit"] = _outline_radius_limit(modifier)
        parameters.append(fields)
        fringe = max(0, math.ceil(float(np.max(fields["thickness"])) + 0.5))
        fringe += _outline_blur_fringe(fields)
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
                                  modifier.color, distance_cache, amount,
                                  antialiasing=modifier.antialiasing,
                                  blur_radius=fields["blur_radius"],
                                  blur_strength=fields["blur_strength"],
                                  blur_radius_limit=fields["blur_radius_limit"])
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
    cancelled=None,
    original_pixels=None,
    return_pixels=False,
    pixel_origin=(0, 0),
    _point_lut=True,
) -> QImage:
    active_modifiers = [modifier for modifier in modifiers if not modifier.muted]
    for modifier in active_modifiers:
        if isinstance(modifier, (BrightnessContrastModifier, CurvesModifier)):
            modifier.validate()
    active_modifiers = [modifier for modifier in active_modifiers if not (
        isinstance(modifier, BrightnessContrastModifier)
        and modifier.brightness == 0 and modifier.contrast == 0
        and not {"brightness", "contrast"}.intersection(modifier.parameter_masks))]
    active_modifiers = [modifier for modifier in active_modifiers if not (
        isinstance(modifier, CurvesModifier) and (curves_is_neutral(modifier)
        or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks))]
    if image.isNull() or not active_modifiers:
        return (_qimage_premultiplied(image) if original_pixels is None else original_pixels) if return_pixels else image
    if (not current_contract().floating and len(active_modifiers) == 1 and isinstance(active_modifiers[0], OutlineModifier)
            and active_modifiers[0].style == "solid" and not return_pixels):
        return _outline_qimage(image, active_modifiers[0], mask_fields or {}, outline_distance_cache)
    if not current_contract().floating and not return_pixels and all(isinstance(modifier, OutlineModifier) and modifier.style == "solid"
           for modifier in active_modifiers):
        return _outline_stack_qimage(image, active_modifiers, mask_fields or {}, outline_distance_cache)
    current = _qimage_premultiplied(image) if original_pixels is None else original_pixels.copy()
    if cancelled is not None and cancelled():
        return None
    if _point_lut and original_pixels is None:
        from comic_editor.ui.point_lut import point_chain
        accelerated = point_chain(image, current, active_modifiers)
        if accelerated is not None:
            if cancelled is not None and cancelled():
                return None
            return accelerated if return_pixels else _premultiplied_qimage(np.clip(accelerated, 0., 1.))
    height, width = current.shape[:2]
    mask_fields = mask_fields or {}
    for modifier in active_modifiers:
        if cancelled is not None and cancelled():
            return None
        modifier.validate()
        amount = _parameter_field(
            modifier, "intensity", modifier.intensity,
            (height, width), mask_fields,
        )
        amount = np.asarray(amount, dtype=np.float32) / 100.0
        if np.max(amount) <= 0.0:
            continue
        if isinstance(modifier, SolidColorOverlayModifier):
            from comic_editor.core.blend_modes import blend_rgb
            if isinstance(modifier, TextureModifier):
                from comic_editor.core.texture_library import texture_image
                if modifier.texture_quad is not None:
                    from comic_editor.ui.texture_rendering import transformed_texture
                    texture_pixels, footprint = transformed_texture(
                        modifier, width, height, world_origin, world_to_image)
                    if texture_pixels is None:
                        continue
                else:
                    texture = texture_image(modifier.texture_data, width, height)
                    if texture.isNull():
                        continue
                    texture_pixels = _qimage_premultiplied(texture)
                adjustments = tuple(_parameter_field(modifier, attribute,
                    getattr(modifier, attribute), (height, width), mask_fields)
                    for attribute in ("hue", "saturation", "lightness"))
                if any(np.any(value != 0) for value in adjustments):
                    texture_pixels = _hsl_effect(texture_pixels, *adjustments)
                front = _straight(texture_pixels)
            else:
                from PySide6.QtGui import QColor
                color = QColor(modifier.color)
                front = np.asarray([color.redF(), color.greenF(), color.blueF(), color.alphaF()], dtype=np.float32)
            back = _straight(current)
            if amount.ndim == 2:
                amount = amount[..., None]
            if modifier.blend_mode == "replace":
                # Replace retains the target's silhouette, but uses the raw
                # texture alpha/color, including transparent texture pixels.
                effect = np.concatenate((front[..., :3] * front[..., 3:4] * current[..., 3:4],
                                         front[..., 3:4] * current[..., 3:4]), axis=-1)
                if isinstance(modifier, TextureModifier) and modifier.texture_quad is not None:
                    # Moving/scaling the overlay exposes the original artwork
                    # outside its quad, while transparent pixels inside Replace
                    # still replace incoming coverage.
                    amount = amount * footprint
                current = current * (1 - amount) + effect * amount
            else:
                blended = blend_rgb(back[..., :3], front[..., :3], modifier.blend_mode)
                weight = amount * front[..., 3:4]
                current[..., :3] = current[..., :3] * (1 - weight) + blended * current[..., 3:4] * weight
            continue
        elif isinstance(modifier, (HalftoneModifier, PixelateModifier)):
            from comic_editor.ui.pattern_rendering import apply_pattern_effect
            effect = _qimage_premultiplied(apply_pattern_effect(
                _premultiplied_qimage(current), modifier))
            mask = amount
        elif isinstance(modifier, KuwaharaModifier):
            from comic_editor.core.kuwahara import apply_kuwahara
            effect = apply_kuwahara(current, modifier,
                size=_parameter_field(modifier, "size", modifier.size, (height, width), mask_fields),
                strength=_parameter_field(modifier, "strength", modifier.strength, (height, width), mask_fields),
                cancelled=cancelled, origin=pixel_origin)
            if effect is None:
                return None
            if amount.ndim == 2:
                amount = amount[..., None]
            current[..., :3] += (effect[..., :3] - current[..., :3]) * amount
            continue
        elif isinstance(modifier, CurvesModifier):
            effect = apply_curves(current, modifier)
            if amount.ndim == 2:
                amount = amount[..., None]
            current += (effect - current) * amount
            continue
        elif isinstance(modifier, (DitheringModifier, SharpnessModifier)):
            from comic_editor.core.image_filters import dither, sharpen
            names = ("strength", "levels", "pixel_size") if isinstance(modifier, DitheringModifier) else ("strength", "radius", "threshold")
            parameters = {name: _parameter_field(modifier, name, getattr(modifier, name),
                                               (height, width), mask_fields) for name in names}
            effect = (dither(current, modifier, origin=pixel_origin, **parameters) if isinstance(modifier, DitheringModifier)
                      else sharpen(current, **parameters))
            if amount.ndim == 2:
                amount = amount[..., None]
            current[..., :3] += (effect[..., :3] - current[..., :3]) * amount
            continue
        elif isinstance(modifier, BrightnessContrastModifier):
            effect = _brightness_contrast_effect(
                current,
                _parameter_field(modifier, "brightness", modifier.brightness,
                                 (height, width), mask_fields),
                _parameter_field(modifier, "contrast", modifier.contrast,
                                 (height, width), mask_fields))
            if amount.ndim == 2:
                amount = amount[..., None]
            # The adjustment cannot change coverage. Blend only RGB so an
            # intensity slider does not round antialiased alpha downward.
            current[..., :3] += (effect[..., :3] - current[..., :3]) * amount
            continue
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
                mask = mask * amount
            else:
                mask = amount
        elif isinstance(modifier, DistortModifier):
            from comic_editor.ui.distort_rendering import render_distort
            mapping = world_to_image or QTransform.fromTranslate(-world_origin[0], -world_origin[1])
            inverse, valid = mapping.inverted()
            if not valid:
                continue
            warped = render_distort(_premultiplied_qimage(current), QRectF(0, 0, width, height),
                                     modifier, inverse, QRectF(0, 0, width, height))
            effect = _qimage_premultiplied(warped)
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
            if modifier.style == "brush":
                fields = {name: _parameter_field(modifier, name, getattr(modifier, name),
                                                (height, width), mask_fields)
                          for name in ("thickness", "opacity", "blur_radius", "blur_strength")}
                current = _brush_outline_effect(current, modifier, amount=amount,
                                                 distance_cache=outline_distance_cache, **fields)
                continue
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
                antialiasing=modifier.antialiasing,
                blur_radius=_parameter_field(
                    modifier, "blur_radius", modifier.blur_radius, (height, width), mask_fields,
                ),
                blur_strength=_parameter_field(
                    modifier, "blur_strength", modifier.blur_strength, (height, width), mask_fields,
                ),
                blur_radius_limit=_outline_radius_limit(modifier),
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
    return current if return_pixels else _premultiplied_qimage(current if current_contract().floating else np.clip(current, 0.0, 1.0))


def apply_opacity_mask(
    image: QImage, mask: np.ndarray, black_value: float,
    white_value: float,
) -> QImage:
    """Mask premultiplied bytes in bounded strips, preserving float32 rounding.

    All four channels receive the same opacity, so native ARGB byte order is
    sufficient. Most painted masks are opaque/transparent apart from a small
    antialiased edge; only fractional pixels need floating-point arithmetic.
    """
    if image.isNull():
        return image
    normalized = np.asarray(mask, dtype=np.float32)
    width, height = image.width(), image.height()
    if normalized.shape != (height, width):
        return image
    black, white = float(black_value), float(white_value)
    from comic_editor.render.pixels import current_contract, premultiplied_pixels, working_image
    if current_contract().floating:
        opacity = np.clip(normalized, 0., 1.) * (white - black) + black
        np.clip(opacity, 0., 1., out=opacity)
        pixels = premultiplied_pixels(image)
        pixels *= opacity[..., None]
        return working_image(pixels)
    converted = image.convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
    result = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    # Copy bytes into an ordinary document-pixel image, like the legacy helper.
    # This intentionally does not inherit a source's device-pixel ratio or color
    # space. The caller's source and mask stay unchanged throughout processing.
    data = np.frombuffer(result.bits(), dtype=np.uint8, count=result.sizeInBytes()).reshape(height, result.bytesPerLine())
    pixels = data[:, :width * 4].reshape(height, width, 4)
    source = np.frombuffer(converted.constBits(), dtype=np.uint8, count=converted.sizeInBytes()).reshape(height, converted.bytesPerLine())
    pixels[:] = source[:, :width * 4].reshape(height, width, 4)
    packed = pixels.view(np.uint32).reshape(height, width)
    rows = max(1, min(128, 131072 // width))
    for top in range(0, height, rows):
        bottom = min(height, top + rows)
        opacity = np.clip(normalized[top:bottom], 0.0, 1.0)
        opacity *= white - black
        opacity += black
        np.clip(opacity, 0.0, 1.0, out=opacity)
        target = pixels[top:bottom]
        fractional = (opacity > 0.0) & (opacity < 1.0)
        count = np.count_nonzero(fractional)
        if count <= opacity.size // 4:
            # Clear transparent pixels without converting opaque artwork, then
            # process only the antialiased edge when that edge is sparse.
            packed[top:bottom][~(opacity > 0.0)] = 0
            if not count:
                continue
            current = target[fractional].astype(np.float32)
            current /= 255.0
            current *= opacity[fractional, None]
            current *= 255.0
            target[fractional] = current.astype(np.uint8)
        else:
            current = target.astype(np.float32)
            current /= 255.0
            current *= opacity[..., None]
            # Preserve the legacy arithmetic order. Replacing these with
            # byte*opacity can round integer boundaries differently by one.
            # Opacity is already clipped, so channel clipping is redundant.
            current *= 255.0
            np.copyto(target, current, casting="unsafe")
    return result
