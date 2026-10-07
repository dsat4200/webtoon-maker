"""Reuse circular integration independently of the fresh intensity-mask mix."""
import hashlib

import numpy as np
from PySide6.QtGui import QImage, QTransform

from comic_editor.ui.async_projection import ProjectionPending, projection_result_or_pending
from comic_editor.ui.modifier_rendering import (
    _parameter_field, _premultiplied_qimage, _qimage_premultiplied,
)


_PREVIEW_INPUT_BYTES = 1024 * 1024
_PREVIEW_ANGLE_BYTES = 256 * 1024
_PREVIEW_OUTPUT_BYTES = 1024 * 1024
_PREVIEW_FORMATS = frozenset((QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA8888_Premultiplied, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4_Premultiplied))


def _preview_integration_key(canvas, key, incoming, angle, mapping, shape):
    """Identify only small current provisional pixels, including unsignaled edits.

    Model/source keys alone cannot identify a derived live input or angle field:
    either can improve without a model revision. Read their current native bytes
    within fixed bounds and retain the existing integration dependencies too.
    """
    row_bytes = incoming.width() * incoming.depth() // 8
    if (incoming.isNull() or incoming.format() not in _PREVIEW_FORMATS
            or row_bytes * incoming.height() > _PREVIEW_INPUT_BYTES
            or shape[0] <= 0 or shape[1] <= 0
            or shape[0] * shape[1] * 16 > _PREVIEW_OUTPUT_BYTES
            or angle.nbytes > _PREVIEW_ANGLE_BYTES or not angle.flags.c_contiguous
            or angle.dtype.kind not in 'fbiu' or angle.shape not in ((), shape)):
        return None
    from comic_editor.render.pixels import current_contract
    from comic_editor.render.source_context import source_color_context

    source = hashlib.sha256()
    data = incoming.constBits()
    for row in range(incoming.height()):
        left = row * incoming.bytesPerLine()
        source.update(data[left:left + row_bytes])
    field = hashlib.sha256(memoryview(angle).cast('B')).digest()
    contract = current_contract()
    actual = canvas.chapter.pixel_contract
    return ('radial-integration-preview', key,
        (incoming.width(), incoming.height(), incoming.format().value, source.digest()),
        (angle.dtype.str, angle.shape, field),
        tuple(getattr(mapping, f'm{i}{j}')() for i in range(1, 4) for j in range(1, 4)),
        contract.signature, actual.signature, source_color_context(actual, contract))


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
                        *, source_key, scope, asynchronous, deferred, provisional, navigator, exact,
                        bounded_preview=False):
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
        if ((provisional or navigator) and canvas._interactive_render and not exact
                and not bounded_preview):
            return base, True
        if asynchronous:
            _cancel_obsolete_sampling(canvas._effect_jobs, work_scope, key)
        incoming = QImage(incoming)
        angle = np.array(_parameter_field(modifier, "angle", modifier.angle, shape, fields), copy=True)
        center = tuple(modifier.center)
        mapping = QTransform(mapping)
        preview_key = (_preview_integration_key(canvas, key, incoming, angle, mapping, shape)
            if (bounded_preview and provisional and not exact and not navigator
                and canvas._interactive_render and not asynchronous and not deferred) else None)
        if preview_key is not None:
            integrated = canvas._modifier_cache_get(preview_key)

        def compute(cancelled=None):
            return _float_image(radial_blur(_qimage_premultiplied(incoming), center, angle,
                mapping, output_shape=shape, output_origin=origin, cancelled=cancelled))

        if asynchronous and canvas._effect_jobs.request(work_scope, key, compute,
                5*int(incoming.sizeInBytes())+10*int(base.sizeInBytes()),
                allow_oversized=True, require_exact=deferred):
            if deferred:
                raise ProjectionPending(work_scope, key)
            return base, True
        if integrated is None:
            integrated = compute()
            if preview_key is not None:
                # The ordinary LRU owns the same float32 integration. The
                # explicit preview namespace is rejected by durable admission.
                canvas._modifier_cache_put(preview_key, integrated)
        if not provisional:
            canvas._modifier_cache_put(key, integrated)
            if work_scope is not None and canvas._interactive_render:
                canvas._effect_jobs.retained_put(("result", work_scope), key, integrated)
    amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
    if amount.ndim == 2:
        amount = amount[..., None]
    return _premultiplied_qimage(_qimage_premultiplied(base)*(1-amount)+_float_pixels(integrated)*amount), provisional
