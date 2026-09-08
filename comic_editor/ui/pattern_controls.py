"""Compact controls for realtime halftone and pixelate appearance modifiers."""
from __future__ import annotations

import copy
import secrets
from PySide6.QtCore import Qt, Signal, QRectF
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel,
    QPushButton, QSlider, QToolButton, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    HALFTONE_DOT_STYLES, HALFTONE_GRIDS, HALFTONE_MAX_GRADIENT_STOPS,
    HalftoneModifier,
)


class PatternNumber(QWidget):
    """A decimal spin box and slider sharing one precise model value."""

    def __init__(self, modifier, owner, attribute, label, low, high,
                 decimals=2, suffix="", parent=None):
        super().__init__(parent)
        self.setObjectName(f"patternRow_{attribute}")
        self.factor = 10 ** decimals
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        row = QHBoxLayout()
        name = QLabel(label, self)
        row.addWidget(name, 1)
        self.value = QDoubleSpinBox(self)
        self.value.setObjectName(f"patternValue_{attribute}")
        self.value.setAccessibleName(label)
        self.value.setDecimals(decimals)
        self.value.setRange(low, high)
        self.value.setSingleStep(1.0 if decimals == 0 else .1 if high > 3 else .01)
        self.value.setSuffix(suffix)
        self.value.setKeyboardTracking(False)
        self.value.setValue(getattr(modifier, attribute))
        self.value.setMinimumWidth(82)
        name.setBuddy(self.value)
        row.addWidget(self.value)
        layout.addLayout(row)
        self.slider = QSlider(Qt.Horizontal, self)
        self.slider.setObjectName(f"patternSlider_{attribute}")
        self.slider.setAccessibleName(label)
        self.slider.setRange(round(low * self.factor), round(high * self.factor))
        self.slider.setValue(round(self.value.value() * self.factor))
        self.slider.setSingleStep(1)
        self.slider.setPageStep(max(1, round((high - low) * self.factor / 20)))
        layout.addWidget(self.slider)

        def from_slider(position):
            current = position / self.factor
            self.value.blockSignals(True)
            self.value.setValue(current)
            self.value.blockSignals(False)
            owner.set_parameter(modifier.modifier_id, attribute, current,
                                not self.slider.isSliderDown())
            synchronize()

        def from_value(current):
            owner.set_parameter(modifier.modifier_id, attribute, current, True)
            synchronize()

        def synchronize():
            current = getattr(modifier, attribute)
            self.value.blockSignals(True)
            self.slider.blockSignals(True)
            self.value.setValue(current)
            self.slider.setValue(round(current * self.factor))
            self.slider.blockSignals(False)
            self.value.blockSignals(False)

        self.slider.sliderPressed.connect(owner.begin_parameter_drag)
        self.slider.valueChanged.connect(from_slider)
        self.slider.sliderReleased.connect(owner.finish_parameter_drag)
        self.value.valueChanged.connect(from_value)


class PatternSection(QWidget):
    resetRequested = Signal()

    def __init__(self, title, expanded=True, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 2, 0, 2)
        layout.setSpacing(3)
        self.toggle = QToolButton(self)
        self.toggle.setText(title)
        self.toggle.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        self.toggle.setCheckable(True)
        self.toggle.setChecked(expanded)
        self.toggle.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.toggle.setAutoRaise(True)
        header = QHBoxLayout()
        header.addWidget(self.toggle, 1)
        reset = QToolButton(self)
        reset.setText("Reset")
        reset.setToolTip(f"Reset {title.lower()} settings")
        reset.setAutoRaise(True)
        reset.clicked.connect(self.resetRequested)
        header.addWidget(reset)
        layout.addLayout(header)
        self.body = QWidget(self)
        self.form = QVBoxLayout(self.body)
        self.form.setContentsMargins(0, 0, 0, 0)
        self.form.setSpacing(6)
        layout.addWidget(self.body)
        self.body.setVisible(expanded)
        self.toggle.toggled.connect(self.body.setVisible)
        self.toggle.toggled.connect(lambda open_: self.toggle.setArrowType(Qt.DownArrow if open_ else Qt.RightArrow))


class GradientPreview(QWidget):
    stopSelected = Signal(int)
    stopMoved = Signal(int, float, bool)
    dragStarted = Signal()
    dragFinished = Signal()

    def __init__(self, modifier, parent=None):
        super().__init__(parent)
        self.modifier = modifier
        self.selected = 0
        self._drag = None
        self.setObjectName("halftoneGradientPreview")
        self.setMinimumHeight(43)
        self.setToolTip("Click a color stop to select it. Drag a stop to move it.")

    def paintEvent(self, event):  # noqa: N802
        painter = QPainter(self)
        area = QRectF(7, 2, max(1, self.width() - 14), 24)
        for x in range(7, self.width() - 7, 8):
            for y in range(2, 26, 8):
                painter.fillRect(x, y, 8, 8, QColor("#888888" if (x // 8 + y // 8) % 2 else "#cccccc"))
        gradient = QLinearGradient(area.topLeft(), area.topRight())
        from comic_editor.ui.pattern_rendering import gradient_lut
        samples = gradient_lut(self.modifier.gradient_stops, self.modifier.gradient_interpolation)
        for index, rgba in enumerate(samples):
            gradient.setColorAt(index / (len(samples) - 1), QColor.fromRgbF(*[float(channel) for channel in rgba]))
        painter.fillRect(area, gradient)
        painter.setRenderHint(QPainter.Antialiasing)
        for index, (position, color) in enumerate(self.modifier.gradient_stops):
            x = area.left() + area.width() * position
            painter.setPen(QPen(QColor("#65bcff" if index == self.selected else "#aaaaaa"), 2))
            painter.setBrush(QColor(color))
            painter.drawEllipse(QRectF(x - 5, 29, 10, 10))

    def mousePressEvent(self, event):  # noqa: N802
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        position = max(0., min(1., (event.position().x() - 7) / max(1, self.width() - 14)))
        self._drag = min(range(len(self.modifier.gradient_stops)),
                         key=lambda i: abs(self.modifier.gradient_stops[i][0] - position))
        self.selected = self._drag
        self.stopSelected.emit(self.selected)
        self.dragStarted.emit()
        self.update()

    def mouseMoveEvent(self, event):  # noqa: N802
        if self._drag is not None:
            position = max(0., min(1., (event.position().x() - 7) / max(1, self.width() - 14)))
            self.stopMoved.emit(self._drag, position, False)
            self._drag = self.selected

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._drag is not None:
            self._drag = None
            self.dragFinished.emit()


class HalftoneGradientEditor(QWidget):
    PRESETS = {
        "Black to white": ("#FF000000", "#FFFFFFFF"),
        "Warm print": ("#FF251536", "#FFCE5366", "#FFFFDFA0"),
        "Ocean": ("#FF101040", "#FF197BBA", "#FFBBF3DD"),
        "Sunset": ("#FF1C1259", "#FFA23B8B", "#FFFFA34E", "#FFFFFFBD"),
    }

    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.modifier, self.owner = modifier, owner
        self.setObjectName("halftoneGradientEditor")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(4)
        self.preview = GradientPreview(modifier, self)
        layout.addWidget(self.preview)
        row = QHBoxLayout()
        self.stop = QComboBox(self)
        self.stop.setAccessibleName("Selected gradient stop")
        row.addWidget(self.stop, 1)
        self.position = QDoubleSpinBox(self)
        self.position.setObjectName("gradientStopPosition")
        self.position.setAccessibleName("Gradient stop position")
        self.position.setRange(0, 100)
        self.position.setDecimals(1)
        self.position.setSuffix("%")
        self.position.setKeyboardTracking(False)
        row.addWidget(self.position)
        self.color = QPushButton("Color", self)
        self.color.setToolTip("Edit selected stop color")
        row.addWidget(self.color)
        layout.addLayout(row)
        actions = QHBoxLayout()
        self.add = QPushButton("Add", self)
        self.remove = QPushButton("Remove", self)
        actions.addWidget(self.add)
        actions.addWidget(self.remove)
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.distribute = QPushButton("Distribute", self)
        self.reverse = QPushButton("Reverse", self)
        actions.addWidget(self.distribute)
        actions.addWidget(self.reverse)
        layout.addLayout(actions)
        actions = QHBoxLayout()
        self.move_left = QPushButton("Move left", self)
        self.move_right = QPushButton("Move right", self)
        self.move_left.setToolTip("Move this color to the previous stop")
        self.move_right.setToolTip("Move this color to the next stop")
        actions.addWidget(self.move_left)
        actions.addWidget(self.move_right)
        layout.addLayout(actions)
        self.presets = QComboBox(self)
        self.presets.setAccessibleName("Gradient presets")
        self.presets.addItem("Choose a gradient preset…", None)
        for name in self.PRESETS:
            self.presets.addItem(name, name)
        layout.addWidget(self.presets)
        self.preview.stopSelected.connect(self.select)
        self.preview.stopMoved.connect(self.move_stop)
        self.preview.dragStarted.connect(owner.begin_parameter_drag)
        self.preview.dragFinished.connect(owner.finish_parameter_drag)
        self.stop.currentIndexChanged.connect(self.select)
        self.position.valueChanged.connect(lambda position: self.move_stop(self.preview.selected, position / 100., True))
        self.color.clicked.connect(self.choose_color)
        self.add.clicked.connect(self.add_stop)
        self.remove.clicked.connect(self.remove_stop)
        self.distribute.clicked.connect(self.distribute_stops)
        self.reverse.clicked.connect(self.reverse_stops)
        self.move_left.clicked.connect(lambda: self.move_color(-1))
        self.move_right.clicked.connect(lambda: self.move_color(1))
        self.presets.activated.connect(self.apply_preset)
        self.refresh()

    def refresh(self):
        self.stop.blockSignals(True)
        self.stop.clear()
        for i in range(len(self.modifier.gradient_stops)):
            self.stop.addItem(f"Stop {i + 1}")
        self.stop.blockSignals(False)
        self.select(min(self.preview.selected, len(self.modifier.gradient_stops) - 1))
        self.add.setEnabled(len(self.modifier.gradient_stops) < HALFTONE_MAX_GRADIENT_STOPS)
        self.remove.setEnabled(len(self.modifier.gradient_stops) > 2)

    def select(self, index):
        if not 0 <= index < len(self.modifier.gradient_stops):
            return
        self.preview.selected = index
        self.stop.blockSignals(True)
        self.stop.setCurrentIndex(index)
        self.stop.blockSignals(False)
        position, color = self.modifier.gradient_stops[index]
        self.position.blockSignals(True)
        self.position.setValue(position * 100.)
        self.position.blockSignals(False)
        swatch = QColor(color)
        text_color = "white" if swatch.lightnessF() < .5 else "black"
        self.color.setStyleSheet(f"background-color: {swatch.name(QColor.HexArgb)}; color: {text_color};")
        self.preview.update()
        self.move_left.setEnabled(index > 0)
        self.move_right.setEnabled(index < len(self.modifier.gradient_stops) - 1)

    def apply_stops(self, stops, commit=True, selected=None):
        if selected is not None:
            tagged = list(enumerate(stops))
            tagged.sort(key=lambda pair: pair[1][0])
            self.preview.selected = next(i for i, (old, _) in enumerate(tagged) if old == selected)
        self.owner.set_parameter(self.modifier.modifier_id, "gradient_stops", stops, commit)
        self.refresh()

    def move_stop(self, index, position, commit=True):
        stops = copy.deepcopy(self.modifier.gradient_stops)
        stops[index][0] = position
        self.apply_stops(stops, commit, index)

    def add_stop(self):
        stops = copy.deepcopy(self.modifier.gradient_stops)
        if len(stops) >= HALFTONE_MAX_GRADIENT_STOPS:
            return
        index = max(range(len(stops) - 1), key=lambda i: stops[i + 1][0] - stops[i][0])
        left, right = stops[index], stops[index + 1]
        from comic_editor.ui.pattern_rendering import gradient_lut
        sample = gradient_lut([[0., left[1]], [1., right[1]]],
                              self.modifier.gradient_interpolation, count=3)[1]
        color = QColor.fromRgbF(*[float(channel) for channel in sample])
        stops.append([(left[0] + right[0]) / 2., color.name(QColor.HexArgb)])
        self.apply_stops(stops, selected=len(stops) - 1)

    def remove_stop(self):
        if len(self.modifier.gradient_stops) > 2:
            stops = copy.deepcopy(self.modifier.gradient_stops)
            stops.pop(self.preview.selected)
            self.apply_stops(stops)

    def distribute_stops(self):
        count = len(self.modifier.gradient_stops)
        self.apply_stops([[i / (count - 1), color] for i, (_, color) in enumerate(self.modifier.gradient_stops)])

    def reverse_stops(self):
        self.apply_stops([[1. - position, color] for position, color in self.modifier.gradient_stops],
                         selected=self.preview.selected)

    def move_color(self, direction):
        index = self.preview.selected
        destination = index + direction
        if 0 <= destination < len(self.modifier.gradient_stops):
            stops = copy.deepcopy(self.modifier.gradient_stops)
            stops[index][1], stops[destination][1] = stops[destination][1], stops[index][1]
            self.apply_stops(stops, selected=destination)

    def apply_preset(self, index):
        colors = self.PRESETS.get(self.presets.itemData(index))
        if colors:
            self.apply_stops([[i / (len(colors) - 1), color] for i, color in enumerate(colors)])
        self.presets.setCurrentIndex(0)

    def choose_color(self):
        from comic_editor.ui.color_picker import choose_color
        index = self.preview.selected
        def apply(color):
            stops = copy.deepcopy(self.modifier.gradient_stops)
            stops[index][1] = color
            self.apply_stops(stops)
        self._popup = choose_color(self, self.modifier.gradient_stops[index][1], apply, "Gradient stop color")


class _PatternControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.modifier, self.owner = modifier, owner
        self.numbers, self.combos, self.checks = {}, {}, {}
        self.layout_ = QVBoxLayout(self)
        self.layout_.setContentsMargins(0, 0, 0, 0)
        self.layout_.setSpacing(6)
        self.layout_.setAlignment(Qt.AlignTop)

    def number(self, form, attribute, label, decimals=2, suffix=""):
        low, high = self.modifier.numeric_ranges()[attribute]
        row = PatternNumber(self.modifier, self.owner, attribute, label, low, high, decimals, suffix, self)
        self.numbers[attribute] = row
        form.addWidget(row)
        return row

    def combo(self, form, attribute, label, options):
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label, row))
        control = QComboBox(row)
        control.setObjectName(f"patternCombo_{attribute}")
        control.setAccessibleName(label)
        for text, value in options:
            control.addItem(text, value)
        control.setCurrentIndex(control.findData(getattr(self.modifier, attribute)))
        control.currentIndexChanged.connect(lambda _: self.owner.set_parameter(
            self.modifier.modifier_id, attribute, control.currentData(), True))
        layout.addWidget(control, 1)
        form.addWidget(row)
        self.combos[attribute] = control
        return control

    def check(self, form, attribute, label):
        control = QCheckBox(label, self)
        control.setObjectName(f"patternCheck_{attribute}")
        control.setChecked(getattr(self.modifier, attribute))
        control.toggled.connect(lambda checked: self.owner.set_parameter(
            self.modifier.modifier_id, attribute, checked, True))
        form.addWidget(control)
        self.checks[attribute] = control
        return control


class PixelateControls(_PatternControls):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(modifier, owner, parent)
        self.setObjectName("pixelateControls")
        self.number(self.layout_, "pixel_size", "Pixel size", 0, " px")
        self.number(self.layout_, "blur", "Pre-filter blur", 1, " px")
        for attribute in ("brightness", "contrast", "saturation"):
            self.number(self.layout_, attribute, attribute.title(), 0, "%")
        reset = QPushButton("Reset All", self)
        reset.setObjectName("pixelateResetAll")
        reset.setToolTip("Reset pixel size, blur, brightness, contrast and saturation")
        reset.clicked.connect(self.reset_all)
        self.layout_.addWidget(reset)

    def reset_all(self):
        defaults = type(self.modifier)()
        self.owner.begin_parameter_drag()
        for attribute in self.numbers:
            self.owner.set_parameter(self.modifier.modifier_id, attribute, getattr(defaults, attribute), False)
        self.owner.finish_parameter_drag()
        self.owner.refresh()


class HalftoneControls(_PatternControls):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(modifier, owner, parent)
        self.setObjectName("halftoneControls")
        self.sections = {}
        for title, expanded in (("Pattern", True), ("Image sampling", False), ("Dots and lines", True), ("Colors", True)):
            section = PatternSection(title, expanded, self)
            self.sections[title] = section
            self.layout_.addWidget(section)
            section.resetRequested.connect(lambda name=title: self.reset_section(name))
        pattern = self.sections["Pattern"].form
        self.combo(pattern, "grid_type", "Grid", [(key.title(), key) for key in HALFTONE_GRIDS])
        self.number(pattern, "spacing", "Spacing", 1)
        self.number(pattern, "rotation", "Grid rotation", 1, "°")
        self.number(pattern, "point_spacing", "Point spacing", 1)
        self.number(pattern, "smoothing_iterations", "Smoothing iterations", 0)
        self.number(pattern, "collide_min", "Minimum collision radius")
        self.number(pattern, "collide_max", "Maximum collision radius")
        self.check(pattern, "invert", "Invert")
        self.number(pattern, "level_min", "Minimum level")
        self.number(pattern, "level_max", "Maximum level")
        sampling = self.sections["Image sampling"].form
        self.combo(sampling, "fit_mode", "Fit", [("Short side", "short"), ("Long side", "long"), ("Width", "width"), ("Height", "height")])
        self.number(sampling, "base_resolution", "Base resolution", 0, " px")
        self.number(sampling, "blur", "Blur", 1, " px")
        self.number(sampling, "gamma", "Gamma")
        self.number(sampling, "contrast", "Contrast")
        self.number(sampling, "clamp_min", "Clamp minimum")
        self.number(sampling, "clamp_max", "Clamp maximum")
        dots = self.sections["Dots and lines"].form
        labels = {"incircle": "Incircle", "delaunay": "Delaunay"}
        self.combo(dots, "dot_style", "Style", [(labels.get(key, key.title()), key) for key in HALFTONE_DOT_STYLES])
        self.number(dots, "size", "Size")
        self.number(dots, "scale_factor", "Scale factor")
        self.number(dots, "line_width", "Line width")
        self.number(dots, "line_level_scale", "Line level scale")
        self.number(dots, "max_edge_length", "Maximum edge length", 1)
        self.number(dots, "max_necks", "Maximum necks", 0)
        self.number(dots, "merge_strength", "Merge strength")
        self.number(dots, "min_neck_width", "Minimum neck width")
        self.check(dots, "even_merge_tone", "Even merge tone")
        self.check(dots, "link_rotation", "Link dot and grid rotation")
        self.number(dots, "dot_rotation", "Dot rotation", 1, "°")
        self.number(dots, "sides", "Polygon sides", 0)
        self.check(dots, "star", "Star polygon")
        self.number(dots, "star_inner", "Star inner radius")
        self.number(dots, "corner_rounding", "Corner rounding")
        self.seed_button = QPushButton("Randomize stippling", self)
        self.seed_button.clicked.connect(lambda: owner.set_parameter(modifier.modifier_id, "stipple_seed", secrets.randbits(32), True))
        pattern.addWidget(self.seed_button)
        colors = self.sections["Colors"].form
        self.combo(colors, "color_mode", "Mode", [("Two colors", "two"), ("Gradient", "gradient"),
                                                   ("Source colors", "source"), ("Target layer", "target_layer")])
        self.target_layer_controls = QWidget(self)
        target_layout = QVBoxLayout(self.target_layer_controls)
        target_layout.setContentsMargins(0, 0, 0, 0)
        self.target_layer_label = QLabel(self.target_layer_controls)
        self.target_layer_label.setObjectName("halftoneTargetLayerName")
        self.target_layer_label.setWordWrap(True)
        target_layout.addWidget(self.target_layer_label)
        target_actions = QHBoxLayout()
        self.pick_target_layer = QPushButton("Pick layer or object", self.target_layer_controls)
        self.pick_target_layer.setObjectName("halftonePickTargetLayer")
        self.pick_target_layer.setCheckable(True)
        self.pick_target_layer.setChecked(owner.target_layer_pick_id == modifier.modifier_id)
        self.pick_target_layer.clicked.connect(lambda: owner.begin_target_layer_pick(modifier.modifier_id))
        target_actions.addWidget(self.pick_target_layer, 1)
        self.clear_target_layer = QPushButton("Clear", self.target_layer_controls)
        self.clear_target_layer.setObjectName("halftoneClearTargetLayer")
        self.clear_target_layer.clicked.connect(self.clear_target)
        target_actions.addWidget(self.clear_target_layer)
        target_layout.addLayout(target_actions)
        target_hint = QLabel("Pick a layer or object in the outline. Transparent areas use this object's colors. Press Escape to cancel.", self.target_layer_controls)
        target_hint.setWordWrap(True)
        target_layout.addWidget(target_hint)
        self.number(target_layout, "target_hue", "Hue", 0, "°")
        self.number(target_layout, "target_saturation", "Saturation", 0, "%")
        self.number(target_layout, "target_lightness", "Lightness", 0, "%")
        colors.addWidget(self.target_layer_controls)
        self.color_rows = {}
        for attribute, label in (("foreground", "Foreground"), ("background", "Background")):
            button = QPushButton(label, self)
            button.setObjectName(f"halftoneColor_{attribute}")
            button.setToolTip(getattr(modifier, attribute))
            button.setStyleSheet(f"border-left: 12px solid {QColor(getattr(modifier, attribute)).name()};")
            button.clicked.connect(lambda _=False, key=attribute, widget=button: self.choose_color(key, widget))
            self.color_rows[attribute] = button
            colors.addWidget(button)
        self.swap_colors = QPushButton("Swap colors", self)
        self.swap_colors.setObjectName("halftoneSwapColors")
        self.swap_colors.clicked.connect(self.swap_color_values)
        colors.addWidget(self.swap_colors)
        self.check(colors, "transparent_background", "Transparent background")
        self.combo(colors, "gradient_interpolation", "Interpolation", [("RGB", "rgb"), ("OKLCH", "oklch")])
        self.gradient = HalftoneGradientEditor(modifier, owner, self)
        colors.addWidget(self.gradient)
        self.source_hint = QLabel("Uses the colors of the incoming layer or object.", self)
        self.source_hint.setWordWrap(True)
        colors.addWidget(self.source_hint)
        for control in self.combos.values():
            control.currentIndexChanged.connect(self.update_visibility)
        self.checks["link_rotation"].toggled.connect(self.update_visibility)
        self.checks["star"].toggled.connect(self.update_visibility)
        self.checks["transparent_background"].toggled.connect(self.update_visibility)
        for row in self.numbers.values():
            row.value.valueChanged.connect(self.synchronize_numbers)
            row.slider.valueChanged.connect(self.synchronize_numbers)
        self.update_visibility()

    def synchronize_numbers(self, *_):
        for attribute, row in self.numbers.items():
            row.value.blockSignals(True)
            row.slider.blockSignals(True)
            value = getattr(self.modifier, attribute)
            row.value.setValue(value)
            row.slider.setValue(round(value * row.factor))
            row.value.blockSignals(False)
            row.slider.blockSignals(False)

    def reset_section(self, title):
        attributes = {
            "Pattern": ("grid_type", "spacing", "rotation", "invert", "level_min", "level_max",
                        "point_spacing", "smoothing_iterations", "collide_min", "collide_max", "stipple_seed"),
            "Image sampling": ("fit_mode", "base_resolution", "blur", "gamma", "contrast", "clamp_min", "clamp_max"),
            "Dots and lines": ("dot_style", "size", "scale_factor", "dot_rotation", "link_rotation", "sides", "star",
                               "star_inner", "corner_rounding", "line_width",
                               "line_level_scale", "max_edge_length", "max_necks", "merge_strength", "min_neck_width", "even_merge_tone"),
            "Colors": ("color_mode", "target_layer_id", "target_hue", "target_saturation", "target_lightness",
                       "foreground", "background", "transparent_background", "gradient_stops", "gradient_interpolation"),
        }[title]
        defaults = HalftoneModifier()
        self.owner.begin_parameter_drag()
        for attribute in attributes:
            self.owner.set_parameter(self.modifier.modifier_id, attribute, copy.deepcopy(getattr(defaults, attribute)), False)
        self.owner.finish_parameter_drag()
        self.owner.refresh()

    def update_visibility(self, *_):
        modifier = self.modifier
        polygon = modifier.dot_style == "polygon"
        self.numbers["sides"].setVisible(polygon)
        self.checks["star"].setVisible(polygon)
        self.numbers["star_inner"].setVisible(polygon and modifier.star)
        self.numbers["corner_rounding"].setVisible(modifier.dot_style in {"polygon", "square", "triangle", "delaunay", "liquid"})
        for attribute in ("line_width", "line_level_scale", "point_spacing"):
            self.numbers[attribute].setVisible(modifier.grid_type in {"line", "ring"})
        for attribute in ("smoothing_iterations", "collide_min", "collide_max"):
            self.numbers[attribute].setVisible(modifier.grid_type == "stippling")
        self.numbers["max_edge_length"].setVisible(modifier.dot_style == "delaunay")
        for attribute in ("max_necks", "merge_strength", "min_neck_width"):
            self.numbers[attribute].setVisible(modifier.dot_style == "blob")
        self.checks["even_merge_tone"].setVisible(modifier.dot_style == "liquid")
        self.numbers["dot_rotation"].setEnabled(not modifier.link_rotation)
        self.seed_button.setVisible(modifier.grid_type == "stippling")
        self.color_rows["foreground"].setVisible(modifier.color_mode == "two")
        self.swap_colors.setVisible(modifier.color_mode == "two")
        self.color_rows["background"].setEnabled(not modifier.transparent_background)
        self.gradient.setVisible(modifier.color_mode == "gradient")
        self.combos["gradient_interpolation"].parentWidget().setVisible(modifier.color_mode == "gradient")
        self.source_hint.setVisible(modifier.color_mode == "source")
        self.target_layer_controls.setVisible(modifier.color_mode == "target_layer")
        chapter = self.owner.canvas.chapter
        target = (chapter.layers.get(modifier.target_layer_id) or chapter.objects.get(modifier.target_layer_id)) if chapter else None
        self.target_layer_label.setText(target.name if target else "Selected source is unavailable" if modifier.target_layer_id else "No layer or object selected")
        self.clear_target_layer.setEnabled(bool(modifier.target_layer_id))
        self.gradient.preview.update()

    def clear_target(self):
        self.owner.cancel_target_layer_pick()
        self.owner.set_parameter(self.modifier.modifier_id, "target_layer_id", "", True)
        self.owner.refresh()

    def swap_color_values(self):
        foreground, background = self.modifier.foreground, self.modifier.background
        self.owner.begin_parameter_drag()
        self.owner.set_parameter(self.modifier.modifier_id, "foreground", background, False)
        self.owner.set_parameter(self.modifier.modifier_id, "background", foreground, False)
        self.owner.finish_parameter_drag()
        for attribute, button in self.color_rows.items():
            color = getattr(self.modifier, attribute)
            button.setToolTip(color)
            button.setStyleSheet(f"border-left: 12px solid {QColor(color).name()};")

    def choose_color(self, attribute, button):
        from comic_editor.ui.color_picker import choose_color
        def apply(value):
            self.owner.set_parameter(self.modifier.modifier_id, attribute, value, True)
            button.setStyleSheet(f"border-left: 12px solid {QColor(value).name()};")
            button.setToolTip(value)
        self._popup = choose_color(self, getattr(self.modifier, attribute), apply, attribute.title())
