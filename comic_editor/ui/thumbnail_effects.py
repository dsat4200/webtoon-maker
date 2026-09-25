"""Bounded navigator captures with their original document-space placement."""
from copy import deepcopy
import math

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BrightnessContrastModifier, CurvesModifier, HalftoneModifier, HueSaturationLightnessModifier,
    MirrorModifier, OutlineModifier, PixelateModifier,
)


def capture_scale(canvas, bounds, modifiers):
    if (not canvas._interactive_render
            or getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
            or canvas._render_base_alpha or canvas._rendering_mask_contributor > 0):
        return 1.
    supported = (ArrayModifier, BlurModifier, BrightnessContrastModifier, CurvesModifier, HalftoneModifier,
                 HueSaturationLightnessModifier, MirrorModifier, OutlineModifier,
                 PixelateModifier)
    if any(not isinstance(modifier, supported)
           or isinstance(modifier, BlurModifier) and modifier.mode == "focal"
           for modifier in modifiers):
        return 1.
    width, height = max(1., bounds.width()), max(1., bounds.height())
    return min(1., 256 / max(width, height), math.sqrt(32768 / (width * height)))


def scaled_modifiers(modifiers, scale):
    """Scale local pixel distances; world-space axes and centers stay fixed."""
    result = deepcopy(modifiers)
    for modifier in result:
        fields = ()
        if isinstance(modifier, BlurModifier):
            fields = ("strength",)
        elif isinstance(modifier, OutlineModifier):
            fields = ("thickness", "blur_radius")
        elif isinstance(modifier, PixelateModifier):
            fields = ("pixel_size", "blur")
        for field in fields:
            setattr(modifier, field, getattr(modifier, field) * scale)
            binding = modifier.parameter_masks.get(field)
            if binding is not None:
                binding.black_value *= scale
                binding.white_value *= scale
    return result
