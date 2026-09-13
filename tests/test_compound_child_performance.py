"""Preparation reuse and exact source styles during compound-child editing."""
import math
import sys

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainterPath, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.shape_contours import compile_bound
from comic_editor.ui.shape_outline import OutlineCache
from comic_editor.ui.shape_outline_compound import (
    OutlineSource, _centerline_fractions, _distance, _project,
    _source_boundary_records, _uniform_compound_outline,
)


def _qt_boundary_reference(path, records, tolerance):
    """Original Qt point arithmetic, using every record instead of buckets."""
    from comic_editor.ui.shape_outline_compound import _lines
    epsilon = max(1e-6, tolerance*2)
    for a, b in _lines(path, tolerance/4):
        direction = b-a
        square = QPointF.dotProduct(direction, direction)
        cuts, overlaps = {0., 1.}, []
        for index, (p, q, *_) in enumerate(records):
            first = QPointF.dotProduct(p-a, direction)/square
            last = QPointF.dotProduct(q-a, direction)/square
            lo, hi = max(0., min(first, last)), min(1., max(first, last))
            if hi-lo <= 1e-9:
                continue
            mid = a+direction*((lo+hi)/2)
            if _distance(mid, p+(q-p)*_project(mid, p, q)) <= epsilon:
                cuts.update((lo, hi))
                overlaps.append(index)
        cuts = sorted(cuts)
        for start, end in zip(cuts, cuts[1:]):
            if end-start < 1e-9:
                continue
            first, last = a+direction*start, a+direction*end
            mid, match, best = (first+last)/2, None, epsilon
            for index in overlaps:
                p, q = records[index][:2]
                distance = _distance(mid, p+(q-p)*_project(mid, p, q))
                if distance < best:
                    match, best = index, distance
            if match is None:
                yield first, last, None
            else:
                p, q, source, contour, edge, f0, f1 = records[match]
                yield first, last, (source, contour, edge,
                    f0+(f1-f0)*_project(first, p, q), f0+(f1-f0)*_project(last, p, q))


@pytest.mark.parametrize("large_coordinates", [False, True])
def test_batched_ribbon_fractions_match_first_nearest_source(large_coordinates):
    rng = np.random.default_rng(9)
    values = np.cumsum(rng.normal(size=(140, 2)), axis=0)
    if large_coordinates:
        values += (500., 23000.)
    points = [QPointF(*p) for p in values]
    center, total = [], 0.
    for first, last in zip(points, points[1:]):
        length = _distance(first, last)
        center.append((first, last, total, length))
        total += length
    queries = points + [QPointF(*(values[index]+(.25, -.12))) for index in range(140)]
    expected = []
    for point in queries:
        best, value = float("inf"), 0.
        for first, last, offset, length in center:
            fraction = _project(point, first, last)
            distance = _distance(point, first+(last-first)*fraction)
            if distance < best:
                best, value = distance, (offset+length*fraction)/total
        expected.append(value)
    assert np.allclose(_centerline_fractions(queries, center, total), expected, atol=1e-15, rtol=0)


def test_source_preparation_reuses_local_curves_across_translation():
    bound = BoundGeometry.circle(200, 200, 90)
    contours, cache = compile_bound(bound), OutlineCache()
    source = OutlineSource(bound, 4, QTransform())
    original = _source_boundary_records(source, contours, .125, cache)
    moved = OutlineSource(bound, 4, QTransform.fromTranslate(15, 23000))
    assert _source_boundary_records(moved, contours, .125, cache) is original
    scaled = OutlineSource(bound, 4, QTransform.fromScale(2, 2))
    assert _source_boundary_records(scaled, contours, .125, cache) is not original
    perspective = OutlineSource(bound, 4, QTransform(1, 0, .001, 0, 1, 0, 0, 0, 1))
    assert _source_boundary_records(perspective, contours, .125, cache) is not original
    assert _source_boundary_records(source, contours, .0625, cache) is not original
    bound.nodes[0].x += 2
    assert _source_boundary_records(source, compile_bound(bound), .125, cache) is not original


def test_iterative_cache_accounting_keeps_the_same_memory_estimate():
    from comic_editor.ui.shape_outline import _size
    from comic_editor.ui.shape_outline_compound import BoundarySpan

    def reference(value):
        if isinstance(value, QPainterPath):
            return 256+value.elementCount()*32
        if isinstance(value, (tuple, list)):
            return sys.getsizeof(value)+sum(reference(item) for item in value)
        if hasattr(value, "__dataclass_fields__"):
            return sys.getsizeof(value)+sum(reference(getattr(value, name)) for name in value.__dataclass_fields__)
        return sys.getsizeof(value)

    bound = BoundGeometry.circle(350, 23000, 70)
    value = (bound, compile_bound(bound), [BoundarySpan(0, 1, 2, .125, .75)],
             (None, True, "text", b"bytes", np.float64(2), 2**90))
    assert _size(value) == reference(value)


def test_uniform_outline_fast_path_respects_styles_mapping_and_partition():
    bound = BoundGeometry.rectangle(0, 0, 100, 100)
    source = OutlineSource(bound, 4, QTransform.fromTranslate(15, 20))
    assert _uniform_compound_outline(4, [source], (-1, 0))
    assert not _uniform_compound_outline(4, [source], (0,))
    assert not _uniform_compound_outline(4, [OutlineSource(bound, 4, QTransform.fromScale(2, 2))], None)
    assert not _uniform_compound_outline(4, [OutlineSource(bound, 4, QTransform.fromScale(1, 2))], None)
    assert not _uniform_compound_outline(4, [OutlineSource(bound, 4,
        QTransform(1, 0, .001, 0, 1, 0, 0, 0, 1))], None)
    bound.nodes[0].outline_multiplier = 1.25
    assert not _uniform_compound_outline(4, [source], None)
    bound.nodes[0].outline_multiplier = 1
    bound.nodes[0].outline_enabled = False
    assert not _uniform_compound_outline(4, [source], None)


@pytest.mark.parametrize("uniform", [False, True])
@pytest.mark.parametrize("reflected", [False, True])
@pytest.mark.parametrize("partition", [None, (0,)])
def test_raw_outline_matches_legacy_mesh_and_has_an_isolated_cache(monkeypatch, uniform, reflected, partition):
    from comic_editor.ui import shape_outline_compound
    from comic_editor.ui.shape_contours import bound_path
    from comic_editor.ui.shape_outline import path_key
    root = BoundGeometry.circle(120, 140, 90)
    tail = BoundGeometry.polygon([(160, 160), (240, 240), (190, 160)])
    if not uniform:
        root.nodes[0].outline_multiplier = 1.3
        tail.nodes[1].outline_enabled = False
    mapping = QTransform(-1, 0, 0, 1, 360, 0) if reflected else QTransform.fromTranslate(10, 5)
    sources = [OutlineSource(root, 5, QTransform()), OutlineSource(tail, 5, mapping)]
    path = bound_path(root).united(mapping.map(bound_path(tail)))
    cache, captured = OutlineCache(), []
    original_clip = shape_outline_compound.clip_coverage

    def capture(mesh, fill, **kwargs):
        captured.append(QPainterPath(mesh))
        return original_clip(mesh, fill, **kwargs)

    monkeypatch.setattr(shape_outline_compound, "clip_coverage", capture)
    default = shape_outline_compound.compound_outline(path, 5, sources, cache, .0625, source_indices=partition)
    assert len(captured) == 1
    raw = shape_outline_compound.compound_outline(path, 5, sources, cache, .0625, source_indices=partition, clip=False)
    assert len(captured) == 1
    assert raw.fillRule() == Qt.WindingFill
    assert path_key(raw) == path_key(captured[0])
    assert raw.contains(QPointF(120, 48)) and not default.contains(QPointF(120, 48))
    assert shape_outline_compound.compound_outline(path, 5, sources, cache, .0625, source_indices=partition) is default
    assert shape_outline_compound.compound_outline(path, 5, sources, cache, .0625, source_indices=partition, clip=False) is raw
    assert default is not raw


@pytest.mark.parametrize("scale", [1., 1.2, .7])
def test_large_coordinate_translation_and_scale_remain_affine(scale):
    source = QRectF(360., 22800., 286.9583951447493, 285.813649243533)
    corners = [source.topLeft(), source.topRight(), source.bottomRight(), source.bottomLeft()]
    quad = [(point.x()*scale+13.2, point.y()*scale-13400.813649243533) for point in corners]
    mapping = CanvasWidget._quad_transform(source, quad)
    assert mapping.isAffine()
    for point, expected in zip(corners, quad):
        assert math.dist(mapping.map(point).toTuple(), expected) < 1e-10
    # A real perspective change, even far below one displayed pixel, stays
    # projective rather than being rounded into the affine fast path.
    quad[2] = (quad[2][0]+1e-7, quad[2][1])
    perspective = CanvasWidget._quad_transform(source, quad)
    assert not perspective.isAffine()
    for point, expected in zip(corners, quad):
        assert math.dist(perspective.map(point).toTuple(), expected) < 1e-9


@pytest.mark.parametrize("reference", ["projection", "boundary", "orientation"])
def test_variable_ribbon_pixels_match_scalar_projection(qapp, monkeypatch, reference):
    from comic_editor.ui import shape_outline_compound
    chapter = ChapterDocument(height=9800)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 9800))
    page.fill_color, page.border_width = None, 0
    bubble = chapter.add_layer(page.layer_id, "Bubble", BoundGeometry.rectangle(150, 22707, 420, 213),
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=4))
    bubble.compound_enabled = True
    bubble.translate_y = -13396
    bound = BoundGeometry.polygon([(360, 22800), (438, 23086), (647, 23023)])
    bound.closed = False
    for node, width, outline in zip(bound.nodes, (10., 3.2, .1), (1., 1.3566307516629306, 1.5223502053141933)):
        node.width_multiplier, node.outline_multiplier = width, outline
    chapter.add_layer(bubble.layer_id, "Tail", bound, layer_kind="open_shape",
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_color="#FF000000",
                         outline_thickness=4, base_thickness=12))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())

    def render():
        image = QImage(700, 450, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image, source_rect=QRectF(0, 9300, 700, 450))
        return np.frombuffer(image.constBits(), np.uint8).copy()

    try:
        actual = render()

        def scalar(points, center, total):
            result = []
            for point in points:
                best, value = float("inf"), 0.
                for first, last, offset, length in center:
                    fraction = _project(point, first, last)
                    distance = _distance(point, first+(last-first)*fraction)
                    if distance < best:
                        best, value = distance, (offset+length*fraction)/total
                result.append(value)
            return np.asarray(result)

        if reference == "projection":
            monkeypatch.setattr(shape_outline_compound, "_centerline_fractions", scalar)
        elif reference == "boundary":
            monkeypatch.setattr(shape_outline_compound, "_boundary_sections", _qt_boundary_reference)
        else:
            monkeypatch.setattr(shape_outline_compound, "_preserves_path_winding", lambda _: False)
        canvas._outline_cache.clear()
        canvas._compound_path_cache.clear()
        assert np.array_equal(actual, render())
    finally:
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
