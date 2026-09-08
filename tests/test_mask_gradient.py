from __future__ import annotations

import json

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QTransform
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorGradientStop, ParameterMaskBinding,
    SeriesDocument, ShapeStyle, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _document():
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 400))
    layer = chapter.add_layer(
        page.layer_id, "Color", BoundGeometry.rectangle(30, 30, 440, 340),
        style=ShapeStyle(primary_color="#FFFF4020", outline_thickness=0),
    )
    mask = ToneMask(saved=True, name="Fade")
    other = ToneMask(saved=True, name="Other")
    chapter.masks = {mask.mask_id: mask, other.mask_id: other}
    layer.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    return chapter, page, mask, other


@pytest.fixture
def canvas(qapp):
    widget = CanvasWidget(EditorSettings(snap_to_grid=False))
    chapter, _, mask, _ = _document()
    widget.resize(500, 400)
    widget.set_document(chapter, TileStore())
    widget.scale = 1
    widget.center_x, widget.center_y = 250, 200
    widget.set_tone_mask_mode(mask.mask_id)
    widget.set_tool(ToolKind.GRADIENT)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()


def _drag(canvas, start=(100, 200), end=(400, 200)):
    first = canvas.document_to_widget(QPointF(*start)).toPoint()
    last = canvas.document_to_widget(QPointF(*end)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, pos=first)
    QTest.mouseMove(canvas, last)
    QTest.mouseRelease(canvas, Qt.LeftButton, pos=last)


def _field(canvas):
    return canvas.render_tone_mask_field(
        canvas.active_tone_mask_id, 500, 400, QTransform(), QRectF(0, 0, 500, 400),
    )


def test_mask_gradient_mouse_creation_render_export_and_round_trip(canvas):
    _drag(canvas)
    gradient = canvas.active_mask_gradient()
    assert gradient is not None
    assert gradient.object_id not in canvas.chapter.objects
    assert gradient.mask_only
    assert [n.position for n in gradient.line_field.geometry.nodes] == [(100, 200), (400, 200)]
    assert len(canvas._gradient_control_points(gradient)) == 2
    canvas._update_interaction_cursor(canvas.document_to_widget(QPointF(100, 200)))
    assert canvas.cursor().shape() == Qt.PointingHandCursor
    assert _field(canvas)[200, [50, 250, 450]] == pytest.approx([0, .5, 1], abs=.01)
    restored = ChapterDocument.from_dict(json.loads(json.dumps(canvas.chapter.to_dict())))
    assert restored.masks[canvas.active_tone_mask_id].gradient.to_dict() == gradient.to_dict()
    image = QImage(canvas.chapter.width, 400, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    assert image.pixelColor(450, 150).red() > image.pixelColor(450, 150).green()
    assert image.pixelColor(50, 150) != image.pixelColor(450, 150)


def test_mask_gradient_endpoint_edit_undo_redo_cancel_and_redraw(canvas):
    _drag(canvas)
    original = canvas.active_mask_gradient().to_dict()
    _drag(canvas, (400, 200), (350, 250))
    assert canvas.active_mask_gradient().line_field.geometry.nodes[1].position == (350, 250)
    canvas.command_stack.undo()
    assert canvas.active_mask_gradient().to_dict() == original
    canvas.command_stack.redo()
    assert canvas.active_mask_gradient().line_field.geometry.nodes[1].position == (350, 250)
    before = canvas.chapter.to_dict()
    canvas._tool_press(canvas.document_to_widget(QPointF(350, 250)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(390, 290)), 1)
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.to_dict() == before
    _drag(canvas, (150, 100), (150, 300))
    assert [n.position for n in canvas.active_mask_gradient().line_field.geometry.nodes] == [(150, 100), (150, 300)]
    assert canvas.active_mask_gradient().object_id == original["id"]
    assert len(canvas.active_mask_gradient().line_field.geometry.nodes) == 2


def test_mask_gradient_click_is_noop_and_paint_coexists(canvas):
    QTest.mouseClick(canvas, Qt.LeftButton, pos=canvas.document_to_widget(QPointF(100, 200)).toPoint())
    assert canvas.active_mask_gradient() is None
    assert not canvas.command_stack.can_undo
    _drag(canvas)
    assert canvas.set_tool(ToolKind.RASTER_PENCIL)
    _drag(canvas, (50, 100), (60, 100))
    assert canvas.tiles.object_tiles(canvas.active_tone_mask_id)
    assert _field(canvas)[100, 50] > .9
    assert _field(canvas)[200, 50] < .01


def test_mask_gradient_hard_stops_and_transformed_sampling(canvas):
    _drag(canvas)
    gradient = canvas.active_mask_gradient()
    gradient.ramp.stops = [
        ColorGradientStop(position=0, color="#00FFFFFF"),
        ColorGradientStop(position=.5, color="#00FFFFFF"),
        ColorGradientStop(position=.5, color="#FFFFFFFF"),
        ColorGradientStop(position=1, color="#FFFFFFFF"),
    ]
    field = _field(canvas)
    assert field[200, [200, 249, 251, 300]] == pytest.approx([0, 0, 1, 1])
    transform = QTransform(0, 1, -1, 0, 400, 0)
    rotated = canvas.render_tone_mask_field(
        canvas.active_tone_mask_id, 400, 500, transform, QRectF(0, 0, 500, 400),
    )
    assert rotated[[200, 249, 251, 300], 200] == pytest.approx([0, 0, 1, 1])


def test_switching_masks_during_drag_commits_to_original_mask(canvas):
    original_id = canvas.active_tone_mask_id
    other_id = next(key for key in canvas.chapter.masks if key != original_id)
    canvas._tool_press(canvas.document_to_widget(QPointF(100, 200)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(400, 200)), 1)
    canvas.set_tone_mask_mode(other_id)
    canvas._tool_release()
    assert canvas.chapter.masks[original_id].gradient is not None
    assert canvas.chapter.masks[other_id].gradient is None
    canvas.command_stack.undo()
    assert canvas.chapter.masks[original_id].gradient is None
    canvas.command_stack.redo()
    assert canvas.chapter.masks[original_id].gradient is not None


def test_cancel_new_gradient_leaves_mask_and_history_unchanged(canvas):
    before = canvas.chapter.to_dict()
    canvas._tool_press(canvas.document_to_widget(QPointF(100, 200)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(400, 200)), 1)
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.to_dict() == before
    assert not canvas.command_stack.can_undo


def test_mask_gradient_visibility_is_scoped_to_exact_mask(canvas, monkeypatch):
    _drag(canvas)
    mask_id = canvas.active_tone_mask_id
    gradient_id = canvas.active_mask_gradient().object_id
    drawn = []
    original_draw = canvas._draw_mask_gradient_handles
    def record(painter, obj):
        drawn.append(obj.object_id)
        original_draw(painter, obj)
    monkeypatch.setattr(canvas, "_draw_mask_gradient_handles", record)
    canvas.grab()
    assert drawn == [gradient_id]
    drawn.clear()
    other_id = next(key for key in canvas.chapter.masks if key != mask_id)
    canvas.set_tone_mask_mode(other_id)
    canvas.grab()
    assert drawn == []
    assert canvas.active_mask_gradient() is None
    canvas.set_tone_mask_mode("")
    canvas.grab()
    assert drawn == []
    canvas.set_tone_mask_mode(mask_id)
    canvas.set_tool(ToolKind.GRADIENT)
    canvas.grab()
    assert drawn == [gradient_id]


def test_mask_gradient_settings_and_mask_switching(qapp):
    window = MainWindow()
    chapter, page, mask, other = _document()
    window.series = SeriesDocument()
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    controls = window.gradient_tools_controls
    try:
        window._enter_mask_mode(mask.mask_id)
        assert window.tool_buttons[ToolKind.GRADIENT].isEnabled()
        assert window._activate_tool(ToolKind.GRADIENT)
        assert controls.create_color.isEnabled()
        _drag(window.canvas)
        gradient = window.canvas.active_mask_gradient()
        assert controls.selected_gradient() is gradient
        assert not window.gradient_parameters_group.isHidden()
        assert controls.direction_row.isHidden()
        assert not controls.field_type.isEnabled()
        controls.reverse_direction.click()
        assert _field(window.canvas)[200, [50, 450]] == pytest.approx([1, 0], abs=.01)
        controls._add_stop()
        assert len(gradient.ramp.stops) == 3
        window._enter_mask_mode(other.mask_id)
        assert controls.selected_gradient() is None
        assert window.gradient_parameters_group.isHidden()
        window._enter_mask_mode(mask.mask_id)
        assert controls.selected_gradient().object_id == gradient.object_id
        window._finish_mask_mode(True)
        assert controls.selected_gradient() is None
        assert window.gradient_parameters_group.isHidden()
    finally:
        window._dirty = False
        window.close()
        window.deleteLater()
