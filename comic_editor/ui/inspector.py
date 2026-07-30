"""Floating contextual inspector anchored above the selected entity."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QFontDatabase
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDoubleSpinBox, QFrame, QGridLayout, QHBoxLayout,
    QInputDialog, QLabel, QLineEdit, QMenu, QMessageBox, QPushButton, QSlider,
    QToolButton, QVBoxLayout, QWidget, QWidgetAction,
)

from comic_editor.core.models import (
    RasterObject, TextObject, VectorDrawingObject, VectorFillObject,
)
from comic_editor.core.settings import TextPreset


class ContextInspector(QFrame):
    changed = Signal()
    pencilPresetSelected = Signal(str)
    pencilSettingsRequested = Signal()
    brushSizeSelected = Signal(str, str)
    brushSizesRequested = Signal()
    eraserShapeChanged = Signal(bool)

    def __init__(self, canvas, settings, save_settings_callback, parent=None):
        super().__init__(parent or canvas)
        self.canvas = canvas
        self.settings = settings
        self._save_settings = save_settings_callback
        self._updating = False
        self._underlay_drag_before: dict | None = None
        self._underlay_drag_object_id = ""
        self._alignment_buttons: dict[tuple[str, str], QToolButton] = {}
        self.setObjectName("contextInspector")
        self.setStyleSheet(
            "#contextInspector { background: rgba(28,28,32,238); "
            "border: 1px solid #5d7d9c; border-radius: 8px; }"
        )
        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 6, 8, 6)
        outer.setSpacing(4)

        header = QHBoxLayout()
        self.title = QLabel("Selection")
        self.title.setStyleSheet("font-weight: bold; color: #80c8ff")
        header.addWidget(self.title, 1)
        self.preset_controls = QWidget()
        preset_row = QHBoxLayout(self.preset_controls)
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.setSpacing(2)
        self.preset_combo = QComboBox()
        self.preset_combo.setMinimumWidth(110)
        preset_row.addWidget(self.preset_combo)
        self.preset_save = self._small_button("S", "Overwrite selected preset")
        self.preset_rename = self._small_button("R", "Rename selected preset")
        self.preset_remove = self._small_button("X", "Remove selected preset")
        self.preset_add = self._small_button("+", "Create preset")
        for button in (
            self.preset_save, self.preset_rename, self.preset_remove, self.preset_add,
        ):
            preset_row.addWidget(button)
        header.addWidget(self.preset_controls)
        outer.addLayout(header)

        self.name_row = QWidget()
        name_row = QHBoxLayout(self.name_row)
        name_row.setContentsMargins(0, 0, 0, 0)
        name_row.addWidget(QLabel("Name"))
        self.name = QLineEdit()
        name_row.addWidget(self.name, 1)
        outer.addWidget(self.name_row)

        common = QHBoxLayout()
        self.visible = QCheckBox("Visible")
        self.opacity_lock = QCheckBox("Lock opacity")
        self.opacity = QSlider(Qt.Horizontal)
        self.opacity.setRange(0, 100)
        self.opacity.setFixedWidth(95)
        common.addWidget(self.visible)
        common.addWidget(self.opacity_lock)
        common.addWidget(QLabel("Opacity"))
        common.addWidget(self.opacity)
        outer.addLayout(common)
        self.ignore_parent_mask = QCheckBox("Ignore direct parent mask")
        self.ignore_parent_mask.setToolTip(
            "Allow this drawing and its children outside its direct parent "
            "shape while retaining higher ancestor masks."
        )
        outer.addWidget(self.ignore_parent_mask)

        self.underlay_row = QWidget()
        underlay_layout = QHBoxLayout(self.underlay_row)
        underlay_layout.setContentsMargins(0, 0, 0, 0)
        underlay_layout.addWidget(QLabel("Show underlay"))
        self.underlay = QSlider(Qt.Horizontal)
        self.underlay.setRange(0, 100)
        self.underlay.setToolTip(
            "Reveal the complete selected drawing above shape masks while "
            "reducing its normal in-place copy."
        )
        underlay_layout.addWidget(self.underlay, 1)
        self.underlay_value = QLabel("0%")
        self.underlay_value.setMinimumWidth(36)
        self.underlay_value.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        underlay_layout.addWidget(self.underlay_value)
        outer.addWidget(self.underlay_row)

        self.text_panel = QWidget()
        text_layout = QVBoxLayout(self.text_panel)
        text_layout.setContentsMargins(0, 0, 0, 0)
        text_layout.setSpacing(4)
        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Font"))
        self.font_family = QComboBox()
        self.font_family.addItems(QFontDatabase.families())
        font_row.addWidget(self.font_family, 1)
        text_layout.addLayout(font_row)

        metrics_row = QHBoxLayout()
        metrics_row.setSpacing(4)
        metrics_row.addWidget(QLabel("Size"))
        self.font_size = QDoubleSpinBox()
        self.font_size.setRange(6, 288)
        self.font_size.setMaximumWidth(72)
        metrics_row.addWidget(self.font_size)
        self.bold = QCheckBox("Bold")
        self.italic = QCheckBox("Italic")
        metrics_row.addWidget(self.bold)
        metrics_row.addWidget(self.italic)
        metrics_row.addWidget(QLabel("Kerning"))
        self.kerning = QDoubleSpinBox()
        self.kerning.setRange(-20, 100)
        self.kerning.setMaximumWidth(72)
        metrics_row.addWidget(self.kerning)
        text_layout.addLayout(metrics_row)

        layout_row = QHBoxLayout()
        layout_row.addWidget(QLabel("Layout"))
        self.layout_mode = QComboBox()
        self.layout_mode.addItem("Strict to parent", "strict")
        self.layout_mode.addItem("Free transform", "free")
        layout_row.addWidget(self.layout_mode, 1)
        self.align_button = QToolButton()
        self.align_button.setText("Align")
        self.align_button.setPopupMode(QToolButton.InstantPopup)
        self.align_menu = QMenu(self.align_button)
        self.align_button.setMenu(self.align_menu)
        layout_row.addWidget(self.align_button)
        text_layout.addLayout(layout_row)

        self.margin_row = QWidget()
        margin_layout = QHBoxLayout(self.margin_row)
        margin_layout.setContentsMargins(0, 0, 0, 0)
        margin_layout.addWidget(QLabel("Margin"))
        self.margin = QDoubleSpinBox()
        self.margin.setRange(0, 500)
        self.margin.setSuffix(" px")
        margin_layout.addWidget(self.margin, 1)
        text_layout.addWidget(self.margin_row)

        self.geometry_reference_row = QWidget()
        reference_layout = QHBoxLayout(self.geometry_reference_row)
        reference_layout.setContentsMargins(0, 0, 0, 0)
        reference_layout.addWidget(QLabel("Shape reference"))
        self.geometry_reference = QComboBox()
        self.geometry_reference.addItem("Direct parent", "direct")
        self.geometry_reference.addItem("Closest compound", "compound")
        reference_layout.addWidget(self.geometry_reference, 1)
        outer.addWidget(self.geometry_reference_row)

        self.alignment_widget = QWidget()
        alignment_grid = QGridLayout(self.alignment_widget)
        alignment_grid.setContentsMargins(5, 5, 5, 5)
        alignment_grid.setSpacing(2)
        for row, vertical in enumerate(("top", "middle", "bottom")):
            for column, horizontal in enumerate(("left", "center", "right")):
                button = QToolButton()
                button.setText("●")
                button.setCheckable(True)
                button.setToolTip(f"{vertical.title()} / {horizontal.title()}")
                button.clicked.connect(
                    lambda checked=False, h=horizontal, v=vertical:
                    self._set_alignment(h, v)
                )
                alignment_grid.addWidget(button, row, column)
                self._alignment_buttons[(horizontal, vertical)] = button
        alignment_action = QWidgetAction(self.align_menu)
        alignment_action.setDefaultWidget(self.alignment_widget)
        self.align_menu.addAction(alignment_action)
        outer.addWidget(self.text_panel)

        self.transform_panel = QWidget()
        transform_row = QHBoxLayout(self.transform_panel)
        transform_row.setContentsMargins(0, 0, 0, 0)
        transform_row.addWidget(QLabel("Transform"))
        self.transform_mode = QComboBox()
        self.transform_mode.addItem("Free Projective", "free")
        self.transform_mode.addItem("Uniform", "uniform")
        transform_row.addWidget(self.transform_mode)
        outer.addWidget(self.transform_panel)

        self.raster_tool_panel = QWidget()
        raster_layout = QVBoxLayout(self.raster_tool_panel)
        raster_layout.setContentsMargins(0, 4, 0, 0)
        raster_layout.setSpacing(4)
        self.raster_tool_title = QLabel("Raster Pencil")
        self.raster_tool_title.setStyleSheet(
            "font-weight: bold; color: #80c8ff"
        )
        raster_layout.addWidget(self.raster_tool_title)
        self.pencil_tool_controls = QWidget()
        pencil_layout = QHBoxLayout(self.pencil_tool_controls)
        pencil_layout.setContentsMargins(0, 0, 0, 0)
        pencil_layout.addWidget(QLabel("Preset"))
        self.pencil_preset_combo = QComboBox()
        pencil_layout.addWidget(self.pencil_preset_combo, 1)
        self.pencil_settings_button = QPushButton("Pressure / Presets…")
        pencil_layout.addWidget(self.pencil_settings_button)
        raster_layout.addWidget(self.pencil_tool_controls)
        brush_row = QHBoxLayout()
        brush_row.addWidget(QLabel("Size"))
        self.brush_size_combo = QComboBox()
        self.brush_size_combo.addItem("S", "small")
        self.brush_size_combo.addItem("M", "medium")
        self.brush_size_combo.addItem("L", "large")
        brush_row.addWidget(self.brush_size_combo)
        self.eraser_shape_label = QLabel("Shape")
        self.eraser_shape = QComboBox()
        self.eraser_shape.addItem("Circle", False)
        self.eraser_shape.addItem("Square", True)
        brush_row.addWidget(self.eraser_shape_label)
        brush_row.addWidget(self.eraser_shape)
        self.brush_sizes_button = QPushButton("Configure sizes…")
        brush_row.addWidget(self.brush_sizes_button)
        brush_row.addStretch(1)
        raster_layout.addLayout(brush_row)
        outer.addWidget(self.raster_tool_panel)
        self.setFixedWidth(390)

        for control, signal in (
            (self.visible, self.visible.toggled),
            (self.opacity_lock, self.opacity_lock.toggled),
            (self.opacity, self.opacity.valueChanged),
            (self.ignore_parent_mask, self.ignore_parent_mask.toggled),
            (self.font_family, self.font_family.currentTextChanged),
            (self.font_size, self.font_size.valueChanged),
            (self.bold, self.bold.toggled),
            (self.italic, self.italic.toggled),
            (self.kerning, self.kerning.valueChanged),
            (self.layout_mode, self.layout_mode.currentIndexChanged),
            (self.margin, self.margin.valueChanged),
            (self.geometry_reference,
             self.geometry_reference.currentIndexChanged),
        ):
            signal.connect(self._apply)
        self.name.editingFinished.connect(self._apply)
        self.underlay.sliderPressed.connect(self._begin_underlay_drag)
        self.underlay.valueChanged.connect(self._underlay_changed)
        self.underlay.sliderReleased.connect(self._finish_underlay_drag)
        self.transform_mode.currentIndexChanged.connect(self._transform_settings_changed)
        self.pencil_preset_combo.currentTextChanged.connect(
            self.pencilPresetSelected.emit
        )
        self.pencil_settings_button.clicked.connect(
            self.pencilSettingsRequested.emit
        )
        self.brush_size_combo.currentIndexChanged.connect(
            self._brush_size_changed
        )
        self.brush_sizes_button.clicked.connect(
            self.brushSizesRequested.emit
        )
        self.eraser_shape.currentIndexChanged.connect(
            self._eraser_shape_changed
        )
        self.preset_combo.activated.connect(self._apply_preset)
        self.preset_save.clicked.connect(self._save_preset)
        self.preset_rename.clicked.connect(self._rename_preset)
        self.preset_remove.clicked.connect(self._remove_preset)
        self.preset_add.clicked.connect(self._add_preset)
        self.hide()

    @staticmethod
    def _small_button(text: str, tooltip: str) -> QToolButton:
        button = QToolButton()
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedWidth(24)
        return button

    def _text_entity(self) -> TextObject | None:
        if self.canvas.chapter is None or not self.canvas.selected_object_id:
            return None
        entity = self.canvas.chapter.objects.get(self.canvas.selected_object_id)
        return entity if isinstance(entity, TextObject) else None

    def refresh_brush_controls(self) -> None:
        self.pencil_preset_combo.blockSignals(True)
        self.pencil_preset_combo.clear()
        self.pencil_preset_combo.addItems([
            item["name"] for item in self.settings.pencil_presets
        ])
        self.pencil_preset_combo.setCurrentText(
            self.settings.active_pencil_preset
        )
        self.pencil_preset_combo.blockSignals(False)
        pencil = self.canvas.tool.value == "raster_pencil"
        size = (
            self.settings.active_pencil_size
            if pencil else self.settings.active_eraser_size
        )
        self.brush_size_combo.blockSignals(True)
        self.brush_size_combo.setCurrentIndex(
            max(0, self.brush_size_combo.findData(size))
        )
        self.brush_size_combo.blockSignals(False)
        self.eraser_shape.blockSignals(True)
        self.eraser_shape.setCurrentIndex(
            max(0, self.eraser_shape.findData(self.settings.eraser_square))
        )
        self.eraser_shape.blockSignals(False)

    def _brush_size_changed(self, *args) -> None:
        if self._updating:
            return
        size = self.brush_size_combo.currentData()
        if size:
            self.brushSizeSelected.emit(self.canvas.tool.value, size)

    def _eraser_shape_changed(self, *args) -> None:
        if self._updating:
            return
        self.eraserShapeChanged.emit(bool(self.eraser_shape.currentData()))

    def refresh(self) -> None:
        chapter = self.canvas.chapter
        if (
            chapter is None or not self.canvas.selected_id
            or self.canvas.tool.value == "object_select"
            or self.canvas.selected_kind != "object"
        ):
            self.hide()
            return
        self._updating = True
        entity = chapter.objects[self.canvas.selected_id]
        if isinstance(entity, RasterObject):
            self._updating = False
            self.hide()
            return
        self.title.setText(
            entity.display_name
            if isinstance(entity, TextObject)
            else (
                "Vector Drawing"
                if isinstance(entity, VectorDrawingObject)
                else "Vector Fill"
                if isinstance(entity, VectorFillObject)
                else entity.object_type.title()
            )
        )
        self.opacity_lock.show()
        self.opacity_lock.setChecked(entity.opacity_locked)
        self.text_panel.setVisible(isinstance(entity, TextObject))
        self.preset_controls.setVisible(isinstance(entity, TextObject))
        self.name_row.setVisible(not isinstance(entity, TextObject))
        self.transform_panel.setVisible(
            isinstance(entity, RasterObject)
            or (
                isinstance(entity, TextObject)
                and entity.layout_mode == "free"
            )
        )
        compound_parent = self.canvas.chapter.closest_compound_ancestor(
            entity.parent_layer_id, include_self=True
        )
        self.geometry_reference_row.setVisible(
            isinstance(entity, (RasterObject, TextObject))
            and compound_parent is not None
        )
        self.geometry_reference.setCurrentIndex(max(
            0, self.geometry_reference.findData(
                entity.geometry_reference
            )
        ))
        if isinstance(entity, TextObject):
            self._refresh_presets()
            self.font_family.setCurrentText(entity.font_family)
            self.font_size.setValue(entity.font_size)
            self.bold.setChecked(entity.bold)
            self.italic.setChecked(entity.italic)
            self.kerning.setValue(entity.kerning)
            self.layout_mode.setCurrentIndex(
                max(0, self.layout_mode.findData(entity.layout_mode))
            )
            self.margin.setValue(entity.margin)
            strict = entity.layout_mode == "strict"
            self.margin_row.setVisible(strict)
            for key, button in self._alignment_buttons.items():
                button.setChecked(key == (
                    entity.horizontal_alignment, entity.vertical_alignment
                ))
        # Drawing-tool controls live in the persistent ribbon.  Keep the old
        # child widgets as compatibility signal sources for older settings
        # tests/plugins, but never show them in the floating object inspector.
        raster_tool = False
        self.raster_tool_panel.setVisible(raster_tool)
        if raster_tool:
            pencil = self.canvas.tool.value == "raster_pencil"
            self.raster_tool_title.setText(
                "Raster Pencil" if pencil else "Raster Eraser"
            )
            self.pencil_tool_controls.setVisible(pencil)
            self.eraser_shape_label.setVisible(not pencil)
            self.eraser_shape.setVisible(not pencil)
            self.refresh_brush_controls()
        self.name.setText(entity.name)
        self.visible.setChecked(entity.visible)
        self.opacity.setValue(round(entity.opacity * 100))
        self.opacity.setEnabled(not getattr(entity, "opacity_locked", False))
        self.ignore_parent_mask.setVisible(
            isinstance(entity, (RasterObject, VectorDrawingObject))
        )
        self.ignore_parent_mask.setChecked(
            bool(getattr(entity, "ignore_parent_mask", False))
        )
        drawing = isinstance(entity, (RasterObject, VectorDrawingObject))
        self.underlay_row.setVisible(drawing)
        self.underlay.setValue(round(
            float(getattr(entity, "underlay_opacity", 0.0)) * 100
        ))
        self.underlay_value.setText(f"{self.underlay.value()}%")
        self.transform_mode.setCurrentIndex(
            max(0, self.transform_mode.findData(self.settings.transform_mode))
        )
        self._updating = False
        self.adjustSize()
        self.reposition()
        self.show()
        self.raise_()

    def reposition(self) -> None:
        if not self.isVisible() and not self.canvas.selected_id:
            return
        rect = self.canvas.selected_widget_rect()
        if rect.isEmpty():
            return
        x = max(8, min(
            self.canvas.width() - self.width() - 8,
            rect.center().x() - self.width() // 2,
        ))
        y = rect.top() - self.height() - 10
        if y < 8:
            y = min(self.canvas.height() - self.height() - 8, rect.bottom() + 10)
        self.move(x, max(8, y))

    def _set_alignment(self, horizontal: str, vertical: str) -> None:
        entity = self._text_entity()
        if entity is None:
            return
        for key, button in self._alignment_buttons.items():
            button.setChecked(key == (horizontal, vertical))
        self.align_menu.close()
        self._apply()

    def _apply(self, *args) -> None:
        if (
            self._updating or self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
            or not self.canvas.selected_id
        ):
            return
        chapter = self.canvas.chapter
        before = chapter.to_dict()
        entity = chapter.objects[self.canvas.selected_id]
        if not isinstance(entity, TextObject):
            entity.name = self.name.text().strip() or entity.name
        entity.visible = self.visible.isChecked()
        if isinstance(entity, (RasterObject, VectorDrawingObject)):
            entity.ignore_parent_mask = self.ignore_parent_mask.isChecked()
        entity.opacity_locked = self.opacity_lock.isChecked()
        entity.opacity = (
            chapter.layers[entity.parent_layer_id].opacity
            if entity.opacity_locked else self.opacity.value() / 100
        )
        if isinstance(entity, (RasterObject, TextObject)):
            reference = self.geometry_reference.currentData()
            entity.geometry_reference = (
                reference
                if reference in {"direct", "compound"}
                else "direct"
            )
        if isinstance(entity, TextObject):
            entity.font_family = self.font_family.currentText()
            entity.font_size = self.font_size.value()
            entity.bold = self.bold.isChecked()
            entity.italic = self.italic.isChecked()
            entity.kerning = self.kerning.value()
            entity.layout_mode = self.layout_mode.currentData()
            entity.margin = self.margin.value()
            checked = next(
                (key for key, button in self._alignment_buttons.items()
                 if button.isChecked()),
                (entity.horizontal_alignment, entity.vertical_alignment),
            )
            entity.horizontal_alignment, entity.vertical_alignment = checked
            if entity.layout_mode == "free" and entity.transform_quad is None:
                entity.transform_quad = self.canvas._rect_quad(
                    self.canvas._strict_text_rect(entity)
                )
        after = chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, "Edit properties")
            self.canvas.documentChanged.emit(None)
            self.changed.emit()
            self.canvas.update()
        self.refresh()

    def _transform_settings_changed(self, *args) -> None:
        if self._updating:
            return
        self.settings.transform_mode = self.transform_mode.currentData()
        self._save_settings(self.settings)
        self.canvas.update()

    def _current_preset(self) -> TextPreset | None:
        entity = self._text_entity()
        if entity is None:
            return None
        return TextPreset(
            name=self.preset_combo.currentText() or "Default",
            font_family=entity.font_family, font_size=entity.font_size,
            bold=entity.bold, italic=entity.italic, kerning=entity.kerning,
            layout_mode=entity.layout_mode,
            horizontal_alignment=entity.horizontal_alignment,
            vertical_alignment=entity.vertical_alignment, margin=entity.margin,
        )

    def _refresh_presets(self) -> None:
        current = self.settings.active_text_preset
        self.preset_combo.blockSignals(True)
        self.preset_combo.clear()
        for item in self.settings.text_presets:
            self.preset_combo.addItem(item["name"])
        index = self.preset_combo.findText(current)
        self.preset_combo.setCurrentIndex(max(0, index))
        self.preset_combo.blockSignals(False)
        protected = self.preset_combo.currentText() == "Default"
        self.preset_rename.setEnabled(not protected)
        self.preset_remove.setEnabled(not protected)

    def _apply_preset(self, index: int) -> None:
        entity = self._text_entity()
        if entity is None or index < 0:
            return
        preset = TextPreset.from_dict(self.settings.text_presets[index])
        before = self.canvas.chapter.to_dict()
        for key in (
            "font_family", "font_size", "bold", "italic", "kerning",
            "layout_mode", "horizontal_alignment", "vertical_alignment", "margin",
        ):
            setattr(entity, key, getattr(preset, key))
        self.settings.active_text_preset = preset.name
        self._save_settings(self.settings)
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, "Apply text preset")
            self.canvas.documentChanged.emit(None)
        self.refresh()

    def _save_preset(self) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        index = self.preset_combo.currentIndex()
        if index < 0:
            return
        self.settings.text_presets[index] = preset.to_dict()
        self.settings.active_text_preset = preset.name
        self._save_settings(self.settings)
        self.refresh()

    def _add_preset(self) -> None:
        preset = self._current_preset()
        if preset is None:
            return
        name, accepted = QInputDialog.getText(self, "New text preset", "Preset name")
        if not accepted or not name.strip():
            return
        if any(item["name"].casefold() == name.strip().casefold()
               for item in self.settings.text_presets):
            QMessageBox.warning(self, "Text preset", "That preset name already exists.")
            return
        preset.name = name.strip()
        self.settings.text_presets.append(preset.to_dict())
        self.settings.active_text_preset = preset.name
        self._save_settings(self.settings)
        self.refresh()

    def _rename_preset(self) -> None:
        index = self.preset_combo.currentIndex()
        if index <= 0:
            return
        current = self.settings.text_presets[index]["name"]
        name, accepted = QInputDialog.getText(
            self, "Rename text preset", "Preset name", text=current
        )
        if not accepted or not name.strip():
            return
        if any(
            item_index != index and item["name"].casefold() == name.strip().casefold()
            for item_index, item in enumerate(self.settings.text_presets)
        ):
            QMessageBox.warning(self, "Text preset", "That preset name already exists.")
            return
        self.settings.text_presets[index]["name"] = name.strip()
        self.settings.active_text_preset = name.strip()
        self._save_settings(self.settings)
        self.refresh()

    def _remove_preset(self) -> None:
        index = self.preset_combo.currentIndex()
        if index <= 0:
            return
        self.settings.text_presets.pop(index)
        self.settings.active_text_preset = "Default"
        self._save_settings(self.settings)
        self.refresh()

    def _begin_underlay_drag(self) -> None:
        if (
            self._updating or self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return
        entity = self.canvas.chapter.objects.get(self.canvas.selected_id)
        if not isinstance(entity, (RasterObject, VectorDrawingObject)):
            return
        self._underlay_drag_before = self.canvas.chapter.to_dict()
        self._underlay_drag_object_id = entity.object_id

    def _underlay_changed(self, value: int) -> None:
        self.underlay_value.setText(f"{int(value)}%")
        if (
            self._updating or self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return
        entity = self.canvas.chapter.objects.get(self.canvas.selected_id)
        if not isinstance(entity, (RasterObject, VectorDrawingObject)):
            return
        before = (
            None if self._underlay_drag_before is not None
            else self.canvas.chapter.to_dict()
        )
        entity.underlay_opacity = max(0.0, min(1.0, value / 100.0))
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            after = self.canvas.chapter.to_dict()
            if before != after:
                self.canvas.push_model_change(
                    before, after, "Change drawing underlay"
                )
                self.changed.emit()

    def _finish_underlay_drag(self) -> None:
        before = self._underlay_drag_before
        object_id = self._underlay_drag_object_id
        self._underlay_drag_before = None
        self._underlay_drag_object_id = ""
        if before is None or self.canvas.chapter is None:
            return
        if object_id not in self.canvas.chapter.objects:
            return
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(
                before, after, "Change drawing underlay"
            )
            self.changed.emit()
        self.canvas.interactionFinished.emit()
