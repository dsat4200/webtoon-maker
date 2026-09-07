"""Linear black-to-white source ranges for Posterize Value."""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QSizePolicy, QWidget

from comic_editor.core.models import POSTERIZE_VALUE_MIN_SPAN
from comic_editor.core.posterize import ValueStatistics


class ValueRangeMap(QWidget):
    rangesChanging = Signal(object)
    dragStarted = Signal()
    dragFinished = Signal()
    colorRequested = Signal(str)
    selectionChanged = Signal(str)

    def __init__(self, ranges, parent=None):
        super().__init__(parent)
        self.ranges = copy.deepcopy(ranges)
        self.statistics = ValueStatistics()
        self.selected_id = ranges[0].range_id
        self._drag = None
        self.setObjectName("posterizeValueMap")
        self.setAccessibleName("Posterize Value grayscale ranges")
        self.setMinimumWidth(136)
        self.setMinimumHeight(210)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setMouseTracking(True)
        self.setToolTip("Source brightness: black (0) to white (255). Bar height shows frequency.\n"
                        "Drag an internal boundary; click an output color to edit it.")

    def _columns(self, width=None):
        return max(1, int(((self.width() if width is None else width) - 24.) / 24.))

    def sizeHint(self):
        return QSize(260, self.heightForWidth(260))

    def heightForWidth(self, width):
        return 188 + math.ceil(len(self.ranges) / self._columns(width)) * 24

    def source_rect(self):
        return QRectF(12., 103., max(1., self.width() - 24.), 23.)

    def x_at(self, value):
        rect = self.source_rect()
        return rect.left() + value / 256. * rect.width()

    def handle_point(self, index):
        return QPointF(self.x_at(self.ranges[index].start), 130.)

    def span(self, index):
        end = self.ranges[index + 1].start if index + 1 < len(self.ranges) else 256.
        return end - self.ranges[index].start

    def swatches(self):
        rect, columns = self.source_rect(), self._columns()
        result = []
        for i, item in enumerate(self.ranges):
            row, column = divmod(i, columns)
            in_row = min(columns, len(self.ranges) - row * columns)
            result.append((item, QPointF(rect.left() + rect.width() * (column + .5) / in_row,
                                        177. + row * 24.),
                           QPointF(self.x_at(item.start + self.span(i) / 2.), 149.)))
        return result

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        foreground = self.palette().text().color()
        grid = QColor(foreground)
        grid.setAlpha(60)
        rect = self.source_rect()
        chart = QRectF(rect.left(), 28., rect.width(), 68.)
        painter.setPen(foreground)
        painter.drawText(QRectF(rect.left(), 0., rect.width(), 22.), Qt.AlignLeft | Qt.AlignVCenter, "Value")
        painter.drawText(QRectF(rect.left(), 0., rect.width(), 22.), Qt.AlignRight | Qt.AlignVCenter,
                         f"{len(self.ranges)} colors")
        painter.setPen(QPen(grid, 1.))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(chart)
        painter.drawLine(chart.left(), chart.center().y(), chart.right(), chart.center().y())
        peak = max(1., float(self.statistics.counts.max()))
        for value, count in enumerate(self.statistics.counts):
            height = float(count / peak * chart.height())
            if height:
                painter.fillRect(QRectF(self.x_at(value), chart.bottom() - height,
                                       max(.6, rect.width() / 256.), height),
                                 QColor(max(90, value), max(90, value), max(90, value)))
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        gradient.setColorAt(0., QColor("black"))
        gradient.setColorAt(1., QColor("white"))
        painter.fillRect(rect, gradient)
        painter.setPen(QPen(grid, 1.))
        painter.drawRect(rect)
        for i, item in enumerate(self.ranges):
            selected = item.range_id == self.selected_id
            band = QRectF(self.x_at(item.start), 137., self.span(i) / 256. * rect.width(), 12.)
            painter.fillRect(band, QColor(item.color))
            if selected:
                painter.setPen(QPen(QColor("#65bcff"), 2.))
                painter.setBrush(Qt.NoBrush)
                painter.drawRect(band)
            if i:
                point = self.handle_point(i)
                painter.setPen(QPen(QColor("#65bcff") if selected else foreground, 1.))
                painter.drawLine(QPointF(point.x(), chart.top()), QPointF(point.x(), 125.))
                half = min(4., rect.width() * POSTERIZE_VALUE_MIN_SPAN / 512.)
                painter.setBrush(QColor("#65bcff") if selected else QColor("white"))
                painter.drawPolygon(QPolygonF([QPointF(point.x() - half, 126.),
                                               QPointF(point.x() + half, 126.), QPointF(point.x(), 134.)]))
        for i, (item, point, anchor) in enumerate(self.swatches()):
            selected = item.range_id == self.selected_id
            painter.setPen(QPen(grid, 1.))
            if len(self.ranges) <= self._columns():
                painter.drawLine(anchor, point)
            painter.setBrush(QColor(item.color))
            painter.setPen(QPen(QColor("#65bcff") if selected else foreground, 2. if selected else 1.))
            painter.drawEllipse(point, 6., 6.)
        painter.setPen(foreground)
        painter.drawText(QRectF(rect.left(), 150., 40., 16.), Qt.AlignLeft, "0")
        painter.drawText(QRectF(rect.right() - 40., 150., 40., 16.), Qt.AlignRight, "255")

    def select(self, identifier):
        self.selected_id = identifier
        self.selectionChanged.emit(identifier)
        self.updateGeometry()
        self.update()

    def _handle_at(self, point):
        if len(self.ranges) < 2:
            return None
        distance, index = min((math.hypot(*(point - self.handle_point(i)).toTuple()), i)
                              for i in range(1, len(self.ranges)))
        return index if distance <= 9. else None

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        index = self._handle_at(event.position())
        if index is not None:
            self.select(self.ranges[index].range_id)
            self._drag = index
            self.dragStarted.emit()
        else:
            distance, identifier = min((math.hypot(*(event.position() - point).toTuple()), item.range_id)
                                       for item, point, _ in self.swatches())
            if distance <= 9.:
                self.select(identifier)
                self.colorRequested.emit(identifier)
            elif 28. <= event.position().y() <= 149.:
                value = (event.position().x() - self.source_rect().left()) / self.source_rect().width() * 256.
                index = max(i for i, item in enumerate(self.ranges) if item.start <= max(0., value))
                self.select(self.ranges[index].range_id)
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            self.setCursor(Qt.SplitHCursor if self._handle_at(event.position()) is not None else Qt.ArrowCursor)
            return
        index = self._drag
        low = self.ranges[index - 1].start + POSTERIZE_VALUE_MIN_SPAN
        high = (self.ranges[index + 1].start if index + 1 < len(self.ranges) else 256.) - POSTERIZE_VALUE_MIN_SPAN
        value = (event.position().x() - self.source_rect().left()) / self.source_rect().width() * 256.
        ranges = copy.deepcopy(self.ranges)
        ranges[index].start = max(low, min(high, value))
        self.ranges = ranges
        self.rangesChanging.emit(copy.deepcopy(ranges))
        self.selectionChanged.emit(self.selected_id)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None and event.button() == Qt.LeftButton:
            self._drag = None
            self.unsetCursor()
            self.dragFinished.emit()
        event.accept()
