"""Transactional, resource-aware flattening and Raster modifier prefix baking."""
from dataclasses import replace
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.assets import entity_visual_bounds
from comic_editor.core.commands import CallbackCommand
from comic_editor.core.document_patch import DocumentPatch
from comic_editor.core.changes import ResourceChange
from comic_editor.core.models import (ChildRef, ImageObject, LayerNode, ArrayModifier, ColorFillGradientObject,
    MirrorModifier, RasterObject, RadialBlurModifier, DistortModifier,
    HalftoneModifier, PixelateModifier)
from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.ui.effect_pipeline import empty_image


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
    result = entity_visual_bounds(chapter, canvas.tiles, kind, identifier,
                                  layer_mapping=canvas.layer_world_transform)
    if kind == "layer":
        for ref in target.children:
            result = result.united(visual_bounds(canvas, ref.kind, ref.entity_id))
    if canvas._own_tiling(target):
        result = canvas._tiling_boundary(target).boundingRect()
    parent = target.parent_id if kind == "layer" else target.parent_layer_id
    mapping = canvas.layer_world_transform(parent) if parent else QTransform()
    if isinstance(target, ColorFillGradientObject):
        result = mapping.mapRect(canvas._color_gradient_local_bounds(target))
    inverse, valid = mapping.inverted()
    if not valid:
        raise ValueError("Cannot bake a singular transform")
    return mapping.mapRect(effect_bounds(inverse.mapRect(result),
        canvas._active_modifier_instances(target.modifier_ids), mapping))


def after_state(canvas, before):
    from comic_editor.render.bake_sources import BakeState
    return BakeState(before.model.after(canvas.chapter),
        {identifier: canvas.images.source(identifier) for identifier in before.images},
        {identifier: canvas.tiles.object_tiles(identifier) for identifier in before.tiles},
        {identifier: dict(canvas.tiles._alpha_bounds.get(identifier, {})) for identifier in before.tiles})


def commit(canvas, before, after, label, selection_before, selection_after):
    old, new = DocumentPatch.pair(before.model, after.model)
    forward = canvas._history_change_with_bounds(new.change_set(old, label=label), old, new)
    resources = []
    for identifier in before.tiles.keys() | after.tiles.keys():
        for address in before.tiles.get(identifier, {}).keys() | after.tiles.get(identifier, {}).keys():
            resources.append(ResourceChange(('object', identifier), 'raster', address))
    for identifier in before.images.keys() | after.images.keys():
        if before.images.get(identifier) is not after.images.get(identifier):
            resources.append(ResourceChange(('object', identifier), 'image'))
    forward = replace(forward, resources=tuple(resources))
    def restore(state, patch, selection):
        canvas.images.apply_patch(state.images)
        for identifier, payload in state.tiles.items():
            canvas.tiles.replace_object_tiles(identifier, payload,
                                             alpha_bounds=state.alpha_bounds.get(identifier))
        canvas._restore_history_state(patch, document_patch=True)
        if selection:
            canvas.set_selection_set(selection, primary=selection[-1])
        canvas.update()
    canvas.command_stack.push(CallbackCommand(
        label, lambda: restore(after, new, selection_after), lambda: restore(before, old, selection_before),
        forward, forward.reversed(),
    ), already_done=True)
    canvas.active_modifier_id = ""
    canvas.set_selection_set(selection_after, primary=selection_after[-1])
    canvas._compound_path_cache.clear()
    canvas._emit_typed_hierarchy_changed(forward)
    canvas._emit_typed_document_changed(None, forward)
    canvas.update()


def rasterize(canvas, kind, identifier, *, prepared=None):
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
    if prepared is None:
        from comic_editor.render.bake_sources import prepare_rasterize
        from comic_editor.render.pixels import pixel_scope
        with pixel_scope(chapter.pixel_contract):
            prepared = prepare_rasterize(canvas, kind, identifier, image_factory=empty_image)
    bounds, image = prepared.bounds, prepared.image
    replacement = ImageObject(
        object_id=identifier, parent_layer_id=parent_id, name=target.name,
        custom_name=getattr(target, "custom_name", True), visible=target.visible,
        show_on_top=target.show_on_top,
        blend_mode=getattr(target, "blend_mode", "normal"),
        mask_only=target.mask_only, fill_reference=target.fill_reference,
        ignore_parent_mask=getattr(target, "ignore_parent_mask", False),
        geometry_reference=getattr(target, "geometry_reference", "direct"),
        underlay_opacity=getattr(target, "underlay_opacity", 0),
        source_filename="rasterized.png", source_mime_type="image/png",
        pixel_width=image.width(), pixel_height=image.height(),
        transform_frame=(0, 0, image.width(), image.height()),
        transform_quad=list(prepared.placement),
    )
    if prepared.history is None:
        from comic_editor.render.bake_sources import rasterize_history
        before = rasterize_history(canvas, kind, identifier)
    else:
        before = prepared.history
    if before.model.document_identity != id(chapter):
        raise ValueError('The rasterize source belongs to a retired document')
    selection = list(canvas.selected_entities)
    # Decode/validate before removing any graph or resource.
    canvas.images.put_decoded(identifier, "rasterized.png", prepared.encoded, image)
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
    commit(canvas, before, after_state(canvas, before), "Rasterize", selection, [("object", identifier)])


def apply_raster_modifiers(canvas, modifier_id, *, prepared=None):
    chapter = canvas.chapter
    targets = list(canvas.selected_entities)
    if not targets or any(kind != "object" or not isinstance(chapter.objects.get(identifier), RasterObject) for kind, identifier in targets):
        raise ValueError("Apply requires only Raster objects to be selected")
    stage = chapter.modifiers.get(modifier_id)
    if stage is None or stage.muted:
        raise ValueError("Unmute the modifier before applying it")
    for _, identifier in targets:
        if modifier_id not in chapter.objects[identifier].modifier_ids:
            raise ValueError("The modifier must be attached to every selected Raster")
    if prepared is None:
        from comic_editor.render.bake_sources import prepare_raster_modifiers
        from comic_editor.render.pixels import pixel_scope
        with pixel_scope(chapter.pixel_contract):
            prepared = prepare_raster_modifiers(canvas, modifier_id, targets)
    identifiers = {identifier for _, identifier in targets}
    before = getattr(prepared, 'history', None)
    if before is None:
        from comic_editor.render.bake_sources import raster_prefix_history
        before = raster_prefix_history(canvas, modifier_id, targets)
    if before.model.document_identity != id(chapter):
        raise ValueError('The modifier source belongs to a retired document')
    for source in prepared:
        obj = chapter.objects[source.identifier]
        baked, bounds, tiles, placement = source.baked, source.bounds, source.tiles, source.placement
        if placement is not None:
            obj.x, obj.y = 0., 0.
            obj.transform_frame = canvas._rect_signature(bounds)
            obj.transform_quad = placement
            obj.interaction_rect = canvas._rect_signature(bounds)
            obj.modifier_source_frame = canvas._rect_signature(bounds)
        if obj.modifier_source_frame is not None or any(
            isinstance(chapter.modifiers[mid], (RadialBlurModifier, ArrayModifier, HalftoneModifier, PixelateModifier, DistortModifier)) and not chapter.modifiers[mid].muted
            for mid in obj.modifier_ids
        ):
            obj.modifier_source_frame = canvas._rect_signature(bounds)
        canvas.tiles.replace_object_tiles(obj.object_id, tiles, alpha_bounds=source.alpha_bounds)
        obj.modifier_ids = [mid for mid in obj.modifier_ids if mid not in baked]
        obj.interaction_rect = canvas._rect_signature(QRectF(*obj.interaction_rect).united(bounds))
    chapter._garbage_collect_modifiers()
    commit(canvas, before, after_state(canvas, before), "Apply modifier prefix", targets, targets)


def request_rasterize(canvas, kind, identifier, finished):
    """Capture incrementally and publish one bake transaction on completion."""
    from comic_editor.render.bake_sources import rasterized_source
    from comic_editor.ui.scene_consumers import scene_consumers
    reason = rasterize_reason(canvas, kind, identifier)
    if reason:
        raise ValueError(reason)
    canvas._commit_text_edit()
    def accept(source, error):
        if error is None:
            try:
                rasterize(canvas, kind, identifier, prepared=source)
            except Exception as failure:
                error = failure
        finished(error)
    scene_consumers(canvas).request(('rasterize', kind, identifier), rasterized_source,
                                   (kind, identifier), accept)


def request_apply_raster_modifiers(canvas, modifier_id, finished):
    from comic_editor.render.bake_sources import applied_raster_sources
    from comic_editor.ui.scene_consumers import scene_consumers
    chapter, targets = canvas.chapter, tuple(canvas.selected_entities)
    if not targets or any(kind != 'object' or not isinstance(chapter.objects.get(identifier), RasterObject)
                          for kind, identifier in targets):
        raise ValueError('Apply requires only Raster objects to be selected')
    stage = chapter.modifiers.get(modifier_id)
    if stage is None or stage.muted:
        raise ValueError('Unmute the modifier before applying it')
    if any(modifier_id not in chapter.objects[identifier].modifier_ids for _, identifier in targets):
        raise ValueError('The modifier must be attached to every selected Raster')
    def accept(sources, error):
        if tuple(canvas.selected_entities) != targets:
            return
        if error is None:
            try:
                apply_raster_modifiers(canvas, modifier_id, prepared=sources)
            except Exception as failure:
                error = failure
        finished(error)
    scene_consumers(canvas).request(('apply-raster-prefix', modifier_id), applied_raster_sources,
                                   (modifier_id, targets), accept)
