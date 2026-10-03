"""Solid freehand lasso fills with a replaceable live raster preview."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.commands import TilePatchCommand
from comic_editor.core.models import RasterObject


class LassoBrushFeatures:
    def _init_lasso_brush_features(self) -> None:
        self._lasso_brush = None

    def _begin_lasso_brush(self, point: QPointF) -> None:
        if self.chapter is None or self.active_tone_mask_id:
            return
        obj = self.chapter.objects.get(self.selected_object_id)
        if not isinstance(obj, RasterObject) or len(self.selected_entities) > 1:
            return
        self._cancel_lasso_brush()
        self._cancel_fill_job()
        self._clear_fill_replay()
        self._finalize_raster_paste_overlay(obj.object_id)
        local = self._raster_local_point(obj, point)
        self._lasso_brush = {
            "object": obj, "path": QPainterPath(local), "last": local,
            "color": self._active_fill_color(),
            "selection": QPainterPath(self._drawing_selection_path),
            "tiling": self._tiling_brush_context(obj),
            "before": {}, "preview_keys": set(), "dirty": QRectF(),
            "frame": tuple(obj.interaction_rect), "height": self.chapter.height,
        }
        self._drawing = True

    def _continue_lasso_brush(self, point: QPointF) -> None:
        state = self._lasso_brush
        if state is None:
            return
        local = self._raster_local_point(state["object"], point)
        if local == state["last"]:
            return
        state["path"].lineTo(local)
        state["last"] = local
        try:
            self._preview_lasso_brush()
        except Exception:
            self._cancel_lasso_brush()
            raise

    def _preview_lasso_brush(self) -> None:
        state = self._lasso_brush
        obj = state["object"]
        path = QPainterPath(state["path"])
        path.closeSubpath()
        if state["tiling"]:
            from comic_editor.core.tiling_paint import folded_footprint
            path = folded_footprint(path, *state["tiling"])
            geometry, mapping = state["tiling"]
            path = path.intersected(mapping.inverted()[0].map(geometry.path()))
        if not state["selection"].isEmpty():
            path = path.intersected(state["selection"])
        size = self.tiles.tile_size
        keys = {
            key for key in self.tiles.keys_for_rect(path.boundingRect())
            if path.intersects(QRectF(key[0] * size, key[1] * size, size, size))
        }
        before = state["before"]
        dirty = QRectF()
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
        self._queue_visual_dirty(world)

    def _restore_lasso_brush_frame(self, object_id: str, state) -> None:
        frame, height = state
        self._restore_raster_frame(object_id, frame)
        if self.chapter is not None and self.chapter.height != height:
            self.chapter.height = height
            self.hierarchyChanged.emit()

    def _finish_lasso_brush(self) -> None:
        state = self._lasso_brush
        if state is None:
            return
        obj = state["object"]
        after = self.tiles.snapshot(obj.object_id, set(state["before"]))
        before = {key: image for key, image in state["before"].items() if image != after[key]}
        if not before:
            self._cancel_lasso_brush()
            self.interactionFinished.emit()
            return
        dirty = QRectF(state["dirty"])
        self.command_stack.push(TilePatchCommand(
            "Lasso brush", self.tiles, obj.object_id,
            before, {key: after[key] for key in before},
            lambda object_id=obj.object_id, rect=dirty:
                self._raster_fill_visual_changed(object_id, rect),
            (state["frame"], state["height"]),
            (tuple(obj.interaction_rect), self.chapter.height),
            lambda value, object_id=obj.object_id:
                self._restore_lasso_brush_frame(object_id, value),
        ), already_done=True)
        self._lasso_brush = None
        self._drawing = False
        self.documentChanged.emit(dirty)
        self.interactionFinished.emit()
        self.update()

    def _cancel_lasso_brush(self) -> bool:
        state = getattr(self, "_lasso_brush", None)
        if state is None:
            return False
        obj = state["object"]
        for key, image in state["before"].items():
            self.tiles.set_tile(obj.object_id, key, image)
        self._restore_lasso_brush_frame(obj.object_id, (state["frame"], state["height"]))
        self._lasso_brush = None
        self._drawing = False
        self._invalidate_render_bounds("object", obj.object_id)
        self._queue_visual_dirty(state["dirty"])
        self.update()
        return True
