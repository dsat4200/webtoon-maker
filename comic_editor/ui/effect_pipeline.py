"""Stage-wise effect rendering with explicit image placement."""
import math
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.core.models import MirrorModifier
from comic_editor.core.effect_geometry import effect_bounds, reflection_transform
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied, _premultiplied_qimage, _parameter_field


def aligned(bounds):
    return QRectF(bounds.toAlignedRect())


def empty_image(bounds):
    width, height = max(1, math.ceil(bounds.width())), max(1, math.ceil(bounds.height()))
    if width * height > 64 * 1024 * 1024:
        raise ValueError("Effect bounds are too large to render. Move the mirror axis closer to the artwork.")
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    if image.isNull():
        raise MemoryError("Could not allocate effect image")
    image.fill(Qt.transparent)
    return image


def render_stages(canvas, image, bounds, modifiers, local_to_world, *, nearest=False, required=None):
    bounds = QRectF(bounds)
    inverse, valid = local_to_world.inverted()
    requirements = [None] * len(modifiers)
    if required is not None:
        needed = QRectF(required)
        for index in range(len(modifiers) - 1, -1, -1):
            requirements[index] = needed
            needed = effect_bounds(needed, [modifiers[index]], local_to_world)
    for index, modifier in enumerate(modifiers):
        if modifier.muted:
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
            source = empty_image(target)
            mapping = canvas._world_to_image_transform(local_to_world, target, source.width(), source.height())
            fields = canvas._modifier_mask_fields([modifier], source.width(), source.height(), mapping, local_to_world.mapRect(target))
            if isinstance(modifier, MirrorModifier) and valid:
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
            else:
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                painter.end()
                cached = apply_modifier_stack(
                    source, [modifier], local_to_world.map(target.topLeft()).toTuple(), fields,
                    world_to_image=mapping, nearest=nearest,
                    outline_distance_cache=canvas._outline_distance_cache,
                    blur_pyramid_cache=canvas._blur_pyramid_cache,
                )
            canvas._modifier_cache_put(key, cached)
        image, bounds = cached, target
    return image, bounds
