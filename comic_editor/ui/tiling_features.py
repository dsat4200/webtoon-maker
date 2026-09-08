"""Canvas adapter for tile crops, repeated composites and their editing rig."""
import math
import copy
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QPainter, QPen, QTransform

from comic_editor.core.models import TilingModifier, LayerNode, RasterObject, VectorDrawingObject, new_id, StrokeModifier
from comic_editor.core.tiling import TilingGeometry, polygon_path
from comic_editor.ui.effect_pipeline import aligned, empty_image, render_stages
from comic_editor.ui.modifier_rendering import (
    _qimage_premultiplied, _premultiplied_qimage, _parameter_field, apply_opacity_mask,
)
from comic_editor.ui.tiling_rendering import repeat_image, RepeatMapCache


class TilingFeatures:
    def _own_tiling(self, target):
        if not self.chapter or target is None:
            return None
        return next((m for mid in target.modifier_ids
                     if isinstance(m := self.chapter.modifiers.get(mid), TilingModifier)
                     and not m.muted and (m.intensity > 0 or any(
                         max(b.black_value, b.white_value) > 0 for b in m.parameter_masks.values()))), None)

    def _drawing_tiling(self, drawing):
        if drawing is None or self.active_tone_mask_id:
            return None
        modifier = self._own_tiling(drawing)
        if modifier:
            return drawing, modifier
        for layer in self.chapter.ancestor_layers(drawing.parent_layer_id):
            modifier = self._own_tiling(layer)
            if modifier:
                return layer, modifier
        return None

    def _tiling_boundary(self, target):
        layer = target if isinstance(target, LayerNode) else self.chapter.layers.get(target.parent_layer_id)
        while layer is not None and layer.bound is None:
            layer = self.chapter.layers.get(layer.parent_id)
        if layer is not None:
            return self.layer_world_transform(layer.layer_id).map(self.layer_effective_path(layer.layer_id))
        from PySide6.QtGui import QPainterPath
        path = QPainterPath()
        path.addRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        return path

    def _tiling_accepts_point(self, drawing, point):
        context = self._drawing_tiling(drawing)
        return bool(context and self._tiling_boundary(context[0]).contains(point))

    def _tiling_brush_context(self, drawing):
        context = self._drawing_tiling(drawing)
        if context:
            return TilingGeometry.from_modifier(context[1]), self._drawing_local_to_world_transform(drawing)
        return None

    def _tiling_default_bounds(self, targets):
        bounds = QRectF()
        for kind, identifier in targets:
            target = self.chapter.modifier_target(kind, identifier)
            if target is None:
                continue
            if isinstance(target, RasterObject):
                content = self.tiles.content_bounds(identifier)
                candidate = self._drawing_local_rect_to_world(target, content) if content is not None else self.entity_world_rect("layer", target.parent_layer_id)
            elif isinstance(target, VectorDrawingObject) and not any(s.points for s in target.strokes):
                candidate = self.entity_world_rect("layer", target.parent_layer_id)
            else:
                candidate = self.entity_world_rect(kind, identifier)
            if candidate is not None:
                bounds = candidate if bounds.isEmpty() else bounds.united(candidate)
        return bounds if not bounds.isEmpty() else QRectF(0, 0, self.chapter.width, self.chapter.height)

    def _tiling_fill_frame(self, obj, profile):
        context = profile.get("_tiling_context")
        if context:
            geometry, mapping = context
            return mapping.inverted()[0].map(geometry.path()).boundingRect()
        return QRectF(*obj.interaction_rect)

    def _tile_vector_stroke(self, stroke, context):
        from comic_editor.core.vector_geometry import stroke_cubics, CubicSpan
        geometry, mapping = context
        inverse = mapping.inverted()[0]
        world_bounds = mapping.mapRect(QRectF(*stroke.derived_bounds()))
        clip = [inverse.map(QPointF(*p)).toTuple() for p in geometry.vertices()]
        cubics = stroke_cubics(stroke.points, stroke.closed)
        radius = max(p.width for p in stroke.points)/2+1
        result = []
        for _, transport, cell_path in geometry.cells_intersecting(world_bounds):
            local_cell = inverse.map(cell_path)
            groups, current = [], []
            for index, cubic in enumerate(cubics):
                xs, ys = [p[0] for p in cubic], [p[1] for p in cubic]
                bbox = QRectF(min(xs)-radius, min(ys)-radius, max(xs)-min(xs)+2*radius, max(ys)-min(ys)+2*radius)
                if local_cell.intersects(bbox):
                    current.append(CubicSpan(cubic, index, 0., 1.))
                elif current:
                    groups.append(current)
                    current = []
            if current:
                groups.append(current)
            sources = [self._stroke_from_spans(stroke, group) for group in groups] if cubics else [copy.deepcopy(stroke)]
            transform = mapping*transport*inverse
            for fragment in sources:
                if fragment is None:
                    continue
                if not transform.isAffine():
                    from comic_editor.ui.cage_vectors import warp_vector
                    from comic_editor.core.tiling import map_arrays
                    wrapper = VectorDrawingObject(strokes=[fragment])
                    fragment = warp_vector(wrapper, None, QTransform(), point_mapper=lambda points:
                        np.column_stack(map_arrays(transform, points[:, 0], points[:, 1]))).strokes[0]
                fragment.stroke_id = new_id()
                fragment.tiling_group = stroke.tiling_group or stroke.stroke_id
                fragment.clip_polygon = clip
                for point in fragment.points if transform.isAffine() else ():
                    origin = QPointF(*point.position)
                    a = transform.map(origin+QPointF(.01, 0))-transform.map(origin)
                    b = transform.map(origin+QPointF(0, .01))-transform.map(origin)
                    point.width *= math.sqrt(abs(a.x()*b.y()-a.y()*b.x()))/.01
                    point.position = transform.map(origin).toTuple()
                    if point.incoming is not None:
                        point.incoming = transform.map(QPointF(*point.incoming)).toTuple()
                    if point.outgoing is not None:
                        point.outgoing = transform.map(QPointF(*point.outgoing)).toTuple()
                result.append(fragment)
        return result

    def _render_tiled_vector_group(self, painter, drawing, group):
        originals = [s for s in drawing.strokes if s.tiling_group == group]
        strokes = []
        for stroke in originals:
            if self._vector_gesture_mode == "eraser" and drawing.object_id == self.selected_object_id:
                strokes.extend(self._vector_eraser_preview.get(stroke.stroke_id, [stroke]))
            else:
                strokes.append(self._vector_stroke_with_selection_preview(stroke))
        bounds = QRectF()
        rendered = []
        for stroke in strokes:
            item = self._vector_stroke_image(drawing, stroke, cache_token=repr(stroke.to_dict()))
            if item is not None:
                rendered.append(item)
                bounds = item[1] if bounds.isEmpty() else bounds.united(item[1])
        if not rendered:
            return
        bounds = aligned(bounds)
        image = empty_image(bounds)
        source = QPainter(image)
        source.setCompositionMode(QPainter.CompositionMode_Lighten)
        for item, rect in rendered:
            source.drawImage(rect.translated(-bounds.topLeft()), item)
        source.end()
        painter.drawImage(bounds.topLeft(), image)

    def _wrapped_vector_eraser_preview(self, drawing):
        context = getattr(self, "_tiling_eraser_context", None)
        if context is None or getattr(self, "_tiling_eraser_transport", False):
            return False
        from comic_editor.core.vector_geometry import FreehandSample
        geometry, mapping = context
        sweep = self._vector_sweep
        if not sweep:
            return True
        points = sweep[-2:]
        radius = self.settings.active_eraser_pixels()/2
        xs, ys = [s.x for s in points], [s.y for s in points]
        bounds = mapping.mapRect(QRectF(min(xs)-radius, min(ys)-radius,
            max(xs)-min(xs)+2*radius, max(ys)-min(ys)+2*radius))
        self._tiling_eraser_transport = True
        try:
            for _, transport, _ in geometry.cells_intersecting(bounds):
                local = mapping*transport*mapping.inverted()[0]
                self._vector_sweep = [FreehandSample(*local.map(QPointF(s.x, s.y)).toTuple(), s.pressure) for s in sweep]
                self._update_vector_eraser_preview(drawing)
            if self.settings.vector_eraser_mode == "stroke":
                groups = {s.tiling_group for s in drawing.strokes if s.tiling_group
                          and s.stroke_id in self._vector_eraser_preview and not self._vector_eraser_preview[s.stroke_id]}
                for stroke in drawing.strokes:
                    if stroke.tiling_group in groups:
                        self._vector_eraser_preview[stroke.stroke_id] = []
        finally:
            self._vector_sweep = sweep
            self._tiling_eraser_transport = False
        return True

    def _tiling_children(self, painter, layer, visible):
        mapping = self.layer_world_transform(layer.layer_id)
        local_visible = mapping.inverted()[0].mapRect(visible)
        painter.save()
        painter.setTransform(mapping, True)
        old_reference = self._rendering_compound_references
        self._rendering_compound_references = True
        try:
            self._render_outward_gradient_children(painter, layer, 1., local_visible)
            for ref in reversed(layer.children):
                if ref.kind == "object":
                    self._render_object(painter, self.chapter.objects[ref.entity_id], 1., local_visible)
                else:
                    child = self.chapter.layers[ref.entity_id]
                    if layer.compound_enabled and child.compound_operation != "ignore":
                        self._render_compound_contributor(painter, child, 1., visible)
                    else:
                        self._render_layer(painter, child, 1., visible)
            drawing = self._active_vector_drawing()
            if drawing is not None and not self._has_active_modifiers(drawing.modifier_ids):
                if any(p.layer_id == layer.layer_id for p in self.chapter.ancestor_layers(drawing.parent_layer_id)):
                    # The preview routine consumes the drawing parent's coordinates.
                    painter.save()
                    painter.setTransform(self.layer_world_transform(drawing.parent_layer_id) * mapping.inverted()[0], True)
                    self._render_modified_vector_pencil_preview(painter, drawing.parent_layer_id)
                    painter.restore()
        finally:
            self._rendering_compound_references = old_reference
            painter.restore()

    def _tiling_source(self, target, bounds):
        layer = isinstance(target, LayerNode)
        kind, identifier = ("layer", target.layer_id) if layer else ("object", target.object_id)
        signature = self._modifier_layer_signature(identifier) if layer else self._modifier_object_signature(target)
        key = ("tiling-source", kind, identifier, repr(signature), self._rect_signature(bounds),
               self._render_exclude_text, self._render_base_alpha, self._render_excluded_object_id,
               self._rendering_mask_contributor, self._rendering_compound_references)
        cached = self._modifier_source_cache_get(key)
        if cached is not None:
            return cached
        image = empty_image(bounds)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        self._render_modifier_sources.add((kind, identifier))
        previous_scale = self._vector_render_scale_override
        previous_geometry = getattr(self, "_tiling_capture_geometry", None)
        if layer:
            self._tiling_capture_geometry = TilingGeometry.from_modifier(self._own_tiling(target))
        self._vector_render_scale_override = 1.
        try:
            if layer:
                self._tiling_children(painter, target, bounds)
            else:
                mapping = self.layer_world_transform(target.parent_layer_id)
                painter.setTransform(mapping, True)
                self._render_object_content(painter, target, mapping.inverted()[0].mapRect(bounds))
                if isinstance(target, VectorDrawingObject):
                    self._render_modified_vector_pencil_preview(painter, target.parent_layer_id)
        finally:
            self._vector_render_scale_override = previous_scale
            self._tiling_capture_geometry = previous_geometry
            painter.end()
            self._render_modifier_sources.discard((kind, identifier))
        self._modifier_source_cache_put(key, image)
        return image

    def _tiling_raster_pixels(self, obj, geometry, world_bounds=None):
        destination = self._multi_transform_preview_quads.get(obj.object_id)
        if destination is None and obj.object_id == self.selected_object_id:
            destination = self._transform_preview_quad
        mapping = QTransform.fromTranslate(obj.x, obj.y)*self._drawing_object_transform(obj, destination)*self.layer_world_transform(obj.parent_layer_id)
        inverse = mapping.inverted()[0]
        bounds = inverse.map(geometry.path()).boundingRect()
        if world_bounds is not None:
            bounds = bounds.united(inverse.mapRect(world_bounds))
        bounds = aligned(bounds.adjusted(-2, -2, 2, 2))
        key = ("tile-raster-pixels", repr(self._modifier_object_signature(obj)), self._rect_signature(bounds))
        image = self._modifier_source_cache_get(key)
        if image is None:
            image = empty_image(bounds)
            painter = QPainter(image)
            painter.translate(-bounds.x(), -bounds.y())
            try:
                if not self._render_raster_selection_preview(painter, obj, bounds):
                    for (x, y), tile in self.tiles.iter_tiles(obj.object_id, bounds):
                        painter.drawImage(x*obj.tile_size, y*obj.tile_size, tile)
            finally:
                painter.end()
            self._modifier_source_cache_put(key, image)
        return image, bounds, mapping

    def _render_tiling_raster_capture(self, painter, obj, local_visible):
        geometry = getattr(self, "_tiling_capture_geometry", None)
        if geometry is None:
            return False
        parent = self.layer_world_transform(obj.parent_layer_id)
        bounds = aligned(local_visible)
        source, source_bounds, mapping = self._tiling_raster_pixels(obj, geometry, parent.mapRect(bounds))
        if not hasattr(self, "_tiling_sampling_cache"):
            self._tiling_sampling_cache = RepeatMapCache()
        image = repeat_image(source, source_bounds, geometry, bounds, mapping, nearest=True,
            output_to_world=parent, repeat=False, cache=self._tiling_sampling_cache)
        painter.drawImage(bounds.topLeft(), image)
        return True

    def _tiling_shape_style(self, painter, layer, *, outline):
        from comic_editor.ui.shape_outline import outline_mesh
        path = self.layer_effective_path(layer.layer_id)
        painter.save()
        painter.setTransform(self.layer_world_transform(layer.layer_id), True)
        if outline and layer.border_width > 0 and getattr(self, "_stroke_hide_border_id", None) != layer.layer_id:
            if layer.compound_enabled:
                coverage = self._compound_outline_mesh(layer, path, self._outline_tolerance(painter, path.boundingRect()))
            elif layer.layer_kind == "open_shape":
                style = layer.shape_style
                core = self.open_shape_mesh(layer.bound, style.base_thickness, 0, style.start_cap, style.end_cap, cache=self._outline_cache)
                coverage = outline_mesh(layer.bound, layer.border_width, path, core=core,
                    base_width=style.base_thickness, cache=self._outline_cache,
                    start_cap=style.start_cap, end_cap=style.end_cap)
            else:
                coverage = outline_mesh(layer.bound, layer.border_width, path, cache=self._outline_cache)
            painter.fillPath(coverage, QColor(layer.border_color))
        elif not outline:
            if layer.layer_kind == "open_shape":
                style = layer.shape_style
                core = self.open_shape_mesh(layer.bound, style.base_thickness, 0, style.start_cap, style.end_cap, cache=self._outline_cache)
                painter.fillPath(core, QColor(style.primary_color))
            elif layer.fill_color:
                painter.fillPath(path, QColor(layer.fill_color))
        painter.restore()

    def _tiling_stage(self, target, required=None):
        modifier = self._own_tiling(target)
        geometry = TilingGeometry.from_modifier(modifier)
        boundary = self._tiling_boundary(target)
        bounds = aligned(boundary.boundingRect())
        if required is not None:
            bounds = aligned(bounds.intersected(required))
        source_bounds = aligned(geometry.bounds().adjusted(-2, -2, 2, 2))
        source_mapping = QTransform()
        if isinstance(target, RasterObject):
            source, source_bounds, source_mapping = self._tiling_raster_pixels(target, geometry)
        else:
            source = self._tiling_source(target, source_bounds)
        key = ("tiling-output", source.cacheKey(), repr(modifier.to_dict()), self._rect_signature(bounds), getattr(self, "_stroke_hide_border_id", None),
               self._modifier_parameter_signature([modifier.modifier_id]),
               repr(target.to_dict()), boundary)
        # QPainterPath isn't hashable; serialize its geometry into the key.
        key = (*key[:-1], tuple((boundary.elementAt(i).x, boundary.elementAt(i).y, boundary.elementAt(i).type)
                               for i in range(boundary.elementCount())))
        cached = self._modifier_cache_get(key)
        if cached is not None:
            return cached, bounds
        if not hasattr(self, "_tiling_sampling_cache"):
            self._tiling_sampling_cache = RepeatMapCache()
        repeated = repeat_image(source, source_bounds, geometry, bounds, source_mapping,
            nearest=isinstance(target, RasterObject), output_to_world=QTransform(), cache=self._tiling_sampling_cache)
        fields = self._modifier_mask_fields([modifier], repeated.width(), repeated.height(),
            QTransform.fromTranslate(-bounds.x(), -bounds.y()), bounds)
        amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity,
            (repeated.height(), repeated.width()), fields))/100
        if amount.ndim == 2:
            amount = amount[..., None]
        if np.ndim(amount) or float(amount) != 1:
            incoming = self._tiling_source(target, bounds)
            repeated = _premultiplied_qimage(_qimage_premultiplied(incoming)*(1-amount)
                                            + _qimage_premultiplied(repeated)*amount)
        result = empty_image(bounds)
        painter = QPainter(result)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        if isinstance(target, LayerNode):
            self._tiling_shape_style(painter, target, outline=False)
        painter.save()
        painter.setClipPath(boundary)
        painter.drawImage(bounds.topLeft(), repeated)
        painter.restore()
        if isinstance(target, LayerNode):
            self._tiling_shape_style(painter, target, outline=True)
        painter.end()
        self._modifier_cache_put(key, result)
        return result, bounds

    def _render_tiled_target(self, painter, target, parent_opacity, visible_world):
        modifier = self._own_tiling(target)
        if modifier is None:
            return False
        rest = [m for m in self._active_modifier_instances(target.modifier_ids,
            suppress_outline=self._suppress_outline_for_mask) if not isinstance(m, TilingModifier)]
        if self._render_base_alpha:
            rest = []
        # Other spatial effects may pull any part of the finite tiled fill.
        image, bounds = self._tiling_stage(target, None if rest else visible_world)
        kind, identifier = ("layer", target.layer_id) if isinstance(target, LayerNode) else ("object", target.object_id)
        if rest:
            scope = (kind, identifier, getattr(self, "_effect_preview_channel", "canvas"))
            if any(isinstance(effect, StrokeModifier) for effect in rest):
                from comic_editor.ui.stroke_rendering import render_stroke_stack
                image, bounds = render_stroke_stack(self, target, image, bounds, rest,
                    QTransform(), ("tiled-stroke", image.cacheKey()), scope, tiled=True)
            else:
                image, bounds = render_stages(self, image, bounds, rest, QTransform(),
                    nearest=isinstance(target, RasterObject), required=visible_world, request_scope=scope)
        if target.opacity_mask is not None:
            binding = target.opacity_mask
            image = apply_opacity_mask(image, self.render_tone_mask_field(binding.mask_id,
                image.width(), image.height(), QTransform.fromTranslate(-bounds.x(), -bounds.y()), bounds),
                binding.black_value, binding.white_value)
        parent = target.parent_id if isinstance(target, LayerNode) else target.parent_layer_id
        mapping = self.layer_world_transform(parent) if parent else QTransform()
        opacity = target.opacity if isinstance(target, LayerNode) or not target.opacity_locked else 1.
        if self._render_base_alpha:
            opacity = 1.
        painter.save()
        painter.setTransform(mapping.inverted()[0], True)
        painter.setOpacity(parent_opacity*opacity)
        painter.drawImage(bounds.topLeft(), image)
        painter.restore()
        return True

    def _selected_tiling(self):
        modifier = self.chapter.modifiers.get(self.active_modifier_id) if self.chapter else None
        if self.modifier_mode and isinstance(modifier, TilingModifier) and not modifier.muted and any(
                ref in self.selected_entities for ref in self.chapter.modifier_target_ids(modifier.modifier_id)):
            return modifier, True
        drawing = self.chapter.objects.get(self.selected_object_id) if self.chapter else None
        if isinstance(drawing, (RasterObject, VectorDrawingObject)):
            context = self._drawing_tiling(drawing)
            if context:
                return context[1], False
        return None, False

    def _tiling_handles(self, modifier):
        geometry = TilingGeometry.from_modifier(modifier)
        center = self.document_to_widget(QPointF(*modifier.center))
        scale = self.document_to_widget(QPointF(*geometry.vertices()[0]))
        # Rotation handle uses the tile's actual transformed top edge direction.
        top = self.document_to_widget(geometry.transform().map(QPointF(0, -.8)))
        delta = top-center
        length = math.hypot(delta.x(), delta.y())
        rotate = center+delta*((max(48., length)+24)/max(length, 1e-6))
        return center, scale, rotate

    def _draw_tiling_handles(self, painter):
        modifier, handles = self._selected_tiling()
        if modifier is None:
            return
        geometry = TilingGeometry.from_modifier(modifier)
        painter.save()
        painter.setTransform(QTransform())
        path = self.camera_transform().map(geometry.path())
        painter.setPen(QPen(QColor("#ff8b26"), 1.5, Qt.DotLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(path)
        if handles:
            center, scale, rotate = self._tiling_handles(modifier)
            painter.drawLine(center, rotate)
            for point in (center, scale, rotate):
                painter.setBrush(QColor("#ff8b26"))
                painter.drawEllipse(point, 6, 6)
        painter.restore()

    def _begin_tiling_handle(self, point):
        modifier, handles = self._selected_tiling()
        if not handles:
            return False
        hit = next((i for i, handle in enumerate(self._tiling_handles(modifier))
                    if math.dist(handle.toTuple(), point.toTuple()) <= 12), None)
        if hit is None:
            return False
        self._modifier_handle_drag = {"tiling": modifier.modifier_id, "handle": hit,
            "before": self.chapter.to_dict(), "initial": copy.deepcopy(modifier),
            "press": self.widget_to_document(point)}
        if not hasattr(self, "_tiling_handle_timer"):
            self._tiling_handle_timer = QTimer(self)
            self._tiling_handle_timer.setSingleShot(True)
            self._tiling_handle_timer.timeout.connect(self._flush_tiling_handle)
        self._tiling_handle_pending = None
        return True

    def _move_tiling_handle(self, point):
        if not self._modifier_handle_drag or "tiling" not in self._modifier_handle_drag:
            return False
        self._tiling_handle_pending = QPointF(point)
        if not self._tiling_handle_timer.isActive():
            self._tiling_handle_timer.start(16)
        return True

    def _flush_tiling_handle(self):
        point = getattr(self, "_tiling_handle_pending", None)
        if point is None:
            return False
        self._tiling_handle_timer.stop()
        self._tiling_handle_pending = None
        state = self._modifier_handle_drag
        if not state or "tiling" not in state:
            return False
        modifier = self.chapter.modifiers[state["tiling"]]
        initial = state["initial"]
        current = self.widget_to_document(point)
        center = QPointF(*initial.center)
        if state["handle"] == 0:
            modifier.center = self._snap(center+current-state["press"], self.active_layer_id).toTuple()
        elif state["handle"] == 1:
            modifier.side = max(1., initial.side*math.dist(current.toTuple(), initial.center)
                / max(1e-9, math.dist(state["press"].toTuple(), initial.center)))
        else:
            first, last = state["press"]-center, current-center
            modifier.rotation = initial.rotation+math.degrees(math.atan2(last.y(), last.x())-math.atan2(first.y(), first.x()))
        modifier.validate()
        self._invalidate_scene_cache()
        self.documentChanged.emit(None)
        self.update()
        return True
