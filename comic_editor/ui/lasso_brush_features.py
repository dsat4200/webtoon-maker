"""Solid freehand lasso fills with a replaceable live raster preview."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.commands import TilePatchCommand
from comic_editor.core.changes import ChangeSet, EntityChange, ResourceChange
from comic_editor.core.models import RasterObject


class LassoBrushFeatures:
    def _init_lasso_brush_features(self) -> None:
        self._lasso_brush = None
        self._lasso_brush_deferred = []

    def _begin_lasso_brush(self, point: QPointF, *, _context=None) -> None:
        if self.chapter is None or self.active_tone_mask_id:
            return
        obj = (_context['object'] if _context is not None
               else self.chapter.objects.get(self.selected_object_id))
        if not isinstance(obj, RasterObject) or (_context is None and len(self.selected_entities) > 1):
            return
        context = _context or self._lasso_brush_receipt_context(obj, point)
        if self._native_input_predecessor() is not None:
            pending = {'context': context, 'paths': [], 'released': False, 'cancelled': False,
                       'chapter': self.chapter, 'tiles': self.tiles, 'point': QPointF(point)}
            self._lasso_brush_deferred.append(pending)
            self._raster_contact_point, self._raster_contact_active = QPointF(point), True
            self._defer_native_activation(lambda: self._start_deferred_lasso_brush(pending))
            return
        self._cancel_lasso_brush()
        self._native_input_error = None
        self._cancel_fill_job()
        self._clear_fill_replay()
        self._finalize_raster_paste_overlay(obj.object_id)
        self._lasso_brush = {
            **context,
            "before": {}, "preview_keys": set(), "dirty": QRectF(),
            "frame": tuple(obj.interaction_rect), "height": self.chapter.height,
        }
        state = self._lasso_brush
        from comic_editor.ui.tile_input import TileInputGate
        chapter = self.chapter
        state['input_gate'] = TileInputGate(self, obj.object_id,
            valid=lambda: self._lasso_brush is state and chapter.objects.get(obj.object_id) is obj,
            cancelled=self._cancel_lasso_brush)
        def required_bounds():
            owner = self.tiles._tiles.get(obj.object_id)
            known = self.tiles._alpha_bounds.get(obj.object_id, {})
            return {key for key in owner or () if (owner, key) not in owner.residency.entries and
                    (key not in known or (obj.object_id, *key) in self.tiles._alpha_bounds_dirty)}
        state['input_gate'].prepare_bounds(required_bounds)
        self._raster_contact_point, self._raster_contact_active = QPointF(point), True
        self._drawing = True

    def _lasso_brush_receipt_context(self, obj, point):
        local = self._raster_local_point(obj, point)
        return {'object': obj, 'path': QPainterPath(local), 'last': local,
                'color': self._active_fill_color(),
                'selection': QPainterPath(self._drawing_selection_path),
                'tiling': self._tiling_brush_context(obj)}

    def _start_deferred_lasso_brush(self, pending):
        if pending in self._lasso_brush_deferred:
            self._lasso_brush_deferred.remove(pending)
        if (pending['cancelled'] or self.chapter is not pending['chapter']
                or self.tiles is not pending['tiles']
                or self.chapter.objects.get(pending['context']['object'].object_id)
                    is not pending['context']['object']):
            return
        remaining = self._lasso_brush_deferred
        self._lasso_brush_deferred = []
        active_mask = self.active_tone_mask_id
        try:
            self.active_tone_mask_id = ''
            self._begin_lasso_brush(pending['point'], _context=pending['context'])
            state = self._lasso_brush
            if state is None:
                return
            gate = state['input_gate']
            for path in pending['paths']:
                gate.submit(lambda path=path: self._lasso_brush_source_keys(state, path),
                            lambda path=path: self._preview_lasso_brush(path))
            if pending['released']:
                self._finish_lasso_brush()
        finally:
            self.active_tone_mask_id = active_mask
            self._lasso_brush_deferred = remaining

    def _continue_lasso_brush(self, point: QPointF) -> None:
        if self._lasso_brush_deferred:
            pending = self._lasso_brush_deferred[-1]
            if pending['released']:
                return
            state = pending['context']
            local = self._raster_local_point(state['object'], point)
            if local != state['last']:
                state['path'].lineTo(local)
                state['last'] = local
                pending['point'] = QPointF(point)
                pending['paths'].append(self._lasso_brush_closed_path(state))
            self._raster_contact_point = QPointF(point)
            return
        state = self._lasso_brush
        if state is None:
            return
        local = self._raster_local_point(state["object"], point)
        if local == state["last"]:
            return
        state["path"].lineTo(local)
        state["last"] = local
        self._raster_contact_point = QPointF(point)
        try:
            path = self._lasso_brush_closed_path(state)
            gate = state['input_gate']
            if not gate.released:
                gate.submit(lambda: self._lasso_brush_source_keys(state, path),
                            lambda: self._preview_lasso_brush(path))
        except Exception:
            self._cancel_lasso_brush()
            raise

    def _lasso_brush_closed_path(self, state):
        path = QPainterPath(state["path"])
        path.closeSubpath()
        if state["tiling"]:
            from comic_editor.core.tiling_paint import folded_footprint
            path = folded_footprint(path, *state["tiling"])
            geometry, mapping = state["tiling"]
            path = path.intersected(mapping.inverted()[0].map(geometry.path()))
        if not state["selection"].isEmpty():
            path = path.intersected(state["selection"])
        return path

    def _lasso_brush_source_keys(self, state, path):
        return set(self.tiles.keys_for_rect(path.boundingRect())) | state['preview_keys']

    def _preview_lasso_brush(self, path=None) -> None:
        state = self._lasso_brush
        obj = state["object"]
        path = self._lasso_brush_closed_path(state) if path is None else path
        size = self.tiles.tile_size
        keys = {
            key for key in self.tiles.keys_for_rect(path.boundingRect())
            if path.intersects(QRectF(key[0] * size, key[1] * size, size, size))
        }
        before = state["before"]
        dirty = QRectF()
        changed = set()
        for key in keys | state["preview_keys"]:
            if key not in before:
                original = self.tiles.tile(obj.object_id, key)
                before[key] = QImage(original) if original is not None else None
            original = before[key]
            image = QImage(original) if original is not None else self.tiles._empty(size)
            if key in keys:
                painter = QPainter(image)
                try:
                    painter.setRenderHint(QPainter.Antialiasing)
                    painter.translate(-key[0] * size, -key[1] * size)
                    color = state["color"]
                    if color.alpha() == 0:
                        painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
                        color = QColor("white")
                    painter.fillPath(path, color)
                finally:
                    painter.end()
            # Rebuild from pen-down pixels so changing the closing edge removes
            # the previous preview and translucent fills never accumulate.
            current = self.tiles.tile(obj.object_id, key)
            if image == current or (current is None and self.tiles._alpha_bbox(image) is None):
                continue
            self.tiles.set_tile(obj.object_id, key, image)
            changed.add(key)
            rect = QRectF(key[0] * size, key[1] * size, size, size)
            dirty = rect if dirty.isEmpty() else dirty.united(rect)
        state["preview_keys"] = keys
        if dirty.isEmpty():
            return
        content = self.tiles.content_bounds(obj.object_id)
        frame = QRectF(*state["frame"])
        if content is not None:
            frame = frame.united(content.adjusted(-24, -24, 24, 24))
        obj.interaction_rect = (frame.x(), frame.y(), frame.width(), frame.height())
        self._invalidate_render_bounds("object", obj.object_id)
        world = self.modifier_expanded_dirty(
            obj.object_id, self._drawing_local_rect_to_world(obj, dirty),
        )
        state["dirty"] = world if state["dirty"].isEmpty() else state["dirty"].united(world)
        bottom = math.ceil(self._drawing_local_rect_to_world(obj, content).bottom()) if content else 0
        if bottom > self.chapter.height:
            self.chapter.height = bottom + 1080
            self.hierarchyChanged.emit()
        self._publish_lasso_brush_pixels(state, world, changed)

    def _publish_lasso_brush_pixels(self, state, world, keys):
        obj = state['object']
        owner = self.tiles._tiles.get(obj.object_id)
        change = ChangeSet((EntityChange(('object', obj.object_id), frozenset({'pixels', 'interaction_rect'}),
            world.getRect(), world.getRect()),), tuple(ResourceChange(('object', obj.object_id), 'raster', key,
            new_generation=owner.version(key) if owner is not None else None) for key in keys),
            transient=True, label='Lasso brush preview')
        self._publish_change_set(change, action='transient')
        self._queue_visual_dirty(world, scene=False)

    def _restore_lasso_brush_frame(self, object_id: str, state) -> None:
        frame, height = state
        self._restore_raster_frame(object_id, frame)
        if self.chapter is not None and self.chapter.height != height:
            self.chapter.height = height
            self.hierarchyChanged.emit()

    def _finish_lasso_brush(self) -> None:
        if self._lasso_brush_deferred:
            self._lasso_brush_deferred[-1]['released'] = True
            return
        state = self._lasso_brush
        if state is None:
            return
        gate = state['input_gate']
        if gate.released or gate.closed:
            return
        def finish():
            self._finish_lasso_brush_admitted()
            gate.retire()
            self._raster_contact_active = bool(getattr(self, '_native_deferred_activations', ()))
        gate.finish(finish, keys=lambda: set(state['before']))

    def _finish_lasso_brush_admitted(self) -> None:
        state = self._lasso_brush
        obj = state["object"]
        after = self.tiles.snapshot(obj.object_id, set(state["before"]))
        before = {key: image for key, image in state["before"].items() if image != after[key]}
        if not before:
            self._cancel_lasso_brush()
            self.interactionFinished.emit()
            return
        dirty = QRectF(state["dirty"])
        command = TilePatchCommand(
            "Lasso brush", self.tiles, obj.object_id,
            before, {key: after[key] for key in before},
            lambda object_id=obj.object_id, rect=dirty:
                self._raster_fill_visual_changed(object_id, rect),
            (state["frame"], state["height"]),
            (tuple(obj.interaction_rect), self.chapter.height),
            lambda value, object_id=obj.object_id:
                self._restore_lasso_brush_frame(object_id, value),
        )
        command.forward_change = command.change_set().with_bounds({('object', obj.object_id):
            (dirty.getRect(), dirty.getRect())})
        self.command_stack.push(command, already_done=True)
        self._lasso_brush = None
        self._drawing = False
        self._emit_typed_document_changed(dirty, command.change_set())
        self.interactionFinished.emit()
        self.update()

    def _cancel_lasso_brush(self) -> bool:
        deferred = getattr(self, '_lasso_brush_deferred', ())
        cancelled = False
        for pending in deferred:
            if not pending['released']:
                pending['cancelled'] = True
                cancelled = True
        state = getattr(self, "_lasso_brush", None)
        if state is None:
            return cancelled
        gate = state.get('input_gate')
        if gate is not None and gate.released and not gate.closed:
            return cancelled
        if gate is not None and not gate.closed:
            gate.cancel()
            return True
        if gate is not None:
            gate.retire()
        obj = state["object"]
        for key, image in state["before"].items():
            self.tiles.set_tile(obj.object_id, key, image)
        self._restore_lasso_brush_frame(obj.object_id, (state["frame"], state["height"]))
        self._lasso_brush = None
        self._drawing = False
        self._raster_contact_point, self._raster_contact_active = None, False
        self._invalidate_render_bounds("object", obj.object_id)
        self._publish_lasso_brush_pixels(state, state['dirty'], set(state['before']))
        self.update()
        return True
