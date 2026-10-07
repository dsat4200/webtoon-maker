"""Ownership and value-only previews for attached document-space data."""
from copy import deepcopy

from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform


def translation(transform):
    if transform.type().value <= QTransform.TransformationType.TxTranslate.value:
        return transform.dx(), transform.dy()
    return None


def moving_entities(chapter, roots):
    result = set()
    def visit(kind, identifier):
        if (kind, identifier) in result:
            return
        result.add((kind, identifier))
        if kind == "layer" and identifier in chapter.layers:
            for child in chapter.layers[identifier].children:
                visit(child.kind, child.entity_id)
    for root in roots:
        visit(*root)
    return result


def ownership(chapter):
    modifiers, masks = {}, {}
    for kind, records in (("layer", chapter.layers), ("object", chapter.objects)):
        for identifier, owner in records.items():
            ref = (kind, identifier)
            if owner.opacity_mask is not None:
                masks.setdefault(owner.opacity_mask.mask_id, set()).add(ref)
            for mid in owner.modifier_ids:
                modifiers.setdefault(mid, set()).add(ref)
                modifier = chapter.modifiers.get(mid)
                if modifier is not None:
                    for binding in modifier.parameter_masks.values():
                        masks.setdefault(binding.mask_id, set()).add(ref)
    return modifiers, masks


def record_snapshot(chapter, roots, *, object_fields=None):
    """Retain changed placement records and the rigs this gesture can move."""
    from comic_editor.core.document_patch import RecordSnapshot
    roots = tuple(roots)
    moving = moving_entities(chapter, roots)
    modifiers, masks = ownership(chapter)
    attributes = {'objects': tuple(object_fields)} if object_fields is not None else None
    return RecordSnapshot.capture(chapter, scalars=('size',),
        layers=[identifier for kind, identifier in roots if kind == 'layer'],
        objects=[identifier for kind, identifier in roots if kind == 'object'],
        modifiers=[identifier for identifier, owners in modifiers.items() if owners and owners <= moving],
        masks=[identifier for identifier, owners in masks.items() if owners and owners <= moving],
        attributes=attributes)


def translate_mask(mask, dx, dy):
    """Move owned geometry and place unchanged paint samples at a new origin.

    Contributors are live links to scene entities; their scene transforms
    already determine whether they move. Never move their artwork here.
    """
    from comic_editor.core.assets import _translate_mask
    _translate_mask(mask, dx, dy)


def transform_attached(canvas, roots, transform):
    from comic_editor.ui.transform_modifier_preview import transform_modifier_rig
    moving = moving_entities(canvas.chapter, roots)
    modifiers, masks = ownership(canvas.chapter)
    delta = translation(transform)
    for mid, owners in modifiers.items():
        if owners and owners <= moving and (delta is not None or len(owners) == 1):
            transform_modifier_rig(canvas, canvas.chapter.modifiers[mid], transform)
    if delta is not None and delta != (0., 0.):
        for mask_id, owners in masks.items():
            mask = canvas.chapter.masks.get(mask_id)
            if mask is not None and owners and owners <= moving:
                translate_mask(mask, *delta)


def preview_attachment_context(canvas):
    if canvas.chapter is None:
        return None
    source, destination = canvas._transform_start_quad, canvas._transform_preview_quad
    target = canvas._geometry_transform_target
    if source is None or destination is None or source == destination:
        return None
    if target == ("multi", ""):
        roots = [("object", oid) for oid in canvas._multi_transform_preview_quads]
        parent = QTransform()
    elif target and target[0] == "layer_group":
        roots = [("layer", target[1])]
        layer = canvas.chapter.layers.get(target[1])
        if layer is None:
            return None
        parent = canvas.layer_world_transform(layer.parent_id) if layer.parent_id else QTransform()
    elif target is None and canvas.selected_object_id:
        obj = canvas.chapter.objects.get(canvas.selected_object_id)
        if obj is None:
            return None
        roots = [("object", obj.object_id)]
        parent = canvas.layer_world_transform(obj.parent_layer_id)
    else:
        return None
    projection = getattr(canvas, "_document_projection", None)
    key = (id(canvas.chapter), tuple(roots), tuple(source), tuple(destination),
           tuple(getattr(parent, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
           getattr(projection, "revision", None), id(canvas._model_before),
           len(canvas.chapter.layers), len(canvas.chapter.objects), len(canvas.chapter.modifiers))
    cached = getattr(canvas, "_attached_preview_context", None)
    if cached is not None and cached[0] == key:
        return cached[1]
    # A translation must stay exactly affine, including under an affine parent.
    changes = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(source, destination)]
    if all(abs(dx - changes[0][0]) < 1e-9 and abs(dy - changes[0][1]) < 1e-9
           for dx, dy in changes):
        delta = parent.map(QPointF(*changes[0])) - parent.map(QPointF())
        if not parent.isAffine():
            return None
        transform = QTransform.fromTranslate(delta.x(), delta.y())
    else:
        transform = canvas._quad_to_quad_transform(
            [parent.map(QPointF(*p)).toTuple() for p in source],
            [parent.map(QPointF(*p)).toTuple() for p in destination])
    moving = moving_entities(canvas.chapter, roots)
    modifiers, masks = ownership(canvas.chapter)
    result = transform, moving, modifiers, masks
    canvas._attached_preview_context = key, result
    return result


def effective_preview_mask(canvas, mask):
    context = preview_attachment_context(canvas)
    if context is None:
        return mask
    transform, moving, _, masks = context
    delta = translation(transform)
    owners = masks.get(mask.mask_id, set())
    if delta is None or not owners or not owners <= moving:
        return mask
    result = deepcopy(mask)
    translate_mask(result, *delta)
    return result


def preview_object_bounds(canvas, obj, bounds):
    """Use the live destination for source captures as well as presentation."""
    from comic_editor.core.models import TextObject
    if isinstance(obj, TextObject) or bounds is None:
        return bounds
    destination = canvas._multi_transform_preview_quads.get(obj.object_id)
    if destination is None and obj.object_id == canvas.selected_object_id:
        destination = canvas._transform_preview_quad
    if destination is None:
        return bounds
    source = canvas.object_world_quad(obj.object_id)
    if not source:
        return bounds
    parent = canvas.layer_world_transform(obj.parent_layer_id)
    transform = canvas._quad_to_quad_transform(source,
        [parent.map(QPointF(*p)).toTuple() for p in destination])
    return transform.mapRect(bounds)
