"""Contextual gradient creation, ramp editing, and preset controls."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QHBoxLayout, QLabel, QLineEdit,
    QPushButton, QSlider, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    ColorFillGradientObject, ColorGradientRamp, ColorGradientRampPreset,
    ColorGradientStop, canonical_argb,
)
from comic_editor.ui.color_picker import ColorPickerPopup

BUILTIN_PRIMARY_SECONDARY_ID = "builtin:primary-secondary"


class GradientRampEditor(QWidget):
    """Blender-style stop editor with stable stop identities."""

    editingStarted = Signal()
    rampChanged = Signal(object)
    editingFinished = Signal()
    selectionChanged = Signal(str)
    colorEditRequested = Signal(str)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setMinimumSize(180, 70)
        self.setMouseTracking(True)
        self._ramp = ColorGradientRamp()
        self._selected_stop_id = self._ramp.stops[0].stop_id
        self._dragging = False

    def ramp(self) -> ColorGradientRamp:
        return self._ramp.copy()

    def setRamp(
        self, ramp: ColorGradientRamp, selected_stop_id: str = "",
    ) -> None:  # noqa: N802
        self._ramp = ramp.copy()
        ids = {stop.stop_id for stop in self._ramp.stops}
        self._selected_stop_id = (
            selected_stop_id
            if selected_stop_id in ids else self._ramp.stops[0].stop_id
        )
        self.update()

    def selected_stop_id(self) -> str:
        return self._selected_stop_id

    def selected_stop(self) -> ColorGradientStop | None:
        return next((
            stop for stop in self._ramp.stops
            if stop.stop_id == self._selected_stop_id
        ), None)

    def _ramp_rect(self) -> QRectF:
        return QRectF(12, 7, max(20, self.width() - 24), 30)

    def _stop_point(self, stop: ColorGradientStop) -> QPointF:
        rect = self._ramp_rect()
        return QPointF(rect.left() + stop.position * rect.width(), 52)

    def _stop_at(self, point: QPointF) -> ColorGradientStop | None:
        candidates = [
            (math.dist(point.toTuple(), self._stop_point(stop).toTuple()), stop)
            for stop in self._ramp.stops
        ]
        if not candidates:
            return None
        distance, stop = min(candidates, key=lambda item: item[0])
        return stop if distance <= 13 else None

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = self._ramp_rect()
        block = 7
        for y in range(int(rect.top()), int(rect.bottom()) + 1, block):
            for x in range(int(rect.left()), int(rect.right()) + 1, block):
                painter.fillRect(
                    x, y, block, block,
                    QColor("#b8b8b8")
                    if ((x // block) + (y // block)) % 2
                    else QColor("#eeeeee"),
                )
        gradient = QLinearGradient(rect.topLeft(), rect.topRight())
        for stop in self._ramp.stops:
            gradient.setColorAt(stop.position, QColor(stop.color))
        painter.fillRect(rect, gradient)
        painter.setPen(QPen(QColor("#d8d8d8"), 1))
        painter.drawRect(rect)
        for stop in self._ramp.stops:
            point = self._stop_point(stop)
            path = QPainterPath()
            path.moveTo(point.x(), 38)
            path.lineTo(point.x() - 6, 46)
            path.lineTo(point.x() + 6, 46)
            path.closeSubpath()
            painter.setPen(QPen(
                QColor("#ff9f22")
                if stop.stop_id == self._selected_stop_id
                else QColor("#333333"),
                2,
            ))
            painter.setBrush(QColor(stop.color))
            painter.drawPath(path)
            painter.drawRect(QRectF(point.x() - 8, 46, 16, 16))

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            return super().mousePressEvent(event)
        stop = self._stop_at(event.position())
        if stop is None:
            return
        self._selected_stop_id = stop.stop_id
        self.selectionChanged.emit(stop.stop_id)
        self._dragging = True
        self.editingStarted.emit()
        self.update()
        event.accept()

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        if not self._dragging:
            stop = self._stop_at(event.position())
            self.setToolTip(
                "Double-click to edit this gradient color; drag to move it."
                if stop is not None else "Gradient ramp"
            )
            return
        stop = self.selected_stop()
        if stop is None:
            return
        rect = self._ramp_rect()
        stop.position = max(
            0.0, min(1.0, (event.position().x() - rect.left()) / rect.width())
        )
        self._ramp.validate()
        self.rampChanged.emit(self._ramp.copy())
        self.update()
        event.accept()

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if self._dragging and event.button() == Qt.MouseButton.LeftButton:
            self._dragging = False
            self.editingFinished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # noqa: N802
        stop = self._stop_at(event.position())
        if stop is None:
            return
        self._selected_stop_id = stop.stop_id
        self.selectionChanged.emit(stop.stop_id)
        self.colorEditRequested.emit(stop.stop_id)
        self.update()
        event.accept()


class GradientToolsControls(QWidget):
    """Owns reusable widgets placed into the Gradient Tools ribbon groups."""

    createRequested = Signal(str)
    objectChanged = Signal()
    presetLoadRequested = Signal(str)
    presetAddRequested = Signal()
    presetSaveRequested = Signal(str)
    presetRenameRequested = Signal(str, str)
    presetRemoveRequested = Signal(str)

    def __init__(self, canvas, parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self._loading = False
        self._edit_before: dict | None = None
        self._selected_stop_id = ""

        self.create_widget = QWidget(self)
        create = QVBoxLayout(self.create_widget)
        create.setContentsMargins(0, 0, 0, 0)
        button_row = QHBoxLayout()
        self.create_line = QPushButton("Line / Curve", self.create_widget)
        self.create_radial = QPushButton("Circle / Ellipse", self.create_widget)
        self.create_shape = QPushButton("Parent Shape", self.create_widget)
        button_row.addWidget(self.create_line)
        button_row.addWidget(self.create_radial)
        button_row.addWidget(self.create_shape)
        create.addLayout(button_row)
        type_row = QHBoxLayout()
        type_row.addWidget(QLabel("Selected type", self.create_widget))
        self.field_type = QComboBox(self.create_widget)
        self.field_type.addItem("Line / Curve", "line")
        self.field_type.addItem("Circle / Ellipse", "radial")
        self.field_type.addItem("Parent Shape", "parent_shape")
        type_row.addWidget(self.field_type, 1)
        create.addLayout(type_row)
        self.select_gradient = QPushButton(
            "Select Gradient", self.create_widget
        )
        self.select_gradient.setToolTip(
            "Select the direct gradient matching Selected type"
        )
        create.addWidget(self.select_gradient)

        self.type_parameters_widget = QWidget(self)
        type_parameters = QVBoxLayout(self.type_parameters_widget)
        type_parameters.setContentsMargins(0, 0, 0, 0)
        self.direction_row = QWidget(self.type_parameters_widget)
        direction_layout = QHBoxLayout(self.direction_row)
        direction_layout.setContentsMargins(0, 0, 0, 0)
        direction_layout.addWidget(QLabel("Follow", self.direction_row))
        self.direction_mode = QComboBox(self.direction_row)
        self.direction_mode.addItem("Parallel", "parallel")
        self.direction_mode.addItem("Perpendicular", "perpendicular")
        direction_layout.addWidget(self.direction_mode, 1)
        type_parameters.addWidget(self.direction_row)
        self.reverse_direction = QCheckBox(
            "Reverse direction", self.type_parameters_widget
        )
        type_parameters.addWidget(self.reverse_direction)
        self.uniform = QCheckBox(
            "Uniform", self.type_parameters_widget
        )
        self.uniform.setToolTip(
            "Transition a fixed distance inward from the boundary"
        )
        type_parameters.addWidget(self.uniform)
        self.distance_row = QWidget(self.type_parameters_widget)
        distance_layout = QHBoxLayout(self.distance_row)
        distance_layout.setContentsMargins(0, 0, 0, 0)
        self.distance_label = QLabel("Distance", self.distance_row)
        self.distance_slider = QSlider(
            Qt.Orientation.Horizontal, self.distance_row
        )
        self.distance_slider.setRange(1, 1000)
        self.distance_value = QDoubleSpinBox(self.distance_row)
        self.distance_value.setRange(-1000, 1000)
        self.distance_value.setDecimals(0)
        self.distance_value.setSuffix(" px")
        self.distance_value.setButtonSymbols(
            QDoubleSpinBox.ButtonSymbols.NoButtons
        )
        distance_layout.addWidget(self.distance_label)
        distance_layout.addWidget(self.distance_slider, 1)
        distance_layout.addWidget(self.distance_value)
        type_parameters.addWidget(self.distance_row)

        self.parameters_widget = QWidget(self)
        parameters = QVBoxLayout(self.parameters_widget)
        parameters.setContentsMargins(0, 0, 0, 0)
        self.ramp_editor = GradientRampEditor(self.parameters_widget)
        parameters.addWidget(self.ramp_editor)
        stop_row = QHBoxLayout()
        self.add_stop = QPushButton("+", self.parameters_widget)
        self.add_stop.setToolTip("Add a gradient color")
        self.remove_stop = QPushButton("−", self.parameters_widget)
        self.remove_stop.setToolTip("Remove the selected gradient color")
        stop_row.addWidget(self.add_stop)
        stop_row.addWidget(self.remove_stop)
        stop_row.addStretch(1)
        parameters.addLayout(stop_row)

        self.presets_widget = QWidget(self)
        presets = QVBoxLayout(self.presets_widget)
        presets.setContentsMargins(0, 0, 0, 0)
        self.preset_combo = QComboBox(self.presets_widget)
        presets.addWidget(self.preset_combo)
        self.preset_name = QLineEdit(self.presets_widget)
        self.preset_name.setPlaceholderText("Preset name")
        presets.addWidget(self.preset_name)
        preset_buttons = QHBoxLayout()
        self.add_preset = QPushButton("New", self.presets_widget)
        self.save_preset = QPushButton("Save", self.presets_widget)
        self.remove_preset = QPushButton("Remove", self.presets_widget)
        preset_buttons.addWidget(self.add_preset)
        preset_buttons.addWidget(self.save_preset)
        preset_buttons.addWidget(self.remove_preset)
        presets.addLayout(preset_buttons)

        self.color_popup = ColorPickerPopup(parent=self)
        self.color_popup.colorApplied.connect(self._apply_stop_color)
        self.create_line.clicked.connect(
            lambda: self.createRequested.emit("line")
        )
        self.create_radial.clicked.connect(
            lambda: self.createRequested.emit("radial")
        )
        self.create_shape.clicked.connect(
            lambda: self.createRequested.emit("parent_shape")
        )
        self.field_type.currentIndexChanged.connect(self._field_changed)
        self.select_gradient.clicked.connect(self._select_matching_gradient)
        self.direction_mode.currentIndexChanged.connect(
            self._type_parameter_changed
        )
        self.reverse_direction.toggled.connect(
            self._type_parameter_changed
        )
        self.uniform.toggled.connect(self._type_parameter_changed)
        self.distance_slider.valueChanged.connect(
            self._distance_slider_changed
        )
        self.distance_value.valueChanged.connect(
            self._type_parameter_changed
        )
        self.ramp_editor.editingStarted.connect(self._begin_ramp_edit)
        self.ramp_editor.rampChanged.connect(self._preview_ramp)
        self.ramp_editor.editingFinished.connect(self._finish_ramp_edit)
        self.ramp_editor.selectionChanged.connect(self._stop_selected)
        self.ramp_editor.colorEditRequested.connect(self._edit_stop_color)
        self.add_stop.clicked.connect(self._add_stop)
        self.remove_stop.clicked.connect(self._remove_stop)
        self.preset_combo.activated.connect(self._preset_activated)
        self.add_preset.clicked.connect(self.presetAddRequested)
        self.save_preset.clicked.connect(self._save_preset)
        self.remove_preset.clicked.connect(self._remove_preset)
        self.preset_name.editingFinished.connect(self._rename_preset)
        self.refresh()

    def selected_gradient(self) -> ColorFillGradientObject | None:
        if (
            self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return None
        candidate = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return (
            candidate
            if isinstance(candidate, ColorFillGradientObject) else None
        )

    def context_parent_id(self) -> str:
        if self.canvas.chapter is None:
            return ""
        if self.canvas.selected_kind == "layer":
            layer = self.canvas.chapter.layers.get(self.canvas.selected_id)
            return (
                layer.layer_id
                if layer is not None and layer.bound is not None else ""
            )
        obj = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return obj.parent_layer_id if obj is not None else ""

    def matching_gradients(self, field_type: str | None = None):
        parent_id = self.context_parent_id()
        if not parent_id:
            return []
        return self.canvas.chapter.gradient_children(
            parent_id, field_type or str(self.field_type.currentData())
        )

    def refresh(self) -> None:
        obj = self.selected_gradient()
        self._loading = True
        enabled = obj is not None
        context_parent = self.context_parent_id()
        self.field_type.setEnabled(bool(context_parent))
        self.parameters_widget.setEnabled(enabled)
        self.presets_widget.setEnabled(enabled)
        self.type_parameters_widget.setEnabled(enabled)
        if obj is not None:
            self.field_type.setCurrentIndex(max(
                0, self.field_type.findData(obj.field_type)
            ))
            self.ramp_editor.setRamp(obj.ramp, self._selected_stop_id)
            self._selected_stop_id = self.ramp_editor.selected_stop_id()
            self.remove_stop.setEnabled(len(obj.ramp.stops) > 2)
            preset_index = self.preset_combo.findData(obj.loaded_preset_id)
            self.preset_combo.setCurrentIndex(max(0, preset_index))
            self.direction_mode.setCurrentIndex(max(
                0, self.direction_mode.findData(
                    obj.line_field.direction_mode
                )
            ))
            field = (
                obj.line_field if obj.field_type == "line"
                else obj.radial_field if obj.field_type == "radial"
                else obj.shape_field
            )
            self.reverse_direction.setChecked(field.reverse_direction)
            self.uniform.setChecked(
                bool(getattr(field, "uniform", False))
            )
            distance = (
                obj.line_field.perpendicular_distance
                if obj.field_type == "line" else field.distance
            )
            self.distance_value.setValue(distance)
            self.distance_slider.setValue(
                max(1, min(1000, round(abs(distance))))
            )
        self.direction_row.setVisible(
            obj is not None and obj.field_type == "line"
        )
        radial_or_shape = (
            obj is not None
            and obj.field_type in {"radial", "parent_shape"}
        )
        self.uniform.setVisible(
            radial_or_shape and not obj.radial_field.reverse_direction
            if obj is not None and obj.field_type == "radial"
            else radial_or_shape and not (
                obj.shape_field.reverse_direction
                if obj is not None else False
            )
        )
        show_distance = bool(
            obj is not None
            and (
                (
                    obj.field_type == "line"
                    and obj.line_field.direction_mode == "perpendicular"
                )
                or (
                    obj.field_type == "radial"
                    and (
                        obj.radial_field.reverse_direction
                        or obj.radial_field.uniform
                    )
                )
                or (
                    obj.field_type == "parent_shape"
                    and (
                        obj.shape_field.reverse_direction
                        or obj.shape_field.uniform
                    )
                )
            )
        )
        self.distance_row.setVisible(show_distance)
        self.distance_label.setText(
            "Perpendicular"
            if obj is not None and obj.field_type == "line"
            else "Distance"
        )
        selected_type = str(self.field_type.currentData() or "line")
        matches = (
            self.canvas.chapter.gradient_children(
                context_parent, selected_type
            ) if context_parent else []
        )
        self.select_gradient.setVisible(bool(matches) and obj is None)
        self.create_line.setEnabled(
            bool(context_parent)
            and not self.canvas.chapter.gradient_children(
                context_parent, "line"
            )
        )
        self.create_radial.setEnabled(
            bool(context_parent)
            and not self.canvas.chapter.gradient_children(
                context_parent, "radial"
            )
        )
        self.create_shape.setEnabled(
            bool(context_parent)
            and not self.canvas.chapter.gradient_children(
                context_parent, "parent_shape"
            )
        )
        self._loading = False

    def set_presets(
        self, presets: list[ColorGradientRampPreset],
    ) -> None:
        current = self.preset_combo.currentData()
        self._loading = True
        self.preset_combo.clear()
        self.preset_combo.addItem("Custom", "")
        self.preset_combo.addItem(
            "Primary → Secondary", BUILTIN_PRIMARY_SECONDARY_ID
        )
        for preset in presets:
            self.preset_combo.addItem(preset.name, preset.preset_id)
        index = self.preset_combo.findData(current)
        self.preset_combo.setCurrentIndex(max(0, index))
        self._sync_preset_name()
        self._loading = False

    def _commit_change(self, before: dict, label: str) -> None:
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, label)
            self.objectChanged.emit()
        self.canvas.documentChanged.emit(QRectF())
        self.canvas.update()

    def _field_changed(self) -> None:
        if self._loading:
            return
        obj = self.selected_gradient()
        field_type = self.field_type.currentData()
        if obj is None:
            self.refresh()
            return
        if not field_type or obj.field_type == field_type:
            return
        conflicts = self.canvas.chapter.gradient_children(
            obj.parent_layer_id, str(field_type), excluding=obj.object_id
        )
        if conflicts:
            window = self.canvas.window()
            if hasattr(window, "statusBar"):
                window.statusBar().showMessage(
                    f"This shape already has a {field_type} gradient", 4000
                )
            self.refresh()
            return
        before = self.canvas.chapter.to_dict()
        obj.field_type = str(field_type)
        obj.touch_revision()
        self._commit_change(before, "Change gradient field")
        self.refresh()

    def _select_matching_gradient(self) -> None:
        matches = self.matching_gradients()
        if matches:
            self.canvas.set_selection("object", matches[0].object_id)

    def _distance_slider_changed(self, value: int) -> None:
        if self._loading:
            return
        sign = -1 if self.distance_value.value() < 0 else 1
        self.distance_value.setValue(sign * value)

    def _type_parameter_changed(self, *args) -> None:
        del args
        if self._loading:
            return
        obj = self.selected_gradient()
        if obj is None:
            return
        before = self.canvas.chapter.to_dict()
        if obj.field_type == "line":
            obj.line_field.direction_mode = str(
                self.direction_mode.currentData()
            )
            obj.line_field.reverse_direction = (
                self.reverse_direction.isChecked()
            )
            distance = self.distance_value.value()
            obj.line_field.perpendicular_distance = (
                distance if abs(distance) >= 1 else 1.0
            )
        else:
            field = (
                obj.radial_field
                if obj.field_type == "radial" else obj.shape_field
            )
            field.reverse_direction = self.reverse_direction.isChecked()
            field.uniform = self.uniform.isChecked()
            field.distance = max(
                1.0, abs(self.distance_value.value())
            )
        obj.validate_gradient()
        obj.touch_revision()
        self._commit_change(before, "Change gradient direction")
        self.refresh()

    def _begin_ramp_edit(self) -> None:
        if self._edit_before is None and self.canvas.chapter is not None:
            self._edit_before = self.canvas.chapter.to_dict()

    def _preview_ramp(self, ramp: ColorGradientRamp) -> None:
        obj = self.selected_gradient()
        if obj is None:
            return
        obj.ramp = ramp.copy()
        obj.touch_revision()
        self.canvas.documentChanged.emit(QRectF())
        self.canvas.update()

    def _finish_ramp_edit(self) -> None:
        before, self._edit_before = self._edit_before, None
        if before is not None:
            self._commit_change(before, "Move gradient stop")

    def _stop_selected(self, stop_id: str) -> None:
        self._selected_stop_id = stop_id
        obj = self.selected_gradient()
        self.remove_stop.setEnabled(
            obj is not None and len(obj.ramp.stops) > 2
        )

    @staticmethod
    def _interpolated_stop(
        ramp: ColorGradientRamp,
    ) -> ColorGradientStop:
        ramp.validate()
        left, right = max(
            zip(ramp.stops, ramp.stops[1:]),
            key=lambda pair: pair[1].position - pair[0].position,
        )
        position = (left.position + right.position) / 2
        first, second = QColor(left.color), QColor(right.color)
        color = QColor.fromRgbF(
            (first.redF() + second.redF()) / 2,
            (first.greenF() + second.greenF()) / 2,
            (first.blueF() + second.blueF()) / 2,
            (first.alphaF() + second.alphaF()) / 2,
        )
        return ColorGradientStop(
            position=position,
            color=color.name(QColor.NameFormat.HexArgb).upper(),
        )

    def _add_stop(self) -> None:
        obj = self.selected_gradient()
        if obj is None:
            return
        before = self.canvas.chapter.to_dict()
        stop = self._interpolated_stop(obj.ramp)
        obj.ramp.stops.append(stop)
        obj.ramp.validate()
        obj.touch_revision()
        self._selected_stop_id = stop.stop_id
        self.ramp_editor.setRamp(obj.ramp, stop.stop_id)
        self._commit_change(before, "Add gradient stop")
        self.refresh()

    def _remove_stop(self) -> None:
        obj = self.selected_gradient()
        if obj is None or len(obj.ramp.stops) <= 2:
            return
        before = self.canvas.chapter.to_dict()
        obj.ramp.stops = [
            stop for stop in obj.ramp.stops
            if stop.stop_id != self._selected_stop_id
        ]
        obj.ramp.validate()
        obj.touch_revision()
        self._selected_stop_id = obj.ramp.stops[0].stop_id
        self._commit_change(before, "Remove gradient stop")
        self.refresh()

    def _edit_stop_color(self, stop_id: str) -> None:
        obj = self.selected_gradient()
        if obj is None:
            return
        stop = next((
            item for item in obj.ramp.stops if item.stop_id == stop_id
        ), None)
        if stop is None:
            return
        self._selected_stop_id = stop_id
        self.color_popup.setColor(stop.color)
        self.color_popup.setQuickColors(
            self.canvas.primary_color, self.canvas.secondary_color
        )
        self.color_popup.open()

    def _apply_stop_color(self, color: str) -> None:
        obj = self.selected_gradient()
        if obj is None:
            return
        stop = next((
            item for item in obj.ramp.stops
            if item.stop_id == self._selected_stop_id
        ), None)
        if stop is None:
            return
        before = self.canvas.chapter.to_dict()
        stop.color = canonical_argb(color)
        obj.touch_revision()
        self._commit_change(before, "Change gradient color")
        self.refresh()

    def _preset_activated(self, index: int) -> None:
        if self._loading:
            return
        preset_id = str(self.preset_combo.itemData(index) or "")
        self._sync_preset_name()
        if preset_id:
            self.presetLoadRequested.emit(preset_id)

    def _sync_preset_name(self) -> None:
        preset_id = str(self.preset_combo.currentData() or "")
        built_in = preset_id == BUILTIN_PRIMARY_SECONDARY_ID
        self.preset_name.setEnabled(bool(preset_id) and not built_in)
        self.preset_name.setText(
            self.preset_combo.currentText() if preset_id else ""
        )
        self.save_preset.setEnabled(bool(preset_id) and not built_in)
        self.remove_preset.setEnabled(
            bool(preset_id) and not built_in
            and self.preset_combo.count() > 3
        )

    def _save_preset(self) -> None:
        preset_id = str(self.preset_combo.currentData() or "")
        if preset_id:
            self.presetSaveRequested.emit(preset_id)

    def _rename_preset(self) -> None:
        if self._loading:
            return
        preset_id = str(self.preset_combo.currentData() or "")
        if preset_id:
            self.presetRenameRequested.emit(
                preset_id, self.preset_name.text()
            )

    def _remove_preset(self) -> None:
        preset_id = str(self.preset_combo.currentData() or "")
        if preset_id:
            self.presetRemoveRequested.emit(preset_id)
