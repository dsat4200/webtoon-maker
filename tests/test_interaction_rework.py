from __future__ import annotations

import pytest
from PySide6.QtCore import QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QInputMethodEvent, QPolygonF, QPointingDevice, QTabletEvent,
)
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, OutlineModifier, RasterObject, TextObject,
    VectorDrawingObject, VectorStroke, VectorStrokePoint,
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
    canvas = CanvasWidget(settings or EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    return canvas, chapter, page, layer


def _widget(canvas, x: float, y: float) -> QPointF:
    return canvas.document_to_widget(QPointF(x, y))


class _TouchPoint:
    def __init__(self, position: QPointF):
        self._position = position

    def position(self) -> QPointF:
        return QPointF(self._position)


class _TouchEvent:
    def __init__(self, event_type, positions):
        self._type = event_type
        self._points = [_TouchPoint(point) for point in positions]
        self.accepted = False

    def type(self):
        return self._type

    def points(self):
        return self._points

    def accept(self):
        self.accepted = True


def _tablet_event(event_type, position, pressure, button, buttons):
    return QTabletEvent(
        event_type, QPointingDevice.primaryPointingDevice(),
        position, position, pressure,
        0.0, 0.0, 0.0, 0.0, 0.0,
        Qt.NoModifier, button, buttons,
    )


def test_bound_center_drag_translates_shape_subtree_and_is_undoable(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(layer.layer_id, RasterObject(x=90, y=80))
    original_points = list(layer.bound.points)
    original_object_position = (raster.x, raster.y)
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.BOUND_EDIT)
    canvas._tool_press(_widget(canvas, 200, 200), 1)
    canvas._tool_move(_widget(canvas, 230, 220), 1)
    canvas._tool_release()
    assert layer.bound.points == original_points
    assert (layer.translate_x, layer.translate_y) == pytest.approx((30, 20))
    assert (raster.x, raster.y) == original_object_position
    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id]
    assert restored.bound.points == original_points
    assert (restored.translate_x, restored.translate_y) == (0, 0)


def test_raster_selection_transform_renders_live_without_mutating_tiles(qapp):
    canvas, chapter, _page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id, RasterObject(x=100, y=100)
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(100, 100), 100,
        QColor("#ffcc3311"), square=True, antialias=False,
    )
    outline = OutlineModifier(thickness=5, color="#ff111111")
    chapter.modifiers[outline.modifier_id] = outline
    raster.modifier_ids = [outline.modifier_id]
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    assert canvas.select_all_drawing()
    canvas.scale = 1.0
    canvas.center_x = 450
    canvas.center_y = 350
    canvas.show()
    qapp.processEvents()

    source = QPointF(180, 180)
    destination = source + QPointF(120, 0)
    quad = list(canvas._selection_transform_quad)
    pivot = canvas._selection_pivot
    press = pivot + QPointF(25, 0)
    before_model = chapter.to_dict()
    before_tiles = {
        key: int(image.cacheKey())
        for key, image in canvas.tiles.object_tiles(
            raster.object_id
        ).items()
    }
    before_image = canvas.grab().toImage()
    source_widget = canvas.document_to_widget(source).toPoint()
    destination_widget = canvas.document_to_widget(destination).toPoint()
    source_color = before_image.pixelColor(source_widget)
    destination_color = before_image.pixelColor(destination_widget)
    assert source_color != destination_color

    canvas._tool_press(canvas.document_to_widget(press), 1.0)
    canvas._tool_move(
        canvas.document_to_widget(press + QPointF(120, 0)), 1.0
    )
    qapp.processEvents()
    preview = canvas.grab().toImage()

    assert chapter.to_dict() == before_model
    assert {
        key: int(image.cacheKey())
        for key, image in canvas.tiles.object_tiles(
            raster.object_id
        ).items()
    } == before_tiles
    assert preview.pixelColor(source_widget) == destination_color
    assert preview.pixelColor(destination_widget) == source_color
    assert canvas._selection_transform_quad == pytest.approx([
        (x + 120, y) for x, y in quad
    ])

    canvas._tool_release()
    committed = canvas.grab().toImage()
    assert committed.pixelColor(source_widget) == destination_color
    assert committed.pixelColor(destination_widget) == source_color
    canvas.command_stack.undo()
    restored = canvas.grab().toImage()
    assert restored.pixelColor(source_widget) == source_color
    assert restored.pixelColor(destination_widget) == destination_color


@pytest.mark.parametrize("drawing_kind", ["raster", "vector"])
def test_tablet_selection_translation_uses_open_and_closed_hand_cursors(
    qapp, drawing_kind,
):
    canvas, chapter, _page, layer = _canvas_document()
    if drawing_kind == "raster":
        drawing = chapter.add_object(
            layer.layer_id, RasterObject(x=80, y=70)
        )
        canvas.tiles.paint_dab(
            drawing.object_id, QPointF(100, 100), 120,
            QColor("#ff2255aa"), square=True, antialias=False,
        )
    else:
        drawing = chapter.add_object(
            layer.layer_id,
            VectorDrawingObject(strokes=[VectorStroke(points=[
                VectorStrokePoint(x=100, y=100, width=12),
                VectorStrokePoint(x=220, y=100, width=12),
                VectorStrokePoint(x=220, y=210, width=12),
                VectorStrokePoint(x=100, y=210, width=12),
            ])]),
        )
    canvas.set_selection("object", drawing.object_id)
    canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    assert canvas.select_all_drawing()
    canvas.scale = 1.0
    quad = list(canvas._selection_transform_quad)
    center = QPolygonF([QPointF(*point) for point in quad]).boundingRect().center()
    press_world = center * 0.7 + QPointF(*quad[0]) * 0.3
    press = canvas.document_to_widget(press_world)
    moved = press + QPointF(35, 22)

    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, press, 0.0, Qt.NoButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletPress, press, 0.5, Qt.LeftButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, moved, 0.5, Qt.NoButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletRelease, moved, 0.0, Qt.LeftButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_tablet_shape_translation_uses_hands_but_handle_stays_precise(qapp):
    canvas, _chapter, _page, layer = _canvas_document()
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas.scale = 1.0
    corner = canvas.document_to_widget(QPointF(*layer.bound.points[0]))
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, corner, 0.0, Qt.NoButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.CrossCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletPress, corner, 0.5, Qt.LeftButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.CrossCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletRelease, corner, 0.0, Qt.LeftButton, Qt.NoButton,
    ))

    interior_world = QPointF(260, 260)
    interior = canvas.document_to_widget(interior_world)
    moved = interior + QPointF(30, 20)
    original = list(layer.bound.points)
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, interior, 0.0, Qt.NoButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletPress, interior, 0.5, Qt.LeftButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, moved, 0.5, Qt.NoButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert layer.bound.points == original
    assert (layer.translate_x, layer.translate_y) == (30, 20)
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletRelease, moved, 0.0, Qt.LeftButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_tablet_layer_transform_cage_translation_uses_hand_cursors(qapp):
    canvas, _chapter, _page, layer = _canvas_document()
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.scale = 1.0
    quad, _kind = canvas._active_transform_cage()
    center = QPolygonF([QPointF(*point) for point in quad]).boundingRect().center()
    press_world = center * 0.75 + QPointF(*quad[0]) * 0.25
    press = canvas.document_to_widget(press_world)
    moved = press + QPointF(40, 25)

    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, press, 0.0, Qt.NoButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletPress, press, 0.5, Qt.LeftButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletMove, moved, 0.5, Qt.NoButton, Qt.LeftButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert canvas._transform_preview_quad != canvas._transform_start_quad
    canvas.tabletEvent(_tablet_event(
        QEvent.TabletRelease, moved, 0.0, Qt.LeftButton, Qt.NoButton,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor


def test_tablet_horizontal_pan_matches_canvas_grab_direction(qapp):
    canvas, chapter, page, layer = _canvas_document()
    canvas.scale = 1
    canvas.center_x = 500
    canvas._apply_touch_pan_delta(20, 0)
    assert canvas.center_x == 480
    canvas._begin_navigation("pan", QPointF(100, 100))
    canvas._update_navigation(QPointF(120, 100))
    assert canvas.center_x == 460


def test_tablet_touch_pan_zoom_rotation_preserves_centroid_anchor(qapp):
    canvas, chapter, page, layer = _canvas_document()
    canvas.resize(600, 600)
    canvas.center_x = 300
    canvas.center_y = 300
    canvas.scale = 1
    canvas.rotation = 0

    canvas._rebase_touch_navigation([QPointF(100, 100)])
    canvas._apply_touch_navigation([QPointF(130, 120)])
    assert (canvas.center_x, canvas.center_y) == pytest.approx((270, 280))

    canvas.center_x = 300
    canvas.center_y = 300
    canvas.scale = 1
    canvas.rotation = 0
    start = [QPointF(100, 100), QPointF(200, 100)]
    anchor = canvas.widget_to_document(QPointF(150, 100))
    canvas._rebase_touch_navigation(start)
    state = canvas._apply_touch_navigation([
        QPointF(100, 100), QPointF(100, 300),
    ])
    assert state[2] == pytest.approx(90)
    assert state[3] == pytest.approx(2)
    assert canvas.document_to_widget(anchor) == QPointF(100, 200)

    before = state
    canvas._rebase_touch_navigation([QPointF(100, 200)])
    assert (
        canvas.center_x, canvas.center_y, canvas.rotation, canvas.scale
    ) == before


def test_touch_navigation_live_renders_without_viewport_snapshot(
    qapp, monkeypatch,
):
    canvas, _chapter, _page, _layer = _canvas_document(
        EditorSettings(tablet_mode=True, snap_to_grid=False)
    )
    canvas.center_x = 300
    canvas.center_y = 300
    canvas.rotation = 0
    canvas.scale = 1

    monkeypatch.setattr(
        type(canvas), "grab",
        lambda _self: pytest.fail(
            "touch navigation must not capture the rectangular viewport"
        ),
    )
    begin = _TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100), QPointF(200, 100)]
    )
    update = _TouchEvent(
        QEvent.TouchUpdate, [QPointF(100, 100), QPointF(100, 300)]
    )
    end = _TouchEvent(QEvent.TouchEnd, [])

    assert canvas._touch_event(begin)
    assert canvas._touch_event(update)
    canvas._flush_touch_navigation()
    assert canvas.rotation == pytest.approx(90)
    assert canvas.scale == pytest.approx(2)
    assert canvas._touch_event(end)
    assert begin.accepted and update.accepted and end.accepted


def test_pen_hover_keeps_touch_pan_zoom_and_rotation_active(qapp):
    canvas, _chapter, _page, _layer = _canvas_document(
        EditorSettings(tablet_mode=True, snap_to_grid=False)
    )
    canvas.resize(600, 600)
    canvas.center_x = canvas.center_y = 300
    canvas.scale = 1
    canvas.rotation = 0

    hover = _tablet_event(
        QEvent.TabletMove, QPointF(250, 250), 0.0,
        Qt.NoButton, Qt.NoButton,
    )
    canvas.tabletEvent(hover)
    assert canvas._tablet_hover_widget == QPointF(250, 250)
    assert not canvas._pen_contact_active

    canvas._touch_event(_TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100)],
    ))
    canvas._touch_event(_TouchEvent(
        QEvent.TouchUpdate, [QPointF(130, 120)],
    ))
    canvas._flush_touch_navigation()
    assert (canvas.center_x, canvas.center_y) == pytest.approx((270, 280))
    canvas._touch_event(_TouchEvent(QEvent.TouchEnd, []))

    canvas.center_x = canvas.center_y = 300
    canvas.scale = 1
    canvas.rotation = 0
    canvas._touch_event(_TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100), QPointF(200, 100)],
    ))
    canvas._touch_event(_TouchEvent(
        QEvent.TouchUpdate, [QPointF(100, 100), QPointF(100, 300)],
    ))
    canvas._flush_touch_navigation()
    assert canvas.rotation == pytest.approx(90)
    assert canvas.scale == pytest.approx(2)


def test_pen_contact_cancels_and_blocks_touch_until_release(qapp):
    canvas, _chapter, _page, _layer = _canvas_document(
        EditorSettings(tablet_mode=True, snap_to_grid=False)
    )
    canvas.resize(600, 600)
    canvas.center_x = canvas.center_y = 300
    canvas.scale = 1
    canvas.rotation = 0

    canvas._touch_event(_TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100)],
    ))
    canvas._touch_event(_TouchEvent(
        QEvent.TouchUpdate, [QPointF(140, 130)],
    ))
    assert canvas._touch_pending_points is not None

    canvas.tabletEvent(_tablet_event(
        QEvent.TabletPress, QPointF(250, 250), 0.5,
        Qt.LeftButton, Qt.LeftButton,
    ))
    assert canvas._pen_contact_active
    assert canvas._touch_pending_points is None
    assert not canvas._touch_anchor_points

    before = (canvas.center_x, canvas.center_y, canvas.rotation, canvas.scale)
    canvas._touch_event(_TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100)],
    ))
    canvas._touch_event(_TouchEvent(
        QEvent.TouchUpdate, [QPointF(180, 160)],
    ))
    canvas._flush_touch_navigation()
    assert (canvas.center_x, canvas.center_y, canvas.rotation, canvas.scale) == before

    canvas.tabletEvent(_tablet_event(
        QEvent.TabletRelease, QPointF(250, 250), 0.0,
        Qt.NoButton, Qt.NoButton,
    ))
    assert not canvas._pen_contact_active

    canvas._touch_event(_TouchEvent(
        QEvent.TouchBegin, [QPointF(100, 100)],
    ))
    canvas._touch_event(_TouchEvent(
        QEvent.TouchUpdate, [QPointF(130, 120)],
    ))
    canvas._flush_touch_navigation()
    assert (canvas.center_x, canvas.center_y) == pytest.approx((270, 280))


def test_tablet_navigation_configures_top_level_window(qapp, monkeypatch):
    canvas, _chapter, _page, _layer = _canvas_document(
        EditorSettings(tablet_mode=True, snap_to_grid=False)
    )
    calls = []
    monkeypatch.setattr(
        "comic_editor.ui.canvas.configure_simultaneous_pen_touch",
        lambda hwnd, enabled: calls.append((hwnd, enabled)) or True,
    )

    assert canvas.configure_tablet_navigation()
    assert calls == [(int(canvas.window().winId()), True)]
    assert canvas.configure_tablet_navigation()
    assert len(calls) == 1


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


def test_free_text_alignment_uses_local_projective_transform_rect(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            text="Aligned",
            layout_mode="free",
            width=240,
            height=180,
            vertical_alignment="bottom",
            horizontal_alignment="right",
            transform_quad=[
                (70, 60), (340, 80), (300, 280), (90, 230),
            ],
        ),
    )
    source = QRectF(0, 0, text.width, text.height)
    document = canvas._text_document(text, source.width())
    offset = canvas._text_vertical_offset(text, document, source.height())
    base = canvas._quad_transform(source, text.transform_quad)
    _, origin, aligned = canvas._text_edit_layout(text)
    assert origin == QPointF(0, 0)
    assert offset > 0
    assert aligned.map(QPointF(0, 0)) == base.map(QPointF(0, offset))
    assert document.begin().blockFormat().alignment() & Qt.AlignRight


def test_transform_double_click_reenters_text_edit_at_clicked_position(qapp):
    canvas, chapter, page, layer = _canvas_document()
    text = chapter.add_object(
        layer.layer_id,
        TextObject(
            text="Double click me",
            layout_mode="free",
            width=240,
            height=100,
            transform_quad=[(80, 80), (320, 80), (320, 180), (80, 180)],
        ),
    )
    canvas.set_selection("object", text.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.show()
    qapp.processEvents()
    QTest.mouseDClick(
        canvas, Qt.LeftButton,
        pos=canvas.document_to_widget(QPointF(180, 110)).toPoint(),
    )
    assert canvas.tool == ToolKind.TEXT_EDIT
    assert canvas._text_editing
    assert 0 <= canvas._text_cursor_position <= len(text.text)
    canvas.hide()


def test_transform_handle_uses_global_grid_snap(qapp):
    settings = EditorSettings(snap_to_grid=True)
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
    canvas.command_stack.undo()
    canvas.settings.snap_to_grid = False
    canvas._tool_press(_widget(canvas, 80, 80), 1)
    canvas._tool_move(_widget(canvas, 61, 67), 1)
    canvas._tool_release()
    assert canvas.chapter.objects[text.object_id].transform_quad[0] == (61, 67)


def test_uniform_transform_preserves_quad_proportions(qapp):
    settings = EditorSettings(transform_mode="uniform", snap_to_grid=False)
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


def test_sparse_raster_translation_preserves_tiles_and_uses_standard_undo(
    qapp, monkeypatch,
):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(layer.layer_id, RasterObject(x=100, y=100))
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 20, QColor("#111111")
    )
    original_bounds = canvas.tiles.content_bounds(raster.object_id)
    original_tiles = {
        key: image.cacheKey()
        for key, image in canvas.tiles.iter_tiles(raster.object_id)
    }
    monkeypatch.setattr(
        canvas.tiles,
        "projective_transform",
        lambda *args, **kwargs: pytest.fail(
            "translation should not bake raster tiles"
        ),
    )
    canvas.set_selection("object", raster.object_id)
    assert canvas.tool == ToolKind.RASTER_PENCIL
    canvas.settings.pencil_transform_handles_visible = True
    rect = canvas.object_world_rect(raster.object_id)
    edge = QPointF(rect.left() + rect.width() * 0.25, rect.top())
    canvas._tool_press(canvas.document_to_widget(edge), 1)
    canvas._tool_move(canvas.document_to_widget(edge + QPointF(100, 50)), 1)
    canvas._tool_release()
    moved = canvas.chapter.objects[raster.object_id]
    assert (moved.x, moved.y) == pytest.approx((200, 150))
    assert canvas.tiles.content_bounds(raster.object_id) == original_bounds
    assert {
        key: image.cacheKey()
        for key, image in canvas.tiles.iter_tiles(raster.object_id)
    } == original_tiles
    canvas.command_stack.undo()
    restored = canvas.chapter.objects[raster.object_id]
    assert (restored.x, restored.y) == pytest.approx((100, 100))
    canvas.command_stack.redo()
    redone = canvas.chapter.objects[raster.object_id]
    assert (redone.x, redone.y) == pytest.approx((200, 150))


def test_raster_pencil_expands_frame_with_24px_content_margin(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(0, 0, 40, 40)),
    )
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_stroke(QPointF(100, 90), 1)
    canvas._end_stroke()

    content = canvas.tiles.content_bounds(raster.object_id)
    expected = content.adjusted(-24, -24, 24, 24)
    frame = QRectF(*raster.interaction_rect)
    assert frame.contains(QRectF(0, 0, 40, 40))
    assert frame == frame.united(expected)
    assert frame.left() <= content.left() - 24
    assert frame.right() >= content.right() + 24


def test_transformed_raster_fill_is_local_tile_aware_and_undoable(qapp):
    settings = EditorSettings(snap_to_grid=False, raster_fill_tolerance=0)
    canvas, chapter, _page, layer = _canvas_document(settings)
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(
            x=50, y=60,
            interaction_rect=(0, 0, 16, 8),
            transform_frame=(50, 60, 16, 8),
            transform_quad=[
                (100, 120), (132, 120), (132, 136), (100, 136),
            ],
        ),
    )
    canvas.set_active_colors("#FF336699", "#FFFFFFFF")
    canvas.set_selection("object", raster.object_id)

    assert canvas.set_tool(ToolKind.FILL)
    world_seed = canvas._raster_world_point(raster, QPointF(3, 3))
    assert canvas._apply_raster_fill(raster, world_seed)
    tile = canvas.tiles.tile(raster.object_id, (0, 0))
    assert tile.pixelColor(3, 3) == QColor("#336699")
    assert raster.transform_quad == [
        (100, 120), (132, 120), (132, 136), (100, 136),
    ]
    assert canvas.command_stack.can_undo

    canvas.command_stack.undo()
    assert canvas.tiles.tile(raster.object_id, (0, 0)) is None
    canvas.command_stack.redo()
    assert canvas.tiles.tile(raster.object_id, (0, 0)).pixelColor(
        3, 3
    ) == QColor("#336699")


def test_eraser_recalculates_exact_padded_bounds_and_undoes_frame(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(-30, -30, 240, 240)),
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(40, 40), 10, QColor("#111111")
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(160, 160), 10, QColor("#111111")
    )
    original_bounds = canvas.tiles.content_bounds(raster.object_id)
    original_frame = raster.interaction_rect
    canvas.settings.eraser_size_px["medium"] = 40
    canvas.settings.active_eraser_size = "medium"
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._begin_stroke(QPointF(40, 40), 1)
    canvas._end_stroke()

    remaining = canvas.tiles.content_bounds(raster.object_id)
    padded = remaining.adjusted(-24, -24, 24, 24)
    assert QRectF(*raster.interaction_rect) == padded
    canvas.command_stack.undo()
    restored = canvas.chapter.objects[raster.object_id]
    assert restored.interaction_rect == original_frame
    assert canvas.tiles.content_bounds(raster.object_id) == original_bounds
    canvas.command_stack.redo()
    assert QRectF(*canvas.chapter.objects[raster.object_id].interaction_rect) == padded


def test_erasing_all_raster_pixels_keeps_pre_stroke_frame(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(-20, -10, 100, 90)),
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 8, QColor("#111111")
    )
    original_frame = raster.interaction_rect
    canvas.settings.eraser_size_px["medium"] = 40
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._begin_stroke(QPointF(20, 20), 1)
    canvas._end_stroke()
    assert canvas.tiles.content_bounds(raster.object_id) is None
    assert raster.interaction_rect == original_frame


def test_raster_handle_transform_still_bakes_and_undoes(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(
            x=100, y=100, interaction_rect=(0, 0, 120, 120)
        ),
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 20, QColor("#111111")
    )
    original_bounds = canvas.tiles.content_bounds(raster.object_id)
    canvas.set_selection("object", raster.object_id)
    assert canvas.tool == ToolKind.RASTER_PENCIL
    assert canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(_widget(canvas, 100, 100), 1)
    canvas._tool_move(_widget(canvas, 80, 80), 1)
    canvas._tool_release()

    transformed = canvas.chapter.objects[raster.object_id]
    assert (transformed.x, transformed.y) == (0, 0)
    assert transformed.interaction_rect[:2] == pytest.approx((80, 80))
    canvas.command_stack.undo()
    restored = canvas.chapter.objects[raster.object_id]
    assert (restored.x, restored.y) == (100, 100)
    assert canvas.tiles.content_bounds(raster.object_id) == original_bounds


def test_raster_transform_controls_do_not_consume_interior_pencil(qapp):
    canvas, chapter, page, layer = _canvas_document()
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(
            x=100, y=100, interaction_rect=(0, 0, 160, 160)
        ),
    )
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    # The exact center is the explicit pivot handle. A normal interior point
    # must continue to paint through the cage.
    interior = _widget(canvas, 145, 145)
    canvas._tool_press(interior, 1)
    assert canvas._drawing
    assert canvas._transform_drag_mode is None
    canvas._tool_release()
    assert canvas.tiles.content_bounds(raster.object_id) is not None


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
    QTest.keyRelease(canvas, Qt.Key_Control)
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
    object_id = next(iter(chapter.objects))
    assert window.canvas.selected_kind == "object"
    assert window.canvas.selected_id == object_id
    assert window.canvas.tool == ToolKind.TEXT_EDIT
    assert window.canvas.has_active_text_edit()
    assert window.canvas._text_selection_range() == [0, 4]
    refreshed = window.hierarchy_model.index_for_entity("layer", layer.layer_id)
    assert not window.tree.isExpanded(refreshed)
    assert window.hierarchy_model.data(refreshed, Qt.BackgroundRole).name() == "#303238"
    object_index = window.hierarchy_model.index_for_entity("object", object_id)
    assert window.hierarchy_model.data(object_index, Qt.BackgroundRole).name() == "#050505"
    assert window.tool_buttons[ToolKind.RASTER_PENCIL].isHidden()
    window.show()
    window.canvas.set_selection("object", object_id)
    assert not window.tool_buttons[ToolKind.TEXT_EDIT].isHidden()
    qapp.processEvents()
    assert not hasattr(window, "inspector")
    assert window.ribbon.current_key() == "tool_settings"
    assert window.text_object_group.isVisible()
    window._activate_tool(ToolKind.OBJECT_SELECT)
    assert not hasattr(window, "inspector")
    window.hide()
    window.deleteLater()


def test_reset_rotation_preserves_navigation_center_and_scale(qapp):
    canvas, _chapter, _page, _layer = _canvas_document()
    canvas.center_x = 321.25
    canvas.center_y = 654.75
    canvas.scale = 1.75
    canvas.rotation = 37.0
    canvas.reset_rotation()
    assert canvas.rotation == 0.0
    assert canvas.scale == pytest.approx(1.75)
    assert (canvas.center_x, canvas.center_y) == pytest.approx(
        (321.25, 654.75)
    )


def test_eyedropper_samples_normal_compositor_without_overlays(qapp):
    canvas, chapter, page, _layer = _canvas_document()
    raster = chapter.add_object(
        page.layer_id, RasterObject(x=100, y=100)
    )
    canvas.tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 30, QColor("#FF336699")
    )
    assert canvas.sample_composited_color(QPointF(120, 120)) == "#FF336699"

    canvas.set_tool(ToolKind.EYEDROPPER)
    pointer = canvas.document_to_widget(QPointF(120, 120))
    canvas._tool_press(pointer, 1.0)
    assert canvas._eyedropper_sampling
    assert canvas._eyedropper_widget_point == pointer
    assert canvas._eyedropper_last_color == "#FF336699"
    moved = pointer + QPointF(9, 7)
    canvas._tool_move(moved, 1.0)
    assert canvas._eyedropper_widget_point == moved
    canvas._tool_release()
    assert not canvas._eyedropper_sampling
    assert canvas._eyedropper_widget_point is None
