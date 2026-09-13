"""Angular Scream boundaries stay straight even on curved source contours."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.models import BoundGeometry, ChapterDocument, ScreamModifier, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.stroke_geometry import StrokeLoop, deform_loop, distances, loop_path, normals, sample_path
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.compound_strokes import appearance


def parameters(loop, **overrides):
    values = {"width": 69., "height": 47., "roundness": 0., "intensity": 100.}
    values.update(overrides)
    return {name: np.broadcast_to(value, (len(loop.points),)) for name, value in values.items()}.__getitem__


def corners(points):
    edges = np.roll(points, -1, axis=0) - points
    previous = np.roll(edges, 1, axis=0)
    cross = previous[:, 0] * edges[:, 1] - previous[:, 1] * edges[:, 0]
    lengths = np.linalg.norm(previous, axis=1) * np.linalg.norm(edges, axis=1)
    return np.flatnonzero(np.abs(cross) > np.maximum(1e-10, lengths * 1e-7))


def ellipse_loop():
    path = QPainterPath()
    path.addEllipse(QRectF(120, 95, 300, 210))
    return sample_path(path, 8)[0]


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("vary_width", [False, True])
def test_zero_roundness_has_only_straight_sides_between_tips_and_valleys(reverse, vary_width):
    loop = ellipse_loop()
    if reverse:
        loop = StrokeLoop(loop.points[::-1].copy(), loop.width)
    source = loop.points.copy()
    width = np.linspace(30, 90, len(source)) if vary_width else 69.
    parameter = parameters(loop, width=width)
    result, opacity = deform_loop(loop, ScreamModifier(), parameter)
    expected_spikes = round(np.sum(distances(source)[1] / width))
    # A polygonal spike has exactly two corners, while a normal-offset wave
    # inherits curvature along every side from the underlying ellipse.
    assert len(corners(result.points)) == expected_spikes * 2
    assert result.points.shape == source.shape
    assert result.width == loop.width
    np.testing.assert_array_equal(loop.points, source)
    np.testing.assert_array_equal(opacity, np.ones(len(source)))


@pytest.mark.parametrize("override", [{"height": 0}, {"intensity": 0}])
def test_disabled_scream_preserves_exact_source_geometry(override):
    loop = ellipse_loop()
    result, _ = deform_loop(loop, ScreamModifier(), parameters(loop, **override))
    np.testing.assert_array_equal(result.points, loop.points)


@pytest.mark.parametrize("shape", ["ellipse", "small_ellipse", "sparse"])
@pytest.mark.parametrize("width", [69, 400])
def test_full_roundness_preserves_original_rounded_profile(shape, width):
    loop = ellipse_loop()
    if shape == "small_ellipse":
        loop = StrokeLoop(loop.points / 10, loop.width)
    elif shape == "sparse":
        loop = StrokeLoop(np.array([[0., 0.], [1., 0.], [0., 1.]]), 1)
    parameter = parameters(loop, roundness=100, width=width)
    rates = distances(loop.points)[1] / parameter("width")
    phase = np.r_[0., np.cumsum(rates[:-1])] * max(1, round(rates.sum())) / rates.sum() % 1
    rounded = np.sqrt(np.maximum(0., 1 - (phase * 2 - 1) ** 2))
    expected = loop.points + normals(loop.points) * (rounded * parameter("height"))[:, None]
    result, _ = deform_loop(loop, ScreamModifier(), parameter)
    np.testing.assert_allclose(result.points, expected, atol=1e-10)
    if shape != "sparse":
        assert len(corners(result.points)) > 10 * round(rates.sum())


@pytest.mark.parametrize("points", [
    [[0., 0.], [1., 0.], [0., 1.]],
    [[0., 0.], [100., 0.], [100., 1.]],
])
def test_very_short_sample_loop_keeps_a_closed_polygon(points):
    loop = StrokeLoop(np.asarray(points), 1)
    result, _ = deform_loop(loop, ScreamModifier(), parameters(loop, width=400, height=10))
    assert result.points.shape == loop.points.shape
    assert np.isfinite(result.points).all()
    assert len(corners(result.points)) == 3
    x, y = result.points.T
    assert abs(np.sum(x * np.roll(y, -1) - y * np.roll(x, -1))) > 1


@pytest.mark.parametrize("roundness", [0, 35, 100])
def test_zero_height_mask_regions_remain_unchanged(roundness):
    loop = ellipse_loop()
    inactive = loop.points[:, 0] < 270
    height = np.where(inactive, 0., 47.)
    result, _ = deform_loop(loop, ScreamModifier(), parameters(loop, height=height, roundness=roundness))
    fully_active, _ = deform_loop(loop, ScreamModifier(), parameters(loop, roundness=roundness))
    np.testing.assert_array_equal(result.points[inactive], loop.points[inactive])
    # Sides whose endpoints are in the fully active region retain their exact
    # unmasked shape, including straight sides at zero roundness.
    interior = loop.points[:, 0] > 320
    np.testing.assert_allclose(result.points[interior], fully_active.points[interior], atol=1e-10)


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()


@pytest.fixture
def bubble(qapp):
    chapter = ChapterDocument(height=400, background="#FFE2E7EB")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 500, 400))
    page.fill_color, page.border_width = None, 0
    style = ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF112233", outline_thickness=0)
    bound = BoundGeometry(nodes=BoundGeometry._ellipse_nodes(120, 95, 300, 210), closed=True, primitive="ellipse")
    root = chapter.add_layer(page.layer_id, "Ellipse", bound, style=style)
    root.compound_enabled = True
    tail = chapter.add_layer(root.layer_id, "Tail", BoundGeometry.polygon([(145, 210), (60, 335), (190, 270)]), style=copy.deepcopy(style))
    tail.compound_operation = "add"
    modifier = ScreamModifier(height=47, width=69, roundness=0)
    chapter.add_modifier(modifier, [("layer", root.layer_id)])
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(500, 400)
    canvas.set_document(chapter, TileStore())
    yield canvas, root, tail, modifier
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def test_compound_ellipse_renders_straight_polygon_sides_and_preserves_tail(bubble):
    canvas, root, tail, _ = bubble
    original = copy.deepcopy(root.bound.to_dict())
    value = appearance(canvas, root)
    points = value.loops[0].points
    indexes = corners(points)
    assert len(indexes) == 24
    expected_path = loop_path(points).united(canvas.bound_path(tail.bound))
    expected = QImage(500, 400, QImage.Format_ARGB32_Premultiplied)
    expected.fill(QColor(canvas.chapter.background))
    painter = QPainter(expected)
    painter.setRenderHint(QPainter.Antialiasing)
    painter.setClipPath(expected_path)
    painter.fillPath(expected_path, QColor("white"))
    painter.end()
    actual = QImage(500, 400, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(actual, source_rect=QRectF(0, 0, 500, 400))
    # The complete rendered silhouette must be the derived straight polygon,
    # including its union with the unchanged tail operand.
    delta = np.abs(pixels(actual).astype(int) - pixels(expected).astype(int))
    assert delta.max() <= 2
    assert root.bound.to_dict() == original


def test_zero_roundness_keeps_node_outline_widths_and_cap_settings(bubble):
    canvas, root, _, _ = bubble
    root.shape_style.outline_thickness = 8
    root.shape_style.start_cap, root.shape_style.end_cap = "point", "square"
    for index, node in enumerate(root.bound.nodes):
        node.outline_multiplier = .5 + index * .5
        node.outline_enabled = index != 1
    before = copy.deepcopy(root.to_dict())
    value = appearance(canvas, root)
    widths = np.array([node.outline_multiplier for node in value.bound.nodes])
    assert widths.min() == pytest.approx(.5)
    assert widths.max() == pytest.approx(2, abs=.01)
    assert not all(node.outline_enabled for node in value.bound.nodes)
    assert len(value.bound.nodes) == len(sample_path(canvas.bound_path(root.bound), 8)[0].points)
    assert len(corners(value.loops[0].points)) == 24
    assert root.to_dict() == before


def test_small_bubble_with_width_larger_than_circumference_still_renders_a_fill(bubble):
    canvas, root, tail, modifier = bubble
    root.bound = BoundGeometry.circle(250, 200, 20)
    tail.visible = False
    modifier.width, modifier.height = 400, 10
    value = appearance(canvas, root)
    assert len(corners(value.loops[0].points)) == 4
    image = QImage(500, 400, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, source_rect=QRectF(0, 0, 500, 400))
    result = pixels(image)
    assert (result[200, 250] == 255).all()
    assert np.count_nonzero(np.all(result[:, :, :3] == 255, axis=2)) > 700
