"""Bounded transient captures with their original document-space placement."""
from copy import deepcopy
import math

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BrightnessContrastModifier, CurvesModifier, HalftoneModifier, HueSaturationLightnessModifier,
    MirrorModifier, OutlineModifier, PixelateModifier, KuwaharaModifier, DitheringModifier, SharpnessModifier,
    CageTransformModifier, DistortModifier, RadialBlurModifier, PosterizeModifier,
)


def live_effect_draft(canvas):
    """Only an explicit interactive scene request may compact live effects."""
    return bool(getattr(canvas, '_bounded_effect_preview', False)
                and not getattr(canvas, '_projection_exact', False)
                and canvas._interactive_render
                and getattr(canvas, '_effect_preview_channel', 'canvas') in {'canvas', 'overflow'}
                and not canvas._render_base_alpha
                and canvas._rendering_mask_contributor <= 0
                and not getattr(canvas, '_rendering_halftone_source', False)
                and not getattr(canvas, '_render_cage_source', False)
                and getattr(canvas, '_tiling_capture_geometry', None) is None)


def compact_effects_supported(modifiers):
    supported = (ArrayModifier, BlurModifier, BrightnessContrastModifier, CurvesModifier, HalftoneModifier,
                 HueSaturationLightnessModifier, MirrorModifier, OutlineModifier,
                 PixelateModifier, KuwaharaModifier, DitheringModifier, SharpnessModifier,
                 CageTransformModifier, DistortModifier, RadialBlurModifier, PosterizeModifier)
    return all(isinstance(modifier, supported)
               and not (isinstance(modifier, BlurModifier) and modifier.mode == "focal")
               for modifier in modifiers)


def capture_scale(canvas, bounds, modifiers):
    if (not canvas._interactive_render
            or (getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
                and not live_effect_draft(canvas))
            or canvas._render_base_alpha or canvas._rendering_mask_contributor > 0
            or not compact_effects_supported(modifiers)):
        return 1.
    width, height = max(1., bounds.width()), max(1., bounds.height())
    # Spatial rigs retain document coordinates. The caller's inverse-scale
    # stage mapping places these smaller captures on that same frame, so mask
    # fields and deformation preparation are small too. Exact/source/mask
    # captures above keep their original sampling grids.
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
            from comic_editor.core.brush_outline import scale_outline_brush
            scale_outline_brush(modifier, scale)
        elif isinstance(modifier, PixelateModifier):
            fields = ("pixel_size", "blur")
        elif isinstance(modifier, PosterizeModifier):
            fields = ('simplify_radius',)
        elif isinstance(modifier, KuwaharaModifier):
            fields = ("size", "tensor_radius")
        elif isinstance(modifier, SharpnessModifier):
            fields = ("radius",)
        elif isinstance(modifier, DitheringModifier):
            fields = ("pixel_size",)
        for field in fields:
            setattr(modifier, field, getattr(modifier, field) * scale)
            binding = modifier.parameter_masks.get(field)
            if binding is not None:
                binding.black_value *= scale
                binding.white_value *= scale
    return result
