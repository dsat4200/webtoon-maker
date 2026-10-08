"""Reuse exact circular integration independently of intensity and its mask."""
import numpy as np
from PySide6.QtGui import QImage, QTransform

from comic_editor.ui.async_projection import ProjectionPending, projection_result_or_pending
from comic_editor.ui.modifier_rendering import (
    _parameter_field, _premultiplied_qimage, _qimage_premultiplied,
)


def _float_image(pixels):
    pixels = np.ascontiguousarray(pixels, dtype=np.float32)
    height, width = pixels.shape[:2]
    return QImage(pixels.data, width, height, pixels.strides[0],
                  QImage.Format_RGBA32FPx4_Premultiplied).copy()


def _float_pixels(image):
    # Keep the integration at float precision until the *final* blend. An
    # intermediate RGBA8 image would round twice and change saved artwork.
    rows = np.frombuffer(image.constBits(), dtype=np.float32,
                         count=image.sizeInBytes() // 4).reshape(image.height(), image.bytesPerLine() // 4)
    return rows[:, :image.width()*4].reshape(image.height(), image.width(), 4)


def _cancel_obsolete_sampling(jobs, scope, key):
    # A new angle/center can change swept bounds and therefore the regional
    # scope. Retire those old samples too, without canceling sibling regions
    # at the current settings or an intensity edit using the same integration.
    def owner(work_scope):
        if not work_scope or work_scope[0] != "radial-integration":
            return None
        region = work_scope[1]
        if region and isinstance(region[-1], tuple) and region[-1][:1] == ("effect-region",):
            return region[:-1]
        return region

    def obsolete(job):
        return (owner(job[0]) == owner(scope) and job[0] != scope
                and job[1][0] == "radial-integration" and job[1][2:4] != key[2:4])

    for job in jobs.running_jobs:
        if obsolete(job):
            job[2].set()
    for pending_scope, job in tuple(jobs.pending.items()):
        if obsolete(job):
            jobs.pending.pop(pending_scope)


def render_radial_stage(canvas, incoming, base, modifier, fields, mapping, origin,
                        *, source_key, scope, asynchronous, deferred, provisional, navigator, exact):
    from comic_editor.ui.radial_blur import radial_blur

    shape = (base.height(), base.width())
    binding = modifier.parameter_masks.get("angle")
    angle_signature = (modifier.angle, repr(binding),
                       canvas._tone_mask_signature(binding.mask_id) if binding else ())
    key = ("radial-integration", source_key, tuple(modifier.center), angle_signature,
           canvas._modifier_mapping_signature(mapping), shape, origin)
    work_scope = ("radial-integration", scope) if scope is not None else None
    integrated = None if provisional else canvas._modifier_cache_get(key)
    if integrated is None and not provisional and work_scope is not None:
        integrated = (projection_result_or_pending(canvas, work_scope, key) if deferred
                      else canvas._effect_jobs.result(work_scope, key))
    if integrated is None:
        if (provisional or navigator) and canvas._interactive_render and not exact:
            return base, True
        if asynchronous:
            _cancel_obsolete_sampling(canvas._effect_jobs, work_scope, key)
        incoming = QImage(incoming)
        angle = np.array(_parameter_field(modifier, "angle", modifier.angle, shape, fields), copy=True)
        center = tuple(modifier.center)
        mapping = QTransform(mapping)

        def compute(cancelled=None):
            return _float_image(radial_blur(_qimage_premultiplied(incoming), center, angle,
                mapping, output_shape=shape, output_origin=origin, cancelled=cancelled))

        if asynchronous and canvas._effect_jobs.request(work_scope, key, compute,
                5*int(incoming.sizeInBytes())+10*int(base.sizeInBytes()),
                allow_oversized=True, require_exact=deferred):
            if deferred:
                raise ProjectionPending(work_scope, key)
            return base, True
        integrated = compute()
        if not provisional:
            canvas._modifier_cache_put(key, integrated)
            if work_scope is not None and canvas._interactive_render:
                canvas._effect_jobs.retained_put(("result", work_scope), key, integrated)
    amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
    if amount.ndim == 2:
        amount = amount[..., None]
    return _premultiplied_qimage(_qimage_premultiplied(base)*(1-amount)+_float_pixels(integrated)*amount), provisional
