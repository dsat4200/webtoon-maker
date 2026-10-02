"""Screen-space radial modifier rig, separate from the canvas event router."""
import math
from PySide6.QtCore import QPointF, QRectF, QTimer, Qt
from PySide6.QtGui import QColor, QPen, QTransform
from comic_editor.core.models import RadialBlurModifier


class SpatialModifierFeatures:
    def _render_radial_raster(self, painter, obj, parent_opacity, visible):
        """Use the same tile-coordinate stages as Apply, then transform once.

        Blurring an already-warped Raster and subsequently baking in its local
        tiles would resample in two different orders and change its appearance.
        """
        from PySide6.QtGui import QPainter
        from comic_editor.ui.effect_pipeline import aligned, empty_image, render_stages
        from comic_editor.ui.modifier_rendering import apply_opacity_mask

        destination = self._multi_transform_preview_quads.get(obj.object_id)
        if destination is None and obj.object_id == self.selected_object_id:
            destination = self._transform_preview_quad
        placement = QTransform.fromTranslate(obj.x, obj.y) * self._drawing_object_transform(obj, destination)
        mapping = placement * self.layer_world_transform(obj.parent_layer_id)
        inverse, valid = placement.inverted()
        if not valid:
            return
        bounds = self.tiles.content_bounds(obj.object_id) or QRectF(*obj.interaction_rect)
        if obj.modifier_source_frame is not None:
            bounds = bounds.united(QRectF(*obj.modifier_source_frame))
        selection = self._raster_selection_preview_state(obj)
        if selection is not None:
            bounds = bounds.united(selection[3].map(selection[2]).boundingRect())
        bounds = aligned(bounds)
        modifiers = self._active_modifier_instances(obj.modifier_ids, suppress_outline=self._suppress_outline_for_mask)
        from comic_editor.ui.thumbnail_effects import capture_scale, scaled_modifiers
        thumbnail_scale = capture_scale(self, bounds, modifiers)
        navigator = self._interactive_render and getattr(self, "_effect_preview_channel", "canvas") == "navigator"
        stage_mapping = mapping
        capture_bounds = bounds
        if thumbnail_scale < 1.:
            bounds = aligned(QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(bounds))
            capture_bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
            stage_mapping = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale) * mapping
            modifiers = scaled_modifiers(modifiers, thumbnail_scale)
        signature = self._modifier_object_signature(obj)
        key = ("radial-raster-source", obj.object_id, signature[0], signature[3], signature[4], self._rect_signature(bounds))
        if navigator:
            key = ("navigator-source", thumbnail_scale, key)
        image = self._modifier_source_cache_get(key)
        if image is None:
            image = empty_image(bounds)
            source = QPainter(image)
            source.setTransform(QTransform.fromScale(thumbnail_scale, thumbnail_scale)
                                * QTransform.fromTranslate(-bounds.left(), -bounds.top()))
            try:
                if not self._render_raster_selection_preview(source, obj, capture_bounds):
                    for (x, y), tile in self.tiles.iter_tiles(obj.object_id, capture_bounds):
                        source.drawImage(x*obj.tile_size, y*obj.tile_size, tile)
            finally:
                source.end()
            self._modifier_source_cache_put(key, image)
        required = inverse.mapRect(visible)
        viewport_world = self._modifier_viewport_region(QRectF())
        if not viewport_world.isEmpty():
            # A stroke dirty rectangle limits painting, while an unchanged
            # raster effect retains one window for the full canvas viewport.
            # Map world to tile space once: rotating a bounding rectangle to
            # the parent and back would produce different capture extents.
            world_inverse, world_valid = mapping.inverted()
            if world_valid:
                required = world_inverse.mapRect(viewport_world)
        if getattr(self, "_effect_preview_channel", "canvas") == "navigator":
            required = None
        elif thumbnail_scale < 1.:
            required = QTransform.fromScale(thumbnail_scale, thumbnail_scale).mapRect(required)
        image, bounds = render_stages(self, image, bounds, modifiers, stage_mapping, nearest=True,
            required=required, source_key=key, provisional=navigator,
            request_scope=("object", obj.object_id, getattr(self, "_effect_preview_channel", "canvas")))
        if thumbnail_scale < 1.:
            bounds = QTransform.fromScale(1/thumbnail_scale, 1/thumbnail_scale).mapRect(bounds)
        if obj.opacity_mask is not None:
            binding = obj.opacity_mask
            field = self.render_tone_mask_field(binding.mask_id, image.width(), image.height(),
                self._world_to_image_transform(mapping, bounds, image.width(), image.height()), mapping.mapRect(bounds))
            image = apply_opacity_mask(image, field, binding.black_value, binding.white_value)
        opacity = parent_opacity if self._render_base_alpha or obj.opacity_locked else parent_opacity*obj.opacity
        if obj.object_id == self._live_underlay_object_id:
            opacity *= 1-self._live_underlay_amount
        painter.save()
        self._set_crisp_raster_transform(painter)
        painter.setTransform(placement, True)
        painter.setOpacity(opacity)
        painter.drawImage(bounds, image)
        painter.restore()

    def _init_spatial_features(self):
        from comic_editor.ui.effect_jobs import EffectJobs
        self._effect_jobs = EffectJobs(self)
        self._radial_handle_pending = None
        self._radial_effect_revision = None
        self._radial_handle_timer = QTimer(self)
        self._radial_handle_timer.setSingleShot(True)
        self._radial_handle_timer.timeout.connect(self._flush_radial_handle)

    def _active_radial_modifier(self):
        if not self.modifier_mode:
            return None
        modifier = self.chapter.modifiers.get(self.active_modifier_id) if self.chapter else None
        if isinstance(modifier, RadialBlurModifier) and not modifier.muted and any(
            target in self.chapter.modifier_target_ids(modifier.modifier_id)
            for target in self.selected_entities
        ):
            return modifier
        return None

    def _radial_handle_points(self, modifier):
        center = self.document_to_widget(QPointF(*modifier.center))
        half = math.radians(modifier.angle)/2
        return center, center+QPointF(math.cos(half), math.sin(half))*72

    def _radial_handle_hit(self, point):
        modifier = self._active_radial_modifier()
        if modifier is None:
            return None
        distance, hit = min((math.dist(p.toTuple(), point.toTuple()), i)
                            for i, p in enumerate(self._radial_handle_points(modifier)))
        return hit if distance <= 12 else None

    def _radial_preview_current(self):
        return self.chapter is not None and self._radial_effect_revision == (
            id(self.chapter), self._document_projection.revision)

    def _has_live_radial_effect(self):
        return bool(self.chapter and any(isinstance(modifier, RadialBlurModifier) and not modifier.muted
            and (modifier.intensity > 0 or "intensity" in modifier.parameter_masks)
            and (modifier.angle > 0 or "angle" in modifier.parameter_masks)
            for modifier in self.chapter.modifiers.values()))

    def _radial_mask_gradient_active(self):
        if not self.chapter or not self._mask_gradient_drag:
            return False
        return any(isinstance(modifier, RadialBlurModifier) and not modifier.muted
            and any(binding.mask_id == self.active_tone_mask_id for binding in modifier.parameter_masks.values())
            for modifier in self.chapter.modifiers.values())

    def _draw_radial_modifier_handles(self, painter):
        modifier = self._active_radial_modifier()
        if modifier is None:
            return False
        center, end = self._radial_handle_points(modifier)
        painter.save()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#ff8b26"), 1.5, Qt.DotLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawEllipse(center, 72, 72)
        painter.setPen(QPen(QColor("#ff8b26"), 2))
        painter.drawArc(QRectF(center.x()-72, center.y()-72, 144, 144),
                        round(modifier.angle*8), -round(modifier.angle*16))
        painter.drawLine(center, end)
        painter.drawText(center+QPointF(-25, 96), f"{modifier.angle:.1f}°")
        for point in (center, end):
            painter.setBrush(QColor("#ff8b26"))
            painter.setPen(QPen(QColor("#452005"), 1.5))
            painter.drawEllipse(point, 7, 7)
        painter.restore()
        return True

    def _begin_radial_handle(self, point):
        modifier = self._active_radial_modifier()
        if modifier is None:
            return False
        hit = self._radial_handle_hit(point)
        if hit is None:
            return False
        self._commit_text_edit()
        self._modifier_handle_drag = {"radial": modifier.modifier_id, "handle": hit,
            "before": self.chapter.to_dict(), "center": modifier.center,
            "press": self.widget_to_document(point)}
        return True

    def _queue_radial_handle(self, point):
        if not self._modifier_handle_drag or "radial" not in self._modifier_handle_drag:
            return False
        self._radial_handle_pending = QPointF(point)
        if not self._radial_handle_timer.isActive():
            self._radial_handle_timer.start(16)
        return True

    def _flush_radial_handle(self):
        self._radial_handle_timer.stop()
        point, self._radial_handle_pending = self._radial_handle_pending, None
        state = self._modifier_handle_drag
        if point is None or not state or "radial" not in state or self.chapter is None:
            return
        modifier = self.chapter.modifiers.get(state["radial"])
        if not isinstance(modifier, RadialBlurModifier):
            return
        before = modifier.center, modifier.angle
        if state["handle"] == 0:
            center = QPointF(*state["center"])+self.widget_to_document(point)-state["press"]
            modifier.center = self._snap(center, self.active_layer_id).toTuple()
        else:
            delta = point-self.document_to_widget(QPointF(*modifier.center))
            modifier.angle = min(360., max(0., abs(math.degrees(math.atan2(delta.y(), delta.x())))*2))
        modifier.validate()
        if before[0] == modifier.center and math.isclose(before[1], modifier.angle, abs_tol=1e-9):
            modifier.angle = before[1]
            return
        # A pending whole-view capture must not wait on the previous handle
        # position. Keep finished images while canceling obsolete snapshots.
        self._effect_jobs.cancel(clear_retained=False)
        self._projection_work_waiting = False
        self.documentChanged.emit(None)
        # Only this revision may keep the previous view while exact radial
        # work finishes, including the final repaint after mouse/pen release.
        self._radial_effect_revision = (id(self.chapter), self._document_projection.revision)
        self.update()
