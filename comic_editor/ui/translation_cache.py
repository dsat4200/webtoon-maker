"""Translation-invariant aliases in the ordinary exact effect cache.

Only complete, unchanged sampling frames qualify. Linked scene masks and
external effect sources keep the existing dependency path. No renderer or
durable invalidation policy is introduced here.
"""
from copy import deepcopy
import json

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import RasterObject, ImageObject, VectorDrawingObject, TextObject, LayerNode
from comic_editor.render.pixels import current_contract
from comic_editor.ui.attached_translation import effective_preview_mask, translate_mask
from comic_editor.ui.modifier_rendering import modifier_render_settings
from comic_editor.ui.transform_modifier_preview import transform_modifier_rig


def _coordinates(value):
    # Canonicalize only coordinate subtraction roundoff, more finely than the
    # existing semantic frame keys. Parameter values and pixel bits stay exact.
    if isinstance(value, float):
        return round(value, 10)
    if isinstance(value, (tuple, list)):
        return tuple(_coordinates(item) for item in value)
    return value


def output_key(canvas, target, bounds, mapping, modifiers, *, tile_space=False, geometry=(), opacity=True):
    if (not isinstance(target, (RasterObject, ImageObject, VectorDrawingObject, TextObject, LayerNode))
            or not mapping.isAffine() or canvas._render_base_alpha
            or canvas._rendering_mask_contributor or canvas._render_cage_source
            or canvas._cage_session is not None
            or getattr(canvas, "_tiling_capture_geometry", None) is not None
            or getattr(canvas, "_rendering_halftone_source", False)
            or getattr(canvas, "_effect_preview_channel", "canvas") == "navigator"):
        return None
    if isinstance(target, TextObject) and target.layout_mode == "strict":
        return None  # Parent clipping/wrapping is a real source dependency.
    layer = isinstance(target, LayerNode)
    signature = (canvas._modifier_layer_signature(target.layer_id) if layer
                 else canvas._modifier_object_signature(target))
    live = signature[4]
    if layer and live and live[0] == "layer_group":
        live = live[3:]
    if any(item[0] not in {"transform", "object-transform"} for item in live):
        return None
    parent_id = target.parent_id if layer else target.parent_layer_id
    parent = canvas.layer_world_transform(parent_id) if parent_id else QTransform()
    inverse, valid = parent.inverted()
    if not valid or not parent.isAffine():
        return None
    if layer:
        placement = canvas._layer_parent_transform(target)
        if placement.type().value > QTransform.TransformationType.TxTranslate.value or target.bound is None:
            return None
        quad = [placement.map(QPointF(*p)).toTuple() for p in canvas._rect_quad(QRectF(*target.bound.bbox()))]
    else:
        quad = canvas._multi_transform_preview_quads.get(target.object_id)
        if quad is None and target.object_id == canvas.selected_object_id:
            quad = canvas._transform_preview_quad
    if not layer and quad is None:
        world_quad = canvas.object_world_quad(target.object_id)
        if not world_quad:
            return None
        quad = [inverse.map(QPointF(*p)).toTuple() for p in world_quad]
    anchor = QPointF() if tile_space else QPointF(*quad[0])
    world_anchor = mapping.map(anchor)
    shift = QTransform.fromTranslate(-world_anchor.x(), -world_anchor.y())
    settings = canvas._modifier_entity_settings(target)
    for name in ("position", "translation", "x", "y", "transform_frame", "transform_quad"):
        settings.pop(name, None)
    # Destination shape captures rotations, scaling and persistent mapping;
    # absolute placement is retained only through its native capture phase.
    relative_quad = tuple((x - quad[0][0], y - quad[0][1]) for x, y in quad)
    def relative(rect):
        value = QRectF(rect) if isinstance(rect, QRectF) else QRectF(*rect)
        return canvas._rect_signature(value.translated(-anchor))
    masks, parameters = {}, []
    bindings = []
    def modifier_settings(modifier):
        if (getattr(modifier, "color_mode", None) == "target_layer"
                or getattr(modifier, "source_mode", None) == "beneath"):
            raise ValueError("External effect source")
        rig = deepcopy(modifier)
        transform_modifier_rig(canvas, rig, shift)
        rendered = modifier_render_settings(rig)
        for name in ("frame", "center", "pivot", "points", "source_quad", "axis_start", "axis_end", "focal_center", "texture_quad"):
            if name in rendered:
                rendered[name] = _coordinates(rendered[name])
        bindings.extend(modifier.parameter_masks.values())
        return rendered
    def subtree(entity):
        is_layer = isinstance(entity, LayerNode)
        data = canvas._modifier_entity_settings(entity)
        active = canvas._active_modifier_instances(entity.modifier_ids)
        effects = [modifier_settings(modifier) for modifier in active]
        if entity.opacity_mask is not None:
            bindings.append(entity.opacity_mask)
        if is_layer:
            # Descendant layer visibility and live geometry also affect this
            # source. Keep them on the established dependency path, as for
            # objects, instead of admitting an incomplete translation alias.
            if canvas._modifier_layer_signature(entity.layer_id)[4]:
                raise ValueError("Live source")
            children = [subtree(canvas.chapter.modifier_target(child.kind, child.entity_id))
                        for child in entity.children]
            return data, effects, children
        record = canvas._modifier_object_signature(entity)
        if record[4] or entity.object_id == canvas._live_underlay_object_id:
            raise ValueError("Live source")
        return data, effects, record[3]
    try:
        parameters = [modifier_settings(modifier) for modifier in modifiers]
        source = ([subtree(canvas.chapter.modifier_target(child.kind, child.entity_id)) for child in target.children]
                  if layer else signature[3])
    except ValueError:
        return None
    if opacity and target.opacity_mask is not None:
        bindings.append(target.opacity_mask)
    for binding in bindings:
        mask = canvas.chapter.masks.get(binding.mask_id)
        if mask is None or mask.contributors:
            return None  # Linked source transforms/clips remain world dependencies.
        mask = deepcopy(effective_preview_mask(canvas, mask))
        translate_mask(mask, -world_anchor.x(), -world_anchor.y())
        settings_mask = mask.to_dict()
        # Paint revisions remain dependencies; moving owned geometry does not
        # bump them. Both cache tiers use the same pixel identities below.
        masks[binding.mask_id] = (settings_mask, canvas.tiles.object_signature(binding.mask_id))
    matrix = (mapping.m11(), mapping.m12(), mapping.m21(), mapping.m22())
    phase = () if tile_space else (anchor.x() % 1, anchor.y() % 1)
    key = (settings, relative_quad, source, parameters, masks, matrix,
           phase, relative(bounds), tuple((relative(a), relative(b)) for a, b in geometry),
           repr(current_contract()), canvas._render_exclude_text,
           getattr(canvas, "_suppress_outline_for_mask", False))
    from comic_editor.render.source_context import source_color_context
    context = source_color_context(canvas.chapter.pixel_contract)
    if context:
        key = (*key, context)
    return ("translated-exact-output", target.layer_id if layer else target.object_id,
            json.dumps(key, sort_keys=True, separators=(",", ":")))


def get(canvas, key):
    if key is None:
        return None
    image = canvas._modifier_cache_get(key)
    if image is None:
        retained = canvas._effect_jobs.retained_get(("output", _scope(canvas, key)), key)
        image = retained[0] if retained is not None else None
    return image


def _scope(canvas, key):
    identifier = key[2][1] if key[0] == "translated-opacity-output" else key[1]
    return "translation", identifier, key[0], getattr(canvas, "_effect_preview_channel", "canvas")


def put(canvas, key, image, revision):
    if key is not None and revision == getattr(canvas, "_effect_provisional_revision", 0):
        canvas._modifier_cache_put(key, image)
        canvas._retain_modifier_output(key, _scope(canvas, key), image)
