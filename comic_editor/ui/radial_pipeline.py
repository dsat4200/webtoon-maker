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


def _live_radial_draft(canvas, incoming, base, modifier, fields, mapping, origin,
                       key, policy):
    """Current circular integration on independent temporary input/output grids.

    Full native source/effect frames and parameter fields are retained. The
    small input map uses actual resized dimensions; the output map describes
    centers over the complete original target frame. Neither rounding changes
    the captured native frame or the exact radial caller's mapping.
    """
    from PySide6.QtCore import Qt
    from comic_editor.ui.radial_blur import radial_blur, RadialRenderCancelled
    from comic_editor.render.service import RenderFailed
    import math
    draft_key = ("live-canvas-radial-integration-draft", policy.signature, key, int(incoming.cacheKey()))
    integrated_image = canvas._modifier_cache_get(draft_key)
    output_scale = policy.scale(base.width(), base.height())
    width, height = max(1, math.ceil(base.width()*output_scale)), max(1, math.ceil(base.height()*output_scale))
    xs = np.minimum(((np.arange(width)+.5)*base.width()/width).astype(int), base.width()-1)
    ys = np.minimum(((np.arange(height)+.5)*base.height()/height).astype(int), base.height()-1)
    def field(name, value):
        native = np.asarray(_parameter_field(modifier, name, value, (base.height(), base.width()), fields))
        return native[ys[:, None], xs[None, :]] if native.ndim == 2 else native
    if integrated_image is None:
        input_scale = min(1., 512/max(1, incoming.width(), incoming.height()))
        input_width, input_height = max(1, round(incoming.width()*input_scale)), max(1, round(incoming.height()*input_scale))
        small = incoming.scaled(input_width, input_height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        source_map = mapping * QTransform.fromScale(input_width/incoming.width(), input_height/incoming.height())
        inverse, valid = mapping.inverted()
        if not valid:
            raise RenderFailed("Current radial source mapping is singular")
        output_to_world = (QTransform.fromScale(base.width()/width, base.height()/height)
            * QTransform.fromTranslate(*origin) * inverse)
        angle = field("angle", modifier.angle)
        policy.check_cancelled()
        try:
            integrated = radial_blur(_qimage_premultiplied(small), tuple(modifier.center), angle,
                source_map, output_shape=(height, width), output_to_world=output_to_world,
                cancelled=policy.cancelled)
        except RadialRenderCancelled:
            policy.check_cancelled()
            raise
        policy.check_cancelled()
        integrated_image = _float_image(integrated)
        if integrated_image.isNull():
            raise RenderFailed("Current radial draft returned no integration")
        canvas._modifier_cache_put(draft_key, integrated_image)
    policy.check_cancelled()
    # The integration key intentionally omits intensity and its field.
    # Always blend the current base/amount; never cache that final mix.
    amount = field("intensity", modifier.intensity)/100
    if np.ndim(amount) == 2:
        amount = amount[..., None]
    small_base = base.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    result = _premultiplied_qimage(_qimage_premultiplied(small_base)*(1-amount)+_float_pixels(integrated_image)*amount)
    policy.check_cancelled()
    canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0)+1
    return result.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation), True


def render_radial_stage(canvas, incoming, base, modifier, fields, mapping, origin,
                        *, source_key, scope, asynchronous, deferred, provisional, navigator, exact):
    from comic_editor.ui.radial_blur import radial_blur
    from comic_editor.render.live_canvas_preview import check_live_canvas_cancelled
    policy = check_live_canvas_cancelled(canvas)

    shape = (base.height(), base.width())
    binding = modifier.parameter_masks.get("angle")
    angle_signature = (modifier.angle, repr(binding),
                       canvas._tone_mask_signature(binding.mask_id) if binding else ())
    key = ("radial-integration", source_key, tuple(modifier.center), angle_signature,
           canvas._modifier_mapping_signature(mapping), shape, origin)
    if policy is not None:
        return _live_radial_draft(canvas, incoming, base, modifier, fields, mapping, origin, key, policy)
    work_scope = ("radial-integration", scope) if scope is not None else None
    integrated = None if provisional else canvas._modifier_cache_get(key)
    if integrated is None and not provisional and work_scope is not None:
        integrated = (projection_result_or_pending(canvas, work_scope, key) if deferred
                      else canvas._effect_jobs.result(work_scope, key))
    if integrated is None:
        if (provisional or navigator) and canvas._interactive_render and not exact and policy is None:
            return base, True
        if policy is None and asynchronous:
            _cancel_obsolete_sampling(canvas._effect_jobs, work_scope, key)
        incoming = QImage(incoming)
        angle = np.array(_parameter_field(modifier, "angle", modifier.angle, shape, fields), copy=True)
        center = tuple(modifier.center)
        mapping = QTransform(mapping)

        # Exact inline scene work uses its already-owned scheduler token.
        # Deferred EffectJobs computations retain their existing job callback.
        scene_cancelled = (vars(canvas).get('_scene_cancelled')
                           if exact and not deferred else None)

        def compute(cancelled=None):
            if cancelled is None:
                cancelled = scene_cancelled
            return _float_image(radial_blur(_qimage_premultiplied(incoming), center, angle,
                mapping, output_shape=shape, output_origin=origin, cancelled=cancelled))

        if policy is None and asynchronous and canvas._effect_jobs.request(work_scope, key, compute,
                5*int(incoming.sizeInBytes())+10*int(base.sizeInBytes()),
                allow_oversized=True, require_exact=deferred):
            if deferred:
                raise ProjectionPending(work_scope, key)
            return base, True
        integrated = compute(policy.cancelled if policy is not None else None)
        if policy is not None:
            policy.check_cancelled()
        if not provisional:
            canvas._modifier_cache_put(key, integrated)
            if work_scope is not None and canvas._interactive_render:
                canvas._effect_jobs.retained_put(("result", work_scope), key, integrated)
    amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
    if amount.ndim == 2:
        amount = amount[..., None]
    return _premultiplied_qimage(_qimage_premultiplied(base)*(1-amount)+_float_pixels(integrated)*amount), provisional
