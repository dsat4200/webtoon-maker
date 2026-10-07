"""Cached, cancellable distortion stages with a bounded interactive preview."""
import copy
import math
from threading import Lock, local
import weakref

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
from comic_editor.render.pixels import current_contract


_worker_preparation = None
_worker_preparation_lock = Lock()
_worker_local = local()


_BASE_FREE_TYPES = {
    'distort_twirl', 'distort_deform', 'distort_mesh_warp',
    'distort_lens_distortion', 'distort_pinch_punch',
}


def can_omit_deferred_base(canvas, image, target, modifier, scope, provisional, navigator):
    """Only a full-strength exact CPU warp has no padded blend dependency."""
    return bool(projection_requires_exact(canvas) and projection_deferred(canvas)
                and scope is not None and not provisional and not navigator
                and not image.isNull()
                and modifier.modifier_type in _BASE_FREE_TYPES
                and modifier.intensity == 100 and not modifier.parameter_masks
                and max(image.width() * image.height(),
                        target.width() * target.height()) > 128 * 128)


def _native_frame_bytes(width, height):
    # Use Qt's current native format and 32-bit row alignment, including
    # float16/float32 contracts. This reserves the same padded output working
    # memory even when the unused blend image is never allocated.
    probe = QImage(1, 1, current_contract().image_format)
    if probe.isNull():
        raise MemoryError('Could not inspect the native effect image format')
    return ((width * probe.depth() + 31) // 32) * 4 * height


def _worker_preparation_cache():
    """One bounded immutable cache shared by live executor threads.

    The cache only owns immutable numerical snapshots keyed by source-image
    revision. It never retains a canvas, model, or GUI-owned cache.
    """
    global _worker_preparation
    cache = getattr(_worker_local, "cache", None)
    if cache is None:
        with _worker_preparation_lock:
            cache = _worker_preparation() if _worker_preparation is not None else None
            if cache is None:
                from comic_editor.ui.distort_rendering import PreparedDistortCache
                cache = PreparedDistortCache()
                _worker_preparation = weakref.ref(cache)
            # The registry is weak; the last executor thread releases its
            # cache on exit, including when a canvas closes without app exit.
            _worker_local.cache = cache
    return cache


def render_distort_stage(canvas, image, base, bounds, target, modifier,
                         local_to_world, fields, key, scope, provisional, navigator,
                         *, base_size=None):
    exact = projection_requires_exact(canvas)
    if (exact and provisional and getattr(canvas, '_projection_defer_effects', False)
            and canvas._interactive_render):
        raise ProjectionPending(scope, key)
    from comic_editor.ui.distort_rendering import PreparedDistortCache, render_distort

    if base is None:
        if not can_omit_deferred_base(canvas, image, target, modifier, scope, provisional, navigator):
            raise ValueError('An omitted distortion base requires deferred full-strength exact work')
        expected_size = (max(1, math.ceil(target.width())), max(1, math.ceil(target.height())))
        if base_size != expected_size or base_size[0] * base_size[1] > 64 * 1024 * 1024:
            raise ValueError('Invalid native distortion base dimensions')
        base_width, base_height = base_size
        base_bytes = _native_frame_bytes(base_width, base_height)
    else:
        base_width, base_height = base.width(), base.height()
        base_bytes = int(base.sizeInBytes())
    large = max(image.width() * image.height(), base_width * base_height) > 128 * 128
    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    contact = (not exact and interactive
               and bool(getattr(canvas, "_stroke_projection_active", False)))
    mesh_preview = (not exact and interactive and not navigator
                    and ((modifier.modifier_type == "distort_mesh_warp"
                          and getattr(canvas, "_mesh_warp_preview_id", None) == modifier.modifier_id)
                         or (modifier.modifier_type == "distort_smudge"
                             and getattr(canvas, "_smudge_preview_id", None) == modifier.modifier_id))
                    and getattr(canvas, "_effect_preview_channel", "canvas") in {"canvas", "overflow"})
    deferred = (projection_deferred(canvas) and large and scope is not None
                and not provisional and not navigator)
    if deferred:
        completed = projection_result_or_pending(canvas, scope, key)
        if completed is not None:
            return completed, False
    incoming, original = QImage(image), QImage(base) if base is not None else None
    effect = copy.deepcopy(modifier)
    source_bounds, output_bounds = QRectF(bounds), QRectF(target)
    placement = QTransform(local_to_world)
    amount = np.array(_parameter_field(modifier, "intensity", modifier.intensity,
                                       (base_height, base_width), fields),
                      dtype=np.float32, copy=True)
    amount /= 100.0
    if original is None and (amount.ndim != 0 or float(amount) != 1.0):
        raise ValueError('An omitted distortion base cannot blend a parameter field')
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
        if original is None:
            return warped
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
            12 * int(incoming.sizeInBytes()) + 8 * base_bytes + amount.nbytes,
            allow_oversized=True, require_exact=True)
        raise ProjectionPending(scope, key)
    asynchronous = (interactive and not exact and not contact and large
                    and scope is not None and not provisional and not navigator)
    if asynchronous:
        asynchronous = canvas._effect_jobs.request(
            scope, key, compute,
            12 * int(incoming.sizeInBytes()) + 8 * int(original.sizeInBytes()) + amount.nbytes,
            allow_oversized=True)
    if not exact and (asynchronous or (interactive and (navigator or provisional or contact) and large)):
        draft_key = ("contact-distort-draft" if contact else "distort-draft", key)
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            edge, pixels = (96, 4096) if contact else (224, 32768)
            scale = min(1.0, edge / max(base.width(), base.height()),
                        math.sqrt(pixels / (base.width() * base.height())))
            result = compute(pixel_scale=scale)
            canvas._modifier_cache_put(draft_key, result)
        return result.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True
    return compute(), provisional
