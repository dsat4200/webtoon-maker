"""Cached, cancellable distortion stages with a bounded interactive preview."""
import copy
import math

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QTransform

from comic_editor.ui.modifier_rendering import (
    _parameter_field, _premultiplied_qimage, _qimage_premultiplied,
)


def render_distort_stage(canvas, image, base, bounds, target, modifier,
                         local_to_world, fields, key, scope, provisional, navigator):
    from comic_editor.ui.distort_rendering import render_distort

    incoming, original = QImage(image), QImage(base)
    effect = copy.deepcopy(modifier)
    source_bounds, output_bounds = QRectF(bounds), QRectF(target)
    placement = QTransform(local_to_world)
    amount = np.array(_parameter_field(modifier, "intensity", modifier.intensity,
                                      (base.height(), base.width()), fields),
                      dtype=np.float32, copy=True) / 100.0

    def compute(cancelled=None, pixel_scale=1.0):
        warped = render_distort(incoming, source_bounds, effect, placement,
                                output_bounds, cancelled, pixel_scale=pixel_scale)
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

    interactive = (canvas._interactive_render and not canvas._render_base_alpha
                   and canvas._rendering_mask_contributor <= 0)
    large = max(image.width() * image.height(), base.width() * base.height()) > 128 * 128
    asynchronous = interactive and large and scope is not None and not provisional and not navigator
    if asynchronous:
        asynchronous = canvas._effect_jobs.request(
            scope, key, compute,
            12 * int(incoming.sizeInBytes()) + 8 * int(original.sizeInBytes()) + amount.nbytes,
            allow_oversized=True)
    if asynchronous or (interactive and (navigator or provisional) and large):
        draft_key = ("distort-draft", key)
        result = canvas._modifier_cache_get(draft_key)
        if result is None:
            scale = min(1.0, 224 / max(base.width(), base.height()),
                        math.sqrt(32768 / (base.width() * base.height())))
            result = compute(pixel_scale=scale)
            canvas._modifier_cache_put(draft_key, result)
        return result.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True
    return compute(), provisional
