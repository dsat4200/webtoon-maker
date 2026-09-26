"""Raster Brush gestures, independent of the legacy Pencil stroke path."""
from __future__ import annotations

import time
import math
from collections import OrderedDict
from dataclasses import replace

from PySide6.QtCore import QPointF, QRectF, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.brushes import BrushInput
from comic_editor.core.brush_view import resolve_brush_view_size
from comic_editor.core.commands import TilePatchCommand
from comic_editor.core.models import RasterObject


class BrushFeatures:
    def _paint_brush_view_scale(self, obj, local: QPointF) -> float:
        """Equal-area local-to-screen scale at the pen-down position.

        The Jacobian includes camera zoom/rotation, parent transforms and the
        raster's projective transform. A scalar size preserves anisotropy; it
        does not pretend to cancel shear or perspective across an entire dab.
        """
        transform = self._drawing_local_to_world_transform(obj) * self.camera_transform()
        denominator = transform.m13() * local.x() + transform.m23() * local.y() + transform.m33()
        if abs(denominator) <= 1e-12:
            return 1.0
        scale = math.sqrt(abs(transform.determinant() / denominator ** 3))
        return scale if math.isfinite(scale) and scale > 1e-8 else 1.0

    def _paint_brush_cursor_path(self, center: QPointF) -> QPainterPath:
        """Map the nominal brush circle through the same size/transform path."""
        path = QPainterPath()
        obj = self.chapter.objects.get(self.selected_object_id) if self.chapter else None
        if not isinstance(obj, RasterObject):
            return path
        local = self._raster_local_point(obj, self.widget_to_document(center))
        stroke = self._paint_brush_stroke
        if stroke is not None and obj is self._paint_brush_object:
            brush = stroke.definition
        else:
            brush = resolve_brush_view_size(self.settings.active_paint_brush(),
                                            self._paint_brush_view_scale(obj, local))
        radius = max(1.0 if brush.minimum_pixel else .1, brush.size) / 2
        path.addEllipse(local, radius, radius)
        transform = self._drawing_local_to_world_transform(obj) * self.camera_transform()
        return transform.map(path)

    def _paint_brush_selection_tiles(self):
        """Freeze the active object-local selection for this stroke only."""
        path = QPainterPath(self._drawing_selection_path)
        if path.isEmpty():
            return None
        bounds = path.boundingRect()
        tile_size = self.tiles.tile_size
        cache = OrderedDict()

        def mask_for(key):
            rect = QRectF(key[0] * tile_size, key[1] * tile_size, tile_size, tile_size)
            if not bounds.intersects(rect):
                return None
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
            mask = QImage(tile_size, tile_size, QImage.Format_ARGB32_Premultiplied)
            mask.fill(Qt.transparent)
            painter = QPainter(mask)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.translate(-rect.x(), -rect.y())
            painter.fillPath(path, QColor("white"))
            painter.end()
            cache[key] = mask
            while len(cache) > 64:
                cache.popitem(last=False)
            return mask

        return mask_for

    def _init_brush_features(self) -> None:
        self._paint_brush_stroke = None
        self._paint_brush_object = None
        self._paint_brush_frame = None
        self._paint_brush_chapter_height = None
        self._paint_brush_sample = None
        self._paint_brush_input_axes = (0.0, 0.0, 0.0)
        self._paint_brush_input_tablet = False
        self._paint_brush_packet_time = None
        self._paint_brush_clock_offset = None
        self._paint_brush_last_event_time = None
        self._paint_brush_serial = 0
        self._paint_brush_timer = QTimer(self)
        self._paint_brush_timer.setInterval(16)
        self._paint_brush_timer.timeout.connect(self._tick_paint_brush)

    def _capture_paint_brush_packet(self, event, *, tablet=False):
        self._paint_brush_input_tablet = tablet
        self._paint_brush_input_axes = (
            float(getattr(event, "xTilt", lambda: 0.0)()),
            float(getattr(event, "yTilt", lambda: 0.0)()),
            float(getattr(event, "rotation", lambda: 0.0)()),
        ) if tablet else (0.0, 0.0, 0.0)
        timestamp = float(getattr(event, "timestamp", lambda: 0)()) / 1000
        if timestamp <= 0:
            self._paint_brush_packet_time = None
            return
        if self._paint_brush_clock_offset is None or (
            self._paint_brush_last_event_time is not None
            and timestamp < self._paint_brush_last_event_time
        ):
            self._paint_brush_clock_offset = time.monotonic() - timestamp
        self._paint_brush_last_event_time = timestamp
        self._paint_brush_packet_time = timestamp + self._paint_brush_clock_offset

    def _paint_brush_input(self, point: QPointF, pressure: float) -> BrushInput:
        obj = self._paint_brush_object
        local = self._raster_local_point(obj, point)
        tilt_x, tilt_y, rotation = self._paint_brush_input_axes
        if self._paint_brush_input_tablet:
            local_to_widget = self._drawing_local_to_world_transform(obj) * self.camera_transform()
            inverse, valid = local_to_widget.inverted()
            if valid:
                center = local_to_widget.map(local)
                magnitude = math.hypot(tilt_x, tilt_y)
                if magnitude > 1e-8:
                    direction = inverse.map(center + QPointF(tilt_x / magnitude, tilt_y / magnitude)) - local
                    length = math.hypot(direction.x(), direction.y())
                    if length > 1e-8:
                        tilt_x, tilt_y = direction.x() * magnitude / length, direction.y() * magnitude / length
                radians = math.radians(rotation)
                direction = inverse.map(center + QPointF(math.cos(radians), math.sin(radians))) - local
                rotation = math.degrees(math.atan2(direction.y(), direction.x())) % 360
        timestamp = self._paint_brush_packet_time
        self._paint_brush_packet_time = None
        return BrushInput(
            x=local.x(), y=local.y(), pressure=self._effective_pressure(pressure),
            tilt_x=tilt_x, tilt_y=tilt_y, rotation=rotation,
            time=time.monotonic() if timestamp is None else timestamp,
        )

    def _begin_paint_brush(self, point: QPointF, pressure: float) -> None:
        if self.chapter is None or self.active_tone_mask_id:
            return
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject) or len(self.selected_entities) > 1:
            return
        from comic_editor.core.brush_raster import RasterBrushStroke

        self._finish_paint_brush()
        self._finalize_raster_paste_overlay(obj.object_id)
        definition = resolve_brush_view_size(
            self.settings.active_paint_brush(),
            self._paint_brush_view_scale(obj, self._raster_local_point(obj, point)),
        )
        secondary_active = self.active_color_slot == "secondary"
        color = QColor(self.secondary_color if secondary_active else self.primary_color)
        # Selecting CSP's secondary drawing color makes a two-color material
        # monochromatic; it does not exchange its black/white assignments.
        other = QColor(self.secondary_color)
        definition = replace(definition, sub_color=other.getRgb())
        if color.alpha() == 0:
            # In a gray material, transparent drawing erases only the black
            # part. Making the white/sub portion transparent preserves that
            # coverage distinction in the renderer (including ribbon tips).
            color.setAlpha(255)
            other.setAlpha(0)
            definition = replace(definition, blending_mode="erase", mixing_mode="none",
                                 sub_color=other.getRgb())
        self._paint_brush_object = obj
        self._paint_brush_frame = tuple(obj.interaction_rect)
        self._paint_brush_chapter_height = self.chapter.height
        self._stroke_before = {}
        self._stroke_dirty_world = QRectF()
        self._stroke_erasing = (definition.blending_mode == "erase" and
                                definition.mixing_mode not in {"blend", "running"})
        self._predictive = None
        self._drawing = True
        self._suspend_gc_for_stroke()
        self._paint_brush_serial += 1
        try:
            self._paint_brush_stroke = RasterBrushStroke(
                self.tiles, obj.object_id, definition, color,
                self._stroke_before, seed=self._paint_brush_serial,
                selection_tile=self._paint_brush_selection_tiles(),
            )
            sample = self._paint_brush_input(point, pressure)
            self._paint_brush_sample = sample
            dirty = self._paint_brush_stroke.begin(sample)
            if not dirty.isEmpty():
                self._emit_raster_dirty(obj, dirty)
            if any(scheduler is not None and scheduler.continuous_enabled for scheduler in (
                    self._paint_brush_stroke.scheduler,self._paint_brush_stroke.dual_scheduler)):
                self._paint_brush_timer.start()
        except Exception:
            self._cancel_paint_brush()
            raise

    def _continue_paint_brush(self, point: QPointF, pressure: float) -> None:
        if self._paint_brush_stroke is None:
            return
        try:
            sample = self._paint_brush_input(point, pressure)
            self._paint_brush_sample = sample
            dirty = self._paint_brush_stroke.add(sample)
            if not dirty.isEmpty():
                self._emit_raster_dirty(self._paint_brush_object, dirty)
        except Exception:
            self._cancel_paint_brush()
            raise

    def _tick_paint_brush(self) -> None:
        if self._paint_brush_stroke is None or self._paint_brush_sample is None:
            self._paint_brush_timer.stop()
            return
        try:
            sample = replace(self._paint_brush_sample, time=time.monotonic())
            self._paint_brush_sample = sample
            dirty = self._paint_brush_stroke.add(sample)
            if not dirty.isEmpty():
                self._emit_raster_dirty(self._paint_brush_object, dirty)
        except Exception as error:
            self._cancel_paint_brush()
            self.operationError.emit("Brush stroke", str(error))

    def _interrupt_paint_brush(self) -> None:
        """Commit contact when focus or pointer capture is lost."""
        if getattr(self, "_paint_brush_stroke", None) is None:
            return
        try:
            self._finish_paint_brush()
        except Exception as error:
            self.operationError.emit("Brush stroke", str(error))
        finally:
            self._pen_contact_active = False
            self._tablet_tool_active = False

    def _finish_paint_brush(self) -> None:
        stroke = self._paint_brush_stroke
        if stroke is None:
            return
        self._paint_brush_timer.stop()
        obj = self._paint_brush_object
        try:
            dirty = stroke.finish()
            if not dirty.isEmpty():
                self._emit_raster_dirty(obj, dirty)
            keys = set(self._stroke_before)
            self.tiles.prune_empty(obj.object_id, keys)
            after = self.tiles.snapshot(obj.object_id, keys)
            changed_before = {key: before for key, before in self._stroke_before.items()
                              if before != after[key]}
            keys = set(changed_before)
            if not keys:
                self._cancel_paint_brush()
                self.interactionFinished.emit()
                return
            content = self.tiles.content_bounds(obj.object_id)
            frame_before = self._paint_brush_frame
            if content is not None:
                padded = content.adjusted(-24, -24, 24, 24)
                frame = padded if self._stroke_erasing else QRectF(*frame_before).united(padded)
                obj.interaction_rect = (frame.x(), frame.y(), frame.width(), frame.height())
            else:
                obj.interaction_rect = frame_before
            dirty_world = QRectF(self._stroke_dirty_world)
            if keys:
                self.command_stack.push(TilePatchCommand(
                    "Brush stroke", self.tiles, obj.object_id,
                    changed_before, {key: after[key] for key in keys},
                    lambda object_id=obj.object_id, rect=QRectF(dirty_world):
                        self._raster_fill_visual_changed(object_id, rect),
                    frame_before, tuple(obj.interaction_rect),
                    lambda frame, object_id=obj.object_id:
                        self._restore_raster_frame(object_id, frame),
                ), already_done=True)
            self._clear_paint_brush_state()
            if keys:
                self.documentChanged.emit(dirty_world)
            self.interactionFinished.emit()
            self.update()
        except Exception:
            self._cancel_paint_brush()
            raise

    def _cancel_paint_brush(self) -> None:
        obj = getattr(self, "_paint_brush_object", None)
        if obj is None:
            return
        dirty = QRectF(self._stroke_dirty_world)
        for key, image in self._stroke_before.items():
            self.tiles.set_tile(obj.object_id, key, image)
        if self._paint_brush_frame is not None:
            obj.interaction_rect = self._paint_brush_frame
        if self.chapter is not None and self._paint_brush_chapter_height is not None:
            self.chapter.height = self._paint_brush_chapter_height
        self._clear_paint_brush_state()
        self._invalidate_render_bounds("object", obj.object_id)
        self._queue_visual_dirty(dirty)
        self.update()

    def _clear_paint_brush_state(self) -> None:
        self._paint_brush_timer.stop()
        self._paint_brush_stroke = None
        self._paint_brush_object = None
        self._paint_brush_frame = None
        self._paint_brush_chapter_height = None
        self._paint_brush_sample = None
        self._stroke_before = {}
        self._stroke_dirty_world = QRectF()
        self._stroke_erasing = False
        self._drawing = False
        self._restore_gc_after_stroke()
