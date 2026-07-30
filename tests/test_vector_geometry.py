import math
from dataclasses import dataclass

import pytest

from comic_editor.core.vector_geometry import (
    centerline_hit,
    connect_cubic_paths,
    corridor_contains,
    cubic_arc_length,
    cubic_derivative,
    cubic_eval,
    cubic_subsegment,
    erase_cubics_by_corridor,
    erase_stroke_by_corridor,
    find_face_containing,
    fit_freehand,
    flatten_cubic,
    flatten_stroke,
    nearest_on_cubic,
    nearest_on_stroke,
    path_intersections,
    path_self_intersections,
    resample_freehand,
    simplify_cubics_local,
    simplify_polyline_local,
    simplify_tolerance,
    split_cubic,
    stroke_cubics,
    tangent_bridge,
    trace_planar_faces,
)


def line(start, end):
    return start, start, end, end


def assert_point(actual, expected, tolerance=1.0e-6):
    assert actual[0] == pytest.approx(expected[0], abs=tolerance)
    assert actual[1] == pytest.approx(expected[1], abs=tolerance)


def test_split_and_subsegment_preserve_original_cubic():
    cubic = ((0, 0), (40, 100), (60, -40), (100, 20))
    first, second = split_cubic(cubic, 0.35)
    assert_point(first[-1], cubic_eval(cubic, 0.35))
    assert_point(second[0], cubic_eval(cubic, 0.35))

    sub = cubic_subsegment(cubic, 0.2, 0.7)
    for local in (0, 0.15, 0.5, 1):
        assert_point(
            cubic_eval(sub, local),
            cubic_eval(cubic, 0.2 + 0.5 * local),
        )


def test_flatten_retains_monotonic_source_parameters_and_tolerance():
    cubic = ((0, 0), (0, 100), (100, 100), (100, 0))
    flattened = flatten_cubic(cubic, tolerance=0.2, segment_index=4)
    assert flattened[0].t == 0
    assert flattened[-1].t == 1
    assert all(
        first.t < second.t for first, second in zip(flattened, flattened[1:])
    )
    assert {point.segment_index for point in flattened} == {4}
    for first, second in zip(flattened, flattened[1:]):
        middle_t = (first.t + second.t) / 2
        midpoint = (
            (first.point[0] + second.point[0]) / 2,
            (first.point[1] + second.point[1]) / 2,
        )
        assert math.dist(cubic_eval(cubic, middle_t), midpoint) <= 0.21


def test_flatten_detects_collinear_reversal_instead_of_collapsing_it():
    reversal = ((0, 0), (100, 0), (-100, 0), (0, 0))
    flattened = flatten_cubic(reversal, tolerance=0.1)
    assert len(flattened) > 4
    assert cubic_arc_length(reversal, tolerance=0.01) > 100


def test_arc_length_and_nearest_projection_are_stable():
    cubic = line((0, 0), (100, 0))
    assert cubic_arc_length(cubic) == pytest.approx(100)
    projection = nearest_on_cubic(cubic, (35, 12))
    assert projection.t == pytest.approx(0.399, abs=0.01)
    # The degenerate line controls produce a non-linear parameterization, but
    # the projected document position and distance are still exact.
    assert_point(projection.point, (35, 0), tolerance=0.02)
    assert projection.distance == pytest.approx(12, abs=0.02)


def test_freehand_resampling_and_fitting_preserve_pressure_at_anchors():
    samples = [(0, 0, 0.2), (25, 20, 0.5), (50, 0, 0.9)]
    resampled = resample_freehand(samples, spacing=5)
    assert resampled[0].pressure == pytest.approx(0.2)
    assert resampled[-1].pressure == pytest.approx(0.9)

    fitted = fit_freehand(samples, error=0.4, resample_spacing=2)
    assert_point(fitted[0].point, (0, 0))
    assert_point(fitted[-1].point, (50, 0))
    assert fitted[0].outgoing is not None
    assert fitted[-1].incoming is not None
    assert fitted[0].pressure == pytest.approx(0.2)
    assert fitted[-1].pressure == pytest.approx(0.9)

    dot = fit_freehand([(7, 9, 0.4)])
    assert len(dot) == 1
    assert dot[0].incoming is dot[0].outgoing is None


@dataclass
class StrokePoint:
    x: float
    y: float
    incoming: tuple[float, float] | None = None
    outgoing: tuple[float, float] | None = None
    width: float = 10
    opacity: float = 1


def test_model_friendly_stroke_adapter_and_variable_width_hit():
    points = [
        StrokePoint(0, 0, outgoing=(30, 0), width=4),
        StrokePoint(100, 0, incoming=(70, 0), width=20),
    ]
    cubics = stroke_cubics(points)
    assert cubics == [((0, 0), (30, 0), (70, 0), (100, 0))]
    assert centerline_hit(points, (90, 8)) is not None
    assert centerline_hit(points, (5, 8)) is None
    location = nearest_on_stroke(points, (50, 7))
    assert location is not None
    assert location.width == pytest.approx(12, abs=0.1)
    samples = flatten_stroke(points)
    assert samples[0].width == 4
    assert samples[-1].width == 20


def test_cubic_path_and_self_intersections_keep_both_parameters():
    horizontal = [line((-10, 0), (10, 0))]
    vertical = [line((0, -10), (0, 10))]
    intersections = path_intersections(horizontal, vertical, tolerance=0.05)
    assert len(intersections) == 1
    assert_point(intersections[0].point, (0, 0), tolerance=0.02)

    arch = [((0, 0), (0, 100), (100, 100), (100, 0))]
    upright = [line((50, -100), (50, 100))]
    refined = path_intersections(arch, upright, tolerance=5)
    assert len(refined) == 1
    assert_point(refined[0].point, (50, 75), tolerance=1.0e-5)
    assert refined[0].first_t == pytest.approx(0.5, abs=1.0e-6)

    bow = [
        line((0, 0), (10, 10)),
        line((10, 10), (0, 10)),
        line((0, 10), (10, 0)),
    ]
    self_hits = path_self_intersections(bow, tolerance=0.05)
    assert any(math.dist(hit.point, (5, 5)) < 0.05 for hit in self_hits)


def test_corridor_subtraction_splits_and_preserves_exact_outer_spans():
    cubic = line((0, 0), (100, 0))
    groups = erase_cubics_by_corridor(
        [cubic], [(50, 0)], 10, shape="round"
    )
    assert len(groups) == 2
    left, right = groups[0][0], groups[1][0]
    assert_point(left.cubic[0], (0, 0))
    assert left.cubic[-1][0] == pytest.approx(40, abs=0.1)
    assert right.cubic[0][0] == pytest.approx(60, abs=0.1)
    assert_point(right.cubic[-1], (100, 0))
    assert_point(left.cubic[-1], cubic_eval(cubic, left.t1), tolerance=1.0e-6)
    assert_point(right.cubic[0], cubic_eval(cubic, right.t0), tolerance=1.0e-6)


def test_corridor_subtraction_tracks_continuously_variable_width():
    points = [
        StrokePoint(0, 0, width=2),
        StrokePoint(100, 0, width=30),
    ]
    groups = erase_stroke_by_corridor(
        points, [(50, 8)], 2, shape="round"
    )
    assert len(groups) == 2
    left_end = groups[0][-1].cubic[-1][0]
    right_start = groups[1][0].cubic[0][0]
    # A constant 30px expansion would begin erasing near x=35.  The actual
    # thin-to-thick interpolation keeps substantially more of the thin side.
    assert left_end == pytest.approx(44.84, abs=0.2)
    assert right_start == pytest.approx(57.04, abs=0.2)


def test_square_and_round_corridor_metrics_are_explicit():
    assert corridor_contains((9, 9), [(0, 0)], 10, shape="square")
    assert not corridor_contains((9, 9), [(0, 0)], 10, shape="round")
    assert corridor_contains((5, 3), [(0, 0), (10, 0)], 3)


def test_simplification_mapping_and_local_preservation():
    assert simplify_tolerance(0) == pytest.approx(0.25)
    assert simplify_tolerance(100) == pytest.approx(25)
    points = [(index, 0.5 if index % 2 else -0.5) for index in range(21)]
    simplified = simplify_polyline_local(
        points, [(8, 0), (12, 0)], radius=3, amount=100
    )
    assert points[0] in simplified
    assert points[-1] in simplified
    assert len(simplified) < len(points)

    untouched = line((0, 0), (20, 0))
    touched = ((20, 0), (25, 10), (35, -10), (40, 0))
    result = simplify_cubics_local(
        [untouched, touched], [(30, 0)], radius=5, amount=50
    )
    assert result[0] == untouched

    curve = ((0, 0), (30, 80), (70, -80), (100, 0))
    partial = simplify_cubics_local(
        [curve], [(50, 0)], radius=8, amount=100
    )
    assert len(partial) >= 3
    prefix = partial[0]
    source_end = nearest_on_cubic(curve, prefix[-1]).t
    exact_prefix = cubic_subsegment(curve, 0, source_end)
    for actual, expected in zip(prefix, exact_prefix):
        assert_point(actual, expected, tolerance=1.0e-5)


def test_tangent_bridge_and_path_connection_orient_endpoints():
    bridge = tangent_bridge((0, 0), (30, 10), (1, 0), (1, 0))
    assert_point(bridge[0], (0, 0))
    assert_point(bridge[-1], (30, 10))
    assert cubic_derivative(bridge, 0)[0] > 0
    assert cubic_derivative(bridge, 1)[0] > 0

    first = [line((0, 0), (10, 0))]
    second = [line((30, 0), (20, 0))]
    connected = connect_cubic_paths(first, "end", second, "end")
    assert connected.second_reversed
    assert_point(connected.bridge[0], (10, 0))
    assert_point(connected.bridge[-1], (20, 0))


def test_planar_face_trace_splits_intersections_and_finds_seed_face():
    square = [(0, 0), (20, 0), (20, 20), (0, 20)]
    divider = [(10, 0), (10, 20)]
    faces = trace_planar_faces(
        [square, divider], closed=[True, False]
    )
    assert len(faces) == 2
    assert sorted(face.area for face in faces) == pytest.approx([200, 200])
    left = find_face_containing(faces, (5, 10))
    right = find_face_containing(faces, (15, 10))
    assert left is not None and right is not None and left != right


def test_planar_gap_closing_marks_virtual_edge():
    almost_square = [(0, 0), (20, 0), (20, 20), (0, 20), (0, 3)]
    assert not trace_planar_faces([almost_square], closed=[False])
    faces = trace_planar_faces(
        [almost_square], closed=[False], gap_threshold=4
    )
    assert len(faces) == 1
    assert faces[0].area == pytest.approx(400)
    assert sum(edge.provenance.virtual for edge in faces[0].edges) == 1
