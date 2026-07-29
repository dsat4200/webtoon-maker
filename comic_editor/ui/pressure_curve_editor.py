"""Interactive pressure response curve used by pencil presets."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, Signal
from PySide6.QtGui import QBrush, QColor, QFont, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QFrame

from comic_editor.core.pressure import PressureCurve

PADDING = 40
HANDLE_RADIUS = 10


def _inner(rect: QRect) -> QRectF:
    return QRectF(
        rect.x() + PADDING, rect.y() + PADDING,
        rect.width() - 2 * PADDING, rect.height() - 2 * PADDING,
    )


def _to_widget(rect: QRect, x: float, y: float) -> QPointF:
    graph = _inner(rect)
    return QPointF(
        graph.left() + x * graph.width(),
        graph.bottom() - y * graph.height(),
    )


def _to_ratio(rect: QRect, point: QPointF) -> tuple[float, float]:
    graph = _inner(rect)
    x = (point.x() - graph.left()) / max(1.0, graph.width())
    y = 1.0 - (point.y() - graph.top()) / max(1.0, graph.height())
    return max(0.0, min(1.0, x)), max(0.0, min(1.0, y))


class PressureCurveEditor(QFrame):
    curveChanged = Signal()

    def __init__(self, title: str, curve: PressureCurve, parent=None):
        super().__init__(parent)
        self.title = title
        self._curve = curve
        self._drag_handle: str | None = None
        self.setMinimumSize(280, 220)
        self.setMouseTracking(True)

    def curve(self) -> PressureCurve:
        return self._curve

    def set_curve(self, curve: PressureCurve) -> None:
        self._curve = curve
        self._curve.clamp()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor(28, 28, 34))
        graph = _inner(self.rect())
        painter.setPen(QPen(QColor(50, 50, 56), 1, Qt.DotLine))
        for index in range(1, 5):
            fraction = index / 4
            painter.drawLine(
                QPointF(graph.left(), graph.bottom() - fraction * graph.height()),
                QPointF(graph.right(), graph.bottom() - fraction * graph.height()),
            )
            painter.drawLine(
                QPointF(graph.left() + fraction * graph.width(), graph.top()),
                QPointF(graph.left() + fraction * graph.width(), graph.bottom()),
            )
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QColor(70, 70, 76))
        painter.drawRect(graph)
        p0 = _to_widget(self.rect(), 0, self._curve.minimum)
        p3 = _to_widget(self.rect(), 1, self._curve.maximum)
        control = _to_widget(
            self.rect(), self._curve.control_x, self._curve.control_y
        )
        path = QPainterPath(p0)
        path.cubicTo(control, control, p3)
        painter.setPen(QPen(QColor(80, 200, 255), 2.5))
        painter.drawPath(path)
        for point, color in (
            (p0, QColor(255, 180, 80)),
            (p3, QColor(255, 180, 80)),
            (control, QColor(80, 200, 255)),
        ):
            painter.setPen(QPen(QColor(40, 40, 40), 2))
            painter.setBrush(QBrush(color))
            painter.drawEllipse(point, HANDLE_RADIUS, HANDLE_RADIUS)
        font = QFont()
        font.setPointSize(10)
        font.setBold(True)
        painter.setFont(font)
        painter.setPen(QColor(200, 200, 210))
        painter.drawText(
            QRectF(8, 4, self.width() - 16, PADDING - 4),
            Qt.AlignCenter, self.title,
        )

    def _handle_at(self, point: QPointF) -> str | None:
        positions = {
            "min": _to_widget(self.rect(), 0, self._curve.minimum),
            "max": _to_widget(self.rect(), 1, self._curve.maximum),
            "control": _to_widget(
                self.rect(), self._curve.control_x, self._curve.control_y
            ),
        }
        return next((
            name for name in ("control", "min", "max")
            if math.dist(
                (point.x(), point.y()),
                (positions[name].x(), positions[name].y()),
            ) <= HANDLE_RADIUS * 1.8
        ), None)

    def mousePressEvent(self, event) -> None:  # noqa: N802
        self._drag_handle = self._handle_at(event.position())
        if self._drag_handle:
            self.setCursor(Qt.ClosedHandCursor)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._drag_handle:
            self.setCursor(
                Qt.OpenHandCursor
                if self._handle_at(event.position()) else Qt.ArrowCursor
            )
            return
        x, y = _to_ratio(self.rect(), event.position())
        if self._drag_handle == "min":
            self._curve.minimum = y
        elif self._drag_handle == "max":
            self._curve.maximum = y
        else:
            self._curve.control_x, self._curve.control_y = x, y
        self._curve.clamp()
        self.update()
        self.curveChanged.emit()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        self._drag_handle = None
        self.setCursor(Qt.ArrowCursor)
