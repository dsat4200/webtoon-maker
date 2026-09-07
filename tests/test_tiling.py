import math
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, TilingModifier, BlurModifier, modifier_from_dict
from comic_editor.core.tiling import TilingGeometry
from comic_editor.core.tiles import TileStore
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def scene(qapp):
    document = ChapterDocument(width=128, height=128, document_kind="asset")
    page = document.add_page("Page", BoundGeometry.rectangle(0, 0, 128, 128))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(document, TileStore())
    yield canvas, document, page
    canvas.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def add_raster(scene, parent=None):
    canvas, document, page = scene
    obj = document.add_object(parent or page.layer_id, RasterObject(interaction_rect=(0, 0, 128, 128)))
    canvas.set_selection("object", obj.object_id)
    return obj


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_geometry_mapping_agrees_with_cells(shape):
    geometry = TilingGeometry(shape, (13., -9.), 23., 31.)
    rng = np.random.default_rng(123)
    for raw in rng.uniform(-200, 200, (100, 2)):
        point = QPointF(*raw)
        mapped = geometry.point(point)
        assert geometry.path().contains(mapped)
        assert geometry.point(mapped).toTuple() == pytest.approx(mapped.toTuple(), abs=1e-9)
        assert geometry.cell_transform(geometry.cell(point)).map(point).toTuple() == pytest.approx(mapped.toTuple(), abs=1e-8)
        assert any(cell == geometry.cell(point) for cell, _, _ in geometry.cells_intersecting(QRectF(point.x()-.01, point.y()-.01, .02, .02)))


def test_model_order_conflicts_and_roundtrip(scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    document.add_modifier(BlurModifier(), [("object", obj.object_id)])
    tiling = TilingModifier(center=(16, 16), side=32)
    document.add_modifier(tiling, [("object", obj.object_id)])
    assert obj.modifier_ids[0] == tiling.modifier_id
    loaded = ChapterDocument.from_dict(document.to_dict())
    assert loaded.modifiers[tiling.modifier_id].to_dict() == tiling.to_dict()
    before = document.to_dict()
    with pytest.raises(ValueError):
        document.add_modifier(TilingModifier(muted=True), [("object", obj.object_id)])
    assert document.to_dict() == before
    with pytest.raises(ValueError):
        modifier_from_dict({"type": "tiling", "side": 0})


def test_square_repeat_source_crop_and_mute(scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    canvas.tiles.paint_dab(obj.object_id, QPointF(8, 8), 8, QColor("red"), square=True, antialias=False)
    canvas.tiles.paint_dab(obj.object_id, QPointF(80, 80), 8, QColor("blue"), square=True, antialias=False)
    tiling = TilingModifier(center=(16, 16), side=32)
    document.add_modifier(tiling, [("object", obj.object_id)])
    image = render(canvas)
    for y in (8, 40, 72, 104):
        for x in (8, 40, 72, 104):
            assert image.pixelColor(x, y) == QColor("red")
    assert image.pixelColor(80, 80).alpha() == 0
    tiling.muted = True
    assert render(canvas).pixelColor(80, 80) == QColor("blue")


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_no_translucent_seams(shape, scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    canvas.tiles.paint_dab(obj.object_id, QPointF(16, 16), 100, QColor("red"), opacity=.5, square=True, antialias=False)
    document.add_modifier(TilingModifier(shape=shape, center=(16, 16), side=17, rotation=17), [("object", obj.object_id)])
    image = render(canvas).convertToFormat(QImage.Format_RGBA8888)
    rgba = np.frombuffer(image.constBits(), np.uint8).reshape(128, 128, 4)
    assert np.all(rgba[2:-2, 2:-2, 3] == 128)


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_large_wrapped_dab_is_composited_once(shape):
    store = TileStore()
    geometry = TilingGeometry(shape, (20, 20), 16)
    store.paint_dab("drawing", QPointF(300, 300), 200, QColor("red"), opacity=.5,
        tiling=(geometry, QTransform()), antialias=False)
    assert store._tiles["drawing"][(0, 0)].pixelColor(20, 20).alpha() == 128


def test_raster_wrap_and_undo(scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    tiling = TilingModifier(center=(16, 16), side=32)
    document.add_modifier(tiling, [("object", obj.object_id)])
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_stroke(QPointF(95, 10), 1.)
    canvas._continue_stroke(QPointF(98, 10), 1.)
    canvas._end_stroke()
    image = render(canvas)
    assert image.pixelColor(1, 10).alpha() > 0
    assert image.pixelColor(31, 10).alpha() > 0
    canvas.command_stack.undo()
    assert render(canvas).pixelColor(1, 10).alpha() == 0
    canvas.command_stack.redo()
    assert render(canvas).pixelColor(1, 10).alpha() > 0


def test_vector_stroke_wrap_stays_editable(scene):
    from comic_editor.core.models import VectorDrawingObject
    canvas, document, page = scene
    drawing = document.add_object(page.layer_id, VectorDrawingObject())
    canvas.set_selection("object", drawing.object_id)
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", drawing.object_id)])
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_vector_gesture(drawing, QPointF(94, 12), 1.)
    canvas._continue_vector_gesture(QPointF(100, 12), 1.)
    canvas._end_vector_gesture()
    assert len(drawing.strokes) >= 2
    assert all(s.clip_polygon and s.tiling_group for s in drawing.strokes)
    assert render(canvas).pixelColor(1, 12).alpha() > 0
    assert render(canvas).pixelColor(31, 12).alpha() > 0
    restored = ChapterDocument.from_dict(document.to_dict())
    assert restored.objects[drawing.object_id].to_dict() == drawing.to_dict()


def test_periodic_fill_connects_across_edges_only(scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", obj.object_id)])
    for x in (8, 24):
        canvas.tiles.paint_segment(obj.object_id, QPointF(x, -2), QPointF(x, 34), 2, 2, QColor("black"), antialias=False)
    assert canvas._apply_raster_fill(obj, QPointF(95, 16), color=QColor("red"), profile_overrides={"antialiasing": False, "tolerance": 0, "close_gap": False})
    image = render(canvas)
    assert image.pixelColor(1, 16) == QColor("red")
    assert image.pixelColor(31, 16) == QColor("red")
    assert image.pixelColor(16, 16).alpha() == 0


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_periodic_fill_empty_tile_and_cancellation(shape):
    store = TileStore()
    profile = {"_tiling_context": (TilingGeometry(shape, (24, 24), 20, 19), QTransform()), "antialiasing": False}
    before = {}
    assert store.advanced_fill("r", QPointF(91, 93), QRectF(0, 0, 5, 5), QColor("red"), profile, before,
                               cancel_check=lambda: True).isEmpty()
    assert not before and not store.object_tiles("r")
    assert not store.advanced_fill("r", QPointF(91, 93), QRectF(0, 0, 5, 5), QColor("red"), profile, before).isEmpty()
    assert store._tiles["r"][(0, 0)].pixelColor(24, 24) == QColor("red")


def test_shape_fills_with_children_and_retains_border(scene):
    canvas, document, page = scene
    layer = document.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(8, 8, 112, 112))
    layer.fill_color, layer.border_color, layer.border_width = "#FF00FF00", "#FF0000FF", 2
    obj = add_raster(scene, layer.layer_id)
    obj.ignore_parent_mask = True
    canvas.tiles.paint_dab(obj.object_id, QPointF(16, 16), 8, QColor("red"), square=True, antialias=False)
    document.add_modifier(TilingModifier(center=(20, 20), side=32), [("layer", layer.layer_id)])
    image = render(canvas)
    assert image.pixelColor(16, 16) == QColor("red")
    assert image.pixelColor(48, 48) == QColor("red")
    assert image.pixelColor(24, 24) == QColor(0, 255, 0)
    assert image.pixelColor(8, 40) == QColor("blue")
    assert image.pixelColor(4, 4).alpha() == 0
    before = document.to_dict()
    with pytest.raises(ValueError):
        document.add_modifier(TilingModifier(), [("object", obj.object_id)])
    assert document.to_dict() == before


def test_raster_apply_matches_render(scene):
    from comic_editor.ui.baking import apply_raster_modifiers
    canvas, document, page = scene
    obj = add_raster(scene)
    canvas.tiles.paint_dab(obj.object_id, QPointF(8, 8), 6, QColor("red"), square=True)
    modifier = TilingModifier(center=(16, 16), side=32)
    document.add_modifier(modifier, [("object", obj.object_id)])
    image = render(canvas)
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert render(canvas) == image
    canvas.command_stack.undo()
    assert render(canvas) == image


@pytest.mark.parametrize("mode", ["stroke", "point", "intersection"])
def test_vector_eraser_wraps_from_distant_cell(scene, mode):
    from comic_editor.core.models import VectorDrawingObject
    canvas, document, page = scene
    drawing = document.add_object(page.layer_id, VectorDrawingObject())
    canvas.set_selection("object", drawing.object_id)
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", drawing.object_id)])
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._begin_vector_gesture(drawing, QPointF(94, 12), 1.)
    canvas._continue_vector_gesture(QPointF(100, 12), 1.)
    canvas._end_vector_gesture()
    assert render(canvas).pixelColor(1, 12).alpha() > 0
    canvas.settings.vector_eraser_mode = mode
    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._begin_vector_gesture(drawing, QPointF(97, 12), 1.)
    canvas._end_vector_gesture()
    assert render(canvas).pixelColor(1, 12).alpha() == 0
    canvas.command_stack.undo()
    assert render(canvas).pixelColor(1, 12).alpha() > 0


def test_gizmo_changes_crop_and_is_undoable(scene):
    canvas, document, page = scene
    canvas.settings.snap_to_grid = False
    obj = add_raster(scene)
    modifier = TilingModifier(center=(32, 32), side=32)
    document.add_modifier(modifier, [("object", obj.object_id)])
    canvas.modifier_mode = True
    canvas.active_modifier_id = modifier.modifier_id
    press = canvas.document_to_widget(QPointF(32, 32))
    assert canvas._begin_modifier_handle(press)
    assert canvas._move_modifier_handle(canvas.document_to_widget(QPointF(42, 52)))
    canvas._finish_modifier_handle()
    assert modifier.center == pytest.approx((42, 52))
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].center == (32, 32)


def test_reparent_conflict_is_atomic(scene):
    canvas, document, page = scene
    layer = document.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(0, 0, 100, 100))
    obj = add_raster(scene)
    document.add_modifier(TilingModifier(), [("object", obj.object_id)])
    document.add_modifier(TilingModifier(), [("layer", layer.layer_id)])
    before = document.to_dict()
    with pytest.raises(ValueError):
        document.move_entity("object", obj.object_id, layer.layer_id, 0)
    assert document.to_dict() == before


def test_projective_raster_apply_is_pixel_identical(scene):
    from comic_editor.ui.baking import apply_raster_modifiers
    canvas, document, page = scene
    obj = add_raster(scene)
    obj.transform_frame = (0, 0, 128, 128)
    obj.transform_quad = [(3, 0), (120, 15), (95, 123), (0, 90)]
    canvas.tiles.paint_dab(obj.object_id, QPointF(15, 15), 12, QColor("red"))
    modifier = TilingModifier(center=(16, 16), side=32, rotation=15)
    document.add_modifier(modifier, [("object", obj.object_id)])
    image = render(canvas)
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert render(canvas) == image
    canvas.command_stack.undo()
    assert render(canvas) == image


def test_direct_tiled_asset_has_finite_original_bounds(scene):
    from comic_editor.core.assets import extract_asset
    canvas, document, page = scene
    obj = add_raster(scene)
    canvas.tiles.paint_dab(obj.object_id, QPointF(8, 8), 6, QColor("red"))
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", obj.object_id)])
    manifest, tiles = extract_asset(document, canvas.tiles, "object", obj.object_id, "Tile")
    assert manifest.visual_bounds[2:] == (128, 128)
    assert any(isinstance(m, TilingModifier) for m in manifest.document.modifiers.values())


def test_tiled_asset_retains_compound_boundary(scene):
    from comic_editor.core.assets import extract_asset, _tiling_boundary_path
    canvas, document, page = scene
    shape = document.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(0, 0, 100, 100))
    shape.compound_enabled = True
    hole = document.add_layer(shape.layer_id, "Hole", BoundGeometry.circle(50, 50, 15))
    hole.compound_operation = "subtract"
    document.add_layer(shape.layer_id, "Extension", BoundGeometry.rectangle(80, 0, 40, 100))
    obj = add_raster(scene, shape.layer_id)
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", obj.object_id)])
    manifest, _ = extract_asset(document, canvas.tiles, "object", obj.object_id, "Tile")
    assert manifest.visual_bounds[2:] == (120, 100)
    boundary = _tiling_boundary_path(manifest.document, manifest.document.layers[manifest.document.root_page_ids[0]])
    assert not boundary.contains(QPointF(114, 114))
    assert boundary.contains(QPointF(174, 84))


def test_fill_gesture_replay_keeps_periodic_context(scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    document.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", obj.object_id)])
    canvas.set_tool(ToolKind.FILL)
    canvas._begin_fill_gesture(obj, QPointF(98, 12))
    canvas._finish_fill_gesture(obj)
    assert render(canvas).pixelColor(1, 12).alpha() > 0
    state = canvas._fill_replay_state
    assert state.profile["_tiling_context"]
    assert canvas._fill_replay_is_eligible(state)
    canvas.request_fill_tolerance_replay(20, immediate=True)
    assert render(canvas).pixelColor(1, 12).alpha() > 0


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_wrapped_full_coverage_has_no_seam(shape, scene):
    canvas, document, page = scene
    obj = add_raster(scene)
    modifier = TilingModifier(shape=shape, center=(24, 24), side=20, rotation=19)
    document.add_modifier(modifier, [("object", obj.object_id)])
    canvas.tiles.paint_dab(obj.object_id, QPointF(98, 90), 180, QColor("red"), opacity=.5,
        tiling=canvas._tiling_brush_context(obj), antialias=False)
    image = render(canvas).convertToFormat(QImage.Format_RGBA8888)
    alpha = np.frombuffer(image.constBits(), np.uint8).reshape(128, 128, 4)[..., 3]
    assert np.all(alpha[2:-2, 2:-2] == 128)


def test_sampling_cache_reuses_coordinates_but_not_pixels():
    from comic_editor.ui.tiling_rendering import repeat_image, RepeatMapCache
    source = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("red"))
    cache = RepeatMapCache(100000)
    geometry = TilingGeometry("triangle", (16, 16), 16)
    first = repeat_image(source, QRectF(0, 0, 32, 32), geometry, QRectF(0, 0, 48, 48), nearest=True, cache=cache)
    keys = tuple(cache.entries)
    source.fill(QColor("blue"))
    second = repeat_image(source, QRectF(0, 0, 32, 32), geometry, QRectF(0, 0, 48, 48), nearest=True, cache=cache)
    assert first.pixelColor(20, 20) == QColor("red")
    assert second.pixelColor(20, 20) == QColor("blue")
    assert tuple(cache.entries) == keys
    assert cache.bytes <= cache.budget


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
@pytest.mark.parametrize("fill", [False, True])
@pytest.mark.parametrize("inherited", [False, True])
def test_transformed_wrapped_coverage_has_no_seam(shape, fill, inherited, scene):
    canvas, document, page = scene
    parent = None
    if inherited:
        parent = document.add_layer(page.layer_id, "Tiled shape", BoundGeometry.rectangle(0, 0, 128, 128))
        parent.fill_color, parent.border_width = None, 0
    obj = add_raster(scene, parent.layer_id if parent else None)
    obj.transform_frame = (0, 0, 128, 128)
    obj.transform_quad = [(3, 0), (120, 15), (95, 123), (0, 90)]
    document.add_modifier(TilingModifier(shape=shape, center=(24, 24), side=20, rotation=19),
        [("layer", parent.layer_id)] if parent else [("object", obj.object_id)])
    context = canvas._tiling_brush_context(obj)
    if fill:
        canvas.tiles.advanced_fill(obj.object_id, QPointF(50, 50), QRectF(), QColor("red"),
            {"_tiling_context": context, "antialiasing": False, "opacity": 50})
    else:
        canvas.tiles.paint_dab(obj.object_id, QPointF(98, 90), 180, QColor("red"), opacity=.5,
            tiling=context, antialias=False)
    image = render(canvas).convertToFormat(QImage.Format_RGBA8888)
    alpha = np.frombuffer(image.constBits(), np.uint8).reshape(128, 128, 4)[..., 3]
    assert np.all(alpha[2:-2, 2:-2] == 128)


@pytest.mark.parametrize("shape", ["square", "hexagon", "triangle"])
def test_exact_vertices_edges_and_negative_cells_have_stable_ownership(shape):
    geometry = TilingGeometry(shape, (-19., 11.), 3., 47.)
    vertices = geometry.vertices()
    for first, last in zip(vertices, vertices[1:]+vertices[:1]):
        for point in (QPointF(*first), (QPointF(*first)+QPointF(*last))/2):
            mapped = geometry.point(point)
            assert geometry.point(mapped).toTuple() == pytest.approx(mapped.toTuple(), abs=1e-9)
            assert geometry.cell_transform(geometry.cell(point)).map(point).toTuple() == pytest.approx(mapped.toTuple(), abs=1e-9)


def test_linked_grid_and_conflicting_load(scene):
    canvas, document, page = scene
    first, second = add_raster(scene), add_raster(scene)
    tile = TilingModifier(center=(32, 32), side=20)
    targets = [("object", first.object_id), ("object", second.object_id)]
    document.add_modifier(tile, targets)
    transform = QTransform.fromTranslate(10, 20)
    canvas._transform_single_target_focal_modifiers("object", first.object_id, transform)
    assert tile.center == (32, 32)
    document.set_modifier_targets(tile.modifier_id, targets[:1])
    canvas._transform_single_target_focal_modifiers("object", first.object_id, transform)
    assert tile.center == (42, 52)
    canvas._transform_single_target_focal_modifiers("object", first.object_id, QTransform.fromScale(2, 1))
    assert tile.side == 20
    parent = document.add_layer(page.layer_id, "Parent", BoundGeometry.rectangle(0, 0, 128, 128))
    document.move_entity("object", first.object_id, parent.layer_id, 0)
    payload = document.to_dict()
    parent.modifier_ids.append(tile.modifier_id)
    with pytest.raises(ValueError):
        ChapterDocument.from_dict(document.to_dict())
    parent.modifier_ids.clear()
    assert document.to_dict() == payload


def test_offscreen_source_compound_hole_and_rasterize(scene):
    from comic_editor.ui.baking import rasterize
    canvas, document, page = scene
    shape = document.add_layer(page.layer_id, "Compound", BoundGeometry.rectangle(8, 8, 112, 112))
    shape.compound_enabled = True
    shape.fill_color, shape.border_width = None, 0
    hole = document.add_layer(shape.layer_id, "Hole", BoundGeometry.circle(64, 64, 12))
    hole.compound_operation = "subtract"
    hole.fill_color, hole.border_width = None, 0
    drawing = add_raster(scene, shape.layer_id)
    drawing.ignore_parent_mask = True
    canvas.tiles.paint_dab(drawing.object_id, QPointF(-16, -16), 100, QColor("red"), antialias=False)
    document.add_modifier(TilingModifier(center=(-16, -16), side=32), [("layer", shape.layer_id)])
    image = render(canvas)
    assert image.pixelColor(16, 16) == QColor("red")
    assert image.pixelColor(64, 64).alpha() == 0
    assert image.pixelColor(4, 4).alpha() == 0
    rasterize(canvas, "layer", shape.layer_id)
    assert render(canvas) == image
    canvas.command_stack.undo()
    assert render(canvas) == image


@pytest.mark.parametrize("offset", [0, -64])
def test_periodic_fill_uses_vector_reference_boundaries(scene, offset):
    from comic_editor.core.models import VectorDrawingObject, VectorStroke, VectorStrokePoint
    canvas, document, page = scene
    shape = document.add_layer(page.layer_id, "Tile", BoundGeometry.rectangle(0, 0, 128, 128))
    shape.fill_color, shape.border_width = None, 0
    drawing = add_raster(scene, shape.layer_id)
    document.add_object(shape.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=x+offset, y=-2+offset, width=2), VectorStrokePoint(x=x+offset, y=34+offset, width=2)])
        for x in (8, 24)]))
    document.add_modifier(TilingModifier(center=(16+offset, 16+offset), side=32), [("layer", shape.layer_id)])
    canvas._apply_raster_fill(drawing, QPointF(95, 16), color=QColor("red"), profile_overrides={
        "reference_mode": "all_visible", "antialiasing": False, "tolerance": 0, "close_gap": False})
    image = render(canvas)
    assert image.pixelColor(1, 16) == QColor("red")
    assert image.pixelColor(31, 16) == QColor("red")
    assert image.pixelColor(16, 16).alpha() == 0


def test_empty_drawing_defaults_and_shape_controls(scene):
    from comic_editor.ui.modifier_controls import ModifierControls
    from comic_editor.ui.tiling_controls import TilingSettingsControls
    canvas, document, page = scene
    shape = document.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(20, 30, 80, 60))
    drawing = add_raster(scene, shape.layer_id)
    panel = ModifierControls(canvas)
    try:
        panel.add_modifier("tiling")
        modifier = document.modifiers[drawing.modifier_ids[0]]
        assert modifier.center == (60, 60)
        assert modifier.side == 30
        controls = panel.findChild(TilingSettingsControls)
        controls.shape_control.setCurrentIndex(controls.shape_control.findData("hexagon"))
        assert (modifier.shape, modifier.center, modifier.side, modifier.rotation) == ("hexagon", (60, 60), 30, 0)
        canvas.command_stack.undo()
        assert canvas.chapter.modifiers[modifier.modifier_id].shape == "square"
    finally:
        panel.deleteLater()


def test_cage_bake_retains_and_deforms_vector_clipping():
    from comic_editor.core.cage import CageGrid
    from comic_editor.core.models import VectorDrawingObject, VectorStroke, VectorStrokePoint
    from comic_editor.ui.cage_vectors import warp_vector
    clip = [(0, 0), (16, 0), (16, 16), (0, 16)]
    drawing = VectorDrawingObject(strokes=[VectorStroke(clip_polygon=clip, tiling_group="group", points=[
        VectorStrokePoint(x=8, y=8, width=30)])])
    grid = CageGrid(frame=(0, 0, 16, 16))
    grid.points = [(x+25, y+7) for x, y in grid.rest_points()]
    warped = warp_vector(drawing, grid, QTransform())
    assert warped.strokes[0].tiling_group == "group"
    assert np.asarray(warped.strokes[0].clip_polygon) == pytest.approx(np.asarray(clip)+(25, 7))
