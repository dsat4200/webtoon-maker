"""Contextual controls hosted by the main-window ribbon."""
from __future__ import annotations

from PySide6.QtCore import QObject, QSignalBlocker, Qt, Signal
from PySide6.QtGui import QFont, QFontDatabase, QValidator
from PySide6.QtWidgets import (
    QAbstractSpinBox,
    QApplication,
    QCheckBox,
    QComboBox,
    QDoubleSpinBox,
    QFormLayout,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QSlider,
    QSpinBox,
    QStackedWidget,
    QToolButton,
    QVBoxLayout,
    QWidget,
    QWidgetAction,
)

from comic_editor.core.models import RasterObject, TextObject
from comic_editor.core.settings import TextPreset


def _tool_value(tool: object) -> str:
    return str(getattr(tool, "value", tool or ""))


class _FontSizeSpinBox(QSpinBox):
    """Integer editor that accepts large input before clamping on commit."""

    def validate(self, text: str, position: int):
        raw = text.strip()
        if raw in {"", "+", "-"}:
            return QValidator.State.Intermediate, text, position
        try:
            int(raw)
        except ValueError:
            return QValidator.State.Invalid, text, position
        return QValidator.State.Acceptable, text, position

    def valueFromText(self, text: str) -> int:  # noqa: N802
        try:
            value = int(text.strip())
        except ValueError:
            value = self.minimum()
        return max(self.minimum(), min(self.maximum(), value))


class ToolSettingsControls(QWidget):
    """Pencil, eraser, and fill controls for the Tool Settings page."""

    settingsChanged = Signal()
    pencilPresetSelected = Signal(str)
    pencilSettingsRequested = Signal()
    brushSizeSelected = Signal(str, str)
    brushSizesRequested = Signal()
    eraserShapeChanged = Signal(bool)
    vectorEraserModeChanged = Signal(str)
    fillSettingsChanged = Signal()

    def __init__(self, settings, parent: QWidget | None = None):
        super().__init__(parent)
        self.settings = settings
        self._loading = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)

        self.context_label = QLabel("No settings for the current tool", self)
        self.context_label.setObjectName("ribbonContextTitle")
        layout.addWidget(self.context_label)
        self.stack = QStackedWidget(self)
        layout.addWidget(self.stack, 1)

        self.empty_page = QLabel(
            "Choose Pencil, Eraser, or Fill to edit its settings.", self
        )
        self.empty_page.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.stack.addWidget(self.empty_page)
        self.pencil_page = self._build_pencil_page()
        self.stack.addWidget(self.pencil_page)
        self.eraser_page = self._build_eraser_page()
        self.stack.addWidget(self.eraser_page)
        self.fill_page = self._build_fill_page()
        self.stack.addWidget(self.fill_page)
        self.selection_page = self._build_selection_page()
        self.stack.addWidget(self.selection_page)
        self.refresh()
        self.set_context(None, False)

    def _build_pencil_page(self) -> QWidget:
        page = QWidget(self)
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        row.addWidget(QLabel("Preset", page))
        self.pencil_preset = QComboBox(page)
        self.pencil_preset.setMinimumWidth(120)
        row.addWidget(self.pencil_preset)
        row.addWidget(QLabel("Size", page))
        self.pencil_size = QComboBox(page)
        for label, value in (("S", "small"), ("M", "medium"), ("L", "large")):
            self.pencil_size.addItem(label, value)
        row.addWidget(self.pencil_size)
        self.pencil_presets_button = QPushButton("Pressure / Presets…", page)
        row.addWidget(self.pencil_presets_button)
        self.pencil_sizes_button = QPushButton("Configure sizes…", page)
        row.addWidget(self.pencil_sizes_button)
        row.addStretch(1)

        self.pencil_preset.currentTextChanged.connect(
            self._pencil_preset_changed
        )
        self.pencil_size.currentIndexChanged.connect(
            self._pencil_size_changed
        )
        self.pencil_presets_button.clicked.connect(
            self.pencilSettingsRequested
        )
        self.pencil_sizes_button.clicked.connect(self.brushSizesRequested)
        return page

    def _build_eraser_page(self) -> QWidget:
        page = QWidget(self)
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(5)
        row.addWidget(QLabel("Size", page))
        self.eraser_size = QComboBox(page)
        for label, value in (("S", "small"), ("M", "medium"), ("L", "large")):
            self.eraser_size.addItem(label, value)
        row.addWidget(self.eraser_size)
        row.addWidget(QLabel("Shape", page))
        self.eraser_shape = QComboBox(page)
        self.eraser_shape.addItem("Circle", False)
        self.eraser_shape.addItem("Square", True)
        row.addWidget(self.eraser_shape)
        self.vector_eraser_label = QLabel("Vector mode", page)
        row.addWidget(self.vector_eraser_label)
        self.vector_eraser_mode = QComboBox(page)
        self.vector_eraser_mode.addItem("Stroke", "stroke")
        self.vector_eraser_mode.addItem("Point", "point")
        self.vector_eraser_mode.addItem("Intersection", "intersection")
        row.addWidget(self.vector_eraser_mode)
        self.eraser_sizes_button = QPushButton("Configure sizes…", page)
        row.addWidget(self.eraser_sizes_button)
        row.addStretch(1)

        self.eraser_size.currentIndexChanged.connect(
            self._eraser_size_changed
        )
        self.eraser_shape.currentIndexChanged.connect(
            self._eraser_shape_changed
        )
        self.vector_eraser_mode.currentIndexChanged.connect(
            self._vector_eraser_mode_changed
        )
        self.eraser_sizes_button.clicked.connect(self.brushSizesRequested)
        return page

    def _build_fill_page(self) -> QWidget:
        page = QWidget(self)
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        self.fill_close_gaps = QCheckBox("Close gaps", page)
        row.addWidget(self.fill_close_gaps)
        self.fill_gap_threshold = QDoubleSpinBox(page)
        self.fill_gap_threshold.setRange(0, 1000)
        self.fill_gap_threshold.setDecimals(1)
        self.fill_gap_threshold.setSuffix(" px")
        row.addWidget(self.fill_gap_threshold)
        self.fill_narrow_areas = QCheckBox("Fill narrow areas", page)
        row.addWidget(self.fill_narrow_areas)
        self.fill_area_scaling = QCheckBox("Area scaling", page)
        row.addWidget(self.fill_area_scaling)
        self.fill_area_amount = QDoubleSpinBox(page)
        self.fill_area_amount.setRange(-1000, 1000)
        self.fill_area_amount.setDecimals(1)
        self.fill_area_amount.setSuffix(" px")
        row.addWidget(self.fill_area_amount)
        self.fill_area_mode = QComboBox(page)
        self.fill_area_mode.addItem("Round", "round")
        self.fill_area_mode.addItem("Rectangle", "rectangle")
        row.addWidget(self.fill_area_mode)
        row.addWidget(QLabel("Mode", page))
        self.fill_mode = QComboBox(page)
        self.fill_mode.addItem("Normal", "normal")
        self.fill_mode.addItem("Enclose and Fill", "enclose")
        row.addWidget(self.fill_mode)
        row.addStretch(1)
        for control, signal in (
            (self.fill_close_gaps, self.fill_close_gaps.toggled),
            (self.fill_gap_threshold, self.fill_gap_threshold.valueChanged),
            (self.fill_narrow_areas, self.fill_narrow_areas.toggled),
            (self.fill_area_scaling, self.fill_area_scaling.toggled),
            (self.fill_area_amount, self.fill_area_amount.valueChanged),
            (self.fill_area_mode, self.fill_area_mode.currentIndexChanged),
            (self.fill_mode, self.fill_mode.currentIndexChanged),
        ):
            del control
            signal.connect(self._fill_changed)
        return page

    def _build_selection_page(self) -> QWidget:
        page = QWidget(self)
        row = QHBoxLayout(page)
        row.setContentsMargins(0, 0, 0, 0)
        row.setSpacing(7)
        row.addWidget(QLabel("Transform", page))
        self.selection_transform_mode = QComboBox(page)
        self.selection_transform_mode.addItem("Free", "free")
        self.selection_transform_mode.addItem("Uniform", "uniform")
        self.selection_transform_mode.setToolTip(
            "Free moves corners independently and edges in connected pairs; "
            "Uniform scales the complete selection."
        )
        row.addWidget(self.selection_transform_mode)
        row.addStretch(1)
        self.selection_transform_mode.currentIndexChanged.connect(
            self._selection_transform_mode_changed
        )
        return page

    def set_context(self, tool: object, vector_active: bool) -> None:
        value = _tool_value(tool)
        if value == "raster_pencil":
            self.context_label.setText(
                "Vector Pencil" if vector_active else "Pencil"
            )
            self.stack.setCurrentWidget(self.pencil_page)
        elif value == "raster_eraser":
            self.context_label.setText(
                "Vector Eraser" if vector_active else "Eraser"
            )
            self.stack.setCurrentWidget(self.eraser_page)
        elif value == "fill":
            self.context_label.setText("Vector Fill" if vector_active else "Fill")
            self.stack.setCurrentWidget(self.fill_page)
        elif value in {
            "draw_select_rect", "draw_select_lasso", "draw_select_stroke",
        } and not vector_active:
            self.context_label.setText("Drawing Selection")
            self.stack.setCurrentWidget(self.selection_page)
        else:
            self.context_label.setText("No settings for the current tool")
            self.stack.setCurrentWidget(self.empty_page)
        self.vector_eraser_label.setVisible(vector_active)
        self.vector_eraser_mode.setVisible(vector_active)

    def refresh(self) -> None:
        self._loading = True
        self.pencil_preset.clear()
        self.pencil_preset.addItems(
            [item["name"] for item in self.settings.pencil_presets]
        )
        self.pencil_preset.setCurrentText(
            self.settings.active_pencil_preset
        )
        self.pencil_size.setCurrentIndex(
            max(0, self.pencil_size.findData(
                self.settings.active_pencil_size
            ))
        )
        self.eraser_size.setCurrentIndex(
            max(0, self.eraser_size.findData(
                self.settings.active_eraser_size
            ))
        )
        self.eraser_shape.setCurrentIndex(
            max(0, self.eraser_shape.findData(
                bool(self.settings.eraser_square)
            ))
        )
        self.vector_eraser_mode.setCurrentIndex(
            max(0, self.vector_eraser_mode.findData(
                self.settings.vector_eraser_mode
            ))
        )
        self.fill_close_gaps.setChecked(self.settings.fill_close_gaps)
        self.fill_gap_threshold.setValue(self.settings.fill_gap_threshold)
        self.fill_narrow_areas.setChecked(self.settings.fill_narrow_areas)
        self.fill_area_scaling.setChecked(self.settings.fill_area_scaling)
        self.fill_area_amount.setValue(self.settings.fill_area_amount)
        self.fill_area_mode.setCurrentIndex(
            max(0, self.fill_area_mode.findData(
                self.settings.fill_area_mode
            ))
        )
        self.fill_mode.setCurrentIndex(
            max(0, self.fill_mode.findData(self.settings.fill_mode))
        )
        self.selection_transform_mode.setCurrentIndex(max(
            0, self.selection_transform_mode.findData(
                self.settings.transform_mode
            ),
        ))
        self.fill_gap_threshold.setEnabled(self.settings.fill_close_gaps)
        self.fill_area_amount.setEnabled(self.settings.fill_area_scaling)
        self.fill_area_mode.setEnabled(self.settings.fill_area_scaling)
        self._loading = False

    def _selection_transform_mode_changed(self) -> None:
        if self._loading:
            return
        mode = str(self.selection_transform_mode.currentData())
        if mode not in {"free", "uniform"}:
            return
        self.settings.transform_mode = mode
        self.settings.clamp()
        self.settingsChanged.emit()

    def _pencil_preset_changed(self, name: str) -> None:
        if not self._loading and name:
            self.pencilPresetSelected.emit(name)

    def _pencil_size_changed(self) -> None:
        if not self._loading:
            self.brushSizeSelected.emit(
                "raster_pencil", str(self.pencil_size.currentData())
            )

    def _eraser_size_changed(self) -> None:
        if not self._loading:
            self.brushSizeSelected.emit(
                "raster_eraser", str(self.eraser_size.currentData())
            )

    def _eraser_shape_changed(self) -> None:
        if not self._loading:
            self.eraserShapeChanged.emit(
                bool(self.eraser_shape.currentData())
            )

    def _vector_eraser_mode_changed(self) -> None:
        if self._loading:
            return
        mode = str(self.vector_eraser_mode.currentData())
        self.settings.vector_eraser_mode = mode
        self.vectorEraserModeChanged.emit(mode)
        self.settingsChanged.emit()

    def _fill_changed(self, *args) -> None:
        del args
        if self._loading:
            return
        self.settings.fill_close_gaps = self.fill_close_gaps.isChecked()
        self.settings.fill_gap_threshold = self.fill_gap_threshold.value()
        self.settings.fill_narrow_areas = self.fill_narrow_areas.isChecked()
        self.settings.fill_area_scaling = self.fill_area_scaling.isChecked()
        self.settings.fill_area_amount = self.fill_area_amount.value()
        self.settings.fill_area_mode = str(self.fill_area_mode.currentData())
        self.settings.fill_mode = str(self.fill_mode.currentData())
        self.settings.clamp()
        self.fill_gap_threshold.setEnabled(self.settings.fill_close_gaps)
        self.fill_area_amount.setEnabled(self.settings.fill_area_scaling)
        self.fill_area_mode.setEnabled(self.settings.fill_area_scaling)
        self.settingsChanged.emit()
        self.fillSettingsChanged.emit()


class TextObjectControls(QObject):
    """Selected-text properties hosted by Tool Settings ribbon groups."""

    settingsChanged = Signal()
    objectChanged = Signal()

    def __init__(self, canvas, settings, parent: QObject | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self.settings = settings
        self._loading = False
        self._opacity_before: dict | None = None
        self._preview_roles_enabled: bool | None = None
        self._alignment_buttons: dict[tuple[str, str], QToolButton] = {}
        self.object_widget = self._build_object_widget()
        self.typography_widget = self._build_typography_widget()
        self.layout_widget = self._build_layout_widget()
        self.refresh()

    @staticmethod
    def _small_button(text: str, tooltip: str, parent: QWidget) -> QToolButton:
        button = QToolButton(parent)
        button.setText(text)
        button.setToolTip(tooltip)
        button.setFixedWidth(24)
        return button

    def _build_object_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        preset_row = QHBoxLayout()
        preset_row.setContentsMargins(0, 0, 0, 0)
        preset_row.addWidget(QLabel("Preset", widget))
        self.preset_combo = QComboBox(widget)
        self.preset_combo.setMinimumWidth(120)
        preset_row.addWidget(self.preset_combo, 1)
        self.preset_save = self._small_button(
            "S", "Overwrite selected preset", widget
        )
        self.preset_rename = self._small_button(
            "R", "Rename selected preset", widget
        )
        self.preset_remove = self._small_button(
            "X", "Remove selected preset", widget
        )
        self.preset_add = self._small_button("+", "Create preset", widget)
        for button in (
            self.preset_save, self.preset_rename,
            self.preset_remove, self.preset_add,
        ):
            preset_row.addWidget(button)
        layout.addLayout(preset_row)

        flags = QHBoxLayout()
        self.visible = QCheckBox("Visible", widget)
        self.opacity_lock = QCheckBox("Lock opacity", widget)
        flags.addWidget(self.visible)
        flags.addWidget(self.opacity_lock)
        flags.addWidget(QLabel("Opacity", widget))
        self.opacity = QSlider(Qt.Orientation.Horizontal, widget)
        self.opacity.setRange(0, 100)
        self.opacity.setMinimumWidth(90)
        flags.addWidget(self.opacity, 1)
        self.opacity_value = QLabel("100%", widget)
        self.opacity_value.setMinimumWidth(36)
        flags.addWidget(self.opacity_value)
        layout.addLayout(flags)

        self.preset_combo.activated.connect(self._apply_preset)
        self.preset_save.clicked.connect(self._save_preset)
        self.preset_rename.clicked.connect(self._rename_preset)
        self.preset_remove.clicked.connect(self._remove_preset)
        self.preset_add.clicked.connect(self._add_preset)
        self.visible.toggled.connect(
            lambda checked: self._apply_field("visible", bool(checked))
        )
        self.opacity_lock.toggled.connect(self._opacity_lock_changed)
        self.opacity.sliderPressed.connect(self._begin_opacity_drag)
        self.opacity.valueChanged.connect(self._opacity_changed)
        self.opacity.sliderReleased.connect(self._finish_opacity_drag)
        return widget

    def _build_typography_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        font_row = QHBoxLayout()
        font_row.addWidget(QLabel("Font", widget))
        self.font_family = QComboBox(widget)
        self.font_family.setMinimumWidth(180)
        families = QFontDatabase.families()
        # Some headless Qt platforms do not expose the system font database.
        # Keep the editor usable (and the preview mode meaningful) there too.
        self.font_family.addItems(
            families or [QApplication.font().family() or "Sans Serif"]
        )
        font_row.addWidget(self.font_family, 1)
        self.preview_fonts = QCheckBox("Preview fonts", widget)
        font_row.addWidget(self.preview_fonts)
        layout.addLayout(font_row)

        metrics = QHBoxLayout()
        metrics.addWidget(QLabel("Size", widget))
        self.font_size = _FontSizeSpinBox(widget)
        self.font_size.setRange(6, 250)
        self.font_size.setSingleStep(1)
        self.font_size.setKeyboardTracking(False)
        self.font_size.setMaximumWidth(72)
        metrics.addWidget(self.font_size)
        self.bold = QCheckBox("Bold", widget)
        self.italic = QCheckBox("Italic", widget)
        metrics.addWidget(self.bold)
        metrics.addWidget(self.italic)
        metrics.addWidget(QLabel("Kerning", widget))
        self.kerning = QDoubleSpinBox(widget)
        self.kerning.setRange(-20, 100)
        self.kerning.setSingleStep(0.1)
        self.kerning.setMaximumWidth(78)
        metrics.addWidget(self.kerning)
        layout.addLayout(metrics)

        self.font_family.currentTextChanged.connect(
            lambda value: self._apply_field("font_family", value)
        )
        self.preview_fonts.toggled.connect(self._preview_fonts_changed)
        self.font_size.editingFinished.connect(
            lambda: self._apply_field("font_size", int(self.font_size.value()))
        )
        self.bold.toggled.connect(
            lambda checked: self._apply_field("bold", bool(checked))
        )
        self.italic.toggled.connect(
            lambda checked: self._apply_field("italic", bool(checked))
        )
        self.kerning.editingFinished.connect(
            lambda: self._apply_field("kerning", float(self.kerning.value()))
        )
        return widget

    def _build_layout_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(3)

        first = QHBoxLayout()
        first.addWidget(QLabel("Layout", widget))
        self.layout_mode = QComboBox(widget)
        self.layout_mode.addItem("Strict to parent", "strict")
        self.layout_mode.addItem("Free transform", "free")
        first.addWidget(self.layout_mode)
        self.align_button = QToolButton(widget)
        self.align_button.setText("Align")
        self.align_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.align_menu = QMenu(self.align_button)
        self.align_button.setMenu(self.align_menu)
        alignment_widget = QWidget(self.align_menu)
        alignment_grid = QGridLayout(alignment_widget)
        alignment_grid.setContentsMargins(5, 5, 5, 5)
        alignment_grid.setSpacing(2)
        for row, vertical in enumerate(("top", "middle", "bottom")):
            for column, horizontal in enumerate(("left", "center", "right")):
                button = QToolButton(alignment_widget)
                button.setText("●")
                button.setCheckable(True)
                button.setToolTip(f"{vertical.title()} / {horizontal.title()}")
                button.clicked.connect(
                    lambda checked=False, h=horizontal, v=vertical:
                    self._set_alignment(h, v)
                )
                alignment_grid.addWidget(button, row, column)
                self._alignment_buttons[(horizontal, vertical)] = button
        action = QWidgetAction(self.align_menu)
        action.setDefaultWidget(alignment_widget)
        self.align_menu.addAction(action)
        first.addWidget(self.align_button)
        first.addStretch(1)
        layout.addLayout(first)

        second = QHBoxLayout()
        self.margin_label = QLabel("Margin", widget)
        self.margin = QDoubleSpinBox(widget)
        self.margin.setRange(0, 500)
        self.margin.setSuffix(" px")
        self.margin.setKeyboardTracking(False)
        second.addWidget(self.margin_label)
        second.addWidget(self.margin)
        self.geometry_reference_label = QLabel("Shape reference", widget)
        self.geometry_reference = QComboBox(widget)
        self.geometry_reference.addItem("Direct parent", "direct")
        self.geometry_reference.addItem("Closest compound", "compound")
        second.addWidget(self.geometry_reference_label)
        second.addWidget(self.geometry_reference)
        self.transform_label = QLabel("Transform", widget)
        self.transform_mode = QComboBox(widget)
        self.transform_mode.addItem("Free Projective", "free")
        self.transform_mode.addItem("Uniform", "uniform")
        second.addWidget(self.transform_label)
        second.addWidget(self.transform_mode)
        layout.addLayout(second)

        self.layout_mode.currentIndexChanged.connect(self._layout_mode_changed)
        self.margin.editingFinished.connect(
            lambda: self._apply_field("margin", float(self.margin.value()))
        )
        self.geometry_reference.currentIndexChanged.connect(
            lambda: self._apply_field(
                "geometry_reference", str(self.geometry_reference.currentData())
            )
        )
        self.transform_mode.currentIndexChanged.connect(
            self._transform_mode_changed
        )
        return widget

    def _selected(self) -> TextObject | None:
        if (
            self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return None
        entity = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return entity if isinstance(entity, TextObject) else None

    def _set_font_preview_roles(self) -> None:
        role = Qt.ItemDataRole.FontRole
        preview = bool(self.settings.preview_font_names)
        if preview == self._preview_roles_enabled:
            return
        for index in range(self.font_family.count()):
            self.font_family.setItemData(
                index,
                QFont(self.font_family.itemText(index)) if preview else None,
                role,
            )
        self._preview_roles_enabled = preview

    def refresh(self) -> None:
        entity = self._selected()
        self._loading = True
        controls = (
            self.preset_combo, self.visible, self.opacity_lock, self.opacity,
            self.font_family, self.preview_fonts, self.font_size, self.bold,
            self.italic, self.kerning, self.layout_mode, self.margin,
            self.geometry_reference, self.transform_mode,
        )
        blockers = [QSignalBlocker(control) for control in controls]
        self._refresh_presets()
        self.preview_fonts.setChecked(bool(self.settings.preview_font_names))
        self._set_font_preview_roles()
        self.transform_mode.setCurrentIndex(max(
            0, self.transform_mode.findData(self.settings.transform_mode)
        ))
        if entity is not None:
            self.visible.setChecked(entity.visible)
            self.opacity_lock.setChecked(entity.opacity_locked)
            self.opacity.setValue(round(entity.opacity * 100))
            self.opacity_value.setText(f"{self.opacity.value()}%")
            self.opacity.setEnabled(not entity.opacity_locked)
            self.font_family.setCurrentText(entity.font_family)
            self.font_size.setValue(max(6, min(250, round(entity.font_size))))
            self.bold.setChecked(entity.bold)
            self.italic.setChecked(entity.italic)
            self.kerning.setValue(entity.kerning)
            self.layout_mode.setCurrentIndex(max(
                0, self.layout_mode.findData(entity.layout_mode)
            ))
            self.margin.setValue(entity.margin)
            self.geometry_reference.setCurrentIndex(max(
                0, self.geometry_reference.findData(entity.geometry_reference)
            ))
            for key, button in self._alignment_buttons.items():
                button.setChecked(key == (
                    entity.horizontal_alignment, entity.vertical_alignment
                ))
            strict = entity.layout_mode == "strict"
            self.margin_label.setVisible(strict)
            self.margin.setVisible(strict)
            self.transform_label.setVisible(not strict)
            self.transform_mode.setVisible(not strict)
            compound = self.canvas.chapter.closest_compound_ancestor(
                entity.parent_layer_id, include_self=True
            )
            reference_visible = compound is not None
            self.geometry_reference_label.setVisible(reference_visible)
            self.geometry_reference.setVisible(reference_visible)
        del blockers
        self._loading = False

    def _refresh_presets(self) -> None:
        current = self.settings.active_text_preset
        self.preset_combo.clear()
        for item in self.settings.text_presets:
            self.preset_combo.addItem(item["name"])
        self.preset_combo.setCurrentIndex(max(0, self.preset_combo.findText(current)))
        protected = self.preset_combo.currentText() == "Default"
        self.preset_rename.setEnabled(not protected)
        self.preset_remove.setEnabled(not protected)

    def _commit_text_session(self) -> None:
        self.canvas.commit_active_text_edit()

    def _push_change(self, before: dict, label: str) -> None:
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(before, after, label)
            self.canvas.documentChanged.emit(None)
            self.canvas.update()
            self.objectChanged.emit()

    def _apply_field(self, key: str, value) -> None:
        entity = self._selected()
        if self._loading or entity is None:
            return
        self._commit_text_session()
        entity = self._selected()
        if entity is None:
            return
        before = self.canvas.chapter.to_dict()
        if key == "font_size":
            value = max(6, min(250, int(value)))
        elif key == "geometry_reference" and value not in {"direct", "compound"}:
            value = "direct"
        setattr(entity, key, value)
        if key == "layout_mode" and value == "free" and entity.transform_quad is None:
            entity.transform_quad = self.canvas._rect_quad(
                self.canvas._strict_text_rect(entity)
            )
        self._push_change(before, "Edit text properties")
        self.refresh()

    def _layout_mode_changed(self, *args) -> None:
        del args
        if not self._loading:
            self._apply_field("layout_mode", str(self.layout_mode.currentData()))

    def _set_alignment(self, horizontal: str, vertical: str) -> None:
        entity = self._selected()
        if entity is None:
            return
        self._commit_text_session()
        entity = self._selected()
        if entity is None:
            return
        before = self.canvas.chapter.to_dict()
        entity.horizontal_alignment = horizontal
        entity.vertical_alignment = vertical
        self.align_menu.close()
        self._push_change(before, "Align text")
        self.refresh()

    def _preview_fonts_changed(self, checked: bool) -> None:
        if self._loading:
            return
        self._commit_text_session()
        self.settings.preview_font_names = bool(checked)
        self._set_font_preview_roles()
        self.settingsChanged.emit()

    def _transform_mode_changed(self, *args) -> None:
        del args
        if self._loading:
            return
        mode = str(self.transform_mode.currentData())
        if mode not in {"free", "uniform"}:
            return
        self._commit_text_session()
        self.settings.transform_mode = mode
        self.settings.clamp()
        self.settingsChanged.emit()
        self.canvas.update()

    def _opacity_lock_changed(self, checked: bool) -> None:
        entity = self._selected()
        if self._loading or entity is None:
            return
        self._commit_text_session()
        entity = self._selected()
        if entity is None:
            return
        before = self.canvas.chapter.to_dict()
        entity.opacity_locked = bool(checked)
        if entity.opacity_locked:
            entity.opacity = self.canvas.chapter.layers[
                entity.parent_layer_id
            ].opacity
        self._push_change(before, "Change text opacity lock")
        self.refresh()

    def _begin_opacity_drag(self) -> None:
        if self._loading or self._selected() is None:
            return
        self._commit_text_session()
        self._opacity_before = self.canvas.chapter.to_dict()

    def _opacity_changed(self, value: int) -> None:
        self.opacity_value.setText(f"{int(value)}%")
        entity = self._selected()
        if self._loading or entity is None or entity.opacity_locked:
            return
        before = None if self._opacity_before is not None else self.canvas.chapter.to_dict()
        entity.opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            self._push_change(before, "Change text opacity")

    def _finish_opacity_drag(self) -> None:
        before, self._opacity_before = self._opacity_before, None
        if before is not None:
            self._push_change(before, "Change text opacity")
        self.refresh()

    def _current_preset(self) -> TextPreset | None:
        entity = self._selected()
        if entity is None:
            return None
        return TextPreset(
            name=self.preset_combo.currentText() or "Default",
            font_family=entity.font_family,
            font_size=max(6, min(250, round(entity.font_size))),
            bold=entity.bold,
            italic=entity.italic,
            kerning=entity.kerning,
            layout_mode=entity.layout_mode,
            horizontal_alignment=entity.horizontal_alignment,
            vertical_alignment=entity.vertical_alignment,
            margin=entity.margin,
        )

    def _apply_preset(self, index: int) -> None:
        entity = self._selected()
        if entity is None or index < 0:
            return
        self._commit_text_session()
        entity = self._selected()
        if entity is None:
            return
        preset = TextPreset.from_dict(self.settings.text_presets[index])
        before = self.canvas.chapter.to_dict()
        for key in (
            "font_family", "font_size", "bold", "italic", "kerning",
            "layout_mode", "horizontal_alignment", "vertical_alignment", "margin",
        ):
            setattr(entity, key, getattr(preset, key))
        if entity.layout_mode == "free" and entity.transform_quad is None:
            entity.transform_quad = self.canvas._rect_quad(
                self.canvas._strict_text_rect(entity)
            )
        self.settings.active_text_preset = preset.name
        self.settingsChanged.emit()
        self._push_change(before, "Apply text preset")
        self.refresh()

    def _save_preset(self) -> None:
        self._commit_text_session()
        preset = self._current_preset()
        index = self.preset_combo.currentIndex()
        if preset is None or index < 0:
            return
        self.settings.text_presets[index] = preset.to_dict()
        self.settings.active_text_preset = preset.name
        self.settingsChanged.emit()
        self.refresh()

    def _add_preset(self) -> None:
        self._commit_text_session()
        preset = self._current_preset()
        if preset is None:
            return
        name, accepted = QInputDialog.getText(
            self.object_widget, "New text preset", "Preset name"
        )
        name = name.strip()
        if not accepted or not name:
            return
        if any(item["name"].casefold() == name.casefold()
               for item in self.settings.text_presets):
            QMessageBox.warning(
                self.object_widget, "Text preset",
                "That preset name already exists.",
            )
            return
        preset.name = name
        self.settings.text_presets.append(preset.to_dict())
        self.settings.active_text_preset = name
        self.settingsChanged.emit()
        self.refresh()

    def _rename_preset(self) -> None:
        self._commit_text_session()
        index = self.preset_combo.currentIndex()
        if index <= 0:
            return
        current = self.settings.text_presets[index]["name"]
        name, accepted = QInputDialog.getText(
            self.object_widget, "Rename text preset", "Preset name", text=current
        )
        name = name.strip()
        if not accepted or not name:
            return
        if any(
            item_index != index and item["name"].casefold() == name.casefold()
            for item_index, item in enumerate(self.settings.text_presets)
        ):
            QMessageBox.warning(
                self.object_widget, "Text preset",
                "That preset name already exists.",
            )
            return
        self.settings.text_presets[index]["name"] = name
        self.settings.active_text_preset = name
        self.settingsChanged.emit()
        self.refresh()

    def _remove_preset(self) -> None:
        self._commit_text_session()
        index = self.preset_combo.currentIndex()
        if index <= 0:
            return
        self.settings.text_presets.pop(index)
        self.settings.active_text_preset = "Default"
        self.settingsChanged.emit()
        self.refresh()


class VectorToolsControls(QObject):
    """Owns the persistent Vector Tools ribbon columns."""

    settingsChanged = Signal()
    redrawToolRequested = Signal()
    redrawApplyRequested = Signal()
    connectToolRequested = Signal()
    simplifyToolRequested = Signal()
    simplifyApplyRequested = Signal()

    def __init__(self, settings, parent: QObject | None = None):
        super().__init__(parent)
        self.settings = settings
        self._loading = False
        self.redraw_widget = self._build_redraw_widget()
        self.connect_widget = self._build_connect_widget()
        self.simplify_widget = self._build_simplify_widget()
        self.transform_widget = self._build_transform_widget()
        self.refresh()

    def _build_transform_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Mode", widget))
        self.transform_mode = QComboBox(widget)
        self.transform_mode.addItem("Free", "free")
        self.transform_mode.addItem("Uniform", "uniform")
        self.transform_mode.setToolTip(
            "Free moves corners independently and edges in connected pairs; "
            "Uniform scales the complete selection."
        )
        row.addWidget(self.transform_mode)
        layout.addLayout(row)
        layout.addStretch(1)
        self.transform_mode.currentIndexChanged.connect(
            self._transform_mode_changed
        )
        return widget

    def _build_redraw_widget(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        self.redraw_form = form
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(5)
        form.setVerticalSpacing(2)
        self.redraw_parameter = QComboBox(widget)
        self.redraw_parameter.addItem("Thickness", "thickness")
        self.redraw_parameter.addItem("Opacity", "opacity")
        form.addRow("Parameter", self.redraw_parameter)
        self.redraw_interaction = QComboBox(widget)
        self.redraw_interaction.addItem("Manual Redraw", "manual")
        self.redraw_interaction.addItem("Point Select", "point")
        form.addRow("Interaction", self.redraw_interaction)
        self.redraw_operation = QComboBox(widget)
        self.redraw_operation.addItem("Increase", "increase")
        self.redraw_operation.addItem("Decrease", "decrease")
        self.redraw_operation.addItem("Uniform", "uniform")
        form.addRow("Operation", self.redraw_operation)
        self.redraw_amount_row = QWidget(widget)
        amount_layout = QHBoxLayout(self.redraw_amount_row)
        amount_layout.setContentsMargins(0, 0, 0, 0)
        self.redraw_amount_slider = QSlider(
            Qt.Orientation.Horizontal, self.redraw_amount_row
        )
        self.redraw_amount = QSpinBox(self.redraw_amount_row)
        self.redraw_amount.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        amount_layout.addWidget(self.redraw_amount_slider, 1)
        amount_layout.addWidget(self.redraw_amount)
        form.addRow("Amount / target", self.redraw_amount_row)
        self.redraw_maximum_row = QWidget(widget)
        maximum_layout = QHBoxLayout(self.redraw_maximum_row)
        maximum_layout.setContentsMargins(0, 0, 0, 0)
        self.redraw_maximum_slider = QSlider(
            Qt.Orientation.Horizontal, self.redraw_maximum_row
        )
        self.redraw_maximum = QSpinBox(self.redraw_maximum_row)
        self.redraw_maximum.setButtonSymbols(
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )
        maximum_layout.addWidget(self.redraw_maximum_slider, 1)
        maximum_layout.addWidget(self.redraw_maximum)
        form.addRow("Pressure maximum", self.redraw_maximum_row)
        buttons = QHBoxLayout()
        self.redraw_tool_button = QPushButton("Use Redraw", widget)
        self.redraw_apply_button = QPushButton("Apply", widget)
        buttons.addWidget(self.redraw_tool_button)
        buttons.addWidget(self.redraw_apply_button)
        form.addRow(buttons)
        for combo in (
            self.redraw_parameter,
            self.redraw_interaction,
            self.redraw_operation,
        ):
            combo.currentIndexChanged.connect(self._redraw_changed)
        self.redraw_amount.valueChanged.connect(
            self._redraw_amount_edited
        )
        self.redraw_maximum.valueChanged.connect(
            self._redraw_maximum_edited
        )
        self.redraw_amount_slider.valueChanged.connect(
            self._redraw_amount_slid
        )
        self.redraw_maximum_slider.valueChanged.connect(
            self._redraw_maximum_slid
        )
        self.redraw_tool_button.clicked.connect(self.redrawToolRequested)
        self.redraw_apply_button.clicked.connect(self.redrawApplyRequested)
        return widget

    def _build_connect_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        self.connect_status = QLabel(
            "Sweep across two endpoints to connect them.", widget
        )
        self.connect_status.setWordWrap(True)
        layout.addWidget(self.connect_status)
        self.connect_button = QPushButton("Use Connect", widget)
        self.connect_button.clicked.connect(self.connectToolRequested)
        layout.addWidget(self.connect_button)
        layout.addStretch(1)
        return widget

    def _build_simplify_widget(self) -> QWidget:
        widget = QWidget()
        layout = QVBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        row = QHBoxLayout()
        row.addWidget(QLabel("Amount", widget))
        self.simplify_slider = QSlider(Qt.Orientation.Horizontal, widget)
        self.simplify_slider.setRange(0, 100)
        row.addWidget(self.simplify_slider, 1)
        self.simplify_amount = QSpinBox(widget)
        self.simplify_amount.setRange(0, 100)
        self.simplify_amount.setSuffix("%")
        row.addWidget(self.simplify_amount)
        layout.addLayout(row)
        buttons = QHBoxLayout()
        self.simplify_tool_button = QPushButton("Sweep Simplify", widget)
        self.simplify_apply_button = QPushButton("Apply", widget)
        buttons.addWidget(self.simplify_tool_button)
        buttons.addWidget(self.simplify_apply_button)
        layout.addLayout(buttons)
        layout.addStretch(1)
        self.simplify_slider.valueChanged.connect(
            self.simplify_amount.setValue
        )
        self.simplify_amount.valueChanged.connect(
            self.simplify_slider.setValue
        )
        self.simplify_amount.valueChanged.connect(self._simplify_changed)
        self.simplify_tool_button.clicked.connect(self.simplifyToolRequested)
        self.simplify_apply_button.clicked.connect(
            self.simplifyApplyRequested
        )
        return widget

    def refresh(self) -> None:
        self._loading = True
        self.redraw_parameter.setCurrentIndex(
            max(0, self.redraw_parameter.findData(
                self.settings.vector_redraw_parameter
            ))
        )
        self.redraw_interaction.setCurrentIndex(
            max(0, self.redraw_interaction.findData(
                self.settings.vector_redraw_interaction
            ))
        )
        self.redraw_operation.setCurrentIndex(
            max(0, self.redraw_operation.findData(
                self.settings.vector_redraw_operation
            ))
        )
        # The valid range depends on the selected parameter.  Configure it
        # before assigning persisted values so Qt does not silently clamp a
        # legitimate 100% opacity (or a large thickness) to its 99.99 default.
        self._sync_redraw_controls()
        self.redraw_amount.setValue(self.settings.vector_redraw_amount)
        self.redraw_maximum.setValue(
            self.settings.vector_redraw_thickness_max
            if self.settings.vector_redraw_parameter == "thickness"
            else self.settings.vector_redraw_opacity_max
        )
        self.simplify_amount.setValue(self.settings.vector_simplify_amount)
        self.transform_mode.setCurrentIndex(max(
            0, self.transform_mode.findData(self.settings.transform_mode)
        ))
        self._loading = False

    def _transform_mode_changed(self) -> None:
        if self._loading:
            return
        mode = str(self.transform_mode.currentData())
        if mode not in {"free", "uniform"}:
            return
        self.settings.transform_mode = mode
        self.settings.clamp()
        self.settingsChanged.emit()

    def set_selection_summary(
        self, selected_points: int, selected_strokes: int, total_strokes: int
    ) -> None:
        if selected_points:
            target = f"{selected_points} selected point(s)"
        elif selected_strokes:
            target = f"{selected_strokes} selected stroke(s)"
        else:
            target = f"all {total_strokes} stroke(s)"
        self.redraw_apply_button.setToolTip(f"Apply to {target}")
        self.simplify_apply_button.setToolTip(f"Apply to {target}")
        if (
            (selected_points or selected_strokes)
            and self.redraw_interaction.currentData() == "manual"
        ):
            self.redraw_interaction.setCurrentIndex(
                self.redraw_interaction.findData("point")
            )

    def _sync_redraw_controls(self) -> None:
        manual = self.redraw_interaction.currentData() == "manual"
        for control in (
            self.redraw_operation,
            self.redraw_amount_row,
            self.redraw_apply_button,
        ):
            control.setEnabled(not manual)
        self.redraw_form.setRowVisible(self.redraw_maximum_row, manual)
        opacity = self.redraw_parameter.currentData() == "opacity"
        suffix = "%" if opacity else " px"
        self.redraw_amount.setRange(0, 100 if opacity else 40)
        self.redraw_amount.setSuffix(suffix)
        self.redraw_amount.setSingleStep(1)
        self.redraw_amount_slider.setRange(0, 20 if opacity else 40)
        self.redraw_maximum.setRange(0 if opacity else 1, 100 if opacity else 40)
        self.redraw_maximum.setSuffix(suffix)
        self.redraw_maximum.setSingleStep(1)
        self.redraw_maximum_slider.setRange(
            0 if opacity else 1, 20 if opacity else 40
        )
        self._sync_redraw_slider(
            self.redraw_amount_slider, self.redraw_amount
        )
        self._sync_redraw_slider(
            self.redraw_maximum_slider, self.redraw_maximum
        )

    def _sync_redraw_slider(
        self, slider: QSlider, editor: QSpinBox,
    ) -> None:
        opacity = self.redraw_parameter.currentData() == "opacity"
        value = round(editor.value() / 5) if opacity else editor.value()
        blocker = QSignalBlocker(slider)
        slider.setValue(value)
        del blocker

    def _redraw_amount_edited(self, value: int) -> None:
        del value
        self._sync_redraw_slider(
            self.redraw_amount_slider, self.redraw_amount
        )
        self._redraw_changed()

    def _redraw_maximum_edited(self, value: int) -> None:
        del value
        self._sync_redraw_slider(
            self.redraw_maximum_slider, self.redraw_maximum
        )
        self._redraw_changed()

    def _redraw_amount_slid(self, value: int) -> None:
        mapped = (
            value * 5
            if self.redraw_parameter.currentData() == "opacity"
            else value
        )
        blocker = QSignalBlocker(self.redraw_amount)
        self.redraw_amount.setValue(mapped)
        del blocker
        self._redraw_changed()

    def _redraw_maximum_slid(self, value: int) -> None:
        mapped = (
            value * 5
            if self.redraw_parameter.currentData() == "opacity"
            else value
        )
        blocker = QSignalBlocker(self.redraw_maximum)
        self.redraw_maximum.setValue(mapped)
        del blocker
        self._redraw_changed()

    def _redraw_changed(self, *args) -> None:
        del args
        if self._loading:
            return
        previous_parameter = self.settings.vector_redraw_parameter
        parameter = str(self.redraw_parameter.currentData())
        self.settings.vector_redraw_parameter = parameter
        self.settings.vector_redraw_interaction = str(
            self.redraw_interaction.currentData()
        )
        self.settings.vector_redraw_operation = str(
            self.redraw_operation.currentData()
        )
        if previous_parameter != parameter:
            self._loading = True
            self._sync_redraw_controls()
            self.redraw_maximum.setValue(
                self.settings.vector_redraw_thickness_max
                if parameter == "thickness"
                else self.settings.vector_redraw_opacity_max
            )
            self._loading = False
        self.settings.vector_redraw_amount = self.redraw_amount.value()
        if parameter == "thickness":
            self.settings.vector_redraw_thickness_max = (
                self.redraw_maximum.value()
            )
        else:
            self.settings.vector_redraw_opacity_max = (
                self.redraw_maximum.value()
            )
        self.settings.clamp()
        self._sync_redraw_controls()
        self.settingsChanged.emit()

    def _simplify_changed(self, value: int) -> None:
        if self._loading:
            return
        self.settings.vector_simplify_amount = int(value)
        self.settings.clamp()
        self.settingsChanged.emit()


class RasterObjectControls(QObject):
    """Contextual raster properties and shared transform settings."""

    settingsChanged = Signal()
    objectChanged = Signal()

    def __init__(self, canvas, settings, parent: QObject | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self.settings = settings
        self._loading = False
        self._slider_before: dict[str, dict] = {}
        self.object_widget = self._build_object_widget()
        self.transform_widget = self._build_transform_widget()
        self.refresh()

    def _build_object_widget(self) -> QWidget:
        widget = QWidget()
        form = QFormLayout(widget)
        form.setContentsMargins(0, 0, 0, 0)
        form.setHorizontalSpacing(6)
        form.setVerticalSpacing(3)
        self.name = QLineEdit(widget)
        form.addRow("Name", self.name)
        flags = QWidget(widget)
        flags_layout = QHBoxLayout(flags)
        flags_layout.setContentsMargins(0, 0, 0, 0)
        self.visible = QCheckBox("Visible", flags)
        self.opacity_lock = QCheckBox("Lock opacity", flags)
        flags_layout.addWidget(self.visible)
        flags_layout.addWidget(self.opacity_lock)
        form.addRow(flags)
        self.opacity, self.opacity_value, opacity_row = self._slider_row(widget)
        form.addRow("Opacity", opacity_row)
        self.ignore_parent_mask = QCheckBox(
            "Ignore direct parent mask", widget
        )
        form.addRow(self.ignore_parent_mask)
        self.underlay, self.underlay_value, underlay_row = self._slider_row(
            widget
        )
        form.addRow("Show underlay", underlay_row)
        self.geometry_reference_label = QLabel("Shape reference", widget)
        self.geometry_reference = QComboBox(widget)
        self.geometry_reference.addItem("Direct parent", "direct")
        self.geometry_reference.addItem("Closest compound", "compound")
        form.addRow(
            self.geometry_reference_label, self.geometry_reference
        )

        self.name.editingFinished.connect(self._apply_discrete)
        self.visible.toggled.connect(self._apply_discrete)
        self.opacity_lock.toggled.connect(self._apply_discrete)
        self.ignore_parent_mask.toggled.connect(self._apply_discrete)
        self.geometry_reference.currentIndexChanged.connect(
            self._apply_discrete
        )
        self._connect_slider("opacity", self.opacity)
        self._connect_slider("underlay", self.underlay)
        return widget

    @staticmethod
    def _slider_row(parent: QWidget) -> tuple[QSlider, QLabel, QWidget]:
        row = QWidget(parent)
        layout = QHBoxLayout(row)
        layout.setContentsMargins(0, 0, 0, 0)
        slider = QSlider(Qt.Orientation.Horizontal, row)
        slider.setRange(0, 100)
        value = QLabel("0%", row)
        value.setMinimumWidth(36)
        value.setAlignment(Qt.AlignmentFlag.AlignRight)
        layout.addWidget(slider, 1)
        layout.addWidget(value)
        return slider, value, row

    def _connect_slider(self, key: str, slider: QSlider) -> None:
        slider.sliderPressed.connect(
            lambda current=key: self._begin_slider(current)
        )
        slider.valueChanged.connect(
            lambda value, current=key: self._slider_changed(current, value)
        )
        slider.sliderReleased.connect(
            lambda current=key: self._finish_slider(current)
        )

    def _build_transform_widget(self) -> QWidget:
        widget = QWidget()
        layout = QHBoxLayout(widget)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(QLabel("Mode", widget))
        self.transform_mode = QComboBox(widget)
        self.transform_mode.addItem("Free", "free")
        self.transform_mode.addItem("Uniform", "uniform")
        self.transform_mode.setToolTip(
            "Free moves corners independently and edges in connected pairs; "
            "Uniform scales the complete raster."
        )
        layout.addWidget(self.transform_mode)
        layout.addStretch(1)
        self.transform_mode.currentIndexChanged.connect(
            self._transform_changed
        )
        return widget

    def _selected(self) -> RasterObject | None:
        if (
            self.canvas.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return None
        entity = self.canvas.chapter.objects.get(self.canvas.selected_id)
        return entity if isinstance(entity, RasterObject) else None

    def refresh(self) -> None:
        entity = self._selected()
        self._loading = True
        if entity is not None:
            self.name.setText(entity.name)
            self.visible.setChecked(entity.visible)
            self.opacity_lock.setChecked(entity.opacity_locked)
            self.opacity.setValue(round(entity.opacity * 100))
            self.opacity_value.setText(f"{self.opacity.value()}%")
            self.opacity.setEnabled(not entity.opacity_locked)
            self.ignore_parent_mask.setChecked(entity.ignore_parent_mask)
            self.underlay.setValue(round(entity.underlay_opacity * 100))
            self.underlay_value.setText(f"{self.underlay.value()}%")
            compound = self.canvas.chapter.closest_compound_ancestor(
                entity.parent_layer_id, include_self=True
            )
            visible = compound is not None
            self.geometry_reference_label.setVisible(visible)
            self.geometry_reference.setVisible(visible)
            self.geometry_reference.setCurrentIndex(max(
                0, self.geometry_reference.findData(
                    entity.geometry_reference
                )
            ))
        self.transform_mode.setCurrentIndex(max(
            0, self.transform_mode.findData(self.settings.transform_mode)
        ))
        self._loading = False

    def _write_controls(self, entity: RasterObject) -> None:
        entity.name = self.name.text().strip() or entity.name
        entity.visible = self.visible.isChecked()
        entity.opacity_locked = self.opacity_lock.isChecked()
        entity.opacity = (
            self.canvas.chapter.layers[entity.parent_layer_id].opacity
            if entity.opacity_locked else self.opacity.value() / 100.0
        )
        entity.ignore_parent_mask = self.ignore_parent_mask.isChecked()
        reference = self.geometry_reference.currentData()
        entity.geometry_reference = (
            str(reference)
            if reference in {"direct", "compound"} else "direct"
        )

    def _apply_discrete(self, *args) -> None:
        del args
        entity = self._selected()
        if self._loading or entity is None:
            return
        before = self.canvas.chapter.to_dict()
        self._write_controls(entity)
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(
                before, after, "Edit raster object"
            )
            self.canvas.hierarchyChanged.emit()
            self.canvas.documentChanged.emit(None)
            self.canvas.update()
            self.objectChanged.emit()
        self.refresh()

    def _begin_slider(self, key: str) -> None:
        if self._loading or self._selected() is None:
            return
        self._slider_before[key] = self.canvas.chapter.to_dict()

    def _slider_changed(self, key: str, value: int) -> None:
        label = self.opacity_value if key == "opacity" else self.underlay_value
        label.setText(f"{int(value)}%")
        entity = self._selected()
        if self._loading or entity is None:
            return
        before = (
            None if key in self._slider_before
            else self.canvas.chapter.to_dict()
        )
        if key == "opacity":
            if not entity.opacity_locked:
                entity.opacity = value / 100.0
        else:
            entity.underlay_opacity = value / 100.0
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        if before is not None:
            self._push_slider_change(before, key)

    def _finish_slider(self, key: str) -> None:
        before = self._slider_before.pop(key, None)
        if before is not None:
            self._push_slider_change(before, key)

    def _push_slider_change(self, before: dict, key: str) -> None:
        if self.canvas.chapter is None:
            return
        after = self.canvas.chapter.to_dict()
        if before != after:
            self.canvas.push_model_change(
                before, after,
                "Change raster opacity"
                if key == "opacity" else "Change raster underlay",
            )
            self.objectChanged.emit()
        self.canvas.interactionFinished.emit()

    def _transform_changed(self) -> None:
        if self._loading:
            return
        mode = str(self.transform_mode.currentData())
        if mode not in {"free", "uniform"}:
            return
        self.settings.transform_mode = mode
        self.settings.clamp()
        self.settingsChanged.emit()
