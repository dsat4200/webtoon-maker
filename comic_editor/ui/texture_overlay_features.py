"""Selection-scoped texture placement gizmo, independent of layer transforms."""
import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QPainterPath, QPen, QPolygonF, QTransform
from comic_editor.core.models import TextureModifier


class TextureOverlayFeatures:
    def _active_texture_modifier(self):
        modifier = self.chapter.modifiers.get(self.active_modifier_id) if self.chapter else None
        if (self.modifier_mode and not (self.active_tone_mask_id or self.preview_tone_mask_id)
                and isinstance(modifier, TextureModifier) and not modifier.muted
                and any(ref in self.selected_entities for ref in self.chapter.modifier_target_ids(modifier.modifier_id))):
            return modifier
        return None

    def _texture_default_quad(self, modifier):
        bounds = None
        for ref in self.chapter.modifier_target_ids(modifier.modifier_id):
            rect = self.entity_world_rect(*ref)
            if rect is not None and not rect.isEmpty():
                bounds = rect if bounds is None else bounds.united(rect)
        bounds = bounds or QRectF(0, 0, 100, 100)
        return [bounds.topLeft().toTuple(), bounds.topRight().toTuple(),
                bounds.bottomRight().toTuple(), bounds.bottomLeft().toTuple()]

    def _texture_quad(self, modifier):
        return modifier.texture_quad or self._texture_default_quad(modifier)

    def _texture_mode_rect(self, modifier):
        quad = [self.document_to_widget(QPointF(*p)) for p in self._texture_quad(modifier)]
        top = (quad[0] + quad[1]) / 2
        return QRectF(top.x() - 42, top.y() - 34, 84, 24)

    def _texture_handle_hit(self, widget):
        modifier = self._active_texture_modifier()
        if modifier is None:
            return None
        if self._texture_mode_rect(modifier).contains(widget):
            return "mode"
        quad = self._texture_quad(modifier)
        distances = [math.dist(widget.toTuple(), self.document_to_widget(QPointF(*p)).toTuple())
                     for p in self._quad_handles(quad)]
        if min(distances) <= 10:
            return distances.index(min(distances))
        path = QPainterPath()
        path.addPolygon(QPolygonF([self.document_to_widget(QPointF(*p)) for p in quad]))
        if path.contains(widget):
            return "translate"
        return "rotate" if min(distances[:4]) <= 28 else None

    def _draw_texture_handles(self, painter):
        modifier = self._active_texture_modifier()
        if modifier is None:
            return False
        quad = self._texture_quad(modifier)
        painter.save()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#ff8b26"), 1.5, Qt.DotLine))
        painter.setBrush(Qt.NoBrush)
        painter.drawPolygon(QPolygonF([self.document_to_widget(QPointF(*p)) for p in quad]))
        painter.setPen(QPen(QColor("#ff8b26"), 1.2))
        painter.setBrush(QColor("#3e2a19"))
        for point in self._quad_handles(quad):
            widget = self.document_to_widget(QPointF(*point))
            painter.drawRect(QRectF(widget.x() - 4, widget.y() - 4, 8, 8))
        box = self._texture_mode_rect(modifier)
        painter.drawRoundedRect(box, 4, 4)
        painter.setPen(QColor("#ffffff"))
        painter.drawText(box, Qt.AlignCenter, modifier.transform_mode.title())
        painter.restore()
        return True

    def _begin_texture_handle(self, widget):
        modifier, hit = self._active_texture_modifier(), self._texture_handle_hit(widget)
        if modifier is None or hit is None:
            return False
        if hit == "mode":
            before = self.chapter.to_dict()
            modifier.transform_mode = "free" if modifier.transform_mode == "uniform" else "uniform"
            self.push_model_change(before, self.chapter.to_dict(), "Texture transform mode")
            self.documentChanged.emit(None)
            self.interactionFinished.emit()
            self.update()
            return True
        keys = self._input_press_modifiers
        keys = QGuiApplication.keyboardModifiers() if keys is None else keys
        self._modifier_handle_drag = dict(texture=modifier.modifier_id, handle=hit,
            before=self.chapter.to_dict(), quad=list(self._texture_quad(modifier)),
            press=self.widget_to_document(widget),
            uniform=modifier.transform_mode == "uniform" or bool(keys & Qt.ShiftModifier))
        return True

    def _move_texture_handle(self, widget):
        state = self._modifier_handle_drag
        if not state or "texture" not in state:
            return False
        modifier = self.chapter.modifiers.get(state["texture"]) if self.chapter else None
        if not isinstance(modifier, TextureModifier):
            return False
        point = self.widget_to_document(widget)
        start, handle = state["quad"], state["handle"]
        if handle == "translate":
            delta = point - state["press"]
            candidate = [(x + delta.x(), y + delta.y()) for x, y in start]
        elif handle == "rotate":
            center = QPointF(sum(x for x, _ in start) / 4, sum(y for _, y in start) / 4)
            delta, press = point - center, state["press"] - center
            angle = math.atan2(delta.y(), delta.x()) - math.atan2(press.y(), press.x())
            c, s = math.cos(angle), math.sin(angle)
            candidate = [(center.x() + (x-center.x())*c - (y-center.y())*s,
                          center.y() + (x-center.x())*s + (y-center.y())*c) for x, y in start]
        elif state["uniform"]:
            anchors = self._quad_handles(start)
            origin, initial = anchors[[2, 3, 0, 1, 6, 7, 4, 5][handle]], anchors[handle]
            factor = max(.01, math.dist(origin, point.toTuple()) / max(1e-6, math.dist(origin, initial)))
            candidate = [(origin[0] + (x-origin[0])*factor, origin[1] + (y-origin[1])*factor) for x, y in start]
        else:
            candidate = list(start)
            if handle < 4:
                candidate[handle] = point.toTuple()
            else:
                edge = handle - 4
                delta = point - QPointF(*self._edge_midpoints(start)[edge])
                for index in (edge, (edge + 1) % 4):
                    candidate[index] = (start[index][0] + delta.x(), start[index][1] + delta.y())
        if self._quad_is_valid(candidate):
            modifier.texture_quad = candidate
            modifier.validate()
            self._invalidate_scene_cache()
            self.documentChanged.emit(None)
            self.update()
        return True
