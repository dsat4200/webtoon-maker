"""Pixel processing for nondestructive document modifiers."""
from __future__ import annotations

import numpy as np
from PIL import Image, ImageFilter
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
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
    original: np.ndarray, modifier: HueSaturationLightnessModifier,
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
    hue = np.mod(hue / 6.0 + modifier.hue / 360.0, 1.0)
    sat_delta = modifier.saturation / 100.0
    saturation = (
        saturation + (1.0 - saturation) * sat_delta
        if sat_delta >= 0 else saturation * (1.0 + sat_delta)
    )
    light_delta = modifier.lightness / 100.0
    light = (
        light + (1.0 - light) * light_delta
        if light_delta >= 0 else light * (1.0 + light_delta)
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


def apply_modifier_stack(
    image: QImage, modifiers: list[ModifierInstance],
    world_origin: tuple[float, float],
) -> QImage:
    if image.isNull() or not modifiers:
        return image
    current = _premultiplied(_qimage_rgba(image))
    height, width = current.shape[:2]
    for modifier in modifiers:
        modifier.validate()
        amount = modifier.intensity / 100.0
        if amount <= 0.0:
            continue
        if isinstance(modifier, HueSaturationLightnessModifier):
            effect = _hsl_effect(current, modifier)
            mask = amount
        else:
            effect = _blur_effect(current, modifier.strength)
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
        current = current * (1.0 - mask) + effect * mask
    return _rgba_qimage(_straight(np.clip(current, 0.0, 1.0)) * 255.0)

