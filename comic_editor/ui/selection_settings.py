"""Selection-scoped controls hosted above the layer/object outliner."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QSlider, QStackedWidget, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    ImageObject, RasterObject, TextObject, VectorDrawingObject, VectorFillObject,
)
from comic_editor.ui.layer_settings import LayerSettingsPanel
from comic_editor.ui.tool_ribbon_pages import RasterObjectControls


class SelectionCommonControls(QWidget):
    """Pinned visibility and opacity controls for the exact selection."""

    changed = Signal()

    def __init__(self, canvas, parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self._updating = False
        self._opacity_before: dict | None = None
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(5)
        self.visible = QCheckBox("Visible", self)
        self.opacity = QSlider(Qt.Orientation.Horizontal, self)
        self.opacity.setRange(0, 100)
        self.opacity_value = QLabel("100%", self)
        self.opacity_value.setMinimumWidth(38)
        self.opacity_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        layout.addWidget(self.visible)
        layout.addWidget(QLabel("Opacity", self))
        layout.addWidget(self.opacity, 1)
        layout.addWidget(self.opacity_value)
        self.visible.toggled.connect(self._visibility_changed)
        self.opacity.sliderPressed.connect(self._begin_opacity_drag)
        self.opacity.valueChanged.connect(self._opacity_changed)
        self.opacity.sliderReleased.connect(self._finish_opacity_drag)
        self.refresh()

    def _selected(self):
        chapter = self.canvas.chapter
        if chapter is None or not self.canvas.selected_id:
            return None
        if self.canvas.selected_kind == "layer":
            return chapter.layers.get(self.canvas.selected_id)
        if self.canvas.selected_kind == "object":
            return chapter.objects.get(self.canvas.selected_id)
        return None

    def _commit_text(self, target) -> None:
        if isinstance(target, TextObject):
            self.canvas.commit_active_text_edit()

    def refresh(self) -> None:
        target = self._selected()
        self._updating = True
        enabled = target is not None
        self.visible.setEnabled(enabled)
        self.opacity.setEnabled(
            enabled and not bool(getattr(target, "opacity_locked", False))
        )
        if target is not None:
            self.visible.setChecked(bool(target.visible))
            value = round(float(target.opacity) * 100)
            self.opacity.setValue(value)
            self.opacity_value.setText(f"{value}%")
        else:
            self.visible.setChecked(False)
            self.opacity.setValue(100)
            self.opacity_value.setText("—")
        self._updating = False

    def _visibility_changed(self, checked: bool) -> None:
        target = self._selected()
        if self._updating or target is None:
            return
        self._commit_text(target)
        target = self._selected()
        if target is None:
            return
        before = self.canvas.chapter.to_dict()
        target.visible = bool(checked)
        self._push(before, "Change visibility", hierarchy=True)

    def _begin_opacity_drag(self) -> None:
        target = self._selected()
        if self._updating or target is None:
            return
        self._commit_text(target)
        target = self._selected()
        if target is None or bool(getattr(target, "opacity_locked", False)):
            return
        self._opacity_before = self.canvas.chapter.to_dict()

    def _opacity_changed(self, value: int) -> None:
        self.opacity_value.setText(f"{int(value)}%")
        target = self._selected()
        if (
            self._updating or target is None
            or bool(getattr(target, "opacity_locked", False))
        ):
            return
        if self._opacity_before is None:
            self._commit_text(target)
            target = self._selected()
            if target is None:
                return
            before = self.canvas.chapter.to_dict()
        else:
            before = None
        if self.canvas.selected_kind == "layer":
            self.canvas.chapter.set_layer_opacity(
                target.layer_id, value / 100.0
            )
        else:
            target.opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            self._push(before, "Change opacity")

    def _finish_opacity_drag(self) -> None:
        before, self._opacity_before = self._opacity_before, None
        if before is not None:
            self._push(before, "Change opacity")
        self.refresh()

    def _push(
        self, before: dict, label: str, *, hierarchy: bool = False,
    ) -> None:
        after = self.canvas.chapter.to_dict()
        if before == after:
            return
        self.canvas.push_model_change(before, after, label)
        if hierarchy:
            self.canvas.hierarchyChanged.emit()
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        self.changed.emit()


class VectorObjectSettings(QWidget):
    """Persistent object properties for vector drawings and fills."""

    changed = Signal()

    def __init__(self, canvas, parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self._updating = False
        self._underlay_before: dict | None = None
        form = QFormLayout(self)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(5)
        self.type_label = QLabel("Vector Object", self)
        self.type_label.setStyleSheet("font-weight: bold; color: #80c8ff")
        form.addRow(self.type_label)
        self.name = QLineEdit(self)
        form.addRow("Name", self.name)
        self.opacity_lock = QCheckBox("Lock opacity", self)
        form.addRow(self.opacity_lock)
        self.ignore_parent_mask = QCheckBox(
            "Ignore direct parent mask", self
        )
        form.addRow(self.ignore_parent_mask)
        underlay_row = QWidget(self)
        underlay_layout = QHBoxLayout(underlay_row)
        underlay_layout.setContentsMargins(0, 0, 0, 0)
        self.underlay = QSlider(Qt.Orientation.Horizontal, underlay_row)
        self.underlay.setRange(0, 100)
        self.underlay_value = QLabel("0%", underlay_row)
        self.underlay_value.setMinimumWidth(38)
        underlay_layout.addWidget(self.underlay, 1)
        underlay_layout.addWidget(self.underlay_value)
        self.underlay_row = underlay_row
        self.underlay_label = QLabel("Show underlay", self)
        form.addRow(self.underlay_label, underlay_row)
        self.name.editingFinished.connect(self._apply_discrete)
        self.opacity_lock.toggled.connect(self._apply_discrete)
        self.ignore_parent_mask.toggled.connect(self._apply_discrete)
        self.underlay.sliderPressed.connect(self._begin_underlay)
        self.underlay.valueChanged.connect(self._underlay_changed)
        self.underlay.sliderReleased.connect(self._finish_underlay)

    def _selected(self):
        if (
            self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return None
        target = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return target if isinstance(
            target, (VectorDrawingObject, VectorFillObject)
        ) else None

    def refresh(self) -> None:
        target = self._selected()
        self._updating = True
        enabled = target is not None
        for control in (
            self.name, self.opacity_lock, self.ignore_parent_mask,
            self.underlay,
        ):
            control.setEnabled(enabled)
        if target is not None:
            drawing = isinstance(target, VectorDrawingObject)
            self.type_label.setText(
                "Vector Drawing" if drawing else "Vector Fill"
            )
            self.name.setText(target.name)
            self.opacity_lock.setChecked(target.opacity_locked)
            self.ignore_parent_mask.setVisible(drawing)
            self.ignore_parent_mask.setChecked(target.ignore_parent_mask)
            self.underlay_label.setVisible(drawing)
            self.underlay_row.setVisible(drawing)
            value = round(target.underlay_opacity * 100)
            self.underlay.setValue(value)
            self.underlay_value.setText(f"{value}%")
        self._updating = False

    def _apply_discrete(self, *args) -> None:
        del args
        target = self._selected()
        if self._updating or target is None:
            return
        before = self.canvas.chapter.to_dict()
        target.name = self.name.text().strip() or target.name
        target.opacity_locked = self.opacity_lock.isChecked()
        if target.opacity_locked:
            target.opacity = self.canvas.chapter.layers[
                target.parent_layer_id
            ].opacity
        if isinstance(target, VectorDrawingObject):
            target.ignore_parent_mask = self.ignore_parent_mask.isChecked()
        self._push(before, "Edit vector object")

    def _begin_underlay(self) -> None:
        if not self._updating and isinstance(
            self._selected(), VectorDrawingObject
        ):
            self._underlay_before = self.canvas.chapter.to_dict()

    def _underlay_changed(self, value: int) -> None:
        self.underlay_value.setText(f"{int(value)}%")
        target = self._selected()
        if self._updating or not isinstance(target, VectorDrawingObject):
            return
        before = (
            None if self._underlay_before is not None
            else self.canvas.chapter.to_dict()
        )
        target.underlay_opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            self._push(before, "Change vector underlay")

    def _finish_underlay(self) -> None:
        before, self._underlay_before = self._underlay_before, None
        if before is not None:
            self._push(before, "Change vector underlay")

    def _push(self, before: dict, label: str) -> None:
        after = self.canvas.chapter.to_dict()
        if before == after:
            return
        self.canvas.push_model_change(before, after, label)
        self.canvas.hierarchyChanged.emit()
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        self.changed.emit()
        self.refresh()


class ImageObjectSettings(QWidget):
    """Embedded-image metadata, masking, and parent-fit behavior."""

    changed = Signal()

    def __init__(self, canvas, parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self._updating = False
        self._underlay_before: dict | None = None
        form = QFormLayout(self)
        form.setContentsMargins(8, 8, 8, 8)
        form.setSpacing(5)
        title = QLabel("Image Object", self)
        title.setStyleSheet("font-weight: bold; color: #80c8ff")
        form.addRow(title)
        self.name = QLineEdit(self)
        form.addRow("Name", self.name)
        self.opacity_lock = QCheckBox("Lock opacity", self)
        form.addRow(self.opacity_lock)
        self.ignore_parent_mask = QCheckBox("Ignore direct parent mask", self)
        form.addRow(self.ignore_parent_mask)
        self.underlay = QSlider(Qt.Orientation.Horizontal, self)
        self.underlay.setRange(0, 100)
        self.underlay_value = QLabel("0%", self)
        underlay_row = QWidget(self)
        underlay_layout = QHBoxLayout(underlay_row)
        underlay_layout.setContentsMargins(0, 0, 0, 0)
        underlay_layout.addWidget(self.underlay, 1)
        underlay_layout.addWidget(self.underlay_value)
        form.addRow("Show underlay", underlay_row)
        self.geometry_reference = QComboBox(self)
        self.geometry_reference.addItem("Direct parent", "direct")
        self.geometry_reference.addItem("Closest compound", "compound")
        form.addRow("Shape reference", self.geometry_reference)
        self.placement_mode = QComboBox(self)
        self.placement_mode.addItem("Free transform", "free")
        self.placement_mode.addItem("Fit to parent", "fit_parent")
        form.addRow("Placement", self.placement_mode)
        self.fit_mode = QComboBox(self)
        self.fit_mode.addItem("Auto width", "auto_width")
        self.fit_mode.addItem("Auto height", "auto_height")
        self.fit_mode.addItem("Stretch", "stretch")
        self.fit_mode.addItem("Fit inside", "fit_inside")
        form.addRow("Fit mode", self.fit_mode)
        self.filename = QLabel("â€”", self)
        self.filename.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Original", self.filename)
        self.dimensions = QLabel("â€”", self)
        form.addRow("Dimensions", self.dimensions)
        self.name.editingFinished.connect(self._apply)
        self.opacity_lock.toggled.connect(self._apply)
        self.ignore_parent_mask.toggled.connect(self._apply)
        self.geometry_reference.currentIndexChanged.connect(self._apply)
        self.placement_mode.currentIndexChanged.connect(self._apply)
        self.fit_mode.currentIndexChanged.connect(self._apply)
        self.underlay.sliderPressed.connect(self._begin_underlay)
        self.underlay.valueChanged.connect(self._underlay_changed)
        self.underlay.sliderReleased.connect(self._finish_underlay)

    def _selected(self) -> ImageObject | None:
        if self.canvas.chapter is None or self.canvas.selected_kind != "object":
            return None
        obj = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return obj if isinstance(obj, ImageObject) else None

    def refresh(self) -> None:
        obj = self._selected()
        self._updating = True
        if obj is not None:
            self.name.setText(obj.name)
            self.opacity_lock.setChecked(obj.opacity_locked)
            self.ignore_parent_mask.setChecked(obj.ignore_parent_mask)
            value = round(obj.underlay_opacity * 100)
            self.underlay.setValue(value)
            self.underlay_value.setText(f"{value}%")
            self.geometry_reference.setCurrentIndex(max(
                0, self.geometry_reference.findData(obj.geometry_reference)
            ))
            self.placement_mode.setCurrentIndex(max(
                0, self.placement_mode.findData(obj.placement_mode)
            ))
            self.fit_mode.setCurrentIndex(max(
                0, self.fit_mode.findData(obj.fit_mode)
            ))
            self.fit_mode.setEnabled(obj.placement_mode == "fit_parent")
            self.filename.setText(obj.source_filename)
            self.dimensions.setText(f"{obj.pixel_width} × {obj.pixel_height} px")
        self._updating = False

    def _apply(self, *args) -> None:
        del args
        obj = self._selected()
        if self._updating or obj is None:
            return
        before = self.canvas.chapter.to_dict()
        old_placement = obj.placement_mode
        obj.name = self.name.text().strip() or obj.name
        obj.opacity_locked = self.opacity_lock.isChecked()
        obj.ignore_parent_mask = self.ignore_parent_mask.isChecked()
        reference = self.geometry_reference.currentData()
        obj.geometry_reference = str(reference or "direct")
        placement = str(self.placement_mode.currentData() or "free")
        if old_placement == "fit_parent" and placement == "free":
            obj.transform_quad = self.canvas._image_fit_quad(obj)
            obj.transform_frame = (
                0.0, 0.0, float(obj.pixel_width), float(obj.pixel_height)
            )
        obj.placement_mode = placement
        obj.fit_mode = str(self.fit_mode.currentData() or "auto_height")
        self._push(before, "Edit image object")

    def _begin_underlay(self) -> None:
        if not self._updating and self._selected() is not None:
            self._underlay_before = self.canvas.chapter.to_dict()

    def _underlay_changed(self, value: int) -> None:
        self.underlay_value.setText(f"{value}%")
        obj = self._selected()
        if self._updating or obj is None:
            return
        before = None if self._underlay_before is not None else self.canvas.chapter.to_dict()
        obj.underlay_opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            self._push(before, "Change image underlay")

    def _finish_underlay(self) -> None:
        before, self._underlay_before = self._underlay_before, None
        if before is not None:
            self._push(before, "Change image underlay")

    def _push(self, before: dict, label: str) -> None:
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, label)
            self.canvas.hierarchyChanged.emit()
            self.canvas.documentChanged.emit(None)
            self.changed.emit()
        self.canvas.update()
        self.refresh()


class SelectionSettingsPanel(QWidget):
    """Stacked layer/raster/vector property pages for the outliner."""

    changed = Signal()
    settingsChanged = Signal()

    def __init__(
        self, canvas, settings, save_settings_callback,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack)
        self.layer_page = LayerSettingsPanel(
            canvas, settings, save_settings_callback, self,
            show_common=False,
        )
        self.stack.addWidget(self.layer_page)

        self.raster_controls = RasterObjectControls(
            canvas, settings, self
        )
        self.raster_page = QGroupBox("Raster Object Settings", self)
        raster_layout = QVBoxLayout(self.raster_page)
        raster_layout.addWidget(self.raster_controls.object_widget)
        raster_layout.addWidget(self.raster_controls.transform_widget)
        raster_layout.addStretch(1)
        self.raster_controls.visible.hide()
        opacity_row = self.raster_controls.opacity.parentWidget()
        opacity_row.hide()
        raster_form = self.raster_controls.object_widget.layout()
        opacity_label = raster_form.labelForField(opacity_row)
        if opacity_label is not None:
            opacity_label.hide()
        self.stack.addWidget(self.raster_page)

        self.vector_page = QGroupBox("Vector Object Settings", self)
        vector_layout = QVBoxLayout(self.vector_page)
        self.vector_controls = VectorObjectSettings(canvas, self.vector_page)
        vector_layout.addWidget(self.vector_controls)
        vector_layout.addStretch(1)
        self.stack.addWidget(self.vector_page)

        self.image_page = QGroupBox("Image Object Settings", self)
        image_layout = QVBoxLayout(self.image_page)
        self.image_controls = ImageObjectSettings(canvas, self.image_page)
        image_layout.addWidget(self.image_controls)
        image_layout.addStretch(1)
        self.stack.addWidget(self.image_page)

        self.raster_controls.objectChanged.connect(self.changed)
        self.raster_controls.settingsChanged.connect(self.settingsChanged)
        self.vector_controls.changed.connect(self.changed)
        self.image_controls.changed.connect(self.changed)
        self.refresh()

    def refresh(self) -> None:
        chapter = self.layer_page.canvas.chapter
        target = (
            chapter.objects.get(self.layer_page.canvas.selected_id)
            if chapter is not None
            and self.layer_page.canvas.selected_kind == "object"
            else None
        )
        if isinstance(target, RasterObject):
            self.raster_controls.refresh()
            self.stack.setCurrentWidget(self.raster_page)
        elif isinstance(target, ImageObject):
            self.image_controls.refresh()
            self.stack.setCurrentWidget(self.image_page)
        elif isinstance(target, (VectorDrawingObject, VectorFillObject)):
            self.vector_controls.refresh()
            self.stack.setCurrentWidget(self.vector_page)
        else:
            self.layer_page.setTitle(
                "Parent Layer Settings" if target is not None
                else "Layer Settings"
            )
            self.layer_page.refresh()
            self.stack.setCurrentWidget(self.layer_page)
