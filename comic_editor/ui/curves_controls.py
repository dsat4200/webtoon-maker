"""Histogram-backed tone curve editing inside the shared modifier card."""
from __future__ import annotations

import copy

import numpy as np
from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QPushButton, QSizePolicy, QToolButton, QVBoxLayout, QWidget)

from comic_editor.core.curves import CURVE_BLEND_MODES, CURVE_CHANNELS, evaluate_curve


IDENTITY = [(0., 0.), (1., 1.)]
POINT_GAP = .0001
CHANNEL_LABELS = {"master": "Master", "alpha": "Alpha", "red": "Red", "green": "Green",
    "blue": "Blue", "cyan": "Cyan", "magenta": "Magenta", "yellow": "Yellow",
    "black": "Black", "lightness": "Lightness", "a": "A", "b": "B"}
CHANNEL_COLORS = {"red": "#f77474", "green": "#79d993", "blue": "#78a9ff",
    "cyan": "#6cdee8", "magenta": "#e57fe2", "yellow": "#e6d36c"}


class CurveGraph(QWidget):
    pointsChanged = Signal(object)
    selectionChanged = Signal(int)
    dragStarted = Signal()
    dragFinished = Signal()
    dragCancelled = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.points = list(IDENTITY)
        self.selected_index = 0
        self.channel = "master"
        self.histogram = np.zeros(256)
        self.dragging = False
        self._drag_before = None
        self._drag_selection_before = 0
        self._press_position = None
        self.setObjectName("curvesGraph")
        self.setAccessibleName("Tone curve graph")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(160, 165)
        self.setMaximumHeight(330)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setToolTip("Click to add a point. Drag to adjust it. Arrow keys move the selected point.\n"
                        "Delete or right-click removes a point. Escape cancels a drag.")

    def sizeHint(self):
        return QSize(270, 235)

    def heightForWidth(self, width):
        return max(165, min(330, width - 25))

    def area(self):
        return QRectF(22., 9., max(1., self.width() - 33.), max(1., self.height() - 32.))

    def point_position(self, point):
        area = self.area()
        return QPointF(area.left() + point[0] * area.width(), area.bottom() - point[1] * area.height())

    def point_value(self, point):
        area = self.area()
        return (max(0., min(1., (point.x() - area.left()) / area.width())),
                max(0., min(1., (area.bottom() - point.y()) / area.height())))

    def set_points(self, points, selected=None):
        self.points = [tuple(point) for point in points]
        self.select(self.selected_index if selected is None else selected)

    def select(self, index):
        self.selected_index = max(0, min(len(self.points) - 1, index))
        self.selectionChanged.emit(self.selected_index)
        self.update()

    def set_histogram(self, bins):
        values = np.asarray(bins if bins is not None else np.zeros(256), dtype=float)
        self.histogram = (np.maximum(0., np.nan_to_num(values)) if values.shape == (256,)
                          else np.zeros(256))
        self.update()

    def curve_path(self):
        xs = np.unique(np.r_[np.linspace(0., 1., 257), [point[0] for point in self.points]])
        ys = evaluate_curve(self.points, xs)
        path = QPainterPath()
        for index, point in enumerate(zip(xs, ys)):
            if index:
                path.lineTo(self.point_position(point))
            else:
                path.moveTo(self.point_position(point))
        return path

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        area = self.area()
        painter.fillRect(area, QColor("#202126"))
        painter.save()
        painter.setClipRect(area)
        peak = max(1., float(self.histogram.max()))
        histogram_color = QColor(CHANNEL_COLORS.get(self.channel, "#aab1bc"))
        histogram_color.setAlpha(85)
        painter.setPen(Qt.NoPen)
        painter.setBrush(histogram_color)
        histogram = QPainterPath(area.bottomLeft())
        for index, count in enumerate(self.histogram):
            histogram.lineTo(area.left() + (index + .5) / 256. * area.width(),
                             area.bottom() - count / peak * (area.height() - 2.))
        histogram.lineTo(area.bottomRight())
        histogram.closeSubpath()
        painter.drawPath(histogram)
        painter.setPen(QPen(QColor("#42454d"), 1.))
        for step in (1, 2, 3):
            position = step / 4.
            painter.drawLine(self.point_position((position, 0)), self.point_position((position, 1)))
            painter.drawLine(self.point_position((0, position)), self.point_position((1, position)))
        painter.setPen(QPen(QColor("#858b97"), 1., Qt.DotLine))
        painter.drawLine(area.bottomLeft(), area.topRight())
        painter.setPen(QPen(QColor(CHANNEL_COLORS.get(self.channel, "#f2f3f5")), 1.8))
        painter.setBrush(Qt.NoBrush)
        painter.drawPath(self.curve_path())
        painter.restore()
        painter.setPen(QPen(QColor("#666a75"), 1.))
        painter.setBrush(Qt.NoBrush)
        painter.drawRect(area)
        for index, point in enumerate(self.points):
            selected = index == self.selected_index
            painter.setPen(QPen(QColor("#65bcff") if selected else QColor("#d5d9e2"), 1.5))
            painter.setBrush(QColor("#65bcff") if selected else QColor("#25272c"))
            painter.drawEllipse(self.point_position(point), 4. if selected else 3.5, 4. if selected else 3.5)
        horizontal = QLinearGradient(area.bottomLeft(), area.bottomRight())
        horizontal.setColorAt(0., QColor("black"))
        horizontal.setColorAt(1., QColor("white"))
        painter.fillRect(QRectF(area.left(), area.bottom() + 6., area.width(), 9.), horizontal)
        vertical = QLinearGradient(area.bottomLeft(), area.topLeft())
        vertical.setColorAt(0., QColor("black"))
        vertical.setColorAt(1., QColor("white"))
        painter.fillRect(QRectF(5., area.top(), 9., area.height()), vertical)

    def _near(self, point):
        distance, index = min((((self.point_position(value) - point).manhattanLength()), index)
                              for index, value in enumerate(self.points))
        return index if distance <= 12. else None

    def _change(self, points, selected=None):
        self.points = [tuple(point) for point in points]
        if selected is not None:
            self.selected_index = selected
        self.selected_index = max(0, min(len(self.points) - 1, self.selected_index))
        self.pointsChanged.emit(copy.deepcopy(self.points))
        self.selectionChanged.emit(self.selected_index)
        self.update()

    def move_selected(self, x, y):
        index = self.selected_index
        low = self.points[index - 1][0] + POINT_GAP if index else 0.
        high = self.points[index + 1][0] - POINT_GAP if index + 1 < len(self.points) else 1.
        points = list(self.points)
        points[index] = (max(low, min(high, x)), max(0., min(1., y)))
        self._change(points)

    def remove_selected(self):
        if len(self.points) <= 2:
            return
        self.dragStarted.emit()
        points = list(self.points)
        points.pop(self.selected_index)
        self._change(points, min(self.selected_index, len(points) - 1))
        self.dragFinished.emit()

    def mousePressEvent(self, event):
        nearest = self._near(event.position())
        if event.button() == Qt.RightButton:
            if nearest is not None:
                self.select(nearest)
                self.remove_selected()
            event.accept()
            return
        if event.button() != Qt.LeftButton or not self.area().adjusted(-6, -6, 6, 6).contains(event.position()):
            return super().mousePressEvent(event)
        self.setFocus(Qt.MouseFocusReason)
        self._drag_before = copy.deepcopy(self.points)
        self._drag_selection_before = self.selected_index
        self._press_position = QPointF(event.position())
        self.dragging = True
        self.dragStarted.emit()
        if nearest is not None:
            self.select(nearest)
        elif len(self.points) < 256:
            point = self.point_value(event.position())
            index = min(range(len(self.points)), key=lambda i: abs(self.points[i][0] - point[0]))
            if abs(self.points[index][0] - point[0]) < POINT_GAP:
                self.select(index)
            else:
                points = sorted([*self.points, point])
                self._change(points, points.index(point))
        event.accept()

    def mouseMoveEvent(self, event):
        if self.dragging:
            self.move_selected(*self.point_value(event.position()))
        else:
            self.setCursor(Qt.OpenHandCursor if self._near(event.position()) is not None else Qt.CrossCursor)
        event.accept()

    def mouseReleaseEvent(self, event):
        if self.dragging and event.button() == Qt.LeftButton:
            if event.position() != self._press_position:
                self.move_selected(*self.point_value(event.position()))
            self.dragging = False
            self._drag_before = None
            self.dragFinished.emit()
        event.accept()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape and self.dragging:
            self.dragging = False
            self._change(self._drag_before, self._drag_selection_before)
            self._drag_before = None
            self.dragCancelled.emit()
        elif event.key() in (Qt.Key_Delete, Qt.Key_Backspace):
            self.remove_selected()
        elif event.key() in (Qt.Key_Left, Qt.Key_Right, Qt.Key_Up, Qt.Key_Down):
            amount = 10. / 255. if event.modifiers() & Qt.ShiftModifier else 1. / 255.
            x, y = self.points[self.selected_index]
            self.dragStarted.emit()
            self.move_selected(x + amount * ((event.key() == Qt.Key_Right) - (event.key() == Qt.Key_Left)),
                               y + amount * ((event.key() == Qt.Key_Up) - (event.key() == Qt.Key_Down)))
            self.dragFinished.emit()
        else:
            return super().keyPressEvent(event)
        event.accept()


class CurvesControls(QWidget):
    """A compact graph plus precise controls; document edits use the card owner."""
    def __init__(self, owner, modifier, parent=None):
        super().__init__(parent or owner)
        self.owner, self.modifier = owner, modifier
        self.modifier_id = modifier.modifier_id
        self.channel = "master"
        self._editing = False
        self._curve_before = None
        self.setObjectName("curvesControls")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        row = QHBoxLayout()
        self.mode_combo = self._combo("curvesColorMode", "Color mode")
        for label, value in (("GREY", "gray"), ("RGB", "rgb"), ("CMYK", "cmyk"), ("LAB", "lab")):
            self.mode_combo.addItem(label, value)
        self.channel_combo = self._combo("curvesChannel", "Curve channel")
        row.addWidget(self.mode_combo, 1)
        row.addWidget(self.channel_combo, 1)
        layout.addLayout(row)
        self.graph = CurveGraph(self)
        layout.addWidget(self.graph)
        row = QHBoxLayout()
        self.x_value = self._number("curvesX", "Input X", 0., 1.)
        self.y_value = self._number("curvesY", "Output Y", 0., 1.)
        for label, widget in (("X", self.x_value), ("Y", self.y_value)):
            row.addWidget(QLabel(label, self))
            row.addWidget(widget, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.minimum = self._number("curvesMinimum", "Minimum input", 0., 15.999)
        self.maximum = self._number("curvesMaximum", "Maximum input", .001, 16.)
        for label, widget in (("Min", self.minimum), ("Max", self.maximum)):
            row.addWidget(QLabel(label, self))
            row.addWidget(widget, 1)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.setSpacing(3)
        self.picker_buttons = {}
        for name, label, tooltip in (("add_point", "+", "Add a point from the image"),
                ("black_point", "●", "Pick black point"), ("white_point", "○", "Pick white point"),
                ("gray_point", "G", "Pick gray point"), ("white_balance", "WB", "Pick white balance")):
            button = QToolButton(self)
            button.setObjectName("curvesPicker_" + name)
            button.setText(label)
            button.setToolTip(tooltip)
            button.setAccessibleName(tooltip)
            button.setMinimumSize(27, 25)
            button.clicked.connect(lambda _checked=False, mode=name: self._start_picker(mode))
            self.picker_buttons[name] = button
            row.addWidget(button)
        row.addStretch(1)
        layout.addLayout(row)
        row = QHBoxLayout()
        self.reset_button = QPushButton("Reset channel", self)
        self.reset_button.setObjectName("curvesReset")
        self.reset_button.setToolTip("Reset the selected channel to a straight line")
        row.addWidget(self.reset_button)
        self.reset_all_button = QPushButton("Reset all", self)
        self.reset_all_button.setObjectName("curvesResetAll")
        self.reset_all_button.setToolTip("Reset every channel curve and restore the 0–1 input range")
        row.addWidget(self.reset_all_button)
        layout.addLayout(row)
        row = QHBoxLayout()
        row.addWidget(QLabel("Blend", self))
        self.blend_combo = self._combo("curvesBlendMode", "Blend mode")
        for mode in CURVE_BLEND_MODES:
            self.blend_combo.addItem(mode.replace("_", " ").title(), mode)
        row.addWidget(self.blend_combo, 1)
        layout.addLayout(row)
        self.mode_combo.currentIndexChanged.connect(self._mode_changed)
        self.channel_combo.currentIndexChanged.connect(lambda _index: self.set_channel(self.channel_combo.currentData()))
        self.blend_combo.currentIndexChanged.connect(lambda _index: self._set("blend_mode", self.blend_combo.currentData(), True))
        self.graph.dragStarted.connect(self._begin_drag)
        self.graph.pointsChanged.connect(self._points_changed)
        self.graph.selectionChanged.connect(self._selection_changed)
        self.graph.dragFinished.connect(self._finish_drag)
        self.graph.dragCancelled.connect(self._cancel_drag)
        self.x_value.valueChanged.connect(lambda value: self._numeric_point(value, self.y_value.value()))
        self.y_value.valueChanged.connect(lambda value: self._numeric_point(self.x_value.value(), value))
        self.minimum.valueChanged.connect(lambda value: self._set("input_min", value, True))
        self.maximum.valueChanged.connect(lambda value: self._set("input_max", value, True))
        self.reset_button.clicked.connect(self.reset_curve)
        self.reset_all_button.clicked.connect(self.reset_all_curves)
        self._histogram_timer = QTimer(self)
        self._histogram_timer.setSingleShot(True)
        self._histogram_timer.setInterval(180)
        self._histogram_timer.timeout.connect(self.refresh_histogram)
        owner.canvas.documentChanged.connect(self._document_changed)
        if hasattr(owner.canvas, "chapterReplaced"):
            owner.canvas.chapterReplaced.connect(self._chapter_replaced)
        if hasattr(owner.canvas, "curvesPointSelected"):
            owner.canvas.curvesPointSelected.connect(self._picker_point_selected)
        self.sync_from_modifier()

    def _combo(self, name, accessible):
        combo = QComboBox(self)
        combo.setObjectName(name)
        combo.setAccessibleName(accessible)
        combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        combo.setMinimumContentsLength(6)
        combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return combo

    def _number(self, name, accessible, minimum, maximum):
        value = QDoubleSpinBox(self)
        value.setObjectName(name)
        value.setAccessibleName(accessible)
        value.setDecimals(4)
        value.setRange(minimum, maximum)
        value.setSingleStep(.01)
        value.setKeyboardTracking(False)
        value.setMinimumWidth(65)
        value.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        return value

    def _live_modifier(self):
        chapter = self.owner.canvas.chapter
        modifier = chapter.modifiers.get(self.modifier_id) if chapter is not None else None
        if modifier is not None:
            self.modifier = modifier
        return modifier

    def _key(self):
        return f"{self.modifier.color_mode}:{self.channel}"

    def _set(self, attribute, value, commit):
        if self._live_modifier() is None:
            return
        self._editing = True
        try:
            self.owner.set_parameter(self.modifier_id, attribute, value, commit)
        finally:
            self._editing = False
        if not self.graph.dragging:
            self.sync_from_modifier()

    def sync_from_modifier(self):
        if self._live_modifier() is None:
            self.setEnabled(False)
            self._histogram_timer.stop()
            return
        self.setEnabled(True)
        modifier = self.modifier
        channels = CURVE_CHANNELS[modifier.color_mode]
        if self.channel not in channels:
            self.channel = "master"
        self.mode_combo.blockSignals(True)
        self.mode_combo.setCurrentIndex(self.mode_combo.findData(modifier.color_mode))
        self.mode_combo.blockSignals(False)
        self.channel_combo.blockSignals(True)
        self.channel_combo.clear()
        for channel in channels:
            self.channel_combo.addItem(CHANNEL_LABELS.get(channel, channel.title()), channel)
        self.channel_combo.setCurrentIndex(self.channel_combo.findData(self.channel))
        self.channel_combo.blockSignals(False)
        self.graph.channel = self.channel
        self.graph.set_points(modifier.curves.get(self._key(), IDENTITY))
        for widget, value in ((self.minimum, modifier.input_min), (self.maximum, modifier.input_max)):
            widget.blockSignals(True)
            widget.setRange(0., modifier.input_max - POINT_GAP) if widget is self.minimum else widget.setRange(modifier.input_min + POINT_GAP, 16.)
            widget.setValue(value)
            widget.blockSignals(False)
        self.blend_combo.blockSignals(True)
        self.blend_combo.setCurrentIndex(self.blend_combo.findData(modifier.blend_mode))
        self.blend_combo.blockSignals(False)

    def set_channel(self, channel):
        if self._live_modifier() is None or channel not in CURVE_CHANNELS[self.modifier.color_mode]:
            return
        if self.graph.dragging:
            self.graph.dragging = False
            self._finish_drag()
        self.channel = channel
        self.sync_from_modifier()
        self.schedule_histogram()

    def select_point(self, index):
        self.graph.select(index)

    def _picker_point_selected(self, modifier_id, key, index):
        if modifier_id == self.modifier_id and self._live_modifier() is not None:
            mode, channel = key.split(":", 1)
            if mode == self.modifier.color_mode:
                self.set_channel(channel)
                self.select_point(index)

    def _mode_changed(self, _index):
        self.channel = "master"
        self._set("color_mode", self.mode_combo.currentData(), True)
        self.schedule_histogram()

    def _selection_changed(self, _index):
        index = self.graph.selected_index
        x, y = self.graph.points[index]
        self.x_value.blockSignals(True)
        self.y_value.blockSignals(True)
        self.x_value.setRange(self.graph.points[index - 1][0] + POINT_GAP if index else 0.,
                             self.graph.points[index + 1][0] - POINT_GAP if index + 1 < len(self.graph.points) else 1.)
        self.x_value.setValue(x)
        self.y_value.setValue(y)
        self.x_value.blockSignals(False)
        self.y_value.blockSignals(False)

    def _begin_drag(self):
        if self._live_modifier() is None:
            return
        self._curve_before = copy.deepcopy(self.modifier.curves)
        self.owner.begin_parameter_drag()
        self._histogram_timer.stop()

    def _points_changed(self, points):
        if self._live_modifier() is not None:
            self._set("curves", {**self.modifier.curves, self._key(): points}, False)

    def _finish_drag(self):
        self._curve_before = None
        self.owner.finish_parameter_drag()
        self.schedule_histogram()

    def _cancel_drag(self):
        if self._curve_before is not None:
            self._set("curves", self._curve_before, False)
        self._finish_drag()
        self.sync_from_modifier()

    def _numeric_point(self, x, y):
        self._begin_drag()
        self.graph.move_selected(x, y)
        self._finish_drag()

    def reset_curve(self):
        if self._live_modifier() is None:
            return
        curves = copy.deepcopy(self.modifier.curves)
        curves.pop(self._key(), None)
        self._set("curves", curves, True)
        self.graph.select(0)

    def reset_all_curves(self):
        if self._live_modifier() is None:
            return
        self.owner.begin_parameter_drag()
        try:
            self._set("curves", {}, False)
            self._set("input_min", 0., False)
            self._set("input_max", 1., False)
        finally:
            self.owner.finish_parameter_drag()
        self.graph.select(0)
        self.schedule_histogram()

    def _start_picker(self, mode):
        if self._live_modifier() is not None:
            self.owner.finish_parameter_drag()
            self.owner.canvas.start_curves_picker(self.modifier_id, mode, self.modifier.color_mode, self.channel)

    def _document_changed(self, *_args):
        if not self._editing and not self.graph.dragging:
            self.sync_from_modifier()
        self.schedule_histogram()

    def _chapter_replaced(self, *_args):
        # The old chapter's undo snapshot must never be committed into its
        # replacement if a document switch interrupts a graph gesture.
        if self._curve_before is not None:
            self.owner._parameter_before = None
        self._curve_before = None
        self.graph.dragging = False
        self.graph._drag_before = None
        self._document_changed()

    def schedule_histogram(self):
        if self.isVisible() and self.isEnabled() and self._live_modifier() is not None and not self.graph.dragging:
            self._histogram_timer.start()

    def refresh_histogram(self):
        if not self.isVisible() or self._live_modifier() is None:
            return
        if self.graph.dragging or getattr(self.owner, "_parameter_before", None) is not None:
            self._histogram_timer.start()
            return
        # Small provider adapters can supply statistics directly. Editor
        # canvases always expose the compiler and take the detached path.
        if not hasattr(self.owner.canvas, '_scene_snapshot_compiler'):
            self.graph.set_histogram(self.owner.canvas.curves_histogram(
                self.modifier_id, self.modifier.color_mode, self.channel))
            return
        from comic_editor.render.source_sampling import curves_histogram
        from comic_editor.ui.scene_consumers import scene_consumers
        import weakref
        reference = weakref.ref(self)
        def accept(bins, error):
            from shiboken6 import isValid
            control = reference()
            if control is not None and isValid(control):
                control.graph.set_histogram(None if error is not None else bins)
        scene_consumers(self.owner.canvas).request(('curves-histogram', self.modifier_id),
            curves_histogram, (self.modifier_id, self.modifier.color_mode, self.channel,
                               tuple(self.owner.canvas.selected_entities)), accept)

    def showEvent(self, event):
        super().showEvent(event)
        self.schedule_histogram()

    def hideEvent(self, event):
        self._histogram_timer.stop()
        if self.graph.dragging:
            self.graph.dragging = False
            self._finish_drag()
        super().hideEvent(event)
