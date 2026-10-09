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
from comic_editor.core.changes import ChangeSet, EntityChange, ResourceChange
from comic_editor.core.models import RasterObject
from comic_editor.ui.tool_sessions import InputAdapter, Interruption, ToolSession


def prepare_material_resources(_snapshot, definition):
    from comic_editor.core.brush_raster import prepare_brush_materials
    return prepare_brush_materials(definition)


def _material_working_bytes(_snapshot, definition):
    from comic_editor.core.brush_raster import brush_material_working_bytes
    return brush_material_working_bytes(definition)


prepare_material_resources.admission_priority = 0
prepare_material_resources.working_bytes = _material_working_bytes


def _has_authored_materials(definition):
    while definition is not None:
        if any(tip.shape == 'image' and tip.png for tip in definition.tips):
            return True
        if definition.texture is not None and definition.texture.png and definition.texture.density > 0:
            return True
        definition = definition.dual
    return False


class _PreparedBrushMaterialCache:
    """Bounded, document-independent owners prepared on the input worker.

    Retained source strings keep the identity key alive without hashing large
    PNGs on pen-down. The key includes the complete mip/grayscale request;
    brush size, color and dynamics do not change those original material pixels.
    """
    def __init__(self, budget=64*1024*1024, limit=8):
        self.budget, self.limit = budget, limit
        self.values = OrderedDict()
        self.bytes = 0

    @staticmethod
    def _identity(definition):
        resources = {}
        while definition is not None:
            for tip in definition.tips:
                if tip.shape != 'image' or not tip.png:
                    continue
                maximum = (max(0, int(math.floor(math.log2(max(1, min(tip.width, tip.height))))))
                           if definition.antialiasing >= 2 else 0)
                record = resources.setdefault(id(tip.png), [tip.png, -1, False])
                record[1] = max(record[1], maximum)
            texture = definition.texture
            if texture is not None and texture.png and texture.density > 0:
                resources.setdefault(id(texture.png), [texture.png, -1, False])[2] = True
            definition = definition.dual
        key = tuple(sorted((identifier, record[1], record[2])
                           for identifier, record in resources.items()))
        return key, tuple(record[0] for record in resources.values())

    def get(self, definition):
        key, _sources = self._identity(definition)
        entry = self.values.get(key)
        if entry is None:
            return None
        self.values.move_to_end(key)
        return entry[0]

    def put(self, definition, owner):
        key, sources = self._identity(definition)
        cost = sum(material.nbytes for material in owner._values.values())
        cost += sum(len(source) for source in sources)
        if not key or cost > self.budget or self.limit < 1:
            return
        previous = self.values.pop(key, None)
        if previous is not None:
            self.bytes -= previous[2]
        while self.values and (self.bytes+cost > self.budget or len(self.values) >= self.limit):
            _key, entry = self.values.popitem(last=False)
            self.bytes -= entry[2]
        self.values[key] = owner, sources, cost
        self.bytes += cost


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
        self._paint_brush_input_adapter = InputAdapter()
        self._paint_brush_session = None
        self._paint_brush_tile_input = None
        self._paint_brush_input_bounds = QRectF()
        self._paint_brush_material_cache = _PreparedBrushMaterialCache()
        self._paint_brush_timer = QTimer(self)
        self._paint_brush_timer.setInterval(16)
        self._paint_brush_timer.timeout.connect(self._tick_paint_brush)

    def _capture_paint_brush_packet(self, event, *, tablet=False):
        packet = self._paint_brush_input_adapter.capture(event, tablet=tablet)
        self._paint_brush_input_tablet = packet.tablet
        self._paint_brush_input_axes = (packet.tilt_x, packet.tilt_y, packet.rotation)
        self._paint_brush_packet_time = packet.timestamp
        self._paint_brush_clock_offset = self._paint_brush_input_adapter.offset
        self._paint_brush_last_event_time = self._paint_brush_input_adapter.last_event_time

    def _paint_brush_input(self, point: QPointF, pressure: float, *, obj=None) -> BrushInput:
        obj = obj if obj is not None else self._paint_brush_object
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

    def _begin_paint_brush(self, point: QPointF, pressure: float, *, _sample=None, _definition=None) -> None:
        if self.chapter is None or self.active_tone_mask_id:
            return
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject) or len(self.selected_entities) > 1:
            return
        from comic_editor.core.brush_raster import RasterBrushStroke

        self._finish_paint_brush()
        previous = self._native_input_predecessor()
        if previous is not None:
            # A released cold contact still owns its original transaction.
            # Queue the next gesture only after that terminal publication.
            self._defer_paint_brush_begin(point, pressure)
            return
        self._finalize_raster_paste_overlay(obj.object_id)
        definition = _definition or resolve_brush_view_size(
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
        self._native_input_error = None
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
            self._paint_brush_session = ToolSession(self._paint_brush_stroke.begin,
                self._paint_brush_stroke.add, self._paint_brush_stroke.finish,
                interruption=Interruption.COMMIT)
            sample = _sample or self._paint_brush_input(point, pressure)
            self._paint_brush_sample = sample
            self._paint_brush_input_bounds = QRectF(sample.x, sample.y, 0., 0.)
            from comic_editor.ui.tile_input import TileInputGate
            chapter = self.chapter
            gate = self._paint_brush_tile_input = TileInputGate(self, obj.object_id,
                valid=lambda: chapter.objects.get(obj.object_id) is obj,
                cancelled=self._cancel_paint_brush)
            self._raster_contact_point, self._raster_contact_active = QPointF(point), True
            if _has_authored_materials(definition):
                materials = self._paint_brush_material_cache.get(definition)
                if materials is not None:
                    self._paint_brush_stroke.install_materials(materials)
                else:
                    stroke = self._paint_brush_stroke
                    def adopt_materials(materials):
                        self._paint_brush_material_cache.put(definition, materials)
                        stroke.install_materials(materials)
                    gate.prepare_resources(prepare_material_resources, (definition,), adopt_materials)
            gate.submit(lambda: self._paint_brush_packet_keys(sample),
                        lambda: self._apply_paint_brush_sample(sample, begin=True))
            if not gate.closed and any(scheduler is not None and scheduler.continuous_enabled for scheduler in (
                    self._paint_brush_stroke.scheduler,self._paint_brush_stroke.dual_scheduler)):
                self._paint_brush_timer.start()
        except Exception:
            self._cancel_paint_brush()
            raise

    def _continue_paint_brush(self, point: QPointF, pressure: float, *, _sample=None) -> None:
        deferred = getattr(self, '_paint_brush_deferred', None)
        if deferred:
            deferred[-1]['samples'].append(self._paint_brush_deferred_packet(point, pressure))
            self._raster_contact_point = QPointF(point)
            return
        if self._paint_brush_stroke is None:
            return
        try:
            sample = _sample or self._paint_brush_input(point, pressure)
            self._paint_brush_sample = sample
            self._raster_contact_point = QPointF(point)
            self._submit_paint_brush_sample(sample)
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
            self._submit_paint_brush_sample(sample)
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
        deferred = getattr(self, '_paint_brush_deferred', None)
        if deferred:
            deferred[-1]['released'] = True
            return
        gate = getattr(self, '_paint_brush_tile_input', None)
        if gate is None:
            return self._finish_paint_brush_admitted()
        if gate.closed or gate.released:
            return
        self._paint_brush_timer.stop()
        gate.prepare_bounds(self._paint_brush_untouched_bounds_keys)
        def finish():
            self._finish_paint_brush_admitted()
            gate.retire()
            if self._paint_brush_tile_input is gate:
                self._paint_brush_tile_input = None
            self._raster_contact_active = bool(getattr(self, '_native_deferred_activations', ()))
        gate.finish(finish, keys=self._paint_brush_finish_keys)

    def _finish_paint_brush_admitted(self) -> None:
        stroke = self._paint_brush_stroke
        if stroke is None:
            return
        self._paint_brush_timer.stop()
        obj = self._paint_brush_object
        try:
            dirty = self._paint_brush_session.commit()
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
                command = TilePatchCommand(
                    "Brush stroke", self.tiles, obj.object_id,
                    changed_before, {key: after[key] for key in keys},
                    lambda object_id=obj.object_id, rect=QRectF(dirty_world):
                        self._raster_fill_visual_changed(object_id, rect),
                    frame_before, tuple(obj.interaction_rect),
                    lambda frame, object_id=obj.object_id:
                        self._restore_raster_frame(object_id, frame),
                )
                command.forward_change = command.change_set().with_bounds({("object", obj.object_id):
                    (dirty_world.getRect(), dirty_world.getRect())})
                self.command_stack.push(command, already_done=True)
            self._clear_paint_brush_state()
            if keys:
                self._emit_typed_document_changed(dirty_world, command.change_set())
            self.interactionFinished.emit()
            self.update()
        except Exception:
            self._cancel_paint_brush()
            raise

    def _cancel_paint_brush(self) -> None:
        gate = getattr(self, '_paint_brush_tile_input', None)
        if gate is not None and not gate.closed:
            gate.cancel()
            return
        if gate is not None:
            gate.retire()
            self._paint_brush_tile_input = None
        self._paint_brush_deferred = []
        self._raster_contact_point, self._raster_contact_active = None, False
        obj = getattr(self, "_paint_brush_object", None)
        if obj is None:
            return
        dirty = QRectF(self._stroke_dirty_world)
        keys = tuple(self._stroke_before)
        for key, image in self._stroke_before.items():
            self.tiles.set_tile(obj.object_id, key, image)
        if self._paint_brush_frame is not None:
            obj.interaction_rect = self._paint_brush_frame
        if self.chapter is not None and self._paint_brush_chapter_height is not None:
            self.chapter.height = self._paint_brush_chapter_height
        self._clear_paint_brush_state()
        self._invalidate_render_bounds("object", obj.object_id)
        owner = self.tiles._tiles.get(obj.object_id)
        change = ChangeSet((EntityChange(("object", obj.object_id), frozenset({"pixels", "interaction_rect"}),
            dirty.getRect(), dirty.getRect()),), tuple(ResourceChange(("object", obj.object_id), "raster", key,
            new_generation=owner.version(key) if owner is not None else None) for key in keys),
            transient=True, label="Cancel brush stroke")
        self._publish_change_set(change, action="transient")
        self._queue_visual_dirty(dirty)
        self.update()

    def _clear_paint_brush_state(self) -> None:
        self._paint_brush_timer.stop()
        if self._paint_brush_session is not None:
            self._paint_brush_session.cancel()
        self._paint_brush_session = None
        self._paint_brush_stroke = None
        self._paint_brush_object = None
        self._paint_brush_frame = None
        self._paint_brush_chapter_height = None
        self._paint_brush_sample = None
        self._paint_brush_input_bounds = QRectF()
        self._stroke_before = {}
        self._stroke_dirty_world = QRectF()
        self._stroke_erasing = False
        self._drawing = False
        self._restore_gc_after_stroke()

    def _apply_paint_brush_sample(self, sample, *, begin=False):
        dirty = (self._paint_brush_session.begin(sample) if begin
                 else self._paint_brush_session.update(sample))
        if not dirty.isEmpty():
            self._emit_raster_dirty(self._paint_brush_object, dirty)

    def _submit_paint_brush_sample(self, sample):
        gate = self._paint_brush_tile_input
        if gate is None:
            return self._apply_paint_brush_sample(sample)
        if not gate.released:
            gate.submit(lambda: self._paint_brush_packet_keys(sample),
                        lambda: self._apply_paint_brush_sample(sample))

    @staticmethod
    def _paint_brush_source_radius(definition):
        def maximum(name):
            dynamic = definition.dynamics.get(name)
            return max(1., dynamic.tilt_maximum) if dynamic is not None and dynamic.tilt else 1.
        size = max(1., definition.size * maximum('size'))
        extent = size
        if definition.spray:
            particle = definition.particle_size * (size if definition.particle_size_relative else 1.)
            extent += max(1., particle * maximum('particle_size'))
        radius = extent * max(1., definition.thickness * maximum('thickness'))
        if definition.ribbon:
            radius *= 4.  # The ordinary ribbon join limits its miter to four.
        if definition.mixing_mode == 'running':
            pickup = definition.blur_width if definition.blur_mode == 'fixed' else definition.blur * 20.
            radius += pickup * maximum('blur')
        return radius + 4.

    def _paint_brush_packet_keys(self, sample):
        stroke = self._paint_brush_stroke
        points = [QPointF(sample.x, sample.y)]
        radius = self._paint_brush_source_radius(stroke.definition)
        for scheduler, plane in ((stroke.scheduler, stroke.main),
                                 (stroke.dual_scheduler, stroke.secondary)):
            if scheduler is None:
                continue
            radius = max(radius, self._paint_brush_source_radius(scheduler.definition))
            if scheduler.last is not None:
                points.append(QPointF(scheduler.last.x, scheduler.last.y))
            if scheduler.initial is not None:
                points.append(QPointF(scheduler.initial.x, scheduler.initial.y))
            points.extend(QPointF(dab.x, dab.y) for dab in scheduler.pending)
            points.extend(QPointF(dab.x, dab.y) for dab in plane.ribbon_points)
        left, right = min(point.x() for point in points), max(point.x() for point in points)
        top, bottom = min(point.y() for point in points), max(point.y() for point in points)
        bounds = QRectF(left, top, right-left, bottom-top).adjusted(-radius, -radius, radius, radius)
        self._paint_brush_input_bounds = (bounds if self._paint_brush_input_bounds.isEmpty()
                                         else self._paint_brush_input_bounds.united(bounds))
        return self.tiles.keys_for_rect(bounds)

    def _paint_brush_finish_keys(self):
        stroke = self._paint_brush_stroke
        keys = set(self._stroke_before)
        # Final correction/taper replay retains its original native envelope.
        keys.update(self.tiles.keys_for_rect(self._paint_brush_input_bounds))
        return keys

    def _paint_brush_untouched_bounds_keys(self):
        stroke = self._paint_brush_stroke
        owner = self.tiles._tiles.get(stroke.object_id)
        native_keys = self._paint_brush_finish_keys()
        known = self.tiles._alpha_bounds.get(stroke.object_id, {})
        return {key for key in owner or () if key not in native_keys and
                (owner, key) not in owner.residency.entries and
                (key not in known or (stroke.object_id, *key) in self.tiles._alpha_bounds_dirty)}

    def _paint_brush_deferred_packet(self, point, pressure):
        deferred = getattr(self, '_paint_brush_deferred', None)
        identifier = deferred[-1]['object'] if deferred else self.selected_object_id
        obj = self.chapter.objects.get(identifier)
        return QPointF(point), self._paint_brush_input(point, pressure, obj=obj)

    def _defer_paint_brush_begin(self, point, pressure):
        deferred = getattr(self, '_paint_brush_deferred', None)
        if deferred is None:
            deferred = self._paint_brush_deferred = []
        obj = self.chapter.objects[self.selected_object_id]
        definition = resolve_brush_view_size(self.settings.active_paint_brush(),
            self._paint_brush_view_scale(obj, self._raster_local_point(obj, point)))
        sample = QPointF(point), self._paint_brush_input(point, pressure, obj=obj)
        deferred.append({'samples': [sample],
            'released': False, 'object': self.selected_object_id,
            'definition': definition, 'chapter': self.chapter, 'tiles': self.tiles,
            'selection': QPainterPath(self._drawing_selection_path),
            'primary': self.primary_color, 'secondary': self.secondary_color,
            'slot': self.active_color_slot})
        self._raster_contact_point, self._raster_contact_active = QPointF(point), True
        self._defer_native_activation(self._start_deferred_paint_brush)

    def _start_deferred_paint_brush(self):
        deferred = getattr(self, '_paint_brush_deferred', None)
        if not deferred:
            return
        next_stroke = deferred.pop(0)
        if (self.chapter is not next_stroke['chapter'] or self.tiles is not next_stroke['tiles']
                or next_stroke['object'] not in self.chapter.objects):
            return
        self._paint_brush_deferred = []
        previous = (self.selected_kind, self.selected_id, self.selected_object_id,
                    self.primary_color, self.secondary_color, self.active_color_slot,
                    self._drawing_selection_path, self.selected_entities, self.active_tone_mask_id)
        try:
            self.selected_kind = 'object'
            self.selected_id = self.selected_object_id = next_stroke['object']
            self.selected_entities = [('object', next_stroke['object'])]
            self.active_tone_mask_id = ''
            self.primary_color, self.secondary_color = next_stroke['primary'], next_stroke['secondary']
            self.active_color_slot = next_stroke['slot']
            self._drawing_selection_path = next_stroke['selection']
            for index, (point, sample) in enumerate(next_stroke['samples']):
                if index == 0:
                    self._begin_paint_brush(point, sample.pressure, _sample=sample,
                                            _definition=next_stroke['definition'])
                else:
                    self._continue_paint_brush(point, sample.pressure, _sample=sample)
            if next_stroke['released']:
                self._finish_paint_brush()
        finally:
            (self.selected_kind, self.selected_id, self.selected_object_id,
             self.primary_color, self.secondary_color, self.active_color_slot,
             self._drawing_selection_path, self.selected_entities, self.active_tone_mask_id) = previous
            self._paint_brush_deferred = deferred
