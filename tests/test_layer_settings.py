from __future__ import annotations

from PySide6.QtCore import QObject, Signal
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QScrollArea, QSlider, QSpinBox

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, PathNode,
    RasterObject, ShapeStyle, TextObject, VectorDrawingObject,
    VectorFillObject,
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
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.layer_page
        )

        window.canvas.set_selection("object", raster.object_id)
        qapp.processEvents()
        assert window.canvas.active_layer_id == layer.layer_id
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.raster_page
        )
        assert window.raster_object_controls.name.text() == "Ink"
        assert "raster_object_settings" not in window.ribbon.page_keys()

        window.canvas.set_selection("layer", page.layer_id, False)
        qapp.processEvents()
        assert window.layer_settings.type_label.text() == "Page"
        assert window.layer_settings.border_width.maximum() == 40
        assert window.layer_settings.border_width_slider.maximum() == 40

        window.canvas.set_selection("layer", layer.layer_id, False)
        qapp.processEvents()
        assert window.layer_settings.border_width.maximum() == 500
        assert window.layer_settings.border_width_slider.maximum() == 500
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
    assert layout.indexOf(window.outliner_splitter) >= 0
    settings_host = window.outliner_splitter.widget(0)
    assert settings_host.layout().indexOf(window.settings_scroll) >= 0
    assert settings_host.layout().indexOf(window.selection_common) >= 0
    assert settings_host.layout().indexOf(window.settings_scroll) < settings_host.layout().indexOf(window.selection_common)
    assert isinstance(window.settings_scroll, QScrollArea)
    assert window.settings_scroll.widget() is window.selection_settings
    assert window.outliner_splitter.widget(1) is window.tree
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


def test_thickness_sliders_use_integers_sync_and_coalesce_undo(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    layer.layer_kind = "open_shape"
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100), PathNode(x=500, y=300),
    ], False)
    window.canvas.set_selection("layer", layer.layer_id, False)
    panel = window.layer_settings
    panel.refresh()
    window.canvas.command_stack.clear()
    try:
        assert isinstance(panel.base_thickness_slider, QSlider)
        assert isinstance(panel.base_thickness, QSpinBox)
        assert isinstance(panel.border_width_slider, QSlider)
        assert isinstance(panel.border_width, QSpinBox)

        panel.base_thickness_slider.sliderPressed.emit()
        panel.base_thickness_slider.setValue(12)
        panel.base_thickness_slider.setValue(27)
        panel.base_thickness_slider.setValue(43)
        panel.base_thickness_slider.sliderReleased.emit()

        assert panel.base_thickness.value() == 43
        assert layer.shape_style.base_thickness == 43
        assert len(window.canvas.command_stack._undo) == 1

        panel.border_width.setValue(19)
        assert panel.border_width_slider.value() == 19
        assert layer.shape_style.outline_thickness == 19
    finally:
        window.deleteLater()


def test_raster_tool_controls_live_in_tool_settings_ribbon(qapp, monkeypatch):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, layer, raster = _window_document(window)
    window.show()
    window.canvas.set_selection("object", raster.object_id)
    qapp.processEvents()
    before = chapter.to_dict()
    try:
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.raster_page
        )
        assert "raster_object_settings" not in window.ribbon.page_keys()
        controls = window.tool_settings_controls
        assert controls.stack.currentWidget() is controls.pencil_page

        large = controls.pencil_size.findData("large")
        controls.pencil_size.setCurrentIndex(large)
        assert window.settings.active_pencil_size == "large"
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert chapter.to_dict() == before

        window._activate_tool(ToolKind.RASTER_ERASER)
        qapp.processEvents()
        assert controls.stack.currentWidget() is controls.eraser_page
        square = controls.eraser_shape.findData(True)
        controls.eraser_shape.setCurrentIndex(square)
        assert window.settings.eraser_square is True
        assert window.canvas.tool == ToolKind.RASTER_ERASER
        assert chapter.to_dict() == before

        window._activate_tool(ToolKind.TRANSFORM)
        qapp.processEvents()
        assert window.canvas.tool == ToolKind.TRANSFORM
        assert window.ribbon.current_key() == "tool_settings"
    finally:
        window.hide()
        window.deleteLater()


def test_pencil_tool_settings_remains_selected_across_context_refreshes(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, layer, raster = _window_document(window)
    vector = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Vector Ink")
    )
    window._refresh_hierarchy()
    window.show()
    try:
        # Re-selecting a raster while Pencil is already active must still
        # expose Tool Settings; Canvas does not emit toolChanged in that case.
        window.canvas.set_selection("object", raster.object_id)
        qapp.processEvents()
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert window.ribbon.current_key() == "tool_settings"
        controls = window.tool_settings_controls
        assert controls.stack.currentWidget() is controls.pencil_page
        assert controls.pencil_presets_button.isVisible()
        assert controls.pencil_presets_button.isEnabled()

        # Raster properties now stay in the selection-settings pane.
        assert "raster_object_settings" not in window.ribbon.page_keys()
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.raster_page
        )
        window._sync_contextual_ribbon()
        assert window.ribbon.current_key() == "tool_settings"

        # Vector Pencil uses the same Tool Settings page and must not be
        # replaced by Vector Tools after its default tool is selected.
        window.canvas.set_selection("object", vector.object_id)
        qapp.processEvents()
        assert window.canvas.tool == ToolKind.RASTER_PENCIL
        assert window.ribbon.is_page_visible("vector_tools")
        assert window.ribbon.current_key() == "tool_settings"
        assert controls.stack.currentWidget() is controls.pencil_page

        window.ribbon.tab_bar.setCurrentIndex(
            window.ribbon._tab_keys.index("vector_tools")
        )
        window._sync_contextual_ribbon()
        assert window.ribbon.current_key() == "vector_tools"
    finally:
        window.hide()
        window.deleteLater()


def test_pressure_presets_button_opens_for_raster_and_vector_pencil(
    qapp, monkeypatch,
):
    opened = []

    class FakePencilSettingsDialog(QObject):
        committedPresets = Signal(object, str)

        def __init__(self, presets, active_name, parent):
            super().__init__(parent)
            opened.append((presets, active_name, parent))

        def exec(self):
            return 0

    monkeypatch.setattr(
        main_window_module, "PencilSettingsDialog", FakePencilSettingsDialog,
    )
    window = MainWindow()
    chapter, _page, layer, raster = _window_document(window)
    vector = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Vector Ink")
    )
    window._refresh_hierarchy()
    try:
        controls = window.tool_settings_controls
        for object_id in (raster.object_id, vector.object_id):
            window.canvas.set_selection("object", object_id)
            qapp.processEvents()
            assert window.ribbon.current_key() == "tool_settings"
            controls.pencil_presets_button.click()

        assert len(opened) == 2
        assert all(active_name == window.settings.active_pencil_preset
                   for _presets, active_name, _parent in opened)
    finally:
        window.deleteLater()


def test_common_selection_row_targets_exact_entity_and_coalesces_opacity(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, layer, raster = _window_document(window)
    window._set_chapter(chapter, TileStore())
    window.show()
    qapp.processEvents()
    try:
        window.canvas.set_selection("layer", layer.layer_id, False)
        common = window.selection_common
        assert common.visible.isEnabled()
        assert common.opacity.isEnabled()
        assert common.opacity_value.text() == "100%"

        window.canvas.command_stack.clear()
        common.opacity.sliderPressed.emit()
        common.opacity.setValue(82)
        common.opacity.setValue(47)
        common.opacity.sliderReleased.emit()
        assert chapter.layers[layer.layer_id].opacity == 0.47
        assert common.opacity_value.text() == "47%"
        assert len(window.canvas.command_stack._undo) == 1
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.layers[layer.layer_id].opacity == 1.0
        window.canvas.command_stack.redo()
        assert window.canvas.chapter.layers[layer.layer_id].opacity == 0.47

        raster = window.canvas.chapter.objects[raster.object_id]
        raster.opacity_locked = True
        window.canvas.set_selection("object", raster.object_id)
        common.refresh()
        assert common.opacity.isEnabled()
        assert common.opacity_lock.isChecked()
        common.opacity_lock.setChecked(False)
        qapp.processEvents()
        assert common.opacity.isEnabled()

        window.canvas.clear_selection()
        common.refresh()
        assert not common.visible.isEnabled()
        assert not common.opacity.isEnabled()
    finally:
        window.deleteLater()


def test_selection_settings_switches_object_pages_and_uses_parent_for_contextual(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, layer, raster = _window_document(window)
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Vector")
    )
    fill = chapter.add_vector_fill(
        drawing.object_id, VectorFillObject(name="Fill")
    )
    text = chapter.add_object(layer.layer_id, TextObject(text="Text"))
    gradient = chapter.add_object(
        layer.layer_id, ColorFillGradientObject()
    )
    window._set_chapter(chapter, TileStore())
    panel = window.selection_settings
    try:
        window.canvas.set_selection("object", raster.object_id)
        assert panel.stack.currentWidget() is panel.raster_page
        window.canvas.set_selection("object", drawing.object_id)
        assert panel.stack.currentWidget() is panel.vector_page
        assert panel.vector_controls.ignore_parent_mask.isVisibleTo(panel)
        window.canvas.set_selection("object", fill.object_id)
        assert panel.stack.currentWidget() is panel.vector_page
        assert panel.vector_controls.type_label.text() == "Vector Fill"
        assert panel.vector_controls.ignore_parent_mask.isHidden()
        assert panel.vector_controls.underlay_row.isHidden()

        for object_id in (text.object_id, gradient.object_id):
            window.canvas.set_selection("object", object_id)
            assert panel.stack.currentWidget() is panel.layer_page
            assert panel.layer_page.title() == "Parent Layer Settings"
            assert panel.layer_page.name.text() == "Panel"
        assert window.selection_common.opacity_lock.isChecked()
        assert window.selection_common.opacity.isEnabled()
        window.selection_common.opacity_lock.setChecked(False)
        qapp.processEvents()
        assert window.selection_common.opacity.isEnabled()
    finally:
        window.deleteLater()


def test_common_text_visibility_commits_typing_before_property_undo(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    text = chapter.add_object(page.layer_id, TextObject(text="abc"))
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", text.object_id)
        window.canvas._begin_text_session(text)
        window.canvas._text_cursor_position = len(text.text)
        window.canvas._text_selection_anchor = len(text.text)
        window.canvas.setFocus()
        QTest.keyClicks(window.canvas, "x")
        assert text.text == "abcx"
        assert len(window.canvas.command_stack._undo) == 0

        window.selection_common.visible.click()
        assert len(window.canvas.command_stack._undo) == 2
        assert not window.canvas.chapter.objects[text.object_id].visible
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[text.object_id].visible
        assert window.canvas.chapter.objects[text.object_id].text == "abcx"
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[text.object_id].text == "abc"
    finally:
        window.deleteLater()
