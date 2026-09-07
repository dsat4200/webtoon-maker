"""Contextual nondestructive modifier stack controls."""
from __future__ import annotations

from PySide6.QtCore import QPoint, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QMenu, QPushButton,
    QSlider, QSpinBox, QToolButton, QVBoxLayout, QWidget, QSizePolicy,
    QInputDialog, QMessageBox,
)

from comic_editor.core.models import (
    BlurModifier, HueSaturationLightnessModifier, ModifierInstance,
    OutlineModifier, MirrorModifier, RadialBlurModifier, RasterObject, LayerNode,
    canonical_argb, TilingModifier,
    CageTransformModifier, PosterizeModifier, PosterizeValueModifier, POSTERIZE_MAX_COLORS,
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
        self._dragged = False
        self.setObjectName("modifierTitleBar")
        row = QHBoxLayout(self)
        row.setContentsMargins(5, 3, 3, 3)
        row.setSpacing(3)
        self.label = QLabel(title, self)
        self.label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.label.setToolTip(title)
        self.label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        row.addWidget(self.label, 1)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton:
            self._press = QPoint(event.position().toPoint())
            self._dragged = False
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._press is not None and event.buttons() & Qt.MouseButton.LeftButton:
            if (event.position().toPoint() - self._press).manhattanLength() >= 4:
                self._dragged = True
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
            if self._dragged:
                self.dragFinished.emit(self.modifier_id)
            else:
                self.activated.emit(self.modifier_id)
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
        selected = owner.canvas.modifier_mode and owner.canvas.active_modifier_id == modifier.modifier_id
        self.setStyleSheet("#modifierCard { border: 2px solid " + ("#65bcff; background-color: #203f59" if selected else "transparent") + "; border-radius: 4px; }")
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
        self.mute_button = self._title_button(
            "eye-closed" if modifier.muted else "eye",
            "Unmute modifier" if modifier.muted else "Mute modifier",
            lambda: owner.set_parameter(
                modifier.modifier_id, "muted", not modifier.muted, True
            ),
        )
        self.mute_button.setCheckable(True)
        self.mute_button.setChecked(modifier.muted)
        title.layout().addWidget(self.mute_button)
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
        if isinstance(modifier, PosterizeModifier):
            from comic_editor.ui.posterize_controls import SimplifyColorsControls
            form.addWidget(SimplifyColorsControls(modifier, owner, body))
            form.addWidget(QLabel("Posterization", body))
        form.addWidget(self._slider_row(
            "Intensity", 0, 100, round(modifier.intensity),
            "intensity", "%",
        ))
        if isinstance(modifier, TilingModifier):
            from comic_editor.ui.tiling_controls import TilingSettingsControls
            form.addWidget(TilingSettingsControls(owner, modifier, body))
        elif isinstance(modifier, CageTransformModifier):
            from comic_editor.ui.cage_controls import CageSettingsControls
            form.addWidget(CageSettingsControls(owner.canvas, body, modifier.modifier_id))
        elif isinstance(modifier, PosterizeModifier):
            from comic_editor.ui.posterize_controls import PosterizeControls
            form.addWidget(PosterizeControls(modifier, owner, body))
        elif isinstance(modifier, HueSaturationLightnessModifier):
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
        elif isinstance(modifier, RadialBlurModifier):
            form.addWidget(self._slider_row("Angle", 0, 360, round(modifier.angle), "angle", "°"))
        elif isinstance(modifier, OutlineModifier):
            form.addWidget(self._slider_row(
                "Thickness", 0, 25, round(modifier.thickness),
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
        if isinstance(modifier, MirrorModifier) and any(
            isinstance(owner.canvas.chapter.modifier_target(*t), LayerNode)
            and owner.canvas.chapter.modifier_target(*t).layer_kind != "text_container"
            for t in owner.targets()
        ):
            operation = QComboBox(body)
            for value in ("ignore", "add", "subtract"):
                operation.addItem(value.title(), value)
            operation.setCurrentIndex(operation.findData(modifier.compound_operation))
            operation.setToolTip("Reflected shape contribution to the nearest compound parent")
            operation.currentIndexChanged.connect(lambda _i: owner.set_parameter(modifier.modifier_id, "compound_operation", operation.currentData(), True))
            form.addWidget(operation)
        if owner.targets() and all(isinstance(owner.canvas.chapter.modifier_target(*t), RasterObject) for t in owner.targets()):
            self.apply_button = QPushButton("Apply", self)
            self.apply_button.setEnabled(not modifier.muted)
            self.apply_button.setToolTip("Bake this modifier and all earlier unmuted modifiers into the selected Raster pixels")
            self.apply_button.clicked.connect(lambda: owner.apply_modifier(modifier.modifier_id))
            outer.addWidget(self.apply_button)

    def mouseReleaseEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.activated.emit(self.modifier.modifier_id)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _title_button(self, icon, tooltip, callback):
        button = QToolButton(self)
        size = 22 if isinstance(self.modifier, PosterizeModifier) else 28
        button.setFixedSize(size, size)
        if isinstance(self.modifier, PosterizeModifier):
            button.setStyleSheet("padding: 2px;")
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
        value_box.setKeyboardTracking(False)
        if isinstance(self.modifier, PosterizeModifier):
            layout.setSpacing(3)
            value_box.setFixedWidth(58)
            mask_button.setFixedSize(22, 22)
        value_box.valueChanged.connect(lambda _value: self.owner.begin_parameter_drag())
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
            self.owner.finish_parameter_drag
        )
        if isinstance(self.modifier, PosterizeModifier):
            layout.addStretch(1)
            layout.addWidget(value_box)
            container = QWidget(self)
            column = QVBoxLayout(container)
            column.setContentsMargins(0, 0, 0, 0)
            column.setSpacing(2)
            column.addWidget(row)
            column.addWidget(slider)
            return container
        layout.addWidget(slider, 1)
        layout.addWidget(value_box)
        return row

    def _choose_color(self, button: QPushButton) -> None:
        from comic_editor.ui.color_picker import choose_color
        def apply(value):
            button.setText(value)
            button.setStyleSheet(
                f"QPushButton {{ background: {QColor(value).name()}; }}"
            )
            self.owner.set_parameter(
                self.modifier.modifier_id, "color", value,
                True,
            )
        self._color_popup = choose_color(self, self.modifier.color, apply, "Outline color")


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
        self.blurs_menu = QMenu("Blurs", menu)
        menu.addMenu(self.blurs_menu)
        blurs = self.blurs_menu
        blurs.addAction("Blur").triggered.connect(
            lambda: self.add_modifier("blur")
        )
        blurs.addAction("Blur Legacy").triggered.connect(lambda: self.add_modifier("blur_legacy"))
        blurs.addAction("Radial Blur").triggered.connect(lambda: self.add_modifier("radial_blur"))
        menu.addAction("Outline").triggered.connect(
            lambda: self.add_modifier("outline")
        )
        menu.addAction("Mirror").triggered.connect(lambda: self.add_modifier("mirror"))
        menu.addAction("Tiling").triggered.connect(lambda: self.add_modifier("tiling"))
        menu.addAction("Posterize…").triggered.connect(lambda: self.add_modifier("posterize"))
        menu.addAction("Posterize Value…").triggered.connect(lambda: self.add_modifier("posterize_value"))
        menu.addAction("Cage Transform").triggered.connect(lambda: self.add_modifier("cage_transform"))
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
        self.canvas.modifierSelectionChanged.connect(self._refresh_selection_style)

    def _refresh_selection_style(self, _identifier=""):
        for identifier, card in self._cards.items():
            selected = self.canvas.modifier_mode and self.canvas.active_modifier_id == identifier
            card.setStyleSheet("#modifierCard { border: 2px solid " +
                ("#65bcff; background-color: #203f59" if selected else "transparent") + "; border-radius: 4px; }")

    def targets(self) -> list[tuple[str, str]]:
        if self.canvas.chapter is None:
            return []
        return [
            target for target in self.canvas.selected_entities
            if self.canvas.chapter.modifier_target(*target) is not None
        ]

    @property
    def active_modifier_id(self):
        return self.canvas.active_modifier_id

    @active_modifier_id.setter
    def active_modifier_id(self, value):
        self.canvas.active_modifier_id = value

    def apply_modifier(self, modifier_id):
        from comic_editor.ui.baking import apply_raster_modifiers
        from PySide6.QtWidgets import QMessageBox
        try:
            apply_raster_modifiers(self.canvas, modifier_id)
        except (ValueError, MemoryError, OSError) as error:
            QMessageBox.warning(self, "Apply modifier", str(error))
        self.refresh()

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
        self.add_button.setEnabled(bool(self.canvas.selected_entities))
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
            card.activated.connect(self.toggle_modifier)
            card.dragStarted.connect(self.begin_reorder)
            card.dragMoved.connect(self.move_reorder)
            card.dragFinished.connect(self.finish_reorder)
            self._cards[modifier_id] = card
            self.stack_layout.insertWidget(self.stack_layout.count() - 1, card)

    def _changed(self) -> None:
        # Shared effects can extend far beyond the selected target.
        self.canvas._compound_path_cache.clear()
        self.canvas._invalidate_scene_cache()
        self.canvas.update()
        self.canvas.documentChanged.emit(None)

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
        if self.canvas._cage_edit_before is not None:
            self.canvas.finish_cage(True)
        targets = list(self.canvas.selected_entities)
        if chapter is None or not targets:
            return
        before = chapter.to_dict()
        if modifier_type == "tiling":
            bounds = self.canvas._tiling_default_bounds(targets)
            side = max(1., min(256., min(bounds.width(), bounds.height())/2))
            modifier = TilingModifier(center=bounds.center().toTuple(), side=side)
        elif modifier_type == "cage_transform":
            bounds = self._default_bounds()
            modifier = CageTransformModifier(frame=self.canvas._rect_signature(bounds) if bounds is not None and not bounds.isEmpty() else (0., 0., 100., 100.))
        elif modifier_type in {"posterize", "posterize_value"}:
            value_mode = modifier_type == "posterize_value"
            modifier = PosterizeValueModifier() if value_mode else PosterizeModifier()
            incompatible = chapter.incompatible_modifier_targets(modifier, targets)
            if incompatible:
                self.canvas.report_incompatible("Add modifier", chapter.modifier_compatibility_message(modifier, incompatible), incompatible)
                return
            count, accepted = QInputDialog.getInt(
                self, modifier.name, "How many colors?", 6, 1, POSTERIZE_MAX_COLORS,
            )
            if not accepted:
                return
            from comic_editor.ui.posterize_controls import PosterizeSampler
            try:
                modifier.ranges = PosterizeSampler(value_mode=value_mode).sample(self.canvas, targets).initialize(count)
            except (ValueError, MemoryError) as error:
                QMessageBox.warning(self, modifier.name, str(error))
                return
        elif modifier_type == "hsl":
            modifier: ModifierInstance = HueSaturationLightnessModifier()
        elif modifier_type in {"blur", "blur_legacy"}:
            bounds = self._default_bounds()
            center = bounds.center() if bounds is not None else QPoint()
            modifier = BlurModifier(
                name="Blur Legacy" if modifier_type == "blur_legacy" else "Blur",
                algorithm="legacy" if modifier_type == "blur_legacy" else "normal",
                focal_center=(float(center.x()), float(center.y())),
                focal_radius=max(
                    1.0,
                    min(bounds.width(), bounds.height()) / 2
                    if bounds is not None else 100.0,
                ),
            )
        elif modifier_type == "radial_blur":
            bounds = self._default_bounds()
            center = bounds.center() if bounds is not None else QPoint()
            modifier = RadialBlurModifier(center=(float(center.x()), float(center.y())))
        elif modifier_type == "mirror":
            bounds = self._default_bounds()
            center = bounds.center() if bounds is not None else QPoint()
            radius = max(25.0, bounds.height() / 2) if bounds is not None else 100.0
            modifier = MirrorModifier(axis_start=(center.x(), center.y() - radius), axis_end=(center.x(), center.y() + radius))
        else:
            modifier = OutlineModifier()
        incompatible = chapter.incompatible_modifier_targets(modifier, targets)
        if incompatible:
            self.canvas.report_incompatible("Add modifier", chapter.modifier_compatibility_message(modifier, incompatible), incompatible)
            return
        chapter.add_modifier(modifier, targets)
        self.canvas.incompatibleSelection.emit([])
        self.canvas._remember_modifier(modifier.modifier_id)
        self.active_modifier_id = modifier.modifier_id
        self.canvas.active_modifier_id = modifier.modifier_id
        self._changed()
        self._push(before, "Add modifier")
        self.refresh()

    def remove_modifier(self, modifier_id: str) -> None:
        if self.canvas._cage_edit_before is not None:
            self.canvas.finish_cage(True)
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
        if not self.canvas.modifier_mode and modifier_id:
            return
        if modifier_id != self.active_modifier_id and self.canvas._cage_edit_before is not None:
            self.canvas.finish_cage(True)
        self.canvas._remember_modifier(modifier_id)
        self.active_modifier_id = modifier_id
        self.canvas.active_modifier_id = modifier_id
        self.canvas.update()

    def toggle_modifier(self, modifier_id):
        self.activate_modifier("" if self.active_modifier_id == modifier_id else modifier_id)
        self.refresh()

    def begin_parameter_drag(self) -> None:
        if self.canvas._cage_edit_before is not None:
            self.canvas.finish_cage(True)
        if self._parameter_before is None and self.canvas.chapter is not None:
            self._parameter_before = self.canvas.chapter.to_dict()

    def set_parameter(
        self, modifier_id: str, attribute: str, value, commit: bool,
    ) -> None:
        if self.canvas._cage_edit_before is not None:
            self.canvas.finish_cage(True)
        chapter = self.canvas.chapter
        modifier = chapter.modifiers.get(modifier_id) if chapter else None
        if modifier is None or not hasattr(modifier, attribute):
            return
        before = chapter.to_dict() if commit and self._parameter_before is None else None
        setattr(modifier, attribute, value)
        modifier.validate()
        if attribute == "muted" or not modifier.muted:
            self._changed()
        if commit and before is not None:
            self._push(before, "Edit modifier")
        if attribute in {"expanded", "muted"}:
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
        if not modifier.muted:
            self._changed()
        if commit:
            self.finish_parameter_drag()

    def begin_reorder(self, _modifier_id: str) -> None:
        if self._reorder_before is None and self.canvas.chapter is not None:
            self._reorder_before = self.canvas.chapter.to_dict()

    def move_reorder(self, modifier_id: str, global_y: int) -> None:
        ids = self.common_ids()
        if isinstance(self.canvas.chapter.modifiers.get(modifier_id), TilingModifier):
            return
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
        if ids and isinstance(self.canvas.chapter.modifiers.get(ids[0]), TilingModifier):
            destination = max(1, destination)
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
        if not self.link_modifier_id or chapter is None:
            return False
        modifier = chapter.modifiers[self.link_modifier_id]
        incompatible = chapter.incompatible_modifier_targets(modifier, [target])
        if incompatible:
            self.canvas.report_incompatible("Link modifier", chapter.modifier_compatibility_message(modifier, incompatible), incompatible)
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
        modifier = chapter.modifiers[self.link_modifier_id]
        incompatible = chapter.incompatible_modifier_targets(modifier, self.link_working)
        if incompatible:
            self.canvas.report_incompatible("Link modifier", chapter.modifier_compatibility_message(modifier, incompatible), incompatible)
            return
        chapter.set_modifier_targets(self.link_modifier_id, self.link_working)
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
