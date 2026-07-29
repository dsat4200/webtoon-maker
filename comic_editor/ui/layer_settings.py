"""Permanent settings panel for the canvas's active layer."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QCheckBox, QColorDialog, QComboBox, QDoubleSpinBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSlider,
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

        self.base_thickness = QDoubleSpinBox()
        self.base_thickness.setRange(0.1, 1000)
        self.base_thickness.setSuffix(" px")
        self.base_thickness_label = QLabel("Stroke thickness")
        self.form.addRow(self.base_thickness_label, self.base_thickness)

        self.border_width = QDoubleSpinBox()
        self.border_width.setRange(0, 200)
        self.border_width.setSuffix(" px")
        self.border_width_label = QLabel("Outline")
        self.form.addRow(self.border_width_label, self.border_width)
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
            self.base_thickness.valueChanged,
            self.border_width.valueChanged,
        ):
            signal.connect(self._apply)
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
                self.fill_row, self.base_thickness, self.border_width,
                self.border_color, self.grid_override, self.grid_size,
                self.grid_divisions,
            ):
                widget.setEnabled(False)
            self._updating = False
            return

        for widget in (
            self.name, self.visible, self.opacity, self.rectangle_mode,
            self.fill_row, self.base_thickness, self.border_width,
            self.border_color, self.grid_override, self.grid_size,
            self.grid_divisions,
        ):
            widget.setEnabled(True)
        self.type_label.setText(self._layer_title(layer))
        self.name.setText(layer.name)
        self.visible.setChecked(layer.visible)
        self.opacity.setValue(round(layer.opacity * 100))

        is_fill = layer.layer_kind == "fill"
        is_open = layer.layer_kind == "open_shape"
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
        self._set_pair_visible(
            self.base_thickness_label, self.base_thickness, is_open
        )
        self.base_thickness.setValue(layer.shape_style.base_thickness)
        self._set_pair_visible(
            self.border_width_label, self.border_width, not is_fill
        )
        self._set_pair_visible(
            self.border_color_label, self.border_color, not is_fill
        )
        self.border_width.setValue(layer.border_width)
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

    def _apply(self, *args) -> None:
        chapter = self.canvas.chapter
        if self._updating or chapter is None:
            return
        layer = chapter.layers.get(self.canvas.active_layer_id)
        if layer is None:
            return
        before = chapter.to_dict()
        layer.name = self.name.text().strip() or layer.name
        layer.visible = self.visible.isChecked()
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
            self.canvas.push_model_change(
                before, after, "Edit layer settings"
            )
            self.canvas.documentChanged.emit(None)
            self.canvas.hierarchyChanged.emit()
            self.changed.emit()
        self.canvas.update()
