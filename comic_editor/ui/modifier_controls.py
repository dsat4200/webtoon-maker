"""Contextual nondestructive modifier stack controls."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QColorDialog, QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
    OutlineModifier,
    canonical_argb,
)
from comic_editor.ui.icons import iconoir
from comic_editor.ui.mask_controls import DualEndpointSlider, MaskButton


class ModifierTitleBar(QFrame):
    activated = Signal(str)
    dragStarted = Signal(str)
    dragMoved = Signal(str, int)
    dragFinished = Signal(str)

    def __init__(self, modifier_id: str, title: str, parent=None):
        super().__init__(parent)
        self.modifier_id = modifier_id
        self._press = None
        self.setObjectName("modifierTitleBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(5, 3, 3, 3)
        self.label = QLabel(title, self)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.label, 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = QPoint(event.position().toPoint())
            self.activated.emit(self.modifier_id)
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self._press).manhattanLength() >= 4:
                self.dragStarted.emit(self.modifier_id)
                self.dragMoved.emit(
                    self.modifier_id, int(event.globalPosition().y())
                )
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is not None:
            self._press = None
            self.dragFinished.emit(self.modifier_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class ModifierCard(QFrame):
    removeRequested = Signal(str)
    linkRequested = Signal(str)
    activated = Signal(str)
    dragStarted = Signal(str)
    dragMoved = Signal(str, int)
    dragFinished = Signal(str)

    def __init__(self, modifier: ModifierInstance, owner, parent=None):
        super().__init__(parent)
        self.modifier = modifier
        self.owner = owner
        self.setObjectName("modifierCard")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(1, 1, 1, 5)
        outer.setSpacing(4)
        title = ModifierTitleBar(
            modifier.modifier_id, modifier.name, self
        )
        title.activated.connect(self.activated)
        title.dragStarted.connect(self.dragStarted)
        title.dragMoved.connect(self.dragMoved)
        title.dragFinished.connect(self.dragFinished)
        collapse = self._title_button(
            "nav-arrow-down" if modifier.expanded else "nav-arrow-right",
            "Collapse modifier" if modifier.expanded else "Expand modifier",
            lambda: owner.set_parameter(
                modifier.modifier_id, "expanded", not modifier.expanded, True
            ),
        )
        title.layout().insertWidget(0, collapse)
        self.link_button = self._title_button(
            "link-square", "Edit linked targets",
            lambda: self.linkRequested.emit(modifier.modifier_id),
        )
        self.link_button.setCheckable(True)
        self.link_button.setChecked(
            owner.link_modifier_id == modifier.modifier_id
        )
        title.layout().addWidget(self.link_button)
        title.layout().addWidget(self._title_button(
            "trash", "Remove modifier",
            lambda: self.removeRequested.emit(modifier.modifier_id),
        ))
        outer.addWidget(title)
        body = QWidget(self)
        form = QVBoxLayout(body)
        form.setContentsMargins(6, 0, 6, 0)
        form.setSpacing(4)
        form.addWidget(self._slider_row(
            "Intensity", 0, 100, round(modifier.intensity),
            "intensity", "%",
        ))
        if isinstance(modifier, HueSaturationLightnessModifier):
            form.addWidget(self._slider_row(
                "Hue", -180, 180, round(modifier.hue), "hue", "°"
            ))
            form.addWidget(self._slider_row(
                "Saturation", -100, 100, round(modifier.saturation),
                "saturation", "%",
            ))
            form.addWidget(self._slider_row(
                "Lightness", -100, 100, round(modifier.lightness),
                "lightness", "%",
            ))
        elif isinstance(modifier, BlurModifier):
            form.addWidget(self._slider_row(
                "Strength", 0, 100, round(modifier.strength),
                "strength", " px",
            ))
            mode_row = QWidget(body)
            mode_layout = QHBoxLayout(mode_row)
            mode_layout.setContentsMargins(0, 0, 0, 0)
            mode_layout.addWidget(QLabel("Mode", mode_row))
            mode = QComboBox(mode_row)
            mode.addItem("Full", "full")
            mode.addItem("Focal Point", "focal")
            mode.setCurrentIndex(max(0, mode.findData(modifier.mode)))
            mode.currentIndexChanged.connect(
                lambda _index, control=mode: owner.set_parameter(
                    modifier.modifier_id, "mode", control.currentData(), True
                )
            )
            mode_layout.addWidget(mode, 1)
            form.addWidget(mode_row)
        elif isinstance(modifier, OutlineModifier):
            form.addWidget(self._slider_row(
                "Thickness", 0, 100, round(modifier.thickness),
                "thickness", " px",
            ))
            form.addWidget(self._slider_row(
                "Opacity", 0, 100, round(modifier.opacity),
                "opacity", "%",
            ))
            color_row = QWidget(body)
            color_layout = QHBoxLayout(color_row)
            color_layout.setContentsMargins(0, 0, 0, 0)
            color_layout.addWidget(QLabel("Color", color_row))
            color = QPushButton(modifier.color, color_row)
            color.setStyleSheet(
                f"QPushButton {{ background: {QColor(modifier.color).name()}; }}"
            )
            color.clicked.connect(lambda: self._choose_color(color))
            color_layout.addWidget(color, 1)
            form.addWidget(color_row)
        outer.addWidget(body)
        body.setVisible(modifier.expanded)

    def _title_button(self, icon, tooltip, callback):
        button = QToolButton(self)
        button.setIcon(iconoir(icon))
        button.setToolTip(tooltip)
        button.setAutoRaise(True)
        button.clicked.connect(callback)
        return button

    def _slider_row(
        self, label: str, minimum: int, maximum: int, value: int,
        attribute: str, suffix: str,
    ) -> QWidget:
        row = QWidget(self)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel(label, row))
        binding = self.modifier.parameter_masks.get(attribute)
        mask_button = MaskButton(row)
        mask_button.setChecked(binding is not None)
        context = (
            "modifier", self.modifier.modifier_id, attribute,
            float(minimum), float(maximum), 0.0, float(value),
        )
        mask_button.clicked.connect(
            lambda _checked=False, value=context:
            self.owner.request_mask(value)
        )
        mask_button.hoverChanged.connect(
            lambda hovered, value=context:
            self.owner.preview_mask(value, hovered)
        )
        mask_button.entitiesDropped.connect(
            lambda entities, value=context:
            self.owner.drop_mask_contributors(value, entities)
        )
        mask_button.detachRequested.connect(
            lambda value=context: self.owner.detach_mask(value)
        )
        layout.addWidget(mask_button)
        if binding is not None:
            endpoints = DualEndpointSlider(
                minimum, maximum,
                binding.black_value, binding.white_value, row,
            )
            endpoints.valuesChanging.connect(
                lambda black, white: self.owner.set_mask_endpoints(
                    self.modifier.modifier_id, attribute,
                    black, white, False,
                )
            )
            endpoints.valuesCommitted.connect(
                lambda black, white: self.owner.set_mask_endpoints(
                    self.modifier.modifier_id, attribute,
                    black, white, True,
                )
            )
            layout.addWidget(endpoints, 1)
            return row
        slider = QSlider(Qt.Orientation.Horizontal, row)
        slider.setRange(minimum, maximum)
        slider.setValue(value)
        value_box = QSpinBox(row)
        value_box.setRange(minimum, maximum)
        value_box.setSuffix(suffix)
        value_box.setValue(value)
        slider.valueChanged.connect(value_box.setValue)
        value_box.valueChanged.connect(slider.setValue)
        slider.sliderPressed.connect(self.owner.begin_parameter_drag)
        slider.valueChanged.connect(
            lambda current: self.owner.set_parameter(
                self.modifier.modifier_id, attribute, float(current), False
            )
        )
        slider.sliderReleased.connect(self.owner.finish_parameter_drag)
        value_box.editingFinished.connect(
            lambda: self.owner.set_parameter(
                self.modifier.modifier_id, attribute,
                float(value_box.value()), True,
            )
        )
        layout.addWidget(slider, 1)
        layout.addWidget(value_box)
        return row

    def _choose_color(self, button: QPushButton) -> None:
        color = QColorDialog.getColor(
            QColor(self.modifier.color), self, "Outline Color",
            QColorDialog.ColorDialogOption.ShowAlphaChannel,
        )
        if color.isValid():
            value = canonical_argb(color.name(QColor.NameFormat.HexArgb))
            button.setText(value)
            button.setStyleSheet(
                f"QPushButton {{ background: {QColor(value).name()}; }}"
            )
            self.owner.set_parameter(
                self.modifier.modifier_id, "color", value,
                True,
            )


class ModifierControls(QWidget):
    linkModeChanged = Signal(object)
    maskRequested = Signal(object)
    maskPreviewRequested = Signal(str, bool)
    maskContributorsDropped = Signal(object, object)
    maskDetachRequested = Signal(object)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.active_modifier_id = ""
        self.link_modifier_id = ""
        self.link_original: set[tuple[str, str]] = set()
        self.link_working: set[tuple[str, str]] = set()
        self._parameter_before = None
        self._reorder_before = None
        self._cards: dict[str, ModifierCard] = {}
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.summary = QLabel("Select a drawing, image, or shape.", self)
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        self.add_button = QPushButton("Add Modifier", self)
        menu = QMenu(self.add_button)
        menu.addAction("Hue / Saturation / Lightness").triggered.connect(
            lambda: self.add_modifier("hsl")
        )
        menu.addAction("Blur").triggered.connect(
            lambda: self.add_modifier("blur")
        )
        menu.addAction("Outline").triggered.connect(
            lambda: self.add_modifier("outline")
        )
        self.add_button.setMenu(menu)
        layout.addWidget(self.add_button)
        self.stack = QWidget(self)
        self.stack_layout = QVBoxLayout(self.stack)
        self.stack_layout.setContentsMargins(0, 0, 0, 0)
        self.stack_layout.setSpacing(6)
        self.stack_layout.addStretch(1)
        layout.addWidget(self.stack)
        self.canvas.selectionChanged.connect(lambda *_: self.refresh())
        self.canvas.selectionSetChanged.connect(lambda *_: self.refresh())
        self.canvas.chapterReplaced.connect(lambda *_: self.refresh())

    def targets(self) -> list[tuple[str, str]]:
        if self.canvas.chapter is None:
            return []
        return [
            target for target in self.canvas.selected_entities
            if self.canvas.chapter.modifier_target(*target) is not None
        ]

    def common_ids(self) -> list[str]:
        chapter = self.canvas.chapter
        targets = self.targets()
        if chapter is None or not targets:
            return []
        primary = chapter.modifier_target(*targets[-1])
        common = set(primary.modifier_ids)
        for target_ref in targets[:-1]:
            common.intersection_update(
                chapter.modifier_target(*target_ref).modifier_ids
            )
        return [item for item in primary.modifier_ids if item in common]

    def refresh(self) -> None:
        while self.stack_layout.count() > 1:
            item = self.stack_layout.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._cards.clear()
        chapter = self.canvas.chapter
        targets = self.targets()
        eligible = bool(targets) and len(targets) == len(
            self.canvas.selected_entities
        )
        self.add_button.setEnabled(eligible)
        if not eligible:
            self.summary.setText("Select a drawing, image, or bounded shape.")
            return
        ids = self.common_ids()
        self.summary.setText(
            "Shared modifiers" if len(targets) > 1 else "Modifier stack"
        )
        for modifier_id in ids:
            modifier = chapter.modifiers.get(modifier_id)
            if modifier is None:
                continue
            card = ModifierCard(modifier, self, self.stack)
            card.removeRequested.connect(self.remove_modifier)
            card.linkRequested.connect(self.toggle_link_mode)
            card.activated.connect(self.activate_modifier)
            card.dragStarted.connect(self.begin_reorder)
            card.dragMoved.connect(self.move_reorder)
            card.dragFinished.connect(self.finish_reorder)
            self._cards[modifier_id] = card
            self.stack_layout.insertWidget(self.stack_layout.count() - 1, card)

    def _changed(self) -> None:
        bounds = self._default_bounds()
        if bounds is None or bounds.isEmpty():
            self.canvas._invalidate_scene_cache()
            self.canvas.update()
            return
        dirty = bounds.adjusted(-320.0, -320.0, 320.0, 320.0)
        self.canvas._queue_visual_dirty(
            dirty, scene=True, notify_preview=False
        )

    def _push(self, before, label: str) -> None:
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, label)
            self.canvas.documentChanged.emit(None)

    def _default_bounds(self):
        rect = None
        for kind, entity_id in self.targets():
            candidate = self.canvas.entity_world_rect(kind, entity_id)
            if candidate is not None:
                rect = candidate if rect is None else rect.united(candidate)
        return rect

    def add_modifier(self, modifier_type: str) -> None:
        chapter = self.canvas.chapter
        targets = self.targets()
        if chapter is None or not targets:
            return
        before = chapter.to_dict()
        if modifier_type == "hsl":
            modifier: ModifierInstance = HueSaturationLightnessModifier()
        elif modifier_type == "blur":
            bounds = self._default_bounds()
            center = bounds.center() if bounds is not None else QPoint()
            modifier = BlurModifier(
                focal_center=(float(center.x()), float(center.y())),
                focal_radius=max(
                    1.0,
                    min(bounds.width(), bounds.height()) / 2
                    if bounds is not None else 100.0,
                ),
            )
        else:
            modifier = OutlineModifier()
        chapter.add_modifier(modifier, targets)
        self.active_modifier_id = modifier.modifier_id
        self.canvas.active_modifier_id = modifier.modifier_id
        self._changed()
        self._push(before, "Add modifier")
        self.refresh()

    def remove_modifier(self, modifier_id: str) -> None:
        chapter = self.canvas.chapter
        if chapter is None or modifier_id not in chapter.modifiers:
            return
        before = chapter.to_dict()
        chapter.remove_modifier(modifier_id)
        if self.active_modifier_id == modifier_id:
            self.activate_modifier("")
        self._changed()
        self._push(before, "Remove modifier")
        self.refresh()

    def activate_modifier(self, modifier_id: str) -> None:
        self.active_modifier_id = modifier_id
        self.canvas.active_modifier_id = modifier_id
        self.canvas.update()

    def begin_parameter_drag(self) -> None:
        if self._parameter_before is None and self.canvas.chapter is not None:
            self._parameter_before = self.canvas.chapter.to_dict()

    def set_parameter(
        self, modifier_id: str, attribute: str, value, commit: bool,
    ) -> None:
        chapter = self.canvas.chapter
        modifier = chapter.modifiers.get(modifier_id) if chapter else None
        if modifier is None or not hasattr(modifier, attribute):
            return
        before = chapter.to_dict() if commit and self._parameter_before is None else None
        setattr(modifier, attribute, value)
        modifier.validate()
        self.activate_modifier(modifier_id)
        self._changed()
        if commit and before is not None:
            self._push(before, "Edit modifier")
        if attribute == "expanded":
            self.refresh()

    def finish_parameter_drag(self) -> None:
        before, self._parameter_before = self._parameter_before, None
        if before is not None:
            self._push(before, "Edit modifier")

    def request_mask(self, context: tuple) -> None:
        self.maskRequested.emit(context)

    def preview_mask(self, context: tuple, hovered: bool) -> None:
        chapter = self.canvas.chapter
        modifier = chapter.modifiers.get(context[1]) if chapter else None
        binding = modifier.parameter_masks.get(context[2]) if modifier else None
        self.maskPreviewRequested.emit(
            binding.mask_id if binding is not None else "", bool(hovered)
        )

    def drop_mask_contributors(
        self, context: tuple, entities: list[tuple[str, str]],
    ) -> None:
        self.maskContributorsDropped.emit(context, entities)

    def detach_mask(self, context: tuple) -> None:
        self.maskDetachRequested.emit(context)

    def set_mask_endpoints(
        self, modifier_id: str, attribute: str,
        black: float, white: float, commit: bool,
    ) -> None:
        chapter = self.canvas.chapter
        modifier = chapter.modifiers.get(modifier_id) if chapter else None
        binding = modifier.parameter_masks.get(attribute) if modifier else None
        if binding is None:
            return
        if self._parameter_before is None:
            self._parameter_before = chapter.to_dict()
        binding.black_value = float(black)
        binding.white_value = float(white)
        modifier.validate()
        self._changed()
        if commit:
            self.finish_parameter_drag()

    def begin_reorder(self, _modifier_id: str) -> None:
        if self._reorder_before is None and self.canvas.chapter is not None:
            self._reorder_before = self.canvas.chapter.to_dict()

    def move_reorder(self, modifier_id: str, global_y: int) -> None:
        ids = self.common_ids()
        if modifier_id not in ids or len(ids) < 2:
            return
        local_y = self.stack.mapFromGlobal(QPoint(0, global_y)).y()
        destination = len(ids) - 1
        for index, candidate_id in enumerate(ids):
            card = self._cards.get(candidate_id)
            if card is not None and local_y < card.geometry().center().y():
                destination = index
                break
        source = ids.index(modifier_id)
        if destination == source:
            return
        ids.insert(destination, ids.pop(source))
        common = set(ids)
        for target_ref in self.targets():
            target = self.canvas.chapter.modifier_target(*target_ref)
            iterator = iter(ids)
            target.modifier_ids = [
                next(iterator) if item in common else item
                for item in target.modifier_ids
            ]
        self._changed()
        for card in self._cards.values():
            self.stack_layout.removeWidget(card)
        for index, candidate_id in enumerate(ids):
            card = self._cards.get(candidate_id)
            if card is not None:
                self.stack_layout.insertWidget(index, card)

    def finish_reorder(self, _modifier_id: str) -> None:
        before, self._reorder_before = self._reorder_before, None
        if before is not None:
            self._push(before, "Reorder modifiers")

    def toggle_link_mode(self, modifier_id: str) -> None:
        if self.link_modifier_id == modifier_id:
            self.commit_link_mode()
            return
        chapter = self.canvas.chapter
        if chapter is None or modifier_id not in chapter.modifiers:
            return
        if self.link_modifier_id:
            self.cancel_link_mode()
        self.link_modifier_id = modifier_id
        self.link_original = set(chapter.modifier_target_ids(modifier_id))
        self.link_working = set(self.link_original)
        self.linkModeChanged.emit(set(self.link_working))
        self.refresh()

    def toggle_link_target(self, kind: str, entity_id: str) -> bool:
        chapter = self.canvas.chapter
        target = (kind, entity_id)
        if not self.link_modifier_id or chapter.modifier_target(*target) is None:
            return False
        if target in self.link_working:
            self.link_working.remove(target)
        else:
            self.link_working.add(target)
        self.linkModeChanged.emit(set(self.link_working))
        return True

    def commit_link_mode(self) -> None:
        chapter = self.canvas.chapter
        if not self.link_modifier_id or chapter is None:
            return
        before = chapter.to_dict()
        chapter.set_modifier_targets(
            self.link_modifier_id, self.link_working
        )
        self.link_modifier_id = ""
        self.link_original.clear()
        self.link_working.clear()
        self.linkModeChanged.emit(None)
        self._changed()
        self._push(before, "Change modifier links")
        self.refresh()

    def cancel_link_mode(self) -> bool:
        if not self.link_modifier_id:
            return False
        self.link_modifier_id = ""
        self.link_original.clear()
        self.link_working.clear()
        self.linkModeChanged.emit(None)
        self.refresh()
        return True
