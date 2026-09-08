"""Document-space array rig with screen-sized handles."""
import math
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPen, QTransform
from comic_editor.core.models import ArrayModifier


class ArrayFeatures:
    def _active_array_modifier(self):
        modifier = self.chapter.modifiers.get(self.active_modifier_id) if self.chapter else None
        if (self.modifier_mode and isinstance(modifier, ArrayModifier) and not modifier.muted
                and any(ref in self.chapter.modifier_target_ids(modifier.modifier_id)
                        for ref in self.selected_entities)):
            return modifier
        return None

    def _array_handle_points(self, modifier):
        return tuple(self.document_to_widget(QPointF(*p)) for p in
                     (modifier.axis_start, modifier.axis_end, modifier.center))

    def _draw_array_handles(self, painter):
        modifier = self._active_array_modifier()
        if modifier is None:
            return False
        start, end, center = self._array_handle_points(modifier)
        delta = end - start
        length = math.hypot(delta.x(), delta.y())
        direction = delta / max(1e-6, length)
        painter.save()
        painter.setTransform(QTransform())
        painter.setPen(QPen(QColor("#ff8b26"), 1.5, Qt.DotLine))
        extent = self.width() + self.height()
        painter.drawLine(start - direction*extent, end + direction*extent)
        painter.setPen(QPen(QColor("#ff8b26"), 2))
        painter.drawLine(start, end)
        # An arrow on the segment identifies forward even for backward arrays.
        tip = start + delta*.65
        normal = QPointF(-direction.y(), direction.x())
        if length > 25:
            painter.drawLine(tip, tip - direction*9 + normal*5)
            painter.drawLine(tip, tip - direction*9 - normal*5)
        for point in (start, end):
            painter.setPen(QPen(QColor("#452005"), 1.5))
            painter.setBrush(QColor("#ff8b26"))
            painter.drawEllipse(point, 7, 7)
        painter.setPen(QPen(QColor("#ff8b26"), 1))
        spacing = math.dist(modifier.axis_start, modifier.axis_end)
        angle = math.degrees(math.atan2(modifier.axis_end[1]-modifier.axis_start[1],
                                      modifier.axis_end[0]-modifier.axis_start[0]))
        painter.drawText((start + end)/2 + QPointF(8, -13), f"{spacing:.1f} px  ·  {angle:.1f}°")
        painter.setPen(QPen(QColor("#65bcff"), 2))
        painter.setBrush(QColor("#203f59"))
        painter.drawEllipse(center, 9, 9)
        painter.drawLine(center-QPointF(13, 0), center+QPointF(13, 0))
        painter.drawLine(center-QPointF(0, 13), center+QPointF(0, 13))
        painter.drawText(center+QPointF(14, -12), "Center")
        painter.restore()
        return True

    def _begin_array_handle(self, point):
        modifier = self._active_array_modifier()
        if modifier is None:
            return False
        points = self._array_handle_points(modifier)
        # Dots retain a reachable center when a pivot coincides with them;
        # the outer crosshair remains draggable as the independent pivot.
        hit = next((i for i, p in enumerate(points)
                    if math.dist(p.toTuple(), point.toTuple()) <= (14 if i == 2 else 9)), None)
        if hit is None:
            return False
        self._commit_text_edit()
        self._modifier_handle_drag = {
            "array": modifier.modifier_id, "handle": hit, "before": self.chapter.to_dict(),
            "press": self.widget_to_document(point),
            "origin": (modifier.axis_start, modifier.axis_end, modifier.center)[hit],
        }
        return True

    def _move_array_handle(self, point):
        state = self._modifier_handle_drag
        if not state or "array" not in state:
            return False
        modifier = self.chapter.modifiers.get(state["array"])
        if not isinstance(modifier, ArrayModifier):
            return False
        position = QPointF(*state["origin"]) + self.widget_to_document(point) - state["press"]
        position = self._snap(position, self.active_layer_id)
        setattr(modifier, ("axis_start", "axis_end", "center")[state["handle"]], position.toTuple())
        modifier.validate()
        self._invalidate_scene_cache()
        self.documentChanged.emit(None)
        self.update()
        return True
