"""Conservative viewport bounds, independent of document visibility and pixels.

Unknown footprints deliberately remain renderable. Exact local eyedropper
samples may opt in without interactive drafts. Effect source captures and
exports do not use this cache: dependencies can be outside the requested area.
"""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QPolygonF, QTransform

from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.core.models import (
    BlurModifier, BrightnessContrastModifier, CurvesModifier, GradientObject, HalftoneModifier, ImageObject, LayerNode,
    OutlineModifier, PixelateModifier, RasterObject, TextObject, VectorDrawingObject,
)


class SceneRenderBounds:
    def __init__(self, canvas):
        self.canvas = canvas
        self.document = None
        self.tiles = None
        self.bounds: dict[tuple[str, str], QRectF | None] = {}
        self.transforms: dict[str, QTransform] = {}
        self.live_branches: set[tuple[str, str]] = set()
        self.prepared_preview_targets = None
        self.enabled = False
        self.exact_sampling = False
        self.margin = 2.0

    def clear(self, *_args):
        self.bounds.clear()
        self.transforms.clear()

    def invalidate(self, kind, identifier):
        if kind == "layer":
            # A moved layer also changes every descendant's world transform.
            self.clear()
            return
        self.bounds.pop((kind, identifier), None)
        obj = self.canvas.chapter.objects.get(identifier) if self.canvas.chapter else None
        parent_id = obj.parent_layer_id if obj is not None else None
        while parent_id:
            self.bounds.pop(("layer", parent_id), None)
            parent_id = self.canvas.chapter.layers[parent_id].parent_id

    def prepare(self):
        canvas = self.canvas
        if self.document is not canvas.chapter or self.tiles is not canvas.tiles:
            self.document, self.tiles = canvas.chapter, canvas.tiles
            self.clear()
        preview_targets = self._preview_targets()
        self.prepared_preview_targets = preview_targets
        # Transform previews only change their target branches. Their old
        # bounds stay cached, but are never used until the preview ends.
        self.enabled = not (
            preview_targets is None
            or canvas._cage_session is not None
            or canvas._page_gap_draft is not None
        )
        self.margin = 2.0 / max(canvas.scale, 0.001)
        self.live_branches.clear()
        # Live ink/eraser/selection overlays need not yet be in the model.
        # Retain their branch without rebuilding all static siblings per dab.
        kind, identifier = canvas.selected_kind, (
            canvas.selected_object_id if canvas.selected_kind == "object"
            else canvas.selected_id
        )
        retained = [(kind, identifier)] if identifier else []
        retained.extend(("object", object_id) for object_id in
                        canvas.__dict__.get("_selection_raster_states", {}))
        retained.extend(preview_targets or ())
        pending = [key for key in preview_targets or () if key[0] == "layer"]
        for affected_kind, affected_id in retained if canvas.chapter is not None else []:
            entity = (canvas.chapter.layers if affected_kind == "layer" else
                      canvas.chapter.objects).get(affected_id)
            layer = (entity if affected_kind == "layer" else
                     canvas.chapter.layers.get(entity.parent_layer_id)
                     if entity is not None else None)
            while layer is not None:
                if layer.compound_enabled:
                    # A changed operand can move sibling images fitted to
                    # the effective compound, including nested compounds.
                    pending.append(("layer", layer.layer_id))
                layer = canvas.chapter.layers.get(layer.parent_id)
        if pending:
            seen = set()
            while pending:
                key = pending.pop()
                if key in seen:
                    continue
                seen.add(key)
                retained.append(key)
                if key[0] == "layer":
                    layer = canvas.chapter.layers.get(key[1])
                    if layer is not None:
                        pending.extend((child.kind, child.entity_id) for child in layer.children)
        for kind, identifier in retained if canvas.chapter is not None else []:
            self.live_branches.add((kind, identifier))
            entity = (canvas.chapter.objects if kind == "object" else
                      canvas.chapter.layers).get(identifier)
            parent_id = (entity.parent_layer_id if kind == "object" else
                         entity.parent_id) if entity is not None else None
            while parent_id:
                self.live_branches.add(("layer", parent_id))
                parent_id = canvas.chapter.layers[parent_id].parent_id

    def _preview_targets(self):
        """None means an unknown preview; an empty set means no transform."""
        canvas = self.canvas
        targets = {("object", identifier) for identifier in canvas._multi_transform_preview_quads}
        if canvas._selection_transform_quad is not None:
            targets.update(("object", identifier) for identifier in
                           canvas.__dict__.get("_selection_raster_states", {}))
        if canvas._transform_preview_quad is not None:
            target = canvas._geometry_transform_target
            if target is None:
                targets.add(("object", canvas.selected_object_id))
            elif target[0] in {"layer", "layer_group"}:
                targets.add(("layer", target[1]))
            elif target[0] == "object":
                targets.add(target)
            elif target[0] == "multi":
                targets.update(("object", identifier)
                               for identifier in canvas._multi_transform_start_world_quads)
                targets.update(canvas.selected_entities)
            else:
                return None
            if not targets:
                return None
        for kind, identifier in targets:
            if canvas.chapter is None or kind not in {"object", "layer"}:
                return None
            if identifier not in (canvas.chapter.objects if kind == "object" else canvas.chapter.layers):
                return None
        return frozenset(targets)

    def usable(self):
        canvas = self.canvas
        return bool(
            self.enabled and (canvas._interactive_render or self.exact_sampling)
            # A selection/preview may start before the next full scene paint.
            # Captured backgrounds must not use unprepared target bounds.
            and self._preview_targets() == self.prepared_preview_targets
            and canvas._cage_session is None
            and canvas._page_gap_draft is None
            and canvas.chapter is self.document and canvas.tiles is self.tiles
            and not canvas._render_modifier_sources
            and not canvas._render_base_alpha
            and not canvas._render_cage_source
            and not canvas._rendering_mask_contributor
            and not canvas._rendering_compound_references
            and not getattr(canvas, "_rendering_halftone_source", False)
            and getattr(canvas, "_tiling_capture_geometry", None) is None
        )

    def _transform(self, layer_id):
        # Bounds are bypassed for live branches, but inherited clip rejection
        # also maps the requested region through its parent's transform. That
        # mapping must follow the preview without replacing the committed
        # cached matrix needed when a drag is canceled.
        preview = self.canvas._geometry_transform_target
        if (preview is not None and preview[0] == "layer_group"
                and ("layer", layer_id) in self.live_branches):
            return self.canvas.layer_world_transform(layer_id)
        if layer_id not in self.transforms:
            self.transforms[layer_id] = self.canvas.layer_world_transform(layer_id)
        return self.transforms[layer_id]

    @staticmethod
    def _mapped(mapping, bounds):
        if bounds is None or not mapping.isAffine():
            return None
        result = mapping.mapRect(bounds)
        return result if all(math.isfinite(v) for v in result.getRect()) else None

    def layer_visible(self, layer, visible_world):
        key = ("layer", layer.layer_id)
        if not self.usable() or key in self.live_branches:
            return True
        bounds = self.entity_bounds(*key)
        return bounds is None or bounds.intersects(visible_world.adjusted(
            -self.margin, -self.margin, self.margin, self.margin))

    def object_visible(self, obj, local_visible):
        key = ("object", obj.object_id)
        if not self.usable() or key in self.live_branches:
            return True
        bounds = self.entity_bounds(*key)
        if bounds is None:
            return True
        visible = self._mapped(self._transform(obj.parent_layer_id), local_visible)
        return visible is None or bounds.intersects(visible.adjusted(
            -self.margin, -self.margin, self.margin, self.margin))

    def painter_clip_visible(self, painter, local_visible):
        """Reject work already excluded by an inherited scene clip.

        A parent can remain visible because one child escapes its mask, while
        ordinary siblings (including effects with unknown bounds) are wholly
        clipped. Compare conservative rectangles in device coordinates so the
        antialiasing guard remains two pixels under scaled/rotated parents.
        Independent effect-source captures must still render their full input.
        """
        if not self.usable() or not painter.hasClipping():
            return True
        mapping = painter.combinedTransform()
        clip = self._mapped(mapping, painter.clipBoundingRect())
        visible = self._mapped(mapping, local_visible)
        if clip is None or visible is None:
            return True
        return clip.intersects(visible.adjusted(-2., -2., 2., 2.))

    def layer_clip_visible(self, painter, layer, visible_world):
        # Before _render_layer applies its own transform, the painter uses the
        # parent's logical coordinates. Escaped children arrive after that
        # parent's clip is restored, so this never reinstates an ignored mask.
        if not self.usable() or not painter.hasClipping():
            return True
        parent = self._transform(layer.parent_id) if layer.parent_id else QTransform()
        inverse, valid = parent.inverted()
        visible = self._mapped(inverse, visible_world) if valid else None
        return visible is None or self.painter_clip_visible(painter, visible)

    def entity_bounds(self, kind, identifier):
        key = (kind, identifier)
        if key in self.bounds:
            return self.bounds[key]
        target = (self.document.layers if kind == "layer" else
                  self.document.objects).get(identifier)
        if target is None:
            return None
        # Seed before recursion; malformed/transient cyclic hierarchies must
        # fall back to rendering rather than recurse in the visibility query.
        self.bounds[key] = None
        modifiers = self.canvas._active_modifier_instances(target.modifier_ids)
        from comic_editor.core.models import KuwaharaModifier, DitheringModifier, SharpnessModifier
        bounded_effects = (BlurModifier, BrightnessContrastModifier, CurvesModifier, OutlineModifier,
                           KuwaharaModifier, DitheringModifier, SharpnessModifier)
        if isinstance(target, ImageObject):
            # These image effects are clipped to the complete source image
            # frame. They cannot bring a distant image into the current view.
            bounded_effects += (HalftoneModifier, PixelateModifier)
        if any(not isinstance(modifier, bounded_effects)
               for modifier in modifiers):
            return None
        if kind == "layer":
            result = self._layer_bounds(target)
            parent_id = target.parent_id
        else:
            result = self._object_bounds(target)
            parent_id = target.parent_layer_id
        if result is not None and modifiers:
            mapping = self._transform(parent_id) if parent_id else QTransform()
            inverse, valid = mapping.inverted()
            result = (self._mapped(mapping, effect_bounds(
                inverse.mapRect(result), modifiers, mapping))
                if valid and mapping.isAffine() else None)
        self.bounds[key] = result
        return result

    def _layer_bounds(self, layer: LayerNode):
        if layer.compound_enabled:
            # The composed fill already includes transformed operands, tails,
            # cuts and mirrors. Its attributed outline is clipped to this same
            # path. Escaped children are added below like ordinary layers.
            local = self.canvas.layer_effective_path(layer.layer_id).controlPointRect()
            result = self._mapped(self._transform(layer.layer_id), local)
            if result is None:
                return None
        elif layer.bound is None:
            result = QRectF()
        else:
            local = QRectF(*layer.bound.bbox())
            if layer.layer_kind == "open_shape":
                style = layer.shape_style
                from comic_editor.ui.shape_outline import core_mesh, outline_mesh
                core = core_mesh(layer.bound, style.base_thickness, 0,
                    style.start_cap, style.end_cap, cache=self.canvas._outline_cache)
                clip = core_mesh(layer.bound, style.base_thickness,
                    style.outline_thickness * 2, style.start_cap, style.end_cap,
                    cache=self.canvas._outline_cache)
                outline = outline_mesh(layer.bound, style.outline_thickness,
                    clip, core=core, base_width=style.base_thickness,
                    start_cap=style.start_cap, end_cap=style.end_cap,
                    cache=self.canvas._outline_cache)
                local = core.controlPointRect().united(clip.controlPointRect()).united(
                    outline.controlPointRect()).adjusted(-2, -2, 2, 2)
            result = self._mapped(self._transform(layer.layer_id), local)
            if result is None:
                return None
        for child in layer.children:
            target = (self.document.layers if child.kind == "layer" else
                      self.document.objects).get(child.entity_id)
            if target is None:
                continue
            outward = isinstance(target, GradientObject) and self.canvas._is_outward_gradient(target)
            if layer.bound is not None and layer.layer_kind != "text_container" \
                    and not target.ignore_parent_mask and not outward:
                # Ordinary descendants are clipped to this layer. Their own
                # masks, repeats and effects cannot escape that inherited clip.
                continue
            child_bounds = self.entity_bounds(child.kind, child.entity_id)
            if child_bounds is None:
                return None
            result = result.united(child_bounds)
        return result

    def _object_bounds(self, obj):
        canvas = self.canvas
        if isinstance(obj, RasterObject):
            if obj.modifier_source_frame is not None:
                return None
            content = canvas.tiles.content_bounds(obj.object_id)
            local = (content if content is not None else
                     QRectF(*obj.interaction_rect)).translated(obj.x, obj.y)
            if obj.transform_quad is not None:
                local = self._mapped(canvas._drawing_object_transform(obj), local)
        elif isinstance(obj, VectorDrawingObject):
            local = QRectF(*obj.derived_bounds()).translated(obj.x, obj.y)
            if obj.transform_quad is not None:
                local = self._mapped(canvas._drawing_object_transform(obj), local)
        elif isinstance(obj, ImageObject):
            local = QPolygonF([QPointF(*point) for point in
                               canvas._image_model_local_quad(obj)]).boundingRect()
        elif isinstance(obj, TextObject):
            local = (canvas._strict_text_rect(obj) if obj.layout_mode == "strict"
                     else QPolygonF([QPointF(*point) for point in
                                     canvas._text_quad(obj)]).boundingRect())
        else:
            # A gradient's field frame is not its painted extent (outward and
            # compound-referenced gradients are important examples).
            return None
        return self._mapped(self._transform(obj.parent_layer_id), local)
