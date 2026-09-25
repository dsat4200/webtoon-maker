"""Exact cache reuse for compound speech bubbles during manipulation."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage, QTransform
from scipy.spatial import cKDTree
from scipy.ndimage import binary_dilation

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, MirrorModifier, ParameterMaskBinding,
    PathContour, ScreamModifier, ShapeStyle, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.stroke_geometry import sample_path
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import compound_strokes
from comic_editor.ui.shape_contours import compile_bound


@pytest.fixture
def bubble(qapp):
    chapter = ChapterDocument(height=500)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 500))
    page.fill_color, page.border_width = None, 0
    bound = BoundGeometry.rectangle(160, 140, 300, 140)
    bound.primitive = "ellipse"
    layer = chapter.add_layer(page.layer_id, "Bubble", bound, style=ShapeStyle(
        primary_color="#FFFFFFFF", outline_color="#FF112233", outline_thickness=5))
    layer.compound_enabled = True
    modifier = ScreamModifier(height=47, width=69, roundness=0)
    chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(700, 500)
    canvas.set_document(chapter, TileStore())
    yield canvas, chapter, layer, modifier
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def _pixels(canvas):
    image = QImage(1080, 500, QImage.Format_RGBA8888)
    canvas.render_preview(image)
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize("width,enabled", [(1., True), (2.3, True), (.4, False)])
def test_uniform_outline_attribution_is_exact_without_resampling(bubble, monkeypatch, width, enabled):
    canvas, _, layer, _ = bubble
    bound = BoundGeometry.circle(180, 160, 40)
    bound.additional_contours.append(PathContour(
        nodes=BoundGeometry.circle(320, 160, 30).nodes, closed=True))
    for contour in bound.iter_contours():
        for node in contour.nodes:
            node.outline_multiplier, node.outline_enabled = width, enabled
    loops = sample_path(canvas.bound_path(bound), 5)
    # Keep the former nearest-source calculation as a reference. Uniform
    # styles must produce exactly the same samples, including disabled ink.
    samples, widths, flags = [], [], []
    for contour, source in zip(compile_bound(bound), bound.iter_contours()):
        for edge in contour.edges:
            first, last = source.nodes[edge.index], source.nodes[(edge.index+1) % len(source.nodes)]
            length = edge.path.length()
            count = min(8192, max(2, int(length*2)+1))
            for fraction in np.linspace(0, 1, count, endpoint=False):
                samples.append(edge.path.pointAtPercent(edge.path.percentAtLength(length*fraction)).toTuple())
                widths.append(first.outline_multiplier+(last.outline_multiplier-first.outline_multiplier)*fraction)
                flags.append(first.outline_enabled)
    tree = cKDTree(samples)
    expected = [(np.asarray(widths)[index], np.asarray(flags)[index])
                for loop in loops for index in [tree.query(loop.points)[1]]]
    monkeypatch.setattr(compound_strokes, "compile_bound", lambda _: pytest.fail("Uniform attribution must not resample curves"))
    actual = compound_strokes._outline_styles(bound, loops)
    for a, b in zip(actual, expected):
        assert np.array_equal(a[0], b[0])
        assert np.array_equal(a[1], b[1])


@pytest.mark.parametrize("placement", ["translate", "affine", "projective"])
def test_unmasked_placement_reuses_appearance_and_matches_fresh_pixels(bubble, placement):
    canvas, _, layer, _ = bubble
    original = compound_strokes.appearance(canvas, layer)
    if placement == "translate":
        layer.translate_x, layer.translate_y = 21, 13
    else:
        layer.transform_frame = layer.bound.bbox()
        layer.transform_quad = [(170, 120), (490, 160),
                                (460, 300), (140 if placement == "affine" else 170, 260)]
    assert compound_strokes.appearance(canvas, layer) is original
    cached = _pixels(canvas)
    canvas._compound_stroke_appearances.clear()
    canvas._outline_cache.clear()
    canvas._clear_compound_path_cache()
    fresh = _pixels(canvas)
    assert compound_strokes.appearance(canvas, layer) is not original
    assert np.array_equal(cached, fresh)


def test_world_mask_placement_and_geometry_edits_invalidate_appearance(bubble):
    canvas, chapter, layer, modifier = bubble
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["height"] = ParameterMaskBinding(mask.mask_id, 0, 47)
    canvas.tiles.paint_dab(mask.mask_id, QPointF(160, 210), 180, QColor("white"), square=True)
    before = compound_strokes.appearance(canvas, layer)
    layer.translate_x = 240
    moved = compound_strokes.appearance(canvas, layer)
    assert moved is not before
    assert any(not np.array_equal(a.points, b.points) for a, b in zip(before.loops, moved.loops))
    modifier.parameter_masks.clear()
    unmasked = compound_strokes.appearance(canvas, layer)
    layer.bound.nodes[0].x -= 20
    resized = compound_strokes.appearance(canvas, layer)
    assert resized is not unmasked
    layer.vertex_radius = 12
    assert compound_strokes.appearance(canvas, layer) is not resized


def test_compound_card_metadata_reuses_geometry_but_effect_parameter_invalidates(bubble, monkeypatch):
    canvas, _chapter, layer, modifier = bubble
    original = compound_strokes.appearance(canvas, layer)
    expected = _pixels(canvas)
    calls = []
    deform = compound_strokes.deform_loop
    monkeypatch.setattr(compound_strokes, "deform_loop", lambda *args, **kwargs:
        (calls.append(True), deform(*args, **kwargs))[1])
    modifier.name = "My bubble border"
    modifier.expanded = not modifier.expanded
    assert compound_strokes.appearance(canvas, layer) is original
    assert np.array_equal(_pixels(canvas), expected)
    assert not calls
    modifier.height += 7
    assert compound_strokes.appearance(canvas, layer) is not original
    assert calls
    canvas._clear_compound_path_cache()
    assert not np.array_equal(_pixels(canvas), expected)


@pytest.mark.parametrize("operation", ["add", "subtract", "none"])
def test_direct_line_iteration_preserves_final_pixels(bubble, monkeypatch, operation):
    from comic_editor.core.vector_geometry import flatten_cubic
    from comic_editor.ui.shape_contours import path_segments
    from comic_editor.ui import shape_outline_compound
    canvas, chapter, layer, _ = bubble
    if operation != "none":
        tail = chapter.add_layer(layer.layer_id, "Tail", BoundGeometry.polygon(
            [(330, 250), (420, 350), (370, 250)]), style=copy.deepcopy(layer.shape_style))
        tail.compound_operation = operation
    after = _pixels(canvas)

    def legacy_lines(path, tolerance):
        for segment in path_segments(path):
            points = [QPointF(*p.point) for p in flatten_cubic(segment.cubic, tolerance)]
            for a, b in zip(points, points[1:]):
                if shape_outline_compound._distance(a, b) > 1e-9:
                    yield a, b

    monkeypatch.setattr(shape_outline_compound, "_lines", legacy_lines)
    canvas._compound_stroke_appearances.clear()
    canvas._outline_cache.clear()
    canvas._clear_compound_path_cache()
    before = _pixels(canvas)
    assert np.array_equal(after, before)


def test_nested_operand_mappings_are_local_and_world_reflections_stay_world_based(bubble):
    from comic_editor.ui.modifier_rendering import reflection_transform
    from comic_editor.ui.shape_outline_compound import transform_key
    canvas, chapter, layer, _ = bubble
    layer.transform_frame = layer.bound.bbox()
    layer.transform_quad = [(170, 120), (490, 160), (460, 300), (140, 260)]
    child = chapter.add_layer(layer.layer_id, "Child", BoundGeometry.rectangle(200, 200, 100, 80))
    child.translate_x, child.translate_y = 13, -21
    child.compound_enabled = True
    grandchild = chapter.add_layer(child.layer_id, "Grandchild", BoundGeometry.circle(280, 240, 30))
    grandchild.transform_frame = grandchild.bound.bbox()
    grandchild.transform_quad = [(250, 205), (310, 215), (305, 270), (245, 260)]
    mirror = MirrorModifier(compound_operation="add")
    chapter.add_modifier(mirror, [("layer", child.layer_id)])
    path = canvas.layer_effective_path(layer.layer_id)
    sources = canvas._compound_outline_mesh(layer, path, sources_only=True)
    assert transform_key(sources[0].mapping) == transform_key(QTransform())
    assert transform_key(sources[1].mapping) == transform_key(canvas._layer_parent_transform(child))
    assert transform_key(sources[2].mapping) == transform_key(
        canvas._layer_parent_transform(grandchild) * canvas._layer_parent_transform(child))
    inverse, _ = canvas.layer_world_transform(layer.layer_id).inverted()
    assert transform_key(sources[3].mapping) == transform_key(
        canvas.layer_world_transform(child.layer_id) * reflection_transform(mirror) * inverse)
    assert transform_key(sources[4].mapping) == transform_key(
        canvas.layer_world_transform(grandchild.layer_id) * reflection_transform(mirror) * inverse)


@pytest.mark.parametrize("operation", ["add", "subtract", "none"])
def test_collinear_reduction_preserves_geometry_and_only_changes_boundary_antialiasing(bubble, monkeypatch, operation):
    canvas, chapter, layer, _ = bubble
    if operation != "none":
        tail = chapter.add_layer(layer.layer_id, "Tail", BoundGeometry.polygon(
            [(330, 250), (420, 350), (370, 250)]), style=copy.deepcopy(layer.shape_style))
        tail.compound_operation = operation
    value = compound_strokes.appearance(canvas, layer)
    points = value.loops[0].points
    corners = np.asarray([node.position for node in value.bound.nodes])
    assert len(corners) < len(points) / 10
    starts, ends = corners, np.roll(corners, -1, axis=0)
    direction = ends-starts
    offsets = points[:, None, :]-starts
    fractions = np.clip(np.sum(offsets*direction, axis=2)/np.sum(direction**2, axis=1), 0, 1)
    distances = np.linalg.norm(offsets-fractions[..., None]*direction, axis=2)
    assert float(np.max(np.min(distances, axis=1))) < 1e-8
    after = _pixels(canvas).reshape(500, 1080, 4)
    monkeypatch.setattr(compound_strokes, "_boundary_indexes", lambda loop, _: np.arange(len(loop.points)))
    canvas._compound_stroke_appearances.clear()
    canvas._outline_cache.clear()
    canvas._clear_compound_path_cache()
    before = _pixels(canvas).reshape(500, 1080, 4)
    changed = np.any(before != after, axis=2)
    boundary = np.zeros(changed.shape, dtype=bool)
    boundary[1:] |= np.any(before[1:] != before[:-1], axis=2)
    boundary[:-1] |= np.any(before[1:] != before[:-1], axis=2)
    boundary[:, 1:] |= np.any(before[:, 1:] != before[:, :-1], axis=2)
    boundary[:, :-1] |= np.any(before[:, 1:] != before[:, :-1], axis=2)
    assert not np.any(changed & ~binary_dilation(boundary))
    # Qt tessellates a long line and many collinear lines slightly differently.
    # Preserve all filled regions and constrain the small edge coverage change.
    delta = np.abs(before.astype(int)-after.astype(int))
    assert np.sum(delta) < 10000
    if np.any(changed):
        assert np.quantile(delta[changed], .99) <= 8


@pytest.mark.parametrize("custom_style", ["width", "disabled"])
def test_collinear_reduction_keeps_every_varying_style_anchor(bubble, custom_style):
    canvas, _, layer, _ = bubble
    if custom_style == "width":
        layer.bound.nodes[0].outline_multiplier = 2
    else:
        layer.bound.nodes[0].outline_enabled = False
    value = compound_strokes.appearance(canvas, layer)
    assert len(value.bound.nodes) == len(value.loops[0].points)


def test_root_transform_reuses_the_attributed_outline(bubble):
    canvas, _, layer, _ = bubble
    layer.bound.nodes[0].outline_multiplier = 1.1
    path = compound_strokes.appearance(canvas, layer).path
    original = canvas._compound_outline_mesh(layer, path, .0625)
    count = canvas._outline_cache.builds["attribution"]
    layer.transform_frame = layer.bound.bbox()
    for offset in range(3):
        layer.transform_quad = [(170, 120), (490+offset*3, 160),
                                (460+offset*3, 300), (140, 260)]
        assert canvas._compound_outline_mesh(layer, path, .0625) is original
        assert canvas._outline_cache.builds["attribution"] == count
