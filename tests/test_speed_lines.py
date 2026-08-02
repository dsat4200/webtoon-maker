from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientStop, LineGradientField,
    PathNode, SpeedLineCenterObject, SpeedLinesGradientObject,
    object_from_dict,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def _ramp() -> ColorGradientRamp:
    return ColorGradientRamp(stops=[
        ColorGradientStop(position=0, color="#FFFF0000"),
        ColorGradientStop(position=1, color="#FF0000FF"),
    ])


def _gradient_document(field_type="line"):
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 400, 300))
    gradient = SpeedLinesGradientObject(
        field_type=field_type,
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=50, y=150), PathNode(x=350, y=150),
        ])),
        radial_field=__import__(
            "comic_editor.core.models", fromlist=["RadialGradientField"]
        ).RadialGradientField(
            origin_x=200, origin_y=150, radius_x=120, radius_y=120,
        ),
        color_ramp=_ramp(),
        thickness_ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#FF000000"),
            ColorGradientStop(position=1, color="#FFFFFFFF"),
        ]),
        speed_field=__import__(
            "comic_editor.core.models", fromlist=["SpeedLinesField"]
        ).SpeedLinesField(density=0.05),
    )
    chapter.add_object(page.layer_id, gradient)
    return chapter, page, gradient


def test_speed_lines_model_round_trip_preserves_ramps_and_center():
    chapter, _page, gradient = _gradient_document("radial")
    center = chapter.add_speed_center(
        gradient.object_id,
        SpeedLineCenterObject(geometry=BoundGeometry.rectangle(
            160, 110, 80, 80
        )),
    )
    gradient.speed_field.randomness_distance = 37
    gradient.speed_field.randomness_scale = 8
    restored = object_from_dict(gradient.to_dict())
    assert isinstance(restored, SpeedLinesGradientObject)
    assert restored.gradient_type == "speed_lines"
    assert restored.color_ramp.stops[-1].color == "#FF0000FF"
    assert restored.thickness_ramp.stops[-1].color == "#FFFFFFFF"
    assert restored.speed_field.density == 0.05
    assert restored.speed_field.randomness_distance == 37
    assert restored.speed_field.randomness_scale == 8
    assert restored.center_shape_id == center.object_id
    center_restored = object_from_dict(center.to_dict())
    assert isinstance(center_restored, SpeedLineCenterObject)
    assert center_restored.owner_gradient_id == gradient.object_id


def test_speed_center_ownership_invariants():
    chapter = ChapterDocument()
    page = chapter.add_page()
    gradient = chapter.add_object(
        page.layer_id, SpeedLinesGradientObject(field_type="radial")
    )
    center = chapter.add_speed_center(
        gradient.object_id, SpeedLineCenterObject()
    )
    assert gradient.center_shape_id == center.object_id
    assert center.parent_layer_id == page.layer_id
    assert not any(
        ref.kind == "object" and ref.entity_id == center.object_id
        for ref in page.children
    )

    with pytest.raises(ValueError):
        chapter.add_speed_center(
            gradient.object_id, SpeedLineCenterObject()
        )
    with pytest.raises(ValueError):
        chapter.move_entity("object", center.object_id, page.layer_id, 0)

    deleted = chapter.delete_entity("object", center.object_id)
    assert center.object_id in deleted
    assert gradient.center_shape_id == ""
    assert center.object_id not in chapter.objects

    chapter.validate()

    gradient2 = chapter.add_object(
        page.layer_id, SpeedLinesGradientObject(field_type="line")
    )
    center2 = chapter.add_speed_center(
        gradient2.object_id, SpeedLineCenterObject()
    )
    deleted = chapter.delete_entity("object", gradient2.object_id)
    assert center2.object_id in deleted
    assert center2.object_id not in chapter.objects


def test_color_and_speed_gradients_coexist_per_field_type():
    chapter, _page, gradient = _gradient_document("line")
    color = chapter.add_object(
        gradient.parent_layer_id,
        ColorFillGradientObject(field_type="line"),
    )
    assert isinstance(color, ColorFillGradientObject)
    with pytest.raises(ValueError):
        chapter.add_object(
            gradient.parent_layer_id,
            SpeedLinesGradientObject(field_type="line"),
        )
    with pytest.raises(ValueError):
        chapter.add_object(
            gradient.parent_layer_id,
            ColorFillGradientObject(field_type="line"),
        )


def test_v12_chapter_with_speed_lines_migrates_to_v13():
    chapter, _page, gradient = _gradient_document("parent_shape")
    center = chapter.add_speed_center(
        gradient.object_id, SpeedLineCenterObject()
    )
    payload = chapter.to_dict()
    payload["schema_version"] = 12
    restored = ChapterDocument.from_dict(payload)
    assert restored.schema_version == 13
    restored_gradient = restored.objects[gradient.object_id]
    assert isinstance(restored_gradient, SpeedLinesGradientObject)
    assert restored.speed_center_for(gradient.object_id) is not None


def _render(chapter, obj):
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    image = QImage(1080, 500, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return canvas, image


def test_line_speed_lines_render_parallel_and_perpendicular(qapp):
    chapter, _page, gradient = _gradient_document("line")
    gradient.speed_field.density = 0.1
    gradient.line_field.perpendicular_distance = 40
    _canvas, image = _render(chapter, gradient)
    # Parallel family sits at +40 and extends downward; above stays clear.
    assert any(
        image.pixelColor(100, y).name() != "#ffffff"
        for y in range(190, 300)
    )
    assert all(
        image.pixelColor(100, y).name() == "#ffffff"
        for y in range(50, 150)
    )

    gradient.line_field.direction_mode = "perpendicular"
    gradient.line_field.perpendicular_distance = 60
    _canvas, image = _render(chapter, gradient)
    # Strokes point down from the curve; above the curve stays clear.
    assert any(
        image.pixelColor(200, y).name() != "#ffffff"
        for y in range(151, 210)
    )
    assert all(
        image.pixelColor(200, y).name() == "#ffffff"
        for y in range(100, 149)
    )


def test_radial_speed_lines_converge_at_center_and_jitter(qapp):
    chapter, _page, gradient = _gradient_document("radial")
    gradient.speed_field.randomness_distance = 30
    gradient.speed_field.randomness_scale = 10
    _canvas, image = _render(chapter, gradient)
    near_boundary = image.pixelColor(100, 150)
    near_center = image.pixelColor(200, 110)
    assert near_boundary.name() != "#ffffff"
    assert near_center.name() != "#ffffff"
    assert near_center != near_boundary

    # Deterministic across identical renders.
    first = image
    _canvas, second = _render(chapter, gradient)
    assert first == second


def test_shape_speed_lines_outward_ignores_center(qapp):
    chapter, _page, gradient = _gradient_document("parent_shape")
    gradient.speed_field.density = 0.1
    gradient.shape_field.reverse_direction = True
    gradient.shape_field.distance = 80
    center = chapter.add_speed_center(
        gradient.object_id,
        SpeedLineCenterObject(geometry=BoundGeometry.rectangle(
            100, 100, 200, 100,
        )),
    )
    _canvas, image = _render(chapter, gradient)
    assert image.pixelColor(450, 150).name() != "#ffffff"
    assert image.pixelColor(550, 150).name() == "#ffffff"
    assert center.object_id in chapter.objects


def test_center_shape_truncates_lines_and_mismatch_is_ignored(qapp):
    chapter, _page, gradient = _gradient_document("parent_shape")
    gradient.speed_field.density = 0.1
    center = chapter.add_speed_center(
        gradient.object_id,
        SpeedLineCenterObject(geometry=BoundGeometry.rectangle(
            160, 100, 80, 80,
        )),
    )
    _canvas, image = _render(chapter, gradient)
    assert image.pixelColor(200, 150).name() == "#ffffff"
    assert image.pixelColor(150, 150).name() != "#ffffff"

    # Open center under a closed parent has no effect; still renders.
    second_layer = chapter.add_layer(
        _page.layer_id, "Second",
        BoundGeometry.rectangle(0, 0, 300, 200),
    )
    gradient2 = chapter.add_object(
        second_layer.layer_id,
        SpeedLinesGradientObject(field_type="parent_shape"),
    )
    chapter.add_speed_center(
        gradient2.object_id,
        SpeedLineCenterObject(geometry=BoundGeometry.path([
            PathNode(x=100, y=100), PathNode(x=300, y=100),
        ], closed=False)),
    )
    _canvas, image = _render(chapter, gradient2)
    assert image.pixelColor(200, 150).name() != "#ffffff"


def test_speed_lines_gizmos_and_direction_toggle(qapp):
    chapter, _page, gradient = _gradient_document("line")
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", gradient.object_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)

    controls = canvas._gradient_control_points(gradient)
    assert "distance:" in controls
    assert "direction:" in controls

    hit = canvas._gradient_control_hit(
        gradient, controls["direction:"]
    )
    assert hit == ("direction", "")
    assert canvas._begin_gradient_edit(gradient, controls["direction:"])
    assert gradient.line_field.direction_mode == "perpendicular"


def test_speed_center_shape_edit_routes_to_center_geometry(qapp):
    chapter, _page, gradient = _gradient_document("radial")
    center = chapter.add_speed_center(
        gradient.object_id,
        SpeedLineCenterObject(geometry=BoundGeometry.path([
            PathNode(x=200, y=105), PathNode(x=245, y=150),
            PathNode(x=200, y=195), PathNode(x=155, y=150),
        ], closed=True)),
    )
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", center.object_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    assert canvas.selected_kind == "object"
    assert canvas.selected_id == center.object_id

    node = center.geometry.nodes[0]
    assert canvas._begin_shape_edit(QPointF(node.x, node.y))
    assert canvas._active_shape_control == "node"
    canvas._update_shape_edit(QPointF(node.x + 40, node.y + 15))
    assert center.geometry.nodes[0].position == pytest.approx((240, 120))
    assert center.geometry.nodes[1].position == (245, 150)
    canvas._tool_release()

    # Not click-selectable.
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    canvas.set_selection("layer", _page.layer_id)
    canvas._request_object_selection(
        QPointF(200, 150), canvas.document_to_widget(QPointF(200, 150))
    )
    assert canvas.selected_id != center.object_id


def test_impact_parameters_clamp_and_round_trip():
    from comic_editor.core.models import SpeedLinesField
    field = SpeedLinesField(
        density=0.5, gap=0, close_range=0,
        randomness_distance=0, randomness_scale=1,
    )
    field.density = 99
    field.gap = -5
    field.randomness_scale = 500
    field.validate()
    assert field.density == 0.5
    assert field.gap == 0.0
    assert field.randomness_scale == 100
    assert SpeedLinesField.from_dict(field.to_dict()).density == 0.5
