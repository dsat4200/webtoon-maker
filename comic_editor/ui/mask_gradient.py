"""Private, chapter-space endpoint gradients owned by tone masks."""
from __future__ import annotations

import math

import numpy as np

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainter, QPen, QPolygonF, QTransform

from comic_editor.core.document_patch import RecordSnapshot
from comic_editor.core.changes import ChangeSet, EntityChange

from comic_editor.core.models import (
    BoundGeometry, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LimitedMaskGradient, LineGradientField, PathNode, object_from_dict,
)


class MaskGradientFeatures:
    def _mask_gradient_changed(self, mask, *, transient=False):
        if transient:
            change = ChangeSet((EntityChange(('mask', mask.mask_id),
                frozenset({'gradient', 'limited_gradients', 'revision'})),),
                transient=True, label='Edit mask gradient')
            self._publish_change_set(change, action='transient')
        else:
            change = self._last_published_change
        self._emit_typed_document_changed(QRectF(), change)

    def active_mask_gradient(self) -> ColorFillGradientObject | None:
        mask = (
            self.chapter.masks.get(self.active_tone_mask_id)
            if self.chapter else None
        )
        if mask is None:
            return None
        selected_id = getattr(self, "_active_mask_gradient_id", "")
        for limited in mask.limited_gradients:
            if limited.gradient.object_id == selected_id:
                return limited.gradient
        return mask.gradient or (mask.limited_gradients[0].gradient if mask.limited_gradients else None)

    def active_limited_mask_gradient(self) -> LimitedMaskGradient | None:
        obj = self.active_mask_gradient()
        mask = self.chapter.masks.get(self.active_tone_mask_id) if self.chapter else None
        return next((entry for entry in mask.limited_gradients if entry.gradient is obj), None) if mask else None

    def select_mask_gradient(self, object_id: str) -> None:
        self._finish_mask_gradient()
        self._active_mask_gradient_id = object_id
        self.maskContentChanged.emit()
        self.update()

    def add_limited_mask_gradient(self, shape: str) -> LimitedMaskGradient | None:
        mask = self.chapter.masks.get(self.active_tone_mask_id) if self.chapter else None
        if mask is None or shape not in {"linear", "circular"}:
            return None
        self._finish_mask_gradient()
        before = RecordSnapshot.capture(self.chapter, masks=(mask.mask_id,))
        center = self.widget_to_document(QPointF(self.width() / 2, self.height() / 2))
        center.setX(max(0, min(self.chapter.width, center.x())))
        center.setY(max(0, min(self.chapter.height, center.y())))
        radius = max(20, min(self.chapter.width, self.chapter.height, self.width() / max(self.scale, .05)) / 4)
        first = center if shape == "circular" else center - QPointF(radius / 2, 0)
        second = first + QPointF(radius, 0)
        obj = ColorFillGradientObject(
            name=f"Limited {'Circular' if shape == 'circular' else 'Linear'} {len(mask.limited_gradients) + 1}",
            mask_only=True, gradient_shape=shape,
            line_field=LineGradientField(BoundGeometry.path([
                PathNode(x=first.x(), y=first.y()), PathNode(x=second.x(), y=second.y()),
            ], False)),
            ramp=ColorGradientRamp(stops=[
                ColorGradientStop(position=0, color="#FFFFFFFF" if shape == "circular" else "#00FFFFFF"),
                ColorGradientStop(position=1, color="#00FFFFFF" if shape == "circular" else "#FFFFFFFF"),
            ]),
        )
        limited = LimitedMaskGradient(gradient=obj, half_width=radius / 2)
        mask.limited_gradients.append(limited)
        mask.touch()
        self._active_mask_gradient_id = obj.object_id
        self._gradient_tool_shape = shape
        self.push_model_change(before, before.after(self.chapter), "Add limited mask gradient")
        self._invalidate_tone_mask_overlay()
        self._mask_gradient_changed(mask)
        self.maskContentChanged.emit()
        self.update()
        return limited

    def remove_active_mask_gradient(self) -> None:
        mask = self.chapter.masks.get(self.active_tone_mask_id) if self.chapter else None
        obj = self.active_mask_gradient()
        if mask is None or obj is None:
            return
        self._finish_mask_gradient()
        before = RecordSnapshot.capture(self.chapter, masks=(mask.mask_id,))
        if mask.gradient is obj:
            mask.gradient = None
        else:
            mask.limited_gradients = [entry for entry in mask.limited_gradients if entry.gradient is not obj]
        mask.touch()
        self._active_mask_gradient_id = ""
        self.push_model_change(before, before.after(self.chapter), "Remove mask gradient")
        self._invalidate_tone_mask_overlay()
        self._mask_gradient_changed(mask)
        self.maskContentChanged.emit()
        self.update()

    def _limited_gradient_controls(self, limited: LimitedMaskGradient) -> dict[str, QPointF]:
        first, last = limited.gradient.line_field.geometry.nodes[0], limited.gradient.line_field.geometry.nodes[-1]
        origin, delta = QPointF(*first.position), QPointF(last.x-first.x, last.y-first.y)
        radius = max(math.hypot(delta.x(), delta.y()), 1e-6)
        if limited.gradient.gradient_shape == "circular":
            # Offset bound handles from the endpoint direction to keep them distinct.
            axis = QPointF(delta.x()+delta.y(), delta.y()-delta.x()) / math.sqrt(2)
            return {"start_bound": origin + axis*limited.start_bound,
                    "end_bound": origin + axis*limited.end_bound}
        perpendicular = QPointF(-delta.y()/radius, delta.x()/radius)
        return {"start_bound": origin + delta*limited.start_bound - perpendicular*limited.half_width,
                "end_bound": origin + delta*limited.end_bound - perpendicular*limited.half_width,
                "half_width": origin + delta*((limited.start_bound+limited.end_bound)/2) + perpendicular*limited.half_width}

    def _limited_gradient_hit(self, point: QPointF) -> str:
        limited = self.active_limited_mask_gradient()
        if limited is None:
            return ""
        for key, position in self._limited_gradient_controls(limited).items():
            # The zero-radius inner handle overlaps the center endpoint.
            if key == "start_bound" and limited.gradient.gradient_shape == "circular" and limited.start_bound == 0:
                continue
            if math.dist(point.toTuple(), position.toTuple()) * self.scale <= 9:
                return key
        return ""

    def _mask_gradient_press(self, point: QPointF) -> None:
        mask = self.chapter.masks.get(self.active_tone_mask_id)
        if mask is None:
            return
        obj = self.active_mask_gradient()
        bound_hit = self._limited_gradient_hit(point)
        hit = ("bound", bound_hit) if bound_hit else self._gradient_control_hit(obj, point) if obj else None
        self._mask_gradient_drag = {
            "mask_id": mask.mask_id,
            "before": RecordSnapshot.capture(self.chapter, masks=(mask.mask_id,)),
            "gradient": obj.to_dict() if obj else None,
            "limited": self.active_limited_mask_gradient().to_dict() if self.active_limited_mask_gradient() else None,
            "revision": mask.revision,
            "start": QPointF(point),
            "node": hit[1] if hit else "",
            "control": hit[0] if hit else "",
            "nodes": tuple((node, node.position) for node in obj.line_field.geometry.nodes) if obj else (),
            "last_point": QPointF(point),
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
        if point == state["last_point"]:
            return
        state["last_point"] = QPointF(point)
        if not state["moved"]:
            distance = math.dist(point.toTuple(), state["start"].toTuple())
            if distance * self.scale < 3:
                return
            state["moved"] = True
        if state["control"] == "bound":
            limited = self.active_limited_mask_gradient()
            first, last = obj.line_field.geometry.nodes[0], obj.line_field.geometry.nodes[-1]
            dx, dy = last.x-first.x, last.y-first.y
            radius = max(math.hypot(dx, dy), 1e-6)
            x, y = point.x()-first.x, point.y()-first.y
            key = state["node"]
            value = (math.hypot(x, y)/radius if obj.gradient_shape == "circular"
                     else abs(-x*dy+y*dx)/radius if key == "half_width"
                     else (x*dx+y*dy)/(radius*radius))
            if key == "start_bound":
                value = min(value, limited.end_bound-.001)
            setattr(limited, key, value)
            limited.validate()
        elif state["control"] == "translate":
            delta = point-state["start"]
            for node, position in state["nodes"]:
                node.position = (QPointF(*position)+delta).toTuple()
        elif not state["node"]:
            start = state["start"]
            if obj is None:
                obj = ColorFillGradientObject(
                    name="Mask Gradient", mask_only=True,
                    gradient_shape=self._gradient_tool_shape,
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
            state["control"] = "node"
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
        self._mask_gradient_changed(mask, transient=True)
        self.update()

    def _mask_gradient_hover(self, point: QPointF) -> None:
        obj = self.active_mask_gradient()
        hit = self._gradient_control_hit(obj, point) if obj else None
        self.setCursor(Qt.PointingHandCursor if hit or self._limited_gradient_hit(point) else Qt.CrossCursor)
        self.setToolTip(
            "Drag to adjust the gradient application bound" if self._limited_gradient_hit(point)
            else "Drag to move both gradient handles" if hit and hit[0] == "translate"
            else "Drag to move this gradient endpoint" if hit
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
                    state["before"], state["before"].after(self.chapter),
                    "Edit mask gradient" if state["gradient"] else "Add mask gradient",
                )
            else:
                if state["limited"]:
                    old_id = state["gradient"]["id"]
                    mask.limited_gradients = [
                        LimitedMaskGradient.from_dict(state["limited"]) if entry.gradient.object_id == old_id else entry
                        for entry in mask.limited_gradients
                    ]
                else:
                    mask.gradient = object_from_dict(state["gradient"]) if state["gradient"] else None
                mask.revision = state["revision"]
            self._invalidate_tone_mask_overlay()
            self._mask_gradient_changed(mask, transient=not commit)
            self.maskContentChanged.emit()
        self.interactionFinished.emit()
        self.update()
        return True

    def _draw_mask_gradient_handles(
        self, painter: QPainter, obj: ColorFillGradientObject,
    ) -> None:
        limited = self.active_limited_mask_gradient()
        if limited is not None:
            nodes = obj.line_field.geometry.nodes
            origin = QPointF(*nodes[0].position)
            delta = QPointF(nodes[-1].x-nodes[0].x, nodes[-1].y-nodes[0].y)
            radius = max(math.hypot(delta.x(), delta.y()), 1e-6)
            painter.save()
            painter.setPen(QPen(QColor("#58d7e8"), 1.5/max(self.scale, .05), Qt.DashLine))
            painter.setBrush(Qt.NoBrush)
            if obj.gradient_shape == "circular":
                for bound in (limited.start_bound, limited.end_bound):
                    painter.drawEllipse(origin, radius*bound, radius*bound)
            else:
                side = QPointF(-delta.y(), delta.x()) * (limited.half_width/radius)
                first, last = origin+delta*limited.start_bound, origin+delta*limited.end_bound
                painter.drawPolygon(QPolygonF([first-side, last-side, last+side, first+side]))
            painter.setBrush(QColor("#163d45"))
            size = 5/max(self.scale, .05)
            for key, point in self._limited_gradient_controls(limited).items():
                if key == "start_bound" and obj.gradient_shape == "circular" and limited.start_bound == 0:
                    continue
                painter.drawRect(QRectF(point.x()-size, point.y()-size, size*2, size*2))
            painter.restore()
        self._draw_endpoint_gradient_handles(painter, obj)

    def _render_mask_gradient_field(
        self, obj: ColorFillGradientObject, width: int, height: int,
        world_to_image: QTransform,
        limited: LimitedMaskGradient | None = None,
        *, output: np.ndarray | None = None, subtract: bool = False,
    ) -> np.ndarray:
        """Sample at the requested resolution, including rotated export tiles."""
        inverse, valid = world_to_image.inverted()
        if not valid:
            return output if output is not None else np.zeros((height, width), dtype=np.float32)
        first, second = obj.line_field.geometry.nodes[0], obj.line_field.geometry.nodes[-1]
        dx, dy = second.x - first.x, second.y - first.y
        # Use the standard gradient ramp sampler to preserve coincident stops.
        lut = self._cached_gradient_ramp_lut(obj.ramp)
        circular = obj.gradient_shape == "circular"
        radius = max(math.hypot(dx, dy), 1e-6)
        result = output if output is not None else np.empty((height, width), dtype=np.float32)
        x = np.arange(width, dtype=np.float32)[None, :] + .5
        # Export tiles can be much larger than the editing preview. Keep the
        # coordinate and distance temporaries bounded, without downsampling.
        rows = max(1, 262144 // max(width, 1))
        for start in range(0, height, rows):
            end = min(height, start + rows)
            y = np.arange(start, end, dtype=np.float32)[:, None] + .5
            divisor = inverse.m13() * x + inverse.m23() * y + inverse.m33()
            with np.errstate(divide="ignore", invalid="ignore"):
                world_x = (inverse.m11() * x + inverse.m21() * y + inverse.dx()) / divisor
                world_y = (inverse.m12() * x + inverse.m22() * y + inverse.dy()) / divisor
                if circular:
                    distance = np.hypot(world_x - first.x, world_y - first.y)
                    position = distance / radius
                else:
                    position = ((world_x - first.x) * dx + (world_y - first.y) * dy) / max(dx * dx + dy * dy, 1e-12)
                scalar = np.clip(position, 0, 1)
            if obj.line_field.reverse_direction:
                scalar = 1 - scalar
            indices = np.rint(np.nan_to_num(scalar) * (len(lut) - 1)).astype(np.int32)
            coverage = (
                (world_x >= 0) & (world_x < self.chapter.width)
                & (world_y >= 0) & (world_y < self.chapter.height)
            )
            alpha = lut[indices, 3] / np.float32(255)
            if limited is not None:
                edge = (limited.end_bound-position)*radius
                if not circular or limited.start_bound > 0:
                    edge = np.minimum(edge, (position-limited.start_bound)*radius)
                if not circular:
                    cross = np.abs(-(world_x-first.x)*dy + (world_y-first.y)*dx)/radius
                    edge = np.minimum(edge, limited.half_width-cross)
                falloff = np.clip(edge/limited.feather, 0, 1) if limited.feather > 0 else (edge >= 0)
                alpha = alpha * falloff
            values = np.where(coverage, alpha, 0)
            if output is None:
                result[start:end] = values
            elif subtract:
                np.subtract(result[start:end], values, out=result[start:end])
            else:
                np.add(result[start:end], values, out=result[start:end])
        return result
