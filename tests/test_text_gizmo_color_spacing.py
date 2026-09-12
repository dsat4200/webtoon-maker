"""Text gizmos keep range formatting transactional and line spacing undoable."""
from __future__ import annotations

import copy

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox

from comic_editor.core.models import BoundGeometry, ChapterDocument, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.text_styles import apply_text_color, text_color_at
from comic_editor.core.tiles import TileStore
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.color_picker import ColorPickerPopup
from comic_editor.ui.main_window import MainWindow


def _chapter():
    chapter = ChapterDocument(height=700)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 700))
    obj = chapter.add_object(page.layer_id, TextObject(
        text="One\nTwo\nThree", layout_mode="free", width=320, height=240,
        transform_quad=[(80, 80), (400, 80), (400, 320), (80, 320)],
    ))
    return chapter, obj


@pytest.fixture
def text_canvas(qapp):
    chapter, obj = _chapter()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(1000, 750)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 360, 250, 1.0
    canvas.set_selection("object", obj.object_id)
    canvas.show()
    canvas.start_text_edit()
    qapp.processEvents()
    yield canvas, obj.object_id
    if canvas._text_color_popup is not None:
        canvas._text_color_popup.reject()
    canvas.hide()
    canvas.deleteLater()


def _open_picker(canvas, qapp):
    overlay = canvas._text_gizmo_overlay
    assert overlay.isVisible()
    assert overlay.color.x() >= overlay.italic.geometry().right()
    assert overlay.color.width() == overlay.color.height() == 34
    assert overlay.color.text() == ""
    assert overlay.color.accessibleName() == "Text color"
    QTest.mouseClick(overlay.color, Qt.LeftButton)
    qapp.processEvents()
    popup = canvas._text_color_popup
    assert isinstance(popup, ColorPickerPopup)
    assert popup.isVisible()
    return popup


def _apply(popup, qapp):
    QTest.mouseClick(popup.buttons.button(QDialogButtonBox.Apply), Qt.LeftButton)
    qapp.processEvents()


def _key(canvas, key, modifiers=Qt.NoModifier):
    QTest.keyClick(canvas, key, modifiers)
    if modifiers & Qt.ControlModifier:
        QTest.keyRelease(canvas, Qt.Key_Control)


def test_color_gizmo_preserves_range_and_applies_once_with_keyboard_undo(text_canvas, qapp):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    original = obj.to_dict()
    canvas._text_selection_anchor, canvas._text_cursor_position = 1, 6
    revision = canvas.command_stack.revision
    swatch = canvas._text_gizmo_overlay.color
    assert swatch.grab().toImage().pixelColor(17, 17) == QColor("#FF111111")
    popup = _open_picker(canvas, qapp)
    popup.setColor("#FFCC2244")
    popup.picker.setFocus()
    qapp.processEvents()
    assert canvas._text_selection_range() == [1, 6]
    assert obj.to_dict() == original  # Picker preview is a draft until Apply.
    assert swatch.property("textColor") == "#FF111111"
    _apply(popup, qapp)
    assert canvas.has_active_text_edit()
    assert canvas._text_selection_range() == [1, 6]
    assert canvas.command_stack.revision == revision + 1
    assert swatch.property("textColor") == "#FFCC2244"
    assert [text_color_at(obj, i) for i in range(len(obj.text))] == [
        "#FFCC2244" if 1 <= i < 6 else "#FF111111" for i in range(len(obj.text))
    ]
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    qapp.processEvents()
    assert canvas.chapter.objects[object_id].to_dict() == original
    assert swatch.property("textColor") == "#FF111111"
    _key(canvas, Qt.Key_Y, Qt.ControlModifier)
    qapp.processEvents()
    assert text_color_at(canvas.chapter.objects[object_id], 3) == "#FFCC2244"
    assert swatch.property("textColor") == "#FFCC2244"


def test_swatch_follows_selection_caret_and_selected_object(text_canvas, qapp):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    apply_text_color(obj, 2, 5, "#804422CC")
    canvas.commit_active_text_edit()
    canvas.start_text_edit()
    swatch = canvas._text_gizmo_overlay.color
    for anchor, position in ((3, 3), (2, 5), (5, 2)):
        canvas._text_selection_anchor, canvas._text_cursor_position = anchor, position
        canvas.update()
        qapp.processEvents()
        assert swatch.property("textColor") == "#804422CC"
        popup = _open_picker(canvas, qapp)
        assert popup.color_argb() == "#804422CC"
        popup.setColor("#FFFF0000")
        popup.reject()
        qapp.processEvents()
        assert swatch.property("textColor") == "#804422CC"
    _key(canvas, Qt.Key_Home, Qt.ControlModifier)
    qapp.processEvents()
    assert swatch.property("textColor") == "#FF111111"
    second = canvas.chapter.add_object(obj.parent_layer_id, TextObject(
        text="", text_color="#FF2266CC", layout_mode="free",
    ))
    canvas.set_selection("object", second.object_id)
    canvas.start_text_edit()
    qapp.processEvents()
    assert swatch.property("textColor") == "#FF2266CC"
    canvas.set_solo_entities({("object", object_id)})
    qapp.processEvents()
    assert not canvas._text_gizmo_overlay.isVisible()
    canvas.set_solo_entities(set())
    qapp.processEvents()
    assert canvas._text_gizmo_overlay.isVisible()


def test_color_with_no_selection_recolors_every_existing_run(text_canvas, qapp):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    apply_text_color(obj, 2, 5, "#FF00FF00")
    canvas.commit_active_text_edit()
    canvas.start_text_edit()
    canvas._text_cursor_position = canvas._text_selection_anchor = 3
    original = obj.to_dict()
    popup = _open_picker(canvas, qapp)
    popup.setColor("#804422CC")
    _apply(popup, qapp)
    assert obj.text_color == "#804422CC"
    assert obj.color_runs == []
    assert all(text_color_at(obj, i) == "#804422CC" for i in range(len(obj.text)))
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    assert canvas.chapter.objects[object_id].to_dict() == original


def test_cancel_keeps_text_selection_typing_history_and_color(text_canvas, qapp):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    old_text = obj.text
    canvas._replace_text_selection("!")
    canvas._text_selection_anchor, canvas._text_cursor_position = 1, 3
    original = obj.to_dict()
    history = copy.deepcopy(canvas._text_local_history)
    revision = canvas.command_stack.revision
    popup = _open_picker(canvas, qapp)
    popup.setColor("#FF22CC88")
    QTest.mouseClick(popup.buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
    qapp.processEvents()
    assert obj.to_dict() == original
    assert canvas._text_selection_range() == [1, 3]
    assert canvas._text_local_history == history
    assert canvas.command_stack.revision == revision
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    assert obj.text == old_text


def test_color_apply_separates_prior_typing_and_noop_adds_no_command(text_canvas, qapp):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    original_text = obj.text
    canvas._replace_text_selection("!")
    popup = _open_picker(canvas, qapp)
    popup.setColor("#FF22CC88")
    _apply(popup, qapp)
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    obj = canvas.chapter.objects[object_id]
    assert obj.text == original_text + "!"
    assert obj.text_color == "#FF111111"
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    assert canvas.chapter.objects[object_id].text == original_text
    revision = canvas.command_stack.revision
    popup = _open_picker(canvas, qapp)
    _apply(popup, qapp)
    assert canvas.command_stack.revision == revision


@pytest.mark.parametrize("accept", [False, True])
def test_text_color_eyedropper_restores_range_and_apply_cancel(qapp, monkeypatch, accept):
    monkeypatch.setattr(main_window_module, "save_settings", lambda _settings: None)
    window = MainWindow()
    chapter, obj = _chapter()
    page = chapter.layers[obj.parent_layer_id]
    sample = chapter.add_layer(page.layer_id, "Sample", BoundGeometry.rectangle(500, 80, 180, 180))
    sample.fill_color, sample.border_width = "#FF2266CC", 0
    window._set_chapter(chapter, TileStore())
    window.resize(1400, 900)
    window.show()
    canvas = window.canvas
    canvas.set_selection("object", obj.object_id)
    canvas.start_text_edit()
    canvas._text_selection_anchor, canvas._text_cursor_position = 1, 5
    canvas.center_x, canvas.center_y, canvas.scale = 400, 220, 1
    qapp.processEvents()
    try:
        revision = canvas.command_stack.revision
        popup = _open_picker(canvas, qapp)
        QTest.mouseClick(popup.workspace.panel.eyedropper, Qt.LeftButton)
        qapp.processEvents()
        assert QApplication.activeModalWidget() is None
        assert canvas.tool == ToolKind.EYEDROPPER
        point = canvas.document_to_widget(QPointF(570, 150)).toPoint()
        QTest.mouseClick(canvas, Qt.LeftButton, pos=point)
        qapp.processEvents()
        assert popup.isVisible()
        assert popup.color_argb() == "#FF2266CC"
        assert canvas.tool == ToolKind.TEXT_EDIT
        assert obj.color_runs == []
        if accept:
            _apply(popup, qapp)
        else:
            popup.reject()
            qapp.processEvents()
        assert canvas.has_active_text_edit()
        assert canvas._text_selection_range() == [1, 5]
        assert text_color_at(obj, 2) == ("#FF2266CC" if accept else "#FF111111")
        assert text_color_at(obj, 0) == "#FF111111"
        assert canvas.command_stack.revision == revision + int(accept)
    finally:
        if canvas._text_color_popup is not None:
            canvas._text_color_popup.reject()
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        canvas._effect_jobs.cancel()
        window.hide()
        window.deleteLater()


def test_typing_delete_and_local_undo_preserve_colored_unicode_ranges(text_canvas):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    obj.text = "ab😀cdef"
    apply_text_color(obj, 2, 5, "#FF2266CC")
    original = obj.to_dict()
    canvas._text_selection_anchor, canvas._text_cursor_position = 3, 4
    canvas._replace_text_selection("XYZ")
    assert obj.text == "ab😀XYZdef"
    assert [text_color_at(obj, i) for i in range(2, 7)] == ["#FF2266CC"] * 5
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    assert obj.to_dict() == original
    canvas._text_selection_anchor = canvas._text_cursor_position = 2
    _key(canvas, Qt.Key_Right)
    assert canvas._text_cursor_position == 3  # Emoji uses two Qt code units.
    _key(canvas, Qt.Key_Backspace)
    assert obj.text == "abcdef"
    assert text_color_at(obj, 2) == "#FF2266CC"
    _key(canvas, Qt.Key_Z, Qt.ControlModifier)
    assert obj.to_dict() == original


@pytest.mark.parametrize("zoom", [0.25, 2.0])
def test_line_spacing_handle_uses_screen_steps_and_undo(text_canvas, qapp, zoom):
    canvas, object_id = text_canvas
    obj = canvas.chapter.objects[object_id]
    canvas.scale = zoom
    normal_height = canvas._text_document(obj, obj.width).size().height()
    handles = {
        key: canvas.document_to_widget(point)
        for key, point in canvas._text_property_handle_positions().items()
    }
    assert set(handles) == {"font_size", "kerning", "line_spacing"}
    for key, position in handles.items():
        assert canvas._text_property_handle_hit(position) == key
    position = handles["line_spacing"]
    revision = canvas.command_stack.revision
    QTest.mousePress(canvas, Qt.LeftButton, pos=position.toPoint())
    QTest.mouseMove(canvas, (position + QPointF(8, 0)).toPoint())
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=(position + QPointF(8, 0)).toPoint())
    qapp.processEvents()
    assert obj.line_spacing == 1.2
    assert canvas._text_document(obj, obj.width).size().height() > normal_height
    assert canvas.command_stack.revision == revision + 1
    canvas.command_stack.undo()
    assert canvas.chapter.objects[object_id].line_spacing == 1.0


def test_line_spacing_handle_clamps_cancels_and_noop_is_not_an_undo(text_canvas):
    canvas, object_id = text_canvas
    position = canvas.document_to_widget(canvas._text_property_handle_positions()["line_spacing"])
    revision = canvas.command_stack.revision
    canvas._tool_press(position, 1)
    canvas._tool_release()
    assert canvas.command_stack.revision == revision
    canvas._tool_press(position, 1)
    canvas._tool_move(position + QPointF(500, 0), 1)
    assert canvas.chapter.objects[object_id].line_spacing == 3.0
    canvas._tool_move(position - QPointF(500, 0), 1)
    assert canvas.chapter.objects[object_id].line_spacing == 0.5
    _key(canvas, Qt.Key_Escape)
    assert canvas.chapter.objects[object_id].line_spacing == 1.0
    assert canvas.command_stack.revision == revision
