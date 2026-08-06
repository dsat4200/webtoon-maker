"""Permanent settings panel for the canvas's active layer."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QSlider,
    QSpinBox, QWidget,
)

from comic_editor.core.models import GridSettings


class LayerSettingsPanel(QGroupBox):
    changed = Signal()

    def __init__(self, canvas, settings, save_settings_callback, parent=None):
        super().__init__("Layer Settings", parent)
        self.canvas = canvas
        self.settings = settings
        self._save_settings = save_settings_callback
        self._updating = False
        self._thickness_drag_before: dict | None = None
        self.form = QFormLayout(self)
        self.form.setContentsMargins(8, 8, 8, 8)
        self.form.setSpacing(5)

        self.type_label = QLabel("No active layer")
        self.type_label.setStyleSheet("font-weight: bold; color: #80c8ff")
        self.form.addRow(self.type_label)

        self.name = QLineEdit()
        self.form.addRow("Name", self.name)
        common = QWidget()
        common_layout = QHBoxLayout(common)
        common_layout.setContentsMargins(0, 0, 0, 0)
        self.visible = QCheckBox("Visible")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        common_layout.addWidget(self.visible)
        common_layout.addWidget(QLabel("Opacity"))
        common_layout.addWidget(self.opacity, 1)
        self.form.addRow(common)
        self.ignore_parent_mask = QCheckBox("Ignore direct parent mask")
        self.ignore_parent_mask.setToolTip(
            "Allow this layer and its descendants outside its direct parent "
            "shape while retaining higher ancestor masks."
        )
        self.form.addRow(self.ignore_parent_mask)

        self.compound_enabled = QCheckBox("Compound shape")
        self.form.addRow(self.compound_enabled)
        self.compound_operation = QComboBox()
        self.compound_operation.addItem("Add", "add")
        self.compound_operation.addItem("Subtract", "subtract")
        self.compound_operation.addItem("Ignore", "ignore")
        self.compound_operation_label = QLabel("Compound operation")
        self.form.addRow(
            self.compound_operation_label, self.compound_operation
        )
        self.flatten_compound = QPushButton("Flatten Compound")
        self.form.addRow(self.flatten_compound)

        self.rectangle_mode = QComboBox()
        self.rectangle_mode.addItem("Normal transform", "normal")
        self.rectangle_mode.addItem("Free points", "free")
        self.rectangle_mode_label = QLabel("Rectangle edit")
        self.form.addRow(self.rectangle_mode_label, self.rectangle_mode)

        self.fill_enabled = QCheckBox("Fill")
        self.fill_color = QPushButton("Color")
        self.fill_color.setProperty("color", "#ffffff")
        fill_row = QWidget()
        fill_layout = QHBoxLayout(fill_row)
        fill_layout.setContentsMargins(0, 0, 0, 0)
        fill_layout.addWidget(self.fill_enabled)
        fill_layout.addWidget(self.fill_color, 1)
        self.fill_row = fill_row
        self.form.addRow(fill_row)

        self.base_thickness_row = QWidget()
        base_thickness_layout = QHBoxLayout(self.base_thickness_row)
        base_thickness_layout.setContentsMargins(0, 0, 0, 0)
        self.base_thickness_slider = QSlider(Qt.Horizontal)
        self.base_thickness_slider.setRange(0, 1000)
        self.base_thickness = QSpinBox()
        self.base_thickness.setRange(0, 1000)
        self.base_thickness.setSuffix(" px")
        base_thickness_layout.addWidget(self.base_thickness_slider, 1)
        base_thickness_layout.addWidget(self.base_thickness)
        self.base_thickness_label = QLabel("Stroke thickness")
        self.form.addRow(
            self.base_thickness_label, self.base_thickness_row
        )

        self.border_width_row = QWidget()
        border_width_layout = QHBoxLayout(self.border_width_row)
        border_width_layout.setContentsMargins(0, 0, 0, 0)
        self.border_width_slider = QSlider(Qt.Horizontal)
        self.border_width_slider.setRange(0, 500)
        self.border_width = QSpinBox()
        self.border_width.setRange(0, 500)
        self.border_width.setSuffix(" px")
        border_width_layout.addWidget(self.border_width_slider, 1)
        border_width_layout.addWidget(self.border_width)
        self.border_width_label = QLabel("Outline")
        self.form.addRow(self.border_width_label, self.border_width_row)
        self.border_color = QPushButton("Color")
        self.border_color.setProperty("color", "#111111")
        self.border_color_label = QLabel("Outline color")
        self.form.addRow(self.border_color_label, self.border_color)

        self.point_width = QDoubleSpinBox()
        self.point_width.setRange(0.1, 10.0)
        self.point_width.setSingleStep(0.1)
        self.point_width.setSuffix("×")
        self.point_width.setReadOnly(True)
        self.point_width_label = QLabel("Point width")
        self.form.addRow(self.point_width_label, self.point_width)
        self.point_roundness = QDoubleSpinBox()
        self.point_roundness.setRange(0, 10000)
        self.point_roundness.setSuffix(" px")
        self.point_roundness.setReadOnly(True)
        self.point_roundness_label = QLabel("Point roundness")
        self.form.addRow(self.point_roundness_label, self.point_roundness)

        self.grid_override = QCheckBox("Override inherited grid")
        self.form.addRow(self.grid_override)
        self.grid_size = QSpinBox()
        self.grid_size.setRange(8, 1080)
        self.grid_size_label = QLabel("Grid size")
        self.form.addRow(self.grid_size_label, self.grid_size)
        self.grid_divisions = QSpinBox()
        self.grid_divisions.setRange(1, 16)
        self.grid_divisions_label = QLabel("Divisions")
        self.form.addRow(self.grid_divisions_label, self.grid_divisions)

        for signal in (
            self.visible.toggled,
            self.opacity.valueChanged,
            self.grid_override.toggled,
            self.grid_size.valueChanged,
            self.grid_divisions.valueChanged,
            self.fill_enabled.toggled,
            self.compound_enabled.toggled,
            self.compound_operation.currentIndexChanged,
            self.ignore_parent_mask.toggled,
        ):
            signal.connect(self._apply)
        self.base_thickness.valueChanged.connect(
            lambda value: self._thickness_spin_changed(
                self.base_thickness_slider, value
            )
        )
        self.border_width.valueChanged.connect(
            lambda value: self._thickness_spin_changed(
                self.border_width_slider, value
            )
        )
        self.base_thickness_slider.valueChanged.connect(
            lambda value: self._thickness_slider_changed(
                self.base_thickness, value
            )
        )
        self.border_width_slider.valueChanged.connect(
            lambda value: self._thickness_slider_changed(
                self.border_width, value
            )
        )
        for slider in (
            self.base_thickness_slider, self.border_width_slider,
        ):
            slider.sliderPressed.connect(self._begin_thickness_drag)
            slider.sliderReleased.connect(self._finish_thickness_drag)
        self.name.editingFinished.connect(self._apply)
        self.rectangle_mode.currentIndexChanged.connect(
            self._rectangle_mode_changed
        )
        self.fill_color.clicked.connect(
            lambda: self._choose_color(self.fill_color)
        )
        self.border_color.clicked.connect(
            lambda: self._choose_color(self.border_color)
        )
        self.flatten_compound.clicked.connect(self._flatten_compound)
        self.refresh()

    @staticmethod
    def _set_color_button(button: QPushButton, color: str) -> None:
        button.setProperty("color", color)
        button.setText(color.upper())
        button.setStyleSheet(
            f"background: {color}; color: "
            f"{'#000000' if QColor(color).lightness() > 150 else '#ffffff'}"
        )

    def _choose_color(self, button: QPushButton) -> None:
        color = QColorDialog.getColor(
            QColor(str(button.property("color"))), self
        )
        if color.isValid():
            self._set_color_button(button, color.name())
            self._apply()

    @staticmethod
    def _set_pair_visible(label: QWidget, field: QWidget, visible: bool) -> None:
        label.setVisible(visible)
        field.setVisible(visible)

    @staticmethod
    def _set_thickness_visible(
        label: QWidget, row: QWidget, slider: QWidget, field: QWidget,
        visible: bool,
    ) -> None:
        for widget in (label, row, slider, field):
            widget.setVisible(visible)

    @staticmethod
    def _layer_title(layer) -> str:
        if layer.is_page:
            return "Page"
        if layer.layer_kind == "fill":
            return "Fill Layer"
        if layer.layer_kind == "open_shape":
            return "Open Shape"
        return {
            "rectangle": "Rectangle Layer",
            "ellipse": "Circle Layer",
            "custom": "Shape Layer",
        }[layer.bound.primitive]

    def refresh(self) -> None:
        chapter = self.canvas.chapter
        layer = (
            chapter.layers.get(self.canvas.active_layer_id)
            if chapter is not None else None
        )
        self._updating = True
        if layer is None:
            self.type_label.setText("No active layer")
            for widget in (
                self.name, self.visible, self.opacity, self.rectangle_mode,
                self.fill_row, self.base_thickness_row, self.border_width_row,
                self.border_color, self.grid_override, self.grid_size,
                self.grid_divisions, self.compound_enabled,
                self.compound_operation, self.flatten_compound,
                self.ignore_parent_mask,
            ):
                widget.setEnabled(False)
            self._updating = False
            return

        for widget in (
            self.name, self.visible, self.opacity, self.rectangle_mode,
            self.fill_row, self.base_thickness_row, self.border_width_row,
            self.border_color, self.grid_override, self.grid_size,
            self.grid_divisions, self.compound_enabled,
            self.compound_operation, self.flatten_compound,
            self.ignore_parent_mask,
        ):
            widget.setEnabled(True)
        self.type_label.setText(self._layer_title(layer))
        self.name.setText(layer.name)
        self.visible.setChecked(layer.visible)
        self.opacity.setValue(round(layer.opacity * 100))
        self.ignore_parent_mask.setVisible(
            not layer.is_page and layer.layer_kind != "fill"
        )
        self.ignore_parent_mask.setChecked(layer.ignore_parent_mask)

        is_fill = layer.layer_kind == "fill"
        is_open = layer.layer_kind == "open_shape"
        compound_capable = not layer.is_page and not is_fill
        self.compound_enabled.setVisible(compound_capable)
        self.compound_enabled.setChecked(layer.compound_enabled)
        compound_parent = (
            chapter.closest_compound_ancestor(layer.layer_id)
            if not layer.is_page else None
        )
        self._set_pair_visible(
            self.compound_operation_label, self.compound_operation,
            compound_parent is not None,
        )
        self.compound_operation.setCurrentIndex(max(
            0, self.compound_operation.findData(layer.compound_operation)
        ))
        self.flatten_compound.setVisible(
            compound_capable and layer.compound_enabled
        )
        is_rectangle = (
            layer.bound is not None
            and layer.bound.primitive == "rectangle"
            and not is_fill
        )
        self._set_pair_visible(
            self.rectangle_mode_label, self.rectangle_mode, is_rectangle
        )
        self.rectangle_mode.setCurrentIndex(max(
            0, self.rectangle_mode.findData(
                self.settings.rectangle_edit_mode
            ),
        ))

        self.fill_enabled.setText("Stroke" if is_open else "Fill")
        self.fill_enabled.setChecked(bool(layer.fill_color))
        self.fill_enabled.setEnabled(not is_fill and not is_open)
        self._set_color_button(
            self.fill_color, layer.fill_color or "#ffffff"
        )
        self._set_thickness_visible(
            self.base_thickness_label, self.base_thickness_row,
            self.base_thickness_slider, self.base_thickness, is_open,
        )
        base_thickness = round(layer.shape_style.base_thickness)
        self.base_thickness.setValue(base_thickness)
        self.base_thickness_slider.setValue(base_thickness)
        self._set_thickness_visible(
            self.border_width_label, self.border_width_row,
            self.border_width_slider, self.border_width, not is_fill,
        )
        self._set_pair_visible(
            self.border_color_label, self.border_color, not is_fill
        )
        border_maximum = 40 if layer.is_page else 500
        self.border_width_slider.setRange(0, border_maximum)
        self.border_width.setRange(0, border_maximum)
        border_width = round(layer.border_width)
        self.border_width.setValue(border_width)
        self.border_width_slider.setValue(border_width)
        self._set_color_button(self.border_color, layer.border_color)

        selected_node = (
            self.canvas._selected_shape_node(layer.bound)
            if (
                self.canvas.selected_kind == "layer"
                and self.canvas.selected_id == layer.layer_id
                and layer.bound is not None
            ) else None
        )
        for label, field in (
            (self.point_width_label, self.point_width),
            (self.point_roundness_label, self.point_roundness),
        ):
            self._set_pair_visible(label, field, selected_node is not None)
        if selected_node is not None:
            self.point_width.setValue(selected_node.width_multiplier)
            self.point_roundness.setValue(selected_node.roundness)

        effective_grid = chapter.effective_grid(layer.layer_id)
        self.grid_override.setVisible(not is_fill)
        self._set_pair_visible(
            self.grid_size_label, self.grid_size, not is_fill
        )
        self._set_pair_visible(
            self.grid_divisions_label, self.grid_divisions, not is_fill
        )
        self.grid_override.setChecked(layer.grid_override is not None)
        self.grid_size.setValue(effective_grid.size)
        self.grid_divisions.setValue(effective_grid.divisions)
        self.grid_size.setEnabled(layer.grid_override is not None)
        self.grid_divisions.setEnabled(layer.grid_override is not None)
        self._updating = False

    def _rectangle_mode_changed(self, *args) -> None:
        if self._updating:
            return
        mode = self.rectangle_mode.currentData()
        if mode not in {"normal", "free"}:
            return
        self.settings.rectangle_edit_mode = mode
        self.settings.clamp()
        self._save_settings(self.settings)
        self.canvas.update()

    def _thickness_spin_changed(
        self, slider: QSlider, value: int,
    ) -> None:
        if slider.value() != value:
            slider.blockSignals(True)
            slider.setValue(value)
            slider.blockSignals(False)
        self._apply()

    def _thickness_slider_changed(
        self, field: QSpinBox, value: int,
    ) -> None:
        if field.value() != value:
            field.blockSignals(True)
            field.setValue(value)
            field.blockSignals(False)
        self._apply(push_undo=self._thickness_drag_before is None)

    def _begin_thickness_drag(self) -> None:
        if self._updating or self.canvas.chapter is None:
            return
        self._thickness_drag_before = self.canvas.chapter.to_dict()

    def _finish_thickness_drag(self) -> None:
        before, self._thickness_drag_before = (
            self._thickness_drag_before, None
        )
        if before is None or self.canvas.chapter is None:
            return
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(
                before, after, "Edit layer settings"
            )

    def _apply(self, *args, push_undo: bool = True) -> None:
        chapter = self.canvas.chapter
        if self._updating or chapter is None:
            return
        layer = chapter.layers.get(self.canvas.active_layer_id)
        if layer is None:
            return
        before = chapter.to_dict()
        layer.name = self.name.text().strip() or layer.name
        layer.visible = self.visible.isChecked()
        if not layer.is_page and layer.layer_kind != "fill":
            layer.ignore_parent_mask = self.ignore_parent_mask.isChecked()
        chapter.set_layer_opacity(layer.layer_id, self.opacity.value() / 100)
        layer.fill_color = (
            str(self.fill_color.property("color"))
            if layer.layer_kind in {"fill", "open_shape"}
            or self.fill_enabled.isChecked()
            else None
        )
        if layer.layer_kind != "fill":
            layer.shape_style.base_thickness = self.base_thickness.value()
            layer.border_width = self.border_width.value()
            layer.border_color = str(self.border_color.property("color"))
            layer.compound_enabled = self.compound_enabled.isChecked()
            operation = self.compound_operation.currentData()
            if operation in {"add", "subtract", "ignore"}:
                layer.compound_operation = operation
        if layer.layer_kind != "fill" and self.grid_override.isChecked():
            if layer.grid_override is None:
                inherited = (
                    chapter.effective_grid(layer.parent_id)
                    if layer.parent_id else chapter.grid
                )
                layer.grid_override = GridSettings.from_dict(
                    inherited.to_dict()
                )
            layer.grid_override.size = self.grid_size.value()
            layer.grid_override.divisions = self.grid_divisions.value()
            layer.grid_override.validate()
        else:
            layer.grid_override = None
        after = chapter.to_dict()
        if before != after:
            if push_undo:
                self.canvas.push_model_change(
                    before, after, "Edit layer settings"
                )
            self.canvas.documentChanged.emit(None)
            self.canvas.hierarchyChanged.emit()
            self.changed.emit()
        self.canvas.update()

    def _flatten_compound(self) -> None:
        chapter = self.canvas.chapter
        layer = (
            chapter.layers.get(self.canvas.active_layer_id)
            if chapter is not None else None
        )
        if layer is None or not layer.compound_enabled:
            return
        answer = QMessageBox.question(
            self, "Flatten Compound Shape",
            "Compile this compound and remove its contributing construction "
            "layers? This can be undone.",
        )
        if answer != QMessageBox.Yes:
            return
        if not self.canvas.flatten_compound_layer(layer.layer_id):
            QMessageBox.warning(
                self, "Cannot Flatten",
                "The calculated compound result is empty.",
            )
            return
        self.changed.emit()
        self.refresh()
