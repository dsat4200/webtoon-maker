from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import (
    QColor, QFont, QImage, QMouseEvent, QPainter, QPolygonF,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (
    QApplication, QInputDialog, QMessageBox, QSpinBox,
)

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    LineGradientField, PathNode, TextObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _chapter_with_texts():
    chapter = ChapterDocument()
    page = chapter.add_page()
    first = chapter.add_object(
        page.layer_id,
        TextObject(
            text="First", layout_mode="free", width=240, height=100,
            transform_quad=[(80, 80), (320, 80), (320, 180), (80, 180)],
        ),
    )
    second = chapter.add_object(
        page.layer_id,
        TextObject(
            text="Second", layout_mode="free", width=240, height=100,
            transform_quad=[(400, 80), (640, 80), (640, 180), (400, 180)],
        ),
    )
    return chapter, page, first, second


def _canvas_with_text():
    chapter, _page, first, second = _chapter_with_texts()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 650)
    canvas.set_document(chapter, TileStore())
    canvas.center_x = 360
    canvas.center_y = 250
    canvas.scale = 1.0
    canvas.set_selection("object", first.object_id)
    canvas.show()
    QApplication.processEvents()
    return canvas, first, second


def _type_spin_value(spin: QSpinBox, value: str) -> None:
    spin.setFocus()
    QApplication.processEvents()
    spin.lineEdit().selectAll()
    QTest.keyClicks(spin.lineEdit(), value)
    QTest.keyClick(spin.lineEdit(), Qt.Key.Key_Return)
    QApplication.processEvents()


def test_text_ribbon_owns_complete_selected_object_controls(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, first, second = _chapter_with_texts()
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", first.object_id)
        qapp.processEvents()
        controls = window.text_object_controls

        assert window.ribbon.current_key() == "tool_settings"
        assert not hasattr(window, "inspector")
        assert window.selection_settings.stack.currentWidget() is (
            window.selection_settings.layer_page
        )
        assert window.selection_settings.layer_page.title() == (
            "Parent Layer Settings"
        )
        assert window.tool_settings_group.isHidden()
        assert window.text_object_group.isVisible()
        assert window.text_typography_group.isVisible()
        assert window.text_layout_group.isVisible()
        assert isinstance(controls.font_size, QSpinBox)
        assert controls.font_size.minimum() == 6
        assert controls.font_size.maximum() == 250
        assert controls.font_size.keyboardTracking() is False
        assert controls.kerning.minimum() == -20
        assert controls.kerning.maximum() == 100
        assert controls.margin.isHidden()
        assert controls.transform_mode.isVisible()
        assert controls.geometry_reference.isHidden()

        controls.layout_mode.setCurrentIndex(
            controls.layout_mode.findData("strict")
        )
        qapp.processEvents()
        assert controls.margin.isVisible()
        assert controls.transform_mode.isHidden()
        controls.layout_mode.setCurrentIndex(
            controls.layout_mode.findData("free")
        )
        page = window.canvas.chapter.layers[first.parent_layer_id]
        page.compound_enabled = True
        controls.refresh()
        assert controls.geometry_reference.isVisible()
        page.compound_enabled = False
        controls.refresh()

        _type_spin_value(controls.font_size, "250")
        assert first.font_size == 250
        assert second.font_size == 32
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[first.object_id].font_size == 32
        window.canvas.command_stack.redo()
        assert window.canvas.chapter.objects[first.object_id].font_size == 250

        _type_spin_value(controls.font_size, "999")
        assert window.canvas.chapter.objects[first.object_id].font_size == 250

        controls.bold.click()
        controls.italic.click()
        selected = window.canvas.chapter.objects[first.object_id]
        assert selected.bold and selected.italic
        assert not window.canvas.chapter.objects[second.object_id].bold

        window.settings.preview_font_names = False
        controls.refresh()
        assert controls.font_family.itemData(
            0, Qt.ItemDataRole.FontRole
        ) is None
        controls.preview_fonts.setChecked(True)
        preview_font = controls.font_family.itemData(
            0, Qt.ItemDataRole.FontRole
        )
        assert isinstance(preview_font, QFont)
        assert preview_font.family() == controls.font_family.itemText(0)

        names = iter((("Comic", True), ("Lettering", True)))
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, **kwargs: next(names)
        )
        controls._add_preset()
        assert window.settings.active_text_preset == "Comic"
        assert window.settings.text_presets[-1]["font_size"] == 250
        controls._rename_preset()
        assert window.settings.active_text_preset == "Lettering"
        controls._remove_preset()
        assert window.settings.active_text_preset == "Default"
        assert all(
            item["name"] != "Lettering"
            for item in window.settings.text_presets
        )
    finally:
        window.hide()
        window.deleteLater()


def test_text_gizmo_overlay_edits_only_selected_text_and_supports_cancel(qapp):
    canvas, first, second = _canvas_with_text()
    try:
        overlay = canvas._text_gizmo_overlay
        assert canvas.tool == ToolKind.TEXT_EDIT
        assert overlay.isVisible()
        assert overlay.decrease.size().toTuple() == (38, 34)
        assert overlay.size.size().toTuple() == (73, 34)
        assert overlay.layout().contentsMargins().left() == 6
        assert overlay.layout().spacing() == 4

        overlay.increase.click()
        assert canvas.chapter.objects[first.object_id].font_size == 33
        assert canvas.chapter.objects[second.object_id].font_size == 32
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].font_size == 32

        overlay.bold.click()
        overlay.italic.click()
        selected = canvas.chapter.objects[first.object_id]
        assert selected.bold and selected.italic

        _type_spin_value(overlay.size, "250")
        assert canvas.chapter.objects[first.object_id].font_size == 250

        overlay.size.setFocus()
        qapp.processEvents()
        overlay.size.lineEdit().selectAll()
        QTest.keyClicks(overlay.size.lineEdit(), "80")
        QTest.keyClick(overlay.size.lineEdit(), Qt.Key.Key_Escape)
        qapp.processEvents()
        assert canvas.chapter.objects[first.object_id].font_size == 250

        canvas.set_tool(ToolKind.OBJECT_SELECT)
        qapp.processEvents()
        canvas.update()
        qapp.processEvents()
        assert overlay.isHidden()
    finally:
        canvas.hide()
        canvas.deleteLater()


@pytest.mark.parametrize("layout_mode", ["strict", "free"])
def test_mouse_drag_and_shift_navigation_select_text(qapp, layout_mode):
    canvas, first, _second = _canvas_with_text()
    try:
        first.layout_mode = layout_mode
        document, origin, transform = canvas._text_edit_layout(first)
        start = canvas._text_caret_rect(document, 0).center()
        end = canvas._text_caret_rect(document, len(first.text)).center()
        start_world = origin + transform.map(start + QPointF(1, 0))
        end_world = origin + transform.map(end - QPointF(1, 0))

        assert canvas._begin_text_pointer(start_world)
        canvas._update_text_pointer(end_world)
        selected = canvas._text_selection_range()
        assert selected[0] == 0
        assert selected[1] >= len(first.text) - 1

        canvas._text_cursor_position = 1
        canvas._text_selection_anchor = 1
        canvas.setFocus()
        QTest.keyClick(
            canvas, Qt.Key.Key_Right, Qt.KeyboardModifier.ShiftModifier
        )
        assert canvas._text_selection_anchor == 1
        assert canvas._text_cursor_position == 2
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_free_text_boundary_translates_while_interior_edits(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        quad = canvas.object_world_quad(first.object_id)
        boundary = QPointF(
            quad[0][0] * 0.75 + quad[1][0] * 0.25,
            quad[0][1] * 0.75 + quad[1][1] * 0.25,
        )
        mode, handle = canvas._text_transform_control_hit(quad, boundary)
        assert (mode, handle) == ("translate", None)

        before = list(first.transform_quad)
        canvas._tool_press(canvas.document_to_widget(boundary), 1.0)
        assert canvas._transform_drag_mode == "translate"
        canvas._tool_move(canvas.document_to_widget(boundary + QPointF(35, 24)), 1.0)
        assert canvas._transform_preview_quad != before
        canvas._tool_release()
        assert first.transform_quad[0] == pytest.approx((115, 104))
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].transform_quad == before

        center = QPolygonF([
            QPointF(*point) for point in canvas.object_world_quad(first.object_id)
        ]).boundingRect().center()
        canvas._tool_press(canvas.document_to_widget(center), 1.0)
        assert canvas._text_dragging
        assert canvas._model_before is None
        canvas._tool_release()
    finally:
        canvas.hide()
        canvas.deleteLater()


@pytest.mark.parametrize("layout_mode", ["strict", "free"])
def test_text_double_click_word_triple_click_all_and_live_drag(
    qapp, layout_mode,
):
    canvas, first, _second = _canvas_with_text()
    try:
        first.text = "alpha beta"
        first.layout_mode = layout_mode
        document, origin, transform = canvas._text_edit_layout(first)
        beta = canvas._text_caret_rect(document, 7).center()
        beta_world = origin + transform.map(beta)
        beta_widget = canvas.document_to_widget(beta_world).toPoint()

        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=beta_widget)
        qapp.processEvents()
        assert canvas._text_selection_range() == [6, 10]

        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=beta_widget)
        qapp.processEvents()
        assert canvas._text_selection_range() == [0, len(first.text)]

        start = canvas._text_caret_rect(document, 0).center()
        end = canvas._text_caret_rect(document, 5).center()
        start_widget = canvas.document_to_widget(
            origin + transform.map(start)
        ).toPoint()
        end_widget = canvas.document_to_widget(
            origin + transform.map(end)
        ).toPoint()
        QTest.mousePress(canvas, Qt.MouseButton.LeftButton, pos=start_widget)
        QTest.mouseMove(canvas, end_widget)
        qapp.processEvents()
        assert canvas._text_selection_range()[1] >= 4
        QTest.mouseRelease(canvas, Qt.MouseButton.LeftButton, pos=end_widget)
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_text_hover_uses_ibeam_but_boundary_keeps_translate_cursor(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        quad = canvas.object_world_quad(first.object_id)
        interior = QPointF(
            quad[0][0] * 0.65 + quad[2][0] * 0.35,
            quad[0][1] * 0.65 + quad[2][1] * 0.35,
        )
        def hover(world):
            widget = canvas.document_to_widget(world)
            canvas.mouseMoveEvent(QMouseEvent(
                QEvent.Type.MouseMove,
                widget,
                QPointF(canvas.mapToGlobal(widget.toPoint())),
                Qt.MouseButton.NoButton,
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
            ))

        hover(interior)
        assert canvas.cursor().shape() == Qt.CursorShape.IBeamCursor

        boundary = QPointF(
            quad[0][0] * 0.75 + quad[1][0] * 0.25,
            quad[0][1] * 0.75 + quad[1][1] * 0.25,
        )
        hover(boundary)
        assert canvas.cursor().shape() == Qt.CursorShape.SizeAllCursor
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_text_selection_uses_translucent_orange_without_white_foreground(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        canvas._begin_text_session(first)
        assert canvas.select_all()
        document = canvas._text_document(first, first.width)
        image = QImage(300, 120, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        canvas._draw_text_document(painter, first, document)
        painter.end()

        colors = [
            image.pixelColor(x, y)
            for y in range(image.height())
            for x in range(image.width())
            if image.pixelColor(x, y).alpha() > 0
        ]
        assert any(
            color.red() > 225
            and 130 < color.green() < 185
            and color.blue() < 90
            and 85 <= color.alpha() <= 120
            for color in colors
        )
        assert any(
            color.red() < 80 and color.green() < 80 and color.blue() < 80
            for color in colors
        )
        assert not any(
            color.red() > 245 and color.green() > 245 and color.blue() > 245
            for color in colors
        )
    finally:
        canvas.hide()
        canvas.deleteLater()


@pytest.mark.parametrize("operation", ["typing", "paste", "delete", "backspace"])
def test_selected_text_is_replaced_as_one_edit_transaction(
    qapp, operation,
):
    canvas, first, _second = _canvas_with_text()
    try:
        first.text = "abcd"
        canvas._begin_text_session(first)
        canvas._text_selection_anchor = 1
        canvas._text_cursor_position = 3
        before_commands = len(canvas.command_stack._undo)
        canvas.setFocus()
        if operation == "typing":
            QTest.keyClicks(canvas, "X")
            expected = "aXd"
        elif operation == "paste":
            qapp.clipboard().setText("XY")
            QTest.keyClick(canvas, Qt.Key.Key_V, Qt.KeyboardModifier.ControlModifier)
            QTest.keyRelease(canvas, Qt.Key.Key_Control)
            expected = "aXYd"
        else:
            key = (
                Qt.Key.Key_Delete
                if operation == "delete" else Qt.Key.Key_Backspace
            )
            QTest.keyClick(canvas, key)
            expected = "ad"
        assert first.text == expected
        canvas.commit_active_text_edit()
        assert len(canvas.command_stack._undo) == before_commands + 1
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].text == "abcd"
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_configurable_select_all_targets_canvas_text_but_yields_to_fields(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, first, _second = _chapter_with_texts()
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", first.object_id)
        window.canvas._begin_text_session(first)
        window.canvas._text_cursor_position = 2
        window.canvas._text_selection_anchor = 2
        window.canvas.setFocus()
        qapp.processEvents()
        QTest.keyClick(
            window.canvas, Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier
        )
        QTest.keyRelease(window.canvas, Qt.Key.Key_Control)
        assert window.canvas._text_selection_range() == [0, len(first.text)]

        window.canvas._text_cursor_position = 2
        window.canvas._text_selection_anchor = 2
        field = window.text_object_controls.font_size
        field.setFocus()
        qapp.processEvents()
        QTest.keyClick(
            field.lineEdit(), Qt.Key.Key_A, Qt.KeyboardModifier.ControlModifier
        )
        QTest.keyRelease(field.lineEdit(), Qt.Key.Key_Control)
        assert field.lineEdit().selectedText() == field.lineEdit().text()
        assert window.canvas._text_selection_range() == [2, 2]
    finally:
        window.hide()
        window.deleteLater()


def test_legacy_text_size_is_preserved_until_size_itself_is_edited(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, first, _second = _chapter_with_texts()
    first.font_size = 300.5
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", first.object_id)
        qapp.processEvents()
        controls = window.text_object_controls
        assert controls.font_size.value() == 250

        controls.bold.click()
        assert window.canvas.chapter.objects[first.object_id].font_size == 300.5

        _type_spin_value(controls.font_size, "250")
        assert window.canvas.chapter.objects[first.object_id].font_size == 250
    finally:
        window.hide()
        window.deleteLater()


def test_ribbon_property_edit_commits_active_typing_first(qapp, monkeypatch):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, _page, first, _second = _chapter_with_texts()
    first.text = "ab"
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", first.object_id)
        window.canvas._begin_text_session(first)
        window.canvas._text_cursor_position = 2
        window.canvas._text_selection_anchor = 2
        window.canvas.setFocus()
        QTest.keyClicks(window.canvas, "c")
        assert window.canvas.chapter.objects[first.object_id].text == "abc"

        window.text_object_controls.bold.click()
        assert window.canvas.chapter.objects[first.object_id].bold
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[first.object_id].text == "abc"
        assert not window.canvas.chapter.objects[first.object_id].bold
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[first.object_id].text == "ab"
    finally:
        window.hide()
        window.deleteLater()


def test_text_property_handles_are_zoom_independent_snapped_and_undoable(qapp):
    canvas, first, _second = _canvas_with_text()
    try:
        start_count = len(canvas.command_stack._undo)
        size_handle = canvas.document_to_widget(
            canvas._text_property_handle_positions()["font_size"]
        )
        canvas._tool_press(size_handle, 1)
        canvas._tool_release()
        assert canvas.chapter.objects[first.object_id].font_size == 32
        assert len(canvas.command_stack._undo) == start_count

        canvas._tool_press(size_handle, 1)
        canvas._tool_move(size_handle + QPointF(8, 0), 1)
        canvas._tool_release()
        assert canvas.chapter.objects[first.object_id].font_size == 34
        assert len(canvas.command_stack._undo) == start_count + 1
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].font_size == 32

        canvas.scale = 2.0
        size_handle = canvas.document_to_widget(
            canvas._text_property_handle_positions()["font_size"]
        )
        canvas._tool_press(size_handle, 1)
        canvas._tool_move(size_handle + QPointF(8, 0), 1)
        canvas._tool_release()
        assert canvas.chapter.objects[first.object_id].font_size == 34

        kerning_handle = canvas.document_to_widget(
            canvas._text_property_handle_positions()["kerning"]
        )
        canvas._tool_press(kerning_handle, 1)
        canvas._tool_move(kerning_handle + QPointF(8, 0), 1)
        canvas._tool_release()
        assert canvas.chapter.objects[first.object_id].kerning == 1.0

        size_handle = canvas.document_to_widget(
            canvas._text_property_handle_positions()["font_size"]
        )
        before_cancel = canvas.chapter.objects[first.object_id].font_size
        canvas._tool_press(size_handle, 1)
        canvas._tool_move(size_handle + QPointF(12, 0), 1)
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert canvas.chapter.objects[first.object_id].font_size == before_cancel
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_delete_hotkey_deletes_entities_but_yields_to_text_editing(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    monkeypatch.setattr(
        QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes
    )
    window = MainWindow()
    chapter, _page, first, _second = _chapter_with_texts()
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("object", first.object_id)
        window.canvas.setFocus()
        qapp.processEvents()
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert first.object_id not in window.canvas.chapter.objects
        window.canvas.command_stack.undo()
        assert first.object_id in window.canvas.chapter.objects

        text = window.canvas.chapter.objects[first.object_id]
        text.text = "ab"
        window.canvas.set_selection("object", first.object_id)
        window.canvas._begin_text_session(text)
        window.canvas._text_cursor_position = 1
        window.canvas._text_selection_anchor = 1
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert first.object_id in window.canvas.chapter.objects
        assert window.canvas.chapter.objects[first.object_id].text == "a"

        window.canvas.commit_active_text_edit()
        controls = window.text_object_controls
        controls.font_size.setFocus()
        qapp.processEvents()
        QTest.keyClick(controls.font_size, Qt.Key.Key_Delete)
        assert first.object_id in window.canvas.chapter.objects

        controls.font_size.clearFocus()
        page_id = window.canvas.chapter.root_page_ids[0]
        child = window.canvas.chapter.add_layer(page_id, "Delete me")
        window.canvas.set_selection("layer", child.layer_id)
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert child.layer_id not in window.canvas.chapter.layers
        window.canvas.command_stack.undo()
        assert child.layer_id in window.canvas.chapter.layers

        window.active_session = SimpleNamespace(
            kind="asset",
            asset_manifest=SimpleNamespace(root_id=page_id),
        )
        window.canvas.set_selection("layer", page_id)
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert page_id in window.canvas.chapter.layers
        assert "cannot be deleted" in window.statusBar().currentMessage()
        window.active_session = None
    finally:
        window.hide()
        window.deleteLater()


def test_delete_hotkey_yields_to_shape_and_gradient_point_editors(
    qapp, monkeypatch,
):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _value: None)
    window = MainWindow()
    chapter, page, _first, _second = _chapter_with_texts()
    layer = chapter.add_layer(
        page.layer_id, "Shape", BoundGeometry.rectangle(20, 240, 300, 180)
    )
    gradient = chapter.add_object(
        page.layer_id,
        ColorFillGradientObject(
            line_field=LineGradientField(BoundGeometry.path([
                PathNode(x=80, y=350),
                PathNode(x=200, y=420),
                PathNode(x=360, y=350),
            ])),
        ),
    )
    window._set_chapter(chapter, TileStore())
    window.show()
    try:
        window.canvas.set_selection("layer", layer.layer_id)
        window.canvas.set_tool(ToolKind.SHAPE_EDIT)
        window.canvas._selected_shape_node_id = layer.bound.nodes[0].node_id
        window.canvas.setFocus()
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert layer.layer_id in window.canvas.chapter.layers
        assert len(window.canvas.chapter.layers[layer.layer_id].bound.nodes) == 3

        current_gradient = window.canvas.chapter.objects[gradient.object_id]
        window.canvas.set_selection("object", gradient.object_id)
        window.canvas.set_tool(ToolKind.SHAPE_EDIT)
        window.canvas._selected_shape_node_id = (
            current_gradient.line_field.geometry.nodes[1].node_id
        )
        QTest.keyClick(window.canvas, Qt.Key.Key_Delete)
        assert gradient.object_id in window.canvas.chapter.objects
        assert len(
            window.canvas.chapter.objects[
                gradient.object_id
            ].line_field.geometry.nodes
        ) == 2
    finally:
        window.hide()
        window.deleteLater()


def test_free_text_transform_reuses_cached_render_until_commit(qapp, monkeypatch):
    canvas, first, _second = _canvas_with_text()
    canvas.set_tool(ToolKind.TRANSFORM)
    calls = 0
    grid_calls = 0
    original = canvas._text_document
    original_grid = canvas._draw_grid

    def counted(*args, **kwargs):
        nonlocal calls
        # The static scene may legitimately lay out other visible text once.
        # The selected item itself must be laid out exactly once into its
        # dedicated transform cache and never again during pointer moves.
        if args[0].object_id == first.object_id:
            calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas, "_text_document", counted)

    def counted_grid(*args, **kwargs):
        nonlocal grid_calls
        grid_calls += 1
        return original_grid(*args, **kwargs)

    monkeypatch.setattr(canvas, "_draw_grid", counted_grid)
    try:
        start = canvas.document_to_widget(QPointF(80, 80))
        canvas._tool_press(start, 1)
        assert not canvas._transform_static_cache.isNull()
        assert not canvas._text_transform_cache.isNull()
        assert calls == 1
        assert grid_calls == 1

        for offset in range(4, 44, 4):
            canvas._tool_move(start + QPointF(-offset, -offset), 1)
            qapp.processEvents()
            assert canvas._selected_world_quad()[0] == (
                80 - offset, 80 - offset
            )
        assert calls == 1
        assert grid_calls == 1
        assert not canvas.grab().toImage().isNull()
        assert calls == 1

        canvas._tool_release()
        assert canvas.chapter.objects[first.object_id].transform_quad[0] == (40, 40)
        assert canvas._text_transform_cache.isNull()
        canvas.command_stack.undo()
        assert canvas.chapter.objects[first.object_id].transform_quad[0] == (80, 80)

        canvas.set_tool(ToolKind.TRANSFORM)
        start = canvas.document_to_widget(QPointF(80, 80))
        canvas._tool_press(start, 1)
        canvas._tool_move(start + QPointF(-20, -20), 1)
        assert not canvas._text_transform_cache.isNull()
        QTest.keyClick(canvas, Qt.Key.Key_Escape)
        assert canvas._text_transform_cache.isNull()
        assert canvas._transform_static_cache.isNull()
        assert canvas.chapter.objects[first.object_id].transform_quad[0] == (80, 80)
    finally:
        canvas.hide()
        canvas.deleteLater()
