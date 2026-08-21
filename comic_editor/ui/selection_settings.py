"""Selection-scoped controls hosted above the layer/object outliner."""
from __future__ import annotations

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPushButton, QSlider, QStackedWidget, QToolButton,
    QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    ImageObject, RasterObject, TextObject, VectorDrawingObject,
)
from comic_editor.ui.layer_settings import LayerSettingsPanel
from comic_editor.ui.tool_ribbon_pages import RasterObjectControls
from comic_editor.ui.icons import iconoir
from comic_editor.ui.mask_controls import DualEndpointSlider, MaskButton


class SelectionCommonControls(QWidget):
    """Pinned visibility and opacity controls for the exact selection."""

    changed = Signal()
    maskRequested = Signal(object)
    maskPreviewRequested = Signal(str, bool)
    maskContributorsDropped = Signal(object, object)
    maskDetachRequested = Signal(object)

    def __init__(self, canvas, parent: QWidget | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self._updating = False
        self._opacity_before: dict | None = None
        self._opacity_start_value = 100
        layout = QHBoxLayout(self)
        layout.setContentsMargins(4, 2, 4, 2)
        layout.setSpacing(5)
        self.trash_button = QToolButton(self)
        self.trash_button.setAutoRaise(True)
        self.trash_button.setFixedSize(30, 28)
        self.trash_button.setAccessibleName("Delete")
        self.trash_button.setToolTip("Delete selected")
        self.trash_button.setIcon(iconoir("trash"))
        self.visible_button = QToolButton(self)
        self.visible_button.setCheckable(True)
        self.visible_button.setAutoRaise(True)
        self.visible_button.setFixedSize(30, 28)
        self.visible_button.setAccessibleName("Visibility")
        self.visible_button.setToolTip("Visible (click to hide)")
        self.visible = self.visible_button
        self.opacity_lock = QToolButton(self)
        self.opacity_lock.setCheckable(True)
        self.opacity_lock.setAutoRaise(True)
        self.opacity_lock.setFixedSize(30, 28)
        self.opacity_lock.setAccessibleName("Lock opacity")
        self.opacity = QSlider(Qt.Orientation.Horizontal, self)
        self.opacity.setRange(0, 100)
        self.opacity_mask_button = MaskButton(self)
        self.opacity_endpoints = DualEndpointSlider(0, 100, 0, 100, self)
        self.opacity_endpoints.hide()
        self.opacity_value = QLabel("100%", self)
        self.opacity_value.setMinimumWidth(38)
        self.opacity_value.setAlignment(
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter
        )
        self.mask_only_button = QToolButton(self)
        self.mask_only_button.setCheckable(True)
        self.mask_only_button.setAutoRaise(True)
        self.mask_only_button.setFixedSize(30, 28)
        self.mask_only_button.setAccessibleName("Show as mask only")
        self.mask_only_button.setToolTip("Show as mask only")
        layout.addWidget(self.trash_button)
        layout.addWidget(self.visible_button)
        layout.addWidget(self.opacity_lock)
        layout.addWidget(QLabel("Opacity", self))
        layout.addWidget(self.opacity_mask_button)
        layout.addWidget(self.opacity, 1)
        layout.addWidget(self.opacity_endpoints, 1)
        layout.addWidget(self.opacity_value)
        layout.addWidget(self.mask_only_button)
        self.trash_button.clicked.connect(self._delete_selected)
        self.visible_button.toggled.connect(self._visibility_changed)
        self.opacity_lock.toggled.connect(self._opacity_lock_changed)
        self.mask_only_button.toggled.connect(self._mask_only_changed)
        self.opacity.sliderPressed.connect(self._begin_opacity_drag)
        self.opacity.valueChanged.connect(self._opacity_changed)
        self.opacity.sliderReleased.connect(self._finish_opacity_drag)
        self.opacity_mask_button.clicked.connect(self._request_mask)
        self.opacity_mask_button.hoverChanged.connect(self._preview_mask)
        self.opacity_mask_button.entitiesDropped.connect(
            self._drop_mask_contributors
        )
        self.opacity_mask_button.detachRequested.connect(
            lambda: self.maskDetachRequested.emit(self._mask_context())
        )
        self.opacity_endpoints.valuesChanging.connect(
            lambda black, white: self._set_mask_endpoints(
                black, white, False
            )
        )
        self.opacity_endpoints.valuesCommitted.connect(
            lambda black, white: self._set_mask_endpoints(
                black, white, True
            )
        )
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

    def _selected_entities(self):
        chapter = self.canvas.chapter
        if chapter is None:
            return []
        entities = getattr(self.canvas, "selected_entities", [])
        if len(entities) > 1:
            result = []
            for kind, eid in entities:
                ent = chapter.objects.get(eid) if kind == "object" else chapter.layers.get(eid)
                if ent is not None:
                    result.append((kind, eid, ent))
            if result:
                return result
        target = self._selected()
        if target is not None:
            return [(self.canvas.selected_kind, self.canvas.selected_id, target)]
        return []

    def refresh(self) -> None:
        target = self._selected()
        self._updating = True
        enabled = target is not None
        all_entities = self._selected_entities()
        multi = len(all_entities) > 1
        self.trash_button.setEnabled(enabled)
        self.trash_button.setVisible(enabled)
        if enabled and multi:
            self.trash_button.setToolTip(f"Delete {len(all_entities)} selected objects")
        elif enabled:
            is_page = bool(getattr(target, "is_page", False))
            self.trash_button.setEnabled(not is_page)
            self.trash_button.setToolTip("Delete selected")
        self.visible.setEnabled(enabled)
        object_target = (
            enabled
            and self.canvas.selected_kind == "object"
            and hasattr(target, "opacity_locked")
        )
        self.opacity_lock.setVisible(object_target)
        self.opacity_lock.setEnabled(object_target)
        self.opacity.setEnabled(enabled)
        binding = getattr(target, "opacity_mask", None) if target else None
        maskable = (
            enabled
            and not bool(getattr(target, "is_page", False))
        )
        self.opacity_mask_button.setVisible(maskable)
        self.opacity_mask_button.setChecked(binding is not None)
        self.opacity.setVisible(binding is None)
        self.opacity_endpoints.setVisible(binding is not None)
        if binding is not None:
            self.opacity_endpoints.setValues(
                binding.black_value * 100.0,
                binding.white_value * 100.0,
            )
        self.mask_only_button.setVisible(maskable)
        self.mask_only_button.setEnabled(maskable)
        if target is not None:
            if multi:
                visibles = [bool(e.visible) for _, _, e in all_entities]
                all_visible = all(visibles)
                all_hidden = not any(visibles)
                mixed = not all_visible and not all_hidden
                self.visible.setChecked(all_visible)
                self._update_visibility_button(all_visible, mixed=mixed, count=len(all_entities))
                mask_only_vals = [bool(getattr(e, "mask_only", False)) for _, _, e in all_entities]
                all_mask = all(mask_only_vals)
                mixed_mask = not all_mask and any(mask_only_vals)
                self.mask_only_button.setChecked(all_mask)
                self._update_mask_only_button(all_mask, mixed=mixed_mask)
            else:
                self.visible.setChecked(bool(target.visible))
                self._update_visibility_button(bool(target.visible))
                self._update_mask_only_button(bool(getattr(target, "mask_only", False)))
            self.opacity_lock.setChecked(
                bool(getattr(target, "opacity_locked", False))
            )
            self._update_lock_button(bool(getattr(target, "opacity_locked", False)))
            if self.canvas.selected_kind == "object":
                try:
                    opacity = self.canvas.chapter.effective_object_opacity(
                        target.object_id
                    )
                except (AttributeError, KeyError):
                    opacity = float(getattr(target, "opacity", 1.0))
            else:
                opacity = float(getattr(target, "opacity", 1.0))
            value = max(0, min(100, round(opacity * 100)))
            self.opacity.setValue(value)
            self.opacity_value.setText(f"{value}%")
            self._opacity_start_value = value
        else:
            self.visible.setChecked(False)
            self._update_visibility_button(False)
            self.opacity_lock.setChecked(False)
            self._update_lock_button(False)
            self.mask_only_button.setChecked(False)
            self._update_mask_only_button(False)
            self.opacity.setValue(100)
            self._opacity_start_value = 100
            self.opacity_value.setText("—")
        self._updating = False

    def _mask_context(self):
        target = self._selected()
        if (
            target is None or bool(getattr(target, "is_page", False))
        ):
            return None
        if self.canvas.selected_kind == "object":
            try:
                current = self.canvas.chapter.effective_object_opacity(
                    target.object_id
                ) * 100.0
            except (AttributeError, KeyError):
                current = float(getattr(target, "opacity", 1.0)) * 100.0
        else:
            current = float(getattr(target, "opacity", 1.0)) * 100.0
        return (
            "opacity", self.canvas.selected_kind, self.canvas.selected_id,
            0.0, 100.0, 0.0, current,
        )

    def _request_mask(self) -> None:
        context = self._mask_context()
        if context is not None:
            self.maskRequested.emit(context)

    def _preview_mask(self, hovered: bool) -> None:
        target = self._selected()
        binding = getattr(target, "opacity_mask", None) if target else None
        self.maskPreviewRequested.emit(
            binding.mask_id if binding is not None else "", bool(hovered)
        )

    def _drop_mask_contributors(self, entities) -> None:
        context = self._mask_context()
        if context is not None:
            self.maskContributorsDropped.emit(context, entities)

    def _set_mask_endpoints(
        self, black: float, white: float, commit: bool,
    ) -> None:
        target = self._selected()
        binding = getattr(target, "opacity_mask", None) if target else None
        if target is None or binding is None or self._updating:
            return
        if self._opacity_before is None:
            self._opacity_before = self.canvas.chapter.to_dict()
        binding.black_value = float(black) / 100.0
        binding.white_value = float(white) / 100.0
        target.opacity = binding.white_value
        if hasattr(target, "opacity_locked"):
            target.opacity_locked = False
        self.canvas._invalidate_scene_cache()
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if commit:
            self._finish_opacity_drag()

    def _update_visibility_button(self, visible: bool, mixed: bool = False, count: int | None = None) -> None:
        if mixed:
            self.visible_button.setIcon(iconoir("eye"))
            self.visible_button.setToolTip(f"Mixed visibility ({count} selected) — click to show all" if count else "Mixed visibility — click to show all")
            return
        self.visible_button.setIcon(iconoir("eye" if visible else "eye-closed"))
        if count is not None and count > 1:
            self.visible_button.setToolTip(
                f"Visible — click to hide all {count}" if visible else f"Hidden — click to show all {count}"
            )
        else:
            self.visible_button.setToolTip(
                "Visible (click to hide)" if visible
                else "Hidden (click to show)"
            )

    def _update_lock_button(self, locked: bool) -> None:
        self.opacity_lock.setIcon(iconoir("lock" if locked else "unlock"))
        self.opacity_lock.setToolTip("Opacity locked — click to unlock" if locked else "Opacity unlocked — click to lock")

    def _update_mask_only_button(self, mask_only: bool, mixed: bool = False) -> None:
        self.mask_only_button.setIcon(iconoir("mask-square"))
        if mixed:
            self.mask_only_button.setToolTip("Mixed mask only — click to enable for all")
        else:
            self.mask_only_button.setToolTip("Shown as mask only — click to show normally" if mask_only else "Show as mask only — click to hide visually and show only in masks")

    def _delete_selected(self) -> None:
        chapter = self.canvas.chapter
        if chapter is None or not self.canvas.selected_id:
            return
        entities = self._selected_entities()
        if not entities:
            return
        deletable = []
        for kind, eid, ent in entities:
            if bool(getattr(ent, "is_page", False)):
                continue
            deletable.append((kind, eid))
        if not deletable:
            return
        if len(deletable) == 1:
            msg = "Delete the selected entity and all of its descendants?"
        else:
            msg = f"Delete {len(deletable)} selected objects and their descendants?"
        if QMessageBox.question(self, "Delete selection", msg) != QMessageBox.Yes:
            return
        before = chapter.to_dict()
        for kind, eid in deletable:
            if kind == "object" and eid not in chapter.objects:
                continue
            if kind == "layer" and eid not in chapter.layers:
                continue
            try:
                chapter.delete_entity(kind, eid)
            except Exception:
                continue
        after = chapter.to_dict()
        self.canvas.clear_selection()
        self.canvas.push_model_change(before, after, "Delete entities" if len(deletable) > 1 else "Delete entity")
        self.canvas.hierarchyChanged.emit()
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        self.changed.emit()

    def _mask_only_changed(self, checked: bool) -> None:
        if self._updating:
            return
        entities = self._selected_entities()
        if not entities:
            return
        before = self.canvas.chapter.to_dict()
        for _, _, ent in entities:
            if bool(getattr(ent, "is_page", False)):
                continue
            ent.mask_only = bool(checked)
        self._update_mask_only_button(bool(checked))
        self._push(before, "Change mask only", hierarchy=True)

    def _visibility_changed(self, checked: bool) -> None:
        if self._updating:
            return
        entities = self._selected_entities()
        if not entities:
            return
        target = self._selected()
        if target is not None:
            self._commit_text(target)
            entities = self._selected_entities()
            if not entities:
                return
        before = self.canvas.chapter.to_dict()
        for _, _, ent in entities:
            ent.visible = bool(checked)
        self._update_visibility_button(bool(checked), count=len(entities) if len(entities) > 1 else None)
        self._push(before, "Change visibility", hierarchy=True)

    def _opacity_lock_changed(self, checked: bool) -> None:
        target = self._selected()
        if (
            self._updating
            or target is None
            or self.canvas.selected_kind != "object"
            or not hasattr(target, "opacity_locked")
        ):
            return
        self._commit_text(target)
        target = self._selected()
        if target is None or not hasattr(target, "opacity_locked"):
            return
        before = self.canvas.chapter.to_dict()
        target.opacity_locked = bool(checked)
        self._update_lock_button(bool(checked))
        if target.opacity_locked:
            parent = self.canvas.chapter.layers.get(target.parent_layer_id)
            if parent is not None:
                target.opacity = float(parent.opacity)
        self._push(before, "Change opacity lock")

    def _begin_opacity_drag(self) -> None:
        target = self._selected()
        if self._updating or target is None:
            return
        self._commit_text(target)
        target = self._selected()
        if target is None:
            return
        self._opacity_before = self.canvas.chapter.to_dict()
        self._opacity_start_value = int(self.opacity.value())

    def _opacity_changed(self, value: int) -> None:
        self.opacity_value.setText(f"{int(value)}%")
        target = self._selected()
        if self._updating or target is None:
            return
        if int(value) == int(self._opacity_start_value):
            return
        if self._opacity_before is None:
            self._commit_text(target)
            target = self._selected()
            if target is None:
                return
            before = self.canvas.chapter.to_dict()
            self._opacity_before = before
        else:
            before = None
        if bool(getattr(target, "opacity_locked", False)):
            target.opacity = self._opacity_start_value / 100.0
            target.opacity_locked = False
        if self.canvas.selected_kind == "layer":
            self.canvas.chapter.set_layer_opacity(
                target.layer_id, value / 100.0
            )
        else:
            target.opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None and self._opacity_before is None:
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
        # Opacity inheritance is edited only by the pinned common row.  Keep
        # this compatibility widget hidden for integrations that still hold a
        # reference to the old page control.
        self.opacity_lock.hide()
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
        return target if isinstance(target, VectorDrawingObject) else None

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
    renderOnceRequested = Signal()
    reconnectRequested = Signal()
    relinkRequested = Signal()
    detachRequested = Signal()

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
        self.opacity_lock.hide()
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
        self.source_status = QLabel("", self)
        self.source_status.setWordWrap(True)
        form.addRow("Source status", self.source_status)
        self.source_actions = QWidget(self)
        source_layout = QHBoxLayout(self.source_actions)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.render_once = QPushButton("Render Once", self.source_actions)
        self.reconnect = QPushButton("Reconnect", self.source_actions)
        self.relink = QPushButton("Relink", self.source_actions)
        self.detach = QPushButton("Detach", self.source_actions)
        source_layout.addWidget(self.render_once)
        source_layout.addWidget(self.reconnect)
        source_layout.addWidget(self.relink)
        source_layout.addWidget(self.detach)
        form.addRow(self.source_actions)
        self.name.editingFinished.connect(self._apply)
        self.ignore_parent_mask.toggled.connect(self._apply)
        self.geometry_reference.currentIndexChanged.connect(self._apply)
        self.placement_mode.currentIndexChanged.connect(self._apply)
        self.fit_mode.currentIndexChanged.connect(self._apply)
        self.underlay.sliderPressed.connect(self._begin_underlay)
        self.underlay.valueChanged.connect(self._underlay_changed)
        self.underlay.sliderReleased.connect(self._finish_underlay)
        self.render_once.clicked.connect(self.renderOnceRequested)
        self.reconnect.clicked.connect(self.reconnectRequested)
        self.relink.clicked.connect(self.relinkRequested)
        self.detach.clicked.connect(self.detachRequested)

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
            self.filename.setText(
                f"Blender: {obj.source.display_name}"
                if obj.is_blender_linked else obj.source_filename
            )
            self.dimensions.setText(f"{obj.pixel_width} × {obj.pixel_height} px")
            self.source_actions.setVisible(obj.is_blender_linked)
            self.source_status.setVisible(obj.is_blender_linked)
            self.detach.setEnabled(
                not obj.is_blender_linked
                or self.canvas.images.source(obj.object_id) is not None
                or not self.canvas.images.runtime_frame(obj.object_id).isNull()
            )
            if obj.is_blender_linked and not self.source_status.text():
                self.source_status.setText("Offline — showing the last cached frame")
        else:
            self.source_actions.hide()
            self.source_status.hide()
        self._updating = False

    def set_source_status(self, status: str) -> None:
        self.source_status.setText(str(status))

    def _apply(self, *args) -> None:
        del args
        obj = self._selected()
        if self._updating or obj is None:
            return
        before = self.canvas.chapter.to_dict()
        old_placement = obj.placement_mode
        obj.name = self.name.text().strip() or obj.name
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
        elif isinstance(target, VectorDrawingObject):
            self.vector_controls.refresh()
            self.stack.setCurrentWidget(self.vector_page)
        else:
            self.layer_page.setTitle(
                "Parent Layer Settings" if target is not None
                else "Layer Settings"
            )
            self.layer_page.refresh()
            self.stack.setCurrentWidget(self.layer_page)
