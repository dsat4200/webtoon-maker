"""Dependency-free geometry helpers for editable vector drawings.

The document model deliberately stores ordinary tuples rather than Qt geometry
objects.  This module follows that convention, but :func:`point_xy` also accepts
``QPointF``-like objects and dataclasses with numeric ``x``/``y`` attributes.

Most destructive operations return exact cubic sub-spans.  A caller can thus
trim or split a stroke without flattening the untouched portions of its curves.
Flattened points retain source parameters for hit testing, intersections and
face reconstruction.
"""
from __future__ import annotations

import bisect
import math
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Sequence


Point = tuple[float, float]
Cubic = tuple[Point, Point, Point, Point]
EPSILON = 1.0e-9


def point_xy(value: Any) -> Point:
    """Coerce a tuple, QPointF-like value, or dataclass into a point tuple."""
    if isinstance(value, (tuple, list)):
        return float(value[0]), float(value[1])
    x = getattr(value, "x")
    y = getattr(value, "y")
    return (
        float(x() if callable(x) else x),
        float(y() if callable(y) else y),
    )


def _add(first: Point, second: Point) -> Point:
    return first[0] + second[0], first[1] + second[1]


def _sub(first: Point, second: Point) -> Point:
    return first[0] - second[0], first[1] - second[1]


def _mul(point: Point, scalar: float) -> Point:
    return point[0] * scalar, point[1] * scalar


def _dot(first: Point, second: Point) -> float:
    return first[0] * second[0] + first[1] * second[1]


def _cross(first: Point, second: Point) -> float:
    return first[0] * second[1] - first[1] * second[0]


def _length(point: Point) -> float:
    return math.hypot(*point)


def distance(first: Any, second: Any) -> float:
    return _length(_sub(point_xy(first), point_xy(second)))


def _normalized(point: Point, fallback: Point = (1.0, 0.0)) -> Point:
    magnitude = _length(point)
    if magnitude <= EPSILON:
        return fallback
    return point[0] / magnitude, point[1] / magnitude


def lerp(first: Any, second: Any, amount: float) -> Point:
    a, b = point_xy(first), point_xy(second)
    return (
        a[0] + (b[0] - a[0]) * amount,
        a[1] + (b[1] - a[1]) * amount,
    )


@dataclass(frozen=True)
class CubicSegment:
    """One path segment with optional source-anchor indices."""

    cubic: Cubic
    start_index: int = 0
    end_index: int = 1

    @property
    def p0(self) -> Point:
        return self.cubic[0]

    @property
    def c1(self) -> Point:
        return self.cubic[1]

    @property
    def c2(self) -> Point:
        return self.cubic[2]

    @property
    def p3(self) -> Point:
        return self.cubic[3]


@dataclass(frozen=True)
class FlattenedPoint:
    point: Point
    t: float
    segment_index: int = 0


@dataclass(frozen=True)
class CubicProjection:
    point: Point
    t: float
    distance: float


@dataclass(frozen=True)
class PathProjection:
    point: Point
    segment_index: int
    t: float
    distance: float

    @property
    def scalar_parameter(self) -> float:
        return self.segment_index + self.t


@dataclass(frozen=True)
class StrokeLocation:
    point: Point
    segment_index: int
    t: float
    distance: float
    width: float
    opacity: float

    @property
    def scalar_parameter(self) -> float:
        return self.segment_index + self.t


@dataclass(frozen=True)
class StrokeSample:
    point: Point
    segment_index: int
    t: float
    width: float
    opacity: float


@dataclass(frozen=True)
class PathIntersection:
    point: Point
    first_segment: int
    first_t: float
    second_segment: int
    second_t: float


@dataclass(frozen=True)
class CubicSpan:
    """An exact sub-span of a source cubic."""

    cubic: Cubic
    source_segment: int
    t0: float
    t1: float


@dataclass(frozen=True)
class FreehandSample:
    x: float
    y: float
    pressure: float = 1.0

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class FittedPoint:
    x: float
    y: float
    pressure: float = 1.0
    incoming: Point | None = None
    outgoing: Point | None = None

    @property
    def point(self) -> Point:
        return self.x, self.y


@dataclass(frozen=True)
class FittedCubic:
    cubic: Cubic
    start_sample: int
    end_sample: int


@dataclass(frozen=True)
class ConnectionResult:
    cubics: tuple[Cubic, ...]
    bridge: Cubic
    first_reversed: bool
    second_reversed: bool


@dataclass(frozen=True)
class EdgeProvenance:
    path_index: int
    segment_index: int
    t0: float
    t1: float
    virtual: bool = False


@dataclass(frozen=True)
class FaceEdge:
    start: Point
    end: Point
    provenance: EdgeProvenance


@dataclass(frozen=True)
class PlanarFace:
    vertices: tuple[Point, ...]
    edges: tuple[FaceEdge, ...]
    area: float

    @property
    def bounds(self) -> tuple[float, float, float, float]:
        left = min(point[0] for point in self.vertices)
        top = min(point[1] for point in self.vertices)
        right = max(point[0] for point in self.vertices)
        bottom = max(point[1] for point in self.vertices)
        return left, top, right - left, bottom - top

    def contains(self, point: Any, *, include_boundary: bool = True) -> bool:
        return point_in_polygon(
            point, self.vertices, include_boundary=include_boundary
        )


def cubic_eval(cubic: Cubic, t: float) -> Point:
    """Evaluate a cubic Bézier using de Casteljau interpolation."""
    t = max(0.0, min(1.0, float(t)))
    p01 = lerp(cubic[0], cubic[1], t)
    p12 = lerp(cubic[1], cubic[2], t)
    p23 = lerp(cubic[2], cubic[3], t)
    p012 = lerp(p01, p12, t)
    p123 = lerp(p12, p23, t)
    return lerp(p012, p123, t)


def cubic_derivative(cubic: Cubic, t: float) -> Point:
    t = max(0.0, min(1.0, float(t)))
    inverse = 1.0 - t
    first = _mul(_sub(cubic[1], cubic[0]), 3 * inverse * inverse)
    second = _mul(_sub(cubic[2], cubic[1]), 6 * inverse * t)
    third = _mul(_sub(cubic[3], cubic[2]), 3 * t * t)
    return _add(_add(first, second), third)


def cubic_second_derivative(cubic: Cubic, t: float) -> Point:
    t = max(0.0, min(1.0, float(t)))
    first = _add(
        _sub(cubic[2], _mul(cubic[1], 2.0)),
        cubic[0],
    )
    second = _add(
        _sub(cubic[3], _mul(cubic[2], 2.0)),
        cubic[1],
    )
    return _mul(_add(_mul(first, 1.0 - t), _mul(second, t)), 6.0)


def split_cubic(cubic: Cubic, t: float) -> tuple[Cubic, Cubic]:
    """Split a cubic exactly at ``t`` with de Casteljau."""
    t = max(0.0, min(1.0, float(t)))
    p01 = lerp(cubic[0], cubic[1], t)
    p12 = lerp(cubic[1], cubic[2], t)
    p23 = lerp(cubic[2], cubic[3], t)
    p012 = lerp(p01, p12, t)
    p123 = lerp(p12, p23, t)
    point = lerp(p012, p123, t)
    return (
        (cubic[0], p01, p012, point),
        (point, p123, p23, cubic[3]),
    )


def cubic_subsegment(cubic: Cubic, t0: float, t1: float) -> Cubic:
    """Return the exact source interval ``[t0, t1]`` as a new cubic."""
    t0 = max(0.0, min(1.0, float(t0)))
    t1 = max(0.0, min(1.0, float(t1)))
    if t1 < t0:
        return reverse_cubic(cubic_subsegment(cubic, t1, t0))
    if t0 <= EPSILON and t1 >= 1.0 - EPSILON:
        return cubic
    if t1 - t0 <= EPSILON:
        point = cubic_eval(cubic, (t0 + t1) / 2)
        return point, point, point, point
    left = split_cubic(cubic, t1)[0]
    relative = t0 / t1 if t1 > EPSILON else 0.0
    return split_cubic(left, relative)[1]


def reverse_cubic(cubic: Cubic) -> Cubic:
    return cubic[3], cubic[2], cubic[1], cubic[0]


def _point_line_distance(point: Point, start: Point, end: Point) -> float:
    direction = _sub(end, start)
    denominator = _dot(direction, direction)
    if denominator <= EPSILON:
        return distance(point, start)
    amount = max(0.0, min(1.0, _dot(_sub(point, start), direction) / denominator))
    return distance(point, _add(start, _mul(direction, amount)))


def cubic_flatness(cubic: Cubic) -> float:
    perpendicular = max(
        _point_line_distance(cubic[1], cubic[0], cubic[3]),
        _point_line_distance(cubic[2], cubic[0], cubic[3]),
    )
    control_length = sum(
        distance(first, second)
        for first, second in zip(cubic, cubic[1:])
    )
    chord_length = distance(cubic[0], cubic[3])
    # Perpendicular distance alone misses collinear reversals and overshoot.
    return max(perpendicular, (control_length - chord_length) / 2)


def flatten_cubic(
    cubic: Cubic,
    tolerance: float = 0.5,
    *,
    segment_index: int = 0,
    max_depth: int = 18,
) -> list[FlattenedPoint]:
    """Adaptively flatten a cubic while retaining exact source ``t`` values."""
    tolerance = max(EPSILON, float(tolerance))
    result = [FlattenedPoint(cubic[0], 0.0, segment_index)]

    def visit(curve: Cubic, t0: float, t1: float, depth: int) -> None:
        if depth >= max_depth or cubic_flatness(curve) <= tolerance:
            result.append(FlattenedPoint(curve[3], t1, segment_index))
            return
        first, second = split_cubic(curve, 0.5)
        middle = (t0 + t1) / 2
        visit(first, t0, middle, depth + 1)
        visit(second, middle, t1, depth + 1)

    visit(cubic, 0.0, 1.0, 0)
    return result


def flatten_path(
    cubics: Sequence[Cubic],
    tolerance: float = 0.5,
) -> list[FlattenedPoint]:
    result: list[FlattenedPoint] = []
    for index, cubic in enumerate(cubics):
        points = flatten_cubic(
            cubic, tolerance, segment_index=index
        )
        if result and points:
            points = points[1:]
        result.extend(points)
    return result


def cubic_arc_length(
    cubic: Cubic,
    t0: float = 0.0,
    t1: float = 1.0,
    tolerance: float = 0.05,
) -> float:
    subsegment = cubic_subsegment(cubic, t0, t1)
    points = flatten_cubic(subsegment, max(EPSILON, tolerance))
    return sum(
        distance(first.point, second.point)
        for first, second in zip(points, points[1:])
    )


def path_arc_lengths(
    cubics: Sequence[Cubic],
    tolerance: float = 0.05,
) -> tuple[float, ...]:
    return tuple(cubic_arc_length(cubic, tolerance=tolerance) for cubic in cubics)


def t_at_arc_length(
    cubic: Cubic,
    target: float,
    tolerance: float = 0.02,
) -> float:
    total = cubic_arc_length(cubic, tolerance=tolerance)
    if total <= EPSILON:
        return 0.0
    target = max(0.0, min(total, float(target)))
    lower, upper = 0.0, 1.0
    for _ in range(32):
        middle = (lower + upper) / 2
        length = cubic_arc_length(
            cubic, 0.0, middle, tolerance=tolerance
        )
        if length < target:
            lower = middle
        else:
            upper = middle
    return (lower + upper) / 2


def _nearest_on_segment(
    point: Point, start: Point, end: Point
) -> tuple[Point, float, float]:
    direction = _sub(end, start)
    denominator = _dot(direction, direction)
    if denominator <= EPSILON:
        return start, 0.0, distance(point, start)
    amount = max(
        0.0, min(1.0, _dot(_sub(point, start), direction) / denominator)
    )
    projected = _add(start, _mul(direction, amount))
    return projected, amount, distance(point, projected)


def nearest_on_cubic(
    cubic: Cubic,
    point: Any,
    tolerance: float = 0.25,
) -> CubicProjection:
    """Find a stable nearest point using adaptive seeds and Newton refinement."""
    target = point_xy(point)
    flattened = flatten_cubic(cubic, tolerance)
    candidates: list[float] = [0.0, 1.0]
    for first, second in zip(flattened, flattened[1:]):
        _, amount, _ = _nearest_on_segment(target, first.point, second.point)
        candidates.append(first.t + (second.t - first.t) * amount)
    best_t, best_distance = 0.0, math.inf
    for candidate in candidates:
        t = candidate
        for _ in range(10):
            value = cubic_eval(cubic, t)
            derivative = cubic_derivative(cubic, t)
            second = cubic_second_derivative(cubic, t)
            difference = _sub(value, target)
            numerator = _dot(difference, derivative)
            denominator = _dot(derivative, derivative) + _dot(difference, second)
            if abs(denominator) <= EPSILON:
                break
            updated = max(0.0, min(1.0, t - numerator / denominator))
            if abs(updated - t) <= 1.0e-10:
                t = updated
                break
            t = updated
        candidate_distance = distance(cubic_eval(cubic, t), target)
        if candidate_distance < best_distance:
            best_t, best_distance = t, candidate_distance
    return CubicProjection(cubic_eval(cubic, best_t), best_t, best_distance)


def nearest_on_path(
    cubics: Sequence[Cubic],
    point: Any,
    tolerance: float = 0.25,
) -> PathProjection | None:
    best: PathProjection | None = None
    for index, cubic in enumerate(cubics):
        projection = nearest_on_cubic(cubic, point, tolerance)
        candidate = PathProjection(
            projection.point, index, projection.t, projection.distance
        )
        if best is None or candidate.distance < best.distance:
            best = candidate
    return best


def stroke_cubics(points: Sequence[Any], closed: bool = False) -> list[Cubic]:
    """Build cubic segments from model-like vector control points.

    ``outgoing`` belongs to the first anchor and ``incoming`` to the second.
    Missing controls fall back to their anchor, producing an exact line.
    """
    if len(points) < 2:
        return []
    result: list[Cubic] = []
    count = len(points) if closed else len(points) - 1
    for index in range(count):
        first = points[index]
        second = points[(index + 1) % len(points)]
        start, end = point_xy(first), point_xy(second)
        outgoing = getattr(first, "outgoing", None)
        incoming = getattr(second, "incoming", None)
        result.append((
            start,
            point_xy(outgoing) if outgoing is not None else start,
            point_xy(incoming) if incoming is not None else end,
            end,
        ))
    return result


def interpolate_stroke_attribute(
    points: Sequence[Any],
    segment_index: int,
    t: float,
    attribute: str,
    default: float,
    *,
    closed: bool = False,
) -> float:
    if not points:
        return default
    first = float(getattr(points[segment_index], attribute, default))
    next_index = (segment_index + 1) % len(points)
    if not closed and next_index >= len(points):
        return first
    second = float(getattr(points[next_index], attribute, default))
    return first + (second - first) * max(0.0, min(1.0, t))


def centerline_hit(
    points: Sequence[Any],
    query: Any,
    *,
    closed: bool = False,
    extra_tolerance: float = 0.0,
) -> PathProjection | None:
    """Hit test a variable-width stroke against its centerline."""
    cubics = stroke_cubics(points, closed)
    if not cubics and len(points) == 1:
        radius = float(getattr(points[0], "width", 1.0)) / 2
        hit_distance = distance(point_xy(points[0]), query)
        if hit_distance <= radius + extra_tolerance:
            return PathProjection(point_xy(points[0]), 0, 0.0, hit_distance)
        return None
    projection = nearest_on_path(cubics, query)
    if projection is None:
        return None
    width = interpolate_stroke_attribute(
        points, projection.segment_index, projection.t, "width", 1.0,
        closed=closed,
    )
    return (
        projection
        if projection.distance <= width / 2 + extra_tolerance
        else None
    )


def nearest_on_stroke(
    points: Sequence[Any],
    query: Any,
    *,
    closed: bool = False,
    tolerance: float = 0.25,
) -> StrokeLocation | None:
    """Project onto a model-like stroke and interpolate width and opacity."""
    cubics = stroke_cubics(points, closed)
    if not cubics:
        if not points:
            return None
        point = point_xy(points[0])
        return StrokeLocation(
            point,
            0,
            0.0,
            distance(point, query),
            float(getattr(points[0], "width", 1.0)),
            float(getattr(points[0], "opacity", 1.0)),
        )
    projection = nearest_on_path(cubics, query, tolerance)
    if projection is None:
        return None
    return StrokeLocation(
        projection.point,
        projection.segment_index,
        projection.t,
        projection.distance,
        interpolate_stroke_attribute(
            points,
            projection.segment_index,
            projection.t,
            "width",
            1.0,
            closed=closed,
        ),
        interpolate_stroke_attribute(
            points,
            projection.segment_index,
            projection.t,
            "opacity",
            1.0,
            closed=closed,
        ),
    )


def flatten_stroke(
    points: Sequence[Any],
    *,
    closed: bool = False,
    tolerance: float = 0.5,
) -> list[StrokeSample]:
    """Flatten a stroke with source parameters and interpolated attributes."""
    cubics = stroke_cubics(points, closed)
    if not cubics:
        if not points:
            return []
        return [StrokeSample(
            point_xy(points[0]),
            0,
            0.0,
            float(getattr(points[0], "width", 1.0)),
            float(getattr(points[0], "opacity", 1.0)),
        )]
    result: list[StrokeSample] = []
    for sample in flatten_path(cubics, tolerance):
        result.append(StrokeSample(
            sample.point,
            sample.segment_index,
            sample.t,
            interpolate_stroke_attribute(
                points,
                sample.segment_index,
                sample.t,
                "width",
                1.0,
                closed=closed,
            ),
            interpolate_stroke_attribute(
                points,
                sample.segment_index,
                sample.t,
                "opacity",
                1.0,
                closed=closed,
            ),
        ))
    return result


def _coerce_sample(value: Any) -> FreehandSample:
    if isinstance(value, FreehandSample):
        return value
    if isinstance(value, (tuple, list)):
        return FreehandSample(
            float(value[0]), float(value[1]),
            float(value[2]) if len(value) > 2 else 1.0,
        )
    point = point_xy(value)
    return FreehandSample(
        point[0], point[1], float(getattr(value, "pressure", 1.0))
    )


def deduplicate_samples(
    samples: Iterable[Any],
    minimum_distance: float = 1.0e-4,
) -> list[FreehandSample]:
    result: list[FreehandSample] = []
    for value in samples:
        sample = _coerce_sample(value)
        if result and distance(result[-1].point, sample.point) <= minimum_distance:
            result[-1] = sample
        else:
            result.append(sample)
    return result


def resample_freehand(
    samples: Iterable[Any],
    spacing: float = 1.0,
) -> list[FreehandSample]:
    """Resample positions and pressure at regular document-space intervals."""
    source = deduplicate_samples(samples)
    spacing = max(EPSILON, float(spacing))
    if len(source) < 2:
        return source
    cumulative = [0.0]
    for first, second in zip(source, source[1:]):
        cumulative.append(cumulative[-1] + distance(first.point, second.point))
    total = cumulative[-1]
    if total <= spacing:
        return [source[0], source[-1]]
    targets = [
        min(total, index * spacing)
        for index in range(int(math.floor(total / spacing)) + 1)
    ]
    if targets[-1] < total - EPSILON:
        targets.append(total)
    result: list[FreehandSample] = []
    segment = 0
    for target in targets:
        while segment + 1 < len(cumulative) and cumulative[segment + 1] < target:
            segment += 1
        start_length, end_length = cumulative[segment], cumulative[segment + 1]
        amount = (
            0.0 if end_length - start_length <= EPSILON
            else (target - start_length) / (end_length - start_length)
        )
        point = lerp(source[segment].point, source[segment + 1].point, amount)
        pressure = (
            source[segment].pressure
            + (source[segment + 1].pressure - source[segment].pressure) * amount
        )
        result.append(FreehandSample(*point, pressure))
    return result


def _chord_parameters(points: Sequence[Point], first: int, last: int) -> list[float]:
    parameters = [0.0]
    for index in range(first + 1, last + 1):
        parameters.append(
            parameters[-1] + distance(points[index - 1], points[index])
        )
    if parameters[-1] <= EPSILON:
        return [
            index / max(1, last - first)
            for index in range(last - first + 1)
        ]
    return [value / parameters[-1] for value in parameters]


def _generate_bezier(
    points: Sequence[Point],
    first: int,
    last: int,
    parameters: Sequence[float],
    left_tangent: Point,
    right_tangent: Point,
) -> Cubic:
    start, end = points[first], points[last]
    c00 = c01 = c11 = x0 = x1 = 0.0
    for offset, parameter in enumerate(parameters):
        inverse = 1.0 - parameter
        b0 = inverse ** 3
        b1 = 3 * parameter * inverse * inverse
        b2 = 3 * parameter * parameter * inverse
        b3 = parameter ** 3
        a1 = _mul(left_tangent, b1)
        a2 = _mul(right_tangent, b2)
        base = _add(
            _mul(start, b0 + b1),
            _mul(end, b2 + b3),
        )
        difference = _sub(points[first + offset], base)
        c00 += _dot(a1, a1)
        c01 += _dot(a1, a2)
        c11 += _dot(a2, a2)
        x0 += _dot(a1, difference)
        x1 += _dot(a2, difference)
    determinant = c00 * c11 - c01 * c01
    alpha_left = alpha_right = 0.0
    if abs(determinant) > EPSILON:
        alpha_left = (x0 * c11 - x1 * c01) / determinant
        alpha_right = (c00 * x1 - c01 * x0) / determinant
    chord = distance(start, end)
    epsilon = 1.0e-6 * chord
    if alpha_left < epsilon or alpha_right < epsilon:
        alpha_left = alpha_right = chord / 3
    return (
        start,
        _add(start, _mul(left_tangent, alpha_left)),
        _add(end, _mul(right_tangent, alpha_right)),
        end,
    )


def _newton_parameter(cubic: Cubic, point: Point, parameter: float) -> float:
    value = cubic_eval(cubic, parameter)
    derivative = cubic_derivative(cubic, parameter)
    second = cubic_second_derivative(cubic, parameter)
    difference = _sub(value, point)
    numerator = _dot(difference, derivative)
    denominator = _dot(derivative, derivative) + _dot(difference, second)
    if abs(denominator) <= EPSILON:
        return parameter
    return max(0.0, min(1.0, parameter - numerator / denominator))


def _fit_error(
    points: Sequence[Point],
    first: int,
    last: int,
    cubic: Cubic,
    parameters: Sequence[float],
) -> tuple[float, int]:
    maximum, split = 0.0, (first + last) // 2
    for index in range(first + 1, last):
        difference = _sub(
            cubic_eval(cubic, parameters[index - first]), points[index]
        )
        error = _dot(difference, difference)
        if error >= maximum:
            maximum, split = error, index
    return maximum, split


def fit_cubic_path(
    values: Sequence[Any],
    error: float = 2.0,
) -> list[FittedCubic]:
    """Fit recursive cubic spans to a freehand polyline.

    ``error`` is a maximum document-space distance, not its square.
    """
    points = [point_xy(value) for value in values]
    if len(points) < 2:
        return []
    error_squared = max(EPSILON, float(error)) ** 2
    result: list[FittedCubic] = []

    def fit(
        first: int,
        last: int,
        left_tangent: Point,
        right_tangent: Point,
    ) -> None:
        count = last - first + 1
        if count == 2:
            length = distance(points[first], points[last]) / 3
            result.append(FittedCubic((
                points[first],
                _add(points[first], _mul(left_tangent, length)),
                _add(points[last], _mul(right_tangent, length)),
                points[last],
            ), first, last))
            return
        parameters = _chord_parameters(points, first, last)
        cubic = _generate_bezier(
            points, first, last, parameters, left_tangent, right_tangent
        )
        maximum, split = _fit_error(points, first, last, cubic, parameters)
        if maximum <= error_squared:
            result.append(FittedCubic(cubic, first, last))
            return
        if maximum <= error_squared * 4:
            for _ in range(4):
                parameters = [
                    _newton_parameter(cubic, points[first + offset], parameter)
                    for offset, parameter in enumerate(parameters)
                ]
                if any(
                    parameters[index] >= parameters[index + 1]
                    for index in range(len(parameters) - 1)
                ):
                    break
                cubic = _generate_bezier(
                    points, first, last, parameters,
                    left_tangent, right_tangent,
                )
                maximum, split = _fit_error(
                    points, first, last, cubic, parameters
                )
                if maximum <= error_squared:
                    result.append(FittedCubic(cubic, first, last))
                    return
        center = _normalized(
            _sub(points[split - 1], points[split + 1]),
            _normalized(_sub(points[split], points[split - 1])),
        )
        fit(first, split, left_tangent, center)
        fit(split, last, _mul(center, -1.0), right_tangent)

    fit(
        0,
        len(points) - 1,
        _normalized(_sub(points[1], points[0])),
        _normalized(_sub(points[-2], points[-1])),
    )
    return result


def fit_freehand(
    samples: Sequence[Any],
    error: float = 2.0,
    *,
    resample_spacing: float | None = 1.0,
    attribute_error: float | None = 0.025,
) -> list[FittedPoint]:
    """Return editable cubic anchors while retaining pressure changes.

    Geometry and pressure are fitted independently. Additional pressure
    anchors split an already-fitted cubic exactly, so expressive pressure
    peaks survive without changing the fitted centerline.
    """
    source = (
        deduplicate_samples(samples)
        if resample_spacing is None
        else resample_freehand(samples, resample_spacing)
    )
    if not source:
        return []
    if len(source) == 1:
        sample = source[0]
        return [FittedPoint(sample.x, sample.y, sample.pressure)]
    fitted = fit_cubic_path([sample.point for sample in source], error)
    cumulative = [0.0]
    for first, second in zip(source, source[1:]):
        cumulative.append(
            cumulative[-1] + distance(first.point, second.point)
        )

    def attribute_knots(first: int, last: int) -> list[int]:
        tolerance = (
            None if attribute_error is None
            else max(0.0, float(attribute_error))
        )
        if tolerance is None or last - first < 2:
            return []
        selected: set[int] = set()
        pending = [(first, last)]
        while pending:
            left, right = pending.pop()
            span = cumulative[right] - cumulative[left]
            maximum = tolerance
            split_at = -1
            for index in range(left + 1, right):
                amount = (
                    (index - left) / (right - left)
                    if span <= EPSILON
                    else (cumulative[index] - cumulative[left]) / span
                )
                expected = (
                    source[left].pressure
                    + (source[right].pressure - source[left].pressure) * amount
                )
                deviation = abs(source[index].pressure - expected)
                if deviation > maximum:
                    maximum = deviation
                    split_at = index
            if split_at >= 0:
                selected.add(split_at)
                pending.append((left, split_at))
                pending.append((split_at, right))
        return sorted(selected)

    pieces: list[tuple[Cubic, float]] = []
    for segment in fitted:
        indices = attribute_knots(
            segment.start_sample, segment.end_sample
        )
        span = (
            cumulative[segment.end_sample]
            - cumulative[segment.start_sample]
        )
        parameters: list[float] = []
        for index in indices:
            parameters.append((
                (index - segment.start_sample)
                / max(1, segment.end_sample - segment.start_sample)
                if span <= EPSILON
                else (
                    cumulative[index] - cumulative[segment.start_sample]
                ) / span
            ))
        starts = [0.0, *parameters]
        ends = [*parameters, 1.0]
        pressures = [
            *[source[index].pressure for index in indices],
            source[segment.end_sample].pressure,
        ]
        pieces.extend(
            (cubic_subsegment(segment.cubic, start, end), pressure)
            for start, end, pressure in zip(starts, ends, pressures)
        )

    anchors: list[FittedPoint] = [FittedPoint(
        pieces[0][0][0][0], pieces[0][0][0][1], source[0].pressure,
        outgoing=pieces[0][0][1],
    )]
    for index, (cubic, pressure) in enumerate(pieces):
        outgoing = (
            pieces[index + 1][0][1]
            if index + 1 < len(pieces) else None
        )
        anchors.append(FittedPoint(
            cubic[3][0], cubic[3][1], pressure,
            incoming=cubic[2], outgoing=outgoing,
        ))
    return anchors


def _line_intersection(
    a0: Point,
    a1: Point,
    b0: Point,
    b1: Point,
    epsilon: float = 1.0e-8,
) -> tuple[Point, float, float] | None:
    first = _sub(a1, a0)
    second = _sub(b1, b0)
    denominator = _cross(first, second)
    difference = _sub(b0, a0)
    if abs(denominator) <= epsilon:
        return None
    first_t = _cross(difference, second) / denominator
    second_t = _cross(difference, first) / denominator
    if (
        -epsilon <= first_t <= 1 + epsilon
        and -epsilon <= second_t <= 1 + epsilon
    ):
        first_t = max(0.0, min(1.0, first_t))
        second_t = max(0.0, min(1.0, second_t))
        return _add(a0, _mul(first, first_t)), first_t, second_t
    return None


def _refine_cubic_intersection(
    first: Cubic,
    second: Cubic,
    first_t: float,
    second_t: float,
) -> tuple[Point, float, float]:
    """Newton-refine a flattened intersection against the source cubics."""
    first_t = max(0.0, min(1.0, first_t))
    second_t = max(0.0, min(1.0, second_t))
    for _ in range(12):
        first_point = cubic_eval(first, first_t)
        second_point = cubic_eval(second, second_t)
        difference = _sub(first_point, second_point)
        if _length(difference) <= 1.0e-10:
            break
        first_derivative = cubic_derivative(first, first_t)
        second_derivative = cubic_derivative(second, second_t)
        a, b = first_derivative[0], -second_derivative[0]
        c, d = first_derivative[1], -second_derivative[1]
        determinant = a * d - b * c
        if abs(determinant) <= 1.0e-12:
            break
        right_x, right_y = -difference[0], -difference[1]
        delta_first = (right_x * d - b * right_y) / determinant
        delta_second = (a * right_y - right_x * c) / determinant
        updated_first = max(0.0, min(1.0, first_t + delta_first))
        updated_second = max(0.0, min(1.0, second_t + delta_second))
        if (
            abs(updated_first - first_t) <= 1.0e-12
            and abs(updated_second - second_t) <= 1.0e-12
        ):
            first_t, second_t = updated_first, updated_second
            break
        first_t, second_t = updated_first, updated_second
    first_point = cubic_eval(first, first_t)
    second_point = cubic_eval(second, second_t)
    return (
        (
            (first_point[0] + second_point[0]) / 2,
            (first_point[1] + second_point[1]) / 2,
        ),
        first_t,
        second_t,
    )


def path_intersections(
    first: Sequence[Cubic],
    second: Sequence[Cubic],
    tolerance: float = 0.25,
) -> list[PathIntersection]:
    """Intersect two cubic paths through provenance-preserving flattening."""
    result: list[PathIntersection] = []
    first_flat = [flatten_cubic(cubic, tolerance, segment_index=index)
                  for index, cubic in enumerate(first)]
    second_flat = [flatten_cubic(cubic, tolerance, segment_index=index)
                   for index, cubic in enumerate(second)]
    for first_index, first_points in enumerate(first_flat):
        for second_index, second_points in enumerate(second_flat):
            for a0, a1 in zip(first_points, first_points[1:]):
                for b0, b1 in zip(second_points, second_points[1:]):
                    hit = _line_intersection(
                        a0.point, a1.point, b0.point, b1.point
                    )
                    if hit is None:
                        continue
                    point, first_amount, second_amount = hit
                    first_t = a0.t + (a1.t - a0.t) * first_amount
                    second_t = b0.t + (b1.t - b0.t) * second_amount
                    point, first_t, second_t = _refine_cubic_intersection(
                        first[first_index],
                        second[second_index],
                        first_t,
                        second_t,
                    )
                    candidate = PathIntersection(
                        point, first_index, first_t,
                        second_index, second_t,
                    )
                    if not any(
                        distance(existing.point, point) <= tolerance
                        and abs(
                            existing.first_segment + existing.first_t
                            - (first_index + first_t)
                        ) <= 1.0e-3
                        and abs(
                            existing.second_segment + existing.second_t
                            - (second_index + second_t)
                        ) <= 1.0e-3
                        for existing in result
                    ):
                        result.append(candidate)
    return result


def path_self_intersections(
    cubics: Sequence[Cubic],
    *,
    closed: bool = False,
    tolerance: float = 0.25,
) -> list[PathIntersection]:
    """Return non-adjacent centerline intersections, including loops."""
    pieces: list[tuple[int, FlattenedPoint, FlattenedPoint]] = []
    for index, cubic in enumerate(cubics):
        flattened = flatten_cubic(cubic, tolerance, segment_index=index)
        pieces.extend((index, first, second) for first, second
                      in zip(flattened, flattened[1:]))
    result: list[PathIntersection] = []
    for first_piece, (first_index, a0, a1) in enumerate(pieces):
        for second_index_flat in range(first_piece + 1, len(pieces)):
            second_index, b0, b1 = pieces[second_index_flat]
            same_segment_adjacent = (
                first_index == second_index
                and (
                    abs(a1.t - b0.t) <= 1.0e-8
                    or abs(b1.t - a0.t) <= 1.0e-8
                )
            )
            path_adjacent = abs(first_index - second_index) == 1
            seam_adjacent = (
                closed and {first_index, second_index} == {0, len(cubics) - 1}
            )
            if same_segment_adjacent:
                continue
            hit = _line_intersection(a0.point, a1.point, b0.point, b1.point)
            if hit is None:
                continue
            point, first_amount, second_amount = hit
            first_t = a0.t + (a1.t - a0.t) * first_amount
            second_t = b0.t + (b1.t - b0.t) * second_amount
            point, first_t, second_t = _refine_cubic_intersection(
                cubics[first_index],
                cubics[second_index],
                first_t,
                second_t,
            )
            if (path_adjacent or seam_adjacent) and (
                (first_t <= 1.0e-5 and second_t >= 1 - 1.0e-5)
                or (second_t <= 1.0e-5 and first_t >= 1 - 1.0e-5)
                or (first_t >= 1 - 1.0e-5 and second_t <= 1.0e-5)
                or (second_t >= 1 - 1.0e-5 and first_t <= 1.0e-5)
            ):
                continue
            candidate = PathIntersection(
                point, first_index, first_t, second_index, second_t
            )
            if not any(
                distance(existing.point, point) <= tolerance
                and abs(
                    existing.first_segment + existing.first_t
                    - (first_index + first_t)
                ) <= 1.0e-3
                for existing in result
            ):
                result.append(candidate)
    return result


def distance_to_polyline(point: Any, polyline: Sequence[Any]) -> float:
    target = point_xy(point)
    points = [point_xy(value) for value in polyline]
    if not points:
        return math.inf
    if len(points) == 1:
        return distance(target, points[0])
    return min(
        _nearest_on_segment(target, first, second)[2]
        for first, second in zip(points, points[1:])
    )


def _distance_segment_linf(point: Point, start: Point, end: Point) -> float:
    # max(abs(linear_x), abs(linear_y)) is convex on [0, 1].
    lower, upper = 0.0, 1.0
    direction = _sub(end, start)
    for _ in range(45):
        first_t = (2 * lower + upper) / 3
        second_t = (lower + 2 * upper) / 3
        first = _add(start, _mul(direction, first_t))
        second = _add(start, _mul(direction, second_t))
        first_distance = max(abs(point[0] - first[0]), abs(point[1] - first[1]))
        second_distance = max(
            abs(point[0] - second[0]), abs(point[1] - second[1])
        )
        if first_distance <= second_distance:
            upper = second_t
        else:
            lower = first_t
    projected = _add(start, _mul(direction, (lower + upper) / 2))
    return max(abs(point[0] - projected[0]), abs(point[1] - projected[1]))


def corridor_contains(
    point: Any,
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
) -> bool:
    target = point_xy(point)
    points = [point_xy(value) for value in sweep]
    if not points:
        return False
    radius = max(0.0, float(radius))
    if shape == "square":
        if len(points) == 1:
            metric = max(
                abs(target[0] - points[0][0]), abs(target[1] - points[0][1])
            )
        else:
            metric = min(
                _distance_segment_linf(target, first, second)
                for first, second in zip(points, points[1:])
            )
        return metric <= radius + EPSILON
    return distance_to_polyline(target, points) <= radius + EPSILON


def _cubic_corridor_intervals(
    cubic: Cubic,
    sweep: Sequence[Any],
    radius: float,
    shape: Literal["round", "square"],
) -> list[tuple[float, float]]:
    radius = max(0.0, float(radius))
    approximate_length = max(
        distance(cubic[0], cubic[1])
        + distance(cubic[1], cubic[2])
        + distance(cubic[2], cubic[3]),
        distance(cubic[0], cubic[3]),
    )
    step = max(0.25, radius / 3 if radius > EPSILON else 0.25)
    sample_count = max(16, min(4096, int(math.ceil(approximate_length / step))))
    parameters = [index / sample_count for index in range(sample_count + 1)]
    states = [
        corridor_contains(cubic_eval(cubic, value), sweep, radius, shape=shape)
        for value in parameters
    ]
    boundaries: list[float] = []
    for index, (first_state, second_state) in enumerate(zip(states, states[1:])):
        if first_state == second_state:
            continue
        lower, upper = parameters[index], parameters[index + 1]
        lower_state = first_state
        for _ in range(30):
            middle = (lower + upper) / 2
            state = corridor_contains(
                cubic_eval(cubic, middle), sweep, radius, shape=shape
            )
            if state == lower_state:
                lower = middle
            else:
                upper = middle
        boundaries.append((lower + upper) / 2)
    cuts = [0.0, *boundaries, 1.0]
    result: list[tuple[float, float]] = []
    for start, end in zip(cuts, cuts[1:]):
        if corridor_contains(
            cubic_eval(cubic, (start + end) / 2),
            sweep,
            radius,
            shape=shape,
        ):
            result.append((start, end))
    return result


def corridor_hits_path(
    cubics: Sequence[Cubic],
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
) -> bool:
    return any(
        _cubic_corridor_intervals(cubic, sweep, radius, shape)
        for cubic in cubics
    )


def corridor_path_intervals(
    cubics: Sequence[Cubic],
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
) -> list[tuple[int, float, float]]:
    """Return every source cubic interval covered by an eraser sweep."""
    return [
        (segment_index, start, end)
        for segment_index, cubic in enumerate(cubics)
        for start, end in _cubic_corridor_intervals(
            cubic, sweep, radius, shape
        )
    ]


def _complement_intervals(
    intervals: Sequence[tuple[float, float]],
) -> list[tuple[float, float]]:
    if not intervals:
        return [(0.0, 1.0)]
    merged: list[list[float]] = []
    for start, end in sorted(intervals):
        if not merged or start > merged[-1][1] + 1.0e-7:
            merged.append([start, end])
        else:
            merged[-1][1] = max(merged[-1][1], end)
    result: list[tuple[float, float]] = []
    cursor = 0.0
    for start, end in merged:
        if start > cursor + 1.0e-7:
            result.append((cursor, start))
        cursor = max(cursor, end)
    if cursor < 1.0 - 1.0e-7:
        result.append((cursor, 1.0))
    return result


def erase_cubics_by_corridor(
    cubics: Sequence[Cubic],
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
    closed: bool = False,
) -> list[list[CubicSpan]]:
    """Subtract an eraser corridor and group remaining exact path pieces."""
    groups: list[list[CubicSpan]] = []
    current: list[CubicSpan] = []
    for index, cubic in enumerate(cubics):
        erased = _cubic_corridor_intervals(cubic, sweep, radius, shape)
        kept = _complement_intervals(erased)
        for kept_index, (start, end) in enumerate(kept):
            span = CubicSpan(
                cubic_subsegment(cubic, start, end), index, start, end
            )
            joins_previous = (
                current
                and current[-1].source_segment == index - 1
                and current[-1].t1 >= 1.0 - 1.0e-7
                and start <= 1.0e-7
                and kept_index == 0
            )
            if not joins_previous and current:
                groups.append(current)
                current = []
            current.append(span)
            if end < 1.0 - 1.0e-7:
                groups.append(current)
                current = []
        if not kept and current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if (
        closed and len(groups) > 1
        and groups[0][0].source_segment == 0
        and groups[0][0].t0 <= 1.0e-7
        and groups[-1][-1].source_segment == len(cubics) - 1
        and groups[-1][-1].t1 >= 1.0 - 1.0e-7
    ):
        groups[0] = groups[-1] + groups[0]
        groups.pop()
    return groups


def _variable_cubic_corridor_intervals(
    cubic: Cubic,
    sweep: Sequence[Any],
    radius: float,
    start_width: float,
    end_width: float,
    shape: Literal["round", "square"],
) -> list[tuple[float, float]]:
    """Find covered parameters while interpolating the rendered line width."""
    radius = max(0.0, float(radius))
    start_width = max(0.0, float(start_width))
    end_width = max(0.0, float(end_width))

    def covered(parameter: float) -> bool:
        width = start_width + (end_width - start_width) * parameter
        return corridor_contains(
            cubic_eval(cubic, parameter),
            sweep,
            radius + width / 2,
            shape=shape,
        )

    approximate_length = max(
        distance(cubic[0], cubic[1])
        + distance(cubic[1], cubic[2])
        + distance(cubic[2], cubic[3]),
        distance(cubic[0], cubic[3]),
    )
    effective_radius = radius + max(start_width, end_width) / 2
    step = max(
        0.25,
        effective_radius / 3 if effective_radius > EPSILON else 0.25,
    )
    sample_count = max(
        16, min(4096, int(math.ceil(approximate_length / step)))
    )
    parameters = [
        index / sample_count for index in range(sample_count + 1)
    ]
    states = [covered(parameter) for parameter in parameters]
    boundaries: list[float] = []
    for index, (first_state, second_state) in enumerate(
        zip(states, states[1:])
    ):
        if first_state == second_state:
            continue
        lower, upper = parameters[index], parameters[index + 1]
        for _ in range(30):
            middle = (lower + upper) / 2
            if covered(middle) == first_state:
                lower = middle
            else:
                upper = middle
        boundaries.append((lower + upper) / 2)
    cuts = [0.0, *boundaries, 1.0]
    return [
        (start, end)
        for start, end in zip(cuts, cuts[1:])
        if covered((start + end) / 2)
    ]


def erase_stroke_by_corridor(
    points: Sequence[Any],
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
    closed: bool = False,
) -> list[list[CubicSpan]]:
    """Subtract a sweep using the stroke's continuously varying width."""
    cubics = stroke_cubics(points, closed)
    groups: list[list[CubicSpan]] = []
    current: list[CubicSpan] = []
    for index, cubic in enumerate(cubics):
        following = (index + 1) % len(points)
        erased = _variable_cubic_corridor_intervals(
            cubic,
            sweep,
            radius,
            float(getattr(points[index], "width", 1.0)),
            float(getattr(points[following], "width", 1.0)),
            shape,
        )
        kept = _complement_intervals(erased)
        for kept_index, (start, end) in enumerate(kept):
            span = CubicSpan(
                cubic_subsegment(cubic, start, end), index, start, end
            )
            joins_previous = (
                current
                and current[-1].source_segment == index - 1
                and current[-1].t1 >= 1.0 - 1.0e-7
                and start <= 1.0e-7
                and kept_index == 0
            )
            if not joins_previous and current:
                groups.append(current)
                current = []
            current.append(span)
            if end < 1.0 - 1.0e-7:
                groups.append(current)
                current = []
        if not kept and current:
            groups.append(current)
            current = []
    if current:
        groups.append(current)
    if (
        closed and len(groups) > 1
        and groups[0][0].source_segment == 0
        and groups[0][0].t0 <= 1.0e-7
        and groups[-1][-1].source_segment == len(cubics) - 1
        and groups[-1][-1].t1 >= 1.0 - 1.0e-7
    ):
        groups[0] = groups[-1] + groups[0]
        groups.pop()
    return groups


def erase_intersection_portion(
    cubics: Sequence[Cubic],
    sweep: Sequence[Any],
    radius: float,
    *,
    shape: Literal["round", "square"] = "round",
    closed: bool = False,
    tolerance: float = 0.25,
) -> list[list[CubicSpan]]:
    """Erase from the touched location to nearest centerline intersections.

    For open paths, endpoints act as fallback intersections.  Closed paths with
    no intersections erase the entire stroke, matching whole-loop behavior.
    """
    touched: PathProjection | None = None
    for sweep_point in sweep:
        candidate = nearest_on_path(cubics, sweep_point, tolerance)
        if candidate is None or candidate.distance > radius:
            continue
        if touched is None or candidate.distance < touched.distance:
            touched = candidate
    if touched is None:
        return [[CubicSpan(cubic, index, 0.0, 1.0)
                 for index, cubic in enumerate(cubics)]]
    scalar = touched.scalar_parameter
    cuts: list[float] = []
    for intersection in path_self_intersections(
        cubics, closed=closed, tolerance=tolerance
    ):
        cuts.extend((
            intersection.first_segment + intersection.first_t,
            intersection.second_segment + intersection.second_t,
        ))
    cuts = sorted({
        round(value, 9) for value in cuts
        if abs(value - scalar) > 1.0e-6
    })
    if not closed:
        cuts = [0.0, *cuts, float(len(cubics))]
    if not cuts:
        return []
    before = max((value for value in cuts if value < scalar), default=None)
    after = min((value for value in cuts if value > scalar), default=None)
    if closed and (before is None or after is None):
        shifted = [
            value + (len(cubics) if value < scalar else 0.0)
            for value in cuts
        ]
        after = min((value for value in shifted if value > scalar), default=None)
        before_candidates = [
            value - (len(cubics) if value > scalar else 0.0)
            for value in cuts
        ]
        before = max(
            (value for value in before_candidates if value < scalar),
            default=None,
        )
    if before is None or after is None:
        return []
    erased_by_segment: dict[int, list[tuple[float, float]]] = {}
    cursor, stop = before, after
    while cursor < stop - 1.0e-9:
        wrapped = cursor % len(cubics)
        index = int(math.floor(wrapped))
        local_start = wrapped - index
        available = 1.0 - local_start
        amount = min(available, stop - cursor)
        erased_by_segment.setdefault(index, []).append(
            (local_start, local_start + amount)
        )
        cursor += amount
    groups: list[list[CubicSpan]] = []
    current: list[CubicSpan] = []
    for index, cubic in enumerate(cubics):
        for start, end in _complement_intervals(
            erased_by_segment.get(index, [])
        ):
            span = CubicSpan(
                cubic_subsegment(cubic, start, end), index, start, end
            )
            if (
                current
                and not (
                    current[-1].source_segment == index - 1
                    and current[-1].t1 >= 1 - 1.0e-7
                    and start <= 1.0e-7
                )
            ):
                groups.append(current)
                current = []
            current.append(span)
            if end < 1 - 1.0e-7:
                groups.append(current)
                current = []
        if not _complement_intervals(erased_by_segment.get(index, [])):
            if current:
                groups.append(current)
                current = []
    if current:
        groups.append(current)
    return groups


def simplify_tolerance(amount: float) -> float:
    """Map the UI's 0–100 amount to the specified document tolerance."""
    normalized = max(0.0, min(100.0, float(amount))) / 100.0
    return 0.25 + 24.75 * normalized * normalized


def rdp_indices(points: Sequence[Any], tolerance: float) -> list[int]:
    values = [point_xy(point) for point in points]
    if len(values) <= 2:
        return list(range(len(values)))
    tolerance = max(0.0, float(tolerance))
    keep = {0, len(values) - 1}
    stack = [(0, len(values) - 1)]
    while stack:
        first, last = stack.pop()
        maximum, split = -1.0, -1
        for index in range(first + 1, last):
            candidate = _point_line_distance(
                values[index], values[first], values[last]
            )
            if candidate > maximum:
                maximum, split = candidate, index
        if maximum > tolerance and split >= 0:
            keep.add(split)
            stack.extend(((first, split), (split, last)))
    return sorted(keep)


def simplify_polyline(
    points: Sequence[Any],
    tolerance: float,
) -> list[Point]:
    values = [point_xy(point) for point in points]
    return [values[index] for index in rdp_indices(values, tolerance)]


def simplify_polyline_local(
    points: Sequence[Any],
    sweep: Sequence[Any],
    radius: float,
    amount: float,
) -> list[Point]:
    """Simplify only contiguous point runs touched by a screen-space corridor."""
    values = [point_xy(point) for point in points]
    if len(values) <= 2:
        return values
    affected = [
        corridor_contains(point, sweep, radius) for point in values
    ]
    result: list[Point] = []
    index = 0
    tolerance = simplify_tolerance(amount)
    while index < len(values):
        if not affected[index]:
            if not result or result[-1] != values[index]:
                result.append(values[index])
            index += 1
            continue
        start = max(0, index - 1)
        while index + 1 < len(values) and affected[index + 1]:
            index += 1
        end = min(len(values) - 1, index + 1)
        simplified = simplify_polyline(values[start:end + 1], tolerance)
        if result and simplified and result[-1] == simplified[0]:
            simplified = simplified[1:]
        result.extend(simplified)
        index = end + 1
    return result


def simplify_cubics_local(
    cubics: Sequence[Cubic],
    sweep: Sequence[Any],
    radius: float,
    amount: float,
    *,
    flatten_tolerance: float = 0.25,
) -> list[Cubic]:
    """Refit touched spans while retaining every untouched sub-span exactly."""
    if not cubics:
        return []
    result: list[Cubic] = []
    fit_error = simplify_tolerance(amount)
    for cubic in cubics:
        intervals = _cubic_corridor_intervals(
            cubic, sweep, radius, "round"
        )
        if not intervals:
            result.append(cubic)
            continue
        boundaries = sorted({
            0.0,
            1.0,
            *(value for interval in intervals for value in interval),
        })
        for start, end in zip(boundaries, boundaries[1:]):
            midpoint = (start + end) / 2
            touched = any(
                interval_start - 1.0e-8 <= midpoint
                <= interval_end + 1.0e-8
                for interval_start, interval_end in intervals
            )
            subsegment = cubic_subsegment(cubic, start, end)
            if not touched:
                result.append(subsegment)
                continue
            flattened = [
                item.point for item in flatten_cubic(
                    subsegment, flatten_tolerance
                )
            ]
            reduced = simplify_polyline(flattened, fit_error)
            fitted = fit_cubic_path(reduced, fit_error)
            result.extend(item.cubic for item in fitted)
    return result


def simplify_cubic_segments(
    cubics: Sequence[Cubic],
    segment_indexes: Iterable[int],
    amount: float,
    *,
    closed: bool = False,
    flatten_tolerance: float = 0.25,
) -> list[Cubic]:
    """Refit complete selected spans while retaining other cubics exactly.

    Closed paths may be rotated to keep a selected run contiguous.  Rotating
    the starting anchor does not change their rendered geometry.
    """
    values = list(cubics)
    count = len(values)
    selected = {
        int(index) for index in segment_indexes
        if 0 <= int(index) < count
    }
    if not values or not selected:
        return values

    order = list(range(count))
    if closed and len(selected) < count:
        # Starting at any unselected span makes a run that crosses the stored
        # seam appear only at the end of the linear work list.
        start = next(index for index in order if index not in selected)
        order = order[start:] + order[:start]
    ordered = [values[index] for index in order]
    ordered_selected = [index in selected for index in order]
    fit_error = simplify_tolerance(amount)
    result: list[Cubic] = []
    index = 0
    while index < count:
        if not ordered_selected[index]:
            result.append(ordered[index])
            index += 1
            continue
        end = index + 1
        while end < count and ordered_selected[end]:
            end += 1
        flattened: list[Point] = []
        for cubic in ordered[index:end]:
            samples = [
                item.point for item in flatten_cubic(
                    cubic, flatten_tolerance
                )
            ]
            flattened.extend(samples if not flattened else samples[1:])
        reduced = simplify_polyline(flattened, fit_error)
        fitted = fit_cubic_path(reduced, fit_error)
        result.extend(item.cubic for item in fitted)
        index = end
    return result


def tangent_bridge(
    start: Any,
    end: Any,
    start_tangent: Any,
    end_tangent: Any,
    *,
    maximum_control_ratio: float = 1 / 3,
) -> Cubic:
    """Make a clamped cubic bridge with requested departure/arrival tangents."""
    start_point, end_point = point_xy(start), point_xy(end)
    chord = _sub(end_point, start_point)
    chord_length = _length(chord)
    fallback = _normalized(chord)
    departure = _normalized(point_xy(start_tangent), fallback)
    arrival = _normalized(point_xy(end_tangent), fallback)
    maximum = chord_length * max(0.0, min(0.5, maximum_control_ratio))
    # A sharp backwards tangent can otherwise make an immediate loop.
    if _dot(departure, fallback) < -0.25:
        departure = fallback
    if _dot(arrival, fallback) < -0.25:
        arrival = fallback
    return (
        start_point,
        _add(start_point, _mul(departure, maximum)),
        _sub(end_point, _mul(arrival, maximum)),
        end_point,
    )


def connect_cubic_paths(
    first: Sequence[Cubic],
    first_endpoint: Literal["start", "end"],
    second: Sequence[Cubic],
    second_endpoint: Literal["start", "end"],
) -> ConnectionResult:
    """Orient two paths and connect the chosen endpoints with one bridge."""
    if not first or not second:
        raise ValueError("Both paths require at least one cubic")
    first_reversed = first_endpoint == "start"
    second_reversed = second_endpoint == "end"
    oriented_first = (
        [reverse_cubic(cubic) for cubic in reversed(first)]
        if first_reversed else list(first)
    )
    oriented_second = (
        [reverse_cubic(cubic) for cubic in reversed(second)]
        if second_reversed else list(second)
    )
    first_tangent = cubic_derivative(oriented_first[-1], 1.0)
    second_tangent = cubic_derivative(oriented_second[0], 0.0)
    bridge = tangent_bridge(
        oriented_first[-1][3],
        oriented_second[0][0],
        first_tangent,
        second_tangent,
    )
    return ConnectionResult(
        tuple([*oriented_first, bridge, *oriented_second]),
        bridge,
        first_reversed,
        second_reversed,
    )


def polygon_area(points: Sequence[Any]) -> float:
    values = [point_xy(point) for point in points]
    if len(values) < 3:
        return 0.0
    return sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(values, values[1:] + values[:1])
    ) / 2


def point_in_polygon(
    point: Any,
    polygon: Sequence[Any],
    *,
    include_boundary: bool = True,
) -> bool:
    target = point_xy(point)
    values = [point_xy(value) for value in polygon]
    if len(values) < 3:
        return False
    inside = False
    for first, second in zip(values, values[1:] + values[:1]):
        if (
            include_boundary
            and _point_line_distance(target, first, second) <= 1.0e-8
        ):
            projection = _nearest_on_segment(target, first, second)
            if projection[2] <= 1.0e-8:
                return True
        if (first[1] > target[1]) != (second[1] > target[1]):
            crossing_x = (
                (second[0] - first[0])
                * (target[1] - first[1])
                / (second[1] - first[1])
                + first[0]
            )
            if target[0] < crossing_x:
                inside = not inside
    return inside


@dataclass
class _SourceLine:
    start: Point
    end: Point
    provenance: EdgeProvenance
    cuts: list[float] = field(default_factory=lambda: [0.0, 1.0])


def _quantized(point: Point, epsilon: float) -> tuple[int, int]:
    return round(point[0] / epsilon), round(point[1] / epsilon)


def _coerce_flat_path(
    path: Sequence[Any],
    path_index: int,
) -> list[tuple[Point, float, int]]:
    result: list[tuple[Point, float, int]] = []
    for fallback_index, value in enumerate(path):
        if isinstance(value, FlattenedPoint):
            result.append((value.point, value.t, value.segment_index))
        else:
            result.append((point_xy(value), float(fallback_index), fallback_index))
    return result


def trace_planar_faces(
    paths: Sequence[Sequence[Any]],
    *,
    closed: Sequence[bool] | None = None,
    gap_threshold: float = 0.0,
    epsilon: float = 1.0e-6,
) -> list[PlanarFace]:
    """Split a set of centerline polylines and trace all bounded faces.

    Flattened points preserve their cubic segment/t provenance.  Plain point
    paths use their line index as provenance.  Open endpoints within
    ``gap_threshold`` are greedily connected with virtual edges.
    """
    if closed is None:
        closed = [
            len(path) > 2 and distance(path[0], path[-1]) <= epsilon
            for path in paths
        ]
    if len(closed) != len(paths):
        raise ValueError("closed flags must match paths")
    lines: list[_SourceLine] = []
    open_endpoints: list[tuple[int, Point]] = []
    for path_index, (path, is_closed) in enumerate(zip(paths, closed)):
        values = _coerce_flat_path(path, path_index)
        if len(values) < 2:
            continue
        pairs = list(zip(values, values[1:]))
        if is_closed and distance(values[-1][0], values[0][0]) > epsilon:
            pairs.append((values[-1], values[0]))
        for (
            start,
            start_t,
            segment,
        ), (
            end,
            end_t,
            end_segment,
        ) in pairs:
            if distance(start, end) <= epsilon:
                continue
            if end_segment != segment:
                segment = end_segment
                start_t = 0.0
            lines.append(_SourceLine(
                start,
                end,
                EdgeProvenance(
                    path_index, segment, start_t, end_t, False
                ),
            ))
        if not is_closed:
            open_endpoints.extend((
                (path_index, values[0][0]),
                (path_index, values[-1][0]),
            ))
    if gap_threshold > 0 and len(open_endpoints) > 1:
        candidates: list[tuple[float, int, int]] = []
        for first in range(len(open_endpoints)):
            for second in range(first + 1, len(open_endpoints)):
                separation = distance(
                    open_endpoints[first][1], open_endpoints[second][1]
                )
                if epsilon < separation <= gap_threshold:
                    candidates.append((separation, first, second))
        used: set[int] = set()
        for _, first, second in sorted(candidates):
            if first in used or second in used:
                continue
            used.update((first, second))
            lines.append(_SourceLine(
                open_endpoints[first][1],
                open_endpoints[second][1],
                EdgeProvenance(-1, -1, 0.0, 1.0, True),
            ))
    for first_index, first in enumerate(lines):
        for second_index in range(first_index + 1, len(lines)):
            second = lines[second_index]
            hit = _line_intersection(
                first.start, first.end, second.start, second.end, epsilon
            )
            if hit is None:
                continue
            _, first_t, second_t = hit
            first.cuts.append(first_t)
            second.cuts.append(second_t)
    nodes: dict[tuple[int, int], Point] = {}
    adjacency: dict[tuple[int, int], set[tuple[int, int]]] = {}
    edge_data: dict[
        tuple[tuple[int, int], tuple[int, int]], EdgeProvenance
    ] = {}
    for line in lines:
        cuts = sorted({
            max(0.0, min(1.0, round(value, 12))) for value in line.cuts
        })
        for start_t, end_t in zip(cuts, cuts[1:]):
            start, end = lerp(line.start, line.end, start_t), lerp(
                line.start, line.end, end_t
            )
            if distance(start, end) <= epsilon:
                continue
            start_key, end_key = (
                _quantized(start, epsilon),
                _quantized(end, epsilon),
            )
            nodes.setdefault(start_key, start)
            nodes.setdefault(end_key, end)
            adjacency.setdefault(start_key, set()).add(end_key)
            adjacency.setdefault(end_key, set()).add(start_key)
            source_t0 = (
                line.provenance.t0
                + (line.provenance.t1 - line.provenance.t0) * start_t
            )
            source_t1 = (
                line.provenance.t0
                + (line.provenance.t1 - line.provenance.t0) * end_t
            )
            forward = EdgeProvenance(
                line.provenance.path_index,
                line.provenance.segment_index,
                source_t0,
                source_t1,
                line.provenance.virtual,
            )
            reverse = EdgeProvenance(
                forward.path_index,
                forward.segment_index,
                forward.t1,
                forward.t0,
                forward.virtual,
            )
            edge_data[start_key, end_key] = forward
            edge_data[end_key, start_key] = reverse
    ordered = {
        key: sorted(
            neighbors,
            key=lambda neighbor: math.atan2(
                nodes[neighbor][1] - nodes[key][1],
                nodes[neighbor][0] - nodes[key][0],
            ),
        )
        for key, neighbors in adjacency.items()
    }
    visited: set[tuple[tuple[int, int], tuple[int, int]]] = set()
    faces: list[PlanarFace] = []
    maximum_steps = max(1, len(edge_data) + 1)
    for directed in list(edge_data):
        if directed in visited:
            continue
        start_edge = directed
        edge = directed
        keys: list[tuple[int, int]] = []
        provenance: list[EdgeProvenance] = []
        valid = False
        for _ in range(maximum_steps):
            if edge in visited:
                valid = edge == start_edge
                break
            visited.add(edge)
            start_key, end_key = edge
            keys.append(start_key)
            provenance.append(edge_data[edge])
            neighbors = ordered[end_key]
            reverse_index = neighbors.index(start_key)
            next_key = neighbors[(reverse_index - 1) % len(neighbors)]
            edge = end_key, next_key
            if edge == start_edge:
                valid = True
                break
        if not valid or len(keys) < 3:
            continue
        vertices = tuple(nodes[key] for key in keys)
        area = polygon_area(vertices)
        # With the clockwise-from-reverse traversal above, bounded faces have
        # positive signed area and the unbounded face is negative.
        if area <= epsilon * epsilon:
            continue
        edges = tuple(
            FaceEdge(
                vertices[index],
                vertices[(index + 1) % len(vertices)],
                provenance[index],
            )
            for index in range(len(vertices))
        )
        candidate = PlanarFace(vertices, edges, area)
        if not any(
            abs(existing.area - candidate.area) <= epsilon
            and existing.contains(candidate.vertices[0])
            and candidate.contains(existing.vertices[0])
            for existing in faces
        ):
            faces.append(candidate)
    return sorted(faces, key=lambda face: face.area)


def trace_cubic_faces(
    paths: Sequence[Sequence[Cubic]],
    *,
    closed: Sequence[bool] | None = None,
    gap_threshold: float = 0.0,
    flatten_tolerance: float = 0.25,
) -> list[PlanarFace]:
    flattened = [
        flatten_path(path, flatten_tolerance) for path in paths
    ]
    return trace_planar_faces(
        flattened, closed=closed, gap_threshold=gap_threshold
    )


def find_face_containing(
    faces: Sequence[PlanarFace],
    seed: Any,
) -> PlanarFace | None:
    """Choose the smallest bounded face containing ``seed``."""
    containing = [face for face in faces if face.contains(seed)]
    return min(containing, key=lambda face: face.area, default=None)


__all__ = [
    "ConnectionResult",
    "Cubic",
    "CubicProjection",
    "CubicSegment",
    "CubicSpan",
    "EdgeProvenance",
    "FaceEdge",
    "FittedCubic",
    "FittedPoint",
    "FlattenedPoint",
    "FreehandSample",
    "PathIntersection",
    "PathProjection",
    "PlanarFace",
    "Point",
    "StrokeLocation",
    "StrokeSample",
    "centerline_hit",
    "connect_cubic_paths",
    "corridor_contains",
    "corridor_hits_path",
    "corridor_path_intervals",
    "cubic_arc_length",
    "cubic_derivative",
    "cubic_eval",
    "cubic_flatness",
    "cubic_second_derivative",
    "cubic_subsegment",
    "deduplicate_samples",
    "distance",
    "distance_to_polyline",
    "erase_cubics_by_corridor",
    "erase_intersection_portion",
    "erase_stroke_by_corridor",
    "find_face_containing",
    "fit_cubic_path",
    "fit_freehand",
    "flatten_cubic",
    "flatten_path",
    "flatten_stroke",
    "interpolate_stroke_attribute",
    "lerp",
    "nearest_on_cubic",
    "nearest_on_path",
    "nearest_on_stroke",
    "path_arc_lengths",
    "path_intersections",
    "path_self_intersections",
    "point_in_polygon",
    "point_xy",
    "polygon_area",
    "rdp_indices",
    "resample_freehand",
    "reverse_cubic",
    "simplify_cubics_local",
    "simplify_cubic_segments",
    "simplify_polyline",
    "simplify_polyline_local",
    "simplify_tolerance",
    "split_cubic",
    "stroke_cubics",
    "t_at_arc_length",
    "tangent_bridge",
    "trace_cubic_faces",
    "trace_planar_faces",
]
