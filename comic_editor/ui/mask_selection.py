"""Lasso additions and cutouts for the currently edited tone mask."""
from __future__ import annotations

import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen

from comic_editor.core.commands import TilePatchCommand


class MaskSelectionFeatures:
    def _begin_mask_selection(self, point: QPointF, modifiers) -> None:
        if (
            self.chapter is None
            or self.active_tone_mask_id not in self.chapter.masks
        ):
            return
        self._mask_selection_gesture = {
            "mask_id": self.active_tone_mask_id,
            "points": [QPointF(point)],
            "remove": bool(modifiers & Qt.ControlModifier),
        }
        self.update()

    def _move_mask_selection(self, point: QPointF) -> None:
        gesture = self._mask_selection_gesture
        if gesture is None:
            return
        points = gesture["points"]
        if math.dist(points[-1].toTuple(), point.toTuple()) * self.scale >= 1:
            points.append(QPointF(point))
            self.update()

    def _cancel_mask_selection(self) -> bool:
        if self._mask_selection_gesture is None:
            return False
        self._mask_selection_gesture = None
        self.update()
        return True

    @staticmethod
    def _mask_selection_path(points) -> QPainterPath:
        path = QPainterPath(points[0])
        for point in points[1:]:
            path.lineTo(point)
        path.closeSubpath()
        path.setFillRule(Qt.OddEvenFill)
        return path

    def _draw_mask_selection(self, painter: QPainter) -> None:
        gesture = self._mask_selection_gesture
        if gesture is None or gesture["mask_id"] != self.active_tone_mask_id:
            return
        scale = max(self.scale, .05)
        path = self._mask_selection_path(gesture["points"])
        painter.save()
        painter.setBrush(
            QColor(255, 90, 90, 30) if gesture["remove"]
            else QColor(100, 181, 246, 30)
        )
        painter.setPen(QPen(QColor("#15151a"), 2 / scale))
        painter.drawPath(path)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("white"), 1 / scale, Qt.DashLine))
        painter.drawPath(path)
        point = gesture["points"][-1] + QPointF(14 / scale, -14 / scale)
        painter.setPen(QPen(QColor("white"), 2 / scale))
        painter.drawLine(
            point - QPointF(5 / scale, 0), point + QPointF(5 / scale, 0)
        )
        if not gesture["remove"]:
            painter.drawLine(
                point - QPointF(0, 5 / scale), point + QPointF(0, 5 / scale)
            )
        painter.restore()

    def _finish_mask_selection(self) -> bool:
        gesture, self._mask_selection_gesture = self._mask_selection_gesture, None
        if gesture is None:
            return False
        mask = (
            self.chapter.masks.get(gesture["mask_id"])
            if self.chapter else None
        )
        if mask is not None and len(gesture["points"]) >= 3:
            path = self._mask_selection_path(gesture["points"]).simplified()
            document = QPainterPath()
            document.addRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
            path = path.intersected(document)
            self._apply_mask_selection(mask, path, gesture["remove"])
        self.interactionFinished.emit()
        self.update()
        return True

    def _apply_mask_selection(self, mask, path: QPainterPath, remove: bool) -> None:
        if path.isEmpty():
            return
        before, after = {}, {}
        size = self.tiles.tile_size
        color = QColor("black" if remove else "white")
        for key in self.tiles.keys_for_rect(path.boundingRect()):
            rect = QRectF(key[0] * size, key[1] * size, size, size)
            if not path.intersects(rect):
                continue
            original = self.tiles.tile(mask.mask_id, key)
            image = (
                QImage(original) if original is not None
                else self.tiles._empty(size)
            )
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.translate(-rect.x(), -rect.y())
            painter.fillPath(path, color)
            painter.end()
            if image == original or (
                original is None and self.tiles._alpha_bbox(image) is None
            ):
                continue
            before[key] = QImage(original) if original is not None else None
            after[key] = image
        if not after:
            return
        before_state = (mask.revision, mask.paint_has_subtractions)
        for key, image in after.items():
            self.tiles.set_tile(mask.mask_id, key, image)
        mask.paint_has_subtractions |= remove
        mask.touch()
        self.command_stack.push(TilePatchCommand(
            "Remove from mask" if remove else "Add to mask",
            self.tiles, mask.mask_id, before, after,
            self._mask_selection_changed,
            before_state, (mask.revision, mask.paint_has_subtractions),
            lambda state, mask_id=mask.mask_id:
            self._restore_mask_selection_state(mask_id, state),
        ), already_done=True)
        self._mask_selection_changed()

    def _restore_mask_selection_state(self, mask_id, state) -> None:
        mask = self.chapter.masks.get(mask_id) if self.chapter else None
        if mask is not None:
            mask.revision, mask.paint_has_subtractions = state

    def _mask_selection_changed(self) -> None:
        self._mask_tiles_changed()
        self.maskContentChanged.emit()

    @staticmethod
    def _signed_mask_paint(image: QImage) -> np.ndarray:
        # White contributes coverage; opaque black removes coverage, including
        # that supplied by linked gradients and hierarchy contributors.
        rgba = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        rows = np.frombuffer(rgba.constBits(), dtype=np.uint8).reshape(
            rgba.height(), rgba.bytesPerLine()
        )
        pixels = rows[:, :rgba.width() * 4].reshape(
            rgba.height(), rgba.width(), 4
        )
        return (2 * pixels[..., 0].astype(np.float32) - pixels[..., 3]) / 255
