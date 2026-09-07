"""Posterize source sampling and the interactive circular hue map."""
from __future__ import annotations

import copy
import math

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QTransform
from PySide6.QtWidgets import (
    QCheckBox, QHBoxLayout, QLabel, QPushButton, QSizePolicy, QSlider,
    QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    POSTERIZE_MAX_COLORS, POSTERIZE_MIN_SPAN, POSTERIZE_VALUE_MIN_SPAN,
    PosterizeRange, PosterizeValueModifier,
)
from comic_editor.core.posterize import HueStatistics, ValueStatistics, grayscale_source
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _straight


class PosterizeSampler:
    def __init__(self, value_mode=False):
        self.statistics_type = ValueStatistics if value_mode else HueStatistics
        self._key = None
        self._source_key = None
        self._samples = []
        self.statistics = self.statistics_type()

    def sample(self, canvas, targets, before_id=None, simplify=None):
        """Sample isolated artwork just before this stage, never its own output."""
        from comic_editor.ui.baking import visual_bounds
        chapter = canvas.chapter
        if simplify is None and before_id:
            simplify = chapter.modifiers.get(before_id)
        settings_key = (simplify.simplify_enabled, simplify.simplify_radius,
                        simplify.simplify_tolerance, simplify.simplify_strength) if simplify else None
        key, sources = [], []
        for kind, identifier in targets:
            target = chapter.modifier_target(kind, identifier)
            if target is None:
                continue
            original = target.modifier_ids
            prefix = original[:original.index(before_id)] if before_id in original else original[:]
            try:
                target.modifier_ids = prefix
                signature = (canvas._modifier_layer_signature(identifier) if kind == "layer"
                             else canvas._modifier_object_signature(target))
                bounds = visual_bounds(canvas, kind, identifier)
                parent = target.parent_id if kind == "layer" else target.parent_layer_id
                mapping = canvas.layer_world_transform(parent) if parent else QTransform()
                key.append((kind, identifier, signature, canvas._rect_signature(bounds),
                            tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4))))
                sources.append((kind, target, prefix, bounds, mapping))
            finally:
                target.modifier_ids = original
        source_key = (id(chapter), tuple(key))
        key = (source_key, settings_key)
        if key == self._key:
            return self.statistics
        samples = self._samples if source_key == self._source_key else []
        for kind, target, prefix, bounds, mapping in (sources if source_key != self._source_key else []):
            if bounds.isEmpty():
                continue
            inverse, valid = mapping.inverted()
            if not valid:
                continue
            scale = min(1., 512. / max(bounds.width(), bounds.height()))
            width, height = max(1, math.ceil(bounds.width() * scale)), max(1, math.ceil(bounds.height() * scale))
            image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing)
            painter.setRenderHint(QPainter.SmoothPixmapTransform)
            painter.scale(width / bounds.width(), height / bounds.height())
            painter.translate(-bounds.left(), -bounds.top())
            painter.setTransform(mapping, True)
            original = (target.modifier_ids, target.visible, target.mask_only,
                        canvas._interactive_render, canvas._rendering_compound_references)
            try:
                target.modifier_ids, target.visible, target.mask_only = prefix, True, False
                canvas._interactive_render = False
                canvas._rendering_compound_references = True
                if kind == "layer":
                    canvas._render_layer(painter, target, 1., bounds)
                else:
                    canvas._render_object(painter, target, 1., inverse.mapRect(bounds))
            finally:
                (target.modifier_ids, target.visible, target.mask_only,
                 canvas._interactive_render, canvas._rendering_compound_references) = original
                painter.end()
            samples.append((_straight(_qimage_premultiplied(image)), scale,
                            bounds.width() * bounds.height() / (width * height)))
        statistics = self.statistics_type()
        from comic_editor.core.color_smoothing import simplify_colors
        for pixels, scale, weight in samples:
            if self.statistics_type is ValueStatistics:
                pixels = grayscale_source(pixels)
            statistics.add(simplify_colors(pixels, simplify, scale) if simplify else pixels, weight)
        if sum(pixels.nbytes for pixels, _, _ in samples) <= 32 * 1024 * 1024:
            self._source_key, self._samples = source_key, samples
        else:
            self._source_key, self._samples = None, []
        self._key, self.statistics = key, statistics
        return statistics


class SimplifyColorsControls(QWidget):
    """Optional preprocessing, placed before the existing Posterize controls."""

    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.setObjectName("simplifyColorsControls")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)
        self.enabled = QCheckBox("Simplify colors", self)
        self.enabled.setChecked(modifier.simplify_enabled)
        self.enabled.setToolTip("Smooth fine color variations before posterizing, while protecting stronger edges.")
        layout.addWidget(self.enabled)
        self.body = QWidget(self)
        body = QVBoxLayout(self.body)
        body.setContentsMargins(0, 2, 0, 4)
        body.setSpacing(5)
        self.sliders, self.values = {}, {}
        for label, attribute, low, high, suffix, tooltip in (
            ("Detail size", "simplify_radius", 1, 24, " px",
             "Neighborhood radius in artwork pixels. Larger values smooth larger patches."),
            ("Color tolerance", "simplify_tolerance", 1, 100, "%",
             "Higher values blend more color differences. Lower values protect more boundaries."),
            ("Strength", "simplify_strength", 0, 100, "%",
             "How much simplified color to use before posterizing. Zero restores the original colors."),
        ):
            row = QHBoxLayout()
            row.setSpacing(3)
            name = QLabel(label, self.body)
            name.setToolTip(tooltip)
            row.addWidget(name, 1)
            value = QSpinBox(self.body)
            value.setRange(low, high)
            value.setSuffix(suffix)
            value.setValue(round(getattr(modifier, attribute)))
            value.setKeyboardTracking(False)
            value.setFixedWidth(58)
            value.setAccessibleName(label)
            value.setToolTip(tooltip)
            name.setBuddy(value)
            row.addWidget(value)
            body.addLayout(row)
            slider = QSlider(Qt.Horizontal, self.body)
            slider.setRange(low, high)
            slider.setValue(value.value())
            slider.setAccessibleName(label)
            slider.setToolTip(tooltip)
            body.addWidget(slider)
            value.valueChanged.connect(lambda _value: owner.begin_parameter_drag())
            slider.valueChanged.connect(value.setValue)
            value.valueChanged.connect(slider.setValue)
            slider.sliderPressed.connect(owner.begin_parameter_drag)
            slider.valueChanged.connect(lambda current, key=attribute:
                owner.set_parameter(modifier.modifier_id, key, float(current), False))
            slider.sliderReleased.connect(owner.finish_parameter_drag)
            value.editingFinished.connect(owner.finish_parameter_drag)
            self.sliders[attribute], self.values[attribute] = slider, value
        layout.addWidget(self.body)
        self.body.setVisible(modifier.simplify_enabled)
        self.enabled.toggled.connect(self.body.setVisible)
        self.enabled.toggled.connect(lambda enabled:
            owner.set_parameter(modifier.modifier_id, "simplify_enabled", enabled, True))


class HueRangeWheel(QWidget):
    rangesChanging = Signal(object)
    dragStarted = Signal()
    dragFinished = Signal()
    colorRequested = Signal(str)
    selectionChanged = Signal(str)

    def __init__(self, ranges, parent=None):
        super().__init__(parent)
        self.ranges = copy.deepcopy(ranges)
        self.statistics = HueStatistics()
        self.selected_id = ranges[0].range_id
        self._drag = None
        self.setObjectName("posterizeHueWheel")
        self.setAccessibleName("Posterize hue ranges")
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMouseTracking(True)
        self.setMinimumSize(136, 136)
        self.setMaximumHeight(360)
        policy = QSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        policy.setHeightForWidth(True)
        self.setSizePolicy(policy)
        self.setToolTip("Drag a boundary around the hue ring. Click a color to edit it.\n"
                        "Angle = hue; bar length = frequency. Gray pixels use hue 0°.")

    def sizeHint(self):
        return QSize(260, 260)

    def heightForWidth(self, width):
        return max(136, min(360, width))

    def geometry_values(self):
        return QPointF(self.width() / 2., self.height() / 2.), min(self.width(), self.height()) / 2. - 25.

    def point(self, hue, radius):
        center, _ = self.geometry_values()
        angle = math.radians(hue)
        return center + QPointF(math.cos(angle) * radius, -math.sin(angle) * radius)

    def hue_at(self, point):
        center, _ = self.geometry_values()
        return math.degrees(math.atan2(center.y() - point.y(), point.x() - center.x())) % 360.

    def span(self, index):
        return ((self.ranges[(index + 1) % len(self.ranges)].start - self.ranges[index].start) % 360.
                if len(self.ranges) > 1 else 360.)

    def swatches(self):
        # Distribute cramped swatches with a minimum angular spacing, keeping
        # their order and a leader line back to the actual range midpoint.
        _, radius = self.geometry_values()
        mids = [item.start + self.span(i) / 2. for i, item in enumerate(self.ranges)]
        if len(mids) > 1:
            spacing = min(math.degrees(16. / (radius + 16.)), 342. / len(mids))
            angles = mids[:]
            for _ in range(2 * len(angles)):
                for i in range(len(angles)):
                    j = (i + 1) % len(angles)
                    gap = angles[j] + (360. if j == 0 else 0.) - angles[i]
                    if gap < spacing:
                        shift = (spacing - gap) / 2.
                        angles[i] -= shift
                        angles[j] += shift
        else:
            angles = mids
        return [(item, self.point(angle, radius + 16.), self.point(mid, radius + 3.))
                for item, angle, mid in zip(self.ranges, angles, mids)]

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        center, radius = self.geometry_values()
        inner = radius * .38
        histogram_outer = radius - 28.
        foreground = self.palette().text().color()
        grid = QColor(foreground)
        grid.setAlpha(45)
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(grid, 1.))
        for fraction in (0., .5, 1.):
            r = inner + fraction * (histogram_outer - inner)
            painter.drawEllipse(center, r, r)
        peak = max(1., float(self.statistics.counts.max()))
        for degree, count in enumerate(self.statistics.counts):
            if count > 0:
                painter.setPen(QPen(QColor.fromHsv(degree, 155, 225), 1.3))
                painter.drawLine(self.point(degree + .5, inner),
                                 self.point(degree + .5, inner + count / peak * (histogram_outer - inner)))
        ring = QRectF(center.x() - radius + 16., center.y() - radius + 16.,
                      2 * (radius - 16.), 2 * (radius - 16.))
        for degree in range(360):
            painter.setPen(QPen(QColor.fromHsv(degree, 230, 245), 12.))
            painter.drawArc(ring, degree * 16, 17)
        output_ring = QRectF(center.x() - radius, center.y() - radius, radius * 2., radius * 2.)
        for i, item in enumerate(self.ranges):
            painter.setPen(QPen(QColor(item.color), 6.))
            painter.drawArc(output_ring, round(item.start * 16), round(self.span(i) * 16))
            painter.setPen(QPen(foreground, 1.))
            painter.drawLine(self.point(item.start, histogram_outer), self.point(item.start, radius + 4.))
        for item, position, anchor in self.swatches():
            selected = item.range_id == self.selected_id
            painter.setPen(QPen(foreground if selected else grid, 1.))
            painter.drawLine(anchor, position)
            painter.setBrush(QColor(item.color))
            painter.setPen(QPen(QColor("#65bcff") if selected else foreground, 2. if selected else 1.))
            painter.drawEllipse(position, 7., 7.)
        for item in self.ranges:
            position = self.point(item.start, radius - 16.)
            painter.setBrush(QColor("#65bcff") if item.range_id == self.selected_id else QColor("#ffffff"))
            painter.setPen(QPen(QColor("#202734"), 1.5))
            handle_radius = min(4.5, (radius - 16.) * math.sin(math.radians(POSTERIZE_MIN_SPAN / 2.)) - .5)
            painter.drawEllipse(position, handle_radius, handle_radius)
        painter.setPen(foreground)
        painter.setBrush(Qt.NoBrush)
        font = painter.font()
        font.setBold(True)
        painter.setFont(font)
        painter.drawText(QRectF(center.x() - inner, center.y() - 19., inner * 2., 22.),
                         Qt.AlignCenter, str(len(self.ranges)))
        font.setBold(False)
        font.setPointSizeF(max(7., font.pointSizeF() - 1.))
        painter.setFont(font)
        painter.drawText(QRectF(center.x() - inner, center.y() + 1., inner * 2., 18.),
                         Qt.AlignCenter, "colors")

    def select(self, identifier):
        self.selected_id = identifier
        self.selectionChanged.emit(identifier)
        self.update()

    def _handle_at(self, point):
        _, radius = self.geometry_values()
        distances = [(math.hypot(*(point - self.point(item.start, radius - 16.)).toTuple()), i)
                     for i, item in enumerate(self.ranges)]
        distance, index = min(distances)
        return index if distance <= 9. else None

    def mousePressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        index = self._handle_at(event.position())
        if index is not None:
            item = self.ranges[index]
            self.select(item.range_id)
            previous = self.ranges[index - 1].start
            following = self.ranges[(index + 1) % len(self.ranges)].start
            self._drag = {"id": item.range_id, "hue": self.hue_at(event.position()),
                          "value": item.start,
                          "low": item.start - ((item.start - previous) % 360.) + POSTERIZE_MIN_SPAN,
                          "high": item.start + ((following - item.start) % 360.) - POSTERIZE_MIN_SPAN}
            self.dragStarted.emit()
        else:
            distance, identifier = min((math.hypot(*(event.position() - position).toTuple()), item.range_id)
                                       for item, position, _ in self.swatches())
            if distance <= 9.:
                self.select(identifier)
                self.colorRequested.emit(identifier)
                event.accept()
                return
            hue = self.hue_at(event.position())
            for i, item in enumerate(self.ranges):
                if (hue - item.start) % 360. < self.span(i):
                    self.select(item.range_id)
                    break
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            self.setCursor(Qt.OpenHandCursor if self._handle_at(event.position()) is not None else Qt.ArrowCursor)
            return
        self.setCursor(Qt.ClosedHandCursor)
        hue = self.hue_at(event.position())
        delta = (hue - self._drag["hue"] + 180.) % 360. - 180.
        self._drag["hue"] = hue
        value = self._drag["value"] + delta
        if len(self.ranges) > 1:
            value = max(self._drag["low"], min(self._drag["high"], value))
        self._drag["value"] = value
        ranges = copy.deepcopy(self.ranges)
        next(item for item in ranges if item.range_id == self._drag["id"]).start = value % 360.
        self.ranges = sorted(ranges, key=lambda item: item.start)
        self.rangesChanging.emit(copy.deepcopy(self.ranges))
        self.selectionChanged.emit(self.selected_id)
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event):
        if self._drag is not None and event.button() == Qt.LeftButton:
            self._drag = None
            self.unsetCursor()
            self.dragFinished.emit()
        event.accept()


class PosterizeControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.owner, self.modifier_id = owner, modifier.modifier_id
        self.value_mode = isinstance(modifier, PosterizeValueModifier)
        self.extent = 256. if self.value_mode else 360.
        self.min_span = POSTERIZE_VALUE_MIN_SPAN if self.value_mode else POSTERIZE_MIN_SPAN
        self.sampler = PosterizeSampler(value_mode=self.value_mode)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        from comic_editor.ui.posterize_value_controls import ValueRangeMap
        self.wheel = (ValueRangeMap if self.value_mode else HueRangeWheel)(modifier.ranges, self)
        layout.addWidget(self.wheel)
        self.range_label = QLabel(self)
        self.range_label.setAlignment(Qt.AlignCenter)
        self.range_label.setWordWrap(True)
        layout.addWidget(self.range_label)
        row = QHBoxLayout()
        row.setSpacing(4)
        self.add_button = QToolButton(self)
        self.add_button.setText("+")
        self.remove_button = QToolButton(self)
        self.remove_button.setText("−")
        self.add_button.setFixedSize(28, 28)
        self.remove_button.setFixedSize(28, 28)
        self.color_button = QPushButton("Color…", self)
        self.add_button.setAccessibleName("Add range")
        self.remove_button.setAccessibleName("Remove range")
        row.addWidget(self.add_button)
        row.addWidget(self.remove_button)
        row.addWidget(self.color_button)
        layout.addLayout(row)
        hint = QLabel("Drag boundaries · Click colors to edit", self)
        hint.setAlignment(Qt.AlignCenter)
        hint.setWordWrap(True)
        layout.addWidget(hint)
        self.add_button.setToolTip("Split the selected range")
        self.remove_button.setToolTip("Merge the selected range into the previous range")
        self.wheel.dragStarted.connect(owner.begin_parameter_drag)
        self.wheel.rangesChanging.connect(lambda ranges: owner.set_parameter(self.modifier_id, "ranges", ranges, False))
        self.wheel.dragFinished.connect(owner.finish_parameter_drag)
        self.wheel.colorRequested.connect(self.choose_color)
        self.wheel.selectionChanged.connect(self.update_selection)
        self.add_button.clicked.connect(self.add_range)
        self.remove_button.clicked.connect(self.remove_range)
        self.color_button.clicked.connect(lambda: self.choose_color(self.wheel.selected_id))
        self._sample_timer = QTimer(self)
        self._sample_timer.setSingleShot(True)
        self._sample_timer.setInterval(180)
        self._sample_timer.timeout.connect(self.refresh_statistics)
        owner.canvas.documentChanged.connect(self.schedule_sample)
        self.refresh_statistics()
        self.update_selection()

    def schedule_sample(self, *_):
        self._sample_timer.start()

    def refresh_statistics(self):
        if self.owner.canvas.chapter is None or self.modifier_id not in self.owner.canvas.chapter.modifiers:
            return
        try:
            self.wheel.statistics = self.sampler.sample(self.owner.canvas, self.owner.targets(), self.modifier_id)
        except (ValueError, MemoryError) as error:
            self.range_label.setText(f"Sample unavailable: {error}")
            return
        self.wheel.update()
        self.update_selection()

    def update_selection(self, *_):
        index = next((i for i, item in enumerate(self.wheel.ranges) if item.range_id == self.wheel.selected_id), 0)
        item = self.wheel.ranges[index]
        span = self.wheel.span(index)
        counts = self.wheel.statistics.counts
        coverage = sum(value for source, value in enumerate(counts) if (source - item.start) % self.extent < span)
        percent = coverage * 100. / counts.sum() if counts.sum() else 0.
        interval = (f"Value {item.start:.1f} → {min(255., item.start + span):.1f}" if self.value_mode
                    else f"{item.start:.1f}° → {(item.start + span) % 360.:.1f}°")
        self.range_label.setText(f"{interval}\n{percent:.1f}% of visible color")
        self.add_button.setEnabled(len(self.wheel.ranges) < POSTERIZE_MAX_COLORS and span >= 2. * self.min_span)
        self.remove_button.setEnabled(len(self.wheel.ranges) > 1)

    def commit(self, ranges, selected_id):
        self.owner.set_parameter(self.modifier_id, "ranges", ranges, True)
        self.wheel.ranges = copy.deepcopy(ranges)
        self.wheel.select(selected_id)

    def add_range(self):
        if not self.add_button.isEnabled():
            return
        self.refresh_statistics()
        ranges = copy.deepcopy(self.wheel.ranges)
        index = next(i for i, item in enumerate(ranges) if item.range_id == self.wheel.selected_id)
        span = self.wheel.span(index) / 2.
        start = (ranges[index].start + span) % self.extent
        item = PosterizeRange(start, self.wheel.statistics.average(start, span, ranges[index].color))
        ranges.append(item)
        ranges.sort(key=lambda item: item.start)
        self.commit(ranges, item.range_id)

    def remove_range(self):
        if len(self.wheel.ranges) <= 1:
            return
        ranges = copy.deepcopy(self.wheel.ranges)
        index = next(i for i, item in enumerate(ranges) if item.range_id == self.wheel.selected_id)
        selected_id = ranges[index - 1].range_id
        ranges.pop(index)
        if self.value_mode and index == 0:
            ranges[0].start = 0.
            selected_id = ranges[0].range_id
        self.commit(ranges, selected_id)

    def choose_color(self, identifier):
        from comic_editor.ui.color_picker import choose_color
        item = next((item for item in self.wheel.ranges if item.range_id == identifier), None)
        if item is None:
            return
        def apply(color):
            ranges = copy.deepcopy(self.wheel.ranges)
            target = next((item for item in ranges if item.range_id == identifier), None)
            if target is not None:
                target.color = color
                self.commit(ranges, identifier)
        title = "Posterize Value range color" if self.value_mode else "Posterize range color"
        self._color_popup = choose_color(self, item.color, apply, title)
