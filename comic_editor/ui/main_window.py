"""Standalone series/chapter application shell."""
from __future__ import annotations

import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QItemSelection, QItemSelectionModel, QModelIndex, QSignalBlocker, QTimer, Qt,
    Signal,
)
from PySide6.QtGui import QAction, QCloseEvent, QCursor, QKeySequence, QShortcut
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog, QHBoxLayout,
    QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QInputDialog, QLabel,
    QMainWindow, QMenu, QMessageBox, QPushButton, QSpinBox, QToolBar, QToolButton,
    QToolTip, QTreeView, QVBoxLayout, QWidget,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, TextObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import load_settings, save_settings
from comic_editor.ui.canvas import ToolKind, create_canvas
from comic_editor.ui.inspector import ContextInspector
from comic_editor.ui.layer_settings import LayerSettingsPanel
from comic_editor.ui.hotkeys_dialog import HotkeysDialog
from comic_editor.ui.pencil_settings_dialog import PencilSettingsDialog
from comic_editor.ui.preview import ChapterPreview
from comic_editor.ui.tree_model import HierarchyModel


class CollapsibleToolCategory(QWidget):
    """A single toolbar widget containing a persistent collapsible group."""

    expandedChanged = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.header = QToolButton(self)
        self.header.setText(title)
        self.header.setCheckable(True)
        self.header.setArrowType(Qt.RightArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.header)
        self.contents = QWidget(self)
        self.contents_layout = QVBoxLayout(self.contents)
        self.contents_layout.setContentsMargins(12, 0, 0, 0)
        self.contents_layout.setSpacing(2)
        self.contents.hide()
        layout.addWidget(self.contents)
        self.header.toggled.connect(self.setExpanded)

    def addTool(self, text: str) -> QToolButton:
        button = QToolButton(self.contents)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.contents_layout.addWidget(button)
        return button

    def isExpanded(self) -> bool:
        return self.header.isChecked()

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self.header.isChecked() != expanded:
            blocker = QSignalBlocker(self.header)
            self.header.setChecked(expanded)
            del blocker
        self.contents.setVisible(expanded)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.updateGeometry()
        self.expandedChanged.emit(expanded)


class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.resize(1500, 950)
        self.setWindowTitle("Vertical Comic Editor")
        self.settings = load_settings()
        self.settings.active_pencil_size = self.settings.pencil_default_size
        self.settings.active_eraser_size = self.settings.eraser_default_size
        self.repository: SeriesRepository | None = None
        self.series = None
        self.chapter: ChapterDocument | None = None
        self._dirty = False
        self._last_autosave = 0.0
        self._loading_chapter = False
        self._build_ui()
        self._connect()
        self._install_shortcuts()
        self._refresh_actions()
        self.statusBar().showMessage("Create or open a series")

    # ---- UI ------------------------------------------------------------
    def _build_ui(self) -> None:
        style = Path(__file__).with_name("style.qss")
        if style.is_file():
            QApplication.instance().setStyleSheet(style.read_text(encoding="utf-8"))

        self.file_toolbar = QToolBar("Project", self)
        self.file_toolbar.setMovable(False)
        self.addToolBar(self.file_toolbar)
        self.new_series_action = self.file_toolbar.addAction("New Series")
        self.open_series_action = self.file_toolbar.addAction("Open Series")
        self.save_action = self.file_toolbar.addAction("Save")
        self.file_toolbar.addSeparator()
        self.undo_action = self.file_toolbar.addAction("Undo")
        self.redo_action = self.file_toolbar.addAction("Redo")
        self.file_toolbar.addSeparator()
        self.chapter_combo = QComboBox()
        self.chapter_combo.setMinimumWidth(190)
        self.file_toolbar.addWidget(QLabel("Chapter"))
        self.file_toolbar.addWidget(self.chapter_combo)
        self.new_chapter_action = self.file_toolbar.addAction("New Chapter")
        self.trim_action = self.file_toolbar.addAction("Trim Height")
        self.fullscreen_action = self.file_toolbar.addAction("Fullscreen")
        self.hotkeys_action = self.file_toolbar.addAction("Hotkeys…")
        self.file_toolbar.addWidget(QLabel("Recent"))
        self.recent_combo = QComboBox()
        self.recent_combo.setMinimumWidth(180)
        self.recent_combo.addItem("Open recent…", "")
        for path in self.settings.recent_series or []:
            self.recent_combo.addItem(Path(path).name, path)
        self.file_toolbar.addWidget(self.recent_combo)

        self.tool_toolbar = QToolBar("Tools", self)
        self.tool_toolbar.setMovable(False)
        self.addToolBar(Qt.LeftToolBarArea, self.tool_toolbar)
        self.tool_buttons: dict[ToolKind, QToolButton] = {}
        labels = {
            ToolKind.OBJECT_SELECT: "Object Select",
            ToolKind.RASTER_PENCIL: "Raster Pencil",
            ToolKind.RASTER_ERASER: "Raster Eraser",
            ToolKind.TEXT_EDIT: "Text Edit",
            ToolKind.TRANSFORM: "Transform",
            ToolKind.SHAPE_EDIT: "Shape Edit",
        }
        for tool, label in labels.items():
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            button.clicked.connect(lambda checked=False, selected=tool: self._activate_tool(selected))
            self.tool_toolbar.addWidget(button)
            self.tool_buttons[tool] = button
        self.shapes_category = CollapsibleToolCategory("Shapes")
        self.tool_toolbar.addWidget(self.shapes_category)
        self.shape_tool_buttons: dict[ToolKind, QToolButton] = {}
        for label, tool in (
            ("Add Rectangle", ToolKind.BOX_BOUND),
            ("Add Circle", ToolKind.CIRCLE_BOUND),
            ("Add Shape", ToolKind.SHAPE_CREATE),
        ):
            option = self.shapes_category.addTool(label)
            option.clicked.connect(
                lambda checked=False, selected=tool:
                self._activate_tool(selected)
            )
            self.shape_tool_buttons[tool] = option
        self.tool_toolbar.addSeparator()
        self.add_text_button = QToolButton()
        self.add_text_button.setText("Add Text")
        self.add_raster_button = QToolButton()
        self.add_raster_button.setText("Add Raster")
        self.add_fill_button = QToolButton()
        self.add_fill_button.setText("Add Fill")
        self.tool_toolbar.addWidget(self.add_fill_button)
        self.tool_toolbar.addWidget(self.add_text_button)
        self.tool_toolbar.addWidget(self.add_raster_button)
        self.tool_toolbar.addSeparator()
        self.reset_view_button = QToolButton()
        self.reset_view_button.setText("Reset View")
        self.tool_toolbar.addWidget(self.reset_view_button)
        self.tablet_mode = QCheckBox("Tablet navigation")
        self.tablet_mode.setChecked(self.settings.tablet_mode)
        self.tool_toolbar.addWidget(self.tablet_mode)
        self.page_scope = QCheckBox("Select in page")
        self.page_scope.setChecked(self.settings.page_scope_select)
        self.tool_toolbar.addWidget(self.page_scope)
        self.snap_grid = QCheckBox("Snap to grid")
        self.snap_grid.setChecked(self.settings.snap_to_grid)
        self.tool_toolbar.addWidget(self.snap_grid)

        center = QWidget()
        center_layout = QHBoxLayout(center)
        center_layout.setContentsMargins(0, 0, 0, 0)
        center_layout.setSpacing(0)
        self.canvas = create_canvas(self.settings)
        self.preview = ChapterPreview(self.canvas)
        center_layout.addWidget(self.preview)
        center_layout.addWidget(self.canvas, 1)
        self.setCentralWidget(center)

        self.hierarchy_dock = QDockWidget("Layers and Objects", self)
        self.hierarchy_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        hierarchy_panel = QWidget()
        hierarchy_layout = QVBoxLayout(hierarchy_panel)
        hierarchy_layout.setContentsMargins(6, 6, 6, 6)
        self.layer_settings = LayerSettingsPanel(
            self.canvas, self.settings, save_settings, hierarchy_panel
        )
        hierarchy_layout.addWidget(self.layer_settings)
        self.tree = QTreeView()
        self.tree.setSelectionMode(QTreeView.SingleSelection)
        self.tree.setDragDropMode(QTreeView.InternalMove)
        self.tree.setDefaultDropAction(Qt.MoveAction)
        self.tree.setDropIndicatorShown(True)
        self.tree.setAlternatingRowColors(False)
        self.tree.setEditTriggers(
            QTreeView.EditKeyPressed | QTreeView.DoubleClicked
        )
        self.hierarchy_model = HierarchyModel()
        self.tree.setModel(self.hierarchy_model)
        self.tree.header().setStretchLastSection(False)
        self.tree.header().setSectionResizeMode(0, QHeaderView.Stretch)
        hierarchy_layout.addWidget(self.tree, 1)

        add_row = QHBoxLayout()
        self.add_page_button = QPushButton("+ Page")
        self.add_layer_button = QPushButton("+ Layer")
        for button in (self.add_page_button, self.add_layer_button):
            add_row.addWidget(button)
        hierarchy_layout.addLayout(add_row)
        self.delete_button = QPushButton("Delete Selected")
        hierarchy_layout.addWidget(self.delete_button)

        self.hierarchy_dock.setWidget(hierarchy_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.hierarchy_dock)

        self.inspector = ContextInspector(
            self.canvas, self.settings, save_settings, self.canvas
        )
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)

    def _connect(self) -> None:
        self.new_series_action.triggered.connect(self._create_series)
        self.open_series_action.triggered.connect(self._open_series_dialog)
        self.save_action.triggered.connect(self.save)
        self.new_chapter_action.triggered.connect(self._new_chapter)
        self.trim_action.triggered.connect(self._trim_height)
        self.fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self.hotkeys_action.triggered.connect(self._edit_hotkeys)
        self.recent_combo.activated.connect(self._open_recent)
        self.undo_action.triggered.connect(self.canvas.command_stack.undo)
        self.redo_action.triggered.connect(self.canvas.command_stack.redo)
        self.chapter_combo.currentIndexChanged.connect(self._chapter_selected)
        self.reset_view_button.clicked.connect(self.canvas.reset_view)
        self.preview.scrollRequested.connect(self.canvas.scroll_to_fraction)
        self.tablet_mode.toggled.connect(self._settings_changed)
        self.page_scope.toggled.connect(self._settings_changed)
        self.snap_grid.toggled.connect(self._settings_changed)
        self.canvas.documentChanged.connect(self._mark_dirty)
        self.canvas.hierarchyChanged.connect(self._hierarchy_changed)
        self.canvas.selectionChanged.connect(self._canvas_selection_changed)
        self.canvas.chapterReplaced.connect(self._chapter_replaced)
        self.canvas.cameraChanged.connect(self.inspector.reposition)
        self.canvas.toolChanged.connect(self._canvas_tool_changed)
        self.canvas.interactionFinished.connect(self.inspector.refresh)
        self.canvas.interactionFinished.connect(self.layer_settings.refresh)
        self.canvas.selectionCandidatesRequested.connect(
            self._show_selection_candidates
        )
        self.canvas.primitiveConversionRequested.connect(
            self._confirm_primitive_conversion
        )
        self.canvas.textEditingChanged.connect(
            self._set_text_shortcut_suppression
        )
        self.canvas.command_stack.changed_callback = self._command_stack_changed
        self.inspector.changed.connect(self._hierarchy_changed)
        self.inspector.pencilPresetSelected.connect(
            self._pencil_preset_selected
        )
        self.inspector.pencilSettingsRequested.connect(
            self._edit_pencil_settings
        )
        self.inspector.brushSizeSelected.connect(
            self._brush_size_selected
        )
        self.inspector.brushSizesRequested.connect(self._edit_brush_sizes)
        self.inspector.eraserShapeChanged.connect(
            self._eraser_shape_selected
        )
        self.hierarchy_model.mutationCommitted.connect(self._tree_mutated)
        self.tree.selectionModel().selectionChanged.connect(self._tree_selection_changed)
        self.autosave_timer.timeout.connect(self._autosave)

        self.add_page_button.clicked.connect(self._add_page)
        self.add_layer_button.clicked.connect(self._add_layer)
        self.add_raster_button.clicked.connect(self._add_raster)
        self.add_text_button.clicked.connect(self._add_text)
        self.add_fill_button.clicked.connect(self._add_fill)
        self.delete_button.clicked.connect(self._delete_selected)

    def _install_shortcuts(self) -> None:
        shortcuts = {
            "raster_pencil": ToolKind.RASTER_PENCIL,
            "raster_eraser": ToolKind.RASTER_ERASER,
            "object_select": ToolKind.OBJECT_SELECT,
            "transform": ToolKind.TRANSFORM,
            "shape_edit": ToolKind.SHAPE_EDIT,
        }
        self._shortcuts = []
        self._shortcut_sequences: list[tuple[QShortcut, QKeySequence]] = []
        for action_id, tool in shortcuts.items():
            shortcut = QShortcut(QKeySequence(self.settings.hotkeys[action_id]), self)
            shortcut.activated.connect(lambda selected=tool: self._activate_tool(selected))
            self._shortcuts.append(shortcut)
            self._shortcut_sequences.append((shortcut, shortcut.key()))
        save_shortcut = QShortcut(QKeySequence(self.settings.hotkeys["save"]), self)
        save_shortcut.activated.connect(self.save)
        undo_shortcut = QShortcut(QKeySequence(self.settings.hotkeys["undo"]), self)
        undo_shortcut.activated.connect(self.canvas.command_stack.undo)
        redo_shortcut = QShortcut(QKeySequence(self.settings.hotkeys["redo"]), self)
        redo_shortcut.activated.connect(self.canvas.command_stack.redo)
        fullscreen_shortcut = QShortcut(QKeySequence("Alt+Return"), self)
        fullscreen_shortcut.activated.connect(self._toggle_fullscreen)
        self._shortcuts.extend([save_shortcut, undo_shortcut, redo_shortcut, fullscreen_shortcut])
        reset_shortcut = QShortcut(QKeySequence(self.settings.hotkeys["reset_view"]), self)
        reset_shortcut.activated.connect(self.canvas.reset_view)
        grid_shortcut = QShortcut(QKeySequence(self.settings.hotkeys["toggle_grid"]), self)
        grid_shortcut.activated.connect(self._toggle_grid)
        self._shortcuts.extend([reset_shortcut, grid_shortcut])
        for shortcut in (
            save_shortcut, undo_shortcut, redo_shortcut,
            fullscreen_shortcut, reset_shortcut, grid_shortcut,
        ):
            self._shortcut_sequences.append((shortcut, shortcut.key()))

    # ---- series and chapters ------------------------------------------
    @staticmethod
    def _folder_name(name: str) -> str:
        result = re.sub(r"[^A-Za-z0-9._-]+", "-", name.strip()).strip("-")
        return result or "untitled-series"

    def _create_series(self) -> None:
        parent = QFileDialog.getExistingDirectory(self, "Choose parent folder for the series")
        if not parent:
            return
        name, accepted = QInputDialog.getText(self, "New Series", "Series name")
        if not accepted or not name.strip():
            return
        root = Path(parent) / self._folder_name(name)
        try:
            repository = SeriesRepository(root)
            series = repository.create(name)
            chapter, tiles = repository.create_chapter(series, "Chapter 1")
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to create series", str(error))
            return
        self._adopt_series(repository, series)
        self._set_chapter(chapter, tiles)
        self._remember_series(root)

    def _open_series_dialog(self) -> None:
        root = QFileDialog.getExistingDirectory(self, "Open series folder")
        if root:
            self.open_series(root)

    def open_series(self, root: str | Path) -> bool:
        if not self._confirm_discard_or_save():
            return False
        try:
            repository = SeriesRepository(root)
            series = repository.load_series()
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to open series", str(error))
            return False
        self._adopt_series(repository, series)
        self._remember_series(repository.root)
        if series.chapters:
            self._load_chapter(series.chapters[0].chapter_id)
        return True

    def _open_recent(self, index: int) -> None:
        path = self.recent_combo.itemData(index)
        if path:
            self.open_series(path)
        self.recent_combo.blockSignals(True)
        self.recent_combo.setCurrentIndex(0)
        self.recent_combo.blockSignals(False)

    def _adopt_series(self, repository: SeriesRepository, series) -> None:
        self.repository, self.series = repository, series
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        for reference in series.chapters:
            self.chapter_combo.addItem(reference.name, reference.chapter_id)
        self.chapter_combo.blockSignals(False)
        self.setWindowTitle(f"{series.name} — Vertical Comic Editor")
        self._refresh_actions()

    def _new_chapter(self) -> None:
        if self.repository is None or self.series is None:
            return
        if not self._save_if_dirty():
            return
        name, accepted = QInputDialog.getText(
            self, "New Chapter", "Chapter name",
            text=f"Chapter {len(self.series.chapters) + 1}",
        )
        if not accepted or not name.strip():
            return
        try:
            chapter, tiles = self.repository.create_chapter(self.series, name)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to create chapter", str(error))
            return
        self.chapter_combo.addItem(chapter.name, chapter.chapter_id)
        self.chapter_combo.setCurrentIndex(self.chapter_combo.count() - 1)
        self._set_chapter(chapter, tiles)

    def _chapter_selected(self, index: int) -> None:
        if self._loading_chapter or index < 0:
            return
        chapter_id = self.chapter_combo.itemData(index)
        if self.chapter and chapter_id == self.chapter.chapter_id:
            return
        if not self._save_if_dirty():
            self._sync_chapter_combo()
            return
        self._load_chapter(chapter_id)

    def _load_chapter(self, chapter_id: str) -> None:
        if self.repository is None:
            return
        recover = False
        if self.repository.has_recovery(chapter_id):
            recover = QMessageBox.question(
                self, "Recover autosave",
                "A newer autosave exists for this chapter. Recover it?",
            ) == QMessageBox.Yes
        try:
            chapter, tiles = self.repository.load_chapter(chapter_id, recover=recover)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to open chapter", str(error))
            return
        self._set_chapter(chapter, tiles)
        if recover:
            self._mark_dirty(None)

    def _set_chapter(self, chapter, tiles) -> None:
        self.chapter = chapter
        self.canvas.set_document(chapter, tiles)
        self.hierarchy_model.set_chapter(chapter)
        self._dirty = False
        self._last_autosave = 0
        self._sync_chapter_combo()
        raster = next(
            (obj for obj in chapter.objects.values() if isinstance(obj, RasterObject)),
            None,
        )
        if raster:
            self.canvas.set_selection("object", raster.object_id)
        self._refresh_actions()
        self.statusBar().showMessage(f"{chapter.name} — {chapter.width} × {chapter.height}px")

    def _sync_chapter_combo(self) -> None:
        if not self.chapter:
            return
        index = self.chapter_combo.findData(self.chapter.chapter_id)
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.setCurrentIndex(index)
        self.chapter_combo.blockSignals(False)

    # ---- editing actions ----------------------------------------------
    def _selected_parent_layer(self, allow_page: bool = True):
        if self.chapter is None:
            return None
        if self.canvas.selected_kind == "layer":
            layer = self.chapter.layers.get(self.canvas.selected_id)
        elif self.canvas.selected_kind == "object":
            obj = self.chapter.objects.get(self.canvas.selected_id)
            layer = self.chapter.layers.get(obj.parent_layer_id) if obj else None
        else:
            layer = None
        if (
            layer and layer.layer_kind not in {"fill", "open_shape"}
            and (allow_page or not layer.is_page)
        ):
            return layer
        return None

    def _add_page(self) -> None:
        if self.chapter is None:
            return
        before = self.chapter.to_dict()
        y = self.chapter.minimum_safe_height() + (120 if self.chapter.root_page_ids else 0)
        page = self.chapter.add_page(
            f"Page {len(self.chapter.root_page_ids) + 1}",
            BoundGeometry.rectangle(0, 0, 1080, 1080), y=y,
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add page")
        self._after_structure(page.layer_id, "layer")

    def _add_layer(self) -> None:
        parent = self._selected_parent_layer()
        if parent is None:
            self.statusBar().showMessage("Select a page or layer first", 4000)
            return
        before = self.chapter.to_dict()
        x, y, width, height = parent.bound.bbox()
        layer = self.chapter.add_layer(
            parent.layer_id, self._next_layer_name(),
            BoundGeometry.rectangle(x, y, max(64, width), max(64, height)),
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add layer")
        self._after_structure(layer.layer_id, "layer")

    def _add_raster(self) -> None:
        parent = self._selected_parent_layer(allow_page=False)
        if parent is None:
            self.statusBar().showMessage("Raster objects require a selected non-page layer", 4000)
            return
        if self.canvas.begin_raster_creation(parent.layer_id):
            message = (
                "Drag a box on the canvas to set the raster width and height. "
                "Escape cancels."
            )
            self.statusBar().showMessage(message, 7000)
            QToolTip.showText(QCursor.pos(), message, self)

    def _add_fill(self) -> None:
        parent = self._selected_parent_layer()
        if parent is None or parent.layer_kind == "fill":
            self.statusBar().showMessage(
                "Select a page or bounded layer first", 4000
            )
            return
        before = self.chapter.to_dict()
        count = sum(
            item.layer_kind == "fill" for item in self.chapter.layers.values()
        ) + 1
        layer = self.chapter.add_fill_layer(
            parent.layer_id, f"Fill {count}", self.settings.brush_color
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add fill layer")
        self._after_structure(layer.layer_id, "layer")

    def _add_text(self) -> None:
        parent = self._selected_parent_layer(allow_page=False)
        if parent is None:
            self.statusBar().showMessage("Text objects require a selected non-page layer", 4000)
            return
        before = self.chapter.to_dict()
        left, top, width, height = parent.bound.bbox()
        count = sum(isinstance(item, TextObject) for item in self.chapter.objects.values()) + 1
        obj = TextObject(name=f"Text {count}", text="Text", x=0, y=0)
        preset = next(
            (
                item for item in self.settings.text_presets
                if item["name"] == self.settings.active_text_preset
            ),
            self.settings.text_presets[0],
        )
        for key in (
            "font_family", "font_size", "bold", "italic", "kerning",
            "layout_mode", "horizontal_alignment", "vertical_alignment", "margin",
        ):
            setattr(obj, key, preset[key])
        if obj.layout_mode == "free":
            obj.transform_quad = [
                (left + (width - obj.width) / 2, top + (height - obj.height) / 2),
                (left + (width + obj.width) / 2, top + (height - obj.height) / 2),
                (left + (width + obj.width) / 2, top + (height + obj.height) / 2),
                (left + (width - obj.width) / 2, top + (height + obj.height) / 2),
            ]
        self.chapter.add_object(parent.layer_id, obj)
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add text object")
        self._after_structure(parent.layer_id, "layer")
        self.canvas.set_tool(ToolKind.OBJECT_SELECT)

    def _next_layer_name(self) -> str:
        numbers = []
        for layer in self.chapter.layers.values():
            match = re.fullmatch(r"Layer\s+(\d+)", layer.name)
            if match:
                numbers.append(int(match.group(1)))
        return f"Layer {max(numbers, default=0) + 1}"

    def _delete_selected(self) -> None:
        if self.chapter is None or not self.canvas.selected_id:
            return
        if QMessageBox.question(
            self, "Delete selection",
            "Delete the selected entity and all of its descendants?",
        ) != QMessageBox.Yes:
            return
        before = self.chapter.to_dict()
        deleted = self.chapter.delete_entity(
            self.canvas.selected_kind, self.canvas.selected_id
        )
        # Keep detached tiles in memory until the undo stack is discarded.
        # Persistence writes only referenced objects and removes stale folders.
        after = self.chapter.to_dict()
        self.canvas.selected_id = ""
        self.canvas.selected_kind = ""
        self.canvas.push_model_change(before, after, "Delete entity")
        self._after_structure("", "")

    def _after_structure(self, entity_id: str, kind: str) -> None:
        self._refresh_hierarchy()
        if entity_id:
            self.canvas.set_selection(kind, entity_id)
        self.canvas.hierarchyChanged.emit()
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        self._refresh_actions()

    def _trim_height(self) -> None:
        if self.chapter is None:
            return
        minimum = self.chapter.minimum_safe_height()
        for object_id in self.chapter.objects:
            rect = self.canvas.object_world_rect(object_id)
            if rect is not None:
                minimum = max(minimum, int(rect.bottom() + 0.999))
        value, accepted = QInputDialog.getInt(
            self, "Trim chapter", f"New height (minimum {minimum}px)",
            max(minimum, self.chapter.height), minimum, 10_000_000,
        )
        if not accepted or value == self.chapter.height:
            return
        before = self.chapter.to_dict()
        try:
            self.chapter.trim_height(value)
        except ValueError as error:
            QMessageBox.warning(self, "Cannot trim", str(error))
            return
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Trim chapter")
        self.canvas.hierarchyChanged.emit()
        self._mark_dirty(None)

    def _activate_tool(self, tool: ToolKind) -> None:
        if tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            if self.chapter and self.canvas.selected_kind == "layer":
                layer = self.chapter.layers[self.canvas.selected_id]
                raster_id = layer.last_raster_id
                if raster_id in self.chapter.objects:
                    self.canvas.set_selection("object", raster_id)
            if not self.canvas.set_tool(tool):
                self.statusBar().showMessage(
                    "Select a raster object, or create one in the selected layer", 4000
                )
                self._sync_tool_buttons()
                return
        else:
            self.canvas.set_tool(tool)
        self._sync_tool_buttons()

    def _confirm_primitive_conversion(self, primitive: str) -> None:
        label = "Rectangle" if primitive == "rectangle" else "Circle"
        dialog = QMessageBox(self)
        dialog.setWindowTitle(f"Convert {label} to Shape?")
        dialog.setText(
            f"Adding a point will convert this {label.lower()} into a custom "
            "shape. Its current appearance will be preserved."
        )
        convert = dialog.addButton("Convert to Shape", QMessageBox.AcceptRole)
        dialog.addButton(QMessageBox.Cancel)
        dialog.exec()
        self.canvas.resolve_primitive_conversion(
            dialog.clickedButton() is convert
        )

    def _sync_tool_buttons(self) -> None:
        for tool, button in self.tool_buttons.items():
            button.blockSignals(True)
            button.setChecked(tool == self.canvas.tool)
            button.blockSignals(False)
        raster_selected = (
            self.chapter is not None and self.canvas.selected_kind == "object"
            and isinstance(self.chapter.objects.get(self.canvas.selected_id), RasterObject)
        )
        self.tool_buttons[ToolKind.RASTER_PENCIL].setEnabled(raster_selected)
        self.tool_buttons[ToolKind.RASTER_ERASER].setEnabled(raster_selected)
        self.tool_buttons[ToolKind.RASTER_PENCIL].setVisible(raster_selected)
        self.tool_buttons[ToolKind.RASTER_ERASER].setVisible(raster_selected)
        text_selected = (
            self.chapter is not None and self.canvas.selected_kind == "object"
            and isinstance(self.chapter.objects.get(self.canvas.selected_id), TextObject)
        )
        self.tool_buttons[ToolKind.TEXT_EDIT].setVisible(self.chapter is not None)
        transform_available = raster_selected or (
            text_selected
            and self.chapter.objects[self.canvas.selected_id].layout_mode == "free"
        )
        self.tool_buttons[ToolKind.TRANSFORM].setVisible(transform_available)

    # ---- selection and model synchronization --------------------------
    def _tree_selection_changed(self, selected: QItemSelection, deselected) -> None:
        indexes = selected.indexes()
        index = next((item for item in indexes if item.column() == 0), QModelIndex())
        if not index.isValid():
            return
        item = self.hierarchy_model.item_for_index(index)
        self.canvas.set_selection(
            item.kind, item.entity_id, activate_default_tool=True
        )

    def _show_selection_candidates(self, object_ids, global_point) -> None:
        if self.chapter is None:
            return
        menu = QMenu(self)
        for object_id in object_ids:
            obj = self.chapter.objects.get(object_id)
            if obj is None:
                continue
            parent = self.chapter.layers[obj.parent_layer_id]
            action = menu.addAction(
                f"{obj.name}  ·  {obj.object_type.title()}  ·  {parent.name}"
            )
            action.triggered.connect(
                lambda checked=False, selected=object_id:
                self.canvas.set_selection(
                    "object", selected, activate_default_tool=True
                )
            )
        if not menu.isEmpty():
            menu.exec(global_point)

    def _canvas_selection_changed(self, kind: str, entity_id: str) -> None:
        if entity_id:
            index = self.hierarchy_model.index_for_entity(kind, entity_id)
            if index.isValid():
                blocker = QSignalBlocker(self.tree.selectionModel())
                self.tree.selectionModel().select(
                    index, QItemSelectionModel.ClearAndSelect
                    | QItemSelectionModel.Rows
                )
                self.tree.scrollTo(index)
                del blocker
        self.inspector.refresh()
        self.layer_settings.refresh()
        self._sync_tool_buttons()
        self._refresh_actions()

    def _canvas_tool_changed(self, tool: ToolKind) -> None:
        if tool == ToolKind.OBJECT_SELECT:
            self.inspector.hide()
        else:
            self.inspector.refresh()
        self._sync_tool_buttons()

    def _tree_mutated(self, before: dict, after: dict, label: str) -> None:
        self.canvas.push_model_change(before, after, label)
        self.chapter = self.canvas.chapter
        self.canvas.documentChanged.emit(None)
        self.canvas.update()
        self._mark_dirty(None)

    def _chapter_replaced(self, chapter: ChapterDocument) -> None:
        self.chapter = chapter
        self._refresh_hierarchy()
        self.inspector.refresh()
        self.layer_settings.refresh()
        self.preview.invalidate_all()
        self._sync_tool_buttons()
        self._mark_dirty(None)

    def _hierarchy_changed(self) -> None:
        if self.chapter is not self.canvas.chapter:
            self.chapter = self.canvas.chapter
        self._refresh_hierarchy()
        self.inspector.refresh()
        self.layer_settings.refresh()
        self.preview.invalidate_all()
        self._sync_tool_buttons()

    def _expanded_layer_ids(self) -> set[str]:
        result: set[str] = set()
        if self.chapter is None:
            return result
        for layer_id in self.chapter.layers:
            index = self.hierarchy_model.index_for_entity("layer", layer_id)
            if index.isValid() and self.tree.isExpanded(index):
                result.add(layer_id)
        return result

    def _refresh_hierarchy(self) -> None:
        expanded = self._expanded_layer_ids()
        selected = (self.canvas.selected_kind, self.canvas.selected_id)
        blocker = QSignalBlocker(self.tree.selectionModel())
        self.hierarchy_model.set_chapter(self.chapter)
        for layer_id in expanded:
            index = self.hierarchy_model.index_for_entity("layer", layer_id)
            if index.isValid():
                self.tree.setExpanded(index, True)
        if selected[1]:
            index = self.hierarchy_model.index_for_entity(*selected)
            if index.isValid():
                self.tree.selectionModel().select(
                    index,
                    QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
                )
        del blocker

    # ---- saving, autosave, settings -----------------------------------
    def _mark_dirty(self, dirty_rect) -> None:
        if self.chapter is None:
            return
        self._dirty = True
        self.autosave_timer.start(2000)
        self._refresh_actions()
        self.inspector.reposition()

    def _command_stack_changed(self) -> None:
        self._mark_dirty(None)
        self._refresh_actions()

    def save(self) -> bool:
        if self.repository is None or self.chapter is None:
            return False
        try:
            self.repository.save_chapter(self.chapter, self.canvas.tiles)
            for reference in self.series.chapters:
                if reference.chapter_id == self.chapter.chapter_id:
                    reference.name = self.chapter.name
            self.repository.save_series(self.series)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        self._dirty = False
        self.autosave_timer.stop()
        self.statusBar().showMessage("Saved", 3000)
        self._refresh_actions()
        return True

    def _autosave(self) -> None:
        if not self._dirty or self.repository is None or self.chapter is None:
            return
        elapsed = time.monotonic() - self._last_autosave
        if self._last_autosave and elapsed < 30:
            self.autosave_timer.start(round((30 - elapsed) * 1000))
            return
        try:
            self.repository.save_chapter(self.chapter, self.canvas.tiles, autosave=True)
            self._last_autosave = time.monotonic()
            self.statusBar().showMessage("Recovery autosave updated", 2000)
        except (OSError, ValueError) as error:
            self.statusBar().showMessage(f"Autosave failed: {error}", 7000)

    def _settings_changed(self, *args) -> None:
        self.settings.tablet_mode = self.tablet_mode.isChecked()
        self.settings.page_scope_select = self.page_scope.isChecked()
        self.settings.snap_to_grid = self.snap_grid.isChecked()
        save_settings(self.settings)
        self.canvas.update()

    def _pencil_preset_selected(self, name: str) -> None:
        if not name:
            return
        self.settings.active_pencil_preset = name
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()

    def _brush_size_selected(self, tool: str, size: str) -> None:
        if size not in {"small", "medium", "large"}:
            return
        if tool == ToolKind.RASTER_PENCIL.value:
            self.settings.active_pencil_size = size
        elif tool == ToolKind.RASTER_ERASER.value:
            self.settings.active_eraser_size = size
        else:
            return
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()

    def _eraser_shape_selected(self, square: bool) -> None:
        self.settings.eraser_square = bool(square)
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()

    def _edit_pencil_settings(self) -> None:
        dialog = PencilSettingsDialog(
            self.settings.pencil_presets,
            self.settings.active_pencil_preset,
            self,
        )
        dialog.committedPresets.connect(self._commit_pencil_presets)
        dialog.exec()

    def _commit_pencil_presets(self, presets, active_name: str) -> None:
        self.settings.pencil_presets = presets
        self.settings.active_pencil_preset = active_name
        self.settings.clamp()
        save_settings(self.settings)
        self.inspector.refresh_brush_controls()
        self.canvas.refresh_brush_settings()

    def _edit_brush_sizes(self) -> None:
        dialog = QDialog(self)
        dialog.setWindowTitle("Brush size presets")
        layout = QVBoxLayout(dialog)
        form = QFormLayout()
        controls: dict[tuple[str, str], QSpinBox] = {}
        default_combos: dict[str, QComboBox] = {}
        for kind, values, low, high in (
            ("Pencil", self.settings.pencil_size_px, 1, 200),
            ("Eraser", self.settings.eraser_size_px, 2, 400),
        ):
            for key in ("small", "medium", "large"):
                control = QSpinBox()
                control.setRange(low, high)
                control.setValue(values[key])
                controls[(kind, key)] = control
                form.addRow(f"{kind} {key.title()}", control)
            default_combo = QComboBox()
            for key in ("small", "medium", "large"):
                default_combo.addItem(key.title(), key)
            current_default = (
                self.settings.pencil_default_size
                if kind == "Pencil" else self.settings.eraser_default_size
            )
            default_combo.setCurrentIndex(
                default_combo.findData(current_default)
            )
            default_combos[kind] = default_combo
            form.addRow(f"{kind} default", default_combo)
        layout.addLayout(form)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Save | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            return
        self.settings.pencil_size_px = {
            key: controls[("Pencil", key)].value()
            for key in ("small", "medium", "large")
        }
        self.settings.eraser_size_px = {
            key: controls[("Eraser", key)].value()
            for key in ("small", "medium", "large")
        }
        self.settings.pencil_default_size = default_combos[
            "Pencil"
        ].currentData()
        self.settings.eraser_default_size = default_combos[
            "Eraser"
        ].currentData()
        self.settings.active_pencil_size = self.settings.pencil_default_size
        self.settings.active_eraser_size = self.settings.eraser_default_size
        self.settings.brush_size = self.settings.pencil_size_px["medium"]
        self.settings.eraser_size = self.settings.eraser_size_px["medium"]
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()
        self.inspector.refresh_brush_controls()

    def _set_text_shortcut_suppression(self, editing: bool) -> None:
        for shortcut, sequence in self._shortcut_sequences:
            display = sequence.toString(QKeySequence.PortableText)
            key_name = display.rsplit("+", 1)[-1].strip()
            contains_letter = len(key_name) == 1 and key_name.isalpha()
            contains_shift = "shift" in display.casefold()
            shortcut.setEnabled(not (
                editing and (contains_letter or contains_shift)
            ))

    def _toggle_grid(self) -> None:
        if self.chapter is None:
            return
        before = self.chapter.to_dict()
        self.chapter.grid.enabled = not self.chapter.grid.enabled
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Toggle grid")
        self.canvas.documentChanged.emit(None)
        self.canvas.update()

    def _edit_hotkeys(self) -> None:
        dialog = HotkeysDialog(self.settings.hotkeys, self)
        if dialog.exec() != dialog.Accepted:
            return
        try:
            self.settings.hotkeys = dialog.bindings()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid hotkeys", str(error))
            return
        save_settings(self.settings)
        for shortcut in self._shortcuts:
            shortcut.setParent(None)
            shortcut.deleteLater()
        self._install_shortcuts()

    def _remember_series(self, path: Path) -> None:
        paths = [str(path), *(self.settings.recent_series or [])]
        self.settings.recent_series = list(dict.fromkeys(paths))[:12]
        save_settings(self.settings)
        self.recent_combo.blockSignals(True)
        self.recent_combo.clear()
        self.recent_combo.addItem("Open recent…", "")
        for recent in self.settings.recent_series:
            self.recent_combo.addItem(Path(recent).name, recent)
        self.recent_combo.blockSignals(False)

    def _save_if_dirty(self) -> bool:
        return not self._dirty or self.save()

    def _confirm_discard_or_save(self) -> bool:
        if not self._dirty:
            return True
        answer = QMessageBox.question(
            self, "Unsaved changes", "Save the current chapter before continuing?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Save,
        )
        if answer == QMessageBox.Cancel:
            return False
        return self.save() if answer == QMessageBox.Save else True

    def _refresh_actions(self) -> None:
        active = self.chapter is not None
        self.save_action.setEnabled(active and self._dirty)
        self.new_chapter_action.setEnabled(self.series is not None)
        self.trim_action.setEnabled(active)
        self.undo_action.setEnabled(self.canvas.command_stack.can_undo)
        self.redo_action.setEnabled(self.canvas.command_stack.can_redo)
        self.delete_button.setEnabled(active and bool(self.canvas.selected_id))
        self.add_page_button.setEnabled(active)
        self.add_layer_button.setEnabled(active)
        self.add_raster_button.setEnabled(active)
        self.add_text_button.setEnabled(active)
        self.add_fill_button.setEnabled(active)
        self._sync_tool_buttons()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._confirm_discard_or_save():
            event.ignore()
            return
        save_settings(self.settings)
        event.accept()
