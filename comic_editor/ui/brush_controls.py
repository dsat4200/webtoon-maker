"""Preset library and editable raster Brush controls."""
from __future__ import annotations

from dataclasses import replace
import math
from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, QTimer, QSignalBlocker, Signal
from PySide6.QtGui import QColor, QIcon, QPainter, QPainterPath, QPen, QPixmap, QValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox, QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QInputDialog, QLabel, QLineEdit,
    QMessageBox, QPushButton, QScrollArea, QSizePolicy, QStyle, QStyleOptionComboBox,
    QSlider, QStackedWidget, QStylePainter, QTabWidget, QVBoxLayout, QWidget,
)

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, SIGNED_COLOR_DYNAMICS
from .brush_preview_queue import live_brush_stroke, preview_queue


class BrushPreview(QLabel):
    """Debounced preview rendered by the same engine as the document stroke."""

    def __init__(self, parent=None, height=110):
        super().__init__(parent)
        self.setObjectName("brushStrokePreview")
        self.setMinimumHeight(height)
        self.setMaximumHeight(height)
        self.setMinimumWidth(80)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setAlignment(Qt.AlignCenter)
        self.setStyleSheet("background: #ffffff; border: 1px solid #777777;")
        self._definition = None
        self._color = None
        self._sub_color = None
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(120)
        self._timer.timeout.connect(self._render_when_visible)

    def set_definition(self, definition):
        preview_queue().cancel(self)
        self._definition = definition
        self._timer.start()

    def set_colors(self, color, sub_color):
        preview_queue().cancel(self)
        self._color = QColor(color) if color is not None else None
        self._sub_color = QColor(sub_color) if sub_color is not None else None
        self._timer.start()

    def _render(self):
        """Request cooperative rendering; export callers use the core helper."""
        self._request_render()

    def _render_when_visible(self):
        if self.isVisible():
            if live_brush_stroke(self):
                # A preset switch can leave a preview queued when pen-down
                # arrives. Its synthetic stroke must not block live input.
                self._timer.start()
                return
            self._request_render()

    def _request_render(self):
        if self._definition is not None:
            preview_queue().submit(self,self._definition,
                max(32,min(640,self.contentsRect().width())),self.height(),self._color,self._sub_color)

    def _preview_is_visible(self):
        return self.isVisible()

    def _preview_ready(self,pixmap,error,context):
        if error:
            self.setText("Preview unavailable")
            self.setToolTip(error)
        else:
            self.setPixmap(pixmap)
            self.setToolTip("The same sample path and pressure are used for every brush.")

    def hideEvent(self,event):
        self._timer.stop()
        preview_queue().cancel(self)
        super().hideEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        if self._definition is not None:
            self._timer.start()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        preview_queue().cancel(self)
        if self._definition is not None:
            self._timer.start()


class BrushPresetCombo(QComboBox):
    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._color = None
        self._sub_color = None
        self.setIconSize(QSize(132, 42))
        self.view().setMinimumWidth(340)
        self._preview_indices = []
        self._popup_open = False
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(self._prepare_next_preview)

    def set_colors(self, color, sub_color):
        preview_queue().cancel(self)
        self._color = QColor(color)
        self._sub_color = QColor(sub_color)
        for index in range(self.count()):
            self.setItemIcon(index, QIcon())
        if self._popup_open:
            self._preview_indices=list(range(self.count()))
            self._preview_timer.start(0)

    def paintEvent(self, event):
        # Only the popup contains stroke thumbnails. Keep the selected name
        # readable when the tool-settings sidebar is narrow.
        painter = QStylePainter(self)
        option = QStyleOptionComboBox()
        self.initStyleOption(option)
        option.currentIcon = QIcon()
        painter.drawComplexControl(QStyle.CC_ComboBox, option)
        painter.drawControl(QStyle.CE_ComboBoxLabel, option)

    def showPopup(self):
        # Generate only when requested; opening the application never renders
        # every brush in a potentially large imported collection.
        self._preview_indices = list(range(self.count()))
        self._popup_open = True
        super().showPopup()
        self._preview_timer.start(0)

    def hidePopup(self):
        self._popup_open = False
        preview_queue().cancel(self)
        self._preview_timer.stop()
        self._preview_indices = []
        super().hidePopup()

    def _prepare_next_preview(self):
        if not self._popup_open or not self._preview_indices:
            return
        index = self._preview_indices.pop(0)
        definition = next((data for data in self.settings.brush_presets
                           if data["id"] == self.itemData(index)), None)
        if definition is not None:
            try:
                preview_queue().submit(self,BrushDefinition.from_dict(definition),132,42,
                    self._color,self._sub_color,context=(index,self.itemData(index)),priority=1)
                return
            except Exception:
                self.setItemIcon(index, QIcon())
        if self._preview_indices:
            self._preview_timer.start(0)

    def _preview_is_visible(self):
        return self._popup_open and self.isVisible()

    def _preview_ready(self,pixmap,error,context):
        if self._popup_open and context:
            index,identifier=context
            if index<self.count() and self.itemData(index)==identifier:
                self.setItemIcon(index,QIcon(pixmap) if pixmap is not None else QIcon())
            if self._preview_indices:
                self._preview_timer.start(0)

    def hideEvent(self,event):
        self._popup_open=False
        self._preview_timer.stop()
        self._preview_indices=[]
        preview_queue().cancel(self)
        super().hideEvent(event)


class _WholeNumberBox(QDoubleSpinBox):
    """Accept fractional pasted/typed input, then round it on commit."""
    def _parsed_number(self, text):
        text = text.strip()
        if self.prefix() and text.startswith(self.prefix()):
            text = text[len(self.prefix()):]
        if self.suffix() and text.endswith(self.suffix()):
            text = text[:-len(self.suffix())]
        return self.locale().toDouble(text.strip())

    def validate(self, text, position):
        value, valid = self._parsed_number(text)
        if valid and math.isfinite(value):
            state = (QValidator.Acceptable if self.minimum() <= value <= self.maximum()
                     else QValidator.Intermediate)
            return state, text, position
        return super().validate(text, position)

    def valueFromText(self, text):
        value, valid = self._parsed_number(text)
        return math.floor(value + .5) if valid and math.isfinite(value) else self.value()


def _number(parent, value, low, high, step=.01, decimals=2):
    widget = (_WholeNumberBox if decimals == 0 else QDoubleSpinBox)(parent)
    widget.setRange(low, high)
    widget.setDecimals(decimals)
    widget.setSingleStep(step)
    widget.setValue(value)
    # Numeric displays round for readability. Opening and accepting a preset
    # must not round its imported settings until the user changes the field.
    widget._brush_original_value = value
    widget._brush_value_edited = False
    widget.valueChanged.connect(lambda _value: setattr(widget, "_brush_value_edited", True))
    return widget


def _number_slider(control, label, *, maximum=100):
    """Whole-number slider, with a wider typed range for large brush sizes."""
    row = QWidget(control.parentWidget())
    row.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
    layout = QHBoxLayout(row)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(6)
    slider = QSlider(Qt.Horizontal, row)
    slider.setRange(0, maximum)
    slider.setSingleStep(1)
    slider.setPageStep(10)
    slider.setMinimumWidth(40)
    slider.setAccessibleName(label)
    control.setAccessibleName(label)
    control.setButtonSymbols(QAbstractSpinBox.NoButtons)
    # Commit typed text together on Enter/focus loss. The editor's settings
    # receiver refreshes panels, which must not interrupt an unfinished number.
    control.setKeyboardTracking(False)
    control.setFixedWidth(86)
    control.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
    layout.addWidget(slider, 1)
    layout.addWidget(control)
    control._brush_slider = slider

    def from_number(value):
        # Keep an imported or typed size above the slider's practical range.
        # Moving its thumb during a refresh must not clamp the actual value.
        with QSignalBlocker(slider):
            slider.setValue(round(value))

    def from_slider(value):
        control.setValue(value)

    from_number(control.value())
    slider.valueChanged.connect(from_slider)
    control.valueChanged.connect(from_number)
    return row, slider


class BrushCurveEditor(QWidget):
    curveChanged = Signal(object)

    def __init__(self, points, parent=None):
        super().__init__(parent)
        self.points = list(points)
        self._drag = None
        self.setMinimumSize(320, 220)

    def _plot(self):
        return QRectF(24, 12, self.width() - 40, self.height() - 36)

    def _screen(self, point):
        area = self._plot()
        return QPointF(area.left() + point[0] * area.width(), area.bottom() - point[1] * area.height())

    def _normalized(self, point):
        area = self._plot()
        return (max(0, min(1, (point.x() - area.left()) / area.width())),
                max(0, min(1, (area.bottom() - point.y()) / area.height())))

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#282b30"))
        area = self._plot()
        painter.setPen(QPen(QColor("#545961"), 1))
        for step in range(5):
            amount = step / 4
            painter.drawLine(self._screen((amount, 0)), self._screen((amount, 1)))
            painter.drawLine(self._screen((0, amount)), self._screen((1, amount)))
        path = QPainterPath()
        for index, point in enumerate(self.points):
            if index:
                path.lineTo(self._screen(point))
            else:
                path.moveTo(self._screen(point))
        painter.setPen(QPen(QColor("#80c9ff"), 2))
        painter.drawPath(path)
        painter.setBrush(QColor("#e5f4ff"))
        for point in self.points:
            painter.drawEllipse(self._screen(point), 5, 5)
        painter.setPen(QColor("#c6ccd4"))
        painter.drawText(QRectF(area.left(), area.bottom() + 3, area.width(), 20), Qt.AlignCenter, "Input →")
        painter.end()

    def _changed(self):
        self.curveChanged.emit(tuple(self.points))
        self.update()

    def mousePressEvent(self, event):
        position = event.position()
        near = min(range(len(self.points)), key=lambda index: (
            self._screen(self.points[index]) - position).manhattanLength())
        close = (self._screen(self.points[near]) - position).manhattanLength() <= 14
        if event.button() == Qt.RightButton:
            if close and 0 < near < len(self.points) - 1:
                self.points.pop(near)
                self._changed()
            return
        if event.button() != Qt.LeftButton:
            return
        if close:
            self._drag = near
        elif self._plot().contains(position) and len(self.points) < 64:
            point = self._normalized(position)
            self.points.append(point)
            self.points.sort()
            self._drag = self.points.index(point)
            self._changed()
        event.accept()

    def mouseMoveEvent(self, event):
        if self._drag is None:
            return
        x, y = self._normalized(event.position())
        index = self._drag
        if index == 0:
            x = 0.0
        elif index == len(self.points) - 1:
            x = 1.0
        else:
            x = max(self.points[index - 1][0] + .001, min(self.points[index + 1][0] - .001, x))
        self.points[index] = (x, y)
        self._changed()
        event.accept()

    def mouseReleaseEvent(self, event):
        self._drag = None
        event.accept()


class BrushCurveButton(QPushButton):
    valueChanged = Signal(object)

    def __init__(self, points, parent=None):
        super().__init__("Edit response curve…", parent)
        self._curve = tuple(points)
        self.clicked.connect(self._edit)

    def value(self):
        return self._curve

    def _set_curve(self, points):
        self._curve = tuple(points)
        self.valueChanged.emit(self._curve)

    def _edit(self):
        before = self._curve
        dialog = QDialog(self)
        dialog.setWindowTitle("Brush response curve")
        layout = QVBoxLayout(dialog)
        explanation = QLabel("Drag a point to change the response. Click the graph to add a point; right-click an interior point to remove it.", dialog)
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        editor = BrushCurveEditor(self._curve, dialog)
        editor.curveChanged.connect(self._set_curve)
        layout.addWidget(editor)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, dialog)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            self._set_curve(before)


class BrushSettingsDialog(QDialog):
    """Detailed controls edit a copy; accepting publishes one complete preset."""

    def __init__(self, definition: BrushDefinition, parent=None, *, allow_dual=True,
                 color=None, sub_color=None):
        super().__init__(parent)
        self.setWindowTitle("Brush settings")
        self.resize(570, 650)
        self.definition = definition
        self._allow_dual = allow_dual
        self._dual_definition = definition.dual or BrushDefinition(id="secondary-tip", name="Second brush", size=definition.size)
        self._dual_reference_size = definition.size
        self.controls = {}
        self.dynamic_controls = {}
        self.texture_controls = {}
        self._taper_parameters = set(definition.taper_parameters)
        self._taper_minima = dict(definition.taper_minima)
        layout = QVBoxLayout(self)
        self.name = QLineEdit(definition.name, self)
        name_form = QFormLayout()
        name_form.addRow("Name", self.name)
        layout.addLayout(name_form)
        self.preview = BrushPreview(self, height=130)
        self.preview.set_colors(color, sub_color)
        layout.addWidget(self.preview)
        tabs = QTabWidget(self)
        layout.addWidget(tabs, 1)

        def page(title):
            widget = QWidget(tabs)
            form = QFormLayout(widget)
            form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
            area = QScrollArea(tabs)
            area.setWidgetResizable(True)
            area.setWidget(widget)
            tabs.addTab(area, title)
            return form

        def number(form, key, label, low=0, high=1, step=.01, decimals=2, scale=1):
            original = getattr(definition, key)
            control = _number(self, original * scale, low * scale, high * scale, step * scale, decimals)
            control._brush_original_value = original
            control._brush_value_scale = scale
            self.controls[key] = control
            if key in {"size", "opacity"}:
                row, slider = _number_slider(control, label, maximum=200 if key == "size" else 100)
                slider.setObjectName(f"brushSettings{key.title()}Slider")
                form.addRow(label, row)
            else:
                form.addRow(label, control)

        def boolean(form, key, label):
            control = QCheckBox(self)
            control.setChecked(bool(getattr(definition, key)))
            self.controls[key] = control
            form.addRow(label, control)

        def choice(form, key, label, options):
            control = QComboBox(self)
            for value in options:
                control.addItem(value.replace("_", " ").title(), value)
            if control.findData(getattr(definition, key)) < 0:
                value = getattr(definition, key)
                control.addItem(str(value).replace("_", " ").title(), value)
            control.setCurrentIndex(max(0, control.findData(getattr(definition, key))))
            self.controls[key] = control
            form.addRow(label, control)

        form = page("Stroke")
        number(form, "size", "Size (px)", .1, 4096, 1, decimals=0)
        self.controls["size"].setToolTip("The slider covers 0–200 px. Type a larger size when needed.")
        if allow_dual:
            boolean(form, "size_by_view", "Keep the same size on screen")
            self.controls["size_by_view"].setToolTip(
                "Keep the brush's apparent size while zooming. The view scale is captured at the start of each stroke. "
                "Linked second-brush sizes follow it. Previews use 100% view."
            )
        number(form, "opacity", "Stroke opacity", step=.01, decimals=0, scale=100)
        self.controls["opacity"].setSuffix(" %")
        number(form, "density", "Dab density")
        number(form, "hardness", "Hardness")
        boolean(form, "minimum_pixel", "Keep a minimum of one pixel")
        number(form, "thickness", "Tip thickness", .01, 10)
        number(form, "angle", "Tip angle (degrees)", -360, 360, 1)
        choice(form, "direction", "Direction", ("fixed", "stroke", "tilt", "rotation"))
        antialias = QComboBox(self)
        for index, label in enumerate(("None", "Weak", "Medium", "Strong")):
            antialias.addItem(label, index)
        antialias.setCurrentIndex(definition.antialiasing)
        self.controls["antialiasing"] = antialias
        form.addRow("Antialiasing", antialias)
        number(form, "spacing", "Dab spacing", .001, 4096, .001, 4)
        choice(form, "spacing_mode", "Spacing units", ("relative", "fixed"))
        boolean(form, "density_by_gap", "Adjust density by gap")
        boolean(form, "continuous", "Continuous paint")
        number(form, "continuous_rate", "Dabs per second", 1, 240, 1)
        self.continuous_note = QLabel(self)
        self.continuous_note.setWordWrap(True)
        form.addRow(self.continuous_note)

        form = page("Correction")
        boolean(form, "correct_velocity", "Correct drawing speed (unavailable)")
        self.controls["correct_velocity"].setEnabled(False)
        correction_note = "Drawing-speed correction is not implemented. The imported value is preserved but has no effect."
        self.controls["correct_velocity"].setToolTip(correction_note)
        form.labelForField(self.controls["correct_velocity"]).setToolTip(correction_note)
        number(form, "stabilization", "Stabilization")
        number(form, "post_correction", "Post-correction")
        self.controls["post_correction"].valueChanged.connect(self._update_continuous_controls)
        choice(form, "taper_mode", "Taper mode", ("length", "percentage", "fade"))
        number(form, "taper_start", "Starting taper", 0, 10000, 1)
        number(form, "taper_end", "Ending taper", 0, 10000, 1)
        self._ending_taper_label = form.labelForField(self.controls["taper_end"])
        number(form, "taper_minimum", "Default taper minimum")
        self.taper_target = QComboBox(self)
        for channel, label in (
            ("size", "Brush size"), ("opacity", "Opacity"), ("density", "Dab density"),
            ("thickness", "Tip thickness"), ("spacing", "Dab spacing"),
            ("particle_size", "Particle size"), ("particle_density", "Particle density"),
            ("texture_density", "Texture density"), ("paint_amount", "Amount of paint"),
            ("paint_density", "Density of paint"), ("blur", "Blur"),
            ("hardness", "Hardness"), ("color_stretch", "Color stretch"),
        ):
            self.taper_target.addItem(label, channel)
        form.addRow("Taper parameter", self.taper_target)
        self.taper_enabled = QCheckBox("Apply taper to this parameter", self)
        form.addRow(self.taper_enabled)
        self.taper_minimum_editor = _number(self, definition.taper_minimum, 0, 1)
        form.addRow("Parameter minimum", self.taper_minimum_editor)
        self.taper_target.currentIndexChanged.connect(self._load_taper_target)
        self.taper_enabled.toggled.connect(self._taper_target_edited)
        self.taper_minimum_editor.valueChanged.connect(self._taper_minimum_edited)
        self.controls["taper_minimum"].valueChanged.connect(self._load_taper_target)
        self._load_taper_target()
        self.controls["taper_mode"].currentIndexChanged.connect(self._update_taper_units)
        self._update_taper_units()

        form = page("Tips / spray")
        tip_names = QLabel("\n".join(f"{tip.name} ({tip.width} × {tip.height}, {tip.mode})" for tip in definition.tips), self)
        tip_names.setWordWrap(True)
        form.addRow("Tip materials", tip_names)
        choice(form, "repeat_mode", "Repeat", ("forward", "reverse", "random", "pingpong", "hold_last", "once", "one_random"))
        choice(form, "flip_x", "Flip horizontal", ("none", "fixed", "random", "alternate", "reverse"))
        choice(form, "flip_y", "Flip vertical", ("none", "fixed", "random", "alternate", "reverse"))
        for key in ("flip_x", "flip_y"):
            self.controls[key].setItemText(self.controls[key].findData("reverse"), "Reverse leg")
        boolean(form, "ribbon", "Ribbon")
        boolean(form, "spray", "Spraying effect")
        number(form, "particle_size", "Particle size", .001 if definition.particle_size_relative else .1,
               4096, .001 if definition.particle_size_relative else 1,
               4 if definition.particle_size_relative else 2)
        self._particle_size_label = form.labelForField(self.controls["particle_size"])
        boolean(form, "particle_size_relative", "Particle size relative to brush")
        self.controls["particle_size_relative"].toggled.connect(self._update_particle_size_units)
        self._update_particle_size_units()
        number(form, "particle_density", "Particles per dab", 1, 256, 1)
        number(form, "spray_deviation", "Spray deviation", -1, 1)
        number(form, "particle_angle", "Particle angle", -360, 360, 1)
        self.controls["particle_angle"].setSuffix(" °")
        self.controls["particle_angle"].setToolTip("Direction relative to the registered tip, independent of the whole brush angle.")
        choice(form, "particle_direction", "Particle direction", ("fixed", "stroke", "whole_spray", "center", "radial", "random"))
        for value, label in (("fixed", "Independent"), ("stroke", "Line direction"),
                             ("whole_spray", "Whole spray"), ("center", "Toward center"),
                             ("radial", "Outward (legacy)"), ("random", "Random (legacy)")):
            combo = self.controls["particle_direction"]
            combo.setItemText(combo.findData(value), label)
        number(form, "particle_angle_random", "Angle randomness")
        self.controls["particle_angle_random"].setToolTip("Adds random variation to any particle direction. 0 is fixed; 1 covers a full turn.")

        if allow_dual:
            form = page("Dual brush")
            self.dual_enabled = QCheckBox("Enable second brush", self)
            self.dual_enabled.setChecked(definition.dual is not None)
            form.addRow(self.dual_enabled)
            self.dual_label = QLabel(self._dual_definition.name, self)
            form.addRow("Second brush", self.dual_label)
            self.dual_edit = QPushButton("Edit second brush…", self)
            self.dual_edit.clicked.connect(self._edit_dual)
            form.addRow(self.dual_edit)
            boolean(form, "dual_link_size", "Link second brush size")
            boolean(form, "dual_apply_rgb", "Apply second brush RGB")
            choice(form, "dual_mode", "Second brush blend", (
                "normal", "multiply", "darken", "color_burn", "linear_burn", "subtract", "lighten",
                "screen", "color_dodge", "glow_dodge", "add", "add_glow", "overlay", "soft_light",
                "hard_light", "difference", "exclusion", "height", "height_linear",
            ))
            note = QLabel("The second brush uses its own tip, dynamics and texture. Its output combines with the main brush before the stroke is painted.", self)
            note.setWordWrap(True)
            form.addRow(note)

        form = page("Dynamics")
        description = QLabel("Choose a parameter, then adjust its response to pressure, tilt, speed or randomness. Imported response curves are preserved.", self)
        description.setWordWrap(True)
        form.addRow(description)
        self.controls["global_pressure_curve"] = BrushCurveButton(definition.global_pressure_curve, self)
        form.addRow("Input pressure compensation", self.controls["global_pressure_curve"])
        self.ribbon_dynamics_note = QLabel("Ribbons follow the stroke. Pen-controlled and random tip-angle changes are not supported yet. The fixed tip angle still applies; imported values are preserved.", self)
        self.ribbon_dynamics_note.setWordWrap(True)
        form.addRow(self.ribbon_dynamics_note)
        self.dynamics_selector = QComboBox(self)
        form.addRow("Parameter", self.dynamics_selector)
        self.dynamics_stack = QStackedWidget(self)
        form.addRow(self.dynamics_stack)
        self.dynamics_selector.currentIndexChanged.connect(self.dynamics_stack.setCurrentIndex)
        for channel, channel_label in (
            ("size", "Brush size"), ("opacity", "Opacity"), ("density", "Dab density"),
            ("thickness", "Tip thickness"), ("angle", "Tip angle"), ("spacing", "Dab spacing"),
            ("particle_size", "Particle size"), ("particle_density", "Particle density"),
            ("hue", "Hue variation"), ("saturation", "Saturation variation"),
            ("luminosity", "Luminosity variation"), ("sub_color_mix", "Secondary color mix"),
            ("hue_shift", "Hue offset"), ("saturation_shift", "Saturation offset"),
            ("luminosity_shift", "Luminosity offset"), ("sub_color_amount", "Secondary color amount"),
            ("texture_density", "Texture density"), ("paint_amount", "Amount of paint"),
            ("paint_density", "Density of paint"), ("blur", "Blur"),
            ("hardness", "Hardness"), ("color_stretch", "Color stretch"),
        ):
            values = definition.dynamics.get(channel, BrushDynamics())
            signed = channel in SIGNED_COLOR_DYNAMICS
            group = QWidget(self)
            group_form = QFormLayout(group)
            group_form.setContentsMargins(0, 8, 0, 8)
            if signed:
                note = QLabel("Negative minima reverse the color offset. Response curves remain normalized from 0 to 1.", group)
                note.setWordWrap(True)
                group_form.addRow(note)
            fields = {}
            for field, label in (("pressure", "Pen pressure"), ("tilt", "Tilt"), ("velocity", "Speed")):
                checkbox = QCheckBox(self)
                checkbox.setChecked(getattr(values, field))
                fields[field] = checkbox
                group_form.addRow(label, checkbox)
                minimum_key = "minimum" if field == "pressure" else f"{field}_minimum"
                spin = _number(self, getattr(values, minimum_key), -1 if signed else 0, 1)
                fields[minimum_key] = spin
                group_form.addRow(f"{label} minimum", spin)
                if field == "tilt" and not signed:
                    maximum = _number(self, values.tilt_maximum * 100, 0, 1000, 1, 2)
                    maximum.setSuffix("%")
                    maximum._brush_original_value = values.tilt_maximum
                    maximum._brush_value_scale = 100
                    maximum.setToolTip("100% is the original value. Larger values allow tilt to enlarge this parameter.")
                    fields["tilt_maximum"] = maximum
                    group_form.addRow("Tilt maximum", maximum)
                curve_key = f"{field}_curve"
                curve = BrushCurveButton(getattr(values, curve_key), self)
                fields[curve_key] = curve
                group_form.addRow(curve)
            fields["random"] = _number(self, values.random, -1 if signed else 0, 1)
            group_form.addRow("Random minimum (1 = off)", fields["random"])
            fields["velocity_scale"] = _number(self, values.velocity_scale, 1, 100000, 100, 0)
            group_form.addRow("Maximum speed (px/s)", fields["velocity_scale"])
            self.dynamic_controls[channel] = fields
            self.dynamics_stack.addWidget(group)
            self.dynamics_selector.addItem(channel_label, channel)
            if channel == "angle":
                self.angle_dynamics_group = group
            elif channel == "opacity":
                self.opacity_dynamics_group = group
        self.controls["ribbon"].toggled.connect(self._update_ribbon_controls)
        self._update_ribbon_controls()

        form = page("Paint / color")
        choice(form, "blending_mode", "Blending", (
            "normal", "darken", "multiply", "color_burn", "linear_burn", "subtract", "darker_color",
            "lighten", "screen", "color_dodge", "glow_dodge", "add", "add_glow", "lighter_color",
            "overlay", "soft_light", "hard_light", "vivid_light", "linear_light", "pin_light", "hard_mix",
            "difference", "exclusion", "divide", "hue", "saturation", "color", "luminosity", "erase", "background",
        ))
        choice(form, "blend_tips", "Combine overlapping tips", ("normal", "darken"))
        choice(form, "mixing_mode", "Color mixing", ("none", "blend", "running", "smear"))
        self.ink_compatibility_note = QLabel(
            "Opacity dynamics and other blending modes are unavailable with Blend or Running color. Stored settings are preserved.", self
        )
        self.ink_compatibility_note.setWordWrap(True)
        form.addRow(self.ink_compatibility_note)
        self.controls["mixing_mode"].currentIndexChanged.connect(self._mixing_mode_changed)
        choice(form, "mixing_space", "Mixing color space", ("standard", "perceptual"))
        choice(form, "color_change_target", "Color changes affect", ("main", "sub", "both"))
        for value, label in (("main", "Main color"), ("sub", "Secondary color"), ("both", "Both colors")):
            control = self.controls["color_change_target"]
            control.setItemText(control.findData(value), label)
        number(form, "hue_shift", "Hue offset", -1, 1, 1 / 360, scale=360)
        self.controls["hue_shift"].setSuffix("°")
        for key, label, minimum in (
            ("saturation_shift", "Saturation offset", -1),
            ("luminosity_shift", "Luminosity offset", -1),
            ("sub_color_amount", "Secondary color amount", 0),
        ):
            number(form, key, label, minimum, 1, scale=100)
            self.controls[key].setSuffix(" %")
        for key, label in (("paint_amount", "Amount of paint"), ("paint_density", "Density of paint"),
                           ("color_stretch", "Color stretch"),
                           ("hue_jitter", "Hue variation"), ("saturation_jitter", "Saturation variation"),
                           ("luminosity_jitter", "Luminosity variation"), ("sub_color_mix", "Mix secondary color"),
                           ("stroke_hue_jitter", "Hue variation per stroke"),
                           ("stroke_saturation_jitter", "Saturation variation per stroke"),
                           ("stroke_luminosity_jitter", "Luminosity variation per stroke"),
                           ("stroke_sub_color_mix", "Secondary color per stroke")):
            number(form, key, label)
        choice(form, "blur_mode", "Blur width mode", ("automatic", "fixed"))
        number(form, "blur_width", "Fixed blur width", 0, 10000, 1)
        self.controls["blur_width"].setSuffix(" px")
        self.controls["blur_mode"].currentIndexChanged.connect(self._running_blur_mode_changed)
        number(form, "watercolor_edge", "Watercolor edge width", 0, 100)
        number(form, "watercolor_opacity", "Watercolor edge opacity")
        number(form, "watercolor_darkness", "Watercolor edge darkness")
        number(form, "watercolor_blur", "Watercolor edge blur", 0, 100)
        boolean(form, "watercolor_after", "Watercolor edge after stroke")
        self.ribbon_paint_note = QLabel("Ribbon brushes do not yet support wet color mixing, spray particles, or painting while held still. Stored settings are preserved.", self)
        self.ribbon_paint_note.setWordWrap(True)
        form.addRow(self.ribbon_paint_note)
        if not allow_dual:
            note = QLabel("The main brush controls the combined stroke's blending, watercolor edge, and post-correction. Those second-brush settings are preserved but have no effect.", self)
            note.setWordWrap(True)
            form.addRow(note)
            for key in ("blending_mode", "post_correction", "watercolor_edge", "watercolor_opacity",
                        "watercolor_darkness", "watercolor_blur", "watercolor_after"):
                self.controls[key].setEnabled(False)
                self.controls[key].setToolTip("Controlled by the main brush for the combined stroke.")
        self.controls["watercolor_after"].toggled.connect(self._update_watercolor_controls)
        self._update_watercolor_controls()

        form = page("Texture / import")
        if definition.texture:
            texture = definition.texture
            form.addRow("Material", QLabel(texture.name, self))
            for key, label, low, high in (
                ("scale", "Scale", .01, 100), ("angle", "Angle", -360, 360),
                ("density", "Density", 0, 1), ("brightness", "Brightness", -1, 1),
                ("contrast", "Contrast", -1, 1),
            ):
                control = _number(self, getattr(texture, key), low, high)
                self.texture_controls[key] = control
                form.addRow(label, control)
            for key, label in (("invert", "Reverse"), ("per_dab", "Apply each dab"), ("emphasize_density", "Emphasize density")):
                control = QCheckBox(self)
                control.setChecked(getattr(texture, key))
                self.texture_controls[key] = control
                form.addRow(label, control)
            mode = QComboBox(self)
            for value in ("normal", "multiply", "subtract", "compare", "outline", "overlay",
                          "color_dodge", "color_burn", "hard_mix", "height"):
                mode.addItem(value.replace("_", " ").title(), value)
            if mode.findData(texture.mode) < 0:
                mode.addItem(f"Imported: {texture.mode}", texture.mode)
            mode.setCurrentIndex(max(0, mode.findData(texture.mode)))
            self.texture_controls["mode"] = mode
            form.addRow("Texture mode", mode)
        else:
            form.addRow(QLabel("This brush has no texture material.", self))
        from comic_editor.core.brush_units import has_physical_lengths
        if has_physical_lengths(definition):
            units = definition.source.get("length_units", {})
            resolution = QLabel(f"Millimeter measurements converted at {units.get('dpi', 300):g} DPI. Re-import at a different resolution to change physical sizing.", self)
            resolution.setWordWrap(True)
            form.addRow("Imported size", resolution)
        diagnostics = QLabel("\n\n".join(definition.warnings) or "No import warnings for this preset.", self)
        diagnostics.setWordWrap(True)
        diagnostics.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Compatibility", diagnostics)
        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel, self)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for control in list(self.controls.values()) + list(self.texture_controls.values()) + [
            control for fields in self.dynamic_controls.values() for control in fields.values()
        ]:
            if isinstance(control, QCheckBox):
                control.toggled.connect(self._preview_changed)
            elif isinstance(control, QComboBox):
                control.currentIndexChanged.connect(self._preview_changed)
            else:
                control.valueChanged.connect(self._preview_changed)
        if allow_dual:
            self.dual_enabled.toggled.connect(self._preview_changed)
        self._update_ribbon_controls()
        self._preview_changed()

    def _edit_dual(self):
        definition = self.result_definition().dual or self._dual_definition
        dialog = BrushSettingsDialog(replace(definition, dual=None), self, allow_dual=False,
                                     color=self.preview._color, sub_color=self.preview._sub_color)
        if dialog.exec() == QDialog.Accepted:
            self._dual_definition = replace(dialog.result_definition(), dual=None)
            self._dual_reference_size = self._value(self.controls["size"])
            self.dual_label.setText(self._dual_definition.name)
            self._preview_changed()

    def _update_taper_units(self):
        mode = self.controls["taper_mode"].currentData()
        percentage = mode == "percentage"
        for key in ("taper_start", "taper_end"):
            self.controls[key].setMaximum(100 if percentage else 10000)
            self.controls[key].setSuffix(" %" if percentage else " px")
        self.controls["taper_start"].setEnabled(mode != "fade")
        self._ending_taper_label.setText("Fade length" if mode == "fade" else "Ending taper")

    def _update_ribbon_controls(self):
        ribbon = self.controls["ribbon"].isChecked()
        self.controls["direction"].setEnabled(not ribbon)
        self.controls["direction"].setToolTip("Ribbons follow the stroke; pen-controlled direction is unavailable." if ribbon else "")
        self.angle_dynamics_group.setEnabled(not ribbon)
        self.ribbon_dynamics_note.setVisible(ribbon)
        for key in ("mixing_mode", "mixing_space", "paint_amount", "paint_density", "color_stretch",
                    "blur", "spray"):
            control = self.controls.get(key)
            if control is not None:
                control.setEnabled(not ribbon)
                control.setToolTip("This setting is not yet supported for ribbons." if ribbon else "")
        for channel in ("paint_amount", "paint_density", "color_stretch", "blur"):
            for control in self.dynamic_controls.get(channel, {}).values():
                control.setEnabled(not ribbon)
        if hasattr(self, "ribbon_paint_note"):
            self.ribbon_paint_note.setVisible(ribbon)
        self._update_continuous_controls()
        self._update_ink_controls()

    def _update_continuous_controls(self):
        ribbon = "ribbon" in self.controls and self.controls["ribbon"].isChecked()
        corrected = self._allow_dual and self._value(self.controls["post_correction"]) > 0
        if ribbon:
            note = "Continuous paint is not yet supported for ribbons. The saved setting is preserved."
        elif corrected:
            note = "Post-correction disables continuous paint for both brushes. The saved setting is preserved."
        else:
            note = ""
        for key in ("continuous", "continuous_rate"):
            self.controls[key].setEnabled(not (ribbon or corrected))
            self.controls[key].setToolTip(note or "Add paint over time, whether moving or held still.")
        self.continuous_note.setText(note)
        self.continuous_note.setVisible(bool(note))

    def _update_ink_controls(self):
        if "mixing_mode" not in self.controls:
            return
        wet = self.controls["mixing_mode"].currentData() in {"blend", "running"}
        self.controls["blending_mode"].setEnabled(not wet and self._allow_dual)
        if wet:
            self.controls["blending_mode"].setToolTip("Blend and Running color use Normal blending.")
        elif self._allow_dual:
            self.controls["blending_mode"].setToolTip("")
        self.opacity_dynamics_group.setEnabled(not wet)
        self.opacity_dynamics_group.setToolTip(
            "Opacity dynamics are unavailable with Blend or Running color." if wet else ""
        )
        self.ink_compatibility_note.setVisible(wet)
        self._update_running_blur_controls()

    def _mixing_mode_changed(self):
        if ("blur_mode" in self.controls and self.controls["mixing_mode"].currentData() == "running"
                and self.controls["blur_mode"].currentData() == "automatic"):
            self._automatic_blur_selected = True
        self._update_ink_controls()

    def _running_blur_mode_changed(self):
        if self.controls["blur_mode"].currentData() == "automatic":
            self._automatic_blur_selected = True
        self._update_running_blur_controls()

    def _update_running_blur_controls(self):
        if "blur_mode" not in self.controls:
            return
        ribbon = self.controls["ribbon"].isChecked()
        running = self.controls["mixing_mode"].currentData() == "running" and not ribbon
        fixed = self.controls["blur_mode"].currentData() == "fixed"
        mode, width = self.controls["blur_mode"], self.controls["blur_width"]
        mode.setEnabled(running)
        width.setEnabled(running and fixed)
        mode.setToolTip("Automatic adjusts blur to the brush size; Fixed uses a width in pixels."
                        if running else "Available with Running color on a non-ribbon brush.")
        width.setToolTip("Fixed blur width in document pixels." if running and fixed else
                         "Choose Running color and Fixed blur width mode. The saved width is preserved.")
        for control in self.dynamic_controls.get("blur", {}).values():
            control.setEnabled(running)

    def _update_watercolor_controls(self):
        after = self.controls["watercolor_after"].isChecked()
        blur = self.controls["watercolor_blur"]
        blur.setEnabled(self._allow_dual and after)
        if not self._allow_dual:
            blur.setToolTip("Controlled by the main brush for the combined stroke.")
        elif not after:
            blur.setToolTip("Enable Watercolor edge after stroke to apply edge blur. The saved blur width is preserved.")
        else:
            blur.setToolTip("")

    def _update_particle_size_units(self):
        relative = self.controls["particle_size_relative"].isChecked()
        control = self.controls["particle_size"]
        control.setDecimals(4 if relative else 2)
        control.setMinimum(.001 if relative else .1)
        control.setSingleStep(.001 if relative else 1)
        control.setSuffix(" ×" if relative else " px")
        self._particle_size_label.setText("Particle / brush size" if relative else "Particle size")
        control.setToolTip("Ratio of the brush size; 0.01 means 1%." if relative else "Particle diameter in pixels.")

    def _load_taper_target(self):
        channel = self.taper_target.currentData()
        self.taper_enabled.blockSignals(True)
        self.taper_minimum_editor.blockSignals(True)
        try:
            self.taper_enabled.setChecked(channel in self._taper_parameters)
            self.taper_minimum_editor.setEnabled(channel in self._taper_parameters)
            self.taper_minimum_editor.setValue(self._taper_minima.get(
                channel, self.controls["taper_minimum"].value()))
        finally:
            self.taper_enabled.blockSignals(False)
            self.taper_minimum_editor.blockSignals(False)

    def _taper_target_edited(self, enabled):
        channel = self.taper_target.currentData()
        if enabled:
            self._taper_parameters.add(channel)
        else:
            self._taper_parameters.discard(channel)
        self.taper_minimum_editor.setEnabled(enabled)
        self._preview_changed()

    def _taper_minimum_edited(self, value):
        self._taper_minima[self.taper_target.currentData()] = value
        self._preview_changed()

    def _preview_changed(self):
        self.preview.set_definition(self.result_definition())

    @staticmethod
    def _value(control):
        if isinstance(control, QCheckBox):
            return control.isChecked()
        if isinstance(control, QComboBox):
            return control.currentData()
        if not getattr(control, "_brush_value_edited", True):
            return control._brush_original_value
        value = control.value()
        scale = getattr(control, "_brush_value_scale", None)
        return value / scale if scale is not None else value

    def result_definition(self) -> BrushDefinition:
        values = {key: self._value(control) for key, control in self.controls.items()}
        if getattr(self, "_automatic_blur_selected", False) and values["blur_mode"] == "automatic":
            # Preserve untouched legacy normalized strengths. An explicit new
            # Automatic choice should work even when that old strength was 0.
            values["blur"] = 1.
        dynamics = dict(self.definition.dynamics)
        for channel, controls in self.dynamic_controls.items():
            value = replace(dynamics.get(channel, BrushDynamics()), **{
                key: self._value(control) for key, control in controls.items()
            })
            if channel in dynamics or value != BrushDynamics():
                dynamics[channel] = value
        texture = self.definition.texture
        if texture:
            texture = replace(texture, **{key: self._value(control) for key, control in self.texture_controls.items()})
        dual = self._dual_definition if self._allow_dual and self.dual_enabled.isChecked() else None
        if (dual is not None and values.get("dual_link_size", False)
                and values["size"] != self._dual_reference_size):
            dual = replace(dual, size=dual.size * values["size"] / max(.1, self._dual_reference_size))
        taper_parameters = tuple(name for name in self.definition.taper_parameters
                                 if name in self._taper_parameters)
        taper_parameters += tuple(sorted(self._taper_parameters - set(taper_parameters)))
        name = self.name.text()
        if name != self.definition.name:
            name = name.strip() or self.definition.name
        result = replace(self.definition, name=name,
                         dynamics=dynamics, texture=texture, dual=dual,
                         taper_parameters=taper_parameters,
                         taper_minima=dict(self._taper_minima), **values)
        return BrushDefinition.from_dict(result.to_dict())


class BrushControls(QWidget):
    settingsChanged = Signal()

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.settings = settings
        self._loading = False
        self._slider_change_pending = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        form = QFormLayout()
        form.setRowWrapPolicy(QFormLayout.WrapAllRows)
        form.setFieldGrowthPolicy(QFormLayout.AllNonFixedFieldsGrow)
        self.presets = BrushPresetCombo(settings, self)
        self.presets.setObjectName("brushPreset")
        self.presets.setMinimumContentsLength(6)
        self.presets.setMinimumWidth(0)
        self.presets.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.presets.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        form.addRow("Preset", self.presets)
        self.size = _number(self, 24, .1, 4096, 1, decimals=0)
        self.size.setObjectName("brushSize")
        self.size.setSuffix(" px")
        self.size.setToolTip("The slider covers 0–200 px. Type a larger size when needed.")
        size_row, self.size_slider = _number_slider(self.size, "Brush size", maximum=200)
        self.size_slider.setObjectName("brushSizeSlider")
        form.addRow("Size", size_row)
        self.opacity = _number(self, 100, 0, 100, 1, decimals=0)
        self.opacity.setObjectName("brushOpacity")
        self.opacity.setSuffix(" %")
        opacity_row, self.opacity_slider = _number_slider(self.opacity, "Brush opacity")
        self.opacity_slider.setObjectName("brushOpacitySlider")
        form.addRow("Opacity", opacity_row)
        layout.addLayout(form)
        self.preview = BrushPreview(self)
        layout.addWidget(self.preview)
        self.edit_button = QPushButton("Brush settings…", self)
        self.import_button = QPushButton("Import .sut…", self)
        layout.addWidget(self.edit_button)
        layout.addWidget(self.import_button)
        self.duplicate_button = QPushButton("Duplicate preset…", self)
        layout.addWidget(self.duplicate_button)
        self.compatibility = QLabel(self)
        self.compatibility.setWordWrap(True)
        self.compatibility.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        layout.addWidget(self.compatibility)
        layout.addStretch(1)
        self.presets.currentIndexChanged.connect(self._preset_changed)
        self.size.valueChanged.connect(lambda: self._values_changed("size"))
        self.opacity.valueChanged.connect(lambda: self._values_changed("opacity"))
        self.size_slider.sliderReleased.connect(self._commit_slider_change)
        self.opacity_slider.sliderReleased.connect(self._commit_slider_change)
        self.edit_button.clicked.connect(self._edit)
        self.import_button.clicked.connect(self._import)
        self.duplicate_button.clicked.connect(self._duplicate)
        self.refresh()
        self.set_preview_colors(QColor(getattr(settings, "primary_color", settings.brush_color)),
                                QColor(getattr(settings, "secondary_color", "#FFFFFFFF")))

    def set_preview_colors(self, color, sub_color):
        self.preview.set_colors(color, sub_color)
        self.presets.set_colors(color, sub_color)

    def refresh(self):
        self._loading = True
        try:
            self.presets.clear()
            for data in self.settings.brush_presets:
                self.presets.addItem(data["name"], data["id"])
            self.presets.setCurrentIndex(max(0, self.presets.findData(self.settings.active_brush_id)))
            self.size.setValue(self.settings.brush_size_px)
            self.opacity.setValue(self.settings.brush_opacity * 100)
            definition = self.settings.active_paint_brush()
            self.preview.set_definition(definition)
            self.compatibility.setText(
                f"{len(definition.warnings)} compatibility notes — see Brush settings."
                if definition.warnings else ""
            )
        finally:
            self._loading = False

    def _preset_changed(self):
        if self._loading or not self.presets.currentData():
            return
        self.settings.active_brush_id = self.presets.currentData()
        selected = next(item for item in self.settings.brush_presets if item["id"] == self.settings.active_brush_id)
        self.settings.brush_size_px = selected["size"]
        self.settings.brush_opacity = selected["opacity"]
        self.refresh()
        self.settingsChanged.emit()

    def _values_changed(self, field):
        if self._loading:
            return
        # Editing one control must not round the other control's stored value.
        if field == "size":
            self.settings.brush_size_px = self.size.value()
        else:
            self.settings.brush_opacity = self.opacity.value() / 100
        self.preview.set_definition(self.settings.active_paint_brush())
        if self.size_slider.isSliderDown() or self.opacity_slider.isSliderDown():
            # The active brush and preview follow every drag step. The outer
            # receiver persists settings and refreshes all panels only once.
            self._slider_change_pending = True
            return
        self.settingsChanged.emit()

    def _commit_slider_change(self):
        if self._slider_change_pending:
            self._slider_change_pending = False
            self.settingsChanged.emit()

    def _publish(self, definition):
        data = definition.to_dict()
        for index, current in enumerate(self.settings.brush_presets):
            if current["id"] == definition.id:
                self.settings.brush_presets[index] = data
                break
        else:
            self.settings.brush_presets.append(data)
        self.settings.active_brush_id = definition.id
        self.settings.brush_size_px = definition.size
        self.settings.brush_opacity = definition.opacity
        self.refresh()
        self.settingsChanged.emit()

    def _edit(self):
        dialog = BrushSettingsDialog(self.settings.active_paint_brush(), self,
                                     color=self.preview._color, sub_color=self.preview._sub_color)
        if dialog.exec() == QDialog.Accepted:
            self._publish(dialog.result_definition())

    def _duplicate(self):
        definition = self.settings.active_paint_brush()
        name, accepted = QInputDialog.getText(self, "Duplicate brush", "Name", text=f"{definition.name} copy")
        if accepted and name.strip():
            self._publish(replace(definition, id=uuid4().hex, name=name.strip()))

    def import_path(self, path: str | Path, *, dpi: float = 300.):
        from comic_editor.core.sut_import import import_sut
        return self._accept_import(import_sut(path,dpi=dpi))

    def _accept_import(self, definition: BrushDefinition):
        definition = BrushDefinition.from_dict(definition.to_dict())
        existing_ids = {item.get("id") for item in self.settings.brush_presets}
        matching_copy = None
        for item in self.settings.brush_presets:
            try:
                current = BrushDefinition.from_dict(item)
            except (TypeError, ValueError, KeyError, AttributeError):
                continue
            if current == definition:
                matching_copy = current
                break
            # A previous re-import copy has its own name and ID. Reuse it only
            # if every other value, including provenance and dual settings,
            # still matches. Never reinterpret an edited canonical preset.
            if (current.id != definition.id and
                    replace(current, id=definition.id, name=definition.name) == definition):
                matching_copy = current
        if matching_copy is not None:
            # Selecting an existing definition must not rewrite its saved data.
            self.settings.active_brush_id = matching_copy.id
            self.settings.brush_size_px = matching_copy.size
            self.settings.brush_opacity = matching_copy.opacity
            self.refresh()
            self.settingsChanged.emit()
            return matching_copy
        if definition.id in existing_ids:
            names = {item.get("name") for item in self.settings.brush_presets}
            base_name = f"{definition.name} copy"
            name = base_name
            suffix = 2
            while name in names:
                name = f"{base_name} {suffix}"
                suffix += 1
            definition = replace(definition, id=uuid4().hex, name=name)
        self._publish(definition)
        return definition

    def _import(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import Clip Studio Paint brush", "", "Clip Studio Paint brush (*.sut)")
        if not path:
            return
        try:
            from comic_editor.core.sut_import import import_sut
            from comic_editor.core.brush_units import has_physical_lengths, with_import_dpi
            definition = import_sut(path)
            if has_physical_lengths(definition):
                dpi, accepted = QInputDialog.getDouble(
                    self, "Brush size resolution",
                    "This brush uses millimeters. Enter your canvas resolution to convert its size to pixels.\nResolution (DPI):",
                    300., 1., 9600., 2)
                if not accepted:
                    return
                if dpi != 300.:
                    definition = with_import_dpi(definition,dpi)
            definition = self._accept_import(definition)
        except (OSError, ValueError, TypeError) as error:
            QMessageBox.warning(self, "Brush import failed", str(error))
            return
        if definition.warnings:
            message = QMessageBox(self)
            message.setWindowTitle("Brush imported")
            message.setText(f"{definition.name} is ready. Some settings require approximation or are not supported.")
            message.setInformativeText("Open Brush settings → Texture / import to review compatibility notes.")
            message.setDetailedText("\n\n".join(definition.warnings))
            message.exec()
