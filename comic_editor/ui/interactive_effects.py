"""Bounded editing previews with exact, detached background rendering."""
from copy import deepcopy

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import BlurModifier, OutlineModifier
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, OutlineDistanceCache, apply_modifier_stack,
)


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
            parameter = "thickness"
            modifier.thickness *= scale
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
    if not upstream_provisional:
        cached = canvas._effect_jobs.result(scope, cache_key)
        if cached is None:
            cached = canvas._modifier_cache_get(cache_key)
        if cached is not None:
            return cached, False
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    pixels = image.width() * image.height()
    preview_only = (interactive and pixels > 16384
                    and getattr(canvas, "_effect_preview_channel", "canvas") == "navigator")
    active = [modifier for modifier in modifiers if not modifier.muted]
    # The cropped outline kernel is already fast at ordinary text sizes.
    inexpensive = not active or pixels <= 16384 or (all(isinstance(m, OutlineModifier) for m in active)
                                      and pixels <= 2 * 1024 * 1024)
    asynchronous = interactive and not inexpensive and not upstream_provisional and not preview_only
    if asynchronous:
        incoming, effects = QImage(image), deepcopy(modifiers)
        masks = {key: np.array(value, copy=True) for key, value in fields.items()}
        mapping = QTransform(world_to_image) if world_to_image is not None else None
        jobs = canvas._effect_jobs
        if not hasattr(jobs, "stack_caches"):
            jobs.stack_caches = (OutlineDistanceCache(16 * 1024 * 1024),
                                 BlurPyramidCache(16 * 1024 * 1024))
        outline_cache, blur_cache = jobs.stack_caches

        def compute(cancelled):
            if cancelled():
                return None
            result = apply_modifier_stack(incoming, effects, world_origin, masks,
                world_to_image=mapping, nearest=nearest,
                outline_distance_cache=outline_cache, blur_pyramid_cache=blur_cache)
            return None if cancelled() else result

        size = int(image.sizeInBytes()) * 32 + sum(value.nbytes for value in masks.values())
        asynchronous = jobs.request(scope, cache_key, compute, size, allow_oversized=True)
    if asynchronous or preview_only or (interactive and upstream_provisional):
        draft_key = ("interactive-draft", cache_key, int(image.cacheKey()))
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            result = _draft(image, modifiers, world_origin, fields, world_to_image, nearest)
            canvas._modifier_cache_put(draft_key, result)
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
        return result.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation), True
    return apply_modifier_stack(image, modifiers, world_origin, fields,
        world_to_image=world_to_image, nearest=nearest,
        outline_distance_cache=canvas._outline_distance_cache,
        blur_pyramid_cache=canvas._blur_pyramid_cache), upstream_provisional
