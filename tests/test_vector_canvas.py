from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QGuiApplication, QImage, QPainter, QPainterPath
import pytest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.core.vector_geometry import stroke_cubics
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def _canvas_with_drawing(strokes=None):
    chapter = ChapterDocument()
    page = chapter.add_page()
    drawing = chapter.add_object(
        page.layer_id,
        VectorDrawingObject(strokes=list(strokes or [])),
    )
    canvas = CanvasWidget(EditorSettings(predictive_ink=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", drawing.object_id)
    return canvas, chapter, drawing


def _stroke(*points):
    return VectorStroke(
        points=[
            VectorStrokePoint(x=x, y=y, width=width, opacity=opacity)
            for x, y, width, opacity in points
        ]
    )


def test_vector_pencil_creates_pressure_stroke_and_undoes(qapp):
    canvas, chapter, drawing = _canvas_with_drawing()
    canvas.set_active_colors("#80402010", "#FFFFFFFF")
    canvas._tool_press(
        canvas.document_to_widget(QPointF(100, 100)), 0.25
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(150, 120)), 0.6
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(220, 180)), 1.0
    )
    canvas._tool_release()

    assert len(drawing.strokes) == 1
    assert drawing.strokes[0].color == "#80402010"
    assert len(drawing.strokes[0].points) >= 2
    assert drawing.strokes[0].points[-1].width >= (
        drawing.strokes[0].points[0].width
    )
    canvas.command_stack.undo()
    assert not drawing.strokes
    canvas.command_stack.redo()
    assert len(drawing.strokes) == 1


def test_vector_pencil_preview_and_commit_follow_persistent_transform(qapp):
    original = _stroke((0, 0, 6, 1), (100, 80, 6, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([original])
    drawing.transform_frame = (0, 0, 100, 80)
    drawing.transform_quad = [
        (140, 120), (310, 105), (325, 255), (125, 235),
    ]
    canvas.center_x = 450
    canvas.center_y = 350
    canvas.scale = 1.0
    canvas.rotation = 0.0
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    object_transform = canvas._drawing_object_transform(drawing)
    first_local = QPointF(25, 25)
    second_local = QPointF(45, 45)
    first_world = object_transform.map(first_local)
    second_world = object_transform.map(second_local)

    canvas._tool_press(canvas.document_to_widget(first_world), 1.0)
    assert canvas._vector_gesture_mode == "pencil"
    assert canvas._pending_vector_press is None
    canvas._tool_move(canvas.document_to_widget(second_world), 1.0)

    image = QImage(900, 700, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setTransform(canvas.camera_transform())
    canvas._draw_live_vector_gesture(painter)
    painter.end()
    expected = canvas.document_to_widget(second_world).toPoint()
    assert any(
        image.pixelColor(expected.x() + dx, expected.y() + dy).alpha() > 0
        for dx in range(-3, 4) for dy in range(-3, 4)
    )

    canvas._tool_release()
    created = drawing.strokes[-1]
    assert created.points[0].position == pytest.approx(first_local.toTuple())
    assert created.points[-1].position == pytest.approx(second_local.toTuple())
    assert drawing.transform_frame == (0, 0, 100, 80)
    assert drawing.transform_quad == [
        (140, 120), (310, 105), (325, 255), (125, 235),
    ]
    canvas.command_stack.undo()
    assert len(drawing.strokes) == 1
    canvas.command_stack.redo()
    assert len(drawing.strokes) == 2


def test_vector_pencil_preserves_zero_pressure_from_capable_pen(qapp):
    canvas, _chapter, drawing = _canvas_with_drawing()
    canvas._device_supports_pressure = True
    canvas._tool_press(
        canvas.document_to_widget(QPointF(100, 100)), 0.0
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(160, 120)), 0.5
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(220, 140)), 1.0
    )
    canvas._tool_release()

    stroke = drawing.strokes[0]
    assert stroke.points[0].width < stroke.points[-1].width
    assert stroke.points[0].opacity < stroke.points[-1].opacity


def test_vector_pencil_tap_creates_round_dot(qapp):
    canvas, _chapter, drawing = _canvas_with_drawing()
    canvas._tool_press(
        canvas.document_to_widget(QPointF(160, 140)), 0.8
    )
    canvas._tool_release()
    assert len(drawing.strokes) == 1
    assert len(drawing.strokes[0].points) == 1
    assert drawing.strokes[0].start_cap == "round"


def test_vector_point_cap_renders_an_outward_tip(qapp):
    stroke = _stroke((50, 50, 20, 1), (100, 50, 20, 1))
    stroke.start_cap = "point"
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.scale = 1.0
    image, target = canvas._vector_stroke_image(drawing, stroke)

    def alpha_at(x, y):
        pixel_x = round((x - target.left()) * image.width() / target.width())
        pixel_y = round((y - target.top()) * image.height() / target.height())
        return image.pixelColor(pixel_x, pixel_y).alpha()

    assert alpha_at(43, 50) > 0
    assert alpha_at(37, 50) == 0


def test_vector_drawing_renders_through_object_pipeline(qapp):
    chapter = ChapterDocument(width=400, height=300)
    page = chapter.add_page()
    layer = chapter.add_layer(
        page.layer_id,
        "Vector clip",
        BoundGeometry.rectangle(0, 0, 250, 240),
    )
    drawing = chapter.add_object(
        layer.layer_id,
            VectorDrawingObject(
                x=20,
                y=0,
                opacity=0.5,
                opacity_locked=False,
                strokes=[
                VectorStroke(
                    color="#FFFF0000",
                    points=[
                        VectorStrokePoint(x=20, y=100, width=20),
                        VectorStrokePoint(x=330, y=100, width=20),
                    ],
                ),
                VectorStroke(
                    color="#8000FF00",
                    points=[
                        VectorStrokePoint(x=20, y=170, width=20),
                        VectorStrokePoint(x=180, y=170, width=20),
                    ],
                ),
            ],
        ),
    )
    chapter.height = 300
    canvas = CanvasWidget(EditorSettings(predictive_ink=False))
    canvas.set_document(chapter, TileStore())
    image = QImage(400, 300, QImage.Format_ARGB32_Premultiplied)

    canvas.render_preview(image)

    overlap = image.pixelColor(100, 100)
    assert overlap.red() > overlap.blue()
    translucent = image.pixelColor(100, 170)
    assert translucent.green() > translucent.red()
    assert 170 <= translucent.red() <= 225
    assert image.pixelColor(300, 100).getRgb()[:3] == (255, 255, 255)


def test_vector_edit_clears_render_cache(qapp):
    stroke = _stroke((100, 100, 8, 1), (200, 100, 8, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas._vector_stroke_image(drawing, stroke)
    assert canvas._vector_render_cache
    canvas._set_vector_selection(
        drawing, {stroke.stroke_id}, {stroke.points[0].point_id}
    )
    canvas._vector_drag_origin = QPointF(100, 100)
    canvas._vector_drag_points = {
        stroke.points[0].point_id: (
            stroke.points[0].position,
            stroke.points[0].incoming,
            stroke.points[0].outgoing,
        )
    }

    canvas._update_vector_anchor_drag(drawing, QPointF(120, 120))

    assert not canvas._vector_render_cache


def test_shape_edit_maps_to_vector_edit_and_selects_a_stroke(qapp):
    stroke = _stroke(
        (100, 100, 8, 1), (200, 100, 8, 1), (260, 160, 8, 1)
    )
    canvas, _chapter, _drawing = _canvas_with_drawing([stroke])
    assert canvas.set_tool(ToolKind.SHAPE_EDIT)
    assert canvas.tool == ToolKind.VECTOR_EDIT
    canvas._tool_press(
        canvas.document_to_widget(QPointF(150, 100)), 1.0
    )
    canvas._tool_release()
    assert stroke.stroke_id in canvas.selected_vector_stroke_ids


def test_object_select_hits_vector_stroke_but_not_empty_interior(qapp):
    stroke = VectorStroke(
        closed=True,
        points=[
            VectorStrokePoint(x=100, y=100, width=8),
            VectorStrokePoint(x=300, y=100, width=8),
            VectorStrokePoint(x=300, y=300, width=8),
            VectorStrokePoint(x=100, y=300, width=8),
        ],
    )
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.settings.page_scope_select = True
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    assert canvas.hit_test_entities(QPointF(100, 180))[0] == {
        "kind": "object", "id": drawing.object_id,
    }
    assert {
        "kind": "object", "id": drawing.object_id,
    } not in canvas.hit_test_entities(QPointF(200, 200))


def test_vector_stroke_eraser_removes_touched_stroke(qapp):
    stroke = _stroke((100, 100, 10, 1), (300, 100, 10, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.settings.vector_eraser_mode = "stroke"
    assert canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._tool_press(
        canvas.document_to_widget(QPointF(180, 100)), 1.0
    )
    canvas._tool_release()
    assert not drawing.strokes
    canvas.command_stack.undo()
    assert len(drawing.strokes) == 1


def test_fill_tool_does_not_create_persistent_vector_fill(qapp):
    stroke = VectorStroke(
        closed=True,
        points=[
            VectorStrokePoint(x=100, y=100, width=6),
            VectorStrokePoint(x=300, y=100, width=6),
            VectorStrokePoint(x=300, y=300, width=6),
            VectorStrokePoint(x=100, y=300, width=6),
        ],
    )
    canvas, chapter, drawing = _canvas_with_drawing([stroke])
    object_ids = set(chapter.objects)
    canvas.set_active_colors("#FF336699", "#FFFFFFFF")
    assert not canvas.set_tool(ToolKind.FILL)
    assert set(chapter.objects) == object_ids
    assert canvas.selected_id == drawing.object_id
    assert not canvas.command_stack.can_undo


def test_intersection_eraser_uses_other_strokes_as_cuts(qapp):
    horizontal = _stroke((40, 200, 6, 1), (360, 200, 6, 1))
    vertical = _stroke((200, 40, 6, 1), (200, 360, 6, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([
        horizontal, vertical,
    ])
    canvas.settings.vector_eraser_mode = "intersection"
    canvas.settings.eraser_size_px["medium"] = 20
    assert canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._tool_press(
        canvas.document_to_widget(QPointF(100, 200)), 1.0
    )
    canvas._tool_release()
    assert len(drawing.strokes) == 2
    remaining_horizontal = next(
        stroke for stroke in drawing.strokes
        if stroke.stroke_id == horizontal.stroke_id
    )
    assert remaining_horizontal.points[0].x == pytest.approx(200, abs=2)
    assert remaining_horizontal.points[-1].x == pytest.approx(360, abs=2)


def test_connect_sweeps_two_endpoints_and_keeps_first_style(qapp):
    first = _stroke((60, 100, 5, 0.4), (160, 100, 7, 0.6))
    first.color = "#FF112233"
    second = _stroke((260, 160, 11, 0.8), (360, 160, 13, 1.0))
    second.color = "#FFAA5500"
    canvas, _chapter, drawing = _canvas_with_drawing([first, second])
    assert canvas.set_tool(ToolKind.VECTOR_CONNECT)
    canvas._tool_press(
        canvas.document_to_widget(QPointF(160, 100)), 1.0
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(260, 160)), 1.0
    )
    canvas._tool_release()
    assert len(drawing.strokes) == 1
    connected = drawing.strokes[0]
    assert connected.stroke_id == first.stroke_id
    assert connected.color == "#FF112233"
    assert connected.points[0].position == (60, 100)
    assert connected.points[-1].position == (360, 160)
    assert connected.points[1].outgoing is not None
    assert connected.points[2].incoming is not None


def test_redraw_apply_targets_points_before_strokes(qapp):
    first = _stroke((60, 100, 5, 1), (160, 100, 7, 1))
    second = _stroke((60, 200, 9, 1), (160, 200, 11, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([first, second])
    canvas._set_vector_selection(
        drawing,
        {first.stroke_id, second.stroke_id},
        {first.points[1].point_id},
    )
    assert canvas.apply_vector_redraw("thickness", "uniform", 25)
    assert first.points[0].width == 5
    assert first.points[1].width == 25
    assert second.points[0].width == 9
    canvas.command_stack.undo()
    restored = next(
        stroke for stroke in drawing.strokes
        if stroke.stroke_id == first.stroke_id
    )
    assert restored.points[1].width == 7


def test_shape_fill_changes_only_selected_shape_background(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(
        page.layer_id, "Shape",
        BoundGeometry.rectangle(100, 100, 240, 180),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_active_colors("#FFCC3300", "#FFFFFFFF")
    assert canvas.set_tool(ToolKind.FILL)
    outline_before = (
        layer.shape_style.outline_color,
        layer.shape_style.outline_thickness,
    )
    canvas._tool_press(
        canvas.document_to_widget(QPointF(100, 180)), 1.0
    )
    assert (
        layer.shape_style.outline_color,
        layer.shape_style.outline_thickness,
    ) == outline_before
    canvas._tool_press(
        canvas.document_to_widget(QPointF(200, 180)), 1.0
    )
    assert layer.fill_color == "#FFCC3300"


def test_square_vector_eraser_uses_square_hit_shape(qapp):
    line = _stroke((50, 100, 1, 1), (150, 100, 1, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([line])
    canvas.settings.vector_eraser_mode = "stroke"
    canvas.settings.eraser_size_px["medium"] = 20
    # Keep the eraser probe outside the explicit cage-handle hit radius.
    canvas.scale = 2.0
    assert canvas.set_tool(ToolKind.RASTER_ERASER)

    # This is inside the 10px square half-extent but outside the circle.
    canvas.settings.eraser_square = False
    canvas._tool_press(
        canvas.document_to_widget(QPointF(160, 110)), 1.0
    )
    canvas._tool_release()
    assert len(drawing.strokes) == 1

    canvas.settings.eraser_square = True
    canvas._tool_press(
        canvas.document_to_widget(QPointF(160, 110)), 1.0
    )
    canvas._tool_release()
    assert drawing.strokes == []


def test_object_select_ignores_fully_transparent_vector_content(qapp):
    stroke = _stroke((100, 100, 10, 1), (300, 100, 10, 1))
    stroke.color = "#00112233"
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.settings.page_scope_select = True
    canvas.set_tool(ToolKind.OBJECT_SELECT)

    assert {
        "kind": "object", "id": drawing.object_id,
    } not in canvas.hit_test_entities(QPointF(200, 100))


def test_vector_select_all_and_selection_translate_are_undoable(qapp):
    stroke = _stroke((20, 30, 4, 1), (120, 80, 8, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    assert canvas.select_all_drawing()
    canvas.scale = 2.0
    assert canvas.selected_vector_point_ids == {
        point.point_id for point in stroke.points
    }
    quad = canvas._selection_transform_quad
    center = QPointF(
        sum(x for x, _ in quad) / 4,
        sum(y for _, y in quad) / 4,
    )
    press = center + QPointF(20, 0)
    canvas._tool_press(canvas.document_to_widget(press), 1)
    canvas._tool_move(
        canvas.document_to_widget(press + QPointF(25, 10)), 1
    )
    canvas._tool_release()
    assert drawing.strokes[0].points[0].position == pytest.approx((45, 40))
    canvas.command_stack.undo()
    restored = canvas.chapter.objects[drawing.object_id]
    assert restored.strokes[0].points[0].position == pytest.approx((20, 30))


def test_vector_selection_translation_repaints_live_before_commit(qapp):
    stroke = _stroke(
        (140, 140, 18, 1), (260, 140, 18, 1),
        (260, 240, 18, 1), (140, 240, 18, 1),
    )
    canvas, chapter, drawing = _canvas_with_drawing([stroke])
    object_ids = set(chapter.objects)
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    assert canvas.select_all_drawing()
    canvas.scale = 1.0
    canvas.center_x = 450
    canvas.center_y = 350
    canvas.show()
    qapp.processEvents()

    source = QPointF(200, 140)
    destination = source + QPointF(120, 0)
    press = QPointF(200, 170)
    source_widget = canvas.document_to_widget(source).toPoint()
    destination_widget = canvas.document_to_widget(destination).toPoint()
    before = canvas.grab().toImage()
    source_color = before.pixelColor(source_widget)
    destination_color = before.pixelColor(destination_widget)
    before_model = chapter.to_dict()
    assert source_color != destination_color

    canvas._tool_press(canvas.document_to_widget(press), 1.0)
    canvas._tool_move(
        canvas.document_to_widget(press + QPointF(120, 0)), 1.0
    )
    qapp.processEvents()
    preview = canvas.grab().toImage()

    assert chapter.to_dict() == before_model
    assert canvas._selection_vector_preview
    assert preview.pixelColor(source_widget) == destination_color
    assert preview.pixelColor(destination_widget) == source_color

    canvas._tool_release()
    assert drawing.strokes[0].points[0].position == pytest.approx((260, 140))


def test_simplify_apply_targets_selected_point_incident_spans(qapp):
    stroke = _stroke(
        (20, 30, 4, 1), (120, 90, 4, 1), (220, 20, 4, 1),
        (320, 100, 4, 1), (420, 30, 4, 1),
    )
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.settings.vector_simplify_amount = 100
    original = stroke_cubics(stroke.points)
    selected_id = stroke.points[2].point_id
    canvas._set_vector_selection(
        drawing, {stroke.stroke_id}, {selected_id}
    )

    assert canvas.apply_vector_simplify()

    rebuilt = stroke_cubics(drawing.strokes[0].points)
    assert original[0] in rebuilt
    assert original[-1] in rebuilt
    assert canvas.selected_vector_stroke_ids == {stroke.stroke_id}
    assert len(canvas.selected_vector_point_ids) == 1


def test_sweep_simplify_selects_anchor_points_and_returns_to_edit(qapp):
    stroke = _stroke(
        (100, 100, 4, 1), (200, 150, 4, 1), (300, 100, 4, 1),
        (400, 150, 4, 1),
    )
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.scale = 1.0
    assert canvas.set_tool(ToolKind.VECTOR_SIMPLIFY)
    touched = stroke.points[1].point_id
    untouched = stroke.points[3].point_id

    canvas._tool_press(
        canvas.document_to_widget(QPointF(200, 150)), 1.0
    )
    assert touched in canvas._vector_simplify_point_ids
    assert untouched not in canvas._vector_simplify_point_ids
    canvas._tool_release()

    assert canvas.tool == ToolKind.VECTOR_EDIT
    assert canvas.selected_vector_stroke_ids == {stroke.stroke_id}
    assert canvas.selected_vector_point_ids
    assert canvas._vector_simplify_point_ids == set()


def test_lasso_remove_deselects_only_enclosed_vector_points(qapp):
    stroke = _stroke(
        (100, 100, 4, 1), (200, 100, 4, 1), (300, 100, 4, 1),
    )
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    all_points = {point.point_id for point in stroke.points}
    canvas._set_vector_selection(drawing, {stroke.stroke_id}, all_points)
    assert canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    canvas._drawing_selection_operation = "remove"
    canvas._drawing_selection_gesture = [
        QPointF(180, 80), QPointF(220, 80),
        QPointF(220, 120), QPointF(180, 120),
    ]

    assert canvas._finish_drawing_selection()

    assert canvas.selected_vector_point_ids == {
        stroke.points[0].point_id, stroke.points[2].point_id,
    }
    assert canvas.selected_vector_stroke_ids == {stroke.stroke_id}


def test_stroke_select_shift_toggles_ctrl_removes_and_lasso_selects_whole(
    qapp, monkeypatch,
):
    first = _stroke((100, 100, 5, 1), (300, 100, 5, 1))
    second = _stroke((100, 180, 5, 1), (300, 180, 5, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([first, second])
    assert canvas.set_tool(ToolKind.DRAW_SELECT_STROKE)

    monkeypatch.setattr(
        QGuiApplication, "keyboardModifiers",
        lambda: Qt.NoModifier,
    )
    canvas._begin_drawing_selection(QPointF(150, 100), QPointF(150, 100))
    assert canvas.selected_vector_stroke_ids == {first.stroke_id}

    monkeypatch.setattr(
        QGuiApplication, "keyboardModifiers",
        lambda: Qt.ShiftModifier,
    )
    canvas._begin_drawing_selection(QPointF(150, 180), QPointF(150, 180))
    assert canvas.selected_vector_stroke_ids == {
        first.stroke_id, second.stroke_id,
    }
    canvas._begin_drawing_selection(QPointF(150, 100), QPointF(150, 100))
    assert canvas.selected_vector_stroke_ids == {second.stroke_id}

    monkeypatch.setattr(
        QGuiApplication, "keyboardModifiers",
        lambda: Qt.ControlModifier,
    )
    canvas._begin_drawing_selection(QPointF(150, 180), QPointF(150, 180))
    assert canvas.selected_vector_stroke_ids == set()

    monkeypatch.setattr(
        QGuiApplication, "keyboardModifiers",
        lambda: Qt.NoModifier,
    )
    canvas._drawing_selection_operation = "replace"
    canvas._drawing_selection_gesture = [
        QPointF(80, 70), QPointF(320, 70),
        QPointF(320, 210), QPointF(80, 210),
    ]
    assert canvas._finish_drawing_selection()
    assert canvas.selected_vector_stroke_ids == {
        first.stroke_id, second.stroke_id,
    }
    assert canvas.selected_vector_point_ids == {
        point.point_id
        for stroke in drawing.strokes
        for point in stroke.points
    }


def test_drawing_selection_outside_tap_selects_but_drag_starts_gesture(qapp):
    stroke = _stroke((100, 100, 5, 1), (300, 100, 5, 1))
    canvas, chapter, drawing = _canvas_with_drawing([stroke])
    page_id = chapter.root_page_ids[0]
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)

    outside = QPointF(0, 500)
    outside_widget = canvas.document_to_widget(outside)
    canvas._tool_press(outside_widget, 1)
    assert canvas._pending_drawing_selection_press is not None
    canvas._tool_release()
    assert canvas.selected_id == page_id
    assert canvas.tool == ToolKind.SHAPE_EDIT

    canvas.set_selection("object", drawing.object_id)
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    canvas._tool_press(outside_widget, 1)
    canvas._tool_move(
        canvas.document_to_widget(QPointF(100, 550)), 1
    )
    assert canvas._pending_drawing_selection_press is None
    assert canvas._drawing_selection_gesture
    canvas._tool_release()
    assert canvas.selected_id == drawing.object_id


def test_rotated_vector_selection_quad_persists_and_undo_restores_it(qapp):
    stroke = _stroke((100, 100, 4, 1), (260, 180, 4, 1))
    canvas, _chapter, _drawing = _canvas_with_drawing([stroke])
    canvas.scale = 1.0
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    assert canvas.select_all_drawing()
    original = list(canvas._selection_transform_quad)
    _handles, rotate, pivot = canvas._transform_control_points(
        original, canvas._selection_pivot
    )
    start_angle = math.atan2(
        rotate.y() - pivot.y(), rotate.x() - pivot.x()
    )
    radius = max(1.0, math.dist(
        rotate.toTuple(), pivot.toTuple()
    ))
    target = QPointF(
        pivot.x() + math.cos(start_angle + 0.7) * radius,
        pivot.y() + math.sin(start_angle + 0.7) * radius,
    )
    canvas._begin_drawing_selection_transform(
        canvas._drawing_selection_object(), rotate
    )
    canvas._update_drawing_selection_transform(
        canvas._drawing_selection_object(), target
    )
    rotated = list(canvas._selection_transform_quad)
    canvas._finish_drawing_selection_transform(
        canvas._drawing_selection_object()
    )
    assert canvas._selection_transform_quad == pytest.approx(rotated)
    assert rotated != pytest.approx(original)

    canvas.command_stack.undo()
    assert canvas._selection_transform_quad == pytest.approx(original)
    canvas.command_stack.redo()
    assert canvas._selection_transform_quad == pytest.approx(rotated)

    center = QPointF(
        sum(x for x, _ in rotated) / 4,
        sum(y for _, y in rotated) / 4,
    )
    press = QPointF(
        center.x() * 0.65 + rotated[0][0] * 0.35,
        center.y() * 0.65 + rotated[0][1] * 0.35,
    )
    canvas._begin_drawing_selection_transform(
        canvas._drawing_selection_object(), press
    )
    canvas._update_drawing_selection_transform(
        canvas._drawing_selection_object(), press + QPointF(20, 15)
    )
    translated = list(canvas._selection_transform_quad)
    canvas._finish_drawing_selection_transform(
        canvas._drawing_selection_object()
    )
    assert translated == pytest.approx([
        (x + 20, y + 15) for x, y in rotated
    ])


def test_delete_selected_vector_points_is_one_undoable_edit(qapp):
    stroke = _stroke((40, 40, 5, 1), (80, 60, 5, 1), (120, 40, 5, 1))
    canvas, _chapter, drawing = _canvas_with_drawing([stroke])
    canvas.set_tool(ToolKind.VECTOR_EDIT)
    removed = stroke.points[1].point_id
    canvas._set_vector_selection(drawing, {stroke.stroke_id}, {removed})

    assert canvas._delete_selected_vector_points()
    assert len(drawing.strokes[0].points) == 2
    assert removed not in {
        point.point_id for point in drawing.strokes[0].points
    }

    canvas.command_stack.undo()
    restored = canvas.chapter.objects[drawing.object_id]
    assert len(restored.strokes[0].points) == 3
