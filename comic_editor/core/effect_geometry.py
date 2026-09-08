"""Coordinate-aware bounds and reflection shared by assets, rendering, and baking."""
import math
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import ArrayModifier, BlurModifier, MirrorModifier, OutlineModifier, RadialBlurModifier, CageTransformModifier, ScreamModifier, WobbleModifier


def array_indices(modifier):
    """Signed steps, excluding the original; an odd centered count favors forward."""
    if modifier.repeat_type == "last":
        return range(-modifier.count, 0)
    if modifier.repeat_type == "center":
        return tuple(i for i in range(-(modifier.count // 2),
                                     (modifier.count + 1) // 2 + 1) if i)
    return range(1, modifier.count + 1)


def array_transform(modifier, step):
    # Translation stays linear. Rotation/scale accumulate about the copy's
    # translated pivot, rather than bending the repetition axis into an orbit.
    scale = (1. + modifier.scale_offset / 100.) ** step
    angle = math.radians(modifier.angle_offset * step)
    a, b = scale * math.cos(angle), scale * math.sin(angle)
    x, y = modifier.center
    dx = (modifier.axis_end[0] - modifier.axis_start[0]) * step
    dy = (modifier.axis_end[1] - modifier.axis_start[1]) * step
    return QTransform(a, b, -b, a, x - a*x + b*y + dx, y - b*x - a*y + dy)


def array_input_bounds(bounds, modifier, local_to_world):
    """Pull an output request back through every copy, including the original."""
    inverse, valid = local_to_world.inverted()
    result = QRectF(bounds)
    if valid:
        for step in array_indices(modifier):
            transform = local_to_world * array_transform(modifier, step) * inverse
            back, invertible = transform.inverted()
            if invertible:
                result = result.united(back.mapRect(bounds))
    return result


def reflection_transform(modifier):
    x, y = modifier.axis_start
    dx = modifier.axis_end[0] - x
    dy = modifier.axis_end[1] - y
    length = dx * dx + dy * dy
    if length < 1e-12:
        return QTransform()
    a, b = (dx * dx - dy * dy) / length, 2 * dx * dy / length
    return QTransform(a, b, b, -a, x - a * x - b * y, y - b * x + a * y)


def radial_sweep_bounds(bounds, center, angle):
    """Conservative exact extrema of a rectangle swept around a center."""
    if angle <= 0 or bounds.isEmpty():
        return QRectF(bounds)
    half = min(360., angle)*math.pi/360
    points = []
    for corner in (bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft()):
        dx, dy = corner.x()-center[0], corner.y()-center[1]
        phase, radius = math.atan2(dy, dx), math.hypot(dx, dy)
        phases = [phase-half, phase+half]
        phases.extend(i*math.pi/2 for i in range(math.ceil((phase-half)/(math.pi/2)),
                                                math.floor((phase+half)/(math.pi/2))+1))
        points.extend(QPointF(center[0]+radius*math.cos(a), center[1]+radius*math.sin(a)) for a in phases)
    return QRectF(QPointF(min(p.x() for p in points), min(p.y() for p in points)),
                  QPointF(max(p.x() for p in points), max(p.y() for p in points))).united(bounds)


def effect_bounds(bounds, modifiers, local_to_world=None):
    result = QRectF(bounds)
    transform = local_to_world or QTransform()
    inverse, valid = transform.inverted()
    for modifier in modifiers:
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, (ScreamModifier, WobbleModifier)):
            attribute = "height" if isinstance(modifier, ScreamModifier) else "position"
            padding = getattr(modifier, attribute)
            binding = modifier.parameter_masks.get(attribute)
            if binding:
                padding = max(padding, binding.black_value, binding.white_value)
            amount = modifier.parameter_masks.get("intensity")
            padding *= max(modifier.intensity, amount.black_value, amount.white_value)/100 if amount else modifier.intensity/100
            result.adjust(-padding-2, -padding-2, padding+2, padding+2)
        elif isinstance(modifier, CageTransformModifier) and valid:
            from comic_editor.core.cage import deformed_bounds
            result = result.united(inverse.mapRect(QRectF(*deformed_bounds(modifier))))
        elif isinstance(modifier, ArrayModifier) and valid:
            source = QRectF(result)
            for step in array_indices(modifier):
                copy = transform * array_transform(modifier, step) * inverse
                result = result.united(copy.mapRect(source))
        elif isinstance(modifier, MirrorModifier) and valid:
            reflected = transform * reflection_transform(modifier) * inverse
            result = result.united(reflected.mapRect(result))
        elif isinstance(modifier, RadialBlurModifier) and valid:
            binding = modifier.parameter_masks.get("angle")
            angle = max(modifier.angle, binding.black_value, binding.white_value) if binding else modifier.angle
            if angle > 0:
                # Bilinear support extends half a source pixel beyond its
                # frame. Include it before sweeping, then align at the caller.
                source = result.adjusted(-.5, -.5, .5, .5)
                result = inverse.mapRect(radial_sweep_bounds(transform.mapRect(source), modifier.center, angle))
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
