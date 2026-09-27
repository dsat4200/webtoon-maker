"""Viewport culling must reduce work without changing the compositor output."""
from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    ArrayModifier, BlurModifier, BoundGeometry, ChapterDocument,
    ColorFillGradientObject, DistortModifier, ImageObject, MirrorModifier, OutlineModifier,
    PathNode, RasterObject, ShapeStyle, TextObject, TilingModifier, VectorDrawingObject, VectorStroke,
    VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.smudge import default_tool_settings, validate_strokes
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    document = ChapterDocument(height=4000)
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 4000))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(480, 480)
    canvas.set_document(document, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 240, 240, 1
    # Compare completed pixels, independently of asynchronous effect timing.
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *_a, **_k: False)
    yield canvas, document, page
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), dtype=np.uint8).copy()


def render(canvas, *, culling=True):
    prepare = canvas._render_bounds.prepare
    if not culling:
        def disabled():
            prepare()
            canvas._render_bounds.enabled = False
        canvas._render_bounds.prepare = disabled
    try:
        canvas._invalidate_scene_cache()
        canvas._ensure_scene_cache()
        return QImage(canvas._scene_cache)
    finally:
        canvas._render_bounds.prepare = prepare


def image_object(canvas, document, parent, x, y, color="red"):
    obj = document.add_object(parent.layer_id, ImageObject(
        x=x, y=y, pixel_width=40, pixel_height=40))
    image = QImage(40, 40, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    canvas.images.put_decoded(obj.object_id, "sample.png", b"", image)
    return obj


def test_smudge_paint_extends_past_parent_mask_and_stays_cullable(scene):
    canvas, document, page = scene
    layer = document.add_layer(page.layer_id, "Smudge", BoundGeometry.rectangle(100, 100, 80, 80))
    layer.fill_color, layer.border_width = None, 0
    obj = image_object(canvas, document, layer, 110, 110)
    points = [
        {"position": [130, 130], "handle": [130, 130], "point_type": "vector",
         "radius": 12, "flow": 100, "strength": 100},
        {"position": [220, 130], "handle": [220, 130], "point_type": "vector",
         "radius": 12, "flow": 100, "strength": 100},
    ]
    stroke = validate_strokes([{"id": "outside", "points": points,
                                "pressure_settings": default_tool_settings()}])[0]
    modifier = DistortModifier(modifier_type="distort_smudge",
                               parameters={"strokes": [stroke]})
    document.add_modifier(modifier, [("object", obj.object_id)])
    expected = render(canvas, culling=False)
    actual = render(canvas)
    assert np.array_equal(pixels(actual), pixels(expected))
    assert actual.pixelColor(195, 130).red() > 100
    assert actual.pixelColor(195, 130).red() > actual.pixelColor(195, 130).blue()
    assert canvas._point_inside_layer_masks(layer.layer_id, QPointF(195, 130), obj)
    assert canvas._render_bounds.entity_bounds("layer", layer.layer_id).right() > 195


def test_offscreen_layers_skip_shape_and_object_work_and_return_when_panned(scene, monkeypatch):
    canvas, document, page = scene
    for index in range(100):
        layer = document.add_layer(page.layer_id, str(index),
            BoundGeometry.rectangle(0, index * 180, 400, 140))
        layer.fill_color, layer.border_width = "#ffffff", 2
        image_object(canvas, document, layer, 20, index * 180 + 20)
    document.height = 18000
    page.bound = BoundGeometry.rectangle(0, 0, 1080, 18000)
    expected = pixels(render(canvas, culling=False))
    calls = []
    original = canvas._layer_operand_path
    monkeypatch.setattr(canvas, "_layer_operand_path", lambda layer, *a, **k:
        (calls.append(layer.layer_id), original(layer, *a, **k))[1])
    actual = pixels(render(canvas))
    assert np.array_equal(actual, expected)
    assert len(calls) <= 5
    calls.clear()
    canvas.center_y = 9240
    lower = pixels(render(canvas))
    assert len(calls) <= 5
    assert np.array_equal(lower, pixels(render(canvas, culling=False)))
    assert all(layer.visible for layer in document.layers.values())


def test_many_objects_in_one_layer_do_not_render_offscreen_images_or_text(scene, monkeypatch):
    canvas, document, page = scene
    for index in range(80):
        image_object(canvas, document, page, 30, index * 120)
        document.add_object(page.layer_id, TextObject(
            text="Unchanged text", layout_mode="free", x=120, y=index * 120,
            width=150, height=40))
    expected = pixels(render(canvas, culling=False))
    calls = []
    original = canvas._render_object_content
    monkeypatch.setattr(canvas, "_render_object_content", lambda painter, obj, visible:
        (calls.append(obj.object_id), original(painter, obj, visible))[1])
    assert np.array_equal(pixels(render(canvas)), expected)
    assert len(calls) <= 10
    # Once indexed, unrelated bounds are not recomputed for a dirty stroke area.
    monkeypatch.setattr(canvas._render_bounds, "_object_bounds", lambda _obj:
        pytest.fail("Static object bounds rebuilt during a dirty repaint"))
    canvas._render_scene_cache_rect(QRect(15, 15, 30, 30))


def test_offscreen_distortion_stack_skips_source_capture_and_returns_when_panned(scene, monkeypatch):
    canvas, document, page = scene
    obj = image_object(canvas, document, page, 30, 1500)
    document.add_modifier(DistortModifier(modifier_type="distort_mesh_warp",
                                          frame=(30, 1500, 40, 40)),
                          [("object", obj.object_id)])
    document.add_modifier(DistortModifier(modifier_type="distort_twirl",
                                          frame=(30, 1500, 40, 40), center=(50, 1520), radius=20),
                          [("object", obj.object_id)])
    expected = pixels(render(canvas, culling=False))
    canvas._render_bounds.clear()
    calls = []
    original = canvas._render_mirror_target
    monkeypatch.setattr(canvas, "_render_mirror_target", lambda painter, target, opacity, visible:
                        (calls.append(target.object_id), original(painter, target, opacity, visible))[1])
    assert np.array_equal(pixels(render(canvas)), expected)
    assert obj.object_id not in calls
    assert canvas._render_bounds.entity_bounds("object", obj.object_id) is not None

    canvas.center_y = 1520
    calls.clear()
    visible = pixels(render(canvas))
    assert obj.object_id in calls
    assert np.array_equal(visible, pixels(render(canvas, culling=False)))


def test_direct_mask_escape_and_transformed_layer_match_unculled_render(scene):
    canvas, document, page = scene
    outside = document.add_layer(page.layer_id, "Outside",
        BoundGeometry.rectangle(700, 700, 150, 150))
    outside.fill_color, outside.border_width = None, 0
    escaped = image_object(canvas, document, outside, 30, 30)
    escaped.ignore_parent_mask = True
    moved = document.add_layer(page.layer_id, "Translated",
        BoundGeometry.rectangle(700, 700, 100, 100))
    moved.translate_x, moved.translate_y = -600, -600
    moved.fill_color = "#ff00ff"
    actual = render(canvas)
    assert actual.pixelColor(40, 40) == QColor("red")
    assert actual.pixelColor(130, 130) == QColor("#ff00ff")
    assert np.array_equal(pixels(actual), pixels(render(canvas, culling=False)))


@pytest.mark.parametrize("modifier", [
    BlurModifier(strength=12), OutlineModifier(thickness=32),
    MirrorModifier(axis_start=(480, 0), axis_end=(480, 480)),
    ArrayModifier(axis_start=(0, 0), axis_end=(-80, 0), count=3),
    TilingModifier(center=(520, 40), side=40),
])
def test_effects_from_outside_viewport_keep_source_pixels(scene, modifier):
    canvas, document, page = scene
    obj = image_object(canvas, document, page, 490, 20)
    document.add_modifier(modifier, [("object", obj.object_id)])
    # Capture once without culling to resolve all effect caches.
    expected = pixels(render(canvas, culling=False))
    canvas._render_bounds.clear()
    assert np.array_equal(pixels(render(canvas)), expected)


def test_outward_gradient_and_compound_children_are_not_clipped_by_bounds(scene):
    canvas, document, page = scene
    layer = document.add_layer(page.layer_id, "Outside",
        BoundGeometry.rectangle(490, 80, 80, 80))
    layer.fill_color, layer.border_width = None, 0
    gradient = document.add_object(layer.layer_id, ColorFillGradientObject())
    gradient.shape_field.reverse_direction = True
    gradient.shape_field.distance = 100
    expected = pixels(render(canvas, culling=False))
    assert np.array_equal(pixels(render(canvas)), expected)
    layer.compound_enabled = True
    child = document.add_layer(layer.layer_id, "Union", BoundGeometry.rectangle(430, 80, 80, 80))
    child.fill_color = "#ff0000"
    canvas.documentChanged.emit(QRectF())
    assert np.array_equal(pixels(render(canvas)), pixels(render(canvas, culling=False)))


def test_model_changes_and_live_ink_invalidate_bounds_without_losing_static_siblings(scene):
    canvas, document, page = scene
    stationary = image_object(canvas, document, page, 40, 40)
    moving = image_object(canvas, document, page, 40, 1000)
    drawing = document.add_object(page.layer_id, RasterObject())
    render(canvas)
    old_stationary = canvas._render_bounds.bounds[("object", stationary.object_id)]
    canvas.tiles.paint_dab(drawing.object_id, QPointF(90, 90), 12, QColor("blue"))
    canvas._emit_raster_dirty(drawing, QRectF(80, 80, 20, 20))
    assert canvas._render_bounds.bounds[("object", stationary.object_id)] is old_stationary
    painted = render(canvas)
    assert painted.pixelColor(90, 90).blue() > 220
    assert np.array_equal(pixels(painted), pixels(render(canvas, culling=False)))
    moving.y = 150
    canvas.documentChanged.emit(QRectF())
    assert render(canvas).pixelColor(50, 160) == QColor("red")
    replacement = image_object(canvas, document, page, 200, 200, "green")
    canvas.hierarchyChanged.emit()
    assert render(canvas).pixelColor(210, 210) == QColor("green")
    assert replacement.visible


def test_export_and_dependency_capture_ignore_viewport_culling(scene):
    canvas, document, page = scene
    obj = image_object(canvas, document, page, 30, 2000)
    render(canvas)
    export = QImage(1080, 4000, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(export)
    assert export.pixelColor(40, 2010) == QColor("red")
    canvas._render_bounds.prepare()
    canvas._interactive_render = True
    canvas._render_modifier_sources.add(("layer", page.layer_id))
    try:
        assert canvas._render_bounds.object_visible(obj, QRectF(0, 0, 480, 480))
    finally:
        canvas._render_modifier_sources.clear()
        canvas._interactive_render = False


def test_vector_control_points_and_wide_strokes_are_included(scene):
    canvas, document, page = scene
    drawing = document.add_object(page.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=500, y=30, width=90),
                             VectorStrokePoint(x=500, y=180, width=90)])]))
    actual = render(canvas)
    assert np.array_equal(pixels(actual), pixels(render(canvas, culling=False)))
    assert actual.pixelColor(470, 80) != QColor("#242428")
    assert drawing.visible


def test_transform_preview_uses_current_state_before_next_scene_prepare(scene):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QPainter

    canvas, document, page = scene
    drawing = document.add_object(page.layer_id, RasterObject(
        interaction_rect=(0, 0, 800, 800)))
    canvas.tiles.paint_dab(
        drawing.object_id, QPointF(700, 700), 30, QColor("red"))
    render(canvas)
    assert not canvas._render_bounds.live_branches
    assert canvas._render_bounds.bounds[("object", drawing.object_id)].left() > 480

    # Selection and a drag can start before Qt paints another scene frame.
    # Raster previews use a cached background and bypass scene preparation.
    canvas.set_selection("object", drawing.object_id)
    canvas._transform_start_quad = [(0, 0), (800, 0), (800, 800), (0, 800)]
    canvas._transform_preview_quad = [(-600, -600), (200, -600), (200, 200), (-600, 200)]
    preview = QImage(480, 480, QImage.Format_ARGB32_Premultiplied)
    preview.fill(Qt.transparent)
    painter = QPainter(preview)
    canvas._render_selected_raster_preview(painter, QRectF(0, 0, 480, 480))
    painter.end()

    assert preview.pixelColor(100, 100) == QColor("red")


@pytest.mark.parametrize("scale,rotation", [(0.2, 30), (1, 90), (2, -15)])
def test_rotated_zoomed_viewport_and_open_shape_extents(scene, scale, rotation):
    canvas, document, page = scene
    document.add_layer(page.layer_id, "Wide curve", BoundGeometry.path([
        PathNode(x=500, y=40, outgoing=(350, 80), width_multiplier=4),
        PathNode(x=500, y=260, incoming=(350, 200), width_multiplier=2),
    ], closed=False), layer_kind="open_shape", style=ShapeStyle(
        primary_color="#ff0000", base_thickness=40,
        outline_thickness=12, start_cap="square", end_cap="round"))
    canvas.scale, canvas.rotation = scale, rotation
    assert np.array_equal(pixels(render(canvas)), pixels(render(canvas, culling=False)))


@pytest.mark.parametrize("flag,value", [
    ("_rendering_halftone_source", True),
    ("_rendering_mask_contributor", 1),
    ("_render_base_alpha", True),
    ("_render_cage_source", True),
    ("_tiling_capture_geometry", object()),
])
def test_independent_source_captures_never_use_scene_bounds(scene, flag, value):
    canvas, document, page = scene
    obj = image_object(canvas, document, page, 30, 2000)
    render(canvas)
    canvas._interactive_render = True
    old = getattr(canvas, flag)
    setattr(canvas, flag, value)
    try:
        assert canvas._render_bounds.object_visible(obj, QRectF(0, 0, 480, 480))
    finally:
        setattr(canvas, flag, old)
        canvas._interactive_render = False

