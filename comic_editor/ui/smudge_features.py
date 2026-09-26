"""Editable two-point smudge strokes and screen-space point controls."""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath, QPainterPathStroker, QPen, QTransform

from comic_editor.core.smudge import fit_smudge_stroke, stroke_cubic


class SmudgeFeatures:
    def _active_smudge_modifier(self):
        modifier = self._active_distort_modifier()
        return modifier if modifier is not None and modifier.modifier_type == "distort_smudge" else None

    def _smudge_selected_stroke(self, modifier=None):
        modifier = modifier or self._active_smudge_modifier()
        if modifier is None:
            return None
        return next((stroke for stroke in modifier.parameters["strokes"]
                     if stroke["id"] == self.smudge_selected_stroke_id), None)

    def smudge_selected_point(self, modifier=None):
        stroke = self._smudge_selected_stroke(modifier)
        index = self.smudge_selected_point_index
        return stroke["points"][index] if stroke is not None and index in (0, 1) else None

    def smudge_select(self, stroke_id, point_index=0):
        self.smudge_selected_stroke_id = str(stroke_id)
        self.smudge_selected_point_index = max(0, min(1, int(point_index)))
        self.smudgeSelectedChanged.emit()
        self.update()

    def _smudge_path(self, stroke):
        points = [self.document_to_widget(QPointF(*point)) for point in stroke_cubic(stroke)]
        path = QPainterPath(points[0])
        path.cubicTo(*points[1:])
        return path

    def _smudge_gizmo_rects(self):
        """A fixed left column like the reference; values belong to one point."""
        height = max(32., min(120., (self.height() - 200.) / 3))
        rects = {}
        for index, key in enumerate(("radius", "flow", "strength")):
            top = 50. + index * (height + 42.)
            rects[key] = QRectF(20, top, 24, height)
        bottom = rects["strength"].bottom() + 18
        rects["point_type"] = QRectF(8, bottom, 80, 26)
        rects["delete"] = QRectF(8, bottom + 32, 80, 26)
        return rects

    @staticmethod
    def _smudge_slider_ratio(key, value):
        if key == "radius":
            return max(0., min(1., math.log(max(.1, value) / .1) / math.log(40960.)))
        return max(0., min(1., value / 100.))

    def _draw_smudge_handles(self, painter, modifier):
        painter.save()
        painter.setTransform(QTransform())
        painter.setRenderHint(painter.RenderHint.Antialiasing)
        selected = self._smudge_selected_stroke(modifier)
        for stroke in modifier.parameters["strokes"]:
            active = stroke is selected
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#ffad42" if active else "#77c7ea"), 2 if active else 1.2))
            painter.drawPath(self._smudge_path(stroke))
            for index, point in enumerate(stroke["points"]):
                anchor = self.document_to_widget(QPointF(*point["position"]))
                if active and point["point_type"] == "bezier":
                    handle = self.document_to_widget(QPointF(*point["handle"]))
                    painter.setPen(QPen(QColor("#9ecde2"), 1))
                    painter.drawLine(anchor, handle)
                    painter.setBrush(QColor("#263d4a"))
                    painter.drawEllipse(handle, 5, 5)
                painter.setPen(QPen(QColor("#183342"), 1.5))
                painter.setBrush(QColor("#ffad42" if active and index == self.smudge_selected_point_index else "#8bd8fa"))
                painter.drawEllipse(anchor, 6, 6)
                painter.setPen(QColor("#efefef"))
                painter.drawText(anchor + QPointF(9, -9), "Start" if index == 0 else "End")
        state = self._modifier_handle_drag or {}
        if state.get("smudge") and state.get("mode") == "draw":
            samples = state["samples"]
            path = QPainterPath(self.document_to_widget(QPointF(*samples[0][:2])))
            for sample in samples[1:]:
                path.lineTo(self.document_to_widget(QPointF(*sample[:2])))
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#f0f0f0"), 1.5, Qt.DotLine))
            painter.drawPath(path)
        point = self.smudge_selected_point(modifier)
        if point is not None:
            anchor = self.document_to_widget(QPointF(*point["position"]))
            radius_end = self.document_to_widget(QPointF(point["position"][0] + point["radius"], point["position"][1]))
            radius = math.dist(anchor.toTuple(), radius_end.toTuple())
            painter.setBrush(Qt.NoBrush)
            painter.setPen(QPen(QColor("#ffad42"), 1, Qt.DotLine))
            painter.drawEllipse(anchor, radius, radius)
            rects = self._smudge_gizmo_rects()
            for key in ("radius", "flow", "strength"):
                rect = rects[key]
                painter.setPen(Qt.NoPen)
                painter.setBrush(QColor(28, 29, 33, 240))
                painter.drawRoundedRect(rect.adjusted(-12, -31, 39, 10), 6, 6)
                painter.setBrush(QColor("#44464d"))
                painter.drawRoundedRect(rect, 5, 5)
                ratio = self._smudge_slider_ratio(key, point[key])
                thumb_y = rect.bottom() - rect.height() * ratio
                painter.setBrush(QColor("#d79543"))
                painter.drawRoundedRect(QRectF(rect.x() + 3, thumb_y - 3, rect.width() - 6, 6), 2, 2)
                painter.setPen(QColor("#efefef"))
                value = f"{point[key]:.1f} px" if key == "radius" else f"{point[key]:.0f}%"
                painter.drawText(QRectF(9, rect.top()-31, 76, 15), Qt.AlignLeft, key.title())
                painter.drawText(QRectF(9, rect.top()-16, 76, 15), Qt.AlignLeft, value)
            for key, text in (("point_type", "Bézier" if point["point_type"] == "bezier" else "Vector"), ("delete", "Delete stroke")):
                painter.setPen(QPen(QColor("#c89149"), 1))
                painter.setBrush(QColor("#292a2f"))
                painter.drawRoundedRect(rects[key], 5, 5)
                painter.setPen(QColor("#efefef"))
                painter.drawText(rects[key], Qt.AlignCenter, text)
        painter.restore()
        return True

    def _smudge_gizmo_changed(self):
        self._distort_changed()
        self.smudgePointChanged.emit()

    def smudge_delete_selected_stroke(self):
        modifier = self._active_smudge_modifier()
        stroke = self._smudge_selected_stroke(modifier)
        if modifier is None or stroke is None:
            return False
        before = self.chapter.to_dict()
        modifier.parameters["strokes"].remove(stroke)
        self.smudge_select("", 0)
        self.push_model_change(before, self.chapter.to_dict(), "Delete smudge stroke")
        self._smudge_gizmo_changed()
        self.interactionFinished.emit()
        return True

    def _begin_smudge_handle(self, widget_point, pressure=1.):
        modifier = self._active_smudge_modifier()
        if modifier is None:
            return False
        point = self.smudge_selected_point(modifier)
        hit = None
        if point is not None:
            rects = self._smudge_gizmo_rects()
            for key, rect in rects.items():
                if rect.adjusted(-8, -5, 8, 5).contains(widget_point):
                    hit = ("parameter", key)
                    break
            if hit == ("parameter", "delete"):
                self.smudge_delete_selected_stroke()
                return True
            if hit == ("parameter", "point_type"):
                before = self.chapter.to_dict()
                point = self.smudge_selected_point(modifier)
                point["point_type"] = "vector" if point["point_type"] == "bezier" else "bezier"
                self.push_model_change(before, self.chapter.to_dict(), "Change smudge point type")
                self._smudge_gizmo_changed()
                self.interactionFinished.emit()
                return True
        if hit is None:
            # Endpoints win over coincident handles, including vector points.
            for stroke in reversed(modifier.parameters["strokes"]):
                for index, node in enumerate(stroke["points"]):
                    anchor = self.document_to_widget(QPointF(*node["position"]))
                    if math.dist(anchor.toTuple(), widget_point.toTuple()) <= 10:
                        self.smudge_select(stroke["id"], index)
                        hit = ("point", index)
                        break
                if hit is not None:
                    break
        if hit is None:
            stroke = self._smudge_selected_stroke(modifier)
            if stroke:
                for index, node in enumerate(stroke["points"]):
                    handle = self.document_to_widget(QPointF(*node["handle"]))
                    if node["point_type"] == "bezier" and math.dist(handle.toTuple(), widget_point.toTuple()) <= 10:
                        self.smudge_select(stroke["id"], index)
                        hit = ("handle", index)
                        break
        if hit is None:
            stroker = QPainterPathStroker()
            stroker.setWidth(14)
            for stroke in reversed(modifier.parameters["strokes"]):
                if stroker.createStroke(self._smudge_path(stroke)).contains(widget_point):
                    self.smudge_select(stroke["id"], 0)
                    hit = ("stroke", None)
                    break
        world = self.widget_to_document(widget_point)
        stroke = self._smudge_selected_stroke(modifier)
        self._modifier_handle_drag = {
            "distort": modifier.modifier_id, "smudge": True,
            "mode": hit[0] if hit else "draw", "handle": hit[1] if hit else None,
            "before": self.chapter.to_dict(), "press": world,
            "stroke_id": stroke["id"] if stroke else "",
            "original": copy.deepcopy(stroke),
            "samples": [(world.x(), world.y(), max(0., min(1., pressure)))],
            "tool_settings": copy.deepcopy(modifier.parameters["tool_settings"]),
        }
        if hit and hit[0] == "parameter":
            self._move_smudge_handle(widget_point)
        self.update()
        return True

    def _cancel_smudge_gesture(self):
        state = self._modifier_handle_drag
        if not state or not state.get("smudge"):
            return False
        self._modifier_handle_drag = None
        identifier, stroke_id = state["distort"], self.smudge_selected_stroke_id
        if self.chapter is not None and self.chapter.to_dict() != state["before"]:
            self.replace_chapter(state["before"])
            if identifier in self.chapter.modifiers:
                self.active_modifier_id = identifier
                self.smudge_selected_stroke_id = stroke_id
            self._distort_changed()
        self.smudgeSelectedChanged.emit()
        self.smudgePointChanged.emit()
        self.interactionFinished.emit()
        self.update()
        return True

    def _move_smudge_handle(self, widget_point, pressure=None):
        state = self._modifier_handle_drag
        if not state or not state.get("smudge") or self.chapter is None:
            return False
        modifier = self.chapter.modifiers.get(state["distort"])
        if modifier is None:
            return False
        world = self.widget_to_document(widget_point)
        if state["mode"] == "draw":
            samples = state["samples"]
            pressure = samples[-1][2] if pressure is None else max(0., min(1., pressure))
            if math.dist(samples[-1][:2], world.toTuple()) * self.scale >= .5:
                samples.append((world.x(), world.y(), pressure))
                if len(samples) > 4096:
                    samples[:] = samples[::2] + [samples[-1]]
                self.update()
            return True
        stroke = next((s for s in modifier.parameters["strokes"] if s["id"] == state["stroke_id"]), None)
        if stroke is None:
            return True
        index = self.smudge_selected_point_index
        node, original = stroke["points"][index], state["original"]["points"][index]
        previous = copy.deepcopy(stroke)
        if state["mode"] == "point":
            delta = world - state["press"]
            for key in ("position", "handle"):
                node[key] = [original[key][0] + delta.x(), original[key][1] + delta.y()]
        elif state["mode"] == "handle":
            node["handle"] = [world.x(), world.y()]
        elif state["mode"] == "stroke":
            delta = world - state["press"]
            for node, original in zip(stroke["points"], state["original"]["points"]):
                for key in ("position", "handle"):
                    node[key] = [original[key][0] + delta.x(), original[key][1] + delta.y()]
        elif state["mode"] == "parameter":
            key = state["handle"]
            rect = self._smudge_gizmo_rects()[key]
            ratio = max(0., min(1., (rect.bottom() - widget_point.y()) / rect.height()))
            node[key] = round(.1 * 40960. ** ratio, 2) if key == "radius" else round(ratio * 100., 1)
        if previous != stroke:
            self._smudge_gizmo_changed()
        return True

    def _finish_smudge_handle(self):
        state = self._modifier_handle_drag
        if not state or not state.get("smudge"):
            return False
        self._modifier_handle_drag = None
        modifier = self.chapter.modifiers.get(state["distort"]) if self.chapter else None
        if modifier is None:
            return True
        if state["mode"] == "draw":
            samples = state["samples"]
            length = sum(math.dist(a[:2], b[:2]) for a, b in zip(samples, samples[1:]))
            if len(samples) >= 2 and length * self.scale >= 3:
                stroke = fit_smudge_stroke(samples, state["tool_settings"])
                modifier.parameters["strokes"].append(stroke)
                self.smudge_select(stroke["id"], 1)
        after = self.chapter.to_dict()
        if state["before"] != after:
            self.push_model_change(state["before"], after,
                                   "Draw smudge stroke" if state["mode"] == "draw" else "Edit smudge stroke")
            self._smudge_gizmo_changed()
        self.smudgeSelectedChanged.emit()
        self.interactionFinished.emit()
        self.update()
        return True
