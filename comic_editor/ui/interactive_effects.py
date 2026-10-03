"""Bounded editing previews with exact, detached background rendering."""
from copy import deepcopy
import math

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import BlurModifier, OutlineModifier, KuwaharaModifier, SharpnessModifier, DitheringModifier
from comic_editor.core.effect_geometry import outline_blur_padding
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, OutlineDistanceCache, apply_modifier_stack,
)
from comic_editor.ui.effect_regions import projection_requires_exact, exact_reference_sampling
from comic_editor.ui.async_projection import (
    ProjectionPending, projection_deferred, projection_result_or_pending,
)


def outline_capture_bounds(canvas, painter, bounds, visible, modifiers):
    """Keep long drawings from replacing the entire effect LRU on every pan.

    Outlines have finite support. A guarded source window gives the same
    visible pixels; full captures and exports still use the complete image.
    Snap the window outward to reduce recapture during small camera changes.
    """
    if not (modifiers and all(isinstance(m, OutlineModifier) and m.style == "solid" for m in modifiers)
            and canvas._interactive_render
            and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
            and painter.combinedTransform().type().value <= QTransform.TransformationType.TxScale.value
            and not canvas._render_modifier_sources
            and not canvas._render_base_alpha
            and not canvas._rendering_mask_contributor
            and not canvas._render_cage_source
            and not getattr(canvas, "_rendering_halftone_source", False)
            and getattr(canvas, "_tiling_capture_geometry", None) is None):
        return bounds
    padding = 4. + sum(canvas._modifier_maximum(m, "thickness", m.thickness) + outline_blur_padding(m)
                       for m in modifiers)
    needed = visible.adjusted(-padding, -padding, padding, padding)
    left, top = math.floor(needed.left() / 256) * 256, math.floor(needed.top() / 256) * 256
    right, bottom = math.ceil(needed.right() / 256) * 256, math.ceil(needed.bottom() / 256) * 256
    return bounds.intersected(QRectF(left, top, right-left, bottom-top))


def _draft(image, modifiers, origin, fields, mapping, nearest):
    scale = min(1., 256 / max(image.width(), image.height()),
                (32768 / (image.width() * image.height())) ** .5)
    width, height = max(1, round(image.width() * scale)), max(1, round(image.height() * scale))
    small = image.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    effects = deepcopy(modifiers)
    for modifier in effects:
        parameter = None
        if isinstance(modifier, BlurModifier):
            parameter = "strength"
            modifier.strength *= scale
            modifier.focal_center = tuple(value * scale for value in modifier.focal_center)
            modifier.focal_radius *= scale
        elif isinstance(modifier, OutlineModifier):
            from comic_editor.core.brush_outline import scale_outline_brush
            scale_outline_brush(modifier, scale)
            parameter = "thickness"
            modifier.thickness *= scale
            modifier.blur_radius *= scale
            radius_mask = modifier.parameter_masks.get("blur_radius")
            if radius_mask is not None:
                radius_mask.black_value *= scale
                radius_mask.white_value *= scale
        elif isinstance(modifier, KuwaharaModifier):
            parameter = "size"
            modifier.size *= scale
            modifier.tensor_radius *= scale
            modifier.quality = "draft"
        elif isinstance(modifier, SharpnessModifier):
            parameter = "radius"
            modifier.radius *= scale
        elif isinstance(modifier, DitheringModifier):
            parameter = "pixel_size"
            modifier.pixel_size *= scale
        if parameter in modifier.parameter_masks:
            binding = modifier.parameter_masks[parameter]
            binding.black_value *= scale
            binding.white_value *= scale
    xs = np.minimum(((np.arange(width) + .5) * image.width() / width).astype(int), image.width() - 1)
    ys = np.minimum(((np.arange(height) + .5) * image.height() / height).astype(int), image.height() - 1)
    masks = {key: value[ys[:, None], xs[None, :]] for key, value in fields.items()
             if np.shape(value) == (image.height(), image.width())}
    transform = mapping * QTransform.fromScale(scale, scale) if mapping is not None else None
    result = apply_modifier_stack(small, effects, tuple(value * scale for value in origin),
                                  masks, world_to_image=transform, nearest=nearest)
    return result


def render_interactive_stack(canvas, image, modifiers, world_origin, mask_fields=None,
                             *, cache_key, scope, world_to_image=None, nearest=False,
                             upstream_provisional=False):
    """Return (image, provisional); only exact pixels enter ``cache_key``.

    Callers capturing modified descendants must propagate their provisional
    revision and avoid caching those captures as exact source images.
    """
    fields = mask_fields or {}
    exact = projection_requires_exact(canvas)
    deferred = projection_deferred(canvas) and scope is not None and not upstream_provisional
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    retain_exact = ((interactive or exact_reference_sampling(canvas))
                    and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator")
    if not upstream_provisional:
        cached = (projection_result_or_pending(canvas, scope, cache_key) if deferred
                  else canvas._effect_jobs.result(scope, cache_key))
        if cached is None:
            cached = canvas._modifier_cache_get(cache_key)
            if cached is not None and retain_exact:
                canvas._effect_jobs.retained_put(("result", scope), cache_key, cached)
        if cached is not None:
            return cached, False
    pixels = image.width() * image.height()
    preview_only = (interactive and not exact and pixels > 16384
                    and getattr(canvas, "_effect_preview_channel", "canvas") == "navigator")
    active = [modifier for modifier in modifiers if not modifier.muted]
    # The cropped outline kernel is already fast at ordinary text sizes.
    inexpensive = not active or pixels <= 16384 or (all(isinstance(m, OutlineModifier) and m.style == "solid"
                                      and outline_blur_padding(m) == 0 for m in active)
                                      and pixels <= 2 * 1024 * 1024)
    if (getattr(canvas, '_stroke_projection_active', False) and pixels > 16384
            and any(isinstance(m, OutlineModifier) for m in active)):
        # A single outline is cheap in isolation, but a stroke patch can cross
        # many outlined layers. Their cumulative distance work blocks contact.
        inexpensive = False
    asynchronous = (deferred and pixels > 16384 or
                    interactive and not exact and not inexpensive and not upstream_provisional and not preview_only)
    if asynchronous:
        incoming, effects = QImage(image), deepcopy(modifiers)
        masks = {key: np.array(value, copy=True) for key, value in fields.items()}
        mapping = QTransform(world_to_image) if world_to_image is not None else None
        jobs = canvas._effect_jobs
        if not hasattr(jobs, "stack_caches"):
            jobs.stack_caches = (OutlineDistanceCache(), BlurPyramidCache())
        outline_cache, blur_cache = jobs.stack_caches

        def compute(cancelled):
            if cancelled():
                return None
            result = apply_modifier_stack(incoming, effects, world_origin, masks,
                world_to_image=mapping, nearest=nearest,
                outline_distance_cache=outline_cache, blur_pyramid_cache=blur_cache,
                cancelled=cancelled)
            return None if cancelled() else result

        size = int(image.sizeInBytes()) * 32 + sum(value.nbytes for value in masks.values())
        asynchronous = jobs.request(scope, cache_key, compute, size, allow_oversized=True,
                                    require_exact=deferred)
        if deferred:
            raise ProjectionPending(scope, cache_key)
    if not exact and (asynchronous or preview_only or (interactive and upstream_provisional)):
        draft_key = ("interactive-draft", cache_key, int(image.cacheKey()))
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            result = _draft(image, modifiers, world_origin, fields, world_to_image, nearest)
            canvas._modifier_cache_put(draft_key, result)
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
        return result.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation), True
    result = apply_modifier_stack(image, modifiers, world_origin, fields,
        world_to_image=world_to_image, nearest=nearest,
        outline_distance_cache=canvas._outline_distance_cache,
        blur_pyramid_cache=canvas._blur_pyramid_cache)
    if retain_exact and not upstream_provisional:
        canvas._effect_jobs.retained_put(("result", scope), cache_key, result)
    return result, upstream_provisional
