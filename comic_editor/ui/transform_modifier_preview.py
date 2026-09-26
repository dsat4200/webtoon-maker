"""Carry sole-target document-space rigs through an ancestor preview."""
from copy import deepcopy
from collections import OrderedDict
import math

from PySide6.QtCore import QPointF
from PySide6.QtGui import QPolygonF, QTransform

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, CageTransformModifier, DistortModifier,
    MirrorModifier, RadialBlurModifier, TilingModifier,
)


_RIG_TYPES = (ArrayModifier, BlurModifier, CageTransformModifier, DistortModifier,
              MirrorModifier, RadialBlurModifier, TilingModifier)


def transform_modifier_rig(canvas, modifier, transform):
    """Apply the established commit transformation to one mutable rig."""
    if isinstance(modifier, DistortModifier):
        if modifier.modifier_type == "distort_smudge":
            from comic_editor.core.smudge import transform_strokes
            modifier.parameters["strokes"] = transform_strokes(
                modifier.parameters.get("strokes", []),
                lambda point: transform.map(QPointF(*point)).toTuple(),
            )
        canvas._transform_distort_modifier(modifier, transform)
    elif isinstance(modifier, TilingModifier):
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
        center, _ramp, end = canvas._focal_points(modifier)
        mapped_center, mapped_end = transform.map(center), transform.map(end)
        delta = mapped_end - mapped_center
        modifier.focal_center = mapped_center.toTuple()
        modifier.focal_radius = max(1., math.hypot(delta.x(), delta.y()))
        modifier.focal_angle = math.atan2(delta.y(), delta.x())


def effective_preview_modifier(canvas, modifier):
    """Return a transformed value copy; shared and unrelated rigs stay fixed."""
    if not isinstance(modifier, _RIG_TYPES):
        return modifier
    target = canvas._geometry_transform_target
    source, destination = canvas._transform_start_quad, canvas._transform_preview_quad
    if (target is None or target[0] != "layer_group" or source is None
            or destination is None or source == destination or canvas.chapter is None):
        canvas._transform_modifier_preview_cache = None
        return modifier
    chapter = canvas.chapter
    moving = chapter.layers.get(target[1])
    if moving is None:
        return modifier
    parent = (canvas.layer_world_transform(moving.parent_id)
              if moving.parent_id else QTransform())
    projection = getattr(canvas, "_document_projection", None)
    key = (id(chapter), target, tuple(source), tuple(destination),
           tuple(getattr(parent, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
           getattr(projection, "revision", None), id(getattr(canvas, "_model_before", None)),
           len(chapter.layers), len(chapter.objects), len(chapter.modifiers))
    cache = getattr(canvas, "_transform_modifier_preview_cache", None)
    if cache is None or cache[0] != key:
        # Index ownership once per current preview, rather than scanning the
        # entire chapter for every modifier used by every source capture.
        owners = {}
        for kind, records in (("layer", chapter.layers), ("object", chapter.objects)):
            for identifier, owner in records.items():
                for modifier_id in owner.modifier_ids:
                    owners[modifier_id] = (kind, identifier) if modifier_id not in owners else None
        transform = QTransform.quadToQuad(
            parent.map(QPolygonF([QPointF(*point) for point in source])),
            parent.map(QPolygonF([QPointF(*point) for point in destination])),
        )
        cache = canvas._transform_modifier_preview_cache = (key, owners, transform, OrderedDict())
    _, owners, transform, values = cache
    owner_ref = owners.get(modifier.modifier_id)
    if owner_ref is None:
        return modifier
    kind, identifier = owner_ref
    owner = chapter.modifier_target(kind, identifier)
    if owner is None:
        return modifier
    parent_id = identifier if kind == "layer" else owner.parent_layer_id
    if not any(layer.layer_id == moving.layer_id for layer in chapter.ancestor_layers(parent_id)):
        return modifier
    # Dataclass values include the rig, masks and parameters without calling
    # validating serializers on the saved model during a transient preview.
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
