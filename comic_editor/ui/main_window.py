"""Standalone series/chapter application shell."""
from __future__ import annotations

import copy
from datetime import datetime
import re
import time
import math
from pathlib import Path

from PySide6.QtCore import (
    QCoreApplication, QEvent, QItemSelection, QItemSelectionModel, QModelIndex,
    QBuffer, QByteArray, QIODevice, QPointF, QRectF, QSignalBlocker, QSize,
    QTimer, Qt,
    Signal,
)
from PySide6.QtGui import (
    QAction, QCloseEvent, QCursor, QImage, QImageReader, QKeySequence,
    QMouseEvent, QShortcut,
)
from PySide6.QtWidgets import (
    QAbstractSpinBox, QApplication, QCheckBox, QComboBox, QDockWidget,
    QFileDialog, QHBoxLayout,
    QDialog, QDialogButtonBox, QFormLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMainWindow, QMenu, QMessageBox, QPlainTextEdit, QPushButton,
    QScrollArea, QSizePolicy, QSpinBox, QSplitter, QTabBar, QTabWidget, QTextEdit,
    QToolBar, QToolButton, QToolTip, QTreeView, QVBoxLayout, QWidget, QFrame,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientRampPreset, ColorGradientStop,
    ColorPalette, GradientObject, PaletteSwatch, PathNode,
    BlenderComicViewSourceDescriptor, EmbeddedImageSourceDescriptor,
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
    ColorHistoryWidget, PaletteEditorWidget, PrimarySecondaryColorPanel,
    canonical_argb,
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
from comic_editor.ui.blender_views import BlenderViewsWidget
from comic_editor.ui.sessions import EditorSession, ProjectContext
from comic_editor.ui.windows_input import tablet_multitouch_native_result
from comic_editor.integrations.blender_controller import (
    BlenderImageSourceController,
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
        self._blender_relink_object_id = ""
        self._build_ui()
        self.blender_sources = BlenderImageSourceController(self.canvas, self)
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

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if event.type() == QEvent.Type.WindowStateChange and hasattr(
            self, "blender_sources"
        ):
            was_minimized = bool(event.oldState() & Qt.WindowState.WindowMinimized)
            is_minimized = self.isMinimized()
            if was_minimized == is_minimized:
                return
            if is_minimized:
                self.blender_sources.stop_for_context_change()
            else:
                self.blender_sources.resume_for_context()

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
        self.export_png_action = self.file_menu.addAction("Export PNG")
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
        self.export_png_toolbar_action = self.file_toolbar.addAction("Export PNG")
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
        self.page_scope = QCheckBox("Select in page")
        self.page_scope.setChecked(self.settings.page_scope_select)
        # Kept as an attribute for compatibility with older integrations;
        # entity selection now always searches the complete chapter.
        self.page_scope.hide()
        self.color_tabs = QTabWidget(self)
        self.color_tabs.setObjectName("colorTabs")
        self.color_tabs.setMinimumHeight(236)
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

        history_page = QWidget(self.color_tabs)
        history_layout = QVBoxLayout(history_page)
        history_layout.setContentsMargins(4, 4, 4, 4)
        self.color_history = ColorHistoryWidget(history_page)
        history_layout.addWidget(self.color_history)
        self.color_tabs.addTab(history_page, "History")

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

        self.blender_views_page = self.ribbon.add_page(
            "blender_views", "Blender Views"
        )
        blender_group = self.blender_views_page.add_group(
            "", minimum_width=760
        )
        blender_group.title_label.hide()
        blender_group.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.blender_views_page.groups_layout.setStretch(0, 1)
        self.blender_views_page.groups_layout.setStretch(
            self.blender_views_page.groups_layout.count() - 1, 0
        )
        self.blender_views_widget = BlenderViewsWidget(self.ribbon)
        self.blender_views_widget.setMinimumHeight(220)
        self.blender_views_widget.setSizePolicy(
            QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding
        )
        self.blender_views_widget.set_endpoint(
            self.settings.blender_bridge_host,
            self.settings.blender_bridge_port,
            self.settings.blender_bridge_token,
        )
        blender_group.add_widget(self.blender_views_widget)

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
        self.export_png_action.triggered.connect(self._export_png)
        self.export_png_toolbar_action.triggered.connect(self._export_png)
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
        self.canvas.chapterReplaced.connect(self._chapter_replaced)
        self.canvas.toolChanged.connect(self._canvas_tool_changed)
        self.canvas.colorSampled.connect(self._eyedropper_preview)
        self.canvas.colorSampleCommitted.connect(self._eyedropper_commit)
        self.canvas.eyedropperGestureChanged.connect(
            self._eyedropper_gesture_changed
        )
        self.canvas.interactionFinished.connect(self.selection_common.refresh)
        self.canvas.interactionFinished.connect(self.selection_settings.refresh)
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
        self.blender_views_widget.connectRequested.connect(
            self._connect_blender_bridge
        )
        self.blender_views_widget.disconnectRequested.connect(
            self.blender_sources.disconnect
        )
        self.blender_views_widget.refreshRequested.connect(
            self.blender_sources.client.refresh_views
        )
        self.blender_views_widget.addRequested.connect(
            self._add_blender_comic_view
        )
        self.blender_sources.viewsChanged.connect(
            self.blender_views_widget.set_views
        )
        self.blender_sources.connectionStateChanged.connect(
            self.blender_views_widget.set_connection_state
        )
        self.blender_sources.connectionStateChanged.connect(
            self._blender_connection_changed
        )
        self.blender_sources.streamStatusChanged.connect(
            self._blender_stream_status_changed
        )
        self.blender_sources.switchDecisionRequired.connect(
            self._resolve_blender_dirty_switch
        )
        self.blender_sources.cachePersisted.connect(
            lambda: self._mark_dirty(None)
        )
        self.blender_sources.errorOccurred.connect(
            self._blender_source_error
        )
        self.canvas.selectionChanged.connect(
            self.blender_sources.handle_selection
        )
        self.selection_settings.image_controls.renderOnceRequested.connect(
            self.blender_sources.render_once
        )
        self.selection_settings.image_controls.reconnectRequested.connect(
            self._reconnect_selected_blender_source
        )
        self.selection_settings.image_controls.relinkRequested.connect(
            self._begin_relink_selected_blender_source
        )
        self.selection_settings.image_controls.detachRequested.connect(
            self._detach_selected_blender_source
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
        self.color_panel.colorCommitted.connect(self._record_color_history)
        self.color_panel.eyedropperRequested.connect(
            lambda: self._activate_tool(ToolKind.EYEDROPPER)
        )
        self.color_history.colorActivated.connect(
            self._history_color_activated
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
            "eyedropper": ToolKind.EYEDROPPER,
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
            "reset_rotation": self.canvas.reset_rotation,
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
        if (
            getattr(self, "_eyedropper_pointer_active", False)
            and self._hotkey_active_hold is not None
            and self._hotkey_active_hold["target"] == ToolKind.EYEDROPPER
        ):
            self._eyedropper_restore_pending = True
            return
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
            self._send_outliner_mouse(
                QEvent.MouseButtonPress, global_position,
                Qt.LeftButton, Qt.LeftButton,
                event.modifiers(), event.pointingDevice(),
            )
            self._tablet_outliner_press = {
                "global": QPointF(global_position),
                "forwarded": True,
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
            self._send_outliner_mouse(
                QEvent.MouseMove, global_position,
                Qt.NoButton, Qt.LeftButton,
                event.modifiers(), state["device"],
            )
            event.accept()
            return True
        self._send_outliner_mouse(
            QEvent.MouseButtonRelease, global_position,
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
        self.blender_sources.flush_pending_frames()
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
        self._blender_relink_object_id = ""
        self.blender_views_widget.set_relink_mode(False)
        self.blender_sources.stop_for_context_change()
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
            self.statusBar().showMessage(
                f"{session.name} — {self.chapter.width} × {self.chapter.height}px"
            )
        finally:
            self._switching_session = False
            self.blender_sources.resume_for_context()

    def _clear_active_session(self) -> None:
        if self.project_tabs.count() > 0:
            return
        self.active_session = None
        self.repository = None
        self.series = None
        self.chapter = None
        self._dirty = False
        self._blender_relink_object_id = ""
        self.blender_views_widget.set_relink_mode(False)
        self.blender_sources.stop_for_context_change()
        self.canvas.asset_repository = None
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
                    session.chapter, session.tiles, session.images
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
            chapter, tiles, images = repository.load_chapter(
                chapter_id, recover=recover, include_images=True
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
            chapter, tiles, images = self.repository.load_chapter(
                chapter_id, recover=recover, include_images=True
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
        self._set_chapter(chapter, tiles, images)
        if recover or load_warnings:
            self._mark_dirty(None)

    def _set_chapter(
        self, chapter, tiles, images: ImageStore | None = None,
    ) -> None:
        self.chapter = chapter
        images = images or ImageStore()
        self.canvas.set_document(chapter, tiles, images)
        if self.active_session is not None:
            self.active_session.chapter = chapter
            self.active_session.tiles = tiles
            self.active_session.images = images
            self.active_session.canvas_state = None
        self.hierarchy_model.set_chapter(chapter)
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

    def _delete_selected(self) -> None:
        if self.chapter is None or not self.canvas.selected_id:
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
            isinstance(
                selected_object,
                (RasterObject, VectorDrawingObject, ImageObject),
            )
            or (
                text_selected
                and self.chapter.objects[self.canvas.selected_id].layout_mode
                == "free"
            )
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
        selected_image = (
            self.chapter.objects.get(item.entity_id)
            if item.kind == "object" else None
        )
        can_freeze = not (
            isinstance(selected_image, ImageObject)
            and selected_image.is_blender_linked
            and self.canvas.images.source(selected_image.object_id) is None
            and self.canvas.images.runtime_frame(selected_image.object_id).isNull()
        )
        copy_asset.setEnabled(
            self._current_project_context() is not None and can_freeze
        )
        if rasterize is not None:
            rasterize.setEnabled(can_freeze)
        selected = menu.exec(self.tree.viewport().mapToGlobal(point))
        if selected is rename:
            self.tree.edit(index)
        elif selected is copy_asset:
            self._copy_selected_as_asset(item.kind, item.entity_id)
        elif rasterize is not None and selected is rasterize:
            self._rasterize_image(item.entity_id)

    def _copy_selected_as_asset(self, kind: str, entity_id: str) -> None:
        context = self._current_project_context()
        if context is None or self.chapter is None:
            return
        self.blender_sources.flush_pending_frames()
        entity = (
            self.chapter.layers.get(entity_id)
            if kind == "layer" else self.chapter.objects.get(entity_id)
        )
        if entity is None:
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
        if obj.is_blender_linked:
            self.blender_sources.flush_pending_frames()
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

    # ---- Blender Comic View image sources ----------------------------
    def _connect_blender_bridge(
        self, host: str, port: int, token: str,
    ) -> None:
        self.settings.blender_bridge_host = host
        self.settings.blender_bridge_port = int(port)
        self.settings.blender_bridge_token = token
        self.settings.clamp()
        save_settings(self.settings)
        self.blender_sources.connect_to_provider(
            self.settings.blender_bridge_host,
            self.settings.blender_bridge_port,
            self.settings.blender_bridge_token,
        )

    def _add_blender_comic_view(self, view) -> None:
        if self._blender_relink_object_id:
            object_id, self._blender_relink_object_id = (
                self._blender_relink_object_id, ""
            )
            self.blender_views_widget.set_relink_mode(False)
            obj = (
                self.chapter.objects.get(object_id)
                if self.chapter is not None else None
            )
            if not isinstance(obj, ImageObject) or not obj.is_blender_linked:
                self.statusBar().showMessage(
                    "The image selected for relinking is no longer available", 5000
                )
                return
            before = self.chapter.to_dict()
            obj.source = BlenderComicViewSourceDescriptor(
                project_uuid=view.project_uuid,
                view_uuid=view.view_uuid,
                display_name=view.name,
                last_revision=view.revision,
            )
            obj.sync_source_metadata()
            after = self.chapter.to_dict()
            self.canvas.push_model_change(
                before, after, "Relink Blender Comic View"
            )
            self.canvas.documentChanged.emit(None)
            self.selection_settings.refresh()
            self.blender_sources.handle_selection()
            self.statusBar().showMessage(
                f"Relinked image to Blender Comic View {view.name}", 5000
            )
            return
        if self.chapter is None or (
            self.active_session is not None
            and self.active_session.kind == "asset"
        ):
            self.statusBar().showMessage(
                "Blender Comic Views can be added only to a chapter", 5000
            )
            return
        parent = self._selected_parent_layer(allow_page=True)
        if parent is None and self.canvas.active_page_id in self.chapter.layers:
            parent = self.chapter.layers[self.canvas.active_page_id]
        if parent is None:
            self.statusBar().showMessage(
                "Select a page or container before adding a Comic View", 5000
            )
            return
        thumbnail = QImage(view.thumbnail)
        if thumbnail.isNull():
            thumbnail = QImage(256, 144, QImage.Format_ARGB32_Premultiplied)
            thumbnail.fill(Qt.transparent)
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        saved = thumbnail.save(buffer, "PNG")
        buffer.close()
        if not saved:
            self.statusBar().showMessage("Unable to encode the Comic View thumbnail", 5000)
            return
        descriptor = BlenderComicViewSourceDescriptor(
            project_uuid=view.project_uuid,
            view_uuid=view.view_uuid,
            display_name=view.name,
            last_revision=view.revision,
        )
        created = self.canvas.place_image_sources(
            [(f"{view.name}.png", "image/png", bytes(payload))],
            parent.layer_id,
            self._selected_parent_center(parent.layer_id),
            insertion_index=self._new_object_insertion_index(parent.layer_id),
            fit_parent=False,
            label="Add Blender Comic View",
            source_descriptors=[descriptor],
            logical_sizes=[(view.width, view.height)],
        )
        if created:
            self.statusBar().showMessage(
                f"Added Blender Comic View {view.name}", 3500
            )
            self.blender_sources.handle_selection()

    def _blender_connection_changed(self, state: str) -> None:
        if state == "connected":
            self.statusBar().showMessage("Connected to Blender Comic Views", 3000)
        self._blender_stream_status_changed(
            "offline" if state != "connected" else "connected"
        )

    def _blender_stream_status_changed(self, status: str) -> None:
        labels = {
            "live": "Ready — Update publishes; Render Once shows a temporary preview",
            "preview": "Preview — temporary unsaved Blender scene (not cached)",
            "activating": "Activating the selected Comic View in Blender…",
            "connected": "Connected — select a linked image to stream it",
            "stopped": "Frozen — showing the last cached frame",
            "frozen": "Frozen — activation was canceled",
            "offline": "Offline — showing the last cached frame",
            "unavailable": "Offline — this Comic View is unavailable; use Relink to choose another",
            "stale": "Stale — Blender has an older revision; update or relink before streaming",
            "error": "Error — showing the last cached frame",
        }
        self.selection_settings.image_controls.set_source_status(
            labels.get(status, str(status).title())
        )

    def _resolve_blender_dirty_switch(self, message: dict) -> None:
        current = str(message.get("current_name", "the current Comic View"))
        destination = str(message.get("destination_name", "the selected view"))
        box = QMessageBox(self)
        box.setWindowTitle("Comic View has unsaved changes")
        box.setText(
            f'"{current}" has scene changes that have not been stored. '
            f'Switch to "{destination}"?'
        )
        update = box.addButton("Update and Switch", QMessageBox.AcceptRole)
        revert = box.addButton("Revert and Switch", QMessageBox.DestructiveRole)
        cancel = box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(update)
        box.exec()
        clicked = box.clickedButton()
        resolution = (
            "update" if clicked is update else
            "revert" if clicked is revert else "cancel"
        )
        self.blender_sources.client.resolve_dirty_switch(resolution)
        if clicked is cancel:
            self._blender_stream_status_changed("frozen")

    def _blender_source_error(self, message: str) -> None:
        self.blender_views_widget.set_status_message(message)
        self.statusBar().showMessage(f"Blender source: {message}", 7000)
        self._blender_stream_status_changed("error")

    def _reconnect_selected_blender_source(self) -> None:
        if self.blender_sources.client.connected:
            self.blender_sources.reconnect_selected()
            return
        self._connect_blender_bridge(
            self.settings.blender_bridge_host,
            self.settings.blender_bridge_port,
            self.settings.blender_bridge_token,
        )

    def _begin_relink_selected_blender_source(self) -> None:
        obj = self.blender_sources.selected_linked_object()
        if obj is None:
            return
        self._blender_relink_object_id = obj.object_id
        self.blender_views_widget.set_relink_mode(True, obj.name)
        self.ribbon.select_page("blender_views")
        self.statusBar().showMessage(
            "Choose a Blender Comic View to relink the selected image", 5000
        )

    def _detach_selected_blender_source(self) -> None:
        obj = self.blender_sources.selected_linked_object()
        if obj is None or self.chapter is None:
            return
        self.blender_sources.flush_pending_frames()
        if self.canvas.images.source(obj.object_id) is None:
            QMessageBox.warning(
                self, "Detach Blender source",
                "This Comic View has no cached frame to preserve.",
            )
            return
        before = self.chapter.to_dict()
        display_name = obj.source.display_name
        obj.source = EmbeddedImageSourceDescriptor(
            filename=f"{display_name}.png", mime_type="image/png"
        )
        obj.sync_source_metadata()
        self.canvas.images.relabel(
            obj.object_id, obj.source_filename, obj.source_mime_type
        )
        self.canvas.images.clear_runtime_frame(obj.object_id)
        after = self.chapter.to_dict()
        self.canvas.push_model_change(
            before, after, "Detach Blender image source"
        )
        self.canvas.documentChanged.emit(None)
        self.selection_settings.refresh()
        self.blender_sources.handle_selection()
        self.statusBar().showMessage(
            "Detached Blender source; the cached frame is now an embedded image",
            5000,
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
        if (
            self._blender_relink_object_id
            and entity_id != self._blender_relink_object_id
        ):
            self._blender_relink_object_id = ""
            self.blender_views_widget.set_relink_mode(False)
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
        self.color_panel.eyedropper.blockSignals(True)
        self.color_panel.eyedropper.setChecked(
            tool == ToolKind.EYEDROPPER
        )
        self.color_panel.eyedropper.blockSignals(False)
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
            history = []
        else:
            self.series.validate()
            primary = canonical_argb(self.series.primary_color)
            secondary = canonical_argb(
                self.series.secondary_color, "#FFFFFFFF"
            )
            palettes = self.series.palettes
            active_palette = self.series.active_palette_id
            history = list(self.series.color_history)
        self.color_panel.set_colors(primary, secondary, emit=False)
        self.palette_editor.set_palettes(
            palettes, active_palette, emit=False
        )
        self.palette_editor.set_new_swatch_color(
            self.color_panel.active_color()
        )
        self.color_history.set_colors(history)
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
        self._record_color_history(color)

    def _record_color_history(self, color: str) -> None:
        if self.series is None:
            return
        color = canonical_argb(color)
        self.series.color_history = [
            color,
            *(
                item for item in self.series.color_history
                if canonical_argb(item) != color
            ),
        ][:24]
        self.color_history.set_colors(self.series.color_history)
        self._schedule_series_preferences_save()

    def _history_color_activated(self, color: str) -> None:
        self.color_panel.apply_color(color, emit=True)
        self._record_color_history(color)

    def _eyedropper_preview(self, color: str) -> None:
        self.color_panel.apply_color(color, emit=True)

    def _eyedropper_commit(self, color: str) -> None:
        self.color_panel.apply_color(color, emit=True)
        self._record_color_history(color)

    def _eyedropper_gesture_changed(self, active: bool) -> None:
        self._eyedropper_pointer_active = bool(active)
        if not active and getattr(self, "_eyedropper_restore_pending", False):
            self._eyedropper_restore_pending = False
            self._restore_active_hotkey_tool()

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
        self.blender_sources.flush_pending_frames()
        try:
            self.repository.save_chapter(
                self.chapter, self.canvas.tiles, self.canvas.images
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
            repository.save_chapter(chapter, tiles, images)
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
        self.blender_sources.flush_pending_frames()
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

    def _export_png(self) -> None:
        if (
            self.chapter is None or self.repository is None
            or self.series is None
            or (self.active_session is not None
                and self.active_session.kind != "series")
        ):
            return
        exports = self.repository.root / "exports"
        raw_name = self.chapter_combo.currentText().strip() or "Chapter"
        safe_name = re.sub(r'[<>:"/\\|?*\x00-\x1f]+', "-", raw_name)
        safe_name = safe_name.strip(" .-") or "Chapter"
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        base = f"{safe_name}-{stamp}"
        try:
            exports.mkdir(parents=True, exist_ok=True)
            destination = exports / f"{base}.png"
            suffix = 2
            while destination.exists():
                destination = exports / f"{base}-{suffix}.png"
                suffix += 1
            image = QImage(
                int(self.chapter.width), int(self.chapter.height),
                QImage.Format.Format_ARGB32_Premultiplied,
            )
            if image.isNull():
                raise MemoryError("could not allocate the chapter image")
            self.canvas.render_preview(image)
            temporary = destination.with_name(f".{destination.name}.tmp.png")
            try:
                if not image.save(str(temporary), "PNG"):
                    raise OSError("Qt could not encode the PNG")
                temporary.replace(destination)
            finally:
                if temporary.exists():
                    temporary.unlink(missing_ok=True)
        except (MemoryError, OSError, ValueError) as error:
            QMessageBox.critical(
                self, "Export PNG", f"Unable to export the chapter:\n{error}"
            )
            return
        self.statusBar().showMessage(f"Exported {destination.name}", 7000)

    def _refresh_actions(self) -> None:
        active = self.chapter is not None
        series_active = active and (
            self.active_session is None or self.active_session.kind == "series"
        )
        self.save_action.setEnabled(active and self._dirty)
        self.save_as_action.setEnabled(self._current_project_context() is not None)
        self.new_chapter_action.setEnabled(series_active and self.series is not None)
        self.trim_action.setEnabled(series_active)
        self.export_png_action.setEnabled(series_active)
        self.export_png_toolbar_action.setEnabled(series_active)
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
        self.blender_sources.shutdown()
        self._flush_series_preferences()
        self.layout_settings_timer.stop()
        self._save_workspace_layout()
        if getattr(self, "_application_event_filter_installed", False):
            application = QApplication.instance()
            if application is not None:
                application.removeEventFilter(self)
            self._application_event_filter_installed = False
        event.accept()
