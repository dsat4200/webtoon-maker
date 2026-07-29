from __future__ import annotations

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, ShapeStyle,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.main_window import MainWindow


def _window_document(window):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    layer = chapter.add_layer(
        page.layer_id, "Panel",
        BoundGeometry.rectangle(100, 100, 500, 400),
    )
    raster = chapter.add_object(layer.layer_id, RasterObject(name="Ink"))
    window.chapter = chapter
    window.canvas.set_document(chapter, TileStore())
    window._refresh_hierarchy()
    return chapter, page, layer, raster


def test_layer_settings_follows_layer_page_and_object_parent(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    window.show()
    qapp.processEvents()
    try:
        window.canvas.set_selection("layer", layer.layer_id, False)
        window.layer_settings.refresh()
        assert window.layer_settings.type_label.text() == "Rectangle Layer"
        assert window.layer_settings.name.text() == "Panel"
        assert window.inspector.isHidden()

        window.canvas.set_selection("object", raster.object_id)
        qapp.processEvents()
        assert window.canvas.active_layer_id == layer.layer_id
        assert window.layer_settings.name.text() == "Panel"
        assert window.inspector.isVisible()

        window.canvas.set_selection("layer", page.layer_id, False)
        qapp.processEvents()
        assert window.layer_settings.type_label.text() == "Page"
        assert window.inspector.isHidden()
    finally:
        window.hide()
        window.deleteLater()


def test_layer_settings_kind_rows_and_position_above_tree(qapp, monkeypatch):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    fill = chapter.add_fill_layer(layer.layer_id, "Fill", "#ff0000")
    open_shape = chapter.add_layer(
        page.layer_id, "Line",
        BoundGeometry.path([
            layer.bound.nodes[0], layer.bound.nodes[1],
        ], False),
        layer_kind="open_shape",
        style=ShapeStyle(primary_color="#000000", base_thickness=9),
    )
    window._refresh_hierarchy()
    layout = window.hierarchy_dock.widget().layout()
    assert layout.indexOf(window.layer_settings) < layout.indexOf(window.tree)
    try:
        window.canvas.set_selection("layer", fill.layer_id, False)
        window.layer_settings.refresh()
        assert window.layer_settings.type_label.text() == "Fill Layer"
        assert window.layer_settings.border_width.isHidden()
        assert window.layer_settings.grid_size.isHidden()

        window.canvas.set_selection("layer", open_shape.layer_id, False)
        window.layer_settings.refresh()
        assert window.layer_settings.type_label.text() == "Open Shape"
        assert not window.layer_settings.base_thickness.isHidden()
        assert window.layer_settings.fill_enabled.text() == "Stroke"
    finally:
        window.deleteLater()


def test_rectangle_mode_setting_is_global_and_not_document_undo(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    window.canvas.set_selection("layer", layer.layer_id, False)
    window.layer_settings.refresh()
    before = chapter.to_dict()
    try:
        index = window.layer_settings.rectangle_mode.findData("free")
        window.layer_settings.rectangle_mode.setCurrentIndex(index)
        assert window.settings.rectangle_edit_mode == "free"
        assert chapter.to_dict() == before
        assert not window.canvas.command_stack.can_undo
    finally:
        window.deleteLater()


def test_layer_settings_preserves_all_bounded_layer_fields(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    window.canvas.set_selection("layer", layer.layer_id, False)
    panel = window.layer_settings
    panel.refresh()
    tool = window.canvas.tool
    try:
        panel._updating = True
        panel.name.setText("Renamed")
        panel.visible.setChecked(False)
        panel.opacity.setValue(45)
        panel.fill_enabled.setChecked(True)
        panel.fill_color.setProperty("color", "#123456")
        panel.border_width.setValue(7)
        panel.border_color.setProperty("color", "#abcdef")
        panel.grid_override.setChecked(True)
        panel.grid_size.setValue(60)
        panel.grid_divisions.setValue(3)
        panel._updating = False
        panel._apply()
        assert layer.name == "Renamed"
        assert layer.visible is False
        assert layer.opacity == 0.45
        assert layer.fill_color == "#123456"
        assert layer.border_width == 7
        assert layer.border_color == "#abcdef"
        assert layer.grid_override.size == 60
        assert layer.grid_override.divisions == 3
        assert window.canvas.tool == tool
        assert window.canvas.command_stack.can_undo

        window.canvas._selected_shape_node_id = layer.bound.nodes[0].node_id
        panel.refresh()
        assert not panel.point_width.isHidden()
        assert not panel.point_roundness.isHidden()
    finally:
        window.deleteLater()


def test_raster_tool_controls_live_in_object_popup(qapp, monkeypatch):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    window.show()
    window.canvas.set_selection("object", raster.object_id)
    qapp.processEvents()
    before = chapter.to_dict()
    try:
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert window.inspector.raster_tool_panel.isVisible()
        assert window.inspector.pencil_tool_controls.isVisible()
        assert window.inspector.eraser_shape.isHidden()
        assert not hasattr(window, "pencil_preset_combo")
        assert not hasattr(window, "pencil_size_combo")
        assert not hasattr(window, "eraser_size_combo")

        large = window.inspector.brush_size_combo.findData("large")
        window.inspector.brush_size_combo.setCurrentIndex(large)
        assert window.settings.active_pencil_size == "large"
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert chapter.to_dict() == before

        window._activate_tool(ToolKind.RASTER_ERASER)
        qapp.processEvents()
        assert window.inspector.raster_tool_panel.isVisible()
        assert window.inspector.pencil_tool_controls.isHidden()
        assert window.inspector.eraser_shape.isVisible()
        square = window.inspector.eraser_shape.findData(True)
        window.inspector.eraser_shape.setCurrentIndex(square)
        assert window.settings.eraser_square is True
        assert window.canvas.tool == ToolKind.RASTER_ERASER
        assert chapter.to_dict() == before

        window._activate_tool(ToolKind.TRANSFORM)
        qapp.processEvents()
        assert window.inspector.raster_tool_panel.isHidden()
        assert window.inspector.isVisible()
    finally:
        window.hide()
        window.deleteLater()
