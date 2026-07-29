from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QInputMethodEvent
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, TextObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _canvas_document(settings: EditorSettings | None = None):
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    layer = chapter.add_layer(
        page.layer_id, "Layer 1", BoundGeometry.rectangle(40, 40, 500, 500)
    )
    canvas = CanvasWidget(settings or EditorSettings(
        snap_to_grid=False, transform_snap_to_grid=False
    ))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    return canvas, chapter, page, layer


def _widget(canvas, x: float, y: float) -> QPointF:
    return canvas.document_to_widget(QPointF(x, y))


def test_bound_center_drag_translates_only_mask_and_is_undoable(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(layer.layer_id, RasterObject(x=90, y=80))
    original_points = list(layer.bound.points)
    original_object_position = (raster.x, raster.y)
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.BOUND_EDIT)
    canvas._tool_press(_widget(canvas, 200, 200), 1)
    canvas._tool_move(_widget(canvas, 230, 220), 1)
    canvas._tool_release()
    assert layer.bound.points == [
        (x + 30, y + 20) for x, y in original_points
    ]
    assert (raster.x, raster.y) == original_object_position
    canvas.command_stack.undo()
    assert canvas.chapter.layers[layer.layer_id].bound.points == original_points


def test_tablet_horizontal_pan_is_opposite_desktop_grab_pan(qapp):
    canvas, chapter, page, layer = _canvas_document()
    canvas.scale = 1
    canvas.center_x = 500
    canvas._apply_touch_pan_delta(20, 0)
    assert canvas.center_x == 520
    canvas._begin_navigation("pan", QPointF(100, 100))
    canvas._update_navigation(QPointF(120, 100))
    assert canvas.center_x == 500


def test_free_text_transform_persists_quad_and_preserves_content(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            text="Editable",
            layout_mode="free",
            width=200,
            height=80,
            transform_quad=[(80, 80), (280, 80), (280, 160), (80, 160)],
        ),
    )
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(_widget(canvas, 80, 80), 1)
    canvas._tool_move(_widget(canvas, 60, 65), 1)
    canvas._tool_release()
    transformed = canvas.chapter.objects[text.object_id]
    assert transformed.text == "Editable"
    assert transformed.transform_quad[0] == pytest.approx((60, 65))
    assert len(canvas._quad_handles(transformed.transform_quad)) == 8
    canvas.command_stack.undo()
    assert canvas.chapter.objects[text.object_id].transform_quad[0] == (80, 80)


def test_transform_handle_snap_is_independent_of_bound_snap(qapp):
    settings = EditorSettings(
        snap_to_grid=False, transform_snap_to_grid=True
    )
    canvas, chapter, page, layer = _canvas_document(settings)
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[(80, 80), (280, 80), (280, 160), (80, 160)],
        ),
    )
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(_widget(canvas, 80, 80), 1)
    canvas._tool_move(_widget(canvas, 61, 67), 1)
    canvas._tool_release()
    assert canvas.chapter.objects[text.object_id].transform_quad[0] == (60, 60)


def test_uniform_transform_preserves_quad_proportions(qapp):
    settings = EditorSettings(
        transform_mode="uniform", transform_snap_to_grid=False
    )
    canvas, chapter, page, layer = _canvas_document(settings)
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[(80, 80), (280, 80), (280, 160), (80, 160)],
        ),
    )
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(_widget(canvas, 80, 80), 1)
    canvas._tool_move(_widget(canvas, 40, 40), 1)
    canvas._tool_release()
    quad = canvas.chapter.objects[text.object_id].transform_quad
    width = ((quad[1][0] - quad[0][0]) ** 2 + (quad[1][1] - quad[0][1]) ** 2) ** 0.5
    height = ((quad[3][0] - quad[0][0]) ** 2 + (quad[3][1] - quad[0][1]) ** 2) ** 0.5
    assert width / height == pytest.approx(2.5)


def test_sparse_raster_transform_and_dedicated_restore(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(layer.layer_id, RasterObject(x=100, y=100))
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 20, QColor("#111111")
    )
    original_bounds = canvas.tiles.content_bounds(raster.object_id)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    center = canvas.object_world_rect(raster.object_id).center()
    canvas._tool_press(canvas.document_to_widget(center), 1)
    canvas._tool_move(canvas.document_to_widget(center + QPointF(100, 50)), 1)
    canvas._tool_release()
    moved_bounds = canvas.tiles.content_bounds(raster.object_id)
    assert moved_bounds.left() > original_bounds.left() + 90
    assert canvas.can_undo_raster_transform()
    assert canvas.undo_raster_transform()
    restored = canvas.tiles.content_bounds(raster.object_id)
    assert restored == original_bounds
    assert not canvas.can_undo_raster_transform()


def test_live_text_session_commits_as_one_document_command(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id,
        TextObject(text="", layout_mode="strict", margin=24),
    )
    canvas.set_selection("object", text.object_id)
    assert canvas.tool == ToolKind.TEXT_EDIT
    canvas._replace_text_selection("Hello")
    canvas._replace_text_selection("\nworld")
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    assert canvas.chapter.objects[text.object_id].text == "Hello\nworld"
    canvas.command_stack.undo()
    assert canvas.chapter.objects[text.object_id].text == ""


def test_text_edit_accepts_keyboard_clipboard_and_ime(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id, TextObject(text="", layout_mode="strict")
    )
    canvas.set_selection("object", text.object_id)
    canvas.show()
    canvas.setFocus()
    QTest.keyClicks(canvas, "abc")
    QTest.keyClick(canvas, Qt.Key_Return)
    QTest.keyClicks(canvas, "def")
    ime = QInputMethodEvent()
    ime.setCommitString("語")
    qapp.sendEvent(canvas, ime)
    assert text.text == "abc\ndef語"
    QTest.keyClick(canvas, Qt.Key_A, Qt.ControlModifier)
    QTest.keyClick(canvas, Qt.Key_C, Qt.ControlModifier)
    assert qapp.clipboard().text() == "abc\ndef語"
    canvas.hide()


def test_text_canvas_can_paint_with_live_inspector_state(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id, TextObject(text="Visible\ntext", layout_mode="strict")
    )
    canvas.set_selection("object", text.object_id)
    canvas.show()
    qapp.processEvents()
    image = canvas.grab().toImage()
    assert not image.isNull()
    canvas.hide()


def test_bound_edit_promotes_parent_and_outside_click_promotes_page(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            layout_mode="free",
            transform_quad=[(80, 80), (280, 80), (280, 160), (80, 160)],
        ),
    )
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.BOUND_EDIT)
    assert canvas.selected_kind == "layer"
    assert canvas.selected_id == layer.layer_id
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(_widget(canvas, 800, 800), 1)
    canvas._tool_release()
    assert canvas.selected_kind == "layer"
    assert canvas.selected_id == page.layer_id
    assert canvas.tool == ToolKind.OBJECT_SELECT


def test_outliner_colors_collapsed_add_and_dynamic_tools(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id, "Layer 1")
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", layer.layer_id)
    layer_index = window.hierarchy_model.index_for_entity("layer", layer.layer_id)
    window.tree.setExpanded(layer_index, False)
    window._add_text()
    assert window.canvas.selected_kind == "layer"
    assert window.canvas.selected_id == layer.layer_id
    refreshed = window.hierarchy_model.index_for_entity("layer", layer.layer_id)
    assert not window.tree.isExpanded(refreshed)
    assert window.hierarchy_model.data(refreshed, Qt.BackgroundRole).name() == "#303238"
    object_id = next(iter(chapter.objects))
    object_index = window.hierarchy_model.index_for_entity("object", object_id)
    assert window.hierarchy_model.data(object_index, Qt.BackgroundRole).name() == "#050505"
    assert window.tool_buttons[ToolKind.RASTER_PENCIL].isHidden()
    window.show()
    window.canvas.set_selection("object", object_id)
    assert not window.tool_buttons[ToolKind.TEXT_EDIT].isHidden()
    qapp.processEvents()
    assert window.inspector.isVisible()
    window._activate_tool(ToolKind.OBJECT_SELECT)
    assert window.inspector.isHidden()
    window.hide()
    window.deleteLater()
