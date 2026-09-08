"""Private, chapter-space line gradients owned by tone masks."""
from __future__ import annotations

import math

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QTransform

from comic_editor.core.models import (
    BoundGeometry, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LineGradientField, PathNode, object_from_dict,
)


class MaskGradientFeatures:
    def active_mask_gradient(self) -> ColorFillGradientObject | None:
        mask = (
            self.chapter.masks.get(self.active_tone_mask_id)
            if self.chapter else None
        )
        return mask.gradient if mask is not None else None

    def _mask_gradient_press(self, point: QPointF) -> None:
        mask = self.chapter.masks.get(self.active_tone_mask_id)
        if mask is None:
            return
        obj = mask.gradient
        hit = self._gradient_control_hit(obj, point) if obj else None
        self._mask_gradient_drag = {
            "mask_id": mask.mask_id,
            "before": self.chapter.to_dict(),
            "gradient": obj.to_dict() if obj else None,
            "revision": mask.revision,
            "start": QPointF(point),
            "node": hit[1] if hit else "",
            "moved": False,
        }

    def _mask_gradient_move(self, point: QPointF) -> None:
        state = self._mask_gradient_drag
        obj = self.active_mask_gradient()
        if state is None:
            self._mask_gradient_hover(point)
            return
        mask = self.chapter.masks.get(state["mask_id"])
        if mask is None:
            return
        if not state["moved"]:
            distance = math.dist(point.toTuple(), state["start"].toTuple())
            if distance * self.scale < 3:
                return
            state["moved"] = True
        if not state["node"]:
            start = state["start"]
            if obj is None:
                obj = ColorFillGradientObject(
                    name="Mask Gradient", mask_only=True,
                    ramp=ColorGradientRamp(stops=[
                        ColorGradientStop(position=0, color="#00FFFFFF"),
                        ColorGradientStop(position=1, color="#FFFFFFFF"),
                    ]),
                )
                mask.gradient = obj
            obj.line_field = LineGradientField(
                BoundGeometry.path([
                    PathNode(x=start.x(), y=start.y()),
                    PathNode(x=point.x(), y=point.y()),
                ], False),
                reverse_direction=obj.line_field.reverse_direction,
            )
            state["node"] = obj.line_field.geometry.nodes[-1].node_id
        else:
            node = next(
                n for n in obj.line_field.geometry.nodes
                if n.node_id == state["node"]
            )
            other = next(n for n in obj.line_field.geometry.nodes if n is not node)
            if math.dist(other.position, point.toTuple()) < 1e-6:
                return
            node.position = point.toTuple()
        obj.touch_revision()
        mask.touch()
        self._invalidate_tone_mask_overlay()
        self._invalidate_scene_cache()
        self.documentChanged.emit(QRectF())
        self.update()

    def _mask_gradient_hover(self, point: QPointF) -> None:
        obj = self.active_mask_gradient()
        hit = self._gradient_control_hit(obj, point) if obj else None
        self.setCursor(Qt.PointingHandCursor if hit else Qt.CrossCursor)
        self.setToolTip(
            "Drag to move this gradient endpoint" if hit
            else "Drag to draw a mask gradient"
        )

    def _finish_mask_gradient(self, commit: bool = True) -> bool:
        state, self._mask_gradient_drag = self._mask_gradient_drag, None
        if state is None:
            return False
        mask = self.chapter.masks.get(state["mask_id"]) if self.chapter else None
        if mask is not None and state["moved"]:
            if commit:
                mask.validate()
                self.push_model_change(
                    state["before"], self.chapter.to_dict(),
                    "Edit mask gradient" if state["gradient"] else "Add mask gradient",
                )
            else:
                mask.gradient = (
                    object_from_dict(state["gradient"])
                    if state["gradient"] else None
                )
                mask.revision = state["revision"]
            self._invalidate_tone_mask_overlay()
            self._invalidate_scene_cache()
            self.documentChanged.emit(QRectF())
            self.maskContentChanged.emit()
        self.interactionFinished.emit()
        self.update()
        return True

    def _draw_mask_gradient_handles(
        self, painter: QPainter, obj: ColorFillGradientObject,
    ) -> None:
        scale = max(self.scale, 0.05)
        first, second = obj.line_field.geometry.nodes
        painter.save()
        painter.setPen(QPen(QColor("#ff9f22"), 2 / scale))
        painter.drawLine(QPointF(*first.position), QPointF(*second.position))
        for node, position in ((first, 0.0), (second, 1.0)):
            if obj.line_field.reverse_direction:
                position = 1.0 - position
            painter.setBrush(self._sample_color_ramp(obj.ramp, position))
            painter.drawEllipse(QPointF(*node.position), 7 / scale, 7 / scale)
        painter.restore()

    def _render_mask_gradient_field(
        self, obj: ColorFillGradientObject, width: int, height: int,
        world_to_image: QTransform,
    ) -> np.ndarray:
        """Sample at the requested resolution, including rotated export tiles."""
        inverse, valid = world_to_image.inverted()
        if not valid:
            return np.zeros((height, width), dtype=np.float32)
        x = np.arange(width, dtype=np.float32)[None, :] + .5
        y = np.arange(height, dtype=np.float32)[:, None] + .5
        divisor = inverse.m13() * x + inverse.m23() * y + inverse.m33()
        with np.errstate(divide="ignore", invalid="ignore"):
            world_x = (inverse.m11() * x + inverse.m21() * y + inverse.dx()) / divisor
            world_y = (inverse.m12() * x + inverse.m22() * y + inverse.dy()) / divisor
        first, second = obj.line_field.geometry.nodes
        dx, dy = second.x - first.x, second.y - first.y
        scalar = np.clip(
            ((world_x - first.x) * dx + (world_y - first.y) * dy)
            / max(dx * dx + dy * dy, 1e-12), 0, 1,
        )
        if obj.line_field.reverse_direction:
            scalar = 1 - scalar
        # Use the standard gradient ramp sampler to preserve coincident stops.
        lut = self._gradient_ramp_lut(obj.ramp)
        indices = np.rint(np.nan_to_num(scalar) * (len(lut) - 1)).astype(np.int32)
        coverage = (
            (world_x >= 0) & (world_x < self.chapter.width)
            & (world_y >= 0) & (world_y < self.chapter.height)
        )
        return np.where(coverage, lut[indices, 3] / np.float32(255), 0)
