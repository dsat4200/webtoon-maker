"""Capture halftone colors from an outliner entity in the effect's frame."""
from contextlib import contextmanager
from dataclasses import replace

from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.core.models import ImageObject, LayerNode


@contextmanager
def source_scope(canvas):
    # A color source can contain its own Target Layer effects, including a
    # reference back to itself. Sample those effects using their incoming
    # colors, so there is no recursive feedback or order-dependent result.
    previous = getattr(canvas, "_rendering_halftone_source", False)
    canvas._rendering_halftone_source = True
    try:
        yield
    finally:
        canvas._rendering_halftone_source = previous


def source_entity(chapter, entity_id):
    return (chapter.layers.get(entity_id) or chapter.objects.get(entity_id)) if chapter else None


def source_signature(canvas, entity_id):
    entity = source_entity(canvas.chapter, entity_id)
    if entity is None:
        return ("halftone-colors", entity_id, "missing")
    with source_scope(canvas):
        layer = isinstance(entity, LayerNode)
        parent_id = entity.layer_id if layer else entity.parent_layer_id
        transform = canvas.layer_world_transform(parent_id) if parent_id else QTransform()
        placement = ()
        if isinstance(entity, ImageObject):
            # Fit-to-parent images change position when their parent shape is
            # edited, even when their own model and cached Blender frame do not.
            placement = (tuple(canvas._image_local_quad(entity)),
                         tuple(canvas._multi_transform_preview_quads.get(entity_id, ())),
                         tuple(canvas._transform_preview_quad or ())
                         if entity_id == canvas.selected_object_id else ())
        return ("halftone-colors", entity_id,
                canvas._modifier_layer_signature(entity_id) if layer
                else canvas._modifier_object_signature(entity), placement,
                tuple(getattr(transform, f"m{i}{j}")()
                      for i in range(1, 4) for j in range(1, 4)))


def render_color_source(canvas, modifier, image, bounds, local_to_world):
    """Return an aligned, cached color image; None uses incoming source colors."""
    if (getattr(canvas, "_rendering_halftone_source", False)
            or modifier.color_mode != "target_layer"
            or canvas.chapter is None):
        return None
    entity = source_entity(canvas.chapter, modifier.target_layer_id)
    if entity is None:
        return None
    layer = isinstance(entity, LayerNode)
    mapping = canvas._world_to_image_transform(
        local_to_world, bounds, image.width(), image.height())
    key = (source_signature(canvas, modifier.target_layer_id), image.width(), image.height(),
           tuple(getattr(mapping, f"m{i}{j}")()
                 for i in range(1, 4) for j in range(1, 4)))
    cached = canvas._modifier_source_cache_get(key)
    if cached is not None:
        return cached
    result = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
    result.fill(Qt.transparent)
    parent_id = entity.parent_id if layer else entity.parent_layer_id
    parent_transform = (canvas.layer_world_transform(parent_id)
                        if parent_id else QTransform())
    # Capture the chosen entity independently of the requesting canvas pass.
    # Hidden sources remain useful as color maps; child visibility, clipping,
    # opacity, and ordinary effects still belong to the selected source.
    overrides = {
        "_render_modifier_sources": set(),
        "_render_base_alpha": False,
        "_interactive_render": (canvas._interactive_render and not canvas._render_base_alpha
                                and canvas._rendering_mask_contributor <= 0),
        "_render_excluded_object_id": "",
        "_render_exclude_text": False,
        "_rendering_mask_contributor": 0,
        "_suppress_outline_for_mask": False,
        "_rendering_compound_references": not layer,
        "_rendering_outward_gradient": not layer,
        "_tiling_capture_geometry": None,
    }
    previous = {name: getattr(canvas, name, None) for name in overrides}
    revision = getattr(canvas, "_effect_provisional_revision", 0)
    painter = QPainter(result)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
    painter.setTransform(parent_transform * mapping)
    try:
        for name, value in overrides.items():
            setattr(canvas, name, value)
        with source_scope(canvas):
            target = replace(entity, visible=True, mask_only=False)
            visible = local_to_world.mapRect(bounds)
            if layer:
                canvas._render_layer(painter, target, 1., visible)
            else:
                inverse, valid = parent_transform.inverted()
                canvas._render_object(painter, target, 1., inverse.mapRect(visible) if valid else visible)
    finally:
        painter.end()
        for name, value in previous.items():
            setattr(canvas, name, value)
    if getattr(canvas, "_effect_provisional_revision", 0) == revision:
        canvas._modifier_source_cache_put(key, result)
    return result


def references_entity(canvas, kind, entity_id):
    """Whether edits to this subtree can change an external color sample."""
    chapter = canvas.chapter
    if chapter is None:
        return False
    entity = (chapter.layers.get(entity_id) if kind == "layer"
              else chapter.objects.get(entity_id))
    if entity is None:
        return False
    parent_id = entity_id if kind == "layer" else entity.parent_layer_id
    ancestry = {layer.layer_id for layer in chapter.ancestor_layers(parent_id)}
    from comic_editor.core.models import HalftoneModifier
    for modifier in chapter.modifiers.values():
        if (not isinstance(modifier, HalftoneModifier) or modifier.muted
                or modifier.color_mode != "target_layer"):
            continue
        target = source_entity(chapter, modifier.target_layer_id)
        if target is None:
            continue
        target_is_layer = isinstance(target, LayerNode)
        if ((kind, entity_id) == ("layer" if target_is_layer else "object", modifier.target_layer_id)
                or target_is_layer and modifier.target_layer_id in ancestry):
            return True
        if kind == "layer" and any(layer.layer_id == entity_id for layer in
                chapter.ancestor_layers(target.layer_id if target_is_layer else target.parent_layer_id)):
            return True
    return False
