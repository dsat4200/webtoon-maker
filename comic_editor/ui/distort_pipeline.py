"""Cached, cancellable distortion stages with a bounded interactive preview."""
import copy
import math
from threading import local

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.ui.modifier_rendering import (
    _parameter_field, _premultiplied_qimage, _qimage_premultiplied,
)
from comic_editor.ui.effect_regions import projection_requires_exact
from comic_editor.ui.async_projection import (
    ProjectionPending, projection_deferred, projection_result_or_pending,
)


_worker_preparation = local()


def _worker_preparation_cache():
    """One bounded cache per executor thread, released when that thread exits.

    The cache only owns immutable numerical snapshots keyed by source-image
    revision. It never retains a canvas, model, or GUI-owned cache.
    """
    cache = getattr(_worker_preparation, "cache", None)
    if cache is None:
        from comic_editor.ui.distort_rendering import PreparedDistortCache
        cache = _worker_preparation.cache = PreparedDistortCache()
    return cache


def render_distort_stage(canvas, image, base, bounds, target, modifier,
                         local_to_world, fields, key, scope, provisional, navigator):
    from comic_editor.ui.distort_rendering import PreparedDistortCache, render_distort

    exact = projection_requires_exact(canvas)
    large = max(image.width() * image.height(), base.width() * base.height()) > 128 * 128
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    mesh_preview = (not exact and interactive and not navigator
                    and modifier.modifier_type == "distort_mesh_warp"
                    and getattr(canvas, "_mesh_warp_preview_id", None) == modifier.modifier_id
                    and getattr(canvas, "_effect_preview_channel", "canvas") in {"canvas", "overflow"})
    deferred = (projection_deferred(canvas) and large and scope is not None
                and not provisional and not navigator)
    if deferred:
        completed = projection_result_or_pending(canvas, scope, key)
        if completed is not None:
            return completed, False
    incoming, original = QImage(image), QImage(base)
    effect = copy.deepcopy(modifier)
    source_bounds, output_bounds = QRectF(bounds), QRectF(target)
    placement = QTransform(local_to_world)
    amount = np.array(_parameter_field(modifier, "intensity", modifier.intensity,
                                      (base.height(), base.width()), fields),
                      dtype=np.float32, copy=True) / 100.0
    preparation_cache = None
    if (exact or mesh_preview) and not deferred:
        preparation_cache = getattr(canvas, "_distort_preparation_cache", None)
        if preparation_cache is None:
            preparation_cache = canvas._distort_preparation_cache = PreparedDistortCache()

    def compute(cancelled=None, pixel_scale=1.0):
        cache = _worker_preparation_cache() if deferred else preparation_cache
        warped = render_distort(incoming, source_bounds, effect, placement,
                                output_bounds, cancelled, pixel_scale=pixel_scale,
                                preparation_cache=cache)
        if warped is None:
            return None
        blend_base, blend_amount = original, amount
        if warped.size() != original.size():
            blend_base = original.scaled(warped.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
            if amount.ndim == 2:
                xs = np.minimum((np.arange(warped.width()) * original.width() / warped.width()).astype(int), original.width() - 1)
                ys = np.minimum((np.arange(warped.height()) * original.height() / warped.height()).astype(int), original.height() - 1)
                blend_amount = amount[ys[:, None], xs[None, :]]
        if blend_amount.ndim == 0 and float(blend_amount) == 1.0:
            return warped
        if blend_amount.ndim == 2:
            blend_amount = blend_amount[..., None]
        return _premultiplied_qimage(_qimage_premultiplied(blend_base) * (1 - blend_amount)
                                     + _qimage_premultiplied(warped) * blend_amount)

    if mesh_preview:
        # Handle motion owns this temporary result. Do not enqueue a native
        # render for every intermediate mesh; release renders finished pixels.
        draft_key = ("distort-draft", key)
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            scale = min(1.0, 224 / max(base.width(), base.height()),
                        math.sqrt(32768 / (base.width() * base.height())))
            result = compute(pixel_scale=scale)
            canvas._modifier_cache_put(draft_key, result)
        return result.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True
    if deferred:
        # This closure owns Qt value copies and detached numeric fields. It
        # must never access the GUI-only prepared-source cache or canvas.
        canvas._effect_jobs.request(
            scope, key, compute,
            12 * int(incoming.sizeInBytes()) + 8 * int(original.sizeInBytes()) + amount.nbytes,
            allow_oversized=True, require_exact=True)
        raise ProjectionPending(scope, key)
    asynchronous = interactive and not exact and large and scope is not None and not provisional and not navigator
    if asynchronous:
        asynchronous = canvas._effect_jobs.request(
            scope, key, compute,
            12 * int(incoming.sizeInBytes()) + 8 * int(original.sizeInBytes()) + amount.nbytes,
            allow_oversized=True)
    if not exact and (asynchronous or (interactive and (navigator or provisional) and large)):
        draft_key = ("distort-draft", key)
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            scale = min(1.0, 224 / max(base.width(), base.height()),
                        math.sqrt(32768 / (base.width() * base.height())))
            result = compute(pixel_scale=scale)
            canvas._modifier_cache_put(draft_key, result)
        return result.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True
    return compute(), provisional
