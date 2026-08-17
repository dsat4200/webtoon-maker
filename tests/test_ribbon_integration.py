from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize, Qt
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QDockWidget, QTabBar

from comic_editor.core.models import (
    BoundGeometry,
    ChapterDocument,
    RasterObject,
    VectorDrawingObject,
    VectorFillObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui import icons as icons_module
from comic_editor.ui.main_window import MainWindow, ResponsiveToolButton


def _vector_chapter():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(
        page.layer_id,
        "Container",
        BoundGeometry.rectangle(0, 0, 500, 500),
    )
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Lines")
    )
    fill = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            name="Region",
            geometry=BoundGeometry.rectangle(50, 50, 100, 100),
        ),
    )
    return chapter, page, layer, drawing, fill


def test_main_window_ribbon_is_contextual_and_tools_are_renamed(qapp):
    window = MainWindow()
    chapter, page, layer, drawing, fill = _vector_chapter()
    window._set_chapter(chapter, TileStore())
    try:
        assert window.tool_buttons[ToolKind.RASTER_PENCIL].text() == "Pencil"
        assert window.tool_buttons[ToolKind.RASTER_ERASER].text() == "Eraser"
        assert window.ribbon.page_keys() == [
            "tool_settings", "asset_library", "blender_views",
            "vector_tools", "gradient_tools",
        ]
        assert window.ribbon.orientation == Qt.Orientation.Vertical
        assert window.ribbon.tab_bar.shape() == QTabBar.Shape.RoundedEast
        assert window.color_tabs.count() == 2
        assert [
            window.color_tabs.tabText(index) for index in range(2)
        ] == ["Picker", "Palette"]
        assert window.ribbon.is_page_visible("vector_tools")
        assert window.ribbon.current_key() == "vector_tools"
        assert window.canvas.tool == ToolKind.RASTER_PENCIL

        window.canvas.set_selection("object", fill.object_id)
        assert not window.ribbon.is_page_visible("vector_tools")
        window.canvas.set_selection("object", drawing.object_id)
        assert window.ribbon.is_page_visible("vector_tools")
        assert window.ribbon.current_key() == "tool_settings"
    finally:
        window.deleteLater()


def test_workspace_splitters_resize_and_persist_collapsed_navigator(qapp):
    window = MainWindow()
    window.resize(1700, 900)
    window.show()
    qapp.processEvents()
    try:
        assert window.workspace_splitter.handleWidth() == 6
        assert window.sidebar_splitter.handleWidth() == 6
        assert window.tool_canvas_splitter.handleWidth() == 5
        assert window.outliner_splitter.handleWidth() == 5
        assert not window.navigator_panel.isExpanded()
        assert window.navigator_panel.width() == 24
        assert window.preview.width() == 92

        window.workspace_splitter.setSizes([320, 1100])
        window.sidebar_splitter.setSizes([360, 300])
        window.tool_canvas_splitter.setSizes([160, 940])
        window.outliner_splitter.setSizes([180, 420])
        window.navigator_panel.setExpanded(True)
        qapp.processEvents()

        assert window.workspace_splitter.sizes()[0] >= 300
        assert all(size > 0 for size in window.sidebar_splitter.sizes())
        assert window.tool_canvas_splitter.sizes()[0] >= 150
        assert window.navigator_panel.width() == 116
        assert window.preview.width() == 92

        window._capture_workspace_layout()
        assert window.settings.ui_splitter_sizes == {
            "sidebar_workspace": window.workspace_splitter.sizes(),
            "tools_colors": window.sidebar_splitter.sizes(),
            "tool_canvas": window.tool_canvas_splitter.sizes(),
            "outliner_settings": window.outliner_splitter.sizes(),
        }
        assert window.settings.navigator_expanded is True
    finally:
        window.deleteLater()


def test_bottom_left_color_picker_is_visible_and_responsive(qapp):
    window = MainWindow()
    window.show()
    qapp.processEvents()
    try:
        assert window.color_tabs.currentIndex() == 0
        assert window.color_panel.isVisibleTo(window)
        assert window.color_panel.picker.isVisibleTo(window)
        assert window.color_panel.picker.width() >= 170
        assert window.color_panel.picker.height() >= 170
        assert window.color_panel.hex_field.isVisibleTo(window)
        assert window.color_panel.hex_copy.isVisibleTo(window)
        assert window.color_panel.hex_paste.isVisibleTo(window)

        window.color_tabs.setCurrentIndex(1)
        qapp.processEvents()
        assert window.palette_editor.isVisibleTo(window)
    finally:
        window.deleteLater()


def test_project_view_controls_and_icon_tool_strip(qapp):
    window = MainWindow()
    window.resize(1800, 900)
    window.show()
    qapp.processEvents()
    try:
        hotkeys = window.file_toolbar.widgetForAction(window.hotkeys_action)
        assert hotkeys.x() < window.tablet_mode.x()
        assert window.tablet_mode.x() < window.reset_view_button.x()
        assert window.reset_view_button.x() < window.snap_grid.x()
        assert window.tablet_mode.text() == "Tablet Navigation"
        assert window.reset_view_button.text() == "Reset View"
        assert window.snap_grid.text() == "Snap to grid"

        expected = {
            ToolKind.OBJECT_SELECT: "Object Select",
            ToolKind.RASTER_PENCIL: "Pencil",
            ToolKind.RASTER_ERASER: "Eraser",
            ToolKind.TEXT_EDIT: "Text Edit",
            ToolKind.TRANSFORM: "Transform",
            ToolKind.SHAPE_EDIT: "Shape Edit",
            ToolKind.INSERT_PAGE_GAP: "Insert Page Gap",
        }
        for tool, label in expected.items():
            button = window.tool_buttons[tool]
            assert isinstance(button, ResponsiveToolButton)
            assert button.command_label == label
            assert button.toolTip() == label
            assert button.height() == 36
            assert button.minimumWidth() == 36
            assert button.iconSize() == QSize(20, 20)
            assert not button.icon().isNull()
            assert not button.icon().pixmap(
                QSize(20, 20), QIcon.Mode.Disabled, QIcon.State.Off
            ).isNull()

        narrow = window.tool_buttons[ToolKind.OBJECT_SELECT]
        narrow.resize(36, 36)
        assert narrow.toolButtonStyle() == Qt.ToolButtonStyle.ToolButtonIconOnly
        narrow.resize(220, 36)
        assert narrow.toolButtonStyle() == (
            Qt.ToolButtonStyle.ToolButtonTextBesideIcon
        )
        assert window.shape_tool_buttons[ToolKind.BOX_BOUND].toolTip() == (
            "Rectangle"
        )
        assert window.shape_tool_buttons[ToolKind.CIRCLE_BOUND].toolTip() == (
            "Circle"
        )
        assert window.shape_tool_buttons[ToolKind.SHAPE_CREATE].toolTip() == (
            "Free Shape"
        )
        assert window.add_page_button.toolTip() == "Add Page"
        assert not hasattr(window, "add_layer_button")
        assert not hasattr(window, "delete_button")
        assert window.hierarchy_dock.features() == (
            QDockWidget.DockWidgetFeature.NoDockWidgetFeatures
        )
        assert window.hierarchy_dock.titleBarWidget().height() == 0
        assert window.file_menu.menuAction() not in window.menuBar().actions()
    finally:
        window.deleteLater()


def test_vendored_iconoir_assets_cover_every_tool_strip_command():
    required = {
        "circle", "cursor-pointer", "curve-array", "design-pencil",
        "edit-pencil", "erase", "fill-color", "frame-select",
        "frame-tool", "media-image-plus", "nav-arrow-down",
        "nav-arrow-right", "page-plus", "path-arrow",
        "plus-square-dashed", "select-window", "selective-tool",
        "split-square-dashed", "text-square", "text",
    }
    root = Path(icons_module.__file__).with_name("icons") / "iconoir"
    available = {path.stem for path in root.glob("*.svg")}
    assert required <= available
    assert {"folder", "folder-plus", "trash", "nav-arrow-up", "eye", "eye-closed"} <= available
    assert "MIT License" in (root / "LICENSE").read_text(encoding="utf-8")
    for name in required:
        icon = icons_module.iconoir(name)
        assert not icon.isNull()
        assert not icon.pixmap(QSize(20, 20)).isNull()


def test_transform_mode_is_not_duplicated_in_ribbon_controls(qapp):
    window = MainWindow()
    chapter, _page, _layer, drawing, _fill = _vector_chapter()
    window._set_chapter(chapter, TileStore())
    try:
        window.settings.transform_mode = "free"
        window.tool_settings_controls.refresh()
        window.vector_tools_controls.refresh()
        assert window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        window._sync_contextual_ribbon()
        controls = window.tool_settings_controls
        assert controls.stack.currentWidget() is controls.empty_page
        assert not hasattr(controls, "selection_transform_mode")
        assert not hasattr(window.vector_tools_controls, "transform_mode")
        assert not hasattr(window.raster_object_controls, "transform_mode")
    finally:
        window.deleteLater()


def test_underlay_slider_coalesces_drag_and_restores_with_undo(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    raster = chapter.add_object(page.layer_id, RasterObject())
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", raster.object_id)
        window.selection_settings.refresh()
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.raster_page
        )
        assert window.ribbon.current_key() == "tool_settings"
        controls = window.raster_object_controls
        controls.underlay.sliderPressed.emit()
        controls.underlay.setValue(25)
        controls.underlay.setValue(65)
        controls.underlay.sliderReleased.emit()
        assert chapter.objects[raster.object_id].underlay_opacity == 0.65
        assert len(window.canvas.command_stack._undo) == 1

        window.canvas.command_stack.undo()
        assert (
            window.chapter.objects[raster.object_id].underlay_opacity == 0.0
        )
        window.canvas.command_stack.redo()
        assert (
            window.chapter.objects[raster.object_id].underlay_opacity == 0.65
        )
    finally:
        window.deleteLater()


def test_raster_object_ribbon_edits_properties_without_transform_mode(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    raster = chapter.add_object(page.layer_id, RasterObject(name="Ink"))
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", raster.object_id)
        controls = window.raster_object_controls
        assert controls.name.text() == "Ink"
        controls.name.setText("Pencils")
        controls.name.editingFinished.emit()
        assert chapter.objects[raster.object_id].name == "Pencils"
        assert window.canvas.command_stack.can_undo

        assert not hasattr(controls, "transform_mode")
        assert not window.tool_buttons[ToolKind.TRANSFORM].isVisible()
        assert not window.canvas.set_tool(ToolKind.TRANSFORM)
    finally:
        window.deleteLater()


def test_add_vector_from_fill_is_anchored_above_owner(qapp):
    window = MainWindow()
    chapter, page, layer, drawing, fill = _vector_chapter()
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", fill.object_id)
        owner_index = next(
            index for index, child in enumerate(layer.children)
            if child.entity_id == drawing.object_id
        )
        before_ids = set(chapter.objects)
        window._add_vector_drawing()
        created = [
            obj for object_id, obj in chapter.objects.items()
            if object_id not in before_ids
        ]
        assert len(created) == 1
        assert isinstance(created[0], VectorDrawingObject)
        assert layer.children[owner_index].entity_id == created[0].object_id
        assert layer.children[owner_index + 1].entity_id == drawing.object_id
        assert window.canvas.selected_id == created[0].object_id
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
    finally:
        window.deleteLater()


def test_vector_branch_stays_open_in_subtree_and_collapses_on_leave(qapp):
    window = MainWindow()
    chapter, page, layer, drawing, fill = _vector_chapter()
    window._set_chapter(chapter, TileStore())
    try:
        drawing_index = window.hierarchy_model.index_for_entity(
            "object", drawing.object_id
        )
        assert window.tree.isExpanded(drawing_index)
        window.canvas.set_selection("object", fill.object_id)
        drawing_index = window.hierarchy_model.index_for_entity(
            "object", drawing.object_id
        )
        assert window.tree.isExpanded(drawing_index)

        window.hierarchy_model.rebuild()
        drawing_index = window.hierarchy_model.index_for_entity(
            "object", drawing.object_id
        )
        assert window.tree.isExpanded(drawing_index)

        window.canvas.set_selection("layer", layer.layer_id)
        drawing_index = window.hierarchy_model.index_for_entity(
            "object", drawing.object_id
        )
        assert not window.tree.isExpanded(drawing_index)
    finally:
        window.deleteLater()


def test_series_colors_and_palette_mutations_autosave(qapp, tmp_path):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Palette Test")
    series.primary_color = "#80112233"
    series.secondary_color = "#40AABBCC"
    repository.save_series(series)
    window = MainWindow()
    try:
        window._adopt_series(repository, series)
        assert window.color_panel.primary_color() == "#80112233"
        assert window.color_panel.secondary_color() == "#40AABBCC"
        assert window.canvas.primary_color == "#80112233"
        assert window.canvas.secondary_color == "#40AABBCC"

        window.color_panel.apply_color("#C0102030")
        window._flush_series_preferences()
        restored = repository.load_series()
        assert restored.primary_color == "#C0102030"

        old_count = len(series.palettes)
        window._add_palette()
        assert len(series.palettes) == old_count + 1
        palette = series.palettes[-1]
        window._add_palette_swatch(palette.palette_id, "#4000FF00")
        restored = repository.load_series()
        assert restored.palettes[-1].swatches[-1].color == "#4000FF00"
    finally:
        window.deleteLater()


def test_vector_redraw_controls_restore_parameter_ranges(qapp):
    window = MainWindow()
    try:
        controls = window.vector_tools_controls
        window.settings.vector_redraw_parameter = "opacity"
        window.settings.vector_redraw_opacity_max = 100
        controls.refresh()
        assert controls.redraw_maximum.value() == 100

        window.settings.vector_redraw_parameter = "thickness"
        window.settings.vector_redraw_thickness_max = 1000
        controls.refresh()
        assert controls.redraw_maximum.value() == 40
        assert controls.redraw_maximum_slider.maximum() == 40
    finally:
        window.deleteLater()
