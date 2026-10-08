"""Settings and curve editors for the Distort modifier category."""
from __future__ import annotations

import copy
import math
from pathlib import Path

import numpy as np

from PySide6.QtCore import QBuffer, QIODevice, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSlider, QSizePolicy, QVBoxLayout, QWidget,
)

from comic_editor.core.distort import DISTORT_TYPES, gizmo_kind, initial_points, parameter_specs


def compact_combo(combo):
    # A long lens name must not widen the entire modifier stack.
    combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
    combo.setMinimumContentsLength(12)
    combo.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
    return combo


class DistortNumber(QWidget):
    def __init__(self, key, spec, controls):
        super().__init__(controls)
        self.setObjectName("distortRow_" + key)
        factor = 10 ** spec["decimals"]
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(1)
        row = QHBoxLayout()
        name = QLabel(spec["label"], self)
        name.setWordWrap(True)
        row.addWidget(name, 1)
        value = QDoubleSpinBox(self)
        value.setObjectName("distortValue_" + key)
        value.setAccessibleName(spec["label"])
        value.setRange(spec["minimum"], spec["maximum"])
        value.setDecimals(spec["decimals"])
        value.setSuffix(spec["suffix"])
        value.setKeyboardTracking(False)
        value.setValue(controls.modifier.parameters[key])
        row.addWidget(value)
        layout.addLayout(row)
        slider = DistortParameterSlider(controls, self)
        slider.setObjectName("distortSlider_" + key)
        slider.setAccessibleName(spec["label"])
        slider.setRange(round(spec["minimum"] * factor), round(spec["maximum"] * factor))
        slider.setValue(round(value.value() * factor))
        slider.setPageStep(max(1, (slider.maximum() - slider.minimum()) // 20))
        layout.addWidget(slider)

        def from_slider(position):
            value.blockSignals(True)
            value.setValue(position / factor)
            value.blockSignals(False)
            controls.set_value(key, position / factor, not slider.isSliderDown())

        def from_value(current):
            slider.blockSignals(True)
            slider.setValue(round(current * factor))
            slider.blockSignals(False)
            controls.set_value(key, current)

        # Mesh dimensions reset the grid in a single atomic undoable operation.
        if key in {"rows", "columns"}:
            slider.hide()
        slider.sliderPressed.connect(controls.owner.begin_parameter_drag)
        slider.sliderReleased.connect(controls.owner.finish_parameter_drag)
        slider.valueChanged.connect(from_slider)
        value.valueChanged.connect(from_value)


class DistortParameterSlider(QSlider):
    """A groove click and follow-up drag retain one original command baseline."""
    def __init__(self, controls, parent=None):
        super().__init__(Qt.Horizontal, parent)
        self._controls = controls

    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self._controls.owner.begin_parameter_drag(self._controls.modifier.modifier_id)
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event):
        super().mouseReleaseEvent(event)
        if event.button() == Qt.LeftButton:
            self._controls.owner.finish_parameter_drag()


class ShearCurveEditor(QWidget):
    """Editable normalized offset curve; endpoints stay at the image boundaries."""
    def __init__(self, key, controls):
        super().__init__(controls)
        self.controls, self.key = controls, key
        self._selected = None
        self.setObjectName("distortCurve_" + key)
        self.setMinimumHeight(125)
        self.setToolTip("Click to add a point. Drag points to bend the image. Right-click an interior point to remove it.")

    def area(self):
        return QRectF(8, 8, max(1, self.width() - 16), max(1, self.height() - 16))

    def point_position(self, point):
        rect = self.area()
        return QPointF(rect.left() + point[0] * rect.width(), rect.center().y() - point[1] * rect.height() / 2)

    def curve(self):
        return copy.deepcopy(self.controls.modifier.parameters[self.key])

    def curve_path(self):
        from comic_editor.ui.distort_rendering import _curve
        points = self.curve()
        positions = np.unique(np.r_[np.linspace(0., 1., 257), [point[0] for point in points]])
        offsets = _curve(points, positions)
        path = QPainterPath()
        for index, point in enumerate(zip(positions, offsets)):
            if index:
                path.lineTo(self.point_position(point))
            else:
                path.moveTo(self.point_position(point))
        return path

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        painter.fillRect(self.rect(), QColor("#20262e"))
        rect = self.area()
        painter.setPen(QPen(QColor("#495361"), 1))
        painter.drawRect(rect)
        painter.drawLine(QPointF(rect.left(), rect.center().y()), QPointF(rect.right(), rect.center().y()))
        points = self.curve()
        painter.setPen(QPen(QColor("#65bcff"), 2))
        painter.drawPath(self.curve_path())
        painter.setBrush(QColor("#d9eeff"))
        for point in points:
            painter.drawEllipse(self.point_position(point), 4, 4)

    def mousePressEvent(self, event):
        points = self.curve()
        closest = min(range(len(points)), key=lambda index: (self.point_position(points[index]) - event.position()).manhattanLength())
        close = (self.point_position(points[closest]) - event.position()).manhattanLength() <= 15
        if event.button() == Qt.RightButton:
            if close and closest not in {0, len(points) - 1}:
                points.pop(closest)
                self.controls.set_value(self.key, points)
                self.update()
            return
        if event.button() != Qt.LeftButton:
            return
        self.controls.owner.begin_parameter_drag()
        if close:
            self._selected = closest
        else:
            rect = self.area()
            position = max(.0001, min(.9999, (event.position().x() - rect.left()) / rect.width()))
            if any(abs(point[0] - position) < .0001 for point in points):
                self._selected = closest
            else:
                points.append([position, max(-1., min(1., (rect.center().y() - event.position().y()) * 2 / rect.height()))])
                points.sort()
                self._selected = next(index for index, point in enumerate(points) if point[0] == position)
                self.controls.set_value(self.key, points, False)
        self.update()

    def mouseMoveEvent(self, event):
        if self._selected is None:
            return
        points, rect = self.curve(), self.area()
        index = self._selected
        position = points[index][0]
        if index not in {0, len(points) - 1}:
            position = max(points[index - 1][0] + .0001,
                           min(points[index + 1][0] - .0001, (event.position().x() - rect.left()) / rect.width()))
        points[index] = [position, max(-1., min(1., (rect.center().y() - event.position().y()) * 2 / rect.height()))]
        self.controls.set_value(self.key, points, False)
        self.update()

    def mouseReleaseEvent(self, event):
        if self._selected is not None:
            self.mouseMoveEvent(event)
            self._selected = None
            self.controls.owner.finish_parameter_drag()


class DistortControls(QWidget):
    def __init__(self, modifier, owner, parent=None):
        super().__init__(parent)
        self.modifier, self.owner = modifier, owner
        modifier.validate()
        self.setObjectName("distortControls")
        form = QVBoxLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        form.setSpacing(5)
        spec = DISTORT_TYPES[modifier.modifier_type]
        gizmo = gizmo_kind(modifier.modifier_type, modifier.parameters)
        if spec["description"]:
            explanation = QLabel(spec["description"], self)
            explanation.setWordWrap(True)
            form.addWidget(explanation)
        if gizmo in {"pins", "quad", "mesh"}:
            self._point_controls(form, gizmo)
        if modifier.modifier_type == "distort_lens_correction":
            self._lens_controls(form)
        if modifier.modifier_type == "distort_displace":
            load = QPushButton("Load displacement map…", self)
            load.setObjectName("distortLoadMap")
            load.clicked.connect(self.load_map)
            form.addWidget(load)
            beneath = QPushButton("Load layers beneath", self)
            beneath.setObjectName("distortLoadBeneath")
            beneath.setToolTip("Use a snapshot of the visible layers beneath this object as the displacement map.")
            beneath.clicked.connect(self.load_beneath)
            form.addWidget(beneath)
            if modifier.parameters["map_png"]:
                form.addWidget(QLabel("Map is embedded in the project.", self))
        for key, parameter in parameter_specs(modifier.modifier_type).items():
            kind = parameter["kind"]
            if not self._parameter_visible(key):
                continue
            single_channel = (modifier.modifier_type == "distort_glitch"
                              and modifier.parameters["mode"] == "channel_flip" and key == "channel_order")
            if single_channel:
                parameter = {**parameter, "label": "Channel", "choices": {"rgb": "Red", "grb": "Green", "brg": "Blue"}}
            if kind == "number":
                form.addWidget(DistortNumber(key, parameter, self))
            elif kind == "choice":
                form.addWidget(QLabel(parameter["label"], self))
                value = compact_combo(QComboBox(self))
                value.setObjectName("distortChoice_" + key)
                value.setAccessibleName(parameter["label"])
                for item, label in parameter["choices"].items():
                    value.addItem(label, item)
                selected = modifier.parameters[key]
                if single_channel:
                    selected = {"r": "rgb", "g": "grb", "b": "brg"}[selected[0]]
                value.setCurrentIndex(value.findData(selected))
                value.currentIndexChanged.connect(lambda _index, k=key, widget=value: self.set_value(k, widget.currentData()))
                form.addWidget(value)
            elif kind == "bool":
                value = QCheckBox(parameter["label"], self)
                value.setObjectName("distortCheck_" + key)
                value.setChecked(modifier.parameters[key])
                value.toggled.connect(lambda checked, k=key: self.set_value(k, checked))
                form.addWidget(value)
            elif kind == "text":
                form.addWidget(QLabel(parameter["label"], self))
                value = QLineEdit(modifier.parameters[key], self)
                value.setObjectName("distortExpression_" + key)
                value.setMaxLength(1024)
                form.addWidget(value)
                error_label = QLabel(self)
                error_label.setObjectName("distortExpressionError_" + key)
                error_label.setWordWrap(True)
                error_label.setStyleSheet("color: #ff9999")
                error_label.hide()
                form.addWidget(error_label)
                value.editingFinished.connect(lambda k=key, widget=value, error=error_label: self.set_expression(k, widget, error))
            elif kind == "curve":
                form.addWidget(QLabel(parameter["label"], self))
                form.addWidget(ShearCurveEditor(key, self))
        if gizmo == "radius":
            row = QHBoxLayout()
            row.addWidget(QLabel("Radius", self), 1)
            radius = QDoubleSpinBox(self)
            radius.setObjectName("distortRadius")
            radius.setAccessibleName("Radius")
            radius.setRange(.01, 1000000.)
            radius.setValue(modifier.radius)
            radius.setSuffix(" px")
            radius.setKeyboardTracking(False)
            row.addWidget(radius)
            form.addLayout(row)
            radius_slider = DistortParameterSlider(self, self)
            radius_slider.setObjectName("distortRadiusSlider")
            radius_slider.setAccessibleName("Radius")
            # A logarithmic scale keeps small radii controllable while retaining
            # the complete 0.01–1,000,000 px numeric range.
            radius_slider.setRange(0, 10000)
            radius_slider.setPageStep(250)
            def slider_position(value):
                return round((math.log10(max(.01, min(1000000., value))) + 2.) * 1250.)
            radius_slider.setValue(slider_position(modifier.radius))
            def from_radius(value):
                radius_slider.blockSignals(True)
                radius_slider.setValue(slider_position(value))
                radius_slider.blockSignals(False)
                owner.set_parameter(modifier.modifier_id, "radius", value, True)
            def from_radius_slider(position):
                value = round(10 ** (position / 1250. - 2.), 2)
                radius.blockSignals(True)
                radius.setValue(value)
                radius.blockSignals(False)
                owner.set_parameter(modifier.modifier_id, "radius", value, not radius_slider.isSliderDown())
            radius.valueChanged.connect(from_radius)
            radius_slider.sliderPressed.connect(owner.begin_parameter_drag)
            radius_slider.valueChanged.connect(from_radius_slider)
            radius_slider.sliderReleased.connect(owner.finish_parameter_drag)
            form.addWidget(radius_slider)
        if gizmo in {"center", "radius"}:
            hint = QLabel("Move the center handle on the canvas.", self)
            hint.setObjectName("distortCenterHint")
            hint.setWordWrap(True)
            form.addWidget(hint)

    def _parameter_visible(self, key):
        if self.modifier.modifier_type != "distort_glitch":
            return True
        mode = self.modifier.parameters["mode"]
        base = mode.removesuffix("_color")
        if key in {"mode", "edges", "interpolation"}:
            return True
        if key == "seed":
            return base in {"shred", "blast", "distort", "scramble", "fuzz", "warp", "light_streaks", "data_blocks"}
        if key == "amount":
            return not mode.startswith(("blast", "shred")) and mode != "warp"
        color = mode.endswith("_color") or mode.startswith("aberration") or mode in {"channel_flip", "quantisation", "fuzz"}
        return {
            "offset_x": base in {"aberration_offset", "sawtooth", "waves", "quantisation", "fuzz", "light_streaks", "slice"},
            "offset_y": base in {"aberration_offset", "sawtooth", "waves", "quantisation", "fuzz", "light_streaks", "slice"},
            "horizontal_strength": mode.startswith(("blast", "shred")) or mode == "warp",
            "vertical_strength": mode.startswith(("blast", "shred")) or mode == "warp",
            "spacing": mode.startswith("shred") or mode in {"quantisation", "data_blocks"},
            "stagger": mode.startswith("blast"), "slice_offset": mode.startswith("slice"),
            "loops": mode.startswith("waves") or mode.startswith("ripple"),
            "turbulence": mode == "warp", "channels": color and mode != "channel_flip", "channel_order": color,
            "bidirectional": mode.startswith("aberration") or mode in {"shred_color", "blast_color"},
        }.get(key, False)

    def set_value(self, key, value, commit=True):
        parameters = {**self.modifier.parameters, key: value}
        if key in {"rows", "columns"}:
            self.owner.begin_parameter_drag()
            self.modifier.parameters = parameters
            self.modifier.points = initial_points(self.modifier.modifier_type, parameters)
            self.modifier.source_points = list(self.modifier.points)
            self.modifier.validate()
            self.owner._changed()
            self.owner.finish_parameter_drag()
            self.owner.refresh()
            return
        if key == "coordinates":
            if value == "polar" and (parameters["x_expression"], parameters["y_expression"]) == ("x", "y"):
                parameters.update(x_expression="r", y_expression="t")
            elif value == "cartesian" and (parameters["x_expression"], parameters["y_expression"]) == ("r", "t"):
                parameters.update(x_expression="x", y_expression="y")
        self.owner.set_parameter(self.modifier.modifier_id, "parameters", parameters, commit)
        if key in {"mode", "coordinates", "map_source"} and commit:
            self.owner.refresh()

    def set_expression(self, key, widget, error_label):
        from comic_editor.ui.distort_equations import validate_equation
        try:
            validate_equation(widget.text())
        except ValueError as error:
            error_label.setText(str(error))
            error_label.show()
            return
        error_label.hide()
        self.set_value(key, widget.text())

    def _point_controls(self, form, gizmo):
        canvas = self.owner.canvas
        source = QCheckBox("Edit source positions", self)
        source.setObjectName("distortEditSource")
        source.setChecked(getattr(canvas, "distort_edit_source", False))
        source.toggled.connect(lambda value: (setattr(canvas, "distort_edit_source", value), canvas.update()))
        form.addWidget(source)
        if gizmo == "pins":
            mode = compact_combo(QComboBox(self))
            mode.setObjectName("distortPinMode")
            for key, label in (("move", "Move pins"), ("add", "Add pins"), ("remove", "Remove pins")):
                mode.addItem(label, key)
            mode.setCurrentIndex(mode.findData(getattr(canvas, "distort_pin_mode", "move")))
            mode.currentIndexChanged.connect(lambda _index: setattr(canvas, "distort_pin_mode", mode.currentData()))
            form.addWidget(mode)
        else:
            grid = QCheckBox("Show grid", self)
            grid.setObjectName("distortShowGrid")
            grid.setChecked(getattr(canvas, "distort_show_grid", True))
            grid.toggled.connect(lambda value: (setattr(canvas, "distort_show_grid", value), canvas.update()))
            form.addWidget(grid)
        row = QVBoxLayout()
        reset = QPushButton("Reset points", self)
        reset.setObjectName("distortResetPoints")
        reset.clicked.connect(self.reset_points)
        row.addWidget(reset)
        sync = QPushButton("Synchronize", self)
        sync.setObjectName("distortSynchronize")
        sync.setToolTip("Move destination points back to their source positions.")
        sync.clicked.connect(lambda: self.reset_points(synchronize=True))
        row.addWidget(sync)
        form.addLayout(row)

    def reset_points(self, _checked=False, *, synchronize=False):
        self.owner.begin_parameter_drag()
        if synchronize:
            self.modifier.points = list(self.modifier.source_points)
        else:
            self.modifier.points = initial_points(self.modifier.modifier_type, self.modifier.parameters)
            self.modifier.source_points = list(self.modifier.points)
        self.modifier.validate()
        self.owner._changed()
        self.owner.finish_parameter_drag()
        self.owner.refresh()

    def load_map(self):
        filename, _filter = QFileDialog.getOpenFileName(self, "Choose displacement map", "", "Images (*.png *.jpg *.jpeg *.bmp *.webp *.tif *.tiff)")
        if not filename:
            return
        image = QImage(filename)
        if image.isNull():
            QMessageBox.warning(self, "Displacement map", "The image could not be loaded.")
            return
        buffer = QBuffer()
        buffer.open(QIODevice.WriteOnly)
        if not image.save(buffer, "PNG"):
            QMessageBox.warning(self, "Displacement map", "The image could not be embedded.")
            return
        parameters = {**self.modifier.parameters, "map_source": "embedded", "map_png": bytes(buffer.data().toBase64()).decode("ascii")}
        self.owner.set_parameter(self.modifier.modifier_id, "parameters", parameters, True)
        self.owner.refresh()

    def load_beneath(self):
        from comic_editor.render.source_sampling import distort_beneath
        from comic_editor.ui.scene_consumers import scene_consumers
        import weakref
        from shiboken6 import isValid
        reference = weakref.ref(self)
        modifier_id = self.modifier.modifier_id
        def accept(encoded, error):
            control = reference()
            if control is None or not isValid(control):
                return
            if error is not None:
                QMessageBox.warning(control, 'Displacement map', str(error))
                return
            if not encoded:
                QMessageBox.information(control, 'Displacement map',
                    'There are no visible pixels beneath the selected object.')
                return
            modifier = control.owner.canvas.chapter.modifiers.get(modifier_id)
            if modifier is None:
                return
            parameters = {**modifier.parameters, 'map_source': 'embedded', 'map_png': encoded}
            control.owner.set_parameter(modifier_id, 'parameters', parameters, True)
            control.owner.refresh()
        scene_consumers(self.owner.canvas).request(('distort-beneath', modifier_id),
                                                   distort_beneath, (modifier_id,), accept)

    def _lens_controls(self, form):
        from comic_editor.core.lens_profiles import load_lens_catalog
        xml = self.modifier.parameters["profile_xml"]
        try:
            catalog = load_lens_catalog(xml_text=xml) if xml else load_lens_catalog()
        except ValueError as error:
            form.addWidget(QLabel(str(error), self))
            return
        camera = compact_combo(QComboBox(self))
        camera.setObjectName("distortCameraProfile")
        camera.addItem("Choose camera…", "")
        for item in catalog.cameras:
            camera.addItem(item.name, item.id)
        camera.setCurrentIndex(max(0, camera.findData(self.modifier.parameters["camera_profile"])))
        lens = compact_combo(QComboBox(self))
        lens.setObjectName("distortLensProfile")
        lens.addItem("Choose lens…", "")
        camera_id = self.modifier.parameters["camera_profile"]
        for item in (catalog.lenses_for_camera(camera_id) if camera_id else catalog.lenses):
            lens.addItem(item.name, item.id)
        lens.setCurrentIndex(max(0, lens.findData(self.modifier.parameters["lens_profile"])))
        def camera_changed(_index):
            parameters = {**self.modifier.parameters, "camera_profile": camera.currentData(), "lens_profile": ""}
            self.owner.set_parameter(self.modifier.modifier_id, "parameters", parameters, True)
            self.owner.refresh()
        camera.currentIndexChanged.connect(camera_changed)
        lens.currentIndexChanged.connect(lambda _index: self.set_value("lens_profile", lens.currentData()))
        form.addWidget(QLabel("Camera", self))
        form.addWidget(camera)
        form.addWidget(QLabel("Lens", self))
        form.addWidget(lens)
        load = QPushButton("Import lens profile…", self)
        load.setObjectName("distortImportLensProfile")
        load.clicked.connect(self.import_lens_profile)
        form.addWidget(load)

    def import_lens_profile(self):
        from comic_editor.core.lens_profiles import load_lens_catalog
        filename, _filter = QFileDialog.getOpenFileName(self, "Import lens calibration", "", "Lensfun XML (*.xml)")
        if not filename:
            return
        try:
            xml = Path(filename).read_text(encoding="utf-8")
            catalog = load_lens_catalog(xml_text=xml)
            if not catalog.lenses:
                raise ValueError("The profile contains no lenses.")
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Lens profile", str(error))
            return
        parameters = {**self.modifier.parameters, "profile_xml": xml, "camera_profile": "", "lens_profile": catalog.lenses[0].id}
        self.owner.set_parameter(self.modifier.modifier_id, "parameters", parameters, True)
        self.owner.refresh()
