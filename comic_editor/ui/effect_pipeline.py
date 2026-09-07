"""Stage-wise effect rendering with explicit image placement."""
import math
import copy
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.core.models import MirrorModifier, RadialBlurModifier, CageTransformModifier, PosterizeModifier
from comic_editor.core.color_smoothing import simplify_padding
from comic_editor.core.effect_geometry import effect_bounds, reflection_transform
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied, _premultiplied_qimage, _parameter_field


def aligned(bounds):
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


def render_stages(canvas, image, bounds, modifiers, local_to_world, *, nearest=False, required=None, request_scope=None):
    bounds = QRectF(bounds)
    inverse, valid = local_to_world.inverted()
    requirements = [None] * len(modifiers)
    if required is not None:
        needed = QRectF(required)
        for index in range(len(modifiers) - 1, -1, -1):
            requirements[index] = needed
            if isinstance(modifiers[index], CageTransformModifier):
                # A displaced cage can pull source pixels from anywhere in the
                # incoming stage, including completely outside this viewport.
                break
            needed = effect_bounds(needed, [modifiers[index]], local_to_world)
            if isinstance(modifiers[index], PosterizeModifier) and not modifiers[index].muted:
                padding = simplify_padding(modifiers[index])
                needed = needed.adjusted(-padding, -padding, padding, padding)
    provisional = False
    for index, modifier in enumerate(modifiers):
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, RadialBlurModifier) and modifier.angle <= 0 and "angle" not in modifier.parameter_masks:
            continue
        target = aligned(effect_bounds(bounds, [modifier], local_to_world))
        if requirements[index] is not None:
            target = aligned(target.intersected(requirements[index]))
        if target.isEmpty():
            image, bounds = empty_image(QRectF(0, 0, 1, 1)), target
            continue
        key = ("stage", int(image.cacheKey()), canvas._rect_signature(bounds),
               canvas._rect_signature(target),
               repr(modifier.to_dict()), canvas._modifier_parameter_signature([modifier.modifier_id]),
               tuple(local_to_world.map(bounds.topLeft()).toTuple()), nearest,
               tuple(getattr(local_to_world, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)))
        cached = canvas._modifier_cache_get(key)
        if cached is None:
            work_target = target
            if isinstance(modifier, PosterizeModifier):
                padding = simplify_padding(modifier)
                work_target = aligned(target.adjusted(-padding, -padding, padding, padding).intersected(bounds))
            source = empty_image(work_target)
            mapping = canvas._world_to_image_transform(local_to_world, work_target, source.width(), source.height())
            fields = canvas._modifier_mask_fields([modifier], source.width(), source.height(), mapping, local_to_world.mapRect(work_target))
            if isinstance(modifier, CageTransformModifier) and valid:
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
                    if warped.size() != base.size():
                        warped = warped.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                    if np.ndim(amount) == 0 and float(amount) == 1.:
                        return warped
                    return _premultiplied_qimage(_qimage_premultiplied(base)*(1-amount)+_qimage_premultiplied(warped)*amount)
                asynchronous = (request_scope is not None and canvas._interactive_render
                    and not canvas._render_base_alpha and not canvas._render_modifier_sources
                    and canvas._rendering_mask_contributor <= 0 and not provisional
                    and source.width()*source.height() > 128*128)
                from comic_editor.ui.gpu_textures import renderer_for
                gpu = renderer_for(canvas)
                warped = gpu.cage(incoming, source_bounds, cage, placement, output_bounds) if gpu is not None else None
                if warped is not None:
                    cached = warped if np.ndim(amount) == 0 and float(amount) == 1. else _premultiplied_qimage(
                        _qimage_premultiplied(base)*(1-amount)+_qimage_premultiplied(warped)*amount)
                elif asynchronous and canvas._effect_jobs.request(
                    (*request_scope, modifier.modifier_id), key, compute,
                    10*int(incoming.sizeInBytes())+4*int(source.sizeInBytes())):
                    draft_key = ("cage-draft", key)
                    cached = canvas._modifier_cache_get(draft_key)
                    if cached is None:
                        cached = compute(pixel_scale=min(1., 192/max(source.width(), source.height())))
                        canvas._modifier_cache_put(draft_key, cached)
                    provisional = True
                else:
                    cached = compute()
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
                    and not canvas._render_base_alpha and not canvas._render_modifier_sources
                    and canvas._rendering_mask_contributor <= 0
                    and not provisional
                )
                if asynchronous and canvas._effect_jobs.request(
                    (*request_scope, modifier.modifier_id), key, compute,
                    5*int(incoming.sizeInBytes())+10*int(source.sizeInBytes()),
                ):
                    cached, provisional = source, True
                else:
                    cached = compute()
            else:
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft() - work_target.topLeft(), image)
                painter.end()
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
    return image, bounds
