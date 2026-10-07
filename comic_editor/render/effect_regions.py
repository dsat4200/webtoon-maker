"""Exact document-region reuse for the stage renderer.

The camera is a request, never part of an effect's identity.  Only genuinely
pointwise operations are evaluated on independent tiles here.  Blur pyramids,
patterns, geometry warps and contour effects keep their existing full-frame
semantics until they have frame-addressed region implementations.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from comic_editor.core.models import (
    BrightnessContrastModifier, CurvesModifier, HueSaturationLightnessModifier,
    PosterizeModifier,
)


TILE_SIZE = 256


def projection_requires_exact(canvas):
    """A cold document tile must be complete before it can be presented.

    This deliberately does not change _interactive_render: interactive source
    windows and coordinate semantics still apply while reduced previews and
    deferred computations are suppressed for this capture.
    """
    return bool(getattr(canvas, "_projection_exact", False))


def exact_reference_sampling(canvas):
    """Opt into bounded exact reuse without changing native sampling rules."""
    return bool(getattr(canvas, "_exact_reference_render", False)
                and projection_requires_exact(canvas)
                and not canvas._render_base_alpha
                and canvas._rendering_mask_contributor <= 0
                and not getattr(canvas, "_render_modifier_sources", ())
                and not getattr(canvas, "_render_cage_source", False)
                and not getattr(canvas, "_rendering_halftone_source", False)
                and not getattr(canvas, "_rendering_compound_references", False)
                and not getattr(canvas, "_rendering_outward_gradient", False)
                and getattr(canvas, "_tiling_capture_geometry", None) is None
                and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator")


def region_requests_enabled(canvas):
    """The projection renderer opts in; exports retain their existing path."""
    return (getattr(canvas, "_effect_region_requests", False)
            and canvas._interactive_render
            and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
            and not canvas._render_base_alpha
            and canvas._rendering_mask_contributor <= 0)


def region_scope(canvas, scope, bounds):
    """Keep independent region jobs/checkpoints from replacing their siblings.

    Pixel revisions and settings belong to the request key, not the scope:
    changing either still cancels obsolete work for this same output region.
    """
    if scope is None or not region_requests_enabled(canvas):
        return scope
    return (*scope, ("effect-region", tuple(QRectF(bounds).getRect())))


def pointwise_stack(modifiers):
    active = [modifier for modifier in modifiers if not modifier.muted
              and (modifier.intensity > 0 or "intensity" in modifier.parameter_masks)]
    return bool(active) and all(
        isinstance(modifier, (BrightnessContrastModifier, CurvesModifier,
                              HueSaturationLightnessModifier))
        or isinstance(modifier, PosterizeModifier) and not modifier.simplify_enabled
        for modifier in active)


def _tiles(frame, requested):
    """Stable half-open tile addresses, including negative document positions."""
    left = math.floor(requested.left() / TILE_SIZE)
    top = math.floor(requested.top() / TILE_SIZE)
    right = math.ceil(requested.right() / TILE_SIZE)
    bottom = math.ceil(requested.bottom() / TILE_SIZE)
    for y in range(top, bottom):
        for x in range(left, right):
            rect = QRectF(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE).intersected(frame)
            if not rect.isEmpty():
                yield (x, y), rect


def pointwise_output(canvas, image, bounds, modifiers, mapping, *, required,
                     signatures, placement, source_identity, request_scope,
                     nearest=False):
    """Return exact pointwise tiles, or ``None`` when this request cannot use them.

    ``image=None`` is a cache-only lookup before source capture.  The complete
    semantic frame remains in ``placement``; masks are sampled using each
    tile's actual document mapping.  Stage boundaries retain RGBA8 rounding,
    matching render_stages rather than merging color operations into one pass.
    """
    if (required is None or request_scope is None
            or not region_requests_enabled(canvas) or not pointwise_stack(modifiers)):
        return None
    frame = QRectF(bounds)
    if frame.isEmpty() or frame != QRectF(frame.toAlignedRect()):
        return None
    if image is not None and (image.width() != int(frame.width())
                              or image.height() != int(frame.height())):
        return None
    requested = QRectF(frame.intersected(required).toAlignedRect()).intersected(frame)
    if requested.isEmpty():
        return None
    active = [(index, modifier) for index, modifier in enumerate(modifiers)
              if not modifier.muted
              and (modifier.intensity > 0 or "intensity" in modifier.parameter_masks)]
    completed = []
    for address, tile_bounds in _tiles(frame, requested):
        current = None
        for index, modifier in active:
            key = ("pointwise-region", source_identity, tuple(signatures[:index + 1]),
                   placement, tuple(tile_bounds.getRect()))
            scope = ("pointwise-region", request_scope, modifier.modifier_id, address)
            retained = canvas._effect_jobs.retained_get(scope, key)
            cached = retained[0] if retained is not None else canvas._modifier_cache_get(key)
            if cached is not None:
                current = cached
                canvas._effect_jobs.retained_put(scope, key, cached)
                continue
            if image is None:
                # A later stage can remain cached after an earlier one was
                # evicted. Only the final stage is required for presentation.
                if index != active[-1][0]:
                    continue
                return None
            if current is None:
                crop = QRectF(tile_bounds)
                crop.translate(-frame.topLeft())
                current = image.copy(crop.toAlignedRect())
            world_to_image = canvas._world_to_image_transform(
                mapping, tile_bounds, current.width(), current.height())
            revision = getattr(canvas, "_effect_provisional_revision", 0)
            fields = canvas._modifier_mask_fields(
                [modifier], current.width(), current.height(), world_to_image,
                mapping.mapRect(tile_bounds))
            if revision != getattr(canvas, "_effect_provisional_revision", 0):
                return None
            from comic_editor.render.modifier_rendering import apply_modifier_stack
            current = apply_modifier_stack(
                current, [modifier], mapping.map(tile_bounds.topLeft()).toTuple(), fields,
                world_to_image=world_to_image, nearest=nearest)
            canvas._modifier_cache_put(key, current)
            canvas._effect_jobs.retained_put(scope, key, current)
        completed.append((tile_bounds, current))
    result = QImage(int(requested.width()), int(requested.height()),
                    QImage.Format_ARGB32_Premultiplied)
    result.fill(Qt.transparent)
    painter = QPainter(result)
    try:
        for tile_bounds, tile in completed:
            painter.drawImage(tile_bounds.topLeft() - requested.topLeft(), tile)
    finally:
        painter.end()
    return result, requested
