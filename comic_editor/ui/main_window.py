"""Standalone series/chapter application shell."""
from __future__ import annotations

import re
import time
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication, QEvent, QItemSelection, QItemSelectionModel, QModelIndex,
    QPointF, QSignalBlocker, QTimer, Qt, Signal,
)
from PySide6.QtGui import (
    QAction, QCloseEvent, QCursor, QKeySequence, QMouseEvent, QShortcut,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDockWidget, QFileDialog, QHBoxLayout,
    QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTabWidget, QTextEdit,
    QToolBar, QToolButton, QToolTip, QTreeView, QVBoxLayout, QWidget, QFrame,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientRampPreset, ColorGradientStop,
    ColorPalette, GradientObject, PaletteSwatch,
    RasterObject, TextObject, VectorDrawingObject, VectorFillObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import load_settings, save_settings
from comic_editor.ui.canvas import ToolKind, create_canvas
from comic_editor.ui.inspector import ContextInspector
from comic_editor.ui.color_picker import (
    PaletteEditorWidget, PrimarySecondaryColorPanel, canonical_argb,
)
from comic_editor.ui.layer_settings import LayerSettingsPanel
from comic_editor.ui.hotkeys_dialog import HotkeysDialog
from comic_editor.ui.hotkeys import (
    MODIFIER_LABELS, chord_keys, chord_text,
)
from comic_editor.ui.gradient_tools import (
    BUILTIN_PRIMARY_SECONDARY_ID, GradientToolsControls,
)
from comic_editor.ui.pencil_settings_dialog import PencilSettingsDialog
from comic_editor.ui.preview import ChapterPreview
from comic_editor.ui.ribbon import RibbonWidget
from comic_editor.ui.tool_ribbon_pages import (
    RasterObjectControls, ToolSettingsControls, VectorToolsControls,
)
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
        self.header.setObjectName("shapeCategoryHeader")
        self.header.setText(title)
        self.header.setCheckable(False)
        self.header.setAutoRaise(True)
        self.header.setArrowType(Qt.RightArrow)
        self.header.setToolButtonStyle(Qt.ToolButtonTextBesideIcon)
        layout.addWidget(self.header)
        self.contents = QWidget(self)
        self.contents_layout = QVBoxLayout(self.contents)
        self.contents_layout.setContentsMargins(12, 0, 0, 0)
        self.contents_layout.setSpacing(2)
        self.contents.hide()
        layout.addWidget(self.contents)
        self._expanded = False
        self.header.clicked.connect(
            lambda checked=False: self.setExpanded(not self._expanded)
        )

    def addTool(self, text: str) -> QToolButton:
        button = QToolButton(self.contents)
        button.setText(text)
        button.setToolButtonStyle(Qt.ToolButtonTextOnly)
        self.contents_layout.addWidget(button)
        return button

    def isExpanded(self) -> bool:
        return self._expanded

    def setExpanded(self, expanded: bool) -> None:
        expanded = bool(expanded)
        if self._expanded == expanded:
            return
        self._expanded = expanded
        self.contents.setVisible(expanded)
        self.header.setArrowType(Qt.DownArrow if expanded else Qt.RightArrow)
        self.updateGeometry()
        self.expandedChanged.emit(expanded)


class ScrollableToolPanel(QWidget):
    """A vertically scrolling tool column with toolbar-compatible helpers."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toolPanel")
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("toolPanelScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.contents = QWidget(self.scroll_area)
        self.contents.setObjectName("toolPanelContents")
        self.contents_layout = QVBoxLayout(self.contents)
        self.contents_layout.setContentsMargins(6, 6, 6, 6)
        self.contents_layout.setSpacing(4)
        self.contents_layout.addStretch(1)
        self.scroll_area.setWidget(self.contents)
        outer.addWidget(self.scroll_area)

    def addWidget(self, widget: QWidget) -> QWidget:  # noqa: N802
        self.contents_layout.insertWidget(
            self.contents_layout.count() - 1, widget
        )
        return widget

    def addSeparator(self) -> QFrame:  # noqa: N802
        separator = QFrame(self.contents)
        separator.setObjectName("toolPanelSeparator")
        separator.setFrameShape(QFrame.Shape.HLine)
        separator.setFrameShadow(QFrame.Shadow.Plain)
        self.addWidget(separator)
        return separator


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

        self.tool_toolbar = ScrollableToolPanel(self)
        self.tool_toolbar.setMinimumHeight(120)
        self.tool_buttons: dict[ToolKind, QToolButton] = {}
        labels = [
            (ToolKind.OBJECT_SELECT, "Object Select"),
            (ToolKind.RASTER_PENCIL, "Pencil"),
            (ToolKind.RASTER_ERASER, "Eraser"),
        ]
        fill_tool = getattr(ToolKind, "FILL", None)
        if fill_tool is not None:
            labels.append((fill_tool, "Fill"))
        labels.extend([
            (ToolKind.TEXT_EDIT, "Text Edit"),
            (ToolKind.TRANSFORM, "Transform"),
            (ToolKind.SHAPE_EDIT, "Shape Edit"),
            (ToolKind.INSERT_PAGE_GAP, "Insert Page Gap"),
        ])
        for tool, label in labels:
            button = QToolButton()
            button.setText(label)
            button.setCheckable(True)
            button.setToolButtonStyle(Qt.ToolButtonTextOnly)
            button.clicked.connect(lambda checked=False, selected=tool: self._activate_tool(selected))
            self.tool_toolbar.addWidget(button)
            self.tool_buttons[tool] = button
        if fill_tool is not None:
            self.fill_tool_button = self.tool_buttons[fill_tool]
        else:
            # Keeps the UI importable while a renderer without vector/fill
            # support is being upgraded in-place.
            self.fill_tool_button = QToolButton()
            self.fill_tool_button.setText("Fill")
            self.fill_tool_button.setCheckable(True)
            self.fill_tool_button.clicked.connect(
                lambda checked=False: self._activate_named_tool("FILL")
            )
            self.tool_toolbar.addWidget(self.fill_tool_button)
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
        self.drawing_selection_category = CollapsibleToolCategory(
            "Drawing Selection"
        )
        self.tool_toolbar.addWidget(self.drawing_selection_category)
        self.drawing_selection_buttons: dict[ToolKind, QToolButton] = {}
        for label, tool in (
            ("Rectangle Select", ToolKind.DRAW_SELECT_RECT),
            ("Lasso Select", ToolKind.DRAW_SELECT_LASSO),
            ("Stroke Select", ToolKind.DRAW_SELECT_STROKE),
        ):
            option = self.drawing_selection_category.addTool(label)
            option.setCheckable(True)
            option.clicked.connect(
                lambda checked=False, selected=tool:
                self._activate_tool(selected)
            )
            self.drawing_selection_buttons[tool] = option
        self.tool_toolbar.addSeparator()
        self.add_text_button = QToolButton()
        self.add_text_button.setText("Add Text")
        self.add_raster_button = QToolButton()
        self.add_raster_button.setText("Add Raster")
        self.add_vector_button = QToolButton()
        self.add_vector_button.setText("Add Vector Drawing")
        self.add_fill_button = QToolButton()
        self.add_fill_button.setText("Add Fill")
        self.tool_toolbar.addWidget(self.add_fill_button)
        self.tool_toolbar.addWidget(self.add_text_button)
        self.tool_toolbar.addWidget(self.add_raster_button)
        self.tool_toolbar.addWidget(self.add_vector_button)
        self.tool_toolbar.addSeparator()
        self.reset_view_button = QToolButton()
        self.reset_view_button.setText("Reset View")
        self.tool_toolbar.addWidget(self.reset_view_button)
        self.tablet_mode = QCheckBox("Tablet navigation")
        self.tablet_mode.setChecked(self.settings.tablet_mode)
        self.tool_toolbar.addWidget(self.tablet_mode)
        self.page_scope = QCheckBox("Select in page")
        self.page_scope.setChecked(self.settings.page_scope_select)
        # Kept as an attribute for compatibility with older integrations;
        # entity selection now always searches the complete chapter.
        self.page_scope.hide()
        self.snap_grid = QCheckBox("Snap to grid")
        self.snap_grid.setChecked(self.settings.snap_to_grid)
        self.tool_toolbar.addWidget(self.snap_grid)

        self.tool_toolbar.addSeparator()

        self.color_tabs = QTabWidget(self)
        self.color_tabs.setObjectName("colorTabs")
        self.color_tabs.setMinimumHeight(225)
        picker_page = QWidget(self.color_tabs)
        picker_layout = QVBoxLayout(picker_page)
        picker_layout.setContentsMargins(4, 4, 4, 4)
        self.color_panel = PrimarySecondaryColorPanel(
            "#FF000000", "#FFFFFFFF", picker_page
        )
        self.color_panel.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.color_panel.picker.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        picker_layout.addWidget(self.color_panel)
        self.color_tabs.addTab(picker_page, "Picker")

        palette_page = QWidget(self.color_tabs)
        palette_layout = QVBoxLayout(palette_page)
        palette_layout.setContentsMargins(4, 4, 4, 4)
        self.palette_editor = PaletteEditorWidget(palette_page)
        self.palette_editor.setMinimumWidth(0)
        palette_layout.addWidget(self.palette_editor)
        self.color_tabs.addTab(palette_page, "Palette")

        self.ribbon = RibbonWidget(self)
        self.ribbon.setMinimumHeight(105)

        self.tool_settings_page = self.ribbon.add_page(
            "tool_settings", "Tool Settings"
        )
        settings_group = self.tool_settings_page.add_group(
            "Current tool", minimum_width=720
        )
        self.tool_settings_controls = ToolSettingsControls(
            self.settings, self.ribbon
        )
        settings_group.add_widget(self.tool_settings_controls)

        self.vector_tools_page = self.ribbon.add_page(
            "vector_tools", "Vector Tools", visible=False
        )
        self.vector_tools_controls = VectorToolsControls(
            self.settings, self.ribbon
        )
        redraw_group = self.vector_tools_page.add_group(
            "Redraw thickness / opacity", minimum_width=280
        )
        redraw_group.add_widget(self.vector_tools_controls.redraw_widget)
        connect_group = self.vector_tools_page.add_group(
            "Connect vector line", minimum_width=190
        )
        connect_group.add_widget(self.vector_tools_controls.connect_widget)
        simplify_group = self.vector_tools_page.add_group(
            "Simplify vector line", minimum_width=260
        )
        simplify_group.add_widget(
            self.vector_tools_controls.simplify_widget
        )
        transform_group = self.vector_tools_page.add_group(
            "Transform Settings", minimum_width=180
        )
        transform_group.add_widget(
            self.vector_tools_controls.transform_widget
        )
        self.raster_object_page = self.ribbon.add_page(
            "raster_object_settings", "Raster Object Settings",
            visible=False,
        )
        canvas_row = QWidget(self)
        canvas_layout = QHBoxLayout(canvas_row)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        self.canvas = create_canvas(self.settings)
        self.raster_object_controls = RasterObjectControls(
            self.canvas, self.settings, self.ribbon
        )
        raster_object_group = self.raster_object_page.add_group(
            "Object Settings", minimum_width=310
        )
        raster_object_group.add_widget(
            self.raster_object_controls.object_widget
        )
        raster_transform_group = self.raster_object_page.add_group(
            "Transform Settings", minimum_width=190
        )
        raster_transform_group.add_widget(
            self.raster_object_controls.transform_widget
        )
        self.gradient_tools_page = self.ribbon.add_page(
            "gradient_tools", "Gradient Tools", visible=False
        )
        self.gradient_tools_controls = GradientToolsControls(
            self.canvas, self.ribbon
        )
        gradient_create_group = self.gradient_tools_page.add_group(
            "Create Gradient", minimum_width=260
        )
        gradient_create_group.add_widget(
            self.gradient_tools_controls.create_widget
        )
        gradient_parameters_group = self.gradient_tools_page.add_group(
            "Gradient Parameters", minimum_width=235
        )
        gradient_parameters_group.add_widget(
            self.gradient_tools_controls.parameters_widget
        )
        gradient_presets_group = self.gradient_tools_page.add_group(
            "Gradient Ramp Presets", minimum_width=165
        )
        gradient_presets_group.add_widget(
            self.gradient_tools_controls.presets_widget
        )
        gradient_type_group = self.gradient_tools_page.add_group(
            "Gradient Type Parameters", minimum_width=220
        )
        gradient_type_group.add_widget(
            self.gradient_tools_controls.type_parameters_widget
        )
        self.preview = ChapterPreview(self.canvas)
        canvas_layout.addWidget(self.preview)
        canvas_layout.addWidget(self.canvas, 1)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.sidebar_splitter.setObjectName("sidebarSplitter")
        self.sidebar_splitter.setChildrenCollapsible(False)
        self.sidebar_splitter.setHandleWidth(6)
        self.sidebar_splitter.setMinimumWidth(190)
        self.sidebar_splitter.addWidget(self.tool_toolbar)
        self.sidebar_splitter.addWidget(self.color_tabs)
        self.sidebar_splitter.setStretchFactor(0, 1)
        self.sidebar_splitter.setStretchFactor(1, 1)

        self.ribbon_canvas_splitter = QSplitter(
            Qt.Orientation.Vertical, self
        )
        self.ribbon_canvas_splitter.setObjectName("ribbonCanvasSplitter")
        self.ribbon_canvas_splitter.setChildrenCollapsible(False)
        self.ribbon_canvas_splitter.setHandleWidth(6)
        self.ribbon_canvas_splitter.addWidget(self.ribbon)
        self.ribbon_canvas_splitter.addWidget(canvas_row)
        self.ribbon_canvas_splitter.setStretchFactor(0, 0)
        self.ribbon_canvas_splitter.setStretchFactor(1, 1)

        self.workspace_splitter = QSplitter(
            Qt.Orientation.Horizontal, self
        )
        self.workspace_splitter.setObjectName("workspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        self.workspace_splitter.addWidget(self.sidebar_splitter)
        self.workspace_splitter.addWidget(self.ribbon_canvas_splitter)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        self.setCentralWidget(self.workspace_splitter)
        self._restore_workspace_layout()

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
        self._tablet_outliner_press: dict | None = None
        self._hierarchy_reset_expanded: set[str] = set()
        self._hierarchy_reset_selection: tuple[str, str] = ("", "")
        self.hierarchy_model.modelAboutToBeReset.connect(
            self._capture_hierarchy_view_state
        )
        self.hierarchy_model.modelReset.connect(
            self._restore_hierarchy_view_state
        )
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
        # Tool controls now live in the ribbon; retain the inspector object
        # panel itself while collapsing its legacy embedded tool row.
        self.inspector.raster_tool_panel.setMaximumHeight(0)
        self.inspector.raster_tool_panel.setMinimumHeight(0)
        self.page_gap_confirmation = QFrame(self.canvas)
        self.page_gap_confirmation.setObjectName("pageGapConfirmation")
        self.page_gap_confirmation.setStyleSheet(
            "#pageGapConfirmation { background: rgba(28,28,32,242); "
            "border: 1px solid #f2a23a; border-radius: 7px; }"
        )
        gap_layout = QHBoxLayout(self.page_gap_confirmation)
        gap_layout.setContentsMargins(8, 6, 8, 6)
        gap_layout.addWidget(QLabel("Adjust the orange page gap"))
        self.confirm_page_gap_button = QPushButton("Confirm Page Gap")
        self.cancel_page_gap_button = QPushButton("Cancel")
        gap_layout.addWidget(self.confirm_page_gap_button)
        gap_layout.addWidget(self.cancel_page_gap_button)
        self.page_gap_confirmation.hide()
        self.autosave_timer = QTimer(self)
        self.autosave_timer.setSingleShot(True)
        self.series_preferences_timer = QTimer(self)
        self.series_preferences_timer.setSingleShot(True)
        self.layout_settings_timer = QTimer(self)
        self.layout_settings_timer.setSingleShot(True)
        self._vector_ribbon_context = False
        self._raster_ribbon_context = False
        self._gradient_ribbon_context = False
        self._selected_gradient_ribbon_id = ""
        self._manual_ribbon_page = ""
        self._programmatic_ribbon_selection = 0
        self._expanded_selected_vector_id = ""

    def _restore_workspace_layout(self) -> None:
        stored = self.settings.ui_splitter_sizes
        width = max(600, self.width())
        height = max(600, self.height())
        sidebar = stored.get("sidebar_workspace", [230, width - 230])
        sidebar_width = max(190, min(
            int(sidebar[0]), max(190, width - 480)
        ))
        self.workspace_splitter.setSizes(
            [sidebar_width, max(480, width - sidebar_width)]
        )

        tools_colors = stored.get(
            "tools_colors",
            [round(height * 0.55), round(height * 0.45)],
        )
        color_minimum = max(
            225, self.color_tabs.minimumSizeHint().height()
        )
        tools_minimum = max(
            90, self.tool_toolbar.minimumSizeHint().height()
        )
        available = max(
            tools_minimum + color_minimum,
            int(tools_colors[0]) + int(tools_colors[1]),
        )
        tools_size = max(
            tools_minimum,
            min(int(tools_colors[0]), available - color_minimum),
        )
        self.sidebar_splitter.setSizes(
            [tools_size, available - tools_size]
        )

        ribbon_canvas = stored.get("ribbon_canvas", [180, height - 180])
        ribbon_minimum = max(105, self.ribbon.minimumSizeHint().height())
        canvas_minimum = max(300, self.canvas.minimumSizeHint().height())
        vertical_available = max(
            ribbon_minimum + canvas_minimum,
            int(ribbon_canvas[0]) + int(ribbon_canvas[1]),
        )
        ribbon_size = max(
            ribbon_minimum,
            min(int(ribbon_canvas[0]), vertical_available - canvas_minimum),
        )
        self.ribbon_canvas_splitter.setSizes(
            [ribbon_size, vertical_available - ribbon_size]
        )

    def _capture_workspace_layout(self) -> None:
        self.settings.ui_splitter_sizes = {
            "sidebar_workspace": self.workspace_splitter.sizes(),
            "tools_colors": self.sidebar_splitter.sizes(),
            "ribbon_canvas": self.ribbon_canvas_splitter.sizes(),
        }
        self.settings.clamp()

    def _schedule_workspace_layout_save(self, *args) -> None:
        del args
        self._capture_workspace_layout()
        self.layout_settings_timer.start(250)

    def _save_workspace_layout(self) -> None:
        self._capture_workspace_layout()
        save_settings(self.settings)

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
        self.snap_grid.toggled.connect(self._settings_changed)
        self.canvas.documentChanged.connect(self._mark_dirty)
        self.ribbon.pageChanged.connect(self._ribbon_page_changed)
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
        self.canvas.pageCreationFinished.connect(
            self._finish_page_creation
        )
        self.canvas.pageCreationInvalid.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )
        self.canvas.pageGapConfirmationChanged.connect(
            self._set_page_gap_confirmation_visible
        )
        self.confirm_page_gap_button.clicked.connect(
            self._confirm_page_gap
        )
        self.cancel_page_gap_button.clicked.connect(
            self._cancel_page_gap
        )
        if hasattr(self.canvas, "vectorSelectionChanged"):
            self.canvas.vectorSelectionChanged.connect(
                lambda strokes, points: self._sync_contextual_ribbon()
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
        self.series_preferences_timer.timeout.connect(
            self._flush_series_preferences
        )
        self.layout_settings_timer.timeout.connect(
            self._save_workspace_layout
        )
        for splitter in (
            self.workspace_splitter,
            self.sidebar_splitter,
            self.ribbon_canvas_splitter,
        ):
            splitter.splitterMoved.connect(
                self._schedule_workspace_layout_save
            )

        self.add_page_button.clicked.connect(self._add_page)
        self.add_layer_button.clicked.connect(self._add_layer)
        self.add_raster_button.clicked.connect(self._add_raster)
        self.add_vector_button.clicked.connect(self._add_vector_drawing)
        self.add_text_button.clicked.connect(self._add_text)
        self.add_fill_button.clicked.connect(self._add_fill)
        self.delete_button.clicked.connect(self._delete_selected)

        self.tool_settings_controls.pencilPresetSelected.connect(
            self._pencil_preset_selected
        )
        self.tool_settings_controls.pencilSettingsRequested.connect(
            self._edit_pencil_settings
        )
        self.tool_settings_controls.brushSizeSelected.connect(
            self._brush_size_selected
        )
        self.tool_settings_controls.brushSizesRequested.connect(
            self._edit_brush_sizes
        )
        self.tool_settings_controls.eraserShapeChanged.connect(
            self._eraser_shape_selected
        )
        self.tool_settings_controls.settingsChanged.connect(
            self._ribbon_settings_changed
        )
        self.vector_tools_controls.settingsChanged.connect(
            self._ribbon_settings_changed
        )
        self.raster_object_controls.settingsChanged.connect(
            self._ribbon_settings_changed
        )
        self.raster_object_controls.objectChanged.connect(
            self._hierarchy_changed
        )
        self.gradient_tools_controls.createRequested.connect(
            self._create_gradient
        )
        self.gradient_tools_controls.objectChanged.connect(
            self._hierarchy_changed
        )
        self.gradient_tools_controls.presetLoadRequested.connect(
            self._load_gradient_preset
        )
        self.gradient_tools_controls.presetAddRequested.connect(
            self._add_gradient_preset
        )
        self.gradient_tools_controls.presetSaveRequested.connect(
            self._save_gradient_preset
        )
        self.gradient_tools_controls.presetRenameRequested.connect(
            self._rename_gradient_preset
        )
        self.gradient_tools_controls.presetRemoveRequested.connect(
            self._remove_gradient_preset
        )
        self.vector_tools_controls.redrawToolRequested.connect(
            lambda: self._activate_named_tool("VECTOR_REDRAW")
        )
        self.vector_tools_controls.connectToolRequested.connect(
            lambda: self._activate_named_tool("VECTOR_CONNECT")
        )
        self.vector_tools_controls.simplifyToolRequested.connect(
            lambda: self._activate_named_tool("VECTOR_SIMPLIFY")
        )
        self.vector_tools_controls.redrawApplyRequested.connect(
            self._apply_vector_redraw
        )
        self.vector_tools_controls.simplifyApplyRequested.connect(
            self._apply_vector_simplify
        )

        self.color_panel.colorChanged.connect(self._series_color_changed)
        self.color_panel.colorsSwapped.connect(
            self._series_colors_swapped
        )
        self.color_panel.activeSlotChanged.connect(
            lambda slot: self.palette_editor.set_new_swatch_color(
                self.color_panel.active_color()
            )
        )
        self.color_panel.picker.interactionFinished.connect(
            lambda color: self._flush_series_preferences()
        )
        self.palette_editor.paletteSelectionChanged.connect(
            self._palette_selected
        )
        self.palette_editor.addPaletteRequested.connect(
            self._add_palette
        )
        self.palette_editor.removePaletteRequested.connect(
            self._remove_palette
        )
        self.palette_editor.paletteNameChanged.connect(
            self._rename_palette
        )
        self.palette_editor.swatchActivated.connect(
            self._palette_swatch_activated
        )
        self.palette_editor.swatchColorChangeRequested.connect(
            self._change_palette_swatch
        )
        self.palette_editor.addSwatchRequested.connect(
            self._add_palette_swatch
        )
        self.palette_editor.removeSwatchRequested.connect(
            self._remove_palette_swatch
        )

    def _install_shortcuts(self) -> None:
        self._tool_hotkey_actions = {
            "raster_pencil": ToolKind.RASTER_PENCIL,
            "raster_eraser": ToolKind.RASTER_ERASER,
            "object_select": ToolKind.OBJECT_SELECT,
            "transform": ToolKind.TRANSFORM,
            "shape_edit": ToolKind.SHAPE_EDIT,
        }
        for action_id, enum_name in (
            ("fill", "FILL"),
            ("vector_redraw", "VECTOR_REDRAW"),
            ("vector_connect", "VECTOR_CONNECT"),
            ("vector_simplify", "VECTOR_SIMPLIFY"),
            ("draw_select_rect", "DRAW_SELECT_RECT"),
            ("draw_select_lasso", "DRAW_SELECT_LASSO"),
            ("draw_select_stroke", "DRAW_SELECT_STROKE"),
            ("insert_page_gap", "INSERT_PAGE_GAP"),
        ):
            tool = getattr(ToolKind, enum_name, None)
            if tool is not None:
                self._tool_hotkey_actions[action_id] = tool
        self._command_hotkey_actions = {
            "save": self.save,
            "undo": self.canvas.command_stack.undo,
            "redo": self.canvas.command_stack.redo,
            "reset_view": self.canvas.reset_view,
            "toggle_grid": self._toggle_grid,
            "select_all": self.canvas.select_all_drawing,
        }
        self._hotkey_bindings = {
            action_id: chord_keys(value)
            for action_id, value in self.settings.hotkeys.items()
            if chord_keys(value)
        }
        if not hasattr(self, "_hotkey_pressed"):
            self._hotkey_pressed: set[int] = set()
            self._hotkey_pending: dict | None = None
            self._hotkey_active_hold: dict | None = None
            self._hotkey_runtime_enabled = True
            self._hotkey_text_editing = False
            self._hotkey_internal_tool_change = False
            self._hotkey_clock = time.monotonic
            self._hotkey_prefix_timer = QTimer(self)
            self._hotkey_prefix_timer.setSingleShot(True)
            self._hotkey_prefix_timer.timeout.connect(
                self._hotkey_prefix_timeout
            )
            QApplication.instance().installEventFilter(self)
            self._application_event_filter_installed = True
            self.canvas.toolChanged.connect(self._hotkey_tool_changed)
            self._fullscreen_shortcut = QShortcut(
                QKeySequence("Alt+Return"), self
            )
            self._fullscreen_shortcut.activated.connect(
                self._toggle_fullscreen
            )
            self._shortcuts = [self._fullscreen_shortcut]
            self._shortcut_sequences = [
                (self._fullscreen_shortcut, self._fullscreen_shortcut.key())
            ]
        else:
            self._hotkey_prefix_timer.stop()
            self._hotkey_pending = None
            self._hotkey_pressed.clear()
            self._restore_active_hotkey_tool()

    def _hotkey_action_for_chord(
        self, chord: frozenset[int],
    ) -> str | None:
        return next((
            action_id for action_id, candidate in self._hotkey_bindings.items()
            if candidate == chord
        ), None)

    def _hotkey_is_prefix(self, chord: frozenset[int]) -> bool:
        return any(
            chord < candidate for candidate in self._hotkey_bindings.values()
        )

    def _hotkey_is_suppressed(
        self, action_id: str, chord: frozenset[int],
    ) -> bool:
        if not self._hotkey_text_input_active():
            return False
        display = chord_text(chord)
        regular = [key for key in chord if key not in MODIFIER_LABELS]
        contains_letter = any(
            len(QKeySequence(key).toString(QKeySequence.PortableText)) == 1
            and QKeySequence(key).toString(
                QKeySequence.PortableText
            ).isalpha()
            for key in regular
        )
        return contains_letter or int(Qt.Key_Shift) in chord

    def _hotkey_text_input_active(self) -> bool:
        focus = QApplication.focusWidget()
        return self._hotkey_text_editing or isinstance(
            focus, (QLineEdit, QPlainTextEdit, QTextEdit)
        )

    def _trigger_hotkey(
        self, action_id: str, chord: frozenset[int],
        started: float, tap: bool = False,
    ) -> None:
        if self._hotkey_is_suppressed(action_id, chord):
            return
        if action_id in self._tool_hotkey_actions:
            tool = self._tool_hotkey_actions[action_id]
            previous = self.canvas.tool
            self._hotkey_internal_tool_change = True
            try:
                activated = self._activate_tool(tool)
            finally:
                self._hotkey_internal_tool_change = False
            if (
                activated and not tap
                and self.settings.hotkey_hold.get(action_id, False)
            ):
                self._hotkey_active_hold = {
                    "action": action_id,
                    "chord": chord,
                    "started": started,
                    "previous": previous,
                    "target": tool,
                }
            return
        callback = self._command_hotkey_actions.get(action_id)
        if callback is not None:
            callback()

    def _restore_active_hotkey_tool(self) -> None:
        active, self._hotkey_active_hold = self._hotkey_active_hold, None
        if active is None or self.canvas.tool != active["target"]:
            return
        self._hotkey_internal_tool_change = True
        try:
            self._activate_tool(active["previous"])
        finally:
            self._hotkey_internal_tool_change = False

    def _hotkey_prefix_timeout(self) -> None:
        pending = self._hotkey_pending
        if (
            pending is None
            or frozenset(self._hotkey_pressed) != pending["chord"]
        ):
            return
        self._hotkey_pending = None
        self._trigger_hotkey(
            pending["action"], pending["chord"], pending["started"]
        )

    def _hotkey_tool_changed(self, tool: ToolKind) -> None:
        if (
            self._hotkey_active_hold is not None
            and not self._hotkey_internal_tool_change
            and tool != self._hotkey_active_hold["target"]
        ):
            self._hotkey_active_hold = None

    def _hotkey_press(self, key: int) -> bool:
        if key in self._hotkey_pressed:
            return False
        self._hotkey_pressed.add(key)
        chord = frozenset(self._hotkey_pressed)
        if (
            self._hotkey_active_hold is not None
            and self._hotkey_active_hold["chord"] < chord
        ):
            self._restore_active_hotkey_tool()
        if self._hotkey_pending is not None:
            self._hotkey_prefix_timer.stop()
            self._hotkey_pending = None
        action_id = self._hotkey_action_for_chord(chord)
        prefix = self._hotkey_is_prefix(chord)
        text_conflict = (
            self._hotkey_text_input_active()
            and (
                int(Qt.Key_Shift) in chord
                or any(
                    candidate not in MODIFIER_LABELS
                    and len(QKeySequence(candidate).toString(
                        QKeySequence.PortableText
                    )) == 1
                    and QKeySequence(candidate).toString(
                        QKeySequence.PortableText
                    ).isalpha()
                    for candidate in chord
                )
            )
        )
        if text_conflict:
            action_id = None
            prefix = False
        if action_id is not None and prefix:
            self._hotkey_pending = {
                "action": action_id,
                "chord": chord,
                "started": self._hotkey_clock(),
            }
            self._hotkey_prefix_timer.start(200)
        elif action_id is not None:
            self._trigger_hotkey(
                action_id, chord, self._hotkey_clock()
            )
        return action_id is not None or prefix

    def _hotkey_release(self, key: int) -> bool:
        chord = frozenset(self._hotkey_pressed)
        consumed = any(key in binding for binding in self._hotkey_bindings.values())
        if self._hotkey_text_input_active() and (
            int(Qt.Key_Shift) in chord
            or any(
                candidate not in MODIFIER_LABELS
                and len(QKeySequence(candidate).toString(
                    QKeySequence.PortableText
                )) == 1
                and QKeySequence(candidate).toString(
                    QKeySequence.PortableText
                ).isalpha()
                for candidate in chord
            )
        ):
            consumed = False
        pending = self._hotkey_pending
        if pending is not None and key in pending["chord"]:
            self._hotkey_prefix_timer.stop()
            self._hotkey_pending = None
            self._trigger_hotkey(
                pending["action"], pending["chord"],
                pending["started"], tap=True,
            )
        active = self._hotkey_active_hold
        self._hotkey_pressed.discard(key)
        if (
            active is not None
            and key in active["chord"]
            and not active["chord"].issubset(self._hotkey_pressed)
        ):
            elapsed = self._hotkey_clock() - active["started"]
            if elapsed >= 0.2:
                self._restore_active_hotkey_tool()
            else:
                self._hotkey_active_hold = None
        return consumed

    def _forward_popup_tablet_event(self, watched, event) -> bool:
        if event.type() not in {
            QEvent.TabletMove, QEvent.TabletPress, QEvent.TabletRelease,
        }:
            return False
        global_position = QPointF(event.globalPosition())
        target = QApplication.widgetAt(global_position.toPoint())
        if target is None and isinstance(watched, QWidget):
            target = watched
        pressed = getattr(self, "_tablet_popup_pressed", None)
        if event.type() == QEvent.TabletRelease and pressed is not None:
            target = pressed
        if target is None:
            return False
        popup = target.window()
        if not (
            isinstance(popup, QMenu)
            or bool(popup.windowFlags() & Qt.Popup)
        ):
            return False
        local = QPointF(target.mapFromGlobal(global_position.toPoint()))
        if event.type() == QEvent.TabletPress:
            mouse_type = QEvent.MouseButtonPress
            button, buttons = Qt.LeftButton, Qt.LeftButton
            self._tablet_popup_pressed = target
        elif event.type() == QEvent.TabletRelease:
            mouse_type = QEvent.MouseButtonRelease
            button, buttons = Qt.LeftButton, Qt.NoButton
            self._tablet_popup_pressed = None
        else:
            mouse_type = QEvent.MouseMove
            button = Qt.NoButton
            buttons = (
                Qt.LeftButton if pressed is not None else Qt.NoButton
            )
        mouse = QMouseEvent(
            mouse_type, local, local, global_position,
            button, buttons, event.modifiers(), event.pointingDevice(),
        )
        QCoreApplication.sendEvent(target, mouse)
        event.accept()
        return True

    def _send_outliner_mouse(
        self, event_type, global_position: QPointF, button, buttons,
        modifiers, device,
    ) -> None:
        viewport = self.tree.viewport()
        local = QPointF(
            viewport.mapFromGlobal(global_position.toPoint())
        )
        mouse = QMouseEvent(
            event_type, local, local, global_position,
            button, buttons, modifiers, device,
        )
        QCoreApplication.sendEvent(viewport, mouse)

    def _forward_outliner_tablet_event(self, watched, event) -> bool:
        if event.type() not in {
            QEvent.TabletMove, QEvent.TabletPress, QEvent.TabletRelease,
        }:
            return False
        viewport = self.tree.viewport()
        global_position = QPointF(event.globalPosition())
        local = viewport.mapFromGlobal(global_position.toPoint())
        inside = viewport.rect().contains(local)
        state = self._tablet_outliner_press
        if event.type() == QEvent.TabletPress:
            if not inside:
                return False
            self._tablet_outliner_press = {
                "global": QPointF(global_position),
                "forwarded": False,
                "device": event.pointingDevice(),
                "modifiers": event.modifiers(),
            }
            event.accept()
            return True
        if state is None:
            if event.type() == QEvent.TabletMove and inside:
                self._send_outliner_mouse(
                    QEvent.MouseMove, global_position,
                    Qt.NoButton, Qt.NoButton,
                    event.modifiers(), event.pointingDevice(),
                )
                event.accept()
                return True
            return False
        if event.type() == QEvent.TabletMove:
            distance = (
                global_position - state["global"]
            ).manhattanLength()
            if (
                not state["forwarded"]
                and distance >= QApplication.startDragDistance()
            ):
                self._send_outliner_mouse(
                    QEvent.MouseButtonPress, state["global"],
                    Qt.LeftButton, Qt.LeftButton,
                    state["modifiers"], state["device"],
                )
                state["forwarded"] = True
            if state["forwarded"]:
                self._send_outliner_mouse(
                    QEvent.MouseMove, global_position,
                    Qt.NoButton, Qt.LeftButton,
                    event.modifiers(), state["device"],
                )
            event.accept()
            return True
        if state["forwarded"]:
            self._send_outliner_mouse(
                QEvent.MouseButtonRelease, global_position,
                Qt.LeftButton, Qt.NoButton,
                event.modifiers(), state["device"],
            )
        else:
            self._send_outliner_mouse(
                QEvent.MouseButtonPress, state["global"],
                Qt.LeftButton, Qt.LeftButton,
                state["modifiers"], state["device"],
            )
            self._send_outliner_mouse(
                QEvent.MouseButtonRelease, state["global"],
                Qt.LeftButton, Qt.NoButton,
                event.modifiers(), state["device"],
            )
        self._tablet_outliner_press = None
        event.accept()
        return True

    def _cancel_outliner_tablet_press(self) -> None:
        state = self._tablet_outliner_press
        if state is not None and state.get("forwarded"):
            self._send_outliner_mouse(
                QEvent.MouseButtonRelease, state["global"],
                Qt.LeftButton, Qt.NoButton,
                state["modifiers"], state["device"],
            )
        self._tablet_outliner_press = None

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if self._forward_popup_tablet_event(watched, event):
            return True
        if self._forward_outliner_tablet_event(watched, event):
            return True
        if event.type() == QEvent.ApplicationDeactivate:
            self._cancel_outliner_tablet_press()
            self._hotkey_prefix_timer.stop()
            self._hotkey_pending = None
            self._hotkey_pressed.clear()
            self._restore_active_hotkey_tool()
            return super().eventFilter(watched, event)
        if (
            not getattr(self, "_hotkey_runtime_enabled", False)
            or event.type() not in {QEvent.KeyPress, QEvent.KeyRelease}
        ):
            return super().eventFilter(watched, event)
        if int(event.key()) in {int(Qt.Key_Shift), int(Qt.Key_Control)}:
            self.canvas.update()
        widget = watched if isinstance(watched, QWidget) else None
        if widget is not None and widget.window() is not self:
            return super().eventFilter(watched, event)
        if event.isAutoRepeat():
            return False
        handled = (
            self._hotkey_press(int(event.key()))
            if event.type() == QEvent.KeyPress
            else self._hotkey_release(int(event.key()))
        )
        return handled or super().eventFilter(watched, event)

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
            series = repository.load_series(
                legacy_primary_color=self.settings.brush_color
            )
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
        self._flush_series_preferences()
        self.repository, self.series = repository, series
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        for reference in series.chapters:
            self.chapter_combo.addItem(reference.name, reference.chapter_id)
        self.chapter_combo.blockSignals(False)
        self._sync_series_color_ui()
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
        initial_object = next(
            (
                obj for obj in chapter.objects.values()
                if isinstance(obj, (RasterObject, VectorDrawingObject))
            ),
            None,
        )
        if initial_object:
            self.canvas.set_selection("object", initial_object.object_id)
        self._sync_contextual_ribbon()
        # The first selection establishes the object context.  Keep the
        # contextual page as the initial landing page; subsequent explicit
        # Pencil/Eraser activations are routed to Tool Settings.
        if isinstance(initial_object, VectorDrawingObject):
            initial_ribbon_page = "vector_tools"
        elif isinstance(initial_object, RasterObject):
            initial_ribbon_page = "raster_object_settings"
        else:
            initial_ribbon_page = ""
        self._refresh_actions()
        if initial_ribbon_page:
            self._select_ribbon_page(initial_ribbon_page)
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
            layer and layer.layer_kind != "fill"
            and (allow_page or not layer.is_page)
        ):
            return layer
        return None

    def _new_object_insertion_index(self, parent_id: str) -> int | None:
        if (
            self.chapter is None
            or self.canvas.selected_kind != "object"
            or self.canvas.selected_id not in self.chapter.objects
        ):
            return None
        selected = self.chapter.objects[self.canvas.selected_id]
        if isinstance(selected, VectorFillObject):
            owner = self.chapter.objects.get(selected.owner_drawing_id)
            if not isinstance(owner, VectorDrawingObject):
                return None
            selected = owner
        if selected.parent_layer_id != parent_id:
            return None
        siblings = self.chapter.layers[parent_id].children
        return next((
            index for index, reference in enumerate(siblings)
            if (
                reference.kind == "object"
                and reference.entity_id == selected.object_id
            )
        ), None)

    def _add_page(self) -> None:
        if (
            self.chapter is None
            or not self.canvas.active_page_id
            or self.canvas.active_page_id not in self.chapter.root_page_ids
        ):
            self.statusBar().showMessage(
                "Select a page or one of its descendants first.", 5000
            )
            return
        anchor_id = self.canvas.active_page_id
        anchor_bounds = self.canvas.page_world_bounds(anchor_id)
        lower_ids = [
            page_id
            for page_id in self.canvas.physically_ordered_pages()
            if (
                page_id != anchor_id
                and self.canvas.page_world_bounds(page_id).top()
                >= anchor_bounds.bottom()
            )
        ]
        if lower_ids:
            action = self._choose_add_page_gap_action()
            if action == "cancel":
                return
            if action == "insert":
                top_ids = [
                    page_id for page_id in self.chapter.root_page_ids
                    if page_id not in lower_ids
                ]
                if self.canvas.begin_page_gap_transaction(
                    "add_page", anchor_id, top_ids, lower_ids,
                    anchor_bounds.bottom(),
                ):
                    self.statusBar().showMessage(
                        "Adjust the page gap, then confirm or cancel.", 7000
                    )
                return
        self._begin_add_page_shape(anchor_id)

    def _choose_add_page_gap_action(self) -> str:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Add Page")
        dialog.setText(
            "A page already exists below the active page. "
            "Would you like to insert and adjust a page gap first?"
        )
        insert = dialog.addButton(
            "Insert Gap", QMessageBox.AcceptRole
        )
        proceed = dialog.addButton(
            "Continue Without Gap", QMessageBox.DestructiveRole
        )
        dialog.addButton(QMessageBox.Cancel)
        dialog.exec()
        return (
            "insert" if dialog.clickedButton() is insert
            else "continue" if dialog.clickedButton() is proceed
            else "cancel"
        )

    def _begin_add_page_shape(
        self, anchor_id: str, *, before: dict | None = None,
        gap_bounds: tuple[float, float] | None = None,
    ) -> None:
        kind = self._choose_page_shape()
        if kind is None:
            if self.canvas.page_gap_transaction() is not None:
                self.canvas.cancel_page_gap_transaction()
            return
        if self.canvas.begin_page_creation(
            anchor_id, kind, before=before, gap_bounds=gap_bounds
        ):
            self.statusBar().showMessage(
                "Draw the closed page below the active page. Escape cancels.",
                7000,
            )
        elif self.canvas.page_gap_transaction() is not None:
            self.canvas.cancel_page_gap_transaction()

    def _set_page_gap_confirmation_visible(self, visible: bool) -> None:
        self.page_gap_confirmation.setVisible(bool(visible))
        if visible:
            self.page_gap_confirmation.adjustSize()
            self.page_gap_confirmation.move(
                max(
                    8,
                    (self.canvas.width()
                     - self.page_gap_confirmation.width()) // 2,
                ),
                10,
            )
            self.page_gap_confirmation.raise_()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        if (
            hasattr(self, "page_gap_confirmation")
            and self.page_gap_confirmation.isVisible()
        ):
            QTimer.singleShot(
                0, lambda: self._set_page_gap_confirmation_visible(True)
            )

    def _confirm_page_gap(self) -> None:
        transaction = self.canvas.confirm_page_gap_transaction()
        if transaction is None:
            self._set_page_gap_confirmation_visible(False)
            return
        if transaction["origin"] != "add_page":
            return
        self._set_page_gap_confirmation_visible(False)
        self._begin_add_page_shape(
            str(transaction["anchor_id"]),
            before=transaction["before"],
            gap_bounds=(
                float(transaction["top_y"]),
                float(transaction["bottom_y"]),
            ),
        )

    def _cancel_page_gap(self) -> None:
        self.canvas.cancel_page_gap_transaction()
        self._set_page_gap_confirmation_visible(False)

    def _choose_page_shape(self) -> str | None:
        dialog = QMessageBox(self)
        dialog.setWindowTitle("Add Page")
        dialog.setText("Choose the closed shape for the new page.")
        rectangle = dialog.addButton(
            "Rectangle", QMessageBox.AcceptRole
        )
        circle = dialog.addButton("Circle", QMessageBox.AcceptRole)
        custom = dialog.addButton(
            "Custom Shape", QMessageBox.AcceptRole
        )
        dialog.addButton(QMessageBox.Cancel)
        dialog.exec()
        return {
            rectangle: "rectangle",
            circle: "circle",
            custom: "custom",
        }.get(dialog.clickedButton())

    def _next_page_name(self) -> str:
        numbered: set[int] = set()
        for page_id in self.chapter.root_page_ids:
            name = self.chapter.layers[page_id].name
            match = re.fullmatch(r"Page\s+(\d+)", name)
            if match:
                numbered.add(int(match.group(1)))
        number = 1
        while number in numbered:
            number += 1
        return f"Page {number}"

    def _finish_page_creation(
        self, bound: BoundGeometry, before: dict, anchor_id: str,
    ) -> None:
        if (
            self.chapter is None
            or anchor_id not in self.chapter.root_page_ids
        ):
            self.canvas.resolve_page_creation(
                False, "The page used as the insertion anchor no longer exists."
            )
            return
        try:
            bound.validate()
            base_height = self.canvas.page_creation_base_height()
            self.chapter.height = base_height
            insertion_index = (
                self.chapter.root_page_ids.index(anchor_id) + 1
            )
            page = self.chapter.add_page(
                self._next_page_name(), bound, index=insertion_index
            )
        except (KeyError, TypeError, ValueError) as error:
            self.canvas.resolve_page_creation(False, str(error))
            return
        self.chapter.height = base_height
        self.canvas._ensure_page_height_safety()
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add page")
        self._after_structure(page.layer_id, "layer")
        # Acknowledge before changing tools: changing away from a creation
        # tool cancels any still-pending draft by design.
        self.canvas.resolve_page_creation(True)
        self.canvas.finish_page_gap_workflow()
        self.canvas.set_tool(ToolKind.SHAPE_EDIT)
        self.statusBar().showMessage(
            f"Created {page.name}", 3000
        )

    def _add_layer(self) -> None:
        placement = self.canvas._target_placement_for_new_bound()
        if placement is None:
            self.statusBar().showMessage("Select a page or layer first", 4000)
            return
        parent_id, insertion_index = placement
        parent = self.chapter.layers[parent_id]
        before = self.chapter.to_dict()
        x, y, width, height = parent.bound.bbox()
        layer = self.chapter.add_layer(
            parent.layer_id, self._next_layer_name(),
            BoundGeometry.rectangle(x, y, max(64, width), max(64, height)),
            index=insertion_index,
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add layer")
        self._after_structure(layer.layer_id, "layer")

    def _add_raster(self) -> None:
        parent = self._selected_parent_layer(allow_page=True)
        if parent is None:
            self.statusBar().showMessage(
                "Raster objects require a selected page or container layer",
                4000,
            )
            return
        insertion_index = self._new_object_insertion_index(parent.layer_id)
        if self.canvas.begin_raster_creation(
            parent.layer_id, insertion_index
        ):
            message = (
                "Drag a box on the canvas to set the raster width and height. "
                "Escape cancels."
            )
            self.statusBar().showMessage(message, 7000)
            QToolTip.showText(QCursor.pos(), message, self)

    def _add_vector_drawing(self) -> None:
        parent = self._selected_parent_layer(allow_page=True)
        if parent is None or self.chapter is None:
            self.statusBar().showMessage(
                "Vector Drawings require a selected page or container layer",
                4000,
            )
            return
        before = self.chapter.to_dict()
        count = sum(
            isinstance(item, VectorDrawingObject)
            for item in self.chapter.objects.values()
        ) + 1
        drawing = VectorDrawingObject(name=f"Vector Drawing {count}")
        self.chapter.add_object(
            parent.layer_id,
            drawing,
            index=self._new_object_insertion_index(parent.layer_id),
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(
            before, after, "Add Vector Drawing"
        )
        self._after_structure(drawing.object_id, "object")
        self._activate_tool(ToolKind.RASTER_PENCIL)

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

    def _create_gradient(self, field_type: str) -> None:
        parent_id = self._gradient_context_parent_id()
        if not parent_id:
            self.statusBar().showMessage(
                "Select a page, shape, or one of its child objects first",
                5000,
            )
            return
        if not self.canvas.begin_gradient_creation(parent_id, field_type):
            self.statusBar().showMessage(
                "Unable to create a gradient in this shape", 5000
            )
            return
        if field_type == "line":
            self.statusBar().showMessage(
                "Draw an open gradient path; Enter or double-click confirms.",
                7000,
            )
        elif field_type == "radial":
            self.statusBar().showMessage(
                "Drag from the gradient origin to set its radius.", 7000
            )
        self._sync_contextual_ribbon()

    def _add_text(self) -> None:
        parent = self._selected_parent_layer(allow_page=True)
        if parent is None:
            self.statusBar().showMessage(
                "Text objects require a selected page or container layer",
                4000,
            )
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
        self.chapter.add_object(
            parent.layer_id, obj,
            index=self._new_object_insertion_index(parent.layer_id),
        )
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
        self.canvas.clear_selection()
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

    def _activate_tool(self, tool: ToolKind) -> bool:
        selected_object = (
            self.chapter.objects.get(self.canvas.selected_id)
            if (
                self.chapter is not None
                and self.canvas.selected_kind == "object"
            )
            else None
        )
        vector_selected = isinstance(selected_object, VectorDrawingObject)
        if tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            if (
                self.chapter and self.canvas.selected_kind == "layer"
                and not vector_selected
            ):
                layer = self.chapter.layers[self.canvas.selected_id]
                raster_id = layer.last_raster_id
                if raster_id in self.chapter.objects:
                    self.canvas.set_selection("object", raster_id)
            if not self.canvas.set_tool(tool):
                self.statusBar().showMessage(
                    "Select a Raster or Vector Drawing, or create one first",
                    4000,
                )
                self._sync_tool_buttons()
                return False
        else:
            target = tool
            if (
                tool == ToolKind.SHAPE_EDIT and vector_selected
                and getattr(ToolKind, "VECTOR_EDIT", None) is not None
            ):
                target = ToolKind.VECTOR_EDIT
            vector_only = {
                candidate for name in (
                    "VECTOR_EDIT", "VECTOR_REDRAW", "VECTOR_CONNECT",
                    "VECTOR_SIMPLIFY",
                    "DRAW_SELECT_STROKE",
                )
                if (candidate := getattr(ToolKind, name, None)) is not None
            }
            if target in vector_only and not vector_selected:
                self.statusBar().showMessage(
                    "Select a Vector Drawing first", 4000
                )
                self._sync_tool_buttons()
                return False
            if not self.canvas.set_tool(target):
                return False
        self._sync_tool_buttons()
        return True

    def _activate_named_tool(self, enum_name: str) -> bool:
        tool = getattr(ToolKind, enum_name, None)
        if tool is None:
            self.statusBar().showMessage(
                f"{enum_name.replace('_', ' ').title()} is unavailable",
                4000,
            )
            self._sync_tool_buttons()
            return False
        return self._activate_tool(tool)

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
        vector_edit = getattr(ToolKind, "VECTOR_EDIT", None)
        for tool, button in self.tool_buttons.items():
            button.blockSignals(True)
            button.setChecked(
                tool == self.canvas.tool
                or (
                    tool == ToolKind.SHAPE_EDIT
                    and self.canvas.tool == vector_edit
                )
            )
            button.blockSignals(False)
        selected_object = (
            self.chapter.objects.get(self.canvas.selected_id)
            if (
                self.chapter is not None
                and self.canvas.selected_kind == "object"
            )
            else None
        )
        raster_selected = isinstance(selected_object, RasterObject)
        vector_selected = isinstance(
            selected_object, VectorDrawingObject
        )
        drawable_selected = raster_selected or vector_selected
        self.drawing_selection_category.setVisible(drawable_selected)
        for tool, button in self.drawing_selection_buttons.items():
            button.setVisible(
                tool != ToolKind.DRAW_SELECT_STROKE or vector_selected
            )
            button.setEnabled(
                drawable_selected
                and (
                    tool != ToolKind.DRAW_SELECT_STROKE or vector_selected
                )
            )
            button.blockSignals(True)
            button.setChecked(self.canvas.tool == tool)
            button.blockSignals(False)
        self.tool_buttons[ToolKind.RASTER_PENCIL].setEnabled(
            drawable_selected
        )
        self.tool_buttons[ToolKind.RASTER_ERASER].setEnabled(
            drawable_selected
        )
        self.tool_buttons[ToolKind.RASTER_PENCIL].setVisible(
            drawable_selected
        )
        self.tool_buttons[ToolKind.RASTER_ERASER].setVisible(
            drawable_selected
        )
        shape_selected = (
            self.chapter is not None and self.canvas.selected_kind == "object"
            and isinstance(
                selected_object, (VectorDrawingObject, VectorFillObject)
            )
        ) or (
            self.chapter is not None
            and self.canvas.selected_kind == "layer"
            and self.canvas.selected_id in self.chapter.layers
            and not self.chapter.layers[self.canvas.selected_id].is_page
            and self.chapter.layers[self.canvas.selected_id].layer_kind
            != "fill"
        )
        self.fill_tool_button.setEnabled(bool(shape_selected))
        self.fill_tool_button.setVisible(bool(shape_selected))
        fill_tool = getattr(ToolKind, "FILL", None)
        self.fill_tool_button.blockSignals(True)
        self.fill_tool_button.setChecked(
            fill_tool is not None and self.canvas.tool == fill_tool
        )
        self.fill_tool_button.blockSignals(False)
        text_selected = isinstance(selected_object, TextObject)
        self.tool_buttons[ToolKind.TEXT_EDIT].setVisible(self.chapter is not None)
        transform_available = (
            text_selected
            and self.chapter.objects[self.canvas.selected_id].layout_mode == "free"
        )
        self.tool_buttons[ToolKind.TRANSFORM].setVisible(transform_available)
        self._sync_contextual_ribbon()

    def _active_vector_drawing(self) -> VectorDrawingObject | None:
        if (
            self.chapter is None
            or self.canvas.selected_kind != "object"
        ):
            return None
        candidate = self.chapter.objects.get(self.canvas.selected_id)
        return (
            candidate
            if isinstance(candidate, VectorDrawingObject)
            else None
        )

    def _gradient_context_parent_id(self) -> str:
        if self.chapter is None or not self.canvas.selected_id:
            return ""
        if self.canvas.selected_kind == "layer":
            layer = self.chapter.layers.get(self.canvas.selected_id)
            if layer is None:
                return ""
            if layer.layer_kind == "fill":
                return layer.parent_id or ""
            return layer.layer_id if layer.bound is not None else ""
        obj = self.chapter.objects.get(self.canvas.selected_id)
        if obj is None:
            return ""
        return (
            obj.parent_layer_id
            if obj.parent_layer_id in self.chapter.layers else ""
        )

    def _ribbon_page_changed(self, key: str) -> None:
        """Remember an explicit ribbon-tab choice across context refreshes."""
        if not self._programmatic_ribbon_selection:
            self._manual_ribbon_page = key

    def _select_ribbon_page(self, key: str) -> bool:
        """Select a page without treating an automatic route as a user choice."""
        self._programmatic_ribbon_selection += 1
        try:
            return self.ribbon.select_page(key)
        finally:
            self._programmatic_ribbon_selection -= 1

    def _sync_contextual_ribbon(self) -> None:
        if not hasattr(self, "ribbon"):
            return
        drawing = self._active_vector_drawing()
        active = drawing is not None
        selected_object = (
            self.chapter.objects.get(self.canvas.selected_id)
            if (
                self.chapter is not None
                and self.canvas.selected_kind == "object"
            )
            else None
        )
        vector_tool_context = isinstance(
            selected_object, (VectorDrawingObject, VectorFillObject)
        )
        raster_active = isinstance(selected_object, RasterObject)
        gradient_selected = isinstance(selected_object, GradientObject)
        gradient_parent_id = self._gradient_context_parent_id()
        gradient_active = bool(gradient_parent_id)
        entering = active and not self._vector_ribbon_context
        entering_raster = (
            raster_active and not self._raster_ribbon_context
        )
        selected_gradient_id = (
            selected_object.object_id if gradient_selected else ""
        )
        entering_gradient = bool(
            selected_gradient_id
            and selected_gradient_id != self._selected_gradient_ribbon_id
        )
        self._vector_ribbon_context = active
        self._raster_ribbon_context = raster_active
        self._gradient_ribbon_context = gradient_active
        self._selected_gradient_ribbon_id = selected_gradient_id
        self.ribbon.set_page_visible("vector_tools", active)
        self.ribbon.set_page_visible(
            "raster_object_settings", raster_active
        )
        self.ribbon.set_page_visible("gradient_tools", gradient_active)
        if (
            self._manual_ribbon_page
            and not self.ribbon.is_page_visible(self._manual_ribbon_page)
        ):
            self._manual_ribbon_page = ""
        if entering:
            self._select_ribbon_page("vector_tools")
        elif entering_raster:
            self._select_ribbon_page("raster_object_settings")
        elif entering_gradient:
            self._select_ribbon_page("gradient_tools")
        elif self._manual_ribbon_page:
            self._select_ribbon_page(self._manual_ribbon_page)
        self.tool_settings_controls.set_context(
            self.canvas.tool, vector_active=vector_tool_context
        )
        if drawing is not None:
            selected_points = len(
                getattr(
                    self.canvas, "selected_vector_point_ids",
                    getattr(self.canvas, "_selected_vector_point_ids", ()),
                )
            )
            selected_strokes = len(
                getattr(
                    self.canvas, "selected_vector_stroke_ids",
                    getattr(self.canvas, "_selected_vector_stroke_ids", ()),
                )
            )
            self.vector_tools_controls.set_selection_summary(
                selected_points, selected_strokes, len(drawing.strokes)
            )
        if raster_active:
            self.raster_object_controls.refresh()
        if gradient_active:
            self.gradient_tools_controls.refresh()
            self._sync_gradient_presets()

    def _apply_vector_redraw(self) -> None:
        method = getattr(self.canvas, "apply_vector_redraw", None)
        if method is None:
            self.statusBar().showMessage(
                "Vector redraw is unavailable", 4000
            )
            return
        changed = method(
            self.settings.vector_redraw_parameter,
            self.settings.vector_redraw_operation,
            self.settings.vector_redraw_amount,
        )
        if changed is False:
            self.statusBar().showMessage(
                "No vector points matched the current selection", 3000
            )

    def _apply_vector_simplify(self) -> None:
        method = getattr(self.canvas, "apply_vector_simplify", None)
        if method is None:
            self.statusBar().showMessage(
                "Vector simplify is unavailable", 4000
            )
            return
        changed = method(self.settings.vector_simplify_amount)
        if changed is False:
            self.statusBar().showMessage(
                "No vector strokes matched the current selection", 3000
            )

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

    def _show_selection_candidates(self, candidates, global_point) -> None:
        if self.chapter is None:
            return
        menu = QMenu(self)
        for candidate in candidates:
            kind = candidate.get("kind", "")
            entity_id = candidate.get("id", "")
            if kind == "object":
                entity = self.chapter.objects.get(entity_id)
                if entity is None:
                    continue
                parent = self.chapter.layers[entity.parent_layer_id]
                label = (
                    f"{entity.display_name if isinstance(entity, TextObject) else entity.name}"
                    f"  ·  {entity.object_type.replace('_', ' ').title()}"
                    f"  ·  {parent.name}"
                )
            else:
                entity = self.chapter.layers.get(entity_id)
                if entity is None:
                    continue
                parent = (
                    self.chapter.layers.get(entity.parent_id)
                    if entity.parent_id else None
                )
                label = (
                    f"{entity.name}  ·  Shape"
                    + (f"  ·  {parent.name}" if parent else "")
                )
            action = menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, selected_kind=kind,
                selected_id=entity_id:
                self.canvas.set_selection(
                    selected_kind, selected_id, activate_default_tool=True
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
        new_vector_id = ""
        if self.chapter is not None and kind == "object":
            selected = self.chapter.objects.get(entity_id)
            if isinstance(selected, VectorDrawingObject):
                new_vector_id = selected.object_id
            elif isinstance(selected, VectorFillObject):
                new_vector_id = selected.owner_drawing_id
        previous_vector_id = self._expanded_selected_vector_id
        if previous_vector_id and previous_vector_id != new_vector_id:
            old_index = self.hierarchy_model.index_for_entity(
                "object", previous_vector_id
            )
            if old_index.isValid():
                self.tree.setExpanded(old_index, False)
        if new_vector_id:
            vector_index = self.hierarchy_model.index_for_entity(
                "object", new_vector_id
            )
            if vector_index.isValid():
                self.tree.setExpanded(vector_index, True)
        self._expanded_selected_vector_id = new_vector_id
        self.inspector.refresh()
        self.layer_settings.refresh()
        self._sync_tool_buttons()
        selected_object = (
            self.chapter.objects.get(entity_id)
            if self.chapter is not None and kind == "object" else None
        )
        if (
            self.canvas.tool in {
                ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER,
            }
            and isinstance(selected_object, (RasterObject, VectorDrawingObject))
        ):
            # Selecting another drawing commonly leaves the contextual tool
            # unchanged, so Canvas does not emit toolChanged.  Route the
            # ribbon here as well as on explicit tool activation.
            self._select_ribbon_page("tool_settings")
        self._refresh_actions()

    def _canvas_tool_changed(self, tool: ToolKind) -> None:
        if tool == ToolKind.OBJECT_SELECT:
            self.inspector.hide()
        else:
            self.inspector.refresh()
        self._sync_tool_buttons()
        if tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}:
            # _sync_tool_buttons() refreshes contextual pages first.  Select
            # Tool Settings afterward so entering a raster/vector context
            # cannot immediately replace the user's pencil controls.
            self._select_ribbon_page("tool_settings")

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
        for object_id, obj in self.chapter.objects.items():
            if not isinstance(obj, VectorDrawingObject):
                continue
            index = self.hierarchy_model.index_for_entity(
                "object", object_id
            )
            if index.isValid() and self.tree.isExpanded(index):
                result.add(object_id)
        return result

    def _capture_hierarchy_view_state(self) -> None:
        self._hierarchy_reset_expanded = self._expanded_layer_ids()
        current = self.tree.currentIndex()
        if current.isValid():
            item = self.hierarchy_model.item_for_index(current)
            self._hierarchy_reset_selection = (item.kind, item.entity_id)
        else:
            self._hierarchy_reset_selection = (
                self.canvas.selected_kind, self.canvas.selected_id
            )

    def _restore_hierarchy_view_state(self) -> None:
        for entity_id in self._hierarchy_reset_expanded:
            kind = (
                "layer"
                if self.chapter is not None
                and entity_id in self.chapter.layers
                else "object"
            )
            index = self.hierarchy_model.index_for_entity(kind, entity_id)
            if index.isValid():
                self.tree.setExpanded(index, True)
        kind, entity_id = self._hierarchy_reset_selection
        if entity_id:
            index = self.hierarchy_model.index_for_entity(kind, entity_id)
            if index.isValid():
                blocker = QSignalBlocker(self.tree.selectionModel())
                self.tree.selectionModel().select(
                    index,
                    QItemSelectionModel.ClearAndSelect
                    | QItemSelectionModel.Rows,
                )
                self.tree.setCurrentIndex(index)
                del blocker

    def _refresh_hierarchy(self) -> None:
        expanded = self._expanded_layer_ids()
        selected = (self.canvas.selected_kind, self.canvas.selected_id)
        blocker = QSignalBlocker(self.tree.selectionModel())
        self.hierarchy_model.set_chapter(self.chapter)
        for entity_id in expanded:
            kind = (
                "layer" if entity_id in self.chapter.layers else "object"
            )
            index = self.hierarchy_model.index_for_entity(kind, entity_id)
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

    # ---- per-series colors and palettes -------------------------------
    def _sync_gradient_presets(self) -> None:
        presets = (
            self.series.gradient_ramp_presets
            if self.series is not None else []
        )
        self.gradient_tools_controls.set_presets(presets)
        obj = self.gradient_tools_controls.selected_gradient()
        if obj is not None:
            index = self.gradient_tools_controls.preset_combo.findData(
                obj.loaded_preset_id
            )
            self.gradient_tools_controls.preset_combo.setCurrentIndex(
                max(0, index)
            )
            self.gradient_tools_controls._sync_preset_name()

    def _gradient_preset_by_id(
        self, preset_id: str,
    ) -> ColorGradientRampPreset | None:
        if self.series is None:
            return None
        return next((
            preset for preset in self.series.gradient_ramp_presets
            if preset.preset_id == preset_id
        ), None)

    def _load_gradient_preset(self, preset_id: str) -> None:
        obj = self.gradient_tools_controls.selected_gradient()
        if obj is None or self.chapter is None:
            return
        if preset_id == BUILTIN_PRIMARY_SECONDARY_ID:
            primary = (
                self.series.primary_color
                if self.series is not None else self.color_panel.primary_color()
            )
            secondary = (
                self.series.secondary_color
                if self.series is not None
                else self.color_panel.secondary_color()
            )
            ramp = ColorGradientRamp(stops=[
                ColorGradientStop(position=0.0, color=primary),
                ColorGradientStop(position=1.0, color=secondary),
            ])
        else:
            preset = self._gradient_preset_by_id(preset_id)
            if preset is None:
                return
            ramp = preset.ramp.copy()
        before = self.chapter.to_dict()
        obj.ramp = ramp
        obj.loaded_preset_id = preset_id
        obj.touch_revision()
        after = self.chapter.to_dict()
        self.canvas.push_model_change(
            before, after, "Load gradient preset"
        )
        self.canvas.documentChanged.emit(None)
        self.gradient_tools_controls.refresh()

    def _add_gradient_preset(self) -> None:
        if self.series is None:
            return
        obj = self.gradient_tools_controls.selected_gradient()
        if obj is None:
            return
        used = {
            preset.name.casefold()
            for preset in self.series.gradient_ramp_presets
        }
        number = 1
        while f"Gradient {number}".casefold() in used:
            number += 1
        preset = ColorGradientRampPreset(
            name=f"Gradient {number}", ramp=obj.ramp.copy()
        )
        self.series.gradient_ramp_presets.append(preset)
        if self.chapter is not None:
            before = self.chapter.to_dict()
            obj.loaded_preset_id = preset.preset_id
            after = self.chapter.to_dict()
            self.canvas.push_model_change(
                before, after, "Associate gradient preset"
            )
        self._schedule_series_preferences_save(immediate=True)
        self._sync_gradient_presets()

    def _save_gradient_preset(self, preset_id: str) -> None:
        if preset_id == BUILTIN_PRIMARY_SECONDARY_ID:
            return
        preset = self._gradient_preset_by_id(preset_id)
        obj = self.gradient_tools_controls.selected_gradient()
        if preset is None or obj is None:
            return
        preset.ramp = obj.ramp.copy()
        preset.validate()
        obj.loaded_preset_id = preset_id
        self._schedule_series_preferences_save(immediate=True)
        self._sync_gradient_presets()

    def _rename_gradient_preset(
        self, preset_id: str, name: str,
    ) -> None:
        if preset_id == BUILTIN_PRIMARY_SECONDARY_ID:
            return
        preset = self._gradient_preset_by_id(preset_id)
        if preset is None:
            return
        candidate = name.strip() or "Gradient"
        if self.series is not None and any(
            other.preset_id != preset_id
            and other.name.casefold() == candidate.casefold()
            for other in self.series.gradient_ramp_presets
        ):
            self.statusBar().showMessage(
                "A gradient preset already has that name", 4000
            )
            self._sync_gradient_presets()
            return
        preset.name = candidate
        preset.validate()
        self._schedule_series_preferences_save(immediate=True)
        self._sync_gradient_presets()

    def _remove_gradient_preset(self, preset_id: str) -> None:
        if (
            self.series is None
            or preset_id == BUILTIN_PRIMARY_SECONDARY_ID
            or len(self.series.gradient_ramp_presets) <= 1
        ):
            return
        self.series.gradient_ramp_presets = [
            preset for preset in self.series.gradient_ramp_presets
            if preset.preset_id != preset_id
        ]
        if self.chapter is not None:
            changed = False
            for obj in self.chapter.objects.values():
                if (
                    isinstance(obj, ColorFillGradientObject)
                    and obj.loaded_preset_id == preset_id
                ):
                    obj.loaded_preset_id = ""
                    changed = True
            if changed:
                self._mark_dirty(None)
        self._schedule_series_preferences_save(immediate=True)
        self._sync_gradient_presets()

    def _sync_series_color_ui(self) -> None:
        if self.series is None:
            primary, secondary = "#FF000000", "#FFFFFFFF"
            palettes, active_palette = [], None
        else:
            self.series.validate()
            primary = canonical_argb(self.series.primary_color)
            secondary = canonical_argb(
                self.series.secondary_color, "#FFFFFFFF"
            )
            palettes = self.series.palettes
            active_palette = self.series.active_palette_id
        self.color_panel.set_colors(primary, secondary, emit=False)
        self.palette_editor.set_palettes(
            palettes, active_palette, emit=False
        )
        self.palette_editor.set_new_swatch_color(
            self.color_panel.active_color()
        )
        self._apply_series_colors_to_canvas(primary, secondary)
        self._sync_gradient_presets()

    def _apply_series_colors_to_canvas(
        self, primary: str, secondary: str
    ) -> None:
        primary = canonical_argb(primary)
        secondary = canonical_argb(secondary, "#FFFFFFFF")
        # brush_color remains the compatibility bridge for older canvas
        # paths and legacy settings, while the canvas owns the two-slot state.
        self.settings.brush_color = primary
        setattr(self.settings, "primary_color", primary)
        setattr(self.settings, "secondary_color", secondary)
        method = getattr(self.canvas, "set_active_colors", None)
        if method is not None:
            method(primary, secondary)
        else:
            self.canvas.refresh_brush_settings()
            self.canvas.update()

    def _series_color_changed(self, slot: str, color: str) -> None:
        color = canonical_argb(color)
        if self.series is not None:
            if slot == "primary":
                self.series.primary_color = color
            elif slot == "secondary":
                self.series.secondary_color = color
        primary = (
            self.series.primary_color
            if self.series is not None else self.color_panel.primary_color()
        )
        secondary = (
            self.series.secondary_color
            if self.series is not None else self.color_panel.secondary_color()
        )
        self._apply_series_colors_to_canvas(primary, secondary)
        self.palette_editor.set_new_swatch_color(color)
        self._schedule_series_preferences_save()

    def _series_colors_swapped(
        self, primary: str, secondary: str,
    ) -> None:
        primary = canonical_argb(primary)
        secondary = canonical_argb(secondary, "#FFFFFFFF")
        if self.series is not None:
            self.series.primary_color = primary
            self.series.secondary_color = secondary
        self._apply_series_colors_to_canvas(primary, secondary)
        self.palette_editor.set_new_swatch_color(
            self.color_panel.active_color()
        )
        self._schedule_series_preferences_save(immediate=True)

    def _palette_by_id(self, palette_id: str) -> ColorPalette | None:
        if self.series is None:
            return None
        return next(
            (
                palette for palette in self.series.palettes
                if palette.palette_id == palette_id
            ),
            None,
        )

    def _palette_selected(self, palette_id: str) -> None:
        if self.series is None or self._palette_by_id(palette_id) is None:
            return
        self.series.active_palette_id = palette_id
        self._schedule_series_preferences_save()

    def _add_palette(self) -> None:
        if self.series is None:
            return
        used_names = {item.name.casefold() for item in self.series.palettes}
        number = 1
        while f"Palette {number}".casefold() in used_names:
            number += 1
        palette = ColorPalette(name=f"Palette {number}")
        self.series.palettes.append(palette)
        self.series.active_palette_id = palette.palette_id
        self.palette_editor.set_palettes(
            self.series.palettes, palette.palette_id
        )
        self._schedule_series_preferences_save(immediate=True)

    def _remove_palette(self, palette_id: str) -> None:
        if self.series is None or len(self.series.palettes) <= 1:
            return
        old_index = next(
            (
                index for index, palette in enumerate(self.series.palettes)
                if palette.palette_id == palette_id
            ),
            -1,
        )
        if old_index < 0:
            return
        self.series.palettes.pop(old_index)
        replacement = self.series.palettes[
            min(old_index, len(self.series.palettes) - 1)
        ]
        self.series.active_palette_id = replacement.palette_id
        self.palette_editor.set_palettes(
            self.series.palettes, replacement.palette_id
        )
        self._schedule_series_preferences_save(immediate=True)

    def _rename_palette(self, palette_id: str, name: str) -> None:
        palette = self._palette_by_id(palette_id)
        if palette is None:
            return
        palette.name = name.strip() or "Palette"
        palette.validate()
        self._schedule_series_preferences_save(immediate=True)

    def _palette_swatch_activated(
        self, palette_id: str, swatch_id: str, color: str
    ) -> None:
        del palette_id, swatch_id
        self.color_panel.apply_color(color, emit=True)

    def _change_palette_swatch(
        self, palette_id: str, swatch_id: str, color: str
    ) -> None:
        palette = self._palette_by_id(palette_id)
        if palette is None:
            return
        swatch = next(
            (
                item for item in palette.swatches
                if item.swatch_id == swatch_id
            ),
            None,
        )
        if swatch is None:
            return
        swatch.color = canonical_argb(color)
        self._schedule_series_preferences_save(immediate=True)

    def _add_palette_swatch(
        self, palette_id: str, color: str
    ) -> None:
        palette = self._palette_by_id(palette_id)
        if palette is None:
            return
        palette.swatches.append(
            PaletteSwatch(color=canonical_argb(color))
        )
        self.palette_editor.set_palettes(
            self.series.palettes, palette_id
        )
        self._schedule_series_preferences_save(immediate=True)

    def _remove_palette_swatch(
        self, palette_id: str, swatch_id: str
    ) -> None:
        palette = self._palette_by_id(palette_id)
        if palette is None:
            return
        before = len(palette.swatches)
        palette.swatches = [
            item for item in palette.swatches
            if item.swatch_id != swatch_id
        ]
        if len(palette.swatches) == before:
            return
        self.palette_editor.set_palettes(
            self.series.palettes, palette_id
        )
        self._schedule_series_preferences_save(immediate=True)

    def _schedule_series_preferences_save(
        self, *, immediate: bool = False
    ) -> None:
        if self.repository is None or self.series is None:
            return
        if immediate:
            self._flush_series_preferences()
        else:
            self.series_preferences_timer.start(250)

    def _flush_series_preferences(self) -> None:
        self.series_preferences_timer.stop()
        if self.repository is None or self.series is None:
            return
        try:
            self.repository.save_series(self.series)
        except (OSError, ValueError) as error:
            self.statusBar().showMessage(
                f"Unable to save color preferences: {error}", 7000
            )

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
        self.settings.snap_to_grid = self.snap_grid.isChecked()
        save_settings(self.settings)
        self.canvas.update()

    def _ribbon_settings_changed(self) -> None:
        self.settings.clamp()
        save_settings(self.settings)
        self.tool_settings_controls.refresh()
        self.vector_tools_controls.refresh()
        self.raster_object_controls.refresh()
        self.canvas.refresh_brush_settings()
        self.canvas.update()

    def _pencil_preset_selected(self, name: str) -> None:
        if not name:
            return
        self.settings.active_pencil_preset = name
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()
        self.tool_settings_controls.refresh()

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
        self.tool_settings_controls.refresh()

    def _eraser_shape_selected(self, square: bool) -> None:
        self.settings.eraser_square = bool(square)
        save_settings(self.settings)
        self.canvas.refresh_brush_settings()
        self.tool_settings_controls.refresh()

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
        self.tool_settings_controls.refresh()
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
        self.tool_settings_controls.refresh()

    def _set_text_shortcut_suppression(self, editing: bool) -> None:
        self._hotkey_text_editing = editing

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
        dialog = HotkeysDialog(
            self.settings.hotkeys, self.settings.hotkey_hold, self
        )
        self._hotkey_runtime_enabled = False
        try:
            result = dialog.exec()
        finally:
            self._hotkey_runtime_enabled = True
            self._hotkey_prefix_timer.stop()
            self._hotkey_pending = None
            self._hotkey_pressed.clear()
            self._restore_active_hotkey_tool()
        if result != QDialog.DialogCode.Accepted:
            return
        try:
            self.settings.hotkeys = dialog.bindings()
        except ValueError as error:
            QMessageBox.warning(self, "Invalid hotkeys", str(error))
            return
        self.settings.hotkey_hold = dialog.hold_bindings()
        save_settings(self.settings)
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
        self.add_vector_button.setEnabled(active)
        self.add_text_button.setEnabled(active)
        self.add_fill_button.setEnabled(active)
        self._sync_tool_buttons()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if not self._confirm_discard_or_save():
            event.ignore()
            return
        self._flush_series_preferences()
        self.layout_settings_timer.stop()
        self._save_workspace_layout()
        if getattr(self, "_application_event_filter_installed", False):
            application = QApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
            self._application_event_filter_installed = False
        event.accept()
