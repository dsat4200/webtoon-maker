"""Coordinate-aware bounds and reflection shared by assets, rendering, and baking."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import BlurModifier, MirrorModifier, OutlineModifier


def reflection_transform(modifier):
    x, y = modifier.axis_start
    dx = modifier.axis_end[0] - x
    dy = modifier.axis_end[1] - y
    length = dx * dx + dy * dy
    if length < 1e-12:
        return QTransform()
    a, b = (dx * dx - dy * dy) / length, 2 * dx * dy / length
    return QTransform(a, b, b, -a, x - a * x - b * y, y - b * x + a * y)


def effect_bounds(bounds, modifiers, local_to_world=None):
    result = QRectF(bounds)
    transform = local_to_world or QTransform()
    inverse, valid = transform.inverted()
    for modifier in modifiers:
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, MirrorModifier) and valid:
            reflected = transform * reflection_transform(modifier) * inverse
            result = result.united(reflected.mapRect(result))
        else:
            attribute = "strength" if isinstance(modifier, BlurModifier) else "thickness"
            padding = getattr(modifier, attribute, 0.0)
            binding = modifier.parameter_masks.get(attribute)
            if binding:
                padding = max(padding, binding.black_value, binding.white_value)
            if isinstance(modifier, BlurModifier):
                padding *= 3
            elif not isinstance(modifier, OutlineModifier):
                padding = 0
            result.adjust(-padding, -padding, padding, padding)
    return result
