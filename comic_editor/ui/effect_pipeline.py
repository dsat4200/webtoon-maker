"""Stage-wise effect rendering with explicit image placement."""
import math
import copy
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.core.models import (ArrayModifier, MirrorModifier, RadialBlurModifier,
    CageTransformModifier, PosterizeModifier, HalftoneModifier, PixelateModifier,
    OutlineModifier)
from comic_editor.core.color_smoothing import simplify_padding
from comic_editor.core.effect_geometry import effect_bounds, reflection_transform, array_indices, array_transform, array_input_bounds
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied, _premultiplied_qimage, _parameter_field


def aligned(bounds):
    # Qt's integer rectangles wrap outside this range. Report an oversized
    # bake instead of producing a corrupt image after cumulative array scale.
    if any(not math.isfinite(v) or abs(v) > 1_000_000_000 for v in
           (bounds.left(), bounds.top(), bounds.right(), bounds.bottom())):
        raise ValueError("Effect bounds are too large to render. Reduce the count, scale offset, or spacing.")
    return QRectF(bounds.toAlignedRect())


def empty_image(bounds):
    width, height = max(1, math.ceil(bounds.width())), max(1, math.ceil(bounds.height()))
    if width * height > 64 * 1024 * 1024:
        raise ValueError("Effect bounds are too large to render. Reduce the effect or move its axis/center closer to the artwork.")
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise MemoryError("Could not allocate effect image")
    image.fill(Qt.transparent)
    return image


def _pattern_draft(source, modifier, fields, color_source):
    """Bound interactive fallback work while the exact CPU image is pending."""
    from comic_editor.ui.modifier_rendering import apply_pattern_modifier
    complex_pattern = isinstance(modifier, HalftoneModifier) and (
        modifier.grid_type in {"radial", "stippling"} or modifier.size > 1.5
        or modifier.dot_style in {"blob", "liquid", "delaunay"})
    edge, pixels = (96, 4096) if complex_pattern else (128, 8192)
    scale = min(1., edge / max(source.width(), source.height()),
                math.sqrt(pixels / (source.width() * source.height())))
    width, height = max(1, round(source.width() * scale)), max(1, round(source.height() * scale))
    image = source.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    colors = (color_source.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
              if color_source is not None else None)
    draft = copy.deepcopy(modifier)
    if isinstance(draft, PixelateModifier):
        draft.pixel_size *= scale
        draft.blur *= scale
    # Pattern masks currently bind intensity. Preserve their source-frame
    # coordinates in the draft; the worker retains the original full field.
    xs = np.minimum(((np.arange(width) + .5) * source.width() / width).astype(int), source.width() - 1)
    ys = np.minimum(((np.arange(height) + .5) * source.height() / height).astype(int), source.height() - 1)
    masks = {key: value[ys[:, None], xs[None, :]] for key, value in fields.items()
             if np.shape(value) == (source.height(), source.width())}
    result = apply_pattern_modifier(image, draft, masks, color_source=colors)
    return result


def _color_signature(canvas, modifier):
    if isinstance(modifier, HalftoneModifier) and modifier.color_mode == "target_layer":
        if getattr(canvas, "_rendering_halftone_source", False):
            return ("incoming-colors",)
        from comic_editor.ui.halftone_source import source_signature
        return source_signature(canvas, modifier.target_layer_id)
    return ()


def render_stages(canvas, image, bounds, modifiers, local_to_world, *, nearest=False,
                  required=None, request_scope=None, provisional=False, source_key=None):
    bounds = QRectF(bounds)
    initial_bounds = canvas._rect_signature(bounds)
    transform_signature = tuple(getattr(local_to_world, f"m{i}{j}")()
                                for i in range(1, 4) for j in range(1, 4))
    signatures = tuple((repr(modifier.to_dict()),
                        canvas._modifier_parameter_signature([modifier.modifier_id]),
                        _color_signature(canvas, modifier)) for modifier in modifiers)
    source_identity = source_key if source_key is not None else int(image.cacheKey())
    placement = (initial_bounds, transform_signature, nearest,
                 canvas._rect_signature(required) if required is not None else None)
    pipeline_key = ("stage-stack", source_identity, signatures, placement)
    navigator = (canvas._interactive_render
                 and getattr(canvas, "_effect_preview_channel", "canvas") == "navigator")
    checkpoint_scope = ("pipeline", request_scope)
    checkpointing = (request_scope is not None and canvas._interactive_render and not navigator
                     and not provisional and not canvas._render_base_alpha
                     and canvas._rendering_mask_contributor <= 0)
    start = 0
    inverse, valid = local_to_world.inverted()
    requirements = [None] * len(modifiers)
    if required is not None:
        needed = QRectF(required)
        for index in range(len(modifiers) - 1, -1, -1):
            requirements[index] = needed
            if isinstance(modifiers[index], (CageTransformModifier, HalftoneModifier, PixelateModifier)):
                # A displaced cage can pull source pixels from anywhere in the
                # incoming stage. Pattern effects also need the full frame:
                # cropping first changes their grid origin and reference scale.
                break
            if isinstance(modifiers[index], ArrayModifier) and not modifiers[index].muted:
                needed = array_input_bounds(needed, modifiers[index], local_to_world)
            else:
                needed = effect_bounds(needed, [modifiers[index]], local_to_world)
            if isinstance(modifiers[index], PosterizeModifier) and not modifiers[index].muted:
                padding = simplify_padding(modifiers[index])
                needed = needed.adjusted(-padding, -padding, padding, padding)
    if checkpointing:
        checkpoint = canvas._effect_jobs.retained_get(checkpoint_scope, pipeline_key)
        if checkpoint is not None:
            image, (start, bounds) = checkpoint
            bounds = QRectF(bounds)
        else:
            canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (0, QRectF(bounds)))
    for index, modifier in enumerate(modifiers):
        if index < start:
            continue
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, RadialBlurModifier) and modifier.angle <= 0 and "angle" not in modifier.parameter_masks:
            continue
        target = effect_bounds(bounds, [modifier], local_to_world)
        if requirements[index] is not None:
            target = target.intersected(requirements[index])
        target = aligned(target)
        if target.isEmpty():
            image, bounds = empty_image(QRectF(0, 0, 1, 1)), target
            continue
        # A semantic capture key survives source-LRU eviction. Only the exact
        # upstream prefix contributes, so editing a later slider still reuses
        # every earlier stage.
        upstream_key = ("stage-input", source_identity, signatures[:index], placement) if source_key is not None else int(image.cacheKey())
        key = ("stage", upstream_key, canvas._rect_signature(bounds),
               canvas._rect_signature(target),
               signatures[index][0], signatures[index][1],
               tuple(local_to_world.map(bounds.topLeft()).toTuple()), nearest, signatures[index][2],
               transform_signature)
        stage_scope = (*request_scope, modifier.modifier_id) if request_scope is not None else None
        cached = None if provisional else canvas._modifier_cache_get(key)
        if cached is None and not provisional and stage_scope is not None:
            cached = canvas._effect_jobs.result(stage_scope, key)
        if cached is None:
            work_target = target
            pattern = isinstance(modifier, (HalftoneModifier, PixelateModifier))
            if pattern:
                work_target = bounds
            outline = isinstance(modifier, OutlineModifier)
            if outline:
                # Width, opacity, color and viewport edits share the same alpha
                # source and exact distance field. Crop only the finished stage;
                # changing its input padding would force another distance build.
                work_target = aligned(bounds.adjusted(-25, -25, 25, 25))
            if isinstance(modifier, PosterizeModifier):
                padding = simplify_padding(modifier)
                work_target = aligned(target.adjusted(-padding, -padding, padding, padding).intersected(bounds))
            # Keep the upstream image identity for cached GPU uploads and blur
            # passes when a pattern slider changes.
            source = image if pattern else None
            if outline:
                padding_key = ("outline-stage-source", upstream_key,
                              canvas._rect_signature(bounds),
                              canvas._rect_signature(work_target))
                source = None if provisional else canvas._modifier_source_cache_get(padding_key)
                if source is None:
                    source = empty_image(work_target)
                    painter = QPainter(source)
                    painter.drawImage(bounds.topLeft() - work_target.topLeft(), image)
                    painter.end()
                    if not provisional:
                        canvas._modifier_source_cache_put(padding_key, source)
            if source is None:
                source = empty_image(work_target)
            mapping = canvas._world_to_image_transform(local_to_world, work_target, source.width(), source.height())
            fields = canvas._modifier_mask_fields([modifier], source.width(), source.height(), mapping, local_to_world.mapRect(work_target))
            if pattern:
                from comic_editor.ui.gpu_pattern_effects import renderer_for
                from comic_editor.ui.modifier_rendering import apply_pattern_modifier
                from comic_editor.ui.halftone_source import render_color_source
                revision = getattr(canvas, "_effect_provisional_revision", 0)
                color_source = (render_color_source(canvas, modifier, source, work_target, local_to_world)
                                if isinstance(modifier, HalftoneModifier) else None)
                provisional |= getattr(canvas, "_effect_provisional_revision", 0) != revision
                cached = apply_pattern_modifier(source, modifier, fields, renderer_for(canvas),
                                                color_source=color_source, allow_cpu_fallback=False)
                if cached is None:
                    asynchronous = (request_scope is not None and canvas._interactive_render
                        and not canvas._render_base_alpha
                        and canvas._rendering_mask_contributor <= 0 and not provisional
                        and not navigator
                        and source.width() * source.height() > 128 * 128)
                    if asynchronous:
                        # QImage copies are detached automatically on later UI
                        # writes; mutable models and NumPy mask fields must be
                        # copied explicitly before a worker can inspect them.
                        incoming, effect = QImage(source), copy.deepcopy(modifier)
                        colors = QImage(color_source) if color_source is not None else None
                        masks = {name: np.array(field, copy=True) for name, field in fields.items()}
                        crop = None
                        if work_target != target:
                            crop = QRectF(target)
                            crop.translate(-work_target.topLeft())
                            crop = crop.toAlignedRect()
                        def compute(cancelled=None, incoming=incoming, effect=effect,
                                    colors=colors, masks=masks, crop=crop):
                            result = apply_pattern_modifier(incoming, effect, masks,
                                color_source=colors, cancelled=cancelled)
                            return result.copy(crop) if crop is not None else result
                        # Includes working arrays used by the full-image CPU
                        # fallback, not only the retained RGBA8 input images.
                        size = (56 * int(incoming.sizeInBytes())
                                + (int(colors.sizeInBytes()) if colors is not None else 0)
                                + sum(field.nbytes for field in masks.values()))
                        asynchronous = canvas._effect_jobs.request(
                            (*request_scope, modifier.modifier_id), key, compute, size,
                            allow_oversized=True)
                    if asynchronous or ((navigator or provisional) and canvas._interactive_render):
                        draft_key = ("pattern-draft", key)
                        cached = canvas._modifier_cache_get(draft_key)
                        if cached is None:
                            cached = _pattern_draft(source, modifier, fields, color_source)
                            canvas._modifier_cache_put(draft_key, cached)
                        cached = cached.scaled(source.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                        provisional = True
                    else:
                        cached = apply_pattern_modifier(source, modifier, fields,
                                                        color_source=color_source)
                if work_target != target:
                    cropped = QRectF(target)
                    cropped.translate(-work_target.topLeft())
                    cached = cached.copy(cropped.toAlignedRect())
            elif isinstance(modifier, CageTransformModifier) and valid:
                from comic_editor.ui.cage_rendering import warp_image
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft()-target.topLeft(), image)
                painter.end()
                shape = (source.height(), source.width())
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
                if amount.ndim == 2:
                    amount = amount[..., None]
                incoming, base, cage = QImage(image), QImage(source), copy.deepcopy(modifier)
                source_bounds, output_bounds, placement = QRectF(bounds), QRectF(target), QTransform(local_to_world)
                def compute(cancelled=None, pixel_scale=1., incoming=incoming, base=base, cage=cage,
                            source_bounds=source_bounds, output_bounds=output_bounds, placement=placement, amount=amount):
                    result = warp_image(incoming, source_bounds, cage, placement, output_bounds, cancelled, pixel_scale=pixel_scale)
                    if result is None:
                        return None
                    warped = result[0]
                    blend_base, blend_amount = base, amount
                    if warped.size() != base.size():
                        if pixel_scale == 1.:
                            warped = warped.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                        else:
                            blend_base = base.scaled(warped.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                            if np.ndim(amount) >= 2:
                                xs = np.minimum((np.arange(warped.width()) * base.width() / warped.width()).astype(int), base.width() - 1)
                                ys = np.minimum((np.arange(warped.height()) * base.height() / warped.height()).astype(int), base.height() - 1)
                                blend_amount = amount[ys[:, None], xs[None, :]]
                    if np.ndim(blend_amount) == 0 and float(blend_amount) == 1.:
                        return warped
                    return _premultiplied_qimage(_qimage_premultiplied(blend_base)*(1-blend_amount)+_qimage_premultiplied(warped)*blend_amount)
                asynchronous = (request_scope is not None and canvas._interactive_render
                    and not canvas._render_base_alpha
                    and canvas._rendering_mask_contributor <= 0 and not provisional
                    and not navigator
                    and source.width()*source.height() > 128*128)
                from comic_editor.ui.gpu_textures import renderer_for
                gpu = renderer_for(canvas)
                warped = gpu.cage(incoming, source_bounds, cage, placement, output_bounds) if gpu is not None else None
                if warped is not None:
                    cached = warped if np.ndim(amount) == 0 and float(amount) == 1. else _premultiplied_qimage(
                        _qimage_premultiplied(base)*(1-amount)+_qimage_premultiplied(warped)*amount)
                elif ((asynchronous and canvas._effect_jobs.request(
                    (*request_scope, modifier.modifier_id), key, compute,
                    10*int(incoming.sizeInBytes())+4*int(source.sizeInBytes()),
                    allow_oversized=True)) or ((navigator or provisional) and canvas._interactive_render)):
                    draft_key = ("cage-draft", key)
                    cached = canvas._modifier_cache_get(draft_key)
                    if cached is None:
                        cached = compute(pixel_scale=min(1., 192/max(source.width(), source.height())))
                        canvas._modifier_cache_put(draft_key, cached)
                    cached = cached.scaled(source.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                    provisional = True
                else:
                    cached = compute()
            elif isinstance(modifier, ArrayModifier) and valid:
                painter = QPainter(source)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
                painter.setRenderHint(QPainter.Antialiasing, not nearest)
                try:
                    for step in array_indices(modifier):
                        transform = local_to_world * array_transform(modifier, step) * inverse
                        if not transform.mapRect(bounds).intersects(target):
                            continue
                        painter.setTransform(QTransform.fromTranslate(bounds.left(), bounds.top())
                            * transform * QTransform.fromTranslate(-target.left(), -target.top()))
                        painter.drawImage(0, 0, image)
                finally:
                    painter.end()
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity,
                    (source.height(), source.width()), fields)) / 100
                if amount.ndim == 2:
                    amount = amount[..., None]
                cached = source if np.ndim(amount) == 0 and float(amount) == 1. else _premultiplied_qimage(
                    _qimage_premultiplied(source) * amount)
                painter = QPainter(cached)
                painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                painter.end()
            elif isinstance(modifier, MirrorModifier) and valid:
                painter = QPainter(source)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
                painter.setRenderHint(QPainter.Antialiasing, not nearest)
                reflection = local_to_world * reflection_transform(modifier) * inverse
                painter.setTransform(QTransform.fromTranslate(bounds.left(), bounds.top()) * reflection * QTransform.fromTranslate(-target.left(), -target.top()))
                painter.drawImage(0, 0, image)
                painter.end()
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, (source.height(), source.width()), fields)) / 100
                if amount.ndim == 2:
                    amount = amount[..., None]
                cached = _premultiplied_qimage(_qimage_premultiplied(source) * amount)
                painter = QPainter(cached)
                painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                painter.end()
            elif isinstance(modifier, RadialBlurModifier):
                from comic_editor.ui.radial_blur import radial_blur
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft()-target.topLeft(), image)
                painter.end()
                shape = (source.height(), source.width())
                angle = _parameter_field(modifier, "angle", modifier.angle, shape, fields)
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
                if amount.ndim == 2:
                    amount = amount[..., None]
                # Keep the complete incoming image: rotations can pull content
                # from outside the requested output tile/viewport.
                incoming, base = QImage(image), QImage(source)
                center = tuple(modifier.center)
                image_mapping = canvas._world_to_image_transform(local_to_world, bounds, image.width(), image.height())
                origin = (target.x()-bounds.x(), target.y()-bounds.y())
                def compute(cancelled=None, incoming=incoming, base=base, center=center,
                            angle=angle, amount=amount, image_mapping=image_mapping,
                            shape=shape, origin=origin):
                    effect = radial_blur(_qimage_premultiplied(incoming), center, angle,
                        image_mapping, output_shape=shape, output_origin=origin, cancelled=cancelled)
                    return _premultiplied_qimage(_qimage_premultiplied(base)*(1-amount)+effect*amount)
                asynchronous = (
                    request_scope is not None and canvas._interactive_render
                    and not canvas._render_base_alpha
                    and canvas._rendering_mask_contributor <= 0
                    and not provisional
                    and not navigator
                )
                if asynchronous and canvas._effect_jobs.request(
                    (*request_scope, modifier.modifier_id), key, compute,
                    5*int(incoming.sizeInBytes())+10*int(source.sizeInBytes()),
                    allow_oversized=True,
                ):
                    cached, provisional = source, True
                elif (provisional or navigator) and canvas._interactive_render:
                    cached, provisional = source, True
                else:
                    cached = compute()
            else:
                if not outline:
                    painter = QPainter(source)
                    painter.drawImage(bounds.topLeft() - work_target.topLeft(), image)
                    painter.end()
                if request_scope is not None or provisional or navigator:
                    from comic_editor.ui.interactive_effects import render_interactive_stack
                    work_key = ("stage-work", key) if work_target != target else key
                    cached, provisional = render_interactive_stack(
                        canvas, source, [modifier], local_to_world.map(work_target.topLeft()).toTuple(), fields,
                        cache_key=work_key, scope=(*(request_scope or ("provisional-stage",)), modifier.modifier_id),
                        world_to_image=mapping, nearest=nearest, upstream_provisional=provisional)
                else:
                    cached = apply_modifier_stack(
                        source, [modifier], local_to_world.map(work_target.topLeft()).toTuple(), fields,
                        world_to_image=mapping, nearest=nearest,
                        outline_distance_cache=canvas._outline_distance_cache,
                        blur_pyramid_cache=canvas._blur_pyramid_cache,
                    )
                if work_target != target:
                    cropped = QRectF(target)
                    cropped.translate(-work_target.topLeft())
                    cached = cached.copy(cropped.toAlignedRect())
            if not provisional:
                canvas._modifier_cache_put(key, cached)
        image, bounds = cached, target
        if checkpointing and not provisional:
            canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (index + 1, QRectF(bounds)))
            canvas._effect_jobs.retained_remove(("result", stage_scope), key)
            canvas._effect_jobs.retained_remove(("result", stage_scope), ("stage-work", key))
    if checkpointing and not provisional:
        canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (len(modifiers), QRectF(bounds)))
    if provisional:
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
    return image, bounds
