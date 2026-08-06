from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject,
    ColorGradientRamp, ColorGradientRampPreset, ColorGradientStop,
    LineGradientField, PathNode, RadialGradientField, SeriesDocument,
    ShapeGradientField, TextObject, object_from_dict,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.gradient_tools import BUILTIN_PRIMARY_SECONDARY_ID


def _ramp() -> ColorGradientRamp:
    return ColorGradientRamp(stops=[
        ColorGradientStop(position=0, color="#FFFF0000"),
        ColorGradientStop(position=1, color="#FF0000FF"),
    ])


def _gradient_document(field_type="line"):
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 300)
    )
    gradient = ColorFillGradientObject(
        field_type=field_type,
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=0, y=150), PathNode(x=400, y=150),
        ])),
        radial_field=RadialGradientField(
            origin_x=200, origin_y=150,
            radius_x=100, radius_y=100,
        ),
        ramp=_ramp(),
    )
    chapter.add_object(page.layer_id, gradient)
    return chapter, page, gradient


def test_gradient_model_round_trip_preserves_dormant_fields_and_hard_stops():
    gradient = ColorFillGradientObject(
        field_type="parent_shape",
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=10, y=20),
            PathNode(x=30, y=40, point_type="bezier",
                     incoming=(25, 35)),
        ]), direction_mode="perpendicular",
            reverse_direction=True, perpendicular_distance=-77),
        radial_field=RadialGradientField(
            origin_x=80, origin_y=90, radius_x=50, radius_y=25,
            rotation=37, ellipse_enabled=True,
            center_auto=False, manual_center=(74, 81),
            reverse_direction=True, uniform=True, distance=63,
        ),
        shape_field=ShapeGradientField(
            center_auto=False, manual_center=(12, 13),
            reverse_direction=True, uniform=True, distance=91,
        ),
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#80112233"),
            ColorGradientStop(position=.5, color="#FF445566"),
            ColorGradientStop(position=.5, color="#FF778899"),
            ColorGradientStop(position=1, color="#FFAABBCC"),
        ]),
        loaded_preset_id="preset",
    )
    restored = object_from_dict(gradient.to_dict())

    assert isinstance(restored, ColorFillGradientObject)
    assert restored.field_type == "parent_shape"
    assert restored.line_field.geometry.nodes[0].position == (10, 20)
    assert restored.radial_field.rotation == 37
    assert restored.radial_field.manual_center == (74, 81)
    assert restored.shape_field.manual_center == (12, 13)
    assert restored.line_field.direction_mode == "perpendicular"
    assert restored.line_field.reverse_direction
    assert restored.line_field.perpendicular_distance == -77
    assert restored.radial_field.distance == 63
    assert restored.shape_field.distance == 91
    assert restored.radial_field.uniform
    assert restored.shape_field.uniform
    assert [stop.position for stop in restored.ramp.stops] == [
        0, .5, .5, 1,
    ]
    assert restored.loaded_preset_id == "preset"


def test_series_v11_gradient_presets_are_copied_and_migrate_from_v9():
    legacy = SeriesDocument().to_dict()
    legacy["schema_version"] = 9
    legacy.pop("gradient_ramp_presets")
    restored = SeriesDocument.from_dict(legacy)
    assert restored.schema_version == 15
    assert len(restored.gradient_ramp_presets) == 1

    preset = restored.gradient_ramp_presets[0]
    obj = ColorFillGradientObject(ramp=preset.ramp.copy())
    obj.ramp.stops[0].color = "#FF123456"
    assert preset.ramp.stops[0].color != obj.ramp.stops[0].color


def test_v11_outward_distance_migrates_to_v12_distance():
    payload = ColorFillGradientObject(
        field_type="parent_shape"
    ).to_dict()
    payload["radial_field"].pop("distance")
    payload["radial_field"].pop("uniform")
    payload["radial_field"]["outward_distance"] = 73
    payload["shape_field"].pop("distance")
    payload["shape_field"].pop("uniform")
    payload["shape_field"]["outward_distance"] = 84

    restored = object_from_dict(payload)

    assert restored.radial_field.distance == 73
    assert restored.shape_field.distance == 84
    assert not restored.radial_field.uniform
    assert not restored.shape_field.uniform


def test_line_and_radial_gradients_render_with_clamped_direction(qapp):
    chapter, _page, gradient = _gradient_document("line")
    settings = EditorSettings()
    settings.snap_to_grid = False
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, TileStore())
    image = QImage(
        1080, 500, QImage.Format.Format_ARGB32_Premultiplied
    )
    canvas.render_preview(image)
    assert image.pixelColor(10, 150).red() > 220
    assert image.pixelColor(390, 150).blue() > 220

    gradient.field_type = "radial"
    gradient.touch_revision()
    canvas.documentChanged.emit(QRectF())
    canvas.render_preview(image)
    assert image.pixelColor(200, 150).blue() > 220
    assert image.pixelColor(300, 150).red() > 220
    assert image.pixelColor(350, 150).red() > 220


def test_parent_shape_gradient_reaches_center_and_is_parent_masked(qapp):
    chapter, _page, gradient = _gradient_document("parent_shape")
    settings = EditorSettings()
    settings.snap_to_grid = False
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, TileStore())
    image = QImage(
        1080, 500, QImage.Format.Format_ARGB32_Premultiplied
    )
    canvas.render_preview(image)
    assert image.pixelColor(200, 150).blue() > 210
    assert image.pixelColor(200, 400).name() == "#ffffff"


def test_gradient_creation_is_bottommost_and_selection_uses_shape_edit(qapp):
    chapter = ChapterDocument(height=700)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 400)
    )
    existing = chapter.add_object(page.layer_id, TextObject())
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_active_colors("#FF102030", "#FFABCDEF")

    gradient = canvas.create_gradient(page.layer_id, "parent_shape")

    assert isinstance(gradient, ColorFillGradientObject)
    assert page.children[-1].entity_id == gradient.object_id
    assert page.children[0].entity_id == existing.object_id
    assert canvas.selected_id == gradient.object_id
    assert canvas.tool == ToolKind.SHAPE_EDIT
    assert gradient.ramp.stops[0].color == "#FF102030"
    assert gradient.ramp.stops[-1].color == "#FFABCDEF"
    canvas.command_stack.undo()
    assert gradient.object_id not in canvas.chapter.objects


def test_object_select_hits_gradient_in_parent_interior(qapp):
    chapter, _page, gradient = _gradient_document("line")
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    point = QPointF(200, 150)

    canvas._request_object_selection(
        point, canvas.document_to_widget(point)
    )

    assert canvas.selected_id == gradient.object_id
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_line_and_radial_creation_flows_commit_one_gradient(qapp):
    chapter = ChapterDocument(height=800)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 500, 500)
    )
    settings = EditorSettings()
    settings.snap_to_grid = False
    canvas = CanvasWidget(settings)
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id)

    assert canvas.begin_gradient_creation(page.layer_id, "line")
    canvas._creation_nodes = [
        PathNode(x=50, y=100), PathNode(x=450, y=100),
    ]
    canvas._finish_shape(False)
    line = chapter.objects[canvas.selected_id]
    assert isinstance(line, ColorFillGradientObject)
    assert not line.line_field.geometry.closed
    assert canvas.command_stack.can_undo

    canvas.set_selection("layer", page.layer_id)
    assert canvas.begin_gradient_creation(page.layer_id, "radial")
    start = canvas.document_to_widget(QPointF(250, 250))
    end = canvas.document_to_widget(QPointF(350, 250))
    canvas._tool_press(start, 1)
    canvas._tool_move(end, 1)
    canvas._tool_release()
    radial = chapter.objects[canvas.selected_id]
    assert isinstance(radial, ColorFillGradientObject)
    assert radial.field_type == "radial"
    assert radial.radial_field.radius_x == 100


def test_gradient_center_manual_drag_and_reset(qapp):
    chapter, _page, gradient = _gradient_document("radial")
    settings = EditorSettings()
    settings.snap_to_grid = False
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", gradient.object_id)
    center = canvas._gradient_local_to_world(
        gradient, gradient.radial_field.center()
    )
    target = center + QPointF(20, 15)

    assert canvas._begin_gradient_edit(gradient, center)
    canvas._update_gradient_edit(gradient, target)
    canvas._tool_release()
    assert not gradient.radial_field.center_auto
    assert gradient.radial_field.manual_center == (220, 165)
    assert canvas._reset_gradient_center(gradient, target)
    assert gradient.radial_field.center_auto
    assert gradient.radial_field.manual_center is None


def test_gradient_context_ribbon_and_preset_loading_are_isolated(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter, page, gradient = _gradient_document("line")
    series = SeriesDocument(
        gradient_ramp_presets=[
            ColorGradientRampPreset(name="Warm", ramp=_ramp())
        ]
    )
    window.series = series
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("layer", page.layer_id)
        qapp.processEvents()
        assert window.ribbon.is_page_visible("gradient_tools")

        window.canvas.set_selection("object", gradient.object_id)
        qapp.processEvents()
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        assert window.ribbon.current_key() == "gradient_tools"

        preset = series.gradient_ramp_presets[0]
        window._load_gradient_preset(preset.preset_id)
        gradient.ramp.stops[0].color = "#FF00FF00"
        assert preset.ramp.stops[0].color == "#FFFF0000"
    finally:
        window._dirty = False
        window.close()


def test_gradient_ramp_controls_edit_object_without_mutating_loaded_preset(
    qapp,
):
    window = MainWindow()
    chapter, _page, gradient = _gradient_document("line")
    preset = ColorGradientRampPreset(name="Loaded", ramp=_ramp())
    gradient.loaded_preset_id = preset.preset_id
    window.series = SeriesDocument(gradient_ramp_presets=[preset])
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("object", gradient.object_id)
    controls = window.gradient_tools_controls
    controls.refresh()
    try:
        controls._add_stop()
        assert len(gradient.ramp.stops) == 3
        assert gradient.loaded_preset_id == preset.preset_id
        assert len(preset.ramp.stops) == 2

        controls._remove_stop()
        assert len(gradient.ramp.stops) == 2
        assert len(preset.ramp.stops) == 2

        radial_before = gradient.radial_field.to_dict()
        index = controls.field_type.findData("radial")
        controls.field_type.setCurrentIndex(index)
        assert gradient.field_type == "radial"
        index = controls.field_type.findData("line")
        controls.field_type.setCurrentIndex(index)
        assert gradient.radial_field.to_dict() == radial_before
    finally:
        window._dirty = False
        window.close()


def test_gradient_reparent_preserves_line_and_radial_world_coordinates():
    chapter = ChapterDocument()
    page = chapter.add_page()
    first = chapter.add_layer(
        page.layer_id, bound=BoundGeometry.rectangle(0, 0, 200, 200)
    )
    second = chapter.add_layer(
        page.layer_id, bound=BoundGeometry.rectangle(0, 0, 200, 200)
    )
    first.translate_x, first.translate_y = 100, 50
    second.translate_x, second.translate_y = 300, 250
    gradient = ColorFillGradientObject(
        line_field=LineGradientField(BoundGeometry.path([
            PathNode(x=10, y=20), PathNode(x=110, y=20),
        ])),
        radial_field=RadialGradientField(
            origin_x=50, origin_y=60, radius_x=20, radius_y=20
        ),
    )
    chapter.add_object(first.layer_id, gradient)
    old_line_world = (110, 70)
    old_origin_world = (150, 110)

    chapter.move_entity(
        "object", gradient.object_id, second.layer_id,
        len(second.children),
    )

    new_x, new_y = chapter.layer_world_translation(second.layer_id)
    assert (
        gradient.line_field.geometry.nodes[0].x + new_x,
        gradient.line_field.geometry.nodes[0].y + new_y,
    ) == old_line_world
    assert (
        gradient.radial_field.origin_x + new_x,
        gradient.radial_field.origin_y + new_y,
    ) == old_origin_world


def test_curve_projection_uses_arc_length_and_perpendicular_side(qapp):
    chapter, _page, gradient = _gradient_document("line")
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    geometry = BoundGeometry.path([
        PathNode(x=0, y=0),
        PathNode(x=0, y=200),
        PathNode(x=400, y=200),
    ])
    path = canvas.bound_path(geometry)
    bounds = QRectF(0, 0, 400, 200)
    amount, _signed, _distance = canvas._path_projection_arrays(
        path, bounds, 400, 200
    )
    assert amount[-1, 0] == pytest.approx(1 / 3, abs=.02)

    gradient.line_field.geometry = BoundGeometry.path([
        PathNode(x=0, y=100), PathNode(x=400, y=100),
    ])
    gradient.line_field.direction_mode = "perpendicular"
    gradient.line_field.perpendicular_distance = 100
    canvas._line_gradient_image(
        gradient, canvas.bound_path(gradient.line_field.geometry),
        QRectF(0, 0, 400, 200),
    )
    scalar, _coverage, _bounds = canvas._gradient_scalar_cache[next(reversed(
        canvas._gradient_scalar_cache
    ))]
    assert scalar[round(scalar.shape[0] * .75), 200] > .45
    assert scalar[round(scalar.shape[0] * .25), 200] == pytest.approx(
        0, abs=.02
    )


def test_shape_gradient_reuses_geometry_cache_for_ramp_edits(qapp):
    chapter, _page, gradient = _gradient_document("parent_shape")
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    path = canvas.layer_effective_path(gradient.parent_layer_id)
    first = canvas._shape_gradient_image(gradient, path)
    geometry_keys = set(canvas._gradient_geometry_cache)
    scalar_keys = set(canvas._gradient_scalar_cache)
    gradient.ramp.stops[0].color = "#4000FF00"
    second = canvas._shape_gradient_image(gradient, path)
    assert first is not None and second is not None
    assert set(canvas._gradient_geometry_cache) == geometry_keys
    assert set(canvas._gradient_scalar_cache) == scalar_keys
    assert first[0].cacheKey() != second[0].cacheKey()


def test_reversed_shape_gradient_renders_outside_below_parent(qapp):
    chapter = ChapterDocument(height=500)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(100, 100, 200, 200)
    )
    gradient = ColorFillGradientObject(
        field_type="parent_shape",
        shape_field=ShapeGradientField(
            reverse_direction=True, distance=60
        ),
        ramp=ColorGradientRamp(stops=[
            ColorGradientStop(position=0, color="#80FF0000"),
            ColorGradientStop(position=1, color="#000000FF"),
        ]),
    )
    chapter.add_object(page.layer_id, gradient)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    image = QImage(1080, 500, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    outside = image.pixelColor(90, 200)
    inside = image.pixelColor(150, 200)
    assert outside.red() > outside.blue()
    assert inside.name() == "#ffffff"


def test_uniform_parent_gradient_uses_physical_inward_distance(qapp):
    chapter, _page, gradient = _gradient_document("parent_shape")
    gradient.shape_field.uniform = True
    gradient.shape_field.distance = 100
    gradient.shape_field.center_auto = False
    gradient.shape_field.manual_center = (25, 25)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    image = QImage(1080, 500, QImage.Format.Format_ARGB32_Premultiplied)

    canvas.render_preview(image)

    assert image.pixelColor(5, 150).red() > 220
    assert image.pixelColor(100, 150).blue() > 200
    assert gradient.shape_field.manual_center == (25, 25)
    controls = canvas._gradient_control_points(gradient)
    assert "distance:" in controls
    assert "center:" not in controls
    gradient.shape_field.uniform = False
    controls = canvas._gradient_control_points(gradient)
    assert "center:" in controls
    assert gradient.shape_field.manual_center == (25, 25)


def test_line_gradient_uses_geometry_only_open_shape_gizmos(qapp):
    chapter, _page, gradient = _gradient_document("line")
    middle = PathNode(
        x=200, y=100, point_type="bezier",
        incoming=(150, 125), outgoing=(250, 75),
        handles_locked=False, roundness=12, roundness_enabled=True,
    )
    gradient.line_field.geometry = BoundGeometry.path([
        PathNode(x=0, y=150),
        middle,
        PathNode(x=400, y=150),
    ], closed=False)
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", gradient.object_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = middle.node_id

    controls = canvas._gradient_control_points(gradient)

    assert f"type:{middle.node_id}" in controls
    assert f"delete:{middle.node_id}" in controls
    assert f"lock:{middle.node_id}" in controls
    assert f"roundness:{middle.node_id}" in controls
    assert not any(key.startswith("cap:") for key in controls)
    assert not any(key.startswith("thickness:") for key in controls)


def test_gradient_type_uniqueness_blocks_create_and_reparent():
    chapter = ChapterDocument()
    first = chapter.add_page()
    second = chapter.add_page()
    chapter.add_object(first.layer_id, ColorFillGradientObject(
        field_type="line"
    ))
    with pytest.raises(ValueError, match="already has"):
        chapter.add_object(first.layer_id, ColorFillGradientObject(
            field_type="line"
        ))
    moving = chapter.add_object(second.layer_id, ColorFillGradientObject(
        field_type="line"
    ))
    with pytest.raises(ValueError, match="already has"):
        chapter.move_entity(
            "object", moving.object_id, first.layer_id, 0
        )
    assert moving.parent_layer_id == second.layer_id


def test_builtin_primary_secondary_preset_is_dynamic_and_immutable(qapp):
    window = MainWindow()
    chapter, _page, gradient = _gradient_document("line")
    window.series = SeriesDocument(
        primary_color="#40112233", secondary_color="#80445566"
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("object", gradient.object_id)
    try:
        window._sync_gradient_presets()
        index = window.gradient_tools_controls.preset_combo.findData(
            BUILTIN_PRIMARY_SECONDARY_ID
        )
        assert index >= 0
        window.gradient_tools_controls.preset_combo.setCurrentIndex(index)
        window.gradient_tools_controls._sync_preset_name()
        assert not window.gradient_tools_controls.save_preset.isEnabled()
        assert not window.gradient_tools_controls.remove_preset.isEnabled()
        window._load_gradient_preset(BUILTIN_PRIMARY_SECONDARY_ID)
        assert gradient.ramp.stops[0].color == "#40112233"
        assert gradient.ramp.stops[-1].color == "#80445566"
        assert all(
            preset.preset_id != BUILTIN_PRIMARY_SECONDARY_ID
            for preset in window.series.gradient_ramp_presets
        )
    finally:
        window._dirty = False
        window.close()
