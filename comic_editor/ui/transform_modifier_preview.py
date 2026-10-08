"""Carry attached document-space rigs through object and group previews."""
from copy import deepcopy
from collections import OrderedDict
import math

from PySide6.QtCore import QPointF

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, CageTransformModifier, DistortModifier,
    MirrorModifier, RadialBlurModifier, TilingModifier,
    TextureModifier,
)


_RIG_TYPES = (ArrayModifier, BlurModifier, CageTransformModifier, DistortModifier,
              MirrorModifier, RadialBlurModifier, TilingModifier, TextureModifier)


def transform_modifier_rig(canvas, modifier, transform):
    """Apply the established commit transformation to one mutable rig."""
    from comic_editor.ui.attached_translation import translation
    delta = translation(transform)
    if isinstance(modifier, TextureModifier):
        if modifier.texture_quad is not None:
            modifier.texture_quad = [transform.map(QPointF(*p)).toTuple() for p in modifier.texture_quad]
    elif isinstance(modifier, DistortModifier):
        if modifier.modifier_type == "distort_smudge":
            if delta is not None:
                strokes = deepcopy(modifier.parameters.get("strokes", []))
                for stroke in strokes:
                    for point in stroke["points"]:
                        for name in ("position", "handle"):
                            x, y = point[name]
                            point[name] = [x + delta[0], y + delta[1]]
                modifier.parameters["strokes"] = strokes
            else:
                from comic_editor.core.smudge import transform_strokes
                modifier.parameters["strokes"] = transform_strokes(
                    modifier.parameters.get("strokes", []),
                    lambda point: transform.map(QPointF(*point)).toTuple(),
                )
        if delta is not None:
            dx, dy = delta
            x, y, w, h = modifier.frame
            modifier.frame = (x + dx, y + dy, w, h)
            modifier.center = (modifier.center[0] + dx, modifier.center[1] + dy)
        else:
            canvas._transform_distort_modifier(modifier, transform)
    elif isinstance(modifier, TilingModifier):
        if delta is not None:
            modifier.center = transform.map(QPointF(*modifier.center)).toTuple()
            return
        a = transform.map(QPointF(1, 0)) - transform.map(QPointF())
        b = transform.map(QPointF(0, 1)) - transform.map(QPointF())
        length_a, length_b = math.hypot(a.x(), a.y()), math.hypot(b.x(), b.y())
        if (transform.isAffine() and abs(length_a - length_b) < 1e-6
                and abs(a.x() * b.x() + a.y() * b.y()) < 1e-6
                and transform.determinant() > 0):
            modifier.center = transform.map(QPointF(*modifier.center)).toTuple()
            modifier.side = max(1., modifier.side * length_a)
            modifier.rotation = (modifier.rotation + math.degrees(math.atan2(a.y(), a.x()))) % 360
    elif isinstance(modifier, CageTransformModifier):
        if delta is not None:
            from comic_editor.core.assets import _translate_cage
            _translate_cage(modifier, *delta)
            return
        rest = modifier.rest_points().reshape(modifier.rows, modifier.columns, 2)
        corners = [rest[0, 0], rest[0, -1], rest[-1, -1], rest[-1, 0]]
        modifier.source_quad = [transform.map(QPointF(*p)).toTuple() for p in corners]
        modifier.points = [transform.map(QPointF(*p)).toTuple() for p in modifier.points]
        modifier.pivot = transform.map(QPointF(*modifier.pivot)).toTuple()
    elif isinstance(modifier, ArrayModifier):
        modifier.axis_start = transform.map(QPointF(*modifier.axis_start)).toTuple()
        modifier.axis_end = transform.map(QPointF(*modifier.axis_end)).toTuple()
        modifier.center = transform.map(QPointF(*modifier.center)).toTuple()
    elif isinstance(modifier, RadialBlurModifier):
        modifier.center = transform.map(QPointF(*modifier.center)).toTuple()
    elif isinstance(modifier, MirrorModifier):
        modifier.axis_start = transform.map(QPointF(*modifier.axis_start)).toTuple()
        modifier.axis_end = transform.map(QPointF(*modifier.axis_end)).toTuple()
    elif isinstance(modifier, BlurModifier):
        if delta is not None:
            modifier.focal_center = transform.map(QPointF(*modifier.focal_center)).toTuple()
            return
        center, _ramp, end = canvas._focal_points(modifier)
        mapped_center, mapped_end = transform.map(center), transform.map(end)
        delta = mapped_end - mapped_center
        modifier.focal_center = mapped_center.toTuple()
        modifier.focal_radius = max(1., math.hypot(delta.x(), delta.y()))
        modifier.focal_angle = math.atan2(delta.y(), delta.x())


def effective_preview_modifier(canvas, modifier):
    """Return a transformed value copy for a wholly moving set of owners."""
    color_edit = getattr(canvas, "_overlay_color_preview", None)
    if (color_edit is not None and canvas.chapter is color_edit[0]
            and modifier is color_edit[1]
            and getattr(canvas, "_history_generation", 0) == color_edit[3]):
        return color_edit[2]
    from comic_editor.ui.attached_translation import preview_attachment_context, preview_cache_context, translation
    if not isinstance(modifier, _RIG_TYPES):
        return modifier
    context = preview_attachment_context(canvas)
    if context is None:
        canvas._transform_modifier_preview_cache = None
        return modifier
    transform, moving, owners, _masks = context
    references = owners.get(modifier.modifier_id, set())
    if not references or not references <= moving or (translation(transform) is None and len(references) != 1):
        return modifier
    baseline, revision = preview_cache_context(canvas)
    key = (id(canvas.chapter), frozenset(moving),
           tuple(getattr(transform, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
           baseline, revision)
    cache = getattr(canvas, "_transform_modifier_preview_cache", None)
    if cache is None or cache[0] != key:
        cache = canvas._transform_modifier_preview_cache = (key, owners, transform, OrderedDict())
    values = cache[3]
    if isinstance(modifier, TextureModifier):
        from comic_editor.core.texture_library import texture_digest
        signature = repr((texture_digest(modifier.texture_data),
                          {key: value for key, value in vars(modifier).items() if key != "texture_data"}))
    else:
        signature = repr(modifier)
    previous = values.get(modifier.modifier_id)
    if previous is not None and previous[0] == signature:
        values.move_to_end(modifier.modifier_id)
        return previous[1]
    result = deepcopy(modifier)
    transform_modifier_rig(canvas, result, transform)
    values[modifier.modifier_id] = (signature, result)
    values.move_to_end(modifier.modifier_id)
    while len(values) > 128:
        values.popitem(last=False)
    return result
