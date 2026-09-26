"""Document-space distortion handles shared by mouse and tablet input."""
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPen, QTransform

from comic_editor.core.distort import gizmo_kind
from comic_editor.core.models import DistortModifier


class DistortFeatures:
    def _mesh_warp_preview_modifier(self):
        if self.chapter is None:
            return None
        drag = self._modifier_handle_drag or {}
        identifier = drag.get("distort") or getattr(self, "_mesh_warp_parameter_drag_id", None)
        modifier = self.chapter.modifiers.get(identifier)
        if (isinstance(modifier, DistortModifier) and not modifier.muted
                and modifier.modifier_type == "distort_mesh_warp"):
            return modifier
        return None

    def _paint_mesh_warp_preview(self, painter, modifier):
        # A temporary viewport image may contain draft pixels; the retained
        # document projection must only ever contain finished artwork.
        if getattr(self, "_mesh_warp_preview_session_id", None) != modifier.modifier_id:
            self._effect_jobs.cancel(clear_retained=False)
            self._mesh_warp_preview_session_id = modifier.modifier_id
        ratio = max(1., self.devicePixelRatioF())
        image = QImage(QSize(round(self.width() * ratio), round(self.height() * ratio)),
                       QImage.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.transparent)
        previous = (getattr(self, "_projection_exact", False),
                    getattr(self, "_effect_region_requests", False),
                    getattr(self, "_mesh_warp_preview_id", None))
        self._projection_exact = self._effect_region_requests = False
        self._mesh_warp_preview_id = modifier.modifier_id
        try:
            self._render_scene_cache_rect(self.rect(), target=image, projection=False)
        finally:
            self._projection_exact, self._effect_region_requests, self._mesh_warp_preview_id = previous
        painter.drawImage(0, 0, image)
        self._mesh_warp_preview_presented = True
        self._projection_frame_pending = True

    def _finish_mesh_warp_preview(self):
        if getattr(self, "_mesh_warp_preview_session_id", None) is not None:
            self._mesh_warp_preview_session_id = None
            self._effect_jobs.cancel(clear_retained=False)
            self._invalidate_scene_cache()
        self._mesh_warp_preview_presented = False

    def _active_distort_modifier(self):
        modifier = self.chapter.modifiers.get(self.active_modifier_id) if self.chapter else None
        if (self.modifier_mode and isinstance(modifier, DistortModifier) and not modifier.muted
                and any(target in self.chapter.modifier_target_ids(modifier.modifier_id)
                        for target in self.selected_entities)):
            return modifier
        return None

    @staticmethod
    def _distort_world_point(modifier, point):
        x, y, w, h = modifier.frame
        return QPointF(x + point[0] * w, y + point[1] * h)

    @staticmethod
    def _distort_normalized_point(modifier, point):
        x, y, w, h = modifier.frame
        return ((point.x() - x) / max(w, 1e-6), (point.y() - y) / max(h, 1e-6))

    def _distort_handle_points(self, modifier):
        kind = gizmo_kind(modifier.modifier_type, modifier.parameters)
        source = getattr(self, "distort_edit_source", False)
        if kind in {"pins", "quad", "mesh"}:
            return [(i, self.document_to_widget(self._distort_world_point(modifier, point)))
                    for i, point in enumerate(modifier.source_points if source else modifier.points)]
        if kind in {"none", "shear"}:
            return []
        center = QPointF(*modifier.center)
        handles = [("center", self.document_to_widget(center))]
        if kind == "radius":
            handles.append(("radius", self.document_to_widget(center + QPointF(modifier.radius, 0))))
        if modifier.modifier_type == "distort_mirror":
            for key, distance in (("input_angle", 60), ("output_angle", 92)):
                angle = math.radians(modifier.parameters[key])
                direction = self.document_to_widget(center + QPointF(math.cos(angle), math.sin(angle))) - handles[0][1]
                direction *= distance / max(1e-9, math.hypot(direction.x(), direction.y()))
                handles.append((key, handles[0][1] + direction))
        return handles

    def _draw_distort_handles(self, painter):
        modifier = self._active_distort_modifier()
        if modifier is None:
            return False
        kind = gizmo_kind(modifier.modifier_type, modifier.parameters)
        handles = self._distort_handle_points(modifier)
        painter.save()
        painter.setTransform(QTransform())
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor("#ff8b26"), 1.5, Qt.DotLine))
        if kind == "radius":
            center, end = handles[0][1], handles[1][1]
            radius = math.dist(center.toTuple(), end.toTuple())
            painter.drawEllipse(center, radius, radius)
            painter.drawLine(center, end)
        elif modifier.modifier_type == "distort_mirror":
            for key, point in handles[1:]:
                painter.drawLine(handles[0][1], point)
                painter.drawText(point + QPointF(9, -6), "In" if key == "input_angle" else "Out")
        elif kind == "quad" and len(handles) >= 4:
            for i in range(4):
                painter.drawLine(handles[i][1], handles[(i + 1) % 4][1])
            if getattr(self, "distort_show_grid", True):
                from comic_editor.core.cage import homography, project
                import numpy as np
                try:
                    matrix = homography(np.asarray([point.toTuple() for _, point in handles]))
                    for t in (.25, .5, .75):
                        pairs = project(matrix, np.asarray(((t, 0), (t, 1), (0, t), (1, t))))
                        painter.drawLine(QPointF(*pairs[0]), QPointF(*pairs[1]))
                        painter.drawLine(QPointF(*pairs[2]), QPointF(*pairs[3]))
                except (ValueError, np.linalg.LinAlgError):
                    pass
        elif kind == "mesh" and getattr(self, "distort_show_grid", True):
            columns = int(modifier.parameters["columns"])
            rows = int(modifier.parameters["rows"])
            for i, (_, point) in enumerate(handles):
                if i % columns < columns - 1 and i + 1 < len(handles):
                    painter.drawLine(point, handles[i + 1][1])
                if i // columns < rows - 1 and i + columns < len(handles):
                    painter.drawLine(point, handles[i + columns][1])
        elif kind == "pins":
            for source, destination in zip(modifier.source_points, modifier.points):
                painter.drawLine(self.document_to_widget(self._distort_world_point(modifier, source)),
                                 self.document_to_widget(self._distort_world_point(modifier, destination)))
        for _, point in handles:
            painter.setBrush(QColor("#66c6ff" if getattr(self, "distort_edit_source", False)
                                    and kind in {"pins", "quad", "mesh"} else "#ff8b26"))
            painter.setPen(QPen(QColor("#452005"), 1.5))
            painter.drawEllipse(point, 6, 6)
        painter.restore()
        return True

    def _begin_distort_handle(self, point):
        modifier = self._active_distort_modifier()
        if modifier is None:
            return False
        kind = gizmo_kind(modifier.modifier_type, modifier.parameters)
        nearby = [(math.dist(candidate.toTuple(), point.toTuple()), key)
                  for key, candidate in self._distort_handle_points(modifier)]
        distance, hit = min(nearby, key=lambda pair: pair[0]) if nearby else (float("inf"), None)
        if distance > 12:
            hit = None
        world = self.widget_to_document(point)
        before = None
        if kind == "pins":
            mode = getattr(self, "distort_pin_mode", "move")
            if hit is not None and mode == "remove":
                before = self.chapter.to_dict()
                del modifier.points[hit]
                del modifier.source_points[hit]
                self._distort_changed()
                self.push_model_change(before, self.chapter.to_dict(), "Remove deform pin")
                return True
            if hit is None and (mode == "add" or not modifier.points) and QRectF(*modifier.frame).contains(world):
                before = self.chapter.to_dict()
                normalized = self._distort_normalized_point(modifier, world)
                modifier.points.append(normalized)
                modifier.source_points.append(normalized)
                hit = len(modifier.points) - 1
                self._distort_changed()
        if hit is None:
            return False
        self._commit_text_edit()
        self._modifier_handle_drag = {"distort": modifier.modifier_id, "handle": hit,
            "before": before or self.chapter.to_dict(), "center": modifier.center,
            "press": world, "source": getattr(self, "distort_edit_source", False)}
        return True

    def _distort_changed(self):
        self._invalidate_scene_cache()
        self.documentChanged.emit(None)
        self.update()

    def _move_distort_handle(self, point):
        state = self._modifier_handle_drag
        if not state or "distort" not in state or self.chapter is None:
            return False
        modifier = self.chapter.modifiers.get(state["distort"])
        if not isinstance(modifier, DistortModifier):
            return False
        world = self.widget_to_document(point)
        handle = state["handle"]
        if handle == "center":
            modifier.center = self._snap(QPointF(*state["center"]) + world - state["press"], self.active_layer_id).toTuple()
        elif handle == "radius":
            modifier.radius = max(1., math.dist(modifier.center, world.toTuple()))
        elif handle in {"input_angle", "output_angle"}:
            delta = world - QPointF(*modifier.center)
            angle = math.degrees(math.atan2(delta.y(), delta.x()))
            if abs((angle - modifier.parameters[handle] + 180) % 360 - 180) < 1e-9:
                return True
            modifier.parameters[handle] = angle
        else:
            points = modifier.source_points if state["source"] else modifier.points
            if handle < len(points):
                points[handle] = self._distort_normalized_point(modifier, self._snap(world, self.active_layer_id))
        modifier.validate()
        self._distort_changed()
        return True

    @staticmethod
    def _transform_distort_modifier(modifier, transform):
        old_frame = QRectF(*modifier.frame)
        new_frame = transform.mapRect(old_frame)
        for name in ("points", "source_points"):
            mapped = []
            for u, v in getattr(modifier, name):
                point = transform.map(QPointF(old_frame.x() + u * old_frame.width(), old_frame.y() + v * old_frame.height()))
                mapped.append(((point.x() - new_frame.x()) / max(1e-6, new_frame.width()),
                               (point.y() - new_frame.y()) / max(1e-6, new_frame.height())))
            setattr(modifier, name, mapped)
        center = QPointF(*modifier.center)
        modifier.radius = max(1., math.dist(transform.map(center).toTuple(),
                                           transform.map(center + QPointF(modifier.radius, 0)).toTuple()))
        modifier.center = transform.map(center).toTuple()
        modifier.frame = new_frame.getRect()
        modifier.validate()

    def capture_distort_beneath(self, modifier_id):
        from comic_editor.ui.distort_sources import capture_distort_beneath
        return capture_distort_beneath(self, modifier_id)
