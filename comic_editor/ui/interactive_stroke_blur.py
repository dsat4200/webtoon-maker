"""Keep blurred stroke material and fill in one resumable exact handoff."""
from copy import deepcopy

import numpy as np
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.ui.interactive_effects import _draft
from comic_editor.ui.interactive_strokes import _pack, _unpack
from comic_editor.ui.modifier_rendering import apply_modifier_stack


def cached_blur(canvas, scope, cache_key):
    key = ("stroke-blur", cache_key)
    packed = canvas._effect_jobs.result(scope, key) if scope is not None else None
    if packed is None:
        packed = canvas._modifier_cache_get(key)
    return _unpack(packed) if packed is not None else None


def render_stroke_blur(canvas, image, background, bounds, modifier, mapping,
                       *, cache_key, scope, provisional=False):
    """Render equally placed channels; only complete pairs can be reused."""
    if not provisional:
        cached = cached_blur(canvas, scope, cache_key)
        if cached is not None:
            return (*cached, False)
    world_to_image = canvas._world_to_image_transform(
        mapping, bounds, image.width(), image.height())
    fields = canvas._modifier_mask_fields([modifier], image.width(), image.height(),
        world_to_image, mapping.mapRect(bounds))
    origin = mapping.map(bounds.topLeft()).toTuple()
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    navigator = getattr(canvas, "_effect_preview_channel", "canvas") == "navigator"
    draft_only = interactive and image.width() * image.height() > 16384
    asynchronous = draft_only and not provisional and not navigator and scope is not None
    if asynchronous:
        incoming, fill = QImage(image), QImage(background)
        effect = deepcopy(modifier)
        masks = {name: np.array(field, copy=True) for name, field in fields.items()}
        placement = QTransform(world_to_image)

        def compute(cancelled, incoming=incoming, fill=fill, effect=effect,
                    masks=masks, placement=placement, origin=origin):
            if cancelled():
                return None
            material = apply_modifier_stack(incoming, [effect], origin, masks,
                world_to_image=placement)
            if cancelled():
                return None
            base = apply_modifier_stack(fill, [effect], origin, masks,
                world_to_image=placement)
            return None if cancelled() else _pack(material, base)

        size = (32 * (int(image.sizeInBytes()) + int(background.sizeInBytes()))
                + sum(field.nbytes for field in masks.values()))
        asynchronous = canvas._effect_jobs.request(
            scope, ("stroke-blur", cache_key), compute, size, allow_oversized=True)
    if draft_only and (asynchronous or provisional or navigator):
        key = ("stroke-blur-draft", cache_key,
               int(image.cacheKey()), int(background.cacheKey()))
        packed = canvas._modifier_cache_get(key)
        if packed is None:
            packed = _pack(
                _draft(image, [modifier], origin, fields, world_to_image, False),
                _draft(background, [modifier], origin, fields, world_to_image, False))
            canvas._modifier_cache_put(key, packed)
        material, base = _unpack(packed)
        return (material.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation),
                base.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation), True)
    material = apply_modifier_stack(image, [modifier], origin, fields,
        world_to_image=world_to_image, blur_pyramid_cache=canvas._blur_pyramid_cache)
    base = apply_modifier_stack(background, [modifier], origin, fields,
        world_to_image=world_to_image, blur_pyramid_cache=canvas._blur_pyramid_cache)
    if not provisional:
        canvas._modifier_cache_put(("stroke-blur", cache_key), _pack(material, base))
    return material, base, provisional
