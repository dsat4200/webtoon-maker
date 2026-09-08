"""Transactional, resource-aware flattening and Raster modifier prefix baking."""
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF
from PySide6.QtGui import QPainter, QTransform

from comic_editor.core.assets import entity_visual_bounds
from comic_editor.core.commands import CallbackCommand
from comic_editor.core.models import ChildRef, ImageObject, LayerNode, ArrayModifier, MirrorModifier, RasterObject, RadialBlurModifier, TilingModifier
from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.ui.effect_pipeline import aligned, empty_image, render_stages


def subtree(canvas, kind, identifier):
    result = {(kind, identifier)}
    if kind == "layer":
        for ref in canvas.chapter.layers[identifier].children:
            result.update(subtree(canvas, ref.kind, ref.entity_id))
    return result


def rasterize_reason(canvas, kind, identifier):
    chapter = canvas.chapter
    target = chapter.layers.get(identifier) if kind == "layer" else chapter.objects.get(identifier)
    if target is None or isinstance(target, LayerNode) and target.is_page:
        return "Pages cannot be rasterized."
    parent_id = target.parent_id if kind == "layer" else target.parent_layer_id
    if parent_id and chapter.layers[parent_id].layer_kind == "text_container":
        return "Rasterize the containing Free Text container instead; its children must remain Text objects."
    if kind == "layer":
        parent = chapter.closest_compound_ancestor(target.parent_id, include_self=True)
        reflected = any(isinstance(chapter.modifiers.get(mid), MirrorModifier) and
                        chapter.modifiers[mid].compound_operation != "ignore" and
                        not chapter.modifiers[mid].muted and chapter.modifiers[mid].intensity > 0
                        for mid in target.modifier_ids)
        if parent is not None and (target.compound_operation != "ignore" or reflected):
            return "Rasterize the containing compound shape instead."
    members = subtree(canvas, kind, identifier)
    descendants = members - {(kind, identifier)}
    for member_kind, member_id in members:
        obj = chapter.objects.get(member_id) if member_kind == "object" else None
        if isinstance(obj, ImageObject) and canvas.images.source(member_id) is None:
            return "An image source is unavailable; reconnect it before rasterizing."
    for mask in chapter.masks.values():
        if any(tuple(ref) in descendants for ref in mask.contributors):
            outside = [entity for entity_kind, table in (("layer", chapter.layers), ("object", chapter.objects))
                       for key, entity in table.items() if (entity_kind, key) not in members]
            required = mask.saved or any(
                entity.opacity_mask is not None and entity.opacity_mask.mask_id == mask.mask_id or
                any(binding.mask_id == mask.mask_id for mid in entity.modifier_ids
                    for binding in chapter.modifiers[mid].parameter_masks.values())
                for entity in outside)
            if required:
                return "A descendant is required by a mask. Remove that reference before rasterizing."
    for obj in chapter.objects.values():
        if ("object", obj.object_id) not in members and any(
            ("object", getattr(obj, attr, "")) in descendants
            for attr in ("center_shape_id", "owner_gradient_id")
        ):
            return "A descendant is required by another object."
    return ""


def visual_bounds(canvas, kind, identifier):
    """Conservative document-space bounds including each subtree effect stage."""
    chapter = canvas.chapter
    target = chapter.layers[identifier] if kind == "layer" else chapter.objects[identifier]
    result = entity_visual_bounds(chapter, canvas.tiles, kind, identifier)
    if kind == "layer":
        for ref in target.children:
            result = result.united(visual_bounds(canvas, ref.kind, ref.entity_id))
    if canvas._own_tiling(target):
        result = canvas._tiling_boundary(target).boundingRect()
    parent = target.parent_id if kind == "layer" else target.parent_layer_id
    mapping = canvas.layer_world_transform(parent) if parent else QTransform()
    inverse, valid = mapping.inverted()
    if not valid:
        raise ValueError("Cannot bake a singular transform")
    return mapping.mapRect(effect_bounds(inverse.mapRect(result),
        [chapter.modifiers[mid] for mid in target.modifier_ids if mid in chapter.modifiers], mapping))


def snapshot(canvas, object_ids):
    return (canvas.chapter.to_dict(), canvas.images.snapshot(),
            {identifier: canvas.tiles.object_tiles(identifier) for identifier in object_ids})


def commit(canvas, before, after, label, selection_before, selection_after):
    def restore(state, selection):
        model, images, tiles = state
        canvas.replace_chapter(model)
        canvas.images.restore(images)
        for identifier, payload in tiles.items():
            canvas.tiles.replace_object_tiles(identifier, payload)
        if selection:
            canvas.set_selection_set(selection, primary=selection[-1])
        canvas.hierarchyChanged.emit()
        canvas.documentChanged.emit(None)
        canvas.update()
    canvas.command_stack.push(CallbackCommand(
        label, lambda: restore(after, selection_after), lambda: restore(before, selection_before)
    ), already_done=True)
    canvas.active_modifier_id = ""
    canvas.set_selection_set(selection_after, primary=selection_after[-1])
    canvas._compound_path_cache.clear()
    canvas.hierarchyChanged.emit()
    canvas.documentChanged.emit(None)
    canvas.update()


def rasterize(canvas, kind, identifier):
    reason = rasterize_reason(canvas, kind, identifier)
    if reason:
        raise ValueError(reason)
    canvas._commit_text_edit()
    chapter = canvas.chapter
    target = chapter.layers[identifier] if kind == "layer" else chapter.objects[identifier]
    members = subtree(canvas, kind, identifier)
    objects = {item for member_kind, item in members if member_kind == "object"} | {identifier}
    parent_id = target.parent_id if kind == "layer" else target.parent_layer_id
    parent = chapter.layers[parent_id]
    position = next(i for i, ref in enumerate(parent.children) if ref.entity_id == identifier)
    bounds = aligned(visual_bounds(canvas, kind, identifier))
    image = empty_image(bounds)
    mapping = canvas.layer_world_transform(parent_id)
    inverse, valid = mapping.inverted()
    if not valid:
        raise ValueError("Cannot rasterize a singular transform")
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.left(), -bounds.top())
    painter.setTransform(mapping, True)
    was_visible, was_mask_only = target.visible, target.mask_only
    old_references = canvas._rendering_compound_references
    old_interactive = canvas._interactive_render
    try:
        target.visible, target.mask_only = True, False
        canvas._interactive_render = False
        canvas._rendering_compound_references = True
        if kind == "layer":
            canvas._render_layer(painter, target, 1.0, bounds)
        else:
            canvas._render_object(painter, target, 1.0, inverse.mapRect(bounds))
    finally:
        target.visible, target.mask_only = was_visible, was_mask_only
        canvas._rendering_compound_references = old_references
        canvas._interactive_render = old_interactive
        painter.end()
    data = QByteArray()
    buffer = QBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    if not image.save(buffer, "PNG"):
        raise ValueError("Unable to encode the rasterized image")
    buffer.close()
    replacement = ImageObject(
        object_id=identifier, parent_layer_id=parent_id, name=target.name,
        custom_name=getattr(target, "custom_name", True), visible=target.visible,
        mask_only=target.mask_only, fill_reference=target.fill_reference,
        ignore_parent_mask=getattr(target, "ignore_parent_mask", False),
        geometry_reference=getattr(target, "geometry_reference", "direct"),
        underlay_opacity=getattr(target, "underlay_opacity", 0),
        source_filename="rasterized.png", source_mime_type="image/png",
        pixel_width=image.width(), pixel_height=image.height(),
        transform_frame=(0, 0, image.width(), image.height()),
        transform_quad=[inverse.map(point).toTuple() for point in
                        (bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft())],
    )
    before = snapshot(canvas, objects)
    selection = list(canvas.selected_entities)
    # Decode/validate before removing any graph or resource.
    canvas.images.put_decoded(identifier, "rasterized.png", bytes(data), image)
    for member_kind, member_id in members:
        if member_kind == "layer":
            chapter.layers.pop(member_id)
        else:
            chapter.objects.pop(member_id)
            canvas.tiles.remove_object(member_id)
            if member_id != identifier:
                canvas.images.remove(member_id)
    chapter.objects[identifier] = replacement
    for layer in chapter.layers.values():
        if layer.last_raster_id in objects:
            layer.last_raster_id = None
    parent.children[position] = ChildRef("object", identifier)
    for mask in chapter.masks.values():
        if (kind, identifier) in mask.contributors:
            mask.contributors = [("object", identifier) if ref == (kind, identifier) else ref for ref in mask.contributors]
            mask.touch()
    chapter._garbage_collect_modifiers()
    commit(canvas, before, snapshot(canvas, objects), "Rasterize", selection, [("object", identifier)])


def apply_raster_modifiers(canvas, modifier_id):
    chapter = canvas.chapter
    targets = list(canvas.selected_entities)
    if not targets or any(kind != "object" or not isinstance(chapter.objects.get(identifier), RasterObject) for kind, identifier in targets):
        raise ValueError("Apply requires only Raster objects to be selected")
    stage = chapter.modifiers.get(modifier_id)
    if stage is None or stage.muted:
        raise ValueError("Unmute the modifier before applying it")
    prepared = []
    for _, identifier in targets:
        obj = chapter.objects[identifier]
        if modifier_id not in obj.modifier_ids:
            raise ValueError("The modifier must be attached to every selected Raster")
        prefix = obj.modifier_ids[:obj.modifier_ids.index(modifier_id) + 1]
        baked = [mid for mid in prefix if not chapter.modifiers[mid].muted]
        bounds = canvas.tiles.content_bounds(identifier) or QRectF(*obj.interaction_rect)
        if obj.modifier_source_frame is not None:
            bounds = bounds.united(QRectF(*obj.modifier_source_frame))
        bounds = aligned(bounds)
        image = empty_image(bounds)
        painter = QPainter(image)
        for (x, y), tile in canvas.tiles.iter_tiles(identifier):
            painter.drawImage(QPointF(x * obj.tile_size, y * obj.tile_size) - bounds.topLeft(), tile)
        painter.end()
        tiling = canvas._own_tiling(obj)
        placement = None
        if tiling and tiling.modifier_id in baked:
            world_image, world_bounds = canvas._tiling_stage(obj)
            world_image, world_bounds = render_stages(canvas, world_image, world_bounds,
                [chapter.modifiers[mid] for mid in baked if mid != tiling.modifier_id], QTransform(), nearest=True)
            mapping = canvas.layer_world_transform(obj.parent_layer_id)
            inverse, valid = mapping.inverted()
            if not valid:
                raise ValueError("Cannot apply tiling through a singular drawing transform")
            # Bake in document pixels to avoid two resamplings through an
            # existing projective raster transform.
            image, bounds = world_image, world_bounds
            placement = [inverse.map(p).toTuple() for p in
                (bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft())]
        else:
            image, bounds = render_stages(canvas, image, bounds, [chapter.modifiers[mid] for mid in baked], canvas._drawing_local_to_world_transform(obj), nearest=True)
        tiles = {}
        size = obj.tile_size
        for y in range(math.floor(bounds.top() / size), math.ceil(bounds.bottom() / size)):
            for x in range(math.floor(bounds.left() / size), math.ceil(bounds.right() / size)):
                tile = empty_image(QRectF(0, 0, size, size))
                painter = QPainter(tile)
                painter.drawImage(bounds.topLeft() - QPointF(x * size, y * size), image)
                painter.end()
                if canvas.tiles._alpha_bbox(tile) is not None:
                    tiles[x, y] = tile
        prepared.append((obj, baked, bounds, tiles, placement))
    identifiers = {identifier for _, identifier in targets}
    before = snapshot(canvas, identifiers)
    for obj, baked, bounds, tiles, placement in prepared:
        if placement is not None:
            obj.x, obj.y = 0., 0.
            obj.transform_frame = canvas._rect_signature(bounds)
            obj.transform_quad = placement
            obj.interaction_rect = canvas._rect_signature(bounds)
            obj.modifier_source_frame = canvas._rect_signature(bounds)
        if obj.modifier_source_frame is not None or any(
            isinstance(chapter.modifiers[mid], (RadialBlurModifier, ArrayModifier)) and not chapter.modifiers[mid].muted
            for mid in obj.modifier_ids
        ):
            obj.modifier_source_frame = canvas._rect_signature(bounds)
        canvas.tiles.replace_object_tiles(obj.object_id, tiles)
        obj.modifier_ids = [mid for mid in obj.modifier_ids if mid not in baked]
        obj.interaction_rect = canvas._rect_signature(QRectF(*obj.interaction_rect).united(bounds))
    chapter._garbage_collect_modifiers()
    commit(canvas, before, snapshot(canvas, identifiers), "Apply modifier prefix", targets, targets)
