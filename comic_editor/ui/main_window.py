"""Standalone series/chapter application shell."""
from __future__ import annotations

import copy
import json
import re
import time
import math
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication, QEvent, QItemSelection, QItemSelectionModel, QModelIndex,
    QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSignalBlocker, QSize,
    QSaveFile, QTimer, Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction, QCloseEvent, QColor, QCursor, QImage, QImageReader, QKeySequence,
    QMouseEvent, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractItemView, QAbstractSpinBox, QApplication, QCheckBox, QComboBox,
    QDockWidget,
    QFileDialog, QHBoxLayout,
    QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QInputDialog, QLabel,
    QDoubleSpinBox, QLineEdit, QListWidget, QMainWindow, QMenu, QMessageBox,
    QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTabBar, QTabWidget, QTextEdit,
    QTableWidget, QTableWidgetItem, QToolBar, QToolButton, QToolTip, QTreeView,
    QVBoxLayout, QWidget, QFrame,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientRampPreset, ColorGradientStop,
    ColorPalette, GradientObject, LayerNode, PaletteSwatch, PathNode,
    ImageObject, RasterObject, SpeedLineCenterObject, SpeedLinesGradientObject,
    TextObject, VectorDrawingObject, VectorFillObject, new_id,
)
from comic_editor.core.assets import (
    AssetManifest, AssetRepository, entity_visual_bounds, extract_asset,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.commands import CallbackCommand, CommandStack
from comic_editor.core.settings import load_settings, save_settings
from comic_editor.core.tiles import TileStore
from comic_editor.core.images import ImageStore
from comic_editor.ui.canvas import ToolKind, create_canvas
from comic_editor.ui.color_picker import (
    PaletteEditorWidget, PrimarySecondaryColorPanel, canonical_argb,
)
from comic_editor.ui.selection_settings import (
    SelectionCommonControls, SelectionSettingsPanel,
)
from comic_editor.ui.icons import iconoir
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
    TextObjectControls, ToolSettingsControls, VectorToolsControls,
)
from comic_editor.ui.tree_model import HierarchyModel
from comic_editor.ui.asset_library import AssetLibraryWidget
from comic_editor.ui.sessions import EditorSession, ProjectContext
from comic_editor.ui.windows_input import tablet_multitouch_native_result
from comic_editor.ui.three_d import ThreeDToolKind, ThreeDViewportController
from comic_editor.ui.three_d_sync import ThreeDSyncCoordinator
from comic_editor.three_d.documents import (
    BlenderChapterDocument, ComicFrameDocument, DrawingMaterial3D,
)
from comic_editor.three_d.repository import BlenderSidecarData
from comic_editor.three_d.protocol import (
    ConflictResolution, grouped_conflicts,
)


class ResponsiveToolButton(QToolButton):
    """Fixed-height command button that reveals its label when it fits."""

    def __init__(
        self, label: str, icon_name: str, parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.command_label = label
        self.setText(label)
        self.setIcon(iconoir(icon_name))
        self.setIconSize(QSize(20, 20))
        self.setToolTip(label)
        self.setMinimumWidth(36)
        self.setFixedHeight(36)
        self.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Fixed
        )
        self._sync_style()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._sync_style()

    def _sync_style(self) -> None:
        required = 20 + 7 + self.fontMetrics().horizontalAdvance(
            self.command_label
        ) + 18
        self.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
            if self.width() >= required
            else Qt.ToolButtonStyle.ToolButtonIconOnly
        )


class CollapsibleToolCategory(QWidget):
    """A single toolbar widget containing a persistent collapsible group."""

    expandedChanged = Signal(bool)

    def __init__(self, title: str, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        self.header = ResponsiveToolButton(
            title, "nav-arrow-right", self
        )
        self.header.setObjectName("shapeCategoryHeader")
        self.header.setText(title)
        self.header.setCheckable(False)
        self.header.setAutoRaise(True)
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

    def addTool(self, text: str, icon_name: str) -> QToolButton:
        button = ResponsiveToolButton(text, icon_name, self.contents)
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
        self.header.setIcon(iconoir(
            "nav-arrow-down" if expanded else "nav-arrow-right"
        ))
        self.updateGeometry()
        self.expandedChanged.emit(expanded)


class ScrollableToolPanel(QWidget):
    """A vertically scrolling tool column with toolbar-compatible helpers."""

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setObjectName("toolPanel")
        self.setMinimumWidth(36)
        self.setMaximumWidth(260)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(0, 0, 0, 0)
        self.scroll_area = QScrollArea(self)
        self.scroll_area.setObjectName("toolPanelScroll")
        self.scroll_area.setWidgetResizable(True)
        self.scroll_area.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.scroll_area.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.contents = QWidget(self.scroll_area)
        self.contents.setObjectName("toolPanelContents")
        self.contents_layout = QVBoxLayout(self.contents)
        self.contents_layout.setContentsMargins(0, 2, 0, 2)
        self.contents_layout.setSpacing(2)
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


class NavigatorPanel(QWidget):
    """A slim persistent rail with an optional chapter preview."""

    expandedChanged = Signal(bool)

    def __init__(
        self, canvas, expanded: bool = False, parent: QWidget | None = None,
    ):
        super().__init__(parent)
        layout = QHBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        rail = QWidget(self)
        rail.setFixedWidth(24)
        rail_layout = QVBoxLayout(rail)
        rail_layout.setContentsMargins(0, 2, 0, 0)
        self.toggle = QToolButton(rail)
        self.toggle.setFixedSize(24, 32)
        rail_layout.addWidget(self.toggle)
        rail_layout.addStretch(1)
        self.preview = ChapterPreview(canvas, self)
        layout.addWidget(rail)
        layout.addWidget(self.preview)
        self.toggle.clicked.connect(
            lambda checked=False: self.setExpanded(not self.isExpanded())
        )
        self.setExpanded(expanded, emit=False)

    def isExpanded(self) -> bool:
        return not self.preview.isHidden()

    def setExpanded(self, expanded: bool, *, emit: bool = True) -> None:
        expanded = bool(expanded)
        self.preview.setVisible(expanded)
        self.toggle.setText("›" if expanded else "‹")
        self.toggle.setToolTip(
            "Collapse page navigator" if expanded
            else "Expand page navigator"
        )
        self.setFixedWidth(116 if expanded else 24)
        if emit:
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
        self.sessions: dict[str, EditorSession] = {}
        self.active_session: EditorSession | None = None
        self._switching_session = False
        self._dirty = False
        self._last_autosave = 0.0
        self._loading_chapter = False
        self._three_d_scene_cache: dict[str, object] = {}
        self._build_ui()
        self.three_d_sync_manager = ThreeDSyncCoordinator(self)
        self._connect()
        self._install_shortcuts()
        self._refresh_actions()
        self.statusBar().showMessage("Create or open a series")

    def nativeEvent(self, event_type, message):  # noqa: N802
        """Opt this window into finger input while a pen is hovering."""
        settings = getattr(self, "settings", None)
        result = tablet_multitouch_native_result(
            event_type, message,
            bool(settings is not None and settings.tablet_mode),
        )
        if result is not None:
            return True, result
        return super().nativeEvent(event_type, message)

    # ---- UI ------------------------------------------------------------
    def _build_ui(self) -> None:
        style = Path(__file__).with_name("style.qss")
        if style.is_file():
            QApplication.instance().setStyleSheet(style.read_text(encoding="utf-8"))

        self.file_menu = QMenu("File", self)
        self.new_series_action = self.file_menu.addAction("New Series")
        self.open_series_action = self.file_menu.addAction("Open Series")
        self.open_recent_menu = self.file_menu.addMenu("Open Recent")
        self.import_images_action = self.file_menu.addAction("Import Images…")
        self.file_menu.addSeparator()
        self.save_action = self.file_menu.addAction("Save")
        self.save_as_action = self.file_menu.addAction("Save As")
        self._rebuild_recent_menu()

        self.file_toolbar = QToolBar("Project", self)
        self.file_toolbar.setMovable(False)
        self.addToolBar(self.file_toolbar)
        self.file_button = QToolButton(self.file_toolbar)
        self.file_button.setText("File")
        self.file_button.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.file_button.setMenu(self.file_menu)
        self.file_toolbar.addWidget(self.file_button)
        self.file_toolbar.addSeparator()
        self.undo_action = self.file_toolbar.addAction("Undo")
        self.redo_action = self.file_toolbar.addAction("Redo")
        self.file_toolbar.addSeparator()
        self.hotkeys_action = self.file_toolbar.addAction("Hotkeys…")
        self.tablet_mode = QCheckBox("Tablet Navigation", self.file_toolbar)
        self.tablet_mode.setChecked(self.settings.tablet_mode)
        self.reset_view_button = QToolButton(self.file_toolbar)
        self.reset_view_button.setText("Reset View")
        self.reset_view_button.setToolButtonStyle(
            Qt.ToolButtonStyle.ToolButtonTextOnly
        )
        self.snap_grid = QCheckBox("Snap to grid", self.file_toolbar)
        self.snap_grid.setChecked(self.settings.snap_to_grid)
        self.file_toolbar.addWidget(self.tablet_mode)
        self.file_toolbar.addWidget(self.reset_view_button)
        self.file_toolbar.addWidget(self.snap_grid)
        self.file_toolbar.addSeparator()
        self.chapter_combo = QComboBox()
        self.chapter_combo.setMinimumWidth(190)
        self.file_toolbar.addWidget(QLabel("Chapter"))
        self.file_toolbar.addWidget(self.chapter_combo)
        self.new_chapter_action = self.file_toolbar.addAction("New Chapter")
        self.trim_action = self.file_toolbar.addAction("Trim Height")
        self.fullscreen_action = self.file_toolbar.addAction("Fullscreen")

        self.tool_toolbar = ScrollableToolPanel(self)
        self.tool_buttons: dict[ToolKind, QToolButton] = {}
        labels = [
            (ToolKind.OBJECT_SELECT, "Object Select", "cursor-pointer"),
            (ToolKind.RASTER_PENCIL, "Pencil", "design-pencil"),
            (ToolKind.RASTER_ERASER, "Eraser", "erase"),
        ]
        fill_tool = getattr(ToolKind, "FILL", None)
        if fill_tool is not None:
            labels.append((fill_tool, "Fill", "fill-color"))
        labels.extend([
            (ToolKind.TEXT_EDIT, "Text Edit", "text"),
            (ToolKind.TRANSFORM, "Transform", "frame-tool"),
            (ToolKind.SHAPE_EDIT, "Shape Edit", "edit-pencil"),
            (
                ToolKind.INSERT_PAGE_GAP, "Insert Page Gap",
                "split-square-dashed",
            ),
        ])
        for tool, label, icon_name in labels:
            button = ResponsiveToolButton(label, icon_name)
            button.setCheckable(True)
            button.clicked.connect(lambda checked=False, selected=tool: self._activate_tool(selected))
            self.tool_toolbar.addWidget(button)
            self.tool_buttons[tool] = button
        if fill_tool is not None:
            self.fill_tool_button = self.tool_buttons[fill_tool]
        else:
            # Keeps the UI importable while a renderer without vector/fill
            # support is being upgraded in-place.
            self.fill_tool_button = ResponsiveToolButton(
                "Fill", "fill-color"
            )
            self.fill_tool_button.setCheckable(True)
            self.fill_tool_button.clicked.connect(
                lambda checked=False: self._activate_named_tool("FILL")
            )
            self.tool_toolbar.addWidget(self.fill_tool_button)
        self.shapes_category = CollapsibleToolCategory("Shapes")
        self.tool_toolbar.addWidget(self.shapes_category)
        self.shape_tool_buttons: dict[ToolKind, QToolButton] = {}
        for label, icon_name, tool in (
            ("Rectangle", "plus-square-dashed", ToolKind.BOX_BOUND),
            ("Circle", "circle", ToolKind.CIRCLE_BOUND),
            ("Free Shape", "path-arrow", ToolKind.SHAPE_CREATE),
        ):
            option = self.shapes_category.addTool(label, icon_name)
            option.clicked.connect(
                lambda checked=False, selected=tool:
                self._activate_tool(selected)
            )
            self.shape_tool_buttons[tool] = option
        self.add_blender_layer_button = self.shapes_category.addTool(
            "3D Layer", "plus-square-dashed"
        )
        blender_shape_menu = QMenu(self.add_blender_layer_button)
        for label, kind in (
            ("Rectangle 3D Layer", "rectangle"),
            ("Ellipse 3D Layer", "circle"),
            ("Free Closed 3D Layer", "custom"),
        ):
            action = blender_shape_menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, selected=kind:
                self._begin_blender_layer_creation(selected)
            )
        self.add_blender_layer_button.setMenu(blender_shape_menu)
        self.add_blender_layer_button.setPopupMode(
            QToolButton.ToolButtonPopupMode.InstantPopup
        )
        self.drawing_selection_category = CollapsibleToolCategory(
            "Drawing Selection"
        )
        self.tool_toolbar.addWidget(self.drawing_selection_category)
        self.drawing_selection_buttons: dict[ToolKind, QToolButton] = {}
        for label, icon_name, tool in (
            ("Rectangle Select", "select-window", ToolKind.DRAW_SELECT_RECT),
            ("Lasso Select", "selective-tool", ToolKind.DRAW_SELECT_LASSO),
            ("Stroke Select", "frame-select", ToolKind.DRAW_SELECT_STROKE),
        ):
            option = self.drawing_selection_category.addTool(label, icon_name)
            option.setCheckable(True)
            option.clicked.connect(
                lambda checked=False, selected=tool:
                self._activate_tool(selected)
            )
            self.drawing_selection_buttons[tool] = option
        self.tool_toolbar.addSeparator()
        self.add_page_button = ResponsiveToolButton("Add Page", "page-plus")
        self.add_text_button = ResponsiveToolButton("Add Text", "text-square")
        self.add_raster_button = ResponsiveToolButton(
            "Add Raster", "media-image-plus"
        )
        self.add_vector_button = ResponsiveToolButton(
            "Add Vector Drawing", "curve-array"
        )
        self.add_fill_button = ResponsiveToolButton("Add Fill", "fill-color")
        self.tool_toolbar.addWidget(self.add_page_button)
        self.tool_toolbar.addWidget(self.add_fill_button)
        self.tool_toolbar.addWidget(self.add_text_button)
        self.tool_toolbar.addWidget(self.add_raster_button)
        self.tool_toolbar.addWidget(self.add_vector_button)
        self.three_d_tool_buttons: dict[ThreeDToolKind, QToolButton] = {}
        for tool, label, icon_name in (
            (ThreeDToolKind.TRANSFORM, "Transform Object", "frame-tool"),
            (ThreeDToolKind.ADD_LIGHT, "Add Light", "circle"),
            (ThreeDToolKind.DRAW_CUBE, "Draw Cube", "plus-square-dashed"),
            (ThreeDToolKind.DRAW_CYLINDER, "Draw Cylinder", "plus-square-dashed"),
            (ThreeDToolKind.SELECT_RECT, "Rectangle Select", "select-window"),
            (ThreeDToolKind.SELECT_LASSO, "Lasso Select", "selective-tool"),
        ):
            button = ResponsiveToolButton(label, icon_name)
            button.setCheckable(True)
            button.clicked.connect(
                lambda checked=False, selected=tool:
                self._activate_three_d_tool(selected)
            )
            button.hide()
            self.tool_toolbar.addWidget(button)
            self.three_d_tool_buttons[tool] = button
        light_menu = QMenu(self)
        for label, light_type in (
            ("Sun", "sun"), ("Point", "point"),
            ("Rectangle", "rectangle"), ("Spot", "spot"),
        ):
            action = light_menu.addAction(label)
            action.triggered.connect(
                lambda checked=False, selected=light_type:
                self._select_three_d_light_type(selected)
            )
        self.three_d_tool_buttons[ThreeDToolKind.ADD_LIGHT].setMenu(light_menu)
        self.three_d_tool_buttons[ThreeDToolKind.ADD_LIGHT].setPopupMode(
            QToolButton.ToolButtonPopupMode.MenuButtonPopup
        )
        self.page_scope = QCheckBox("Select in page")
        self.page_scope.setChecked(self.settings.page_scope_select)
        # Kept as an attribute for compatibility with older integrations;
        # entity selection now always searches the complete chapter.
        self.page_scope.hide()
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

        self.ribbon = RibbonWidget(
            self, orientation=Qt.Orientation.Vertical
        )
        self.ribbon.setMinimumWidth(220)

        self.tool_settings_page = self.ribbon.add_page(
            "tool_settings", "Tool Settings"
        )
        self.tool_settings_group = self.tool_settings_page.add_group(
            "Current tool", minimum_width=720
        )
        self.tool_settings_controls = ToolSettingsControls(
            self.settings, self.ribbon
        )
        self.tool_settings_group.add_widget(self.tool_settings_controls)

        self.asset_library_page = self.ribbon.add_page(
            "asset_library", "Asset Library"
        )
        asset_group = self.asset_library_page.add_group(
            "", minimum_width=760
        )
        asset_group.title_label.hide()
        # The vertical ribbon page has spare viewport height below its
        # minimum-sized groups.  Let this page's sole group consume that
        # space so the grid grows and its footer remains docked at the bottom
        # instead of floating above the color palette.
        asset_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.asset_library_page.groups_layout.setStretch(0, 1)
        self.asset_library_page.groups_layout.setStretch(
            self.asset_library_page.groups_layout.count() - 1, 0
        )
        self.asset_library = AssetLibraryWidget(self.ribbon)
        self.asset_library.setMinimumHeight(220)
        self.asset_library.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        asset_group.add_widget(self.asset_library)

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
        self.canvas = create_canvas(self.settings)
        self.three_d_controller = ThreeDViewportController(
            self, scene_provider=self._three_d_scene_for_frame
        )
        self.canvas.set_three_d_controller(self.three_d_controller)
        self._build_three_d_ribbon()
        self.text_object_controls = TextObjectControls(
            self.canvas, self.settings, self.ribbon
        )
        self.text_object_group = self.tool_settings_page.add_group(
            "Text Object & Presets", minimum_width=360
        )
        self.text_object_group.add_widget(
            self.text_object_controls.object_widget
        )
        self.text_typography_group = self.tool_settings_page.add_group(
            "Typography", minimum_width=430
        )
        self.text_typography_group.add_widget(
            self.text_object_controls.typography_widget
        )
        self.text_layout_group = self.tool_settings_page.add_group(
            "Text Layout", minimum_width=430
        )
        self.text_layout_group.add_widget(
            self.text_object_controls.layout_widget
        )
        for group in (
            self.text_object_group,
            self.text_typography_group,
            self.text_layout_group,
        ):
            group.hide()
        self.gradient_tools_page = self.ribbon.add_page(
            "gradient_tools", "Gradient Tools", visible=False
        )
        self.gradient_tools_controls = GradientToolsControls(
            self.canvas, self.ribbon
        )
        self.gradient_create_group = self.gradient_tools_page.add_group(
            "Create Gradient", minimum_width=420
        )
        self.gradient_create_group.add_widget(
            self.gradient_tools_controls.create_widget
        )
        self.gradient_type_group = self.gradient_tools_page.add_group(
            "Field & Direction", minimum_width=220
        )
        self.gradient_type_group.add_widget(
            self.gradient_tools_controls.type_parameters_widget
        )
        self.gradient_parameters_group = self.gradient_tools_page.add_group(
            "Color Parameters", minimum_width=230
        )
        self.gradient_parameters_group.add_widget(
            self.gradient_tools_controls.parameters_widget
        )
        self.gradient_thickness_group = self.gradient_tools_page.add_group(
            "Thickness Parameters", minimum_width=230
        )
        self.gradient_thickness_group.add_widget(
            self.gradient_tools_controls.thickness_widget
        )
        self.gradient_impact_group = self.gradient_tools_page.add_group(
            "Impact Line Parameters", minimum_width=460
        )
        self.gradient_impact_group.add_widget(
            self.gradient_tools_controls.impact_widget
        )
        self.gradient_tools_controls.contextChanged.connect(
            self._update_gradient_group_visibility
        )
        self._update_gradient_group_visibility("create")
        canvas_shell = QWidget(self)
        canvas_layout = QHBoxLayout(canvas_shell)
        canvas_layout.setContentsMargins(0, 0, 0, 0)
        canvas_layout.setSpacing(0)
        canvas_layout.addWidget(self.canvas, 1)
        self.navigator_panel = NavigatorPanel(
            self.canvas, self.settings.navigator_expanded, canvas_shell
        )
        self.preview = self.navigator_panel.preview
        canvas_layout.addWidget(self.navigator_panel)

        self.sidebar_splitter = QSplitter(Qt.Orientation.Vertical, self)
        self.sidebar_splitter.setObjectName("sidebarSplitter")
        self.sidebar_splitter.setChildrenCollapsible(False)
        self.sidebar_splitter.setHandleWidth(6)
        self.sidebar_splitter.setMinimumWidth(220)
        self.sidebar_splitter.addWidget(self.ribbon)
        self.sidebar_splitter.addWidget(self.color_tabs)
        self.sidebar_splitter.setStretchFactor(0, 1)
        self.sidebar_splitter.setStretchFactor(1, 1)

        self.tool_canvas_splitter = QSplitter(
            Qt.Orientation.Horizontal, self
        )
        self.tool_canvas_splitter.setObjectName("toolCanvasSplitter")
        self.tool_canvas_splitter.setChildrenCollapsible(False)
        self.tool_canvas_splitter.setHandleWidth(5)
        self.tool_canvas_splitter.addWidget(self.tool_toolbar)
        self.tool_canvas_splitter.addWidget(canvas_shell)
        self.tool_canvas_splitter.setStretchFactor(0, 0)
        self.tool_canvas_splitter.setStretchFactor(1, 1)

        self.workspace_splitter = QSplitter(
            Qt.Orientation.Horizontal, self
        )
        self.workspace_splitter.setObjectName("workspaceSplitter")
        self.workspace_splitter.setChildrenCollapsible(False)
        self.workspace_splitter.setHandleWidth(6)
        self.workspace_splitter.addWidget(self.sidebar_splitter)
        self.workspace_splitter.addWidget(self.tool_canvas_splitter)
        self.workspace_splitter.setStretchFactor(0, 0)
        self.workspace_splitter.setStretchFactor(1, 1)
        central = QWidget(self)
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(0, 0, 0, 0)
        central_layout.setSpacing(0)
        self.project_tabs = QTabBar(central)
        self.project_tabs.setObjectName("projectTabs")
        self.project_tabs.setTabsClosable(True)
        self.project_tabs.setMovable(True)
        self.project_tabs.setExpanding(False)
        self.project_tabs.setUsesScrollButtons(True)
        central_layout.addWidget(self.project_tabs)
        central_layout.addWidget(self.workspace_splitter, 1)
        self.setCentralWidget(central)

        self.hierarchy_dock = QDockWidget("Layers and Objects", self)
        self.hierarchy_dock.setAllowedAreas(Qt.RightDockWidgetArea)
        self.hierarchy_dock.setFeatures(QDockWidget.DockWidgetFeature.NoDockWidgetFeatures)
        dock_title = QWidget(self.hierarchy_dock)
        dock_title.setFixedHeight(0)
        self.hierarchy_dock.setTitleBarWidget(dock_title)
        hierarchy_panel = QWidget()
        hierarchy_layout = QVBoxLayout(hierarchy_panel)
        hierarchy_layout.setContentsMargins(4, 4, 4, 4)
        hierarchy_layout.setSpacing(3)
        self.selection_common = SelectionCommonControls(
            self.canvas, hierarchy_panel
        )
        self.selection_settings = SelectionSettingsPanel(
            self.canvas, self.settings, save_settings, hierarchy_panel
        )
        # Keep the established layer/raster attributes available to internal
        # integrations while the visible host is now selection-driven.
        self.layer_settings = self.selection_settings.layer_page
        self.raster_object_controls = self.selection_settings.raster_controls
        self.settings_scroll = QScrollArea(hierarchy_panel)
        self.settings_scroll.setObjectName("selectionSettingsScroll")
        self.settings_scroll.setWidgetResizable(True)
        self.settings_scroll.setHorizontalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAlwaysOff
        )
        self.settings_scroll.setVerticalScrollBarPolicy(
            Qt.ScrollBarPolicy.ScrollBarAsNeeded
        )
        self.settings_scroll.setMinimumHeight(56)
        self.settings_scroll.setWidget(self.selection_settings)
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
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu)
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
        self.outliner_splitter = QSplitter(
            Qt.Orientation.Vertical, hierarchy_panel
        )
        self.outliner_splitter.setObjectName("outlinerSettingsSplitter")
        self.outliner_splitter.setChildrenCollapsible(False)
        self.outliner_splitter.setHandleWidth(5)
        settings_host = QWidget(hierarchy_panel)
        settings_host.setObjectName("selectionSettingsHost")
        settings_host_layout = QVBoxLayout(settings_host)
        settings_host_layout.setContentsMargins(0, 0, 0, 0)
        settings_host_layout.setSpacing(2)
        settings_host_layout.addWidget(self.settings_scroll, 1)
        self.selection_common.setMinimumHeight(30)
        settings_host_layout.addWidget(self.selection_common, 0)
        self.outliner_splitter.addWidget(settings_host)
        self.outliner_splitter.addWidget(self.tree)
        self.outliner_splitter.setStretchFactor(0, 0)
        self.outliner_splitter.setStretchFactor(1, 1)
        hierarchy_layout.addWidget(self.outliner_splitter, 1)

        self.hierarchy_dock.setWidget(hierarchy_panel)
        self.addDockWidget(Qt.RightDockWidgetArea, self.hierarchy_dock)
        self._restore_workspace_layout()
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
        self._text_ribbon_context = False
        self._gradient_ribbon_context = False
        self._selected_gradient_ribbon_id = ""
        self._manual_ribbon_page = ""
        self._programmatic_ribbon_selection = 0
        self._expanded_selected_vector_id = ""

    def _build_three_d_ribbon(self) -> None:
        """Create the six contextual pages used while a 3D layer is active."""
        self.three_d_view_page = self.ribbon.add_page(
            "three_d_view", "View", visible=False
        )
        view_group = self.three_d_view_page.add_group("Overlays")
        view_widget = QWidget(self.ribbon)
        view_layout = QVBoxLayout(view_widget)
        view_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_grid = QCheckBox("Grid", view_widget)
        self.three_d_volume_grid = QCheckBox("Volume grid", view_widget)
        self.three_d_axes = QCheckBox("Colored axes", view_widget)
        self.three_d_floor = QCheckBox("Neutral floor", view_widget)
        self.three_d_overlays = QCheckBox("Overlays", view_widget)
        for control, checked in (
            (self.three_d_grid, True), (self.three_d_volume_grid, False),
            (self.three_d_axes, True),
            (self.three_d_floor, True), (self.three_d_overlays, True),
        ):
            control.setChecked(checked)
            view_layout.addWidget(control)
        view_group.add_widget(view_widget)
        source_group = self.three_d_view_page.add_group("Blender source")
        source_widget = QWidget(self.ribbon)
        source_layout = QVBoxLayout(source_widget)
        source_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_source_status = QLabel("No Blender file linked", source_widget)
        self.three_d_source_status.setWordWrap(True)
        self.three_d_connection_status = QLabel(
            "Blender sync is inactive", source_widget
        )
        self.three_d_connection_status.setWordWrap(True)
        self.three_d_copy_sync = QPushButton(
            "Copy Add-on Connection Settings", source_widget
        )
        self.three_d_replace_source = QPushButton(
            "Link / Replace Blender File", source_widget
        )
        self.three_d_edit_boundary = QPushButton("Edit Boundary", source_widget)
        source_layout.addWidget(self.three_d_source_status)
        source_layout.addWidget(self.three_d_connection_status)
        source_layout.addWidget(self.three_d_copy_sync)
        source_layout.addWidget(self.three_d_replace_source)
        source_layout.addWidget(self.three_d_edit_boundary)
        source_group.add_widget(source_widget)

        self.three_d_rendering_page = self.ribbon.add_page(
            "three_d_rendering", "Rendering", visible=False
        )
        camera_group = self.three_d_rendering_page.add_group("Camera")
        camera_widget = QWidget(self.ribbon)
        camera_form = QFormLayout(camera_widget)
        camera_form.setContentsMargins(0, 0, 0, 0)
        self.three_d_projection = QComboBox(camera_widget)
        for label, value in (
            ("Perspective", "perspective"),
            ("Orthographic", "orthographic"),
            ("Fisheye - Equidistant", "fisheye_equidistant"),
            ("Fisheye - Equisolid", "fisheye_equisolid"),
            ("Fisheye - Stereographic", "fisheye_stereographic"),
            ("Fisheye - Orthographic", "fisheye_orthographic"),
        ):
            self.three_d_projection.addItem(label, value)
        self.three_d_fov = QDoubleSpinBox(camera_widget)
        self.three_d_fov.setRange(1.0, 179.0)
        self.three_d_fov.setValue(50.0)
        self.three_d_fov.setSuffix(" deg")
        self.three_d_ortho_height = QDoubleSpinBox(camera_widget)
        self.three_d_ortho_height.setRange(0.001, 100000.0)
        self.three_d_ortho_height.setValue(10.0)
        camera_form.addRow("Projection", self.three_d_projection)
        camera_form.addRow("FOV", self.three_d_fov)
        camera_form.addRow("Ortho height", self.three_d_ortho_height)
        camera_group.add_widget(camera_widget)
        quality_group = self.three_d_rendering_page.add_group("Quality")
        quality_widget = QWidget(self.ribbon)
        quality_layout = QVBoxLayout(quality_widget)
        quality_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_shadows = QCheckBox("Shadows", quality_widget)
        self.three_d_shadows.setChecked(True)
        self.three_d_shadow_quality = QComboBox(quality_widget)
        self.three_d_shadow_quality.addItem("Low shadows", "low")
        self.three_d_shadow_quality.addItem("Medium shadows", "medium")
        self.three_d_shadow_quality.addItem("High shadows", "high")
        self.three_d_fidelity = QComboBox(quality_widget)
        self.three_d_fidelity.addItem("Interactive", "interactive")
        self.three_d_fidelity.addItem("Full", "full")
        self.three_d_antialiasing = QCheckBox("4x MSAA", quality_widget)
        self.three_d_antialiasing.setChecked(False)
        for control in (
            self.three_d_shadows, self.three_d_shadow_quality,
            self.three_d_fidelity, self.three_d_antialiasing,
        ):
            quality_layout.addWidget(control)
        quality_group.add_widget(quality_widget)

        # Intentionally contains no controls in v1.  Freestyle marks remain
        # in sidecar metadata for the later Freestyle-like implementation.
        self.three_d_outline_page = self.ribbon.add_page(
            "three_d_outline", "Outline Settings", visible=False
        )

        self.three_d_materials_page = self.ribbon.add_page(
            "three_d_materials", "Materials", visible=False
        )
        materials_group = self.three_d_materials_page.add_group(
            "Drawing materials"
        )
        materials_widget = QWidget(self.ribbon)
        materials_layout = QVBoxLayout(materials_widget)
        materials_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_material_list = QListWidget(materials_widget)
        material_buttons = QHBoxLayout()
        self.three_d_material_add = QPushButton("Create", materials_widget)
        self.three_d_material_rename = QPushButton("Rename", materials_widget)
        self.three_d_material_delete = QPushButton("Delete", materials_widget)
        for button in (
            self.three_d_material_add, self.three_d_material_rename,
            self.three_d_material_delete,
        ):
            material_buttons.addWidget(button)
        self.three_d_material_shader = QComboBox(materials_widget)
        self.three_d_material_shader.addItem("Diffuse", "diffuse")
        self.three_d_material_shader.addItem("Toon", "toon")
        self.three_d_material_shader.addItem("Unshaded", "unshaded")
        self.three_d_material_texture = QCheckBox("Use texture", materials_widget)
        self.three_d_material_vertex = QCheckBox(
            "Use vertex color", materials_widget
        )
        material_properties = QFormLayout()
        material_properties.setContentsMargins(0, 0, 0, 0)
        self.three_d_material_tint = QLineEdit(materials_widget)
        self.three_d_material_tint.setPlaceholderText("#AARRGGBB")
        self.three_d_material_tint.setToolTip(
            "Drawing-side tint in #AARRGGBB format"
        )
        self.three_d_material_toon_ramp = QLineEdit(materials_widget)
        self.three_d_material_toon_ramp.setPlaceholderText(
            "0:#FF333333, 0.5:#FFAAAAAA, 1:#FFFFFFFF"
        )
        self.three_d_material_toon_ramp.setToolTip(
            "Comma-separated Toon stops in position:#AARRGGBB format"
        )
        self.three_d_material_outline = QCheckBox(
            "Enable material outline", materials_widget
        )
        self.three_d_material_outline_color = QLineEdit(materials_widget)
        self.three_d_material_outline_color.setPlaceholderText("#AARRGGBB")
        self.three_d_material_outline_width = QDoubleSpinBox(materials_widget)
        self.three_d_material_outline_width.setRange(0.0, 1000.0)
        self.three_d_material_outline_width.setDecimals(2)
        self.three_d_material_outline_width.setSingleStep(0.25)
        self.three_d_material_outline_width.setSuffix(" px")
        material_properties.addRow("Tint", self.three_d_material_tint)
        material_properties.addRow(
            "Toon ramp", self.three_d_material_toon_ramp
        )
        material_properties.addRow(
            "Outline color", self.three_d_material_outline_color
        )
        material_properties.addRow(
            "Outline width", self.three_d_material_outline_width
        )
        materials_layout.addWidget(self.three_d_material_list, 1)
        materials_layout.addLayout(material_buttons)
        materials_layout.addWidget(self.three_d_material_shader)
        materials_layout.addWidget(self.three_d_material_texture)
        materials_layout.addWidget(self.three_d_material_vertex)
        materials_layout.addLayout(material_properties)
        materials_layout.addWidget(self.three_d_material_outline)
        materials_group.add_widget(materials_widget)

        assignments_group = self.three_d_materials_page.add_group(
            "Source assignments"
        )
        assignments_widget = QWidget(self.ribbon)
        assignments_layout = QVBoxLayout(assignments_widget)
        assignments_layout.setContentsMargins(0, 0, 0, 0)
        self.three_d_material_mapping_table = QTableWidget(
            0, 3, assignments_widget
        )
        self.three_d_material_mapping_table.setHorizontalHeaderLabels((
            "Blender material", "Assigned objects / slots", "Drawing material",
        ))
        self.three_d_material_mapping_table.setEditTriggers(
            QAbstractItemView.EditTrigger.NoEditTriggers
        )
        self.three_d_material_mapping_table.setSelectionBehavior(
            QAbstractItemView.SelectionBehavior.SelectRows
        )
        header = self.three_d_material_mapping_table.horizontalHeader()
        header.setSectionResizeMode(0, QHeaderView.ResizeMode.ResizeToContents)
        header.setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeMode.ResizeToContents)
        assignments_layout.addWidget(self.three_d_material_mapping_table)
        assignments_group.add_widget(assignments_widget)

        self.three_d_object_page = self.ribbon.add_page(
            "three_d_object", "Object Properties", visible=False
        )
        transform_group = self.three_d_object_page.add_group("Transform")
        transform_widget = QWidget(self.ribbon)
        transform_form = QFormLayout(transform_widget)
        transform_form.setContentsMargins(0, 0, 0, 0)
        self.three_d_transform_space = QComboBox(transform_widget)
        self.three_d_transform_space.addItem("Global", "global")
        self.three_d_transform_space.addItem("Local", "local")
        self.three_d_gizmo_mode = QComboBox(transform_widget)
        for label, value in (
            ("Move", "move"), ("Rotate", "rotate"),
            ("Scale", "scale"), ("Trackball Rotate", "trackball"),
        ):
            self.three_d_gizmo_mode.addItem(label, value)
        transform_form.addRow("Space", self.three_d_transform_space)
        transform_form.addRow("Gizmo", self.three_d_gizmo_mode)
        self.three_d_transform_fields: list[QDoubleSpinBox] = []
        for label, minimum, maximum, initial in (
            ("Position X", -1_000_000.0, 1_000_000.0, 0.0),
            ("Position Y", -1_000_000.0, 1_000_000.0, 0.0),
            ("Position Z", -1_000_000.0, 1_000_000.0, 0.0),
            ("Rotation X", -360_000.0, 360_000.0, 0.0),
            ("Rotation Y", -360_000.0, 360_000.0, 0.0),
            ("Rotation Z", -360_000.0, 360_000.0, 0.0),
            ("Scale X", -10_000.0, 10_000.0, 1.0),
            ("Scale Y", -10_000.0, 10_000.0, 1.0),
            ("Scale Z", -10_000.0, 10_000.0, 1.0),
        ):
            field = QDoubleSpinBox(transform_widget)
            field.setDecimals(4)
            field.setRange(minimum, maximum)
            field.setValue(initial)
            field.setSingleStep(0.1 if "Rotation" not in label else 1.0)
            field.setEnabled(False)
            field.editingFinished.connect(self._edit_three_d_transform)
            transform_form.addRow(label, field)
            self.three_d_transform_fields.append(field)
        self.three_d_reset_object = QPushButton(
            "Reset to Blender", transform_widget
        )
        transform_form.addRow(self.three_d_reset_object)
        transform_group.add_widget(transform_widget)

        properties_group = self.three_d_object_page.add_group("Parameters")
        self.three_d_entity_properties_widget = QWidget(self.ribbon)
        properties_form = QFormLayout(self.three_d_entity_properties_widget)
        properties_form.setContentsMargins(0, 0, 0, 0)
        self.three_d_entity_kind = QLabel("No editable parameters", self.ribbon)
        properties_form.addRow("Entity", self.three_d_entity_kind)
        self.three_d_entity_property_rows: dict[str, tuple[QWidget, QWidget]] = {}
        self.three_d_entity_property_controls: list[QWidget] = []

        def add_property_row(name: str, label: str, control: QWidget) -> None:
            properties_form.addRow(label, control)
            row_label = properties_form.labelForField(control)
            self.three_d_entity_property_rows[name] = (row_label, control)
            self.three_d_entity_property_controls.append(control)

        def numeric_property(
            name: str, label: str, minimum: float, maximum: float,
            value: float, step: float = 0.1,
        ) -> QDoubleSpinBox:
            control = QDoubleSpinBox(self.three_d_entity_properties_widget)
            control.setDecimals(4)
            control.setRange(minimum, maximum)
            control.setValue(value)
            control.setSingleStep(step)
            control.editingFinished.connect(
                self._edit_three_d_entity_properties
            )
            add_property_row(name, label, control)
            return control

        self.three_d_cube_size_x = numeric_property(
            "size_x", "Size X", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_cube_size_y = numeric_property(
            "size_y", "Size Y", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_cube_size_z = numeric_property(
            "size_z", "Size Z", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_cylinder_radius = numeric_property(
            "radius", "Radius", 0.001, 1_000_000.0, 0.5
        )
        self.three_d_cylinder_depth = numeric_property(
            "depth", "Depth", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_cylinder_segments = QSpinBox(
            self.three_d_entity_properties_widget
        )
        self.three_d_cylinder_segments.setRange(3, 512)
        self.three_d_cylinder_segments.setValue(32)
        self.three_d_cylinder_segments.editingFinished.connect(
            self._edit_three_d_entity_properties
        )
        add_property_row(
            "segments", "Segments", self.three_d_cylinder_segments
        )

        self.three_d_light_type = QComboBox(
            self.three_d_entity_properties_widget
        )
        for label, value in (
            ("Sun", "sun"), ("Point", "point"),
            ("Rectangle", "rectangle"), ("Spot", "spot"),
        ):
            self.three_d_light_type.addItem(label, value)
        self.three_d_light_type.currentIndexChanged.connect(
            self._edit_three_d_entity_properties
        )
        add_property_row("light_type", "Type", self.three_d_light_type)
        self.three_d_light_color = QLineEdit(
            "#FFFFFFFF", self.three_d_entity_properties_widget
        )
        self.three_d_light_color.setPlaceholderText("#AARRGGBB")
        self.three_d_light_color.editingFinished.connect(
            self._edit_three_d_entity_properties
        )
        add_property_row("color", "Color", self.three_d_light_color)
        self.three_d_light_energy = numeric_property(
            "energy", "Energy", 0.0, 1_000_000_000.0, 1.0, 1.0
        )
        self.three_d_light_range = numeric_property(
            "range", "Range", 0.0, 1_000_000.0, 0.0
        )
        self.three_d_light_area_width = numeric_property(
            "area_width", "Area width", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_light_area_height = numeric_property(
            "area_height", "Area height", 0.001, 1_000_000.0, 1.0
        )
        self.three_d_light_spot_angle = numeric_property(
            "spot_angle", "Spot angle (deg)", 0.001, 179.999, 45.0, 1.0
        )
        self.three_d_light_shadow = QCheckBox(
            "Cast shadows", self.three_d_entity_properties_widget
        )
        self.three_d_light_shadow.toggled.connect(
            self._edit_three_d_entity_properties
        )
        add_property_row("casts_shadow", "Shadow", self.three_d_light_shadow)

        self.three_d_camera_type = QComboBox(
            self.three_d_entity_properties_widget
        )
        self.three_d_camera_type.addItem("Perspective", "perspective")
        self.three_d_camera_type.addItem("Orthographic", "orthographic")
        self.three_d_camera_type.currentIndexChanged.connect(
            self._edit_three_d_entity_properties
        )
        add_property_row("camera_type", "Projection", self.three_d_camera_type)
        self.three_d_camera_fov = numeric_property(
            "fov", "Field of view", 0.001, 179.999, 50.0, 1.0
        )
        self.three_d_camera_ortho_scale = numeric_property(
            "ortho_scale", "Ortho scale", 0.001, 1_000_000.0, 10.0
        )
        self.three_d_camera_clip_start = numeric_property(
            "clip_start", "Clip start", 0.000001, 1_000_000.0, 0.01
        )
        self.three_d_camera_clip_end = numeric_property(
            "clip_end", "Clip end", 0.000002, 1_000_000_000.0, 1000.0
        )
        properties_group.add_widget(self.three_d_entity_properties_widget)
        self.three_d_entity_properties_widget.setVisible(False)

        metadata_group = self.three_d_object_page.add_group("Blender metadata")
        self.three_d_object_metadata = QLabel(
            "Select an object in the Blender subtree.", self.ribbon
        )
        self.three_d_object_metadata.setWordWrap(True)
        metadata_group.add_widget(self.three_d_object_metadata)

        self.three_d_tool_settings_page = self.ribbon.add_page(
            "three_d_tool_settings", "Tool Settings", visible=False
        )
        select_group = self.three_d_tool_settings_page.add_group("Selection")
        self.three_d_multi_select = QCheckBox("Enable Multi Select", self.ribbon)
        self.three_d_multi_select.setChecked(False)
        select_group.add_widget(self.three_d_multi_select)

        for control, key in (
            (self.three_d_grid, "grid_visible"),
            (self.three_d_volume_grid, "volume_grid_visible"),
            (self.three_d_axes, "axes_visible"),
            (self.three_d_floor, "floor_visible"),
            (self.three_d_overlays, "overlays_visible"),
            (self.three_d_shadows, "shadows_enabled"),
            (self.three_d_antialiasing, "antialiasing"),
        ):
            control.toggled.connect(
                lambda enabled, setting=key:
                self._set_three_d_renderer_setting(setting, enabled)
            )
        self.three_d_projection.currentIndexChanged.connect(
            lambda _index: self._set_three_d_renderer_setting(
                "projection", self.three_d_projection.currentData()
            )
        )
        self.three_d_fov.valueChanged.connect(
            lambda value: self._set_three_d_renderer_setting("fov", value)
        )
        self.three_d_ortho_height.valueChanged.connect(
            lambda value: self._set_three_d_renderer_setting(
                "ortho_height", value
            )
        )
        self.three_d_shadow_quality.currentIndexChanged.connect(
            lambda _index: self._set_three_d_renderer_setting(
                "shadow_quality", self.three_d_shadow_quality.currentData()
            )
        )
        self.three_d_fidelity.currentIndexChanged.connect(
            lambda _index: self._set_three_d_renderer_setting(
                "quality", self.three_d_fidelity.currentData()
            )
        )
        self.three_d_multi_select.toggled.connect(
            self._three_d_multi_select_changed
        )
        self.three_d_transform_space.currentIndexChanged.connect(
            lambda _index: self.three_d_controller.set_transform_settings(
                space=self.three_d_transform_space.currentData()
            ) if not getattr(self, "_syncing_three_d_controls", False) else None
        )
        self.three_d_gizmo_mode.currentIndexChanged.connect(
            lambda _index: self.three_d_controller.set_transform_settings(
                mode=self.three_d_gizmo_mode.currentData()
            ) if not getattr(self, "_syncing_three_d_controls", False) else None
        )
        self.three_d_reset_object.clicked.connect(
            self._reset_three_d_object_overrides
        )
        self.three_d_edit_boundary.clicked.connect(
            self.canvas.begin_blender_boundary_edit
        )
        self.three_d_replace_source.clicked.connect(
            self._replace_blender_association
        )
        self.three_d_copy_sync.clicked.connect(
            self._copy_three_d_sync_registration
        )
        self.three_d_material_add.clicked.connect(self._add_three_d_material)
        self.three_d_material_rename.clicked.connect(
            self._rename_three_d_material
        )
        self.three_d_material_delete.clicked.connect(
            self._delete_three_d_material
        )
        self.three_d_material_list.currentRowChanged.connect(
            self._three_d_material_selected
        )
        self.three_d_material_shader.currentIndexChanged.connect(
            self._edit_three_d_material
        )
        self.three_d_material_texture.toggled.connect(
            self._edit_three_d_material
        )
        self.three_d_material_vertex.toggled.connect(
            self._edit_three_d_material
        )
        self.three_d_material_tint.editingFinished.connect(
            self._edit_three_d_material
        )
        self.three_d_material_toon_ramp.editingFinished.connect(
            self._edit_three_d_material
        )
        self.three_d_material_outline.toggled.connect(
            self._edit_three_d_material
        )
        self.three_d_material_outline_color.editingFinished.connect(
            self._edit_three_d_material
        )
        self.three_d_material_outline_width.editingFinished.connect(
            self._edit_three_d_material
        )

    def _restore_workspace_layout(self) -> None:
        stored = self.settings.ui_splitter_sizes
        width = max(600, self.width())
        height = max(600, self.height())
        sidebar = stored.get("sidebar_workspace", [230, width - 230])
        sidebar_width = max(220, min(
            int(sidebar[0]), max(220, width - 480)
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
        ribbon_minimum = 90
        available = max(
            ribbon_minimum + color_minimum,
            int(tools_colors[0]) + int(tools_colors[1]),
        )
        ribbon_size = max(
            ribbon_minimum,
            min(int(tools_colors[0]), available - color_minimum),
        )
        self.sidebar_splitter.setSizes(
            [ribbon_size, available - ribbon_size]
        )

        tool_canvas = stored.get("tool_canvas", [44, width - 44])
        tool_width = max(36, min(260, int(tool_canvas[0])))
        self.tool_canvas_splitter.setSizes(
            [tool_width, max(320, int(tool_canvas[1]))]
        )
        outliner = stored.get(
            "outliner_settings",
            [round(height * 0.42), round(height * 0.58)],
        )
        self.outliner_splitter.setSizes(
            [max(56, int(outliner[0])), max(120, int(outliner[1]))]
        )
        self.navigator_panel.setExpanded(
            self.settings.navigator_expanded, emit=False
        )

    def _capture_workspace_layout(self) -> None:
        self.settings.ui_splitter_sizes = {
            "sidebar_workspace": self.workspace_splitter.sizes(),
            "tools_colors": self.sidebar_splitter.sizes(),
            "tool_canvas": self.tool_canvas_splitter.sizes(),
            "outliner_settings": self.outliner_splitter.sizes(),
        }
        self.settings.navigator_expanded = self.navigator_panel.isExpanded()
        self.settings.clamp()

    def _schedule_workspace_layout_save(self, *args) -> None:
        del args
        self._capture_workspace_layout()
        self.layout_settings_timer.start(250)

    def _save_workspace_layout(self) -> None:
        self._capture_workspace_layout()
        save_settings(self.settings)

    def _connect(self) -> None:
        self.project_tabs.currentChanged.connect(self._project_tab_selected)
        self.project_tabs.tabCloseRequested.connect(self._close_project_tab)
        self.new_series_action.triggered.connect(self._create_series)
        self.open_series_action.triggered.connect(self._open_series_dialog)
        self.import_images_action.triggered.connect(self._import_images_dialog)
        self.save_action.triggered.connect(self.save)
        self.save_as_action.triggered.connect(self._save_as)
        self.new_chapter_action.triggered.connect(self._new_chapter)
        self.trim_action.triggered.connect(self._trim_height)
        self.fullscreen_action.triggered.connect(self._toggle_fullscreen)
        self.hotkeys_action.triggered.connect(self._edit_hotkeys)
        self.undo_action.triggered.connect(self._undo)
        self.redo_action.triggered.connect(self._redo)
        self.chapter_combo.currentIndexChanged.connect(self._chapter_selected)
        self.reset_view_button.clicked.connect(self.canvas.reset_view)
        self.preview.scrollRequested.connect(self.canvas.scroll_to_fraction)
        self.tablet_mode.toggled.connect(self._settings_changed)
        self.snap_grid.toggled.connect(self._settings_changed)
        self.canvas.documentChanged.connect(self._mark_dirty)
        self.ribbon.pageChanged.connect(self._ribbon_page_changed)
        self.canvas.hierarchyChanged.connect(self._hierarchy_changed)
        self.canvas.selectionChanged.connect(self._canvas_selection_changed)
        self.canvas.threeDModeChanged.connect(self._three_d_mode_changed)
        self.canvas.blenderLayerCreated.connect(self._blender_layer_created)
        self.canvas.chapterReplaced.connect(self._chapter_replaced)
        self.canvas.toolChanged.connect(self._canvas_tool_changed)
        self.canvas.interactionFinished.connect(self.selection_common.refresh)
        self.canvas.interactionFinished.connect(self.selection_settings.refresh)
        self.canvas.interactionFinished.connect(
            self.canvas.finish_blender_boundary_edit
        )
        self.canvas.interactionFinished.connect(
            self.text_object_controls.refresh
        )
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
        if hasattr(self.canvas, "importStatusMessage"):
            self.canvas.importStatusMessage.connect(
                lambda message: self.statusBar().showMessage(message, 7000)
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
        if hasattr(self.canvas, "transformModeChanged"):
            self.canvas.transformModeChanged.connect(
                lambda _mode: self._ribbon_settings_changed()
            )
        self.canvas.command_stack.changed_callback = self._command_stack_changed
        self.selection_common.changed.connect(self._hierarchy_changed)
        self.selection_settings.changed.connect(self._hierarchy_changed)
        self.selection_settings.settingsChanged.connect(
            self._ribbon_settings_changed
        )
        self.hierarchy_model.mutationCommitted.connect(self._tree_mutated)
        self.hierarchy_model.virtualVisibilityChanged.connect(
            self.three_d_controller.set_virtual_visibility
        )
        self.three_d_controller.hierarchyChanged.connect(
            self._refresh_three_d_hierarchy
        )
        self.three_d_controller.statusMessage.connect(
            lambda message: self.statusBar().showMessage(message, 7000)
        )
        self.three_d_controller.toolChanged.connect(
            lambda _tool: self._sync_three_d_tool_buttons()
        )
        self.three_d_controller.selectionChanged.connect(
            self._three_d_selection_changed
        )
        self.three_d_controller.imageChanged.connect(
            self._persist_three_d_preview
        )
        self.three_d_controller.editingAvailabilityChanged.connect(
            self._three_d_editing_availability_changed
        )
        self.three_d_sync_manager.statusMessage.connect(
            lambda message: self.statusBar().showMessage(message, 9000)
        )
        self.three_d_sync_manager.registrationChanged.connect(
            self._three_d_sync_registration_changed
        )
        self.three_d_sync_manager.accepted.connect(
            lambda _receipt: self._three_d_sync_accepted()
        )
        self.three_d_sync_manager.conflicts.connect(
            self._resolve_three_d_sync_conflicts
        )
        self.three_d_sync_manager.rejected.connect(
            self._three_d_sync_rejected
        )
        self.tree.selectionModel().selectionChanged.connect(self._tree_selection_changed)
        self.tree.customContextMenuRequested.connect(
            self._show_tree_context_menu
        )
        self.asset_library.assetActivated.connect(self._open_asset)
        self.asset_library.renameRequested.connect(self._rename_asset)
        self.asset_library.deleteRequested.connect(self._delete_asset)
        self.asset_library.folderRenameRequested.connect(self._rename_asset_folder)
        self.asset_library.folderDeleteRequested.connect(self._delete_asset_folder)
        self.asset_library.statusMessage.connect(
            lambda message: self.statusBar().showMessage(message, 5000)
        )
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
            self.tool_canvas_splitter,
            self.outliner_splitter,
        ):
            splitter.splitterMoved.connect(
                self._schedule_workspace_layout_save
            )
        self.navigator_panel.expandedChanged.connect(
            self._schedule_workspace_layout_save
        )

        self.add_page_button.clicked.connect(self._add_page)
        self.add_raster_button.clicked.connect(self._add_raster)
        self.add_vector_button.clicked.connect(self._add_vector_drawing)
        self.add_text_button.clicked.connect(self._add_text)
        self.add_fill_button.clicked.connect(self._add_fill)

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
        self.text_object_controls.settingsChanged.connect(
            self._ribbon_settings_changed
        )
        self.text_object_controls.objectChanged.connect(
            self._hierarchy_changed
        )
        self.vector_tools_controls.settingsChanged.connect(
            self._ribbon_settings_changed
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
            "undo": self._undo,
            "redo": self._redo,
            "reset_view": self.canvas.reset_view,
            "toggle_grid": self._toggle_grid,
            "select_all": self.canvas.select_all,
            "delete_selected": self._delete_selected,
            "paste_image": self._paste_image,
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
        if action_id == "paste_image" and not self._clipboard_image_sources():
            return True
        if action_id == "delete_selected":
            return (
                self._hotkey_text_input_active()
                or self.canvas.reserves_delete_key()
            )
        if action_id == "select_all":
            focus = QApplication.focusWidget()
            if isinstance(
                focus, (QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit),
            ):
                return True
            if focus is self.canvas and self.canvas.has_active_text_edit():
                return False
        if not self._hotkey_text_input_active():
            return False
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
            focus, (QAbstractSpinBox, QLineEdit, QPlainTextEdit, QTextEdit)
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
            key == int(Qt.Key_Delete)
            and (
                self._hotkey_text_input_active()
                or self.canvas.reserves_delete_key()
            )
        ):
            return False
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
        if (
            action_id is not None
            and self._hotkey_is_suppressed(action_id, chord)
        ):
            action_id = None
            prefix = False
        canvas_text_select_all = bool(
            action_id == "select_all"
            and QApplication.focusWidget() is self.canvas
            and self.canvas.has_active_text_edit()
        )
        text_conflict = (
            self._hotkey_text_input_active()
            and not canvas_text_select_all
            and (
                int(Qt.Key_Shift) in chord
                or any(
                    candidate not in MODIFIER_LABELS
                    and len(QKeySequence(candidate).toString(
                        QKeySequence.PortableText
                    )) == 1
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
        if (
            key == int(Qt.Key_Delete)
            and (
                self._hotkey_text_input_active()
                or self.canvas.reserves_delete_key()
            )
        ):
            consumed = False
        release_action = self._hotkey_action_for_chord(chord)
        if (
            release_action is not None
            and self._hotkey_is_suppressed(release_action, chord)
        ):
            consumed = False
        canvas_text_select_all = bool(
            release_action == "select_all"
            and QApplication.focusWidget() is self.canvas
            and self.canvas.has_active_text_edit()
        )
        if self._hotkey_text_input_active() and not canvas_text_select_all and (
            int(Qt.Key_Shift) in chord
            or any(
                candidate not in MODIFIER_LABELS
                and len(QKeySequence(candidate).toString(
                    QKeySequence.PortableText
                )) == 1
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
            or popup.windowType() == Qt.Popup
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

    # ---- project tabs and sessions ------------------------------------
    @staticmethod
    def _series_session_key(root: str | Path) -> str:
        return f"series:{str(Path(root).expanduser().resolve()).casefold()}"

    @staticmethod
    def _asset_session_key(context: ProjectContext, asset_id: str) -> str:
        root = str(context.repository.root).casefold()
        return f"asset:{root}:{asset_id}"

    def _tab_index_for_key(self, key: str) -> int:
        for index in range(self.project_tabs.count()):
            if self.project_tabs.tabData(index) == key:
                return index
        return -1

    def _refresh_project_tabs(self) -> None:
        for index in range(self.project_tabs.count()):
            session = self.sessions.get(str(self.project_tabs.tabData(index)))
            if session is not None:
                self.project_tabs.setTabText(index, session.tab_text)

    def _add_editor_session(self, session: EditorSession) -> None:
        existing = self._tab_index_for_key(session.key)
        if existing >= 0:
            self.project_tabs.setCurrentIndex(existing)
            return
        self.sessions[session.key] = session
        index = self.project_tabs.addTab(session.tab_text)
        self.project_tabs.setTabData(index, session.key)
        self.project_tabs.setTabToolTip(index, str(session.context.repository.root))
        self.project_tabs.setCurrentIndex(index)
        if self.active_session is None:
            self._activate_editor_session(session)

    def _capture_active_session(self) -> None:
        session = self.active_session
        if session is None or self.chapter is None:
            return
        state = self.canvas.capture_session_state()
        if state is not None:
            session.canvas_state = state
            session.chapter, session.tiles, session.images = (
                state.chapter, state.tiles, state.images
            )
        session.dirty = self._dirty
        session.last_autosave = self._last_autosave
        session.expanded_entities = self._expanded_layer_ids()
        session.manual_ribbon_page = self._manual_ribbon_page
        self._refresh_project_tabs()

    def _project_tab_selected(self, index: int) -> None:
        if self._switching_session:
            return
        if index < 0:
            self._clear_active_session()
            return
        session = self.sessions.get(str(self.project_tabs.tabData(index)))
        if session is not None and session is not self.active_session:
            self._activate_editor_session(session)

    def _activate_editor_session(self, session: EditorSession) -> None:
        if session is self.active_session:
            return
        self._switching_session = True
        try:
            self._capture_active_session()
            self._adopt_series(session.context.repository, session.context.series)
            self.active_session = session
            self.repository = session.context.repository
            self.series = session.context.series
            self.chapter = session.chapter
            self._dirty = session.dirty
            self._last_autosave = session.last_autosave
            self.canvas.asset_repository = session.context.assets
            self.asset_library.set_repository(session.context.assets)
            self.three_d_controller.set_documents(
                session.chapter,
                session.blender_sidecar if session.kind == "series" else None,
            )
            if session.kind == "series":
                self._load_three_d_previews(session)
            if session.canvas_state is None:
                self.canvas.command_stack = CommandStack()
                self.canvas.set_document(
                    session.chapter, session.tiles, session.images
                )
                initial_kind, initial_id = (
                    (session.asset_manifest.root_kind, session.asset_manifest.root_id)
                    if session.kind == "asset" and session.asset_manifest is not None
                    else ("", "")
                )
                if initial_id:
                    self.canvas.set_selection(initial_kind, initial_id)
            else:
                self.canvas.restore_session_state(session.canvas_state)
            self.canvas.command_stack.changed_callback = self._command_stack_changed
            self.chapter = self.canvas.chapter
            session.chapter, session.tiles, session.images = (
                self.chapter, self.canvas.tiles, self.canvas.images
            )
            self.hierarchy_model.set_chapter(self.chapter)
            self.hierarchy_model.set_blender_hierarchy(
                self.three_d_controller.virtual_hierarchy()
            )
            for entity_id in session.expanded_entities:
                kind = "layer" if entity_id in self.chapter.layers else "object"
                index = self.hierarchy_model.index_for_entity(kind, entity_id)
                if index.isValid():
                    self.tree.setExpanded(index, True)
            self._manual_ribbon_page = session.manual_ribbon_page
            if session.kind == "asset":
                self.chapter_combo.blockSignals(True)
                self.chapter_combo.clear()
                self.chapter_combo.addItem(f"Asset: {session.name}", "")
                self.chapter_combo.setEnabled(False)
            else:
                self.chapter_combo.setEnabled(True)
                self._sync_chapter_combo()
            self.setWindowTitle(f"{session.name} — Vertical Comic Editor")
            self.preview.invalidate_all()
            self.selection_common.refresh()
            self.selection_settings.refresh()
            self._sync_contextual_ribbon()
            self._refresh_actions()
            self._refresh_project_tabs()
            self._refresh_three_d_sync_binding()
            self.statusBar().showMessage(
                f"{session.name} — {self.chapter.width} × {self.chapter.height}px"
            )
        finally:
            self._switching_session = False

    def _clear_active_session(self) -> None:
        if self.project_tabs.count() > 0:
            return
        self.active_session = None
        self.three_d_sync_manager.stop()
        self.repository = None
        self.series = None
        self.chapter = None
        self._dirty = False
        self.canvas.asset_repository = None
        self.three_d_controller.set_documents(None, None)
        self.canvas.clear_document()
        self.hierarchy_model.set_chapter(None)
        self.asset_library.set_repository(None)
        self.chapter_combo.clear()
        self.chapter_combo.setEnabled(False)
        self.setWindowTitle("Vertical Comic Editor")
        self._refresh_actions()

    def _save_editor_session(self, session: EditorSession) -> bool:
        if session is self.active_session:
            self._capture_active_session()
        try:
            if session.kind == "series":
                session.context.repository.save_chapter(
                    session.chapter, session.tiles, session.images,
                    blender_sidecar=session.blender_sidecar,
                    protected_blender_hashes=(
                        self._protected_blender_hashes(session)
                    ),
                )
                for reference in session.context.series.chapters:
                    if reference.chapter_id == session.chapter.chapter_id:
                        reference.name = session.chapter.name
                session.context.repository.save_series(session.context.series)
            else:
                manifest = session.asset_manifest
                if manifest is None:
                    raise ValueError("Asset session has no manifest")
                manifest.document = session.chapter
                bounds = entity_visual_bounds(
                    session.chapter, session.tiles,
                    manifest.root_kind, manifest.root_id,
                )
                manifest.visual_bounds = (
                    bounds.x(), bounds.y(), bounds.width(), bounds.height()
                )
                session.chapter.width = max(
                    session.chapter.width,
                    math.ceil(bounds.right() + 64),
                )
                session.chapter.height = max(
                    session.chapter.height,
                    math.ceil(bounds.bottom() + 64),
                )
                container = session.chapter.layers[
                    session.chapter.root_page_ids[0]
                ]
                container.bound = BoundGeometry.rectangle(
                    0, 0, session.chapter.width, session.chapter.height
                )
                thumbnail = self.canvas.render_asset_thumbnail(
                    manifest, session.tiles, images=session.images
                )
                session.context.assets.save(
                    manifest, session.tiles, thumbnail, images=session.images
                )
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        session.dirty = False
        if session is self.active_session:
            self._dirty = False
            self.autosave_timer.stop()
        self.asset_library.refresh()
        self._refresh_project_tabs()
        warnings = (
            session.context.repository.last_save_warnings
            if session.kind == "series" else []
        )
        if warnings:
            self.statusBar().showMessage(
                f"Saved with warning: {warnings[0]}", 7000
            )
        else:
            self.statusBar().showMessage("Saved", 3000)
        self._refresh_actions()
        return True

    def _close_project_tab(self, index: int) -> None:
        session = self.sessions.get(str(self.project_tabs.tabData(index)))
        if session is None:
            return
        if session is self.active_session:
            self._capture_active_session()
        if session.dirty:
            answer = QMessageBox.question(
                self, "Unsaved changes",
                f"Save {session.name} before closing?",
                QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                QMessageBox.Save,
            )
            if answer == QMessageBox.Cancel:
                return
            if answer == QMessageBox.Save and not self._save_editor_session(session):
                return
        was_active = session is self.active_session
        self.sessions.pop(session.key, None)
        if was_active:
            self.active_session = None
        self.project_tabs.removeTab(index)
        if self.project_tabs.count() == 0:
            self._clear_active_session()
        elif was_active:
            current = self.project_tabs.currentIndex()
            next_session = self.sessions.get(str(self.project_tabs.tabData(current)))
            if next_session is not None:
                self._activate_editor_session(next_session)

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
        context = ProjectContext.create(repository, series)
        self._add_editor_session(EditorSession(
            key=self._series_session_key(root), kind="series",
            context=context, chapter=chapter, tiles=tiles,
        ))
        self._remember_series(root)

    def _open_series_dialog(self) -> None:
        root = QFileDialog.getExistingDirectory(self, "Open series folder")
        if root:
            self.open_series(root)

    def open_series(self, root: str | Path) -> bool:
        key = self._series_session_key(root)
        existing = self._tab_index_for_key(key)
        if existing >= 0:
            self.project_tabs.setCurrentIndex(existing)
            return True
        try:
            repository = SeriesRepository(root)
            series = repository.load_series(
                legacy_primary_color=self.settings.brush_color
            )
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to open series", str(error))
            return False
        self._remember_series(repository.root)
        if not series.chapters:
            QMessageBox.warning(self, "Unable to open series", "The series has no chapters")
            return False
        chapter_id = series.chapters[0].chapter_id
        recover = False
        if repository.has_recovery(chapter_id):
            recover = QMessageBox.question(
                self, "Recover autosave",
                "A newer autosave exists for this chapter. Recover it?",
            ) == QMessageBox.Yes
        try:
            chapter, tiles, images, blender_sidecar = repository.load_chapter(
                chapter_id, recover=recover, include_images=True,
                include_blender=True,
            )
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to open chapter", str(error))
            return False
        load_warnings = list(repository.last_load_warnings)
        if load_warnings:
            QMessageBox.warning(
                self, "Unsupported content omitted",
                "\n".join(load_warnings),
            )
        context = ProjectContext.create(repository, series)
        session = EditorSession(
            key=key, kind="series", context=context,
            chapter=chapter, tiles=tiles, images=images,
            blender_sidecar=blender_sidecar,
            dirty=recover or bool(load_warnings),
        )
        self._add_editor_session(session)
        return True

    def _open_recent_path(self, path: str) -> None:
        if (Path(path).expanduser() / "series.json").is_file():
            self.open_series(path)
            return
        QMessageBox.warning(
            self, "Recent series unavailable",
            f"The series folder no longer exists or is invalid:\n{path}",
        )
        self.settings.recent_series = [
            item for item in self.settings.recent_series or []
            if str(Path(item)).casefold() != str(Path(path)).casefold()
        ]
        save_settings(self.settings)
        self._rebuild_recent_menu()

    def _adopt_series(self, repository: SeriesRepository, series) -> None:
        self._flush_series_preferences()
        self.repository, self.series = repository, series
        self.chapter_combo.blockSignals(True)
        self.chapter_combo.clear()
        for reference in series.chapters:
            self.chapter_combo.addItem(reference.name, reference.chapter_id)
        self.chapter_combo.blockSignals(False)
        self._sync_series_color_ui()
        if self.active_session is None:
            assets = AssetRepository(repository.root)
            self.canvas.asset_repository = assets
            self.asset_library.set_repository(assets)
        self.setWindowTitle(f"{series.name} — Vertical Comic Editor")
        self._refresh_actions()

    def _new_chapter(self) -> None:
        if (
            self.repository is None or self.series is None
            or self.active_session is not None
            and self.active_session.kind != "series"
        ):
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
        if (
            self._loading_chapter or index < 0
            or self.active_session is not None
            and self.active_session.kind != "series"
        ):
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
            chapter, tiles, images, blender_sidecar = self.repository.load_chapter(
                chapter_id, recover=recover, include_images=True,
                include_blender=True,
            )
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Unable to open chapter", str(error))
            return
        load_warnings = list(self.repository.last_load_warnings)
        if load_warnings:
            QMessageBox.warning(
                self, "Unsupported content omitted",
                "\n".join(load_warnings),
            )
        self._set_chapter(chapter, tiles, images, blender_sidecar)
        if recover or load_warnings:
            self._mark_dirty(None)

    def _set_chapter(
        self, chapter, tiles, images: ImageStore | None = None,
        blender_sidecar: BlenderSidecarData | None = None,
    ) -> None:
        self.chapter = chapter
        images = images or ImageStore()
        self.canvas.set_document(chapter, tiles, images)
        self.three_d_controller.set_documents(chapter, blender_sidecar)
        if self.active_session is not None:
            self.active_session.chapter = chapter
            self.active_session.tiles = tiles
            self.active_session.images = images
            self.active_session.blender_sidecar = blender_sidecar
            self.active_session.canvas_state = None
            if self.active_session.kind == "series":
                self._load_three_d_previews(self.active_session)
        self.hierarchy_model.set_chapter(chapter)
        self.hierarchy_model.set_blender_hierarchy(
            self.three_d_controller.virtual_hierarchy()
        )
        self._dirty = False
        if self.active_session is not None:
            self.active_session.dirty = False
        self._last_autosave = 0
        self._sync_chapter_combo()
        initial_object = next(
            (
                obj for obj in chapter.objects.values()
                if isinstance(obj, (RasterObject, VectorDrawingObject, ImageObject))
            ),
            None,
        )
        if initial_object:
            self.canvas.set_selection("object", initial_object.object_id)
        self._sync_contextual_ribbon()
        self._refresh_three_d_sync_binding()
        # The first selection establishes the object context.  Keep the
        # contextual page as the initial landing page; subsequent explicit
        # Pencil/Eraser activations are routed to Tool Settings.
        if isinstance(initial_object, VectorDrawingObject):
            initial_ribbon_page = "vector_tools"
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
            layer is not None
            and self.chapter.document_kind == "asset"
            and layer.layer_id in self.chapter.root_page_ids
        ):
            return None
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

    def _create_gradient(
        self, field_type: str, gradient_type: str = "color_fill",
    ) -> None:
        parent_id = self._gradient_context_parent_id()
        if not parent_id:
            self.statusBar().showMessage(
                "Select a page, shape, or one of its child objects first",
                5000,
            )
            return
        if not self.canvas.begin_gradient_creation(
            parent_id, field_type, gradient_type=gradient_type
        ):
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

    def _edit_speed_center_shape(self) -> None:
        obj = self.gradient_tools_controls.selected_gradient()
        if (
            not isinstance(obj, SpeedLinesGradientObject)
            or self.chapter is None
        ):
            return
        existing = self.chapter.speed_center_for(obj.object_id)
        if existing is not None:
            self.canvas.set_selection("object", existing.object_id)
            self.canvas.set_tool(ToolKind.SHAPE_EDIT)
            self._sync_contextual_ribbon()
            return
        is_line = obj.field_type == "line"
        dialog = QDialog(self)
        dialog.setWindowTitle("Custom Center Shape / Line")
        layout = QVBoxLayout(dialog)
        layout.addWidget(QLabel(
            "Speed lines converge toward the closest point on this "
            "shape's boundary."
        ))
        choices = [
            ("Line", "line")
        ] if is_line else [
            ("Square", "square"),
            ("Rectangle", "rectangle"),
            ("Freeform", "freeform"),
        ]
        for label, kind in choices:
            button = QPushButton(label, dialog)
            button.clicked.connect(
                lambda _checked=False, kind=kind, dialog=dialog: (
                    self._create_speed_center(kind),
                    dialog.accept(),
                )
            )
            layout.addWidget(button)
        layout.addWidget(QLabel(
            "The center shape can be deleted but not moved."
        ))
        dialog.exec()

    def _create_speed_center(self, kind: str) -> None:
        obj = self.gradient_tools_controls.selected_gradient()
        if (
            not isinstance(obj, SpeedLinesGradientObject)
            or self.chapter is None
            or self.chapter.speed_center_for(obj.object_id) is not None
        ):
            return
        parent = self.chapter.layers[obj.parent_layer_id]
        left, top, width, height = parent.bound.bbox()
        center_x, center_y = left + width / 2, top + height / 2
        if kind == "line":
            geometry = BoundGeometry.path([
                PathNode(x=center_x - width * 0.3, y=center_y),
                PathNode(x=center_x + width * 0.3, y=center_y),
            ], closed=False)
        elif kind == "square":
            size = max(60.0, min(300.0, min(width, height) * 0.35))
            geometry = BoundGeometry.rectangle(
                center_x - size / 2, center_y - size / 2,
                size, size,
            )
        elif kind == "rectangle":
            box_width = max(80.0, width * 0.4)
            box_height = max(60.0, height * 0.25)
            geometry = BoundGeometry.rectangle(
                center_x - box_width / 2, center_y - box_height / 2,
                box_width, box_height,
            )
        else:
            size = max(60.0, min(300.0, min(width, height) * 0.3))
            geometry = BoundGeometry.path([
                PathNode(x=center_x, y=center_y - size / 2),
                PathNode(x=center_x + size / 2, y=center_y),
                PathNode(x=center_x, y=center_y + size / 2),
                PathNode(x=center_x - size / 2, y=center_y),
            ], closed=True)
        before = self.chapter.to_dict()
        center = self.chapter.add_speed_center(
            obj.object_id,
            SpeedLineCenterObject(geometry=geometry),
        )
        after = self.chapter.to_dict()
        self.canvas.push_model_change(before, after, "Add speed center")
        self.canvas.set_selection("object", center.object_id)
        self.canvas.set_tool(ToolKind.SHAPE_EDIT)
        self.canvas.documentChanged.emit(None)
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

    def _duplicate_blender_layer(self, layer_id: str) -> None:
        sidecar = self._ensure_blender_sidecar()
        if (
            self.chapter is None or sidecar is None
            or layer_id not in self.chapter.layers
        ):
            return
        before = self.chapter.to_dict()
        sidecar_before = copy.deepcopy(sidecar)
        source = self.chapter.layers[layer_id]
        duplicate = self.chapter.duplicate_blender_layer(layer_id)
        if source.comic_frame_id in sidecar.frames:
            sidecar.duplicate_frame(
                source.comic_frame_id, duplicate.comic_frame_id
            )
        else:
            if source.comic_frame_id not in sidecar.document.frame_ids:
                sidecar.document.frame_ids.append(source.comic_frame_id)
            sidecar.unavailable_frame_ids.add(source.comic_frame_id)
            sidecar.create_frame(
                frame_id=duplicate.comic_frame_id,
                warnings=[
                    "Duplicated from a layer whose comic-frame sidecar was "
                    "missing; this independent frame has no cached source state."
                ],
            )
        after = self.chapter.to_dict()
        sidecar_after = copy.deepcopy(sidecar)
        controller = self.three_d_controller

        def apply(chapter_state: dict, sidecar_state) -> None:
            self.canvas.replace_chapter(chapter_state)
            controller.replace_sidecar(sidecar_state, monotonic=True)
            self._refresh_three_d_hierarchy()

        command = CallbackCommand(
            "Duplicate Blender 3D layer",
            lambda: apply(after, sidecar_after),
            lambda: apply(before, sidecar_before),
        )
        self.canvas.command_stack.push(
            self._tag_three_d_command(
                command, sidecar_before, sidecar_after
            ),
            already_done=True,
        )
        self._after_structure(duplicate.layer_id, "layer")

    def _delete_selected(self) -> None:
        if self.chapter is None or not self.canvas.selected_id:
            return
        if self.canvas.selected_kind == "blender_entity":
            self.statusBar().showMessage(
                "Blender hierarchy rows are read-only", 4000
            )
            return
        if (
            self.active_session is not None
            and self.active_session.kind == "asset"
            and self.active_session.asset_manifest is not None
            and self.canvas.selected_id == self.active_session.asset_manifest.root_id
        ):
            self.statusBar().showMessage("The asset root cannot be deleted", 4000)
            return
        if QMessageBox.question(
            self, "Delete selection",
            "Delete the selected entity and all of its descendants?",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        before = self.chapter.to_dict()
        selected_layer = (
            self.chapter.layers.get(self.canvas.selected_id)
            if self.canvas.selected_kind == "layer" else None
        )
        if selected_layer is not None and selected_layer.layer_kind == "blender":
            sidecar = self._active_blender_sidecar()
            if sidecar is None:
                self.chapter.delete_blender_layer(selected_layer.layer_id)
                after = self.chapter.to_dict()
                self.canvas.clear_selection()
                self.canvas.push_model_change(
                    before, after, "Delete unavailable Blender 3D layer"
                )
                self._after_structure("", "")
                return
            sidecar_before = copy.deepcopy(sidecar)
            frame_id = self.chapter.delete_blender_layer(
                selected_layer.layer_id
            )
            sidecar.delete_frame(frame_id)
            after = self.chapter.to_dict()
            sidecar_after = copy.deepcopy(sidecar)
            self.canvas.clear_selection()

            def apply(chapter_state: dict, sidecar_state) -> None:
                self.canvas.replace_chapter(chapter_state)
                self.three_d_controller.replace_sidecar(
                    sidecar_state, monotonic=True
                )
                self._refresh_three_d_hierarchy()

            command = CallbackCommand(
                "Delete Blender 3D layer",
                lambda: apply(after, sidecar_after),
                lambda: apply(before, sidecar_before),
            )
            self.canvas.command_stack.push(
                self._tag_three_d_command(
                    command, sidecar_before, sidecar_after
                ),
                already_done=True,
            )
            self._after_structure("", "")
            return
        self.chapter.delete_entity(
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

    def _active_blender_sidecar(self) -> BlenderSidecarData | None:
        return (
            self.active_session.blender_sidecar
            if self.active_session is not None
            and self.active_session.kind == "series" else None
        )

    @staticmethod
    def _blender_hashes(sidecar: BlenderSidecarData | None) -> set[str]:
        if sidecar is None:
            return set()
        result: set[str] = set()
        if sidecar.cache_manifest is not None:
            result.update(sidecar.cache_manifest.referenced_hashes())
        for frame in sidecar.frames.values():
            result.update(frame.baked_variant_hashes.values())
        return result

    def _tag_three_d_command(self, command, *sidecars) -> object:
        protected: set[str] = set()
        for sidecar in sidecars:
            protected.update(self._blender_hashes(sidecar))
        command.protected_blender_hashes = protected
        return command

    def _protected_blender_hashes(self, session: EditorSession | None) -> set[str]:
        if session is None:
            return set()
        result = self._blender_hashes(session.blender_sidecar)
        stack = (
            self.canvas.command_stack if session is self.active_session
            else getattr(session.canvas_state, "command_stack", None)
        )
        if stack is None:
            return result
        for command in (*stack._undo, *stack._redo):
            result.update(getattr(command, "protected_blender_hashes", ()))
        return result

    @staticmethod
    def _three_d_preview_root(session: EditorSession) -> Path:
        return (
            session.context.repository.chapter_root(session.chapter.chapter_id)
            / "blender" / "cache" / "previews"
        )

    def _load_three_d_previews(self, session: EditorSession) -> None:
        sidecar = session.blender_sidecar
        if sidecar is None:
            return
        root = self._three_d_preview_root(session)
        for layer in session.chapter.layers.values():
            if layer.layer_kind != "blender" or not layer.comic_frame_id:
                continue
            path = root / f"{layer.comic_frame_id}.png"
            if path.is_file() and not path.is_symlink():
                image = QImage(str(path))
                if not image.isNull():
                    self.three_d_controller.set_cached_image(
                        layer.layer_id, image.convertToFormat(
                            QImage.Format.Format_RGBA8888_Premultiplied
                        )
                    )

    def _persist_three_d_preview(self, layer_id: str) -> None:
        session = self.active_session
        if (
            session is None or session.kind != "series"
            or session.blender_sidecar is None
            or layer_id not in session.chapter.layers
        ):
            return
        layer = session.chapter.layers[layer_id]
        if layer.layer_kind != "blender" or not layer.comic_frame_id:
            return
        service = getattr(self.three_d_controller, "_render_service", None)
        latest = service.latest_result() if service is not None and hasattr(
            service, "latest_result"
        ) else None
        if (
            latest is None or not getattr(latest, "available", False)
            or latest.request.chapter_id != session.chapter.chapter_id
            or latest.request.frame_id != layer.comic_frame_id
        ):
            return
        image = self.three_d_controller.image_for_layer(layer_id)
        if image is None or image.isNull():
            return
        destination = self._three_d_preview_root(session)
        try:
            destination.mkdir(parents=True, exist_ok=True)
            target = destination / f"{layer.comic_frame_id}.png"
            output = QSaveFile(str(target))
            if not output.open(QIODevice.OpenModeFlag.WriteOnly):
                raise OSError(output.errorString())
            if not image.save(output, "PNG") or not output.commit():
                raise OSError(output.errorString())
        except OSError as error:
            self.statusBar().showMessage(
                f"Could not cache the latest 3D frame preview: {error}", 7000
            )

    def _refresh_three_d_sync_binding(self) -> None:
        manager = self.three_d_sync_manager
        session = self.active_session
        if (
            session is None or session.kind != "series"
            or session.blender_sidecar is None
        ):
            manager.stop()
            return
        root = session.context.repository.chapter_root(
            session.chapter.chapter_id
        ).resolve()
        binding = manager.binding
        if (
            binding is not None
            and binding.chapter_root == root
            and binding.series_id == session.context.series.series_id
            and binding.chapter_id == session.chapter.chapter_id
        ):
            self._three_d_sync_registration_changed(
                manager.registration_payload(
                    self.three_d_controller.active_frame_id
                )
            )
            return
        try:
            manager.activate_editor_session(
                session, self._commit_three_d_sync_update,
                process_offline=True,
            )
        except (OSError, ValueError, RuntimeError) as error:
            manager.stop()
            self.statusBar().showMessage(
                f"Blender sync could not start: {error}", 9000
            )

    def _commit_three_d_sync_update(
        self, before: BlenderSidecarData, after: BlenderSidecarData,
        label: str,
    ) -> None:
        session = self.active_session
        if (
            session is None or session.kind != "series"
            or session.blender_sidecar is None
            or session.chapter.chapter_id != after.document.chapter_id
        ):
            raise RuntimeError("The synced chapter is no longer active")
        controller = self.three_d_controller

        def apply(snapshot: BlenderSidecarData, *, monotonic: bool) -> None:
            controller.replace_sidecar(snapshot, monotonic=monotonic)
            session.blender_sidecar = controller.sidecar
            self._three_d_scene_cache.clear()
            self._refresh_three_d_hierarchy()
            self._refresh_three_d_materials()
            self._three_d_sync_registration_changed(
                self.three_d_sync_manager.registration_payload(
                    controller.active_frame_id
                )
            )
            self.canvas.documentChanged.emit(QRectF())
            self.canvas.update()

        apply(after, monotonic=False)
        command = CallbackCommand(
            label,
            lambda: apply(after, monotonic=True),
            lambda: apply(before, monotonic=True),
        )
        self.canvas.command_stack.push(
            self._tag_three_d_command(command, before, after),
            already_done=True,
        )
        self._mark_dirty(None)

    def _three_d_sync_registration_changed(self, payload) -> None:
        if not isinstance(payload, dict) or not payload:
            self.three_d_connection_status.setText("Blender sync is inactive")
            self.three_d_copy_sync.setEnabled(False)
            return
        endpoint = str(payload.get("endpoint", ""))
        chapter_id = str(payload.get("chapter_id", ""))
        frames = len(payload.get("comic_frame_ids", ()))
        self.three_d_connection_status.setText(
            f"Listening: {endpoint}\nChapter: {chapter_id}\n"
            f"{frames} comic frame(s); bearer token ready"
        )
        self.three_d_copy_sync.setEnabled(True)

    def _copy_three_d_sync_registration(self) -> None:
        payload = self.three_d_sync_manager.registration_payload(
            self.three_d_controller.active_frame_id
        )
        if not payload:
            self.statusBar().showMessage("Blender sync is not active", 5000)
            return
        QApplication.clipboard().setText(json.dumps(payload, indent=2))
        self.statusBar().showMessage(
            "Copied the add-on endpoint, token, chapter, inbox, and frame IDs",
            6000,
        )

    def _three_d_sync_accepted(self) -> None:
        self._three_d_scene_cache.clear()
        self._refresh_three_d_hierarchy()
        self._sync_three_d_controls()
        if self.three_d_controller.active:
            self.three_d_controller.request_render()

    def _three_d_sync_rejected(self, receipt) -> None:
        message = (
            receipt.errors[0]
            if getattr(receipt, "errors", ()) else "Blender sync was rejected."
        )
        self.three_d_connection_status.setText(f"Last sync rejected: {message}")
        if self.isVisible():
            QMessageBox.warning(self, "Blender sync rejected", message)

    def _resolve_three_d_sync_conflicts(self, receipt) -> None:
        conflicts = tuple(getattr(receipt, "conflicts", ()))
        if not conflicts:
            return
        conflicts = tuple(
            conflict
            for category_conflicts in grouped_conflicts(conflicts).values()
            for conflict in category_conflicts
        )
        dialog = QDialog(self)
        dialog.setWindowTitle("Resolve Blender Sync Conflicts")
        dialog.resize(980, 520)
        layout = QVBoxLayout(dialog)
        explanation = QLabel(
            "Blender and Webtoon changed the same presentation fields. "
            "Keep Webtoon Override is the default; Use Blender Value removes "
            "the conflicting override.", dialog
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        table = QTableWidget(len(conflicts), 6, dialog)
        table.setHorizontalHeaderLabels((
            "Category", "Property", "Webtoon", "Blender", "Resolution",
            "Category",
        ))
        table.verticalHeader().setVisible(False)
        combos: dict[str, QComboBox] = {}
        rows_by_category: dict[str, list[int]] = {}
        for row, conflict in enumerate(conflicts):
            rows_by_category.setdefault(conflict.category, []).append(row)
            for column, value in enumerate((
                conflict.category, conflict.path,
                json.dumps(conflict.webtoon_value, ensure_ascii=False),
                json.dumps(conflict.blender_value, ensure_ascii=False),
            )):
                item = QTableWidgetItem(str(value))
                item.setFlags(item.flags() & ~Qt.ItemIsEditable)
                table.setItem(row, column, item)
            combo = QComboBox(table)
            combo.addItem(
                "Keep Webtoon Override",
                ConflictResolution.KEEP_WEBTOON_OVERRIDE.value,
            )
            combo.addItem(
                "Use Blender Value",
                ConflictResolution.USE_BLENDER_VALUE.value,
            )
            table.setCellWidget(row, 4, combo)
            combos[conflict.path] = combo
            apply_category = QPushButton("Apply to category", table)

            def apply_to_category(
                checked=False, category=conflict.category, source=combo,
            ) -> None:
                del checked
                for target_row in rows_by_category.get(category, ()):
                    target = table.cellWidget(target_row, 4)
                    if isinstance(target, QComboBox):
                        target.setCurrentIndex(source.currentIndex())

            apply_category.clicked.connect(apply_to_category)
            table.setCellWidget(row, 5, apply_category)
        table.horizontalHeader().setStretchLastSection(False)
        table.horizontalHeader().setSectionResizeMode(
            1, QHeaderView.ResizeMode.Stretch
        )
        layout.addWidget(table, 1)
        buttons = QDialogButtonBox(
            QDialogButtonBox.Apply | QDialogButtonBox.Cancel,
            parent=dialog,
        )
        buttons.button(QDialogButtonBox.Apply).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if dialog.exec() != QDialog.Accepted:
            self.statusBar().showMessage(
                "Blender sync remains paused with unresolved conflicts", 8000
            )
            return
        choices = {
            path: combo.currentData() for path, combo in combos.items()
        }
        resolver = getattr(
            self.three_d_sync_manager, "resolve_conflicts", None
        )
        if not callable(resolver):
            QMessageBox.warning(
                self, "Conflict resolution unavailable",
                "The validated bundle remains queued; retry after restarting "
                "Webtoon Maker.",
            )
            return
        result = resolver(receipt, choices)
        if getattr(result, "errors", ()):
            QMessageBox.warning(
                self, "Blender sync not accepted", result.errors[0]
            )

    def _three_d_scene_for_frame(self, layer_id: str, frame, sidecar):
        """Materialize a neutral render scene from the active hashed GLB."""
        del layer_id
        session = self.active_session
        if session is None or session.kind != "series" or sidecar is None:
            return None
        cache = sidecar.cache_manifest
        digest = str(cache.base_glb_hash or "") if cache is not None else ""

        def load_cached_scene(resource_digest: str):
            blob = (
                session.context.repository.chapter_root(frame.chapter_id)
                / "blender" / "cache" / "blobs"
                / f"{resource_digest}.glb"
            )
            if not blob.is_file():
                raise FileNotFoundError(
                    f"3D cache blob {resource_digest[:12]} is unavailable"
                )
            base = self._three_d_scene_cache.get(resource_digest)
            if base is None:
                from comic_editor.three_d.renderer.gltf import load_gltf
                base = load_gltf(blob).scene
                self._three_d_scene_cache[resource_digest] = base
            return copy.deepcopy(base)

        if digest:
            scene = load_cached_scene(digest)
        else:
            from comic_editor.three_d.renderer.scene import SceneData
            scene = SceneData(scene_id=frame.frame_id)

        if cache is not None and digest:
            from comic_editor.three_d.renderer.resources import (
                replace_object_resource,
            )
            resources = dict(cache.object_resources)
            resources.update({
                str(object_id): str(resource_hash)
                for object_id, resource_hash
                in frame.baked_variant_hashes.items()
                if str(object_id) != "scene"
            })
            for object_id, resource_hash in sorted(resources.items()):
                resource_scene = load_cached_scene(resource_hash)
                scene = replace_object_resource(
                    scene, resource_scene, object_id
                )

        import numpy as np
        from comic_editor.three_d.renderer.camera import (
            quaternion_from_axis_angle, quaternion_multiply,
        )
        from comic_editor.three_d.renderer.materials import (
            DrawingMaterial, SurfaceMaterial, ToonRamp, ToonRampStop,
        )
        from comic_editor.three_d.renderer.mesh import SourceMaterial
        from comic_editor.three_d.renderer.primitives import (
            cube_mesh, cylinder_mesh,
        )
        from comic_editor.three_d.renderer.projection import (
            FisheyeMapping, ProjectionMode,
        )
        from comic_editor.three_d.renderer.scene import (
            LightType, SceneLight, SceneNode,
        )

        source_transforms = frame.source_state.get("transforms", {})
        override_transforms = frame.presentation_overrides.get(
            "transforms", {}
        )
        source_visibility = frame.source_state.get("visibility", {})
        override_visibility = frame.presentation_overrides.get(
            "visibility", {}
        )
        for node_id, node in scene.nodes.items():
            transform = override_transforms.get(
                node_id, source_transforms.get(node_id, {})
            )
            matrix_local = (
                transform.get("matrix_local")
                if isinstance(transform, dict) else None
            )
            if isinstance(matrix_local, (list, tuple)) and len(matrix_local) == 16:
                node.local_matrix = np.asarray(
                    matrix_local, dtype=np.float64
                ).reshape((4, 4), order="F")
            visible = source_visibility.get(node_id, True)
            if isinstance(visible, dict):
                visible = visible.get("visible", not visible.get("hide_render", False))
            override = override_visibility.get(node_id, visible)
            if isinstance(override, dict):
                override = override.get("visible", not override.get("hide_render", False))
            node.visible = bool(override)
            catalog = sidecar.document.object_catalog.get(node_id, {})
            collection_ids = catalog.get("collection_ids", ())
            if collection_ids:
                node.visible = node.visible and any(
                    frame.collection_visible(str(collection_id))
                    for collection_id in collection_ids
                )
        scene.recompute_world_matrices()
        from comic_editor.three_d.frame_scene import (
            apply_pose_and_shape_state,
        )
        apply_pose_and_shape_state(scene, frame)

        source_lights = frame.source_state.get("lights", {})
        override_lights = frame.presentation_overrides.get("lights", {})
        for node_id, node in scene.nodes.items():
            if node.light_index is None or not 0 <= node.light_index < len(scene.lights):
                continue
            base_light = (
                source_lights.get(node_id, {})
                if isinstance(source_lights, dict) else {}
            )
            changed_light = (
                override_lights.get(node_id, {})
                if isinstance(override_lights, dict) else {}
            )
            values = {
                **(base_light if isinstance(base_light, dict) else {}),
                **(changed_light if isinstance(changed_light, dict) else {}),
            }
            light = scene.lights[node.light_index]
            raw_type = str(values.get("type", light.light_type.value)).lower()
            mapped_type = {
                "area": "rectangle", "rect": "rectangle",
            }.get(raw_type, raw_type)
            light.light_type = LightType(mapped_type) if mapped_type in {
                "sun", "point", "rectangle", "spot"
            } else light.light_type
            raw_color = values.get("color")
            if isinstance(raw_color, (list, tuple)) and len(raw_color) >= 3:
                light.color = tuple(float(item) for item in raw_color[:3])
            light.energy = max(0.0, float(values.get("energy", light.energy)))
            raw_range = values.get("range")
            if raw_range is None:
                raw_range = (
                    values.get("cutoff_distance", light.range)
                    if bool(values.get("use_custom_distance", False))
                    else 0.0
                )
            light.range = max(0.0, float(raw_range))
            area_width = max(0.001, float(values.get(
                "size", light.area_size[0]
            )))
            area_height = max(0.001, float(values.get(
                "size_y", area_width
            )))
            if str(values.get("shape", "")).upper() in {"SQUARE", "DISK"}:
                area_height = area_width
            light.area_size = (area_width, area_height)
            outer = max(1.0e-6, min(
                math.pi - 1.0e-6,
                float(values.get("spot_size", light.spot_outer_angle)),
            ))
            blend = max(0.0, min(1.0, float(values.get("spot_blend", 0.0))))
            light.spot_outer_angle = outer
            light.spot_inner_angle = outer * (1.0 - blend)
            light.casts_shadow = bool(values.get(
                "casts_shadow", values.get("use_shadow", light.casts_shadow)
            ))
            light.visible = node.visible
            light.raw_source = copy.deepcopy(values)

        settings = frame.presentation_overrides.get("renderer_settings", {})
        projection = str(settings.get("projection", "perspective"))
        if projection == "orthographic":
            scene.projection.mode = ProjectionMode.ORTHOGRAPHIC
        elif projection.startswith("fisheye_"):
            scene.projection.mode = ProjectionMode.FISHEYE
            scene.projection.fisheye_mapping = {
                "fisheye_equidistant": FisheyeMapping.EQUIDISTANT,
                "fisheye_equisolid": FisheyeMapping.EQUISOLID_ANGLE,
                "fisheye_stereographic": FisheyeMapping.STEREOGRAPHIC,
                "fisheye_orthographic": FisheyeMapping.ORTHOGRAPHIC,
            }.get(projection, FisheyeMapping.EQUIDISTANT)
        else:
            scene.projection.mode = ProjectionMode.PERSPECTIVE
        scene.projection.vertical_fov_deg = float(settings.get("fov", 50.0))
        scene.projection.ortho_height = max(
            0.001, float(settings.get("ortho_height", 10.0))
        )
        overlays_visible = bool(settings.get("overlays_visible", True))
        scene.overlays.grid_visible = overlays_visible and bool(
            settings.get("grid_visible", True)
        )
        scene.overlays.volume_grid_visible = overlays_visible and bool(
            settings.get("volume_grid_visible", False)
        )
        scene.overlays.axes_visible = overlays_visible and bool(
            settings.get("axes_visible", True)
        )
        scene.overlays.floor_visible = bool(settings.get("floor_visible", True))
        scene.shadows.enabled = bool(settings.get("shadows_enabled", True))
        scene.shadows.resolution = {
            "low": 512, "medium": 1024, "high": 2048,
        }.get(str(settings.get("shadow_quality", "medium")), 1024)

        from comic_editor.three_d.frame_scene import (
            apply_blender_camera_state,
        )
        has_blender_camera = apply_blender_camera_state(
            scene, frame, settings
        )
        navigation = frame.presentation_overrides.get(
            "camera_navigation", {}
        )
        if navigation or not has_blender_camera:
            target = navigation.get("target", [0.0, 0.0, 0.0])
            pan = navigation.get("pan", [0.0, 0.0])
            scene.active_camera.target = np.asarray([
                float(target[0]),
                float(target[1]),
                float(target[2]),
            ], dtype=np.float64)
            scene.active_camera.distance = max(
                0.01, float(navigation.get("distance", 8.0))
            )
            exact_orientation = navigation.get("orientation")
            if (
                isinstance(exact_orientation, (list, tuple))
                and len(exact_orientation) == 4
            ):
                candidate = np.asarray(exact_orientation, dtype=np.float64)
                length = float(np.linalg.norm(candidate))
                if np.all(np.isfinite(candidate)) and length > 1.0e-12:
                    scene.active_camera.orientation = candidate / length
                else:
                    exact_orientation = None
            if exact_orientation is None:
                yaw = quaternion_from_axis_angle(
                    np.array([0.0, 1.0, 0.0]),
                    math.radians(float(navigation.get("yaw", 35.0))),
                )
                pitch = quaternion_from_axis_angle(
                    np.array([1.0, 0.0, 0.0]),
                    math.radians(float(navigation.get("pitch", 20.0))),
                )
                scene.active_camera.orientation = quaternion_multiply(
                    yaw, pitch
                )
            # Pan is stored as a camera-plane offset, so rolled and pitched
            # source cameras continue to pan horizontally/vertically on screen.
            scene.active_camera.target += (
                scene.active_camera.right * float(pan[0])
                + scene.active_camera.up * float(pan[1])
            )

        def color_tuple(value: str) -> tuple[float, float, float, float]:
            color = QColor(value)
            return color.redF(), color.greenF(), color.blueF(), color.alphaF()

        scene.drawing_materials = {}
        for material in sidecar.document.drawing_materials:
            ramp = tuple(
                ToonRampStop(position, color_tuple(color)[:3])
                for position, color in material.toon_ramp
            )
            scene.drawing_materials[material.material_id] = DrawingMaterial(
                material_id=material.material_id, name=material.name,
                surface=SurfaceMaterial(material.shader.title()),
                base_color=color_tuple(material.tint),
                use_base_color_texture=material.use_texture,
                use_vertex_color=material.use_vertex_color,
                toon_ramp=ToonRamp(ramp),
                outline_enabled=material.outline_enabled,
                outline_color=color_tuple(material.outline_color),
                outline_thickness_px=material.outline_width,
            )
        scene.material_mappings = dict(sidecar.document.material_mappings)

        def local_matrix(record: dict) -> object:
            transform = record.get("transform", {})
            raw = transform.get("matrix") if isinstance(transform, dict) else None
            if isinstance(raw, (list, tuple)) and len(raw) == 16:
                return np.asarray(raw, dtype=np.float64).reshape((4, 4), order="F")
            value = np.identity(4, dtype=np.float64)
            translation = transform.get("translation", (0.0, 0.0, 0.0))
            rotation = transform.get("rotation", (0.0, 0.0, 0.0))
            scale = transform.get("scale", (1.0, 1.0, 1.0))
            value[:3, 3] = np.asarray(translation[:3], dtype=np.float64)
            rx, ry, rz = (
                math.radians(float(item)) for item in rotation[:3]
            )
            cx, sx = math.cos(rx), math.sin(rx)
            cy, sy = math.cos(ry), math.sin(ry)
            cz, sz = math.cos(rz), math.sin(rz)
            rotate_x = np.array([
                [1.0, 0.0, 0.0], [0.0, cx, -sx], [0.0, sx, cx],
            ])
            rotate_y = np.array([
                [cy, 0.0, sy], [0.0, 1.0, 0.0], [-sy, 0.0, cy],
            ])
            rotate_z = np.array([
                [cz, -sz, 0.0], [sz, cz, 0.0], [0.0, 0.0, 1.0],
            ])
            value[:3, :3] = (
                rotate_z @ rotate_y @ rotate_x
                @ np.diag(np.asarray(scale[:3], dtype=np.float64))
            )
            return value

        for record in frame.local_entities:
            if not isinstance(record, dict):
                continue
            entity_id = str(record.get("id", ""))
            kind = str(record.get("type", ""))
            parameters = record.get("parameters", {})
            matrix = local_matrix(record)
            if kind in {"cube", "cylinder"}:
                mesh = (
                    cube_mesh(
                        entity_id + ":mesh",
                        tuple(parameters.get("size", (1.0, 1.0, 1.0))),
                    )
                    if kind == "cube" else cylinder_mesh(
                        entity_id + ":mesh",
                        float(parameters.get("radius", 0.5)),
                        float(parameters.get("depth", 1.0)),
                        int(parameters.get("vertices", 32)),
                    )
                )
                # Local primitives get an explicit app-owned source material;
                # they must never inherit Blender material slot zero merely
                # because their generated primitive defaults to index zero.
                from dataclasses import replace

                local_material_id = f"webtoon:local-material:{entity_id}"
                material_index = len(scene.source_materials)
                scene.source_materials = (*scene.source_materials, SourceMaterial(
                    local_material_id,
                    str(record.get("name", kind.title())) + " Material",
                ))
                mesh = replace(mesh, primitives=tuple(
                    replace(primitive, material_index=material_index)
                    for primitive in mesh.primitives
                ))
                drawing_material_id = str(
                    parameters.get("drawing_material_id", "")
                )
                if drawing_material_id in scene.drawing_materials:
                    scene.material_mappings[local_material_id] = (
                        drawing_material_id
                    )
                mesh_index = len(scene.meshes)
                scene.meshes = (*scene.meshes, mesh)
                scene.nodes[entity_id] = SceneNode(
                    entity_id, str(record.get("name", kind.title())),
                    local_matrix=matrix, mesh_index=mesh_index,
                    visible=bool(record.get("visible", True)),
                )
                scene.root_node_ids = (*scene.root_node_ids, entity_id)
            elif kind in {"sun", "point", "rectangle", "spot"}:
                light_index = len(scene.lights)
                color = color_tuple(str(parameters.get("color", "#FFFFFFFF")))
                size = parameters.get("size", (1.0, 1.0))
                if not isinstance(size, (list, tuple)):
                    size = (float(size), float(size))
                scene.lights = (*scene.lights, SceneLight(
                    light_id=entity_id,
                    name=str(record.get("name", kind.title())),
                    light_type=LightType(kind), color=color[:3],
                    energy=max(0.0, float(parameters.get("energy", 1.0))),
                    range=max(0.0, float(parameters.get("range", 0.0))),
                    area_size=(float(size[0]), float(size[1])),
                    spot_outer_angle=math.radians(float(
                        parameters.get("spot_size", 45.0)
                    )),
                    casts_shadow=bool(parameters.get("casts_shadow", True)),
                    visible=bool(record.get("visible", True)),
                    raw_source=copy.deepcopy(parameters),
                ))
                scene.nodes[entity_id] = SceneNode(
                    entity_id, str(record.get("name", kind.title())),
                    local_matrix=matrix, light_index=light_index,
                    visible=bool(record.get("visible", True)),
                )
                scene.root_node_ids = (*scene.root_node_ids, entity_id)
        requested_lights = settings.get("active_light_ids")
        if isinstance(requested_lights, (list, tuple)):
            requested = {str(item) for item in requested_lights}
            for node in scene.nodes.values():
                if node.light_index is not None:
                    scene.lights[node.light_index].visible = (
                        scene.lights[node.light_index].visible
                        and node.node_id in requested
                    )
        enabled_lights = [
            (node, scene.lights[node.light_index])
            for node in scene.nodes.values()
            if node.visible and node.light_index is not None
            and scene.lights[node.light_index].visible
        ]
        warnings = list(scene.warnings)
        if len(enabled_lights) > 8:
            warnings.append(
                f"{len(enabled_lights)} lights are enabled; only the first 8 render."
            )
        shadow_count = sum(
            1 for _node, light in enabled_lights[:8] if light.casts_shadow
        )
        if shadow_count > 4:
            warnings.append(
                f"{shadow_count} shadow-casting lights are enabled; only the first 4 cast shadows."
            )
        scene.warnings = tuple(dict.fromkeys(warnings))
        scene.validate()
        scene.recompute_world_matrices()
        return scene

    def _ensure_blender_sidecar(self) -> BlenderSidecarData | None:
        if (
            self.chapter is None or self.active_session is None
            or self.active_session.kind != "series"
        ):
            return None
        sidecar = self.active_session.blender_sidecar
        if sidecar is None:
            sidecar = BlenderSidecarData(BlenderChapterDocument(
                chapter_id=self.chapter.chapter_id,
                series_id=(self.series.series_id if self.series else ""),
            ))
            self.active_session.blender_sidecar = sidecar
        self.three_d_controller.set_documents(self.chapter, sidecar)
        self._refresh_three_d_sync_binding()
        return sidecar

    def _begin_blender_layer_creation(self, kind: str) -> None:
        sidecar = self._ensure_blender_sidecar()
        if sidecar is None:
            self.statusBar().showMessage(
                "3D layers are available only in comic chapters", 5000
            )
            return
        document = sidecar.document
        if not document.file_uuid and not document.blend_path_hint:
            path, _filter = QFileDialog.getOpenFileName(
                self, "Link this chapter to a Blender file", "",
                "Blender files (*.blend)",
            )
            if not path:
                return
            document.blend_path_hint = str(Path(path).resolve())
            document.revision += 1
            self._mark_dirty(None)
        if not self.canvas.begin_blender_layer_creation(kind):
            self.statusBar().showMessage(
                "Select a page or container before drawing a 3D layer", 5000
            )

    def _replace_blender_association(self) -> None:
        sidecar = self._active_blender_sidecar()
        if sidecar is None:
            return
        path, _filter = QFileDialog.getOpenFileName(
            self, "Choose replacement Blender file", "",
            "Blender files (*.blend)",
        )
        if not path:
            return
        path = str(Path(path).resolve())
        document = sidecar.document
        if path == document.blend_path_hint:
            return
        if document.file_uuid or document.blend_path_hint:
            message = (
                "Replace this chapter's Blender association? The next add-on "
                "sync must establish the new file UUID. Existing frame "
                "presentation overrides are retained."
            )
            if self._dirty:
                message += (
                    "\n\nThis chapter also has unsaved changes; they will not "
                    "be discarded."
                )
            if QMessageBox.question(
                self, "Replace Blender Association", message,
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
            ) != QMessageBox.Yes:
                return
        before = copy.deepcopy(sidecar)
        document.file_uuid = ""
        document.blend_path_hint = path
        document.source_revision = 0
        document.revision += 1
        document.warnings.append(
            "Blender association is pending validation by the next sync."
        )
        self._push_sidecar_change(before, "Replace Blender association")
        self._sync_three_d_controls()

    def _blender_layer_created(self, layer_id: str, frame_id: str) -> None:
        sidecar = self._ensure_blender_sidecar()
        if sidecar is None or not frame_id:
            return
        if frame_id not in sidecar.frames:
            sidecar.create_frame(frame_id=frame_id)
        self.three_d_controller.set_documents(self.chapter, sidecar)
        self._refresh_three_d_hierarchy()
        self._refresh_three_d_sync_binding()

    def _activate_three_d_tool(self, tool: ThreeDToolKind) -> bool:
        if (
            not self.canvas.in_three_d_mode
            or not self.three_d_controller.editing_available
        ):
            return False
        self.three_d_controller.set_tool(tool)
        self._sync_three_d_tool_buttons()
        self._select_ribbon_page(
            "three_d_tool_settings"
            if tool in {ThreeDToolKind.SELECT_RECT, ThreeDToolKind.SELECT_LASSO}
            else "three_d_object"
        )
        return True

    def _select_three_d_light_type(self, light_type: str) -> None:
        if not self.canvas.in_three_d_mode:
            return
        self.three_d_controller.set_pending_light_type(light_type)
        button = self.three_d_tool_buttons[ThreeDToolKind.ADD_LIGHT]
        button.setToolTip(f"Add {light_type.title()} Light")
        self._sync_three_d_tool_buttons()
        self._select_ribbon_page("three_d_object")

    def _sync_three_d_tool_buttons(self) -> None:
        active = self.canvas.in_three_d_mode
        editable = active and self.three_d_controller.editing_available
        for tool, button in self.three_d_tool_buttons.items():
            button.setVisible(active)
            button.setEnabled(editable)
            button.blockSignals(True)
            button.setChecked(active and tool == self.three_d_controller.tool)
            button.blockSignals(False)

    def _three_d_editing_availability_changed(
        self, available: bool, reason: str,
    ) -> None:
        self._sync_three_d_tool_buttons()
        if self.canvas.in_three_d_mode:
            self._sync_three_d_controls()
            if not available and reason:
                self.statusBar().showMessage(
                    f"3D editing unavailable: {reason}", 9000
                )

    def _three_d_mode_changed(self, active: bool, layer_id: str) -> None:
        del layer_id
        self.tree.setSelectionMode(
            QTreeView.ExtendedSelection
            if active and self.three_d_controller.multi_select
            else QTreeView.SingleSelection
        )
        for button in self.tool_buttons.values():
            button.setVisible(not active)
        for button in self.shape_tool_buttons.values():
            button.setVisible(not active)
        for button in self.drawing_selection_buttons.values():
            button.setVisible(not active)
        for widget in (
            self.shapes_category, self.drawing_selection_category,
            self.add_page_button, self.add_fill_button, self.add_text_button,
            self.add_raster_button, self.add_vector_button,
        ):
            widget.setVisible(not active)
        self.color_tabs.setVisible(not active)
        self._sync_three_d_tool_buttons()
        three_d_pages = (
            "three_d_view", "three_d_rendering", "three_d_outline",
            "three_d_materials", "three_d_object",
            "three_d_tool_settings",
        )
        for key in three_d_pages:
            self.ribbon.set_page_visible(key, active)
        for key in ("tool_settings", "asset_library", "vector_tools", "gradient_tools"):
            self.ribbon.set_page_visible(key, not active and (
                key in {"tool_settings", "asset_library"}
            ))
        if active:
            self._sync_three_d_controls()
            self._select_ribbon_page("three_d_view")
        else:
            self._sync_contextual_ribbon()
            self._select_ribbon_page("tool_settings")
            self._sync_tool_buttons()
        self._refresh_actions()

    def _set_three_d_renderer_setting(self, key: str, value) -> None:
        if (
            getattr(self, "_syncing_three_d_controls", False)
            or not self.canvas.in_three_d_mode
        ):
            return
        self.three_d_controller.set_renderer_setting(key, value)

    def _three_d_multi_select_changed(self, enabled: bool) -> None:
        if getattr(self, "_syncing_three_d_controls", False):
            return
        self.three_d_controller.set_multi_select(enabled)
        self.tree.setSelectionMode(
            QTreeView.ExtendedSelection if enabled
            else QTreeView.SingleSelection
        )

    def _sync_three_d_controls(self) -> None:
        self._syncing_three_d_controls = True
        controls = (
            self.three_d_grid, self.three_d_volume_grid,
            self.three_d_axes, self.three_d_floor,
            self.three_d_overlays, self.three_d_shadows,
            self.three_d_antialiasing, self.three_d_projection,
            self.three_d_fov, self.three_d_ortho_height,
            self.three_d_shadow_quality, self.three_d_fidelity,
            self.three_d_multi_select, self.three_d_transform_space,
            self.three_d_gizmo_mode, *self.three_d_transform_fields,
            *self.three_d_entity_property_controls,
        )
        for control in controls:
            control.blockSignals(True)
        try:
            editing_available = self.three_d_controller.editing_available
            for page in (
                self.three_d_rendering_page, self.three_d_materials_page,
                self.three_d_object_page, self.three_d_tool_settings_page,
            ):
                page.setEnabled(editing_available)
            for control in (
                self.three_d_grid, self.three_d_volume_grid,
                self.three_d_axes, self.three_d_floor,
                self.three_d_overlays,
            ):
                control.setEnabled(editing_available)
            for control, key, default in (
                (self.three_d_grid, "grid_visible", True),
                (self.three_d_volume_grid, "volume_grid_visible", False),
                (self.three_d_axes, "axes_visible", True),
                (self.three_d_floor, "floor_visible", True),
                (self.three_d_overlays, "overlays_visible", True),
                (self.three_d_shadows, "shadows_enabled", True),
                (self.three_d_antialiasing, "antialiasing", False),
            ):
                control.setChecked(bool(
                    self.three_d_controller.renderer_setting(key, default)
                ))
            for combo, key, default in (
                (self.three_d_projection, "projection", "perspective"),
                (self.three_d_shadow_quality, "shadow_quality", "medium"),
                (self.three_d_fidelity, "quality", "full"),
            ):
                index = combo.findData(
                    self.three_d_controller.renderer_setting(key, default)
                )
                combo.setCurrentIndex(max(0, index))
            self.three_d_fov.setValue(float(
                self.three_d_controller.renderer_setting("fov", 50.0)
            ))
            self.three_d_ortho_height.setValue(float(
                self.three_d_controller.renderer_setting(
                    "ortho_height", 10.0
                )
            ))
            self.three_d_multi_select.setChecked(
                self.three_d_controller.multi_select
            )
            frame = self.three_d_controller._frame()
            tool_settings = (
                frame.presentation_overrides.get("tool_settings", {})
                if frame is not None else {}
            )
            self.three_d_transform_space.setCurrentIndex(max(
                0, self.three_d_transform_space.findData(
                    tool_settings.get("transform_space", "global")
                )
            ))
            self.three_d_gizmo_mode.setCurrentIndex(max(
                0, self.three_d_gizmo_mode.findData(
                    tool_settings.get("gizmo_mode", "move")
                )
            ))
            components = self.three_d_controller.selected_transform_components()
            editable = editing_available and components is not None
            for index, field in enumerate(self.three_d_transform_fields):
                field.setEnabled(editable)
                if editable:
                    field.setValue(float(components[index]))
            descriptor = self.three_d_controller.selected_entity_properties()
            kind = str(descriptor.get("kind", "")) if descriptor else ""
            properties = (
                descriptor.get("properties", {}) if descriptor else {}
            )
            visible_fields = {
                "cube": {"size_x", "size_y", "size_z"},
                "cylinder": {"radius", "depth", "segments"},
                "light": {
                    "light_type", "color", "energy", "range",
                    "area_width", "area_height", "spot_angle",
                    "casts_shadow",
                },
                "camera": {
                    "camera_type", "fov", "ortho_scale",
                    "clip_start", "clip_end",
                },
            }.get(kind, set())
            self.three_d_entity_properties_widget.setVisible(bool(
                visible_fields
            ))
            self.three_d_entity_kind.setText(
                f"{descriptor.get('name', '')} ({kind.title()})"
                if descriptor else "No editable parameters"
            )
            for name, (label, control) in (
                self.three_d_entity_property_rows.items()
            ):
                shown = name in visible_fields
                if label is not None:
                    label.setVisible(shown)
                control.setVisible(shown)
                control.setEnabled(shown and editing_available)
            if descriptor:
                for name, control in (
                    ("size_x", self.three_d_cube_size_x),
                    ("size_y", self.three_d_cube_size_y),
                    ("size_z", self.three_d_cube_size_z),
                    ("radius", self.three_d_cylinder_radius),
                    ("depth", self.three_d_cylinder_depth),
                    ("energy", self.three_d_light_energy),
                    ("range", self.three_d_light_range),
                    ("area_width", self.three_d_light_area_width),
                    ("area_height", self.three_d_light_area_height),
                    ("spot_angle", self.three_d_light_spot_angle),
                    ("fov", self.three_d_camera_fov),
                    ("ortho_scale", self.three_d_camera_ortho_scale),
                    ("clip_start", self.three_d_camera_clip_start),
                    ("clip_end", self.three_d_camera_clip_end),
                ):
                    if name in properties:
                        control.setValue(float(properties[name]))
                if "segments" in properties:
                    self.three_d_cylinder_segments.setValue(
                        int(properties["segments"])
                    )
                if "color" in properties:
                    self.three_d_light_color.setText(str(properties["color"]))
                if "casts_shadow" in properties:
                    self.three_d_light_shadow.setChecked(
                        bool(properties["casts_shadow"])
                    )
                for name, combo in (
                    ("light_type", self.three_d_light_type),
                    ("camera_type", self.three_d_camera_type),
                ):
                    if name in properties:
                        combo.setCurrentIndex(max(
                            0, combo.findData(properties[name])
                        ))
            self.three_d_reset_object.setEnabled(
                editing_available
                and len(self.three_d_controller.selected_entity_ids) == 1
            )
        finally:
            for control in controls:
                control.blockSignals(False)
            self._syncing_three_d_controls = False
        sidecar = self._active_blender_sidecar()
        if sidecar is None:
            self.three_d_source_status.setText("No Blender file linked")
        else:
            document = sidecar.document
            self.three_d_source_status.setText(
                f"File UUID: {document.file_uuid or 'pending first sync'}\n"
                f"{document.blend_path_hint or 'No local path hint'}"
            )
        self._refresh_three_d_materials()
        if hasattr(self, "three_d_sync_manager"):
            self._three_d_sync_registration_changed(
                self.three_d_sync_manager.registration_payload(
                    self.three_d_controller.active_frame_id
                )
            )

    def _refresh_three_d_hierarchy(self) -> None:
        if not hasattr(self, "hierarchy_model"):
            return
        self.hierarchy_model.set_blender_hierarchy(
            self.three_d_controller.virtual_hierarchy()
        )

    def _three_d_metadata_text(
        self, source_id: str, type_label: str,
    ) -> str:
        metadata = self.three_d_controller.selected_entity_metadata(source_id)
        if not metadata:
            return f"Source ID: {source_id}\nType: {type_label}"
        serialized = json.dumps(
            metadata, indent=2, sort_keys=True, ensure_ascii=False,
            default=str,
        )
        if len(serialized) > 6000:
            serialized = serialized[:5997] + "..."
        return (
            f"Source ID: {source_id}\nType: {type_label}\n\n{serialized}"
        )

    def _three_d_selection_changed(self, entity_ids: set[str]) -> None:
        if not self.canvas.in_three_d_mode:
            return
        self.tree.setSelectionMode(
            QTreeView.ExtendedSelection
            if self.three_d_controller.multi_select
            else QTreeView.SingleSelection
        )
        if not entity_ids:
            self.three_d_object_metadata.setText(
                "Select an object in the Blender subtree."
            )
            self._sync_three_d_controls()
            return
        first = sorted(entity_ids)[0]
        index = self.hierarchy_model.index_for_blender_source(
            self.three_d_controller.active_layer_id, first
        )
        if not index.isValid():
            return
        item = self.hierarchy_model.item_for_index(index)
        blocker = QSignalBlocker(self.tree.selectionModel())
        self.tree.selectionModel().select(
            index,
            QItemSelectionModel.ClearAndSelect | QItemSelectionModel.Rows,
        )
        self.tree.setCurrentIndex(index)
        self.tree.scrollTo(index)
        del blocker
        self.canvas.selected_kind = "blender_entity"
        self.canvas.selected_id = item.entity_id
        self.canvas.selectionChanged.emit("blender_entity", item.entity_id)
        self.three_d_object_metadata.setText(
            self._three_d_metadata_text(item.source_id, item.type_label)
        )
        self._sync_three_d_controls()

    def _edit_three_d_transform(self) -> None:
        if (
            getattr(self, "_syncing_three_d_controls", False)
            or not self.canvas.in_three_d_mode
        ):
            return
        values = tuple(field.value() for field in self.three_d_transform_fields)
        if self.three_d_controller.set_selected_transform_components(values):
            self._sync_three_d_controls()

    def _edit_three_d_entity_properties(self, *args) -> None:
        del args
        if (
            getattr(self, "_syncing_three_d_controls", False)
            or not self.canvas.in_three_d_mode
        ):
            return
        descriptor = self.three_d_controller.selected_entity_properties()
        if descriptor is None:
            return
        values = {
            "size_x": self.three_d_cube_size_x.value(),
            "size_y": self.three_d_cube_size_y.value(),
            "size_z": self.three_d_cube_size_z.value(),
            "radius": self.three_d_cylinder_radius.value(),
            "depth": self.three_d_cylinder_depth.value(),
            "segments": self.three_d_cylinder_segments.value(),
            "light_type": self.three_d_light_type.currentData(),
            "color": self.three_d_light_color.text(),
            "energy": self.three_d_light_energy.value(),
            "range": self.three_d_light_range.value(),
            "area_width": self.three_d_light_area_width.value(),
            "area_height": self.three_d_light_area_height.value(),
            "spot_angle": self.three_d_light_spot_angle.value(),
            "casts_shadow": self.three_d_light_shadow.isChecked(),
            "camera_type": self.three_d_camera_type.currentData(),
            "fov": self.three_d_camera_fov.value(),
            "ortho_scale": self.three_d_camera_ortho_scale.value(),
            "clip_start": self.three_d_camera_clip_start.value(),
            "clip_end": self.three_d_camera_clip_end.value(),
        }
        try:
            changed = self.three_d_controller.set_selected_entity_properties(
                values
            )
        except (TypeError, ValueError) as exc:
            self.statusBar().showMessage(str(exc), 7000)
            changed = False
        if changed:
            self._sync_three_d_controls()

    def _push_sidecar_change(
        self, before: BlenderSidecarData, label: str,
    ) -> None:
        sidecar = self._active_blender_sidecar()
        if sidecar is None:
            return
        after = copy.deepcopy(sidecar)
        controller = self.three_d_controller

        def apply(snapshot) -> None:
            controller.replace_sidecar(snapshot, monotonic=True)
            self._refresh_three_d_hierarchy()
            self._refresh_three_d_materials()
            self.canvas.documentChanged.emit(QRectF())
            self.canvas.update()

        command = CallbackCommand(
            label, lambda: apply(after), lambda: apply(before)
        )
        self.canvas.command_stack.push(
            self._tag_three_d_command(command, before, after),
            already_done=True,
        )
        self._mark_dirty(None)

    def _refresh_three_d_materials(self) -> None:
        if not hasattr(self, "three_d_material_list"):
            return
        sidecar = self._active_blender_sidecar()
        current = self.three_d_material_list.currentRow()
        self.three_d_material_list.blockSignals(True)
        self.three_d_material_list.clear()
        if sidecar is not None:
            for material in sidecar.document.drawing_materials:
                self.three_d_material_list.addItem(material.name)
        if self.three_d_material_list.count():
            self.three_d_material_list.setCurrentRow(
                max(0, min(current, self.three_d_material_list.count() - 1))
            )
        self.three_d_material_list.blockSignals(False)
        self._three_d_material_selected(
            self.three_d_material_list.currentRow()
        )
        self._refresh_three_d_material_mappings()

    def _refresh_three_d_material_mappings(self) -> None:
        """Show Blender slot assignments and their drawing-side mappings."""
        if not hasattr(self, "three_d_material_mapping_table"):
            return
        table = self.three_d_material_mapping_table
        sidecar = self._active_blender_sidecar()
        self._refreshing_three_d_material_mappings = True
        try:
            table.setRowCount(0)
            if sidecar is None:
                return
            document = sidecar.document
            raw_assignments = document.extensions.get(
                "source_material_assignments", {}
            )
            if not isinstance(raw_assignments, dict):
                raw_assignments = {}
            assigned_to: dict[str, list[str]] = {}
            for object_id, raw_slots in raw_assignments.items():
                object_id = str(object_id)
                object_data = document.object_catalog.get(object_id, {})
                object_name = str(object_data.get("name") or object_id)
                if isinstance(raw_slots, dict):
                    slots = tuple(raw_slots.items())
                elif isinstance(raw_slots, (list, tuple)):
                    slots = tuple(enumerate(raw_slots))
                else:
                    slots = ((0, raw_slots),)
                for slot, source_id in slots:
                    if source_id is None or not str(source_id):
                        continue
                    source_id = str(source_id)
                    try:
                        slot_label = str(int(slot) + 1)
                    except (TypeError, ValueError):
                        slot_label = str(slot)
                    assigned_to.setdefault(source_id, []).append(
                        f"{object_name} [slot {slot_label}]"
                    )

            source_ids = (
                set(document.material_catalog)
                | set(document.material_mappings)
                | set(assigned_to)
            )
            ordered_sources = sorted(
                source_ids,
                key=lambda source_id: (
                    str(document.material_catalog.get(source_id, {}).get(
                        "name", source_id
                    )).casefold(),
                    source_id,
                ),
            )
            drawing_materials = tuple(document.drawing_materials)
            table.setRowCount(len(ordered_sources))
            for row, source_id in enumerate(ordered_sources):
                source_data = document.material_catalog.get(source_id, {})
                source_name = str(source_data.get("name") or source_id)
                source_item = QTableWidgetItem(source_name)
                source_item.setData(Qt.ItemDataRole.UserRole, source_id)
                source_item.setToolTip(f"Blender material ID: {source_id}")
                table.setItem(row, 0, source_item)

                assignment_labels = assigned_to.get(source_id, [])
                assignment_item = QTableWidgetItem(
                    ", ".join(assignment_labels) if assignment_labels
                    else "Not assigned in participating collections"
                )
                assignment_item.setToolTip("\n".join(assignment_labels))
                table.setItem(row, 1, assignment_item)

                mapping = QComboBox(table)
                mapping.addItem("Use Blender material", "")
                for material in drawing_materials:
                    mapping.addItem(material.name, material.material_id)
                target_id = document.material_mappings.get(source_id, "")
                target_index = mapping.findData(target_id)
                mapping.setCurrentIndex(max(0, target_index))
                mapping.setToolTip(
                    "Choose a drawing-side renderer material, or leave the "
                    "original Blender material unmapped"
                )
                mapping.currentIndexChanged.connect(
                    lambda _index, source_id=source_id, control=mapping:
                    self._set_three_d_material_mapping(
                        source_id, control.currentData()
                    )
                )
                table.setCellWidget(row, 2, mapping)
        finally:
            self._refreshing_three_d_material_mappings = False

    def _set_three_d_material_mapping(
        self, source_id: str, target_id: str | None,
    ) -> None:
        if getattr(self, "_refreshing_three_d_material_mappings", False):
            return
        sidecar = self._active_blender_sidecar()
        if sidecar is None:
            return
        source_id = str(source_id)
        target_id = str(target_id or "")
        drawing_by_id = {
            material.material_id: material
            for material in sidecar.document.drawing_materials
        }
        if target_id and target_id not in drawing_by_id:
            self.statusBar().showMessage(
                "The selected drawing material no longer exists", 4000
            )
            self._refresh_three_d_material_mappings()
            return
        current_target = sidecar.document.material_mappings.get(source_id, "")
        if current_target == target_id:
            return
        before = copy.deepcopy(sidecar)
        if target_id:
            sidecar.document.material_mappings[source_id] = target_id
        else:
            sidecar.document.material_mappings.pop(source_id, None)
        for material in sidecar.document.drawing_materials:
            material.source_material_ids = [
                item for item in material.source_material_ids
                if item != source_id
            ]
        if target_id:
            drawing_by_id[target_id].source_material_ids.append(source_id)
        sidecar.document.revision += 1
        self._push_sidecar_change(before, "Map Blender material")
        self.three_d_controller.request_render()

    def _add_three_d_material(self) -> None:
        sidecar = self._active_blender_sidecar()
        if sidecar is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Create 3D material", "Material name", text="Material"
        )
        if not accepted or not name.strip():
            return
        before = copy.deepcopy(sidecar)
        sidecar.document.drawing_materials.append(
            DrawingMaterial3D(name=name.strip())
        )
        sidecar.document.revision += 1
        self._push_sidecar_change(before, "Create 3D material")
        self.three_d_material_list.setCurrentRow(
            len(sidecar.document.drawing_materials) - 1
        )

    def _rename_three_d_material(self) -> None:
        sidecar = self._active_blender_sidecar()
        row = self.three_d_material_list.currentRow()
        if sidecar is None or not 0 <= row < len(sidecar.document.drawing_materials):
            return
        material = sidecar.document.drawing_materials[row]
        name, accepted = QInputDialog.getText(
            self, "Rename 3D material", "Material name", text=material.name
        )
        if not accepted or not name.strip() or name.strip() == material.name:
            return
        before = copy.deepcopy(sidecar)
        material.name = name.strip()
        sidecar.document.revision += 1
        self._push_sidecar_change(before, "Rename 3D material")

    def _delete_three_d_material(self) -> None:
        sidecar = self._active_blender_sidecar()
        row = self.three_d_material_list.currentRow()
        if sidecar is None or not 0 <= row < len(sidecar.document.drawing_materials):
            return
        material = sidecar.document.drawing_materials[row]
        if QMessageBox.question(
            self, "Delete 3D material?",
            f'Delete drawing material "{material.name}" and clear its mappings?',
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No,
        ) != QMessageBox.Yes:
            return
        before = copy.deepcopy(sidecar)
        sidecar.document.drawing_materials.pop(row)
        sidecar.document.material_mappings = {
            source: target
            for source, target in sidecar.document.material_mappings.items()
            if target != material.material_id
        }
        sidecar.document.revision += 1
        self._push_sidecar_change(before, "Delete 3D material")

    def _three_d_material_selected(self, row: int) -> None:
        sidecar = self._active_blender_sidecar()
        enabled = bool(
            sidecar is not None
            and 0 <= row < len(sidecar.document.drawing_materials)
        )
        for control in (
            self.three_d_material_shader, self.three_d_material_texture,
            self.three_d_material_vertex, self.three_d_material_rename,
            self.three_d_material_delete, self.three_d_material_tint,
            self.three_d_material_toon_ramp,
            self.three_d_material_outline,
            self.three_d_material_outline_color,
            self.three_d_material_outline_width,
        ):
            control.setEnabled(enabled)
        if not enabled:
            return
        material = sidecar.document.drawing_materials[row]
        for control in (
            self.three_d_material_shader, self.three_d_material_texture,
            self.three_d_material_vertex, self.three_d_material_tint,
            self.three_d_material_toon_ramp,
            self.three_d_material_outline,
            self.three_d_material_outline_color,
            self.three_d_material_outline_width,
        ):
            control.blockSignals(True)
        self.three_d_material_shader.setCurrentIndex(max(
            0, self.three_d_material_shader.findData(material.shader)
        ))
        self.three_d_material_texture.setChecked(material.use_texture)
        self.three_d_material_vertex.setChecked(material.use_vertex_color)
        self.three_d_material_tint.setText(material.tint)
        self.three_d_material_toon_ramp.setText(
            self._format_three_d_toon_ramp(material.toon_ramp)
        )
        self.three_d_material_outline.setChecked(material.outline_enabled)
        self.three_d_material_outline_color.setText(material.outline_color)
        self.three_d_material_outline_width.setValue(material.outline_width)
        for control in (
            self.three_d_material_shader, self.three_d_material_texture,
            self.three_d_material_vertex, self.three_d_material_tint,
            self.three_d_material_toon_ramp,
            self.three_d_material_outline,
            self.three_d_material_outline_color,
            self.three_d_material_outline_width,
        ):
            control.blockSignals(False)
        self.three_d_material_toon_ramp.setEnabled(material.shader == "toon")
        self.three_d_material_outline_color.setEnabled(
            material.outline_enabled
        )
        self.three_d_material_outline_width.setEnabled(
            material.outline_enabled
        )

    @staticmethod
    def _format_three_d_toon_ramp(
        ramp: list[tuple[float, str]],
    ) -> str:
        return ", ".join(
            f"{float(position):g}:{color}" for position, color in ramp
        )

    @staticmethod
    def _parse_three_d_color(value: str, label: str) -> str:
        text = str(value).strip()
        digits = text[1:] if text.startswith("#") else text
        if (
            len(digits) not in {3, 4, 6, 8}
            or re.fullmatch(r"[0-9A-Fa-f]+", digits) is None
        ):
            raise ValueError(f"{label} must be a hexadecimal color")
        color = QColor(f"#{digits}")
        if not color.isValid():
            raise ValueError(f"{label} is not a valid color")
        return canonical_argb(color)

    @classmethod
    def _parse_three_d_toon_ramp(
        cls, value: str,
    ) -> list[tuple[float, str]]:
        stops: list[tuple[float, str]] = []
        for raw_stop in re.split(r"\s*[,;]\s*", str(value).strip()):
            if not raw_stop:
                continue
            if ":" not in raw_stop:
                raise ValueError(
                    "Each Toon stop must use position:#AARRGGBB"
                )
            raw_position, raw_color = raw_stop.split(":", 1)
            position = float(raw_position.strip())
            if not math.isfinite(position) or not 0.0 <= position <= 1.0:
                raise ValueError(
                    "Toon ramp positions must be between zero and one"
                )
            stops.append((
                position,
                cls._parse_three_d_color(raw_color, "Toon ramp color"),
            ))
        if not stops:
            raise ValueError("The Toon ramp needs at least one stop")
        return sorted(stops, key=lambda item: item[0])

    def _edit_three_d_material(self, *args) -> None:
        del args
        sidecar = self._active_blender_sidecar()
        row = self.three_d_material_list.currentRow()
        if (
            getattr(self, "_syncing_three_d_controls", False)
            or sidecar is None
            or not 0 <= row < len(sidecar.document.drawing_materials)
        ):
            return
        material = sidecar.document.drawing_materials[row]
        updated = copy.deepcopy(material)
        try:
            updated.shader = self.three_d_material_shader.currentData()
            updated.use_texture = self.three_d_material_texture.isChecked()
            updated.use_vertex_color = self.three_d_material_vertex.isChecked()
            updated.tint = self._parse_three_d_color(
                self.three_d_material_tint.text(), "Tint"
            )
            updated.toon_ramp = self._parse_three_d_toon_ramp(
                self.three_d_material_toon_ramp.text()
            )
            updated.outline_enabled = self.three_d_material_outline.isChecked()
            updated.outline_color = self._parse_three_d_color(
                self.three_d_material_outline_color.text(), "Outline color"
            )
            updated.outline_width = (
                self.three_d_material_outline_width.value()
            )
            updated.validate()
        except (TypeError, ValueError) as error:
            self.statusBar().showMessage(str(error), 5000)
            self._three_d_material_selected(row)
            return
        if updated == material:
            return
        before = copy.deepcopy(sidecar)
        sidecar.document.drawing_materials[row] = updated
        sidecar.document.revision += 1
        self._push_sidecar_change(before, "Edit 3D material")
        self.three_d_controller.request_render()

    def _reset_three_d_object_overrides(self) -> None:
        if not self.three_d_controller.reset_selected_to_blender():
            self.statusBar().showMessage(
                "Select a Blender object or local entity first", 4000
            )

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
        if getattr(self.canvas, "in_three_d_mode", False):
            for button in self.tool_buttons.values():
                button.setVisible(False)
            self.shapes_category.setVisible(False)
            self.drawing_selection_category.setVisible(False)
            self._sync_three_d_tool_buttons()
            self._sync_contextual_ribbon()
            return
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

    def _update_gradient_group_visibility(self, context: str) -> None:
        """Show only the controls relevant to the current gradient context."""
        creating = context == "create"
        editing = context == "color"
        self.gradient_create_group.setVisible(creating)
        self.gradient_type_group.setVisible(editing)
        self.gradient_parameters_group.setVisible(editing)
        self.gradient_thickness_group.setVisible(False)
        self.gradient_impact_group.setVisible(False)

    def _sync_contextual_ribbon(self) -> None:
        if not hasattr(self, "ribbon"):
            return
        if getattr(self.canvas, "in_three_d_mode", False):
            for key in (
                "three_d_view", "three_d_rendering", "three_d_outline",
                "three_d_materials", "three_d_object",
                "three_d_tool_settings",
            ):
                self.ribbon.set_page_visible(key, True)
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
        text_active = isinstance(selected_object, TextObject)
        selected_gradient = self.gradient_tools_controls.selected_gradient()
        gradient_selected = selected_gradient is not None
        gradient_parent_id = self._gradient_context_parent_id()
        gradient_active = bool(gradient_parent_id)
        entering = active and not self._vector_ribbon_context
        entering_text = text_active and not self._text_ribbon_context
        selected_gradient_id = (
            selected_gradient.object_id if gradient_selected else ""
        )
        entering_gradient = bool(
            selected_gradient_id
            and selected_gradient_id != self._selected_gradient_ribbon_id
        )
        self._vector_ribbon_context = active
        self._text_ribbon_context = text_active
        self._gradient_ribbon_context = gradient_active
        self._selected_gradient_ribbon_id = selected_gradient_id
        self.ribbon.set_page_visible("vector_tools", active)
        self.ribbon.set_page_visible("gradient_tools", gradient_active)
        self.tool_settings_group.setVisible(not text_active)
        for group in (
            self.text_object_group,
            self.text_typography_group,
            self.text_layout_group,
        ):
            group.setVisible(text_active)
        if (
            self._manual_ribbon_page
            and not self.ribbon.is_page_visible(self._manual_ribbon_page)
        ):
            self._manual_ribbon_page = ""
        if entering_text:
            self._select_ribbon_page("tool_settings")
        elif entering:
            self._select_ribbon_page("vector_tools")
        elif entering_gradient:
            self._select_ribbon_page("gradient_tools")
        elif self._manual_ribbon_page:
            self._select_ribbon_page(self._manual_ribbon_page)
        self.tool_settings_controls.set_context(
            self.canvas.tool, vector_active=vector_tool_context
        )
        if text_active:
            self.text_object_controls.refresh()
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

    # ---- asset library -------------------------------------------------
    def _current_project_context(self) -> ProjectContext | None:
        if self.active_session is not None:
            return self.active_session.context
        if self.repository is not None and self.series is not None:
            return ProjectContext.create(self.repository, self.series)
        return None

    def _show_tree_context_menu(self, point) -> None:
        index = self.tree.indexAt(point)
        if not index.isValid() or self.chapter is None:
            return
        index = index.siblingAtColumn(0)
        self.tree.setCurrentIndex(index)
        item = self.hierarchy_model.item_for_index(index)
        if item.kind in {"blender_root", "blender_entity"}:
            self.canvas.set_blender_virtual_selection(
                item.owner_layer_id, item.entity_id, item.source_id
            )
            return
        self.canvas.set_selection(item.kind, item.entity_id, activate_default_tool=True)
        menu = QMenu(self)
        rename = menu.addAction("Rename")
        copy_asset = menu.addAction("Copy as Asset")
        rasterize = (
            menu.addAction("Rasterize Image")
            if item.kind == "object" and isinstance(
                self.chapter.objects.get(item.entity_id), ImageObject
            ) else None
        )
        selected_layer = (
            self.chapter.layers.get(item.entity_id)
            if item.kind == "layer" else None
        )
        duplicate_blender = (
            menu.addAction("Duplicate 3D Layer")
            if selected_layer is not None
            and selected_layer.layer_kind == "blender" else None
        )
        copy_asset.setEnabled(
            self._current_project_context() is not None
            and not (
                selected_layer is not None
                and selected_layer.layer_kind == "blender"
            )
        )
        selected = menu.exec(self.tree.viewport().mapToGlobal(point))
        if selected is rename:
            self.tree.edit(index)
        elif selected is copy_asset:
            self._copy_selected_as_asset(item.kind, item.entity_id)
        elif duplicate_blender is not None and selected is duplicate_blender:
            self._duplicate_blender_layer(item.entity_id)
        elif rasterize is not None and selected is rasterize:
            self._rasterize_image(item.entity_id)

    def _copy_selected_as_asset(self, kind: str, entity_id: str) -> None:
        context = self._current_project_context()
        if context is None or self.chapter is None:
            return
        entity = (
            self.chapter.layers.get(entity_id)
            if kind == "layer" else self.chapter.objects.get(entity_id)
        )
        if entity is None:
            return
        if isinstance(entity, LayerNode) and entity.layer_kind == "blender":
            self.statusBar().showMessage(
                "Blender-linked layers cannot be copied to the Asset Library",
                5000,
            )
            return
        default_name = (
            entity.source_filename
            if isinstance(entity, ImageObject)
            else getattr(entity, "name", "Asset")
        )
        name, accepted = QInputDialog.getText(
            self, "Copy as Asset", "Asset name", text=default_name
        )
        if not accepted or not name.strip():
            return
        existing = context.assets.find_by_name(name)
        open_session = (
            self.sessions.get(self._asset_session_key(context, existing.asset_id))
            if existing is not None else None
        )
        if existing is not None:
            answer = QMessageBox.question(
                self, "Replace asset?",
                f'An asset named "{existing.name}" already exists. '
                "Replace the Asset Library version with the selected content?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.No,
            )
            if answer != QMessageBox.Yes:
                return
            if open_session is not None and (
                open_session.dirty
                or open_session is self.active_session and self._dirty
            ):
                answer = QMessageBox.question(
                    self, "Discard unsaved asset changes?",
                    f'"{existing.name}" is open with unsaved changes. '
                    "Replace it and discard those changes?",
                    QMessageBox.Yes | QMessageBox.No,
                    QMessageBox.No,
                )
                if answer != QMessageBox.Yes:
                    return
        try:
            manifest, tiles, images = extract_asset(
                self.chapter, self.canvas.tiles, kind, entity_id, name,
                source_images=self.canvas.images, include_images=True,
            )
            thumbnail = self.canvas.render_asset_thumbnail(
                manifest, tiles, images=images
            )
            if existing is None:
                context.assets.create(
                    manifest, tiles, thumbnail, images=images,
                    folder_id=self.asset_library.selected_folder_id(),
                )
            else:
                manifest = context.assets.replace(
                    existing.asset_id, manifest, tiles, thumbnail,
                    images=images,
                )
        except (OSError, KeyError, ValueError) as error:
            QMessageBox.warning(self, "Unable to create asset", str(error))
            return
        if existing is not None and open_session is not None:
            self._reload_replaced_asset_session(
                open_session, manifest, tiles, images
            )
        self.asset_library.refresh()
        self.ribbon.select_page("asset_library")
        action = "Replaced" if existing is not None else "Created"
        self.statusBar().showMessage(f"{action} asset {manifest.name}", 4000)

    def _rasterize_image(self, object_id: str) -> None:
        if self.chapter is None:
            return
        obj = self.chapter.objects.get(object_id)
        if not isinstance(obj, ImageObject):
            return
        image = self.canvas.images.image(object_id)
        if image.isNull():
            QMessageBox.warning(
                self, "Rasterize Image", "The embedded image cannot be decoded."
            )
            return
        before_model = self.chapter.to_dict()
        before_images = self.canvas.images.snapshot()
        before_tiles = self.canvas.tiles.object_tiles(object_id)
        quad = self.canvas._image_local_quad(obj)
        raster = RasterObject(
            object_id=obj.object_id, name=obj.name,
            custom_name=obj.custom_name,
            parent_layer_id=obj.parent_layer_id,
            visible=obj.visible, opacity=obj.opacity,
            opacity_locked=obj.opacity_locked,
            geometry_reference=obj.geometry_reference,
            ignore_parent_mask=obj.ignore_parent_mask,
            underlay_opacity=obj.underlay_opacity,
            interaction_rect=(0.0, 0.0, image.width(), image.height()),
            transform_frame=(0.0, 0.0, image.width(), image.height()),
            transform_quad=list(quad),
        )
        self.chapter.objects[object_id] = raster
        self.canvas.images.remove(object_id)
        self.canvas.tiles.remove_object(object_id)
        size = self.canvas.tiles.tile_size
        for tile_y in range(math.ceil(image.height() / size)):
            for tile_x in range(math.ceil(image.width() / size)):
                tile = image.copy(
                    tile_x * size, tile_y * size,
                    min(size, image.width() - tile_x * size),
                    min(size, image.height() - tile_y * size),
                )
                self.canvas.tiles.set_tile(object_id, (tile_x, tile_y), tile)
        after_model = self.chapter.to_dict()
        after_images = self.canvas.images.snapshot()
        after_tiles = self.canvas.tiles.object_tiles(object_id)

        def restore(model: dict, resources: dict, tiles: dict) -> None:
            self.canvas.replace_chapter(model)
            self.canvas.images.restore(resources)
            self.canvas.tiles.replace_object_tiles(object_id, tiles)
            self.canvas.set_selection("object", object_id)
            self.canvas.hierarchyChanged.emit()
            self.canvas.documentChanged.emit(None)
            self.canvas.update()

        self.canvas.command_stack.push(CallbackCommand(
            "Rasterize image",
            lambda: restore(after_model, after_images, after_tiles),
            lambda: restore(before_model, before_images, before_tiles),
        ), already_done=True)
        self.canvas.set_selection("object", object_id)
        self.canvas.hierarchyChanged.emit()
        self.canvas.documentChanged.emit(None)
        self._sync_contextual_ribbon()

    def _reload_replaced_asset_session(
        self, session: EditorSession, manifest: AssetManifest, tiles: TileStore,
        images: ImageStore | None = None,
    ) -> None:
        """Reload an open asset tab after its repository entry is replaced."""
        session.asset_manifest = manifest
        session.chapter = manifest.document
        session.tiles = tiles
        session.images = images or ImageStore()
        session.canvas_state = None
        session.dirty = False
        session.last_autosave = 0.0
        session.expanded_entities.clear()
        if session is not self.active_session:
            self._refresh_project_tabs()
            return

        self.chapter = manifest.document
        self._dirty = False
        self._last_autosave = 0.0
        self.autosave_timer.stop()
        self.canvas.command_stack = CommandStack()
        self.canvas.set_document(manifest.document, tiles, session.images)
        self.canvas.command_stack.changed_callback = self._command_stack_changed
        self.canvas.set_selection(manifest.root_kind, manifest.root_id)
        self.chapter_combo.setItemText(0, f"Asset: {manifest.name}")
        self.setWindowTitle(f"{manifest.name} — Vertical Comic Editor")
        self.preview.invalidate_all()
        self.selection_common.refresh()
        self.selection_settings.refresh()
        self._sync_contextual_ribbon()
        session.dirty = False
        self._dirty = False
        self.autosave_timer.stop()
        self._refresh_project_tabs()
        self._refresh_actions()

    def _open_asset(self, asset_id: str) -> None:
        context = self._current_project_context()
        if context is None:
            return
        key = self._asset_session_key(context, asset_id)
        existing = self._tab_index_for_key(key)
        if existing >= 0:
            self.project_tabs.setCurrentIndex(existing)
            return
        recover = False
        if context.assets.has_recovery(asset_id):
            recover = QMessageBox.question(
                self, "Recover autosave",
                "A newer autosave exists for this asset. Recover it?",
            ) == QMessageBox.Yes
        try:
            manifest, tiles, images = context.assets.load(
                asset_id, recover=recover, include_images=True
            )
        except (OSError, ValueError, KeyError) as error:
            QMessageBox.critical(self, "Unable to open asset", str(error))
            return
        load_warnings = list(context.assets.last_load_warnings)
        if load_warnings:
            QMessageBox.warning(
                self, "Unsupported content omitted",
                "\n".join(load_warnings),
            )
        self._add_editor_session(EditorSession(
            key=key, kind="asset", context=context,
            chapter=manifest.document, tiles=tiles, images=images,
            asset_manifest=manifest, dirty=recover or bool(load_warnings),
        ))

    def _rename_asset(self, asset_id: str) -> None:
        context = self._current_project_context()
        if context is None:
            return
        manifest = next((
            asset for asset in context.assets.list_assets()
            if asset.asset_id == asset_id
        ), None)
        if manifest is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename Asset", "Asset name", text=manifest.name
        )
        if not accepted or not name.strip() or name.strip() == manifest.name:
            return
        try:
            renamed = context.assets.rename(asset_id, name)
        except (OSError, ValueError) as error:
            QMessageBox.warning(self, "Unable to rename asset", str(error))
            return
        for session in self.sessions.values():
            if (
                session.kind == "asset"
                and session.context.repository.root == context.repository.root
                and session.asset_manifest is not None
                and session.asset_manifest.asset_id == asset_id
            ):
                session.asset_manifest.name = renamed.name
        if (
            self.active_session is not None
            and self.active_session.kind == "asset"
            and self.active_session.asset_manifest is not None
            and self.active_session.asset_manifest.asset_id == asset_id
        ):
            self.chapter_combo.setItemText(0, f"Asset: {renamed.name}")
            self.setWindowTitle(f"{renamed.name} — Vertical Comic Editor")
        self.asset_library.refresh()
        self._refresh_project_tabs()

    def _delete_asset(self, asset_id: str) -> None:
        context = self._current_project_context()
        if context is None:
            return
        manifest = next(
            (asset for asset in context.assets.list_assets()
             if asset.asset_id == asset_id), None
        )
        if manifest is None:
            return
        answer = QMessageBox.question(
            self, "Delete Asset",
            f'Delete the asset "{manifest.name}"? This cannot be undone.',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            context.assets.delete(asset_id)
        except (OSError, ValueError, FileNotFoundError) as error:
            QMessageBox.warning(self, "Unable to delete asset", str(error))
            return
        for key, session in list(self.sessions.items()):
            if (
                session.kind == "asset"
                and session.context.repository.root == context.repository.root
                and session.asset_manifest is not None
                and session.asset_manifest.asset_id == asset_id
            ):
                index = self._tab_index_for_key(key)
                self.sessions.pop(key, None)
                if index >= 0:
                    self.project_tabs.removeTab(index)
                if session is self.active_session:
                    self.active_session = None
        self.asset_library.refresh()
        if not self.project_tabs.count():
            self._clear_active_session()
        elif self.active_session is None:
            session = self.sessions.get(str(self.project_tabs.tabData(
                self.project_tabs.currentIndex()
            )))
            if session is not None:
                self._activate_editor_session(session)

    def _rename_asset_folder(self, folder_id: str) -> None:
        folder = self.asset_library.repository.get_folder(folder_id) if self.asset_library.repository else None
        if folder is None:
            return
        name, accepted = QInputDialog.getText(
            self, "Rename Folder", "Folder name", text=folder.name
        )
        if not accepted or not name.strip() or name.strip() == folder.name:
            return
        try:
            self.asset_library.repository.rename_folder(folder_id, name)
        except (OSError, ValueError, FileNotFoundError) as error:
            QMessageBox.warning(self, "Unable to rename folder", str(error))
            return
        self.asset_library.refresh()

    def _delete_asset_folder(self, folder_id: str) -> None:
        repository = self.asset_library.repository
        folder = repository.get_folder(folder_id) if repository else None
        if repository is None or folder is None:
            return
        assets = repository.assets_in_folder(folder_id, recursive=True)
        answer = QMessageBox.question(
            self, "Delete Folder",
            f'Delete "{folder.name}" and its {len(assets)} asset(s)?',
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            deleted_ids = repository.delete_folder(folder_id, recursive=True)
        except (OSError, ValueError, FileNotFoundError) as error:
            QMessageBox.warning(self, "Unable to delete folder", str(error))
            return
        deleted = set(deleted_ids)
        for key, session in list(self.sessions.items()):
            if (
                session.kind == "asset"
                and session.context.repository.root == repository.series_root
                and session.asset_manifest is not None
                and session.asset_manifest.asset_id in deleted
            ):
                index = self._tab_index_for_key(key)
                self.sessions.pop(key, None)
                if index >= 0:
                    self.project_tabs.removeTab(index)
                if session is self.active_session:
                    self.active_session = None
        self.asset_library.refresh()
        if not self.project_tabs.count():
            self._clear_active_session()
        elif self.active_session is None:
            session = self.sessions.get(str(self.project_tabs.tabData(
                self.project_tabs.currentIndex()
            )))
            if session is not None:
                self._activate_editor_session(session)

    @staticmethod
    def _image_file_filter() -> str:
        extensions = sorted({
            f"*.{bytes(value).decode('ascii', 'ignore').lower()}"
            for value in QImageReader.supportedImageFormats()
            if bytes(value)
        })
        return f"Images ({' '.join(extensions)})" if extensions else "Images (*)"

    def _selected_parent_center(self, parent_id: str) -> QPointF:
        layer = self.chapter.layers[parent_id]
        bounds = (
            self.canvas.layer_effective_path(parent_id).boundingRect()
            if layer.bound is not None else QRectF(0, 0, 1, 1)
        )
        world_x, world_y = self.chapter.layer_world_translation(parent_id)
        return QPointF(
            bounds.center().x() + world_x,
            bounds.center().y() + world_y,
        )

    def _place_import_sources(
        self, sources: list[tuple[str, str, bytes]], label: str,
    ) -> list[str]:
        parent = self._selected_parent_layer(allow_page=True)
        if (
            parent is None and self.chapter is not None
            and self.chapter.document_kind != "asset"
            and self.canvas.active_page_id in self.chapter.layers
        ):
            parent = self.chapter.layers[self.canvas.active_page_id]
        if parent is None or self.chapter is None:
            self.statusBar().showMessage(
                "Select a page or container before importing images", 5000
            )
            return []
        created = self.canvas.place_image_sources(
            sources, parent.layer_id,
            self._selected_parent_center(parent.layer_id),
            insertion_index=self._new_object_insertion_index(parent.layer_id),
            fit_parent=False, label=label,
        )
        if len(created) != len(sources):
            self.statusBar().showMessage(
                f"Imported {len(created)} of {len(sources)} images", 5000
            )
        elif created:
            self.statusBar().showMessage(
                f"Imported {len(created)} image{'s' if len(created) != 1 else ''}",
                3500,
            )
        return created

    def _import_images_dialog(self) -> None:
        paths, _selected_filter = QFileDialog.getOpenFileNames(
            self, "Import Images", "", self._image_file_filter()
        )
        sources: list[tuple[str, str, bytes]] = []
        for raw in paths:
            path = Path(raw)
            try:
                sources.append((path.name, "", path.read_bytes()))
            except OSError:
                continue
        if sources:
            self._place_import_sources(sources, "Import images")

    def _clipboard_image_sources(self) -> list[tuple[str, str, bytes]]:
        mime = QApplication.clipboard().mimeData()
        sources: list[tuple[str, str, bytes]] = []
        for url in mime.urls() if mime.hasUrls() else []:
            if not url.isLocalFile():
                continue
            path = Path(url.toLocalFile())
            try:
                data = path.read_bytes()
            except OSError:
                continue
            probe = ImageStore()
            try:
                probe.put("clipboard", path.name, data)
            except ValueError:
                continue
            sources.append((path.name, "", data))
        if sources:
            return sources
        if not mime.hasImage():
            return []
        value = mime.imageData()
        image = value.toImage() if hasattr(value, "toImage") else QImage(value)
        if image.isNull():
            return []
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        saved = image.save(buffer, "PNG")
        buffer.close()
        return [(
            "Clipboard Image.png", "image/png", bytes(payload)
        )] if saved else []

    def _paste_image(self) -> bool:
        sources = self._clipboard_image_sources()
        return bool(
            sources and self._place_import_sources(sources, "Paste image")
        )

    # ---- selection and model synchronization --------------------------
    def _tree_selection_changed(self, selected: QItemSelection, deselected) -> None:
        indexes = selected.indexes()
        index = next((item for item in indexes if item.column() == 0), QModelIndex())
        if not index.isValid():
            return
        item = self.hierarchy_model.item_for_index(index)
        if item.kind in {"blender_root", "blender_entity"}:
            self.canvas.set_blender_virtual_selection(
                item.owner_layer_id, item.entity_id, item.source_id
            )
            if item.kind == "blender_entity":
                self.three_d_object_metadata.setText(
                    self._three_d_metadata_text(
                        item.source_id, item.type_label
                    )
                )
            return
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
        self.selection_common.refresh()
        self.selection_settings.refresh()
        self._sync_tool_buttons()
        selected_object = (
            self.chapter.objects.get(entity_id)
            if self.chapter is not None and kind == "object" else None
        )
        if isinstance(selected_object, TextObject):
            self._select_ribbon_page("tool_settings")
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
        self.selection_settings.refresh()
        self._sync_tool_buttons()
        if tool in {
            ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.TEXT_EDIT,
        }:
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
        if self.active_session is not None:
            self.active_session.chapter = chapter
            self.active_session.tiles = self.canvas.tiles
            self.active_session.images = self.canvas.images
            if self.active_session.asset_manifest is not None:
                self.active_session.asset_manifest.document = chapter
        self._refresh_hierarchy()
        self.selection_common.refresh()
        self.selection_settings.refresh()
        self.preview.invalidate_all()
        self._sync_tool_buttons()
        self._mark_dirty(None)

    def _hierarchy_changed(self) -> None:
        if self.chapter is not self.canvas.chapter:
            self.chapter = self.canvas.chapter
        self._refresh_hierarchy()
        self.selection_common.refresh()
        self.selection_settings.refresh()
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
        color_ramp = MainWindow._gradient_color_ramp(obj)
        color_ramp.stops = [
            ColorGradientStop(
                stop_id=stop.stop_id, position=stop.position,
                color=stop.color,
            )
            for stop in ramp.stops
        ]
        color_ramp.validate()
        obj.loaded_preset_id = preset_id
        obj.touch_revision()
        after = self.chapter.to_dict()
        self.canvas.push_model_change(
            before, after, "Load gradient preset"
        )
        self.canvas.documentChanged.emit(None)
        self.gradient_tools_controls.refresh()

    @staticmethod
    def _gradient_color_ramp(obj):
        if isinstance(obj, SpeedLinesGradientObject):
            return obj.color_ramp
        return obj.ramp

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
            name=f"Gradient {number}", ramp=MainWindow._gradient_color_ramp(obj).copy()
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
        preset.ramp = MainWindow._gradient_color_ramp(obj).copy()
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
        if self.active_session is not None:
            self.active_session.dirty = True
        self.autosave_timer.start(2000)
        self._refresh_project_tabs()
        self._refresh_actions()

    def _command_stack_changed(self) -> None:
        self._mark_dirty(None)
        self._refresh_actions()

    def save(self) -> bool:
        if self.active_session is not None:
            return self._save_editor_session(self.active_session)
        if self.repository is None or self.chapter is None:
            return False
        try:
            self.repository.save_chapter(
                self.chapter, self.canvas.tiles, self.canvas.images,
                blender_sidecar=self._active_blender_sidecar(),
            )
            for reference in self.series.chapters:
                if reference.chapter_id == self.chapter.chapter_id:
                    reference.name = self.chapter.name
            self.repository.save_series(self.series)
        except (OSError, ValueError) as error:
            QMessageBox.critical(self, "Save failed", str(error))
            return False
        self._dirty = False
        self.autosave_timer.stop()
        if self.repository.last_save_warnings:
            self.statusBar().showMessage(
                f"Saved with warning: {self.repository.last_save_warnings[0]}",
                7000,
            )
        else:
            self.statusBar().showMessage("Saved", 3000)
        self._refresh_actions()
        return True

    @staticmethod
    def _copy_session_tiles(
        chapter: ChapterDocument, source: TileStore,
    ) -> TileStore:
        copied = TileStore(source.tile_size)
        for object_id, obj in chapter.objects.items():
            if isinstance(obj, RasterObject):
                copied.replace_object_tiles(
                    object_id, source.object_tiles(object_id)
                )
        return copied

    def _write_session_to_clone(
        self, session: EditorSession, repository: SeriesRepository,
        series,
    ) -> None:
        """Write one session through detached models and tiles."""
        chapter = copy.deepcopy(session.chapter)
        tiles = self._copy_session_tiles(chapter, session.tiles)
        images = session.images.clone({
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, ImageObject)
        })
        if session.kind == "series":
            blender_sidecar = copy.deepcopy(session.blender_sidecar)
            if blender_sidecar is not None:
                blender_sidecar.document.series_id = series.series_id
                blender_sidecar.document.revision += 1
            repository.save_chapter(
                chapter, tiles, images,
                blender_sidecar=blender_sidecar,
            )
            for reference in series.chapters:
                if reference.chapter_id == chapter.chapter_id:
                    reference.name = chapter.name
            return

        if session.asset_manifest is None:
            raise ValueError("Asset session has no manifest")
        manifest = copy.deepcopy(session.asset_manifest)
        manifest.document = chapter
        bounds = entity_visual_bounds(
            chapter, tiles, manifest.root_kind, manifest.root_id,
        )
        manifest.visual_bounds = (
            bounds.x(), bounds.y(), bounds.width(), bounds.height()
        )
        chapter.width = max(chapter.width, math.ceil(bounds.right() + 64))
        chapter.height = max(chapter.height, math.ceil(bounds.bottom() + 64))
        container = chapter.layers[chapter.root_page_ids[0]]
        container.bound = BoundGeometry.rectangle(
            0, 0, chapter.width, chapter.height
        )
        thumbnail = self.canvas.render_asset_thumbnail(
            manifest, tiles, images=images
        )
        AssetRepository(repository.root).save(
            manifest, tiles, thumbnail, images=images
        )

    def _rebind_sessions_to_clone(
        self,
        sessions: list[EditorSession],
        repository: SeriesRepository,
        series,
    ) -> None:
        clone_context = ProjectContext.create(repository, series)
        replacements: dict[str, str] = {}
        session_ids = {id(session) for session in sessions}
        for session in sessions:
            old_key = session.key
            session.context = clone_context
            session.key = (
                self._series_session_key(repository.root)
                if session.kind == "series"
                else self._asset_session_key(
                    clone_context, session.asset_manifest.asset_id
                )
            )
            replacements[old_key] = session.key
            session.dirty = False
            session.last_autosave = 0.0
            if session.blender_sidecar is not None:
                session.blender_sidecar.document.series_id = series.series_id
                session.blender_sidecar.document.revision += 1
            session.tiles.dirty.clear()
            session.images.dirty.clear()

        rebound: dict[str, EditorSession] = {}
        for old_key, session in self.sessions.items():
            rebound[replacements.get(old_key, old_key)] = session
        self.sessions = rebound
        for index in range(self.project_tabs.count()):
            old_key = str(self.project_tabs.tabData(index))
            if old_key in replacements:
                self.project_tabs.setTabData(index, replacements[old_key])
                self.project_tabs.setTabToolTip(index, str(repository.root))

        if self.active_session is not None and id(self.active_session) in session_ids:
            self.repository = repository
            self.series = series
            self._dirty = False
            self._last_autosave = 0.0
            self.autosave_timer.stop()
            self.canvas.asset_repository = clone_context.assets
            self.asset_library.set_repository(clone_context.assets)
            self.three_d_controller.set_documents(
                self.active_session.chapter,
                self.active_session.blender_sidecar,
            )
            self.setWindowTitle(
                f"{self.active_session.name} — Vertical Comic Editor"
            )
        self.asset_library.refresh()
        self.preview.invalidate_all()
        self._refresh_project_tabs()
        self._refresh_actions()

    def _save_as(self) -> bool:
        context = self._current_project_context()
        if context is None:
            return False
        parent = QFileDialog.getExistingDirectory(
            self, "Choose parent folder for the series clone",
            str(context.repository.root.parent),
        )
        if not parent:
            return False
        default_name = f"{context.repository.root.name}-copy"
        folder_name, accepted = QInputDialog.getText(
            self, "Save Series As", "New folder name", text=default_name,
        )
        if not accepted or not folder_name.strip():
            return False
        folder_name = folder_name.strip()
        if (
            Path(folder_name).name != folder_name
            or folder_name in {".", ".."}
            or any(character in folder_name for character in '<>:"/\\|?*')
        ):
            QMessageBox.warning(
                self, "Save As failed", "Enter a valid single folder name."
            )
            return False
        destination = Path(parent).expanduser().resolve() / folder_name
        if destination.exists():
            QMessageBox.warning(
                self, "Save As failed",
                f"The destination folder already exists:\n{destination}",
            )
            return False

        self._capture_active_session()
        source_root = context.repository.root
        project_sessions = [
            session for session in self.sessions.values()
            if session.context.repository.root == source_root
        ]
        cloned_series = copy.deepcopy(context.series)
        cloned_series.series_id = new_id()

        def overlay(staged_repository: SeriesRepository) -> None:
            for session in project_sessions:
                self._write_session_to_clone(
                    session, staged_repository, cloned_series
                )
            staged_repository.save_series(cloned_series)

        try:
            cloned_repository = context.repository.clone_to(
                destination, cloned_series, overlay,
            )
        except Exception as error:
            QMessageBox.critical(self, "Save As failed", str(error))
            return False

        self._rebind_sessions_to_clone(
            project_sessions, cloned_repository, cloned_series,
        )
        self._remember_series(cloned_repository.root)
        self.statusBar().showMessage(
            f"Saved clone to {cloned_repository.root}", 5000
        )
        return True

    def _autosave(self) -> None:
        if self.sessions:
            now = time.monotonic()
            deferred: list[float] = []
            saved = False
            for session in self.sessions.values():
                if not session.dirty:
                    continue
                elapsed = now - session.last_autosave
                if session.last_autosave and elapsed < 30:
                    deferred.append(30 - elapsed)
                    continue
                try:
                    if session.kind == "asset" and session.asset_manifest is not None:
                        session.asset_manifest.document = session.chapter
                        session.context.assets.save(
                            session.asset_manifest, session.tiles,
                            images=session.images,
                            autosave=True,
                        )
                    else:
                        session.context.repository.save_chapter(
                            session.chapter, session.tiles, session.images,
                            autosave=True,
                            blender_sidecar=session.blender_sidecar,
                        )
                    session.last_autosave = now
                    saved = True
                except (OSError, ValueError) as error:
                    self.statusBar().showMessage(
                        f"Autosave failed for {session.name}: {error}", 7000
                    )
            if self.active_session is not None:
                self._last_autosave = self.active_session.last_autosave
            if deferred:
                self.autosave_timer.start(
                    max(1, round(min(deferred) * 1000))
                )
            if saved:
                self.statusBar().showMessage("Recovery autosave updated", 2000)
            return
        if not self._dirty or self.repository is None or self.chapter is None:
            return
        elapsed = time.monotonic() - self._last_autosave
        if self._last_autosave and elapsed < 30:
            self.autosave_timer.start(round((30 - elapsed) * 1000))
            return
        try:
            if (
                self.active_session is not None
                and self.active_session.kind == "asset"
                and self.active_session.asset_manifest is not None
            ):
                self.active_session.asset_manifest.document = self.chapter
                self.active_session.context.assets.save(
                    self.active_session.asset_manifest,
                    self.canvas.tiles, images=self.canvas.images,
                    autosave=True,
                )
            else:
                self.repository.save_chapter(
                    self.chapter, self.canvas.tiles, self.canvas.images,
                    autosave=True,
                    blender_sidecar=self._active_blender_sidecar(),
                )
            self._last_autosave = time.monotonic()
            if self.active_session is not None:
                self.active_session.last_autosave = self._last_autosave
            self.statusBar().showMessage("Recovery autosave updated", 2000)
        except (OSError, ValueError) as error:
            self.statusBar().showMessage(f"Autosave failed: {error}", 7000)

    def _settings_changed(self, *args) -> None:
        self.settings.tablet_mode = self.tablet_mode.isChecked()
        self.settings.snap_to_grid = self.snap_grid.isChecked()
        save_settings(self.settings)
        self.canvas.configure_tablet_navigation()
        self.canvas.update()

    def _ribbon_settings_changed(self) -> None:
        self.settings.clamp()
        save_settings(self.settings)
        self.tool_settings_controls.refresh()
        self.text_object_controls.refresh()
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
        self.tool_settings_controls.refresh()

    def _set_text_shortcut_suppression(self, editing: bool) -> None:
        self._hotkey_text_editing = editing

    def _undo(self) -> None:
        """Undo on the command stack owned by the currently active canvas."""
        self.canvas.command_stack.undo()

    def _redo(self) -> None:
        """Redo on the command stack owned by the currently active canvas."""
        self.canvas.command_stack.redo()

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
        self._rebuild_recent_menu()

    def _rebuild_recent_menu(self) -> None:
        self.open_recent_menu.clear()
        recent_paths = self.settings.recent_series or []
        if not recent_paths:
            empty = self.open_recent_menu.addAction("No Recent Series")
            empty.setEnabled(False)
            return
        for recent in recent_paths:
            action = self.open_recent_menu.addAction(Path(recent).name)
            action.setToolTip(recent)
            action.triggered.connect(
                lambda checked=False, path=recent: self._open_recent_path(path)
            )

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
        series_active = active and (
            self.active_session is None or self.active_session.kind == "series"
        )
        self.save_action.setEnabled(active and self._dirty)
        self.save_as_action.setEnabled(self._current_project_context() is not None)
        self.new_chapter_action.setEnabled(series_active and self.series is not None)
        self.trim_action.setEnabled(series_active)
        self.add_page_button.setEnabled(series_active)
        self.undo_action.setEnabled(self.canvas.command_stack.can_undo)
        self.redo_action.setEnabled(self.canvas.command_stack.can_redo)
        self.add_raster_button.setEnabled(active)
        self.add_vector_button.setEnabled(active)
        self.add_text_button.setEnabled(active)
        self.add_fill_button.setEnabled(active)
        self._sync_tool_buttons()

    def _toggle_fullscreen(self) -> None:
        self.showNormal() if self.isFullScreen() else self.showFullScreen()

    def closeEvent(self, event: QCloseEvent) -> None:  # noqa: N802
        if self.sessions:
            self._capture_active_session()
            for session in list(self.sessions.values()):
                if not session.dirty:
                    continue
                answer = QMessageBox.question(
                    self, "Unsaved changes",
                    f"Save {session.name} before exiting?",
                    QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
                    QMessageBox.Save,
                )
                if answer == QMessageBox.Cancel:
                    event.ignore()
                    return
                if answer == QMessageBox.Save and not self._save_editor_session(session):
                    event.ignore()
                    return
        elif not self._confirm_discard_or_save():
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
        sync_manager = getattr(self, "three_d_sync_manager", None)
        if sync_manager is not None:
            sync_manager.stop()
        self.three_d_controller.shutdown()
        event.accept()
