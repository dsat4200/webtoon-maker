"""Shared, source-attributed shape contours for fills, outlines and editing.

Only geometry participates in compilation; outline styling never changes a fill.
The corner construction is shared with the original shape renderer.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath

from comic_editor.core.models import BoundGeometry, PathNode
from comic_editor.core.vector_geometry import (
    CubicSegment, cubic_eval, cubic_subsegment, nearest_on_path,
)


@dataclass(frozen=True)
class CompiledEdge:
    path: QPainterPath
    index: int
    start_id: str
    end_id: str


@dataclass(frozen=True)
class CompiledContour:
    path: QPainterPath
    edges: tuple[CompiledEdge, ...]
    closed: bool


def geometry_key(bound):
    return (bound.primitive, tuple(
        (c.closed, tuple((n.node_id, n.x, n.y, n.incoming, n.outgoing,
                         n.point_type, n.roundness, n.roundness_enabled,
                         n.handles_locked) for n in c.nodes))
        for c in bound.iter_contours()))


def transform_stretch(transform, bounds=None):
    """Conservative output/local scale, including projective denominators."""
    a, b, c, d = transform.m11(), transform.m21(), transform.m12(), transform.m22()
    square = a*a+b*b+c*c+d*d
    if transform.isAffine() or bounds is None:
        return math.sqrt(max(0., (square+math.sqrt(max(0., square*square-4*(a*d-b*c)**2)))/2))
    points = [bounds.topLeft(), bounds.topRight(), bounds.bottomLeft(), bounds.bottomRight()]
    numerators = [0., 0., 0., 0.]
    denominators = []
    for p in points:
        w = transform.m13()*p.x()+transform.m23()*p.y()+transform.m33()
        x = a*p.x()+b*p.y()+transform.m31()
        y = c*p.x()+d*p.y()+transform.m32()
        denominators.append(w)
        values = (a*w-x*transform.m13(), b*w-x*transform.m23(),
                  c*w-y*transform.m13(), d*w-y*transform.m23())
        numerators = [max(old, abs(new)) for old, new in zip(numerators, values)]
    smallest = min(abs(w) for w in denominators)
    if min(denominators) <= 0 <= max(denominators):
        smallest = 1e-9
    return math.sqrt(sum(v*v for v in numerators)) / max(1e-18, smallest*smallest)


def path_segments(path):
    """Decode exact Qt lines/cubics without flattening or losing parameters."""
    result = []
    point = (0., 0.)
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        target = (element.x, element.y)
        if element.isMoveTo():
            point = target
        elif element.isLineTo():
            if target != point:
                delta = (target[0] - point[0], target[1] - point[1])
                result.append(CubicSegment((point,
                    (point[0] + delta[0] / 3, point[1] + delta[1] / 3),
                    (point[0] + delta[0] * 2 / 3, point[1] + delta[1] * 2 / 3), target)))
            point = target
        elif element.isCurveTo():
            second, last = path.elementAt(index + 1), path.elementAt(index + 2)
            target = (last.x, last.y)
            result.append(CubicSegment((point, (element.x, element.y),
                                      (second.x, second.y), target)))
            point = target
            index += 2
        index += 1
    return tuple(result)


def cubic_path(cubic):
    result = QPainterPath(QPointF(*cubic[0]))
    result.cubicTo(*(QPointF(*point) for point in cubic[1:]))
    return result


def make_custom(bound):
    """Convert primitives without changing their visible contour."""
    if bound.primitive == "ellipse":
        spans = path_segments(compile_contour(bound).path)
        nodes = []
        for index, span in enumerate(spans):
            node = PathNode.from_dict(bound.nodes[index % len(bound.nodes)].to_dict())
            node.position = span.cubic[0]
            node.outgoing = span.cubic[1]
            node.incoming = spans[index-1].cubic[2]
            node.point_type, node.handles_locked = "bezier", True
            node.roundness_enabled, node.roundness = False, 0.
            nodes.append(node)
        bound.nodes = nodes
    bound.primitive = "custom"


def raw_edge_cubics(bound):
    """Editable source edges, including hidden ones (straight t is linear)."""
    if bound.primitive == "ellipse":
        return tuple(s.cubic for s in path_segments(compile_contour(bound).path))
    result = []
    count = len(bound.nodes) if bound.closed else max(0, len(bound.nodes)-1)
    for index in range(count):
        a, b = bound.nodes[index], bound.nodes[(index+1) % len(bound.nodes)]
        if a.outgoing is None and b.incoming is None:
            c1 = (a.x+(b.x-a.x)/3, a.y+(b.y-a.y)/3)
            c2 = (a.x+(b.x-a.x)*2/3, a.y+(b.y-a.y)*2/3)
        else:
            c1, c2 = a.outgoing or a.position, b.incoming or b.position
        result.append((a.position, c1, c2, b.position))
    return tuple(result)


def outline_fraction_at(bound, index, parameter):
    """Width position on a logical edge, including its two corner halves."""
    point = cubic_eval(raw_edge_cubics(bound)[index], parameter)
    edge = compile_contour(bound).edges[index]
    cubics = [s.cubic for s in path_segments(edge.path)]
    hit = nearest_on_path(cubics, point, tolerance=.01)
    if hit is None:
        return parameter
    lengths = [cubic_path(c).length() for c in cubics]
    before = sum(lengths[:hit.segment_index])
    before += cubic_path(cubic_subsegment(cubics[hit.segment_index], 0., hit.t)).length()
    return max(0., min(1., before/max(1e-9, sum(lengths))))


def path_slice(path, start, end):
    """Arc-length slice, compatible with the minimum supported Qt (6.7)."""
    if path.isEmpty() or end <= start:
        return QPainterPath()
    if start <= 0 and end >= 1:
        return QPainterPath(path)
    segments = [(s.cubic, cubic_path(s.cubic)) for s in path_segments(path)]
    lengths = [p.length() for _, p in segments]
    total = sum(lengths)
    start, end = start * total, end * total
    offset = 0.
    result = QPainterPath()
    for (cubic, piece), length in zip(segments, lengths):
        if length > 1e-9 and offset < end and offset + length > start:
            t0 = piece.percentAtLength(max(0., start - offset))
            t1 = piece.percentAtLength(min(length, end - offset))
            result.connectPath(cubic_path(cubic_subsegment(cubic, t0, t1)))
        offset += length
    return result


def compile_bound(bound, vertex_radius=0):
    contours = [compile_contour(bound, vertex_radius)]
    for contour in bound.additional_contours:
        contours.append(compile_contour(BoundGeometry(
            nodes=contour.nodes, closed=contour.closed, primitive="custom")))
    return tuple(contours)


def bound_path(bound, vertex_radius=0):
    contours = compile_bound(bound, vertex_radius)
    result = QPainterPath(contours[0].path)
    if len(contours) > 1:
        result.setFillRule(Qt.OddEvenFill)
        for contour in contours[1:]:
            result.addPath(contour.path)
    return result


def compile_contour(
    bound: BoundGeometry, vertex_radius: float = 0.0,
) -> CompiledContour:
    path = QPainterPath()
    path.setFillRule(Qt.WindingFill)
    if bound.primitive == "ellipse":
        x, y, width, height = bound.bbox()
        path.addEllipse(QRectF(x, y, width, height))
        # Retain quadrant ownership for compound boundaries as well as the
        # native ellipse fill. A primitive's baseline must not become its
        # compound parent's fallback width simply because it has no raw lines.
        edges = tuple(CompiledEdge(cubic_path(segment.cubic), index,
                                  bound.nodes[index % len(bound.nodes)].node_id,
                                  bound.nodes[(index+1) % len(bound.nodes)].node_id)
                      for index, segment in enumerate(path_segments(path)))
        return CompiledContour(path, edges, bound.closed)
    nodes = bound.nodes
    if not nodes:
        return CompiledContour(path, (), bound.closed)

    def segment_points(index: int) -> tuple[QPointF, QPointF, QPointF, QPointF]:
        start = nodes[index]
        end = nodes[(index + 1) % len(nodes)]
        p0 = QPointF(start.x, start.y)
        p3 = QPointF(end.x, end.y)
        return (
            p0,
            QPointF(*(start.outgoing or start.position)),
            QPointF(*(end.incoming or end.position)),
            p3,
        )

    def split_cubic(
        points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
    ) -> tuple[
        tuple[QPointF, QPointF, QPointF, QPointF],
        tuple[QPointF, QPointF, QPointF, QPointF],
    ]:
        p0, p1, p2, p3 = points
        a = p0 * (1 - percent) + p1 * percent
        b = p1 * (1 - percent) + p2 * percent
        c = p2 * (1 - percent) + p3 * percent
        d = a * (1 - percent) + b * percent
        e = b * (1 - percent) + c * percent
        point = d * (1 - percent) + e * percent
        return (p0, a, d, point), (point, e, c, p3)

    def sub_cubic(
        points: tuple[QPointF, QPointF, QPointF, QPointF],
        start: float,
        end: float,
    ) -> tuple[QPointF, QPointF, QPointF, QPointF]:
        if end < 1:
            points = split_cubic(points, end)[0]
        if start > 0:
            relative = start / max(end, 1e-9)
            points = split_cubic(points, relative)[1]
        return points

    def cubic_point(
        points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
    ) -> QPointF:
        inverse = 1 - percent
        p0, p1, p2, p3 = points
        return (
            p0 * (inverse ** 3)
            + p1 * (3 * inverse * inverse * percent)
            + p2 * (3 * inverse * percent * percent)
            + p3 * (percent ** 3)
        )

    def cubic_tangent(
        points: tuple[QPointF, QPointF, QPointF, QPointF], percent: float,
    ) -> QPointF:
        inverse = 1 - percent
        p0, p1, p2, p3 = points
        return (
            (p1 - p0) * (3 * inverse * inverse)
            + (p2 - p1) * (6 * inverse * percent)
            + (p3 - p2) * (3 * percent * percent)
        )

    def length_table(
        points: tuple[QPointF, QPointF, QPointF, QPointF],
    ) -> list[tuple[float, float]]:
        result = [(0.0, 0.0)]
        previous = points[0]
        total = 0.0
        for step in range(1, 49):
            percent = step / 48
            current = cubic_point(points, percent)
            total += math.dist(
                (previous.x(), previous.y()),
                (current.x(), current.y()),
            )
            result.append((percent, total))
            previous = current
        return result

    def parameter_at_length(
        table: list[tuple[float, float]], target: float,
    ) -> float:
        target = max(0.0, min(table[-1][1], target))
        for index in range(1, len(table)):
            percent, distance = table[index]
            if distance < target:
                continue
            previous_percent, previous_distance = table[index - 1]
            span = max(distance - previous_distance, 1e-9)
            ratio = (target - previous_distance) / span
            return previous_percent + (percent - previous_percent) * ratio
        return 1.0

    curve_rounding: dict[int, dict[str, object]] = {}
    segment_count = len(nodes) if bound.closed else len(nodes) - 1
    for index, node in enumerate(nodes):
        if not (
            node.roundness_enabled
            and node.roundness > 0
            and node.point_type == "bezier"
            and not node.handles_locked
            and node.incoming is not None
            and node.outgoing is not None
            and (bound.closed or 0 < index < len(nodes) - 1)
        ):
            continue
        incoming_index = index - 1 if index else len(nodes) - 1
        outgoing_index = index
        if not (0 <= incoming_index < segment_count and 0 <= outgoing_index < segment_count):
            continue
        incoming_curve = segment_points(incoming_index)
        outgoing_curve = segment_points(outgoing_index)
        incoming_table = length_table(incoming_curve)
        outgoing_table = length_table(outgoing_curve)
        incoming_length = incoming_table[-1][1]
        outgoing_length = outgoing_table[-1][1]
        radius = min(
            node.roundness, incoming_length / 2, outgoing_length / 2
        )
        if radius <= 1e-6:
            continue
        entry_t = parameter_at_length(
            incoming_table, incoming_length - radius
        )
        exit_t = parameter_at_length(outgoing_table, radius)
        curve_rounding[index] = {
            "entry": cubic_point(incoming_curve, entry_t),
            "exit": cubic_point(outgoing_curve, exit_t),
            "entry_t": entry_t,
            "exit_t": exit_t,
            "incoming_tangent": cubic_tangent(incoming_curve, entry_t),
            "outgoing_tangent": cubic_tangent(outgoing_curve, exit_t),
            "anchor_incoming_tangent": cubic_tangent(incoming_curve, 1.0),
            "anchor_outgoing_tangent": cubic_tangent(outgoing_curve, 0.0),
        }

    def segment_is_cubic(index: int) -> bool:
        start = nodes[index]
        end = nodes[(index + 1) % len(nodes)]
        return start.outgoing is not None or end.incoming is not None

    vector_curve_rounding: dict[int, dict[str, object]] = {}
    for index, node in enumerate(nodes):
        if not (
            node.point_type == "vector"
            and node.roundness_enabled
            and node.roundness > 0
            and (bound.closed or 0 < index < len(nodes) - 1)
        ):
            continue
        incoming_index = index - 1 if index else len(nodes) - 1
        outgoing_index = index
        if not (
            0 <= incoming_index < segment_count
            and 0 <= outgoing_index < segment_count
            and (
                segment_is_cubic(incoming_index)
                or segment_is_cubic(outgoing_index)
            )
        ):
            continue
        incoming_curve = segment_points(incoming_index)
        outgoing_curve = segment_points(outgoing_index)
        incoming_table = length_table(incoming_curve)
        outgoing_table = length_table(outgoing_curve)
        incoming_length = incoming_table[-1][1]
        outgoing_length = outgoing_table[-1][1]
        radius = min(
            node.roundness, incoming_length / 2, outgoing_length / 2
        )
        if radius <= 1e-6:
            continue
        entry_t = parameter_at_length(
            incoming_table, incoming_length - radius
        )
        exit_t = parameter_at_length(outgoing_table, radius)
        vector_curve_rounding[index] = {
            "entry": cubic_point(incoming_curve, entry_t),
            "exit": cubic_point(outgoing_curve, exit_t),
            "entry_t": entry_t,
            "exit_t": exit_t,
            "incoming_tangent": cubic_tangent(incoming_curve, entry_t),
            "outgoing_tangent": cubic_tangent(outgoing_curve, exit_t),
        }

    trim_rounding = {
        **vector_curve_rounding,
        **curve_rounding,
    }

    def rounding(index: int) -> tuple[QPointF, QPointF]:
        if index in curve_rounding:
            corner = curve_rounding[index]
            return corner["entry"], corner["exit"]
        if index in vector_curve_rounding:
            corner = vector_curve_rounding[index]
            return corner["entry"], corner["exit"]
        node = nodes[index]
        position = QPointF(node.x, node.y)
        may_round = (
            node.roundness_enabled
            and node.roundness > 0
            and node.point_type == "vector"
            and (bound.closed or 0 < index < len(nodes) - 1)
        )
        if not may_round:
            return position, position
        previous = nodes[index - 1 if index else len(nodes) - 1]
        following = nodes[(index + 1) % len(nodes)]
        before = QPointF(previous.x - node.x, previous.y - node.y)
        after = QPointF(following.x - node.x, following.y - node.y)
        before_length = max(1e-6, math.hypot(before.x(), before.y()))
        after_length = max(1e-6, math.hypot(after.x(), after.y()))
        distance = min(
            node.roundness, before_length / 2, after_length / 2
        )
        return (
            position + before * (distance / before_length),
            position + after * (distance / after_length),
        )

    rounded = [rounding(index) for index in range(len(nodes))]

    edge_paths = {}
    corners = {}

    def segment(start_index: int, end_index: int) -> None:
        path = QPainterPath(rounded[start_index][1])
        start_node, end_node = nodes[start_index], nodes[end_index]
        target = rounded[end_index][0]
        if start_index in trim_rounding or end_index in trim_rounding:
            points = segment_points(start_index)
            start_t = float(
                trim_rounding.get(start_index, {}).get("exit_t", 0.0)
            )
            end_t = float(
                trim_rounding.get(end_index, {}).get("entry_t", 1.0)
            )
            if start_t > end_t:
                start_t = end_t = (start_t + end_t) / 2
            p0, p1, p2, p3 = sub_cubic(points, start_t, end_t)
            actual_start = rounded[start_index][1]
            p1 += actual_start - p0
            p2 += target - p3
            path.cubicTo(p1, p2, target)
        elif start_node.outgoing is not None or end_node.incoming is not None:
            control_a = (
                QPointF(*start_node.outgoing)
                if start_node.outgoing is not None
                else QPointF(rounded[start_index][1])
            )
            control_b = (
                QPointF(*end_node.incoming)
                if end_node.incoming is not None else QPointF(target)
            )
            path.cubicTo(control_a, control_b, target)
        else:
            path.lineTo(target)
        edge_paths[start_index] = path
        path = QPainterPath(target)
        if end_index in curve_rounding:
            corner = curve_rounding[end_index]
            entry = corner["entry"]
            exit_point = corner["exit"]
            incoming_tangent = corner["incoming_tangent"]
            outgoing_tangent = corner["outgoing_tangent"]
            anchor = QPointF(end_node.x, end_node.y)

            def unit(vector: QPointF) -> QPointF:
                length = math.hypot(vector.x(), vector.y())
                return (
                    vector / length
                    if length > 1e-9 else QPointF()
                )

            incoming_unit = unit(incoming_tangent)
            outgoing_unit = unit(outgoing_tangent)
            incoming_chord = unit(anchor - entry)
            outgoing_chord = unit(exit_point - anchor)
            # The chord bisector is the stable tangent through the point.
            # It follows the actual trimmed curves without inheriting a
            # backwards-facing raw handle that would create a loop.
            shared = incoming_chord + outgoing_chord
            if math.hypot(shared.x(), shared.y()) <= 1e-6:
                shared = exit_point - entry
            if math.hypot(shared.x(), shared.y()) <= 1e-6:
                shared = (
                    unit(corner["anchor_incoming_tangent"])
                    + unit(corner["anchor_outgoing_tangent"])
                )
            shared = unit(shared)
            if math.hypot(shared.x(), shared.y()) <= 1e-6:
                shared = unit(anchor - entry)

            incoming_span = math.dist(
                (entry.x(), entry.y()), (anchor.x(), anchor.y())
            )
            outgoing_span = math.dist(
                (anchor.x(), anchor.y()),
                (exit_point.x(), exit_point.y()),
            )
            shared_handle = min(incoming_span, outgoing_span) / 2

            def safe_outer_handle(
                tangent: QPointF, chord: QPointF, span: float,
            ) -> float:
                projection = QPointF.dotProduct(unit(tangent), unit(chord))
                return span / 3 * max(0.0, min(1.0, projection))

            incoming_handle = safe_outer_handle(
                incoming_tangent, anchor - entry, incoming_span
            )
            outgoing_handle = safe_outer_handle(
                outgoing_tangent, exit_point - anchor, outgoing_span
            )

            # Two local Hermite spans meet at the original point. Equal
            # handles along the shared tangent make that join C1 while
            # the outer handles retain the incident cubic tangents.
            path.cubicTo(
                entry + incoming_unit * incoming_handle,
                anchor - shared * shared_handle,
                anchor,
            )
            path.cubicTo(
                anchor + shared * shared_handle,
                exit_point - outgoing_unit * outgoing_handle,
                exit_point,
            )
        elif end_index in vector_curve_rounding:
            corner = vector_curve_rounding[end_index]
            entry = corner["entry"]
            exit_point = corner["exit"]
            anchor = QPointF(end_node.x, end_node.y)

            def unit(vector: QPointF) -> QPointF:
                length = math.hypot(vector.x(), vector.y())
                return vector / length if length > 1e-9 else QPointF()

            incoming_unit = unit(corner["incoming_tangent"])
            outgoing_unit = unit(corner["outgoing_tangent"])
            chord = exit_point - entry
            chord_length = math.hypot(chord.x(), chord.y())
            if math.hypot(incoming_unit.x(), incoming_unit.y()) <= 1e-9:
                incoming_unit = unit(anchor - entry)
            if math.hypot(outgoing_unit.x(), outgoing_unit.y()) <= 1e-9:
                outgoing_unit = unit(exit_point - anchor)
            incoming_span = math.dist(
                (entry.x(), entry.y()), (anchor.x(), anchor.y())
            )
            outgoing_span = math.dist(
                (anchor.x(), anchor.y()),
                (exit_point.x(), exit_point.y()),
            )
            maximum_incoming = min(
                incoming_span * 2 / 3, chord_length * 2 / 3
            )
            maximum_outgoing = min(
                outgoing_span * 2 / 3, chord_length * 2 / 3
            )

            # Intersect the forward incoming tangent with the reverse
            # outgoing tangent. For ordinary vector corners this is the
            # acute-side corner; curved incidents use the same construction
            # while retaining their actual endpoint derivatives.
            reverse_outgoing = QPointF(
                -outgoing_unit.x(), -outgoing_unit.y()
            )
            denominator = (
                incoming_unit.x() * reverse_outgoing.y()
                - incoming_unit.y() * reverse_outgoing.x()
            )
            incoming_length = outgoing_length = -1.0
            if abs(denominator) > 1e-7:
                delta = exit_point - entry
                incoming_ray = (
                    delta.x() * reverse_outgoing.y()
                    - delta.y() * reverse_outgoing.x()
                ) / denominator
                outgoing_ray = (
                    delta.x() * incoming_unit.y()
                    - delta.y() * incoming_unit.x()
                ) / denominator
                if incoming_ray >= 0 and outgoing_ray >= 0:
                    incoming_length = incoming_ray * 2 / 3
                    outgoing_length = outgoing_ray * 2 / 3
            if incoming_length <= 1e-6 or outgoing_length <= 1e-6:
                fallback = chord_length / 3
                incoming_length = min(fallback, incoming_span / 2)
                outgoing_length = min(fallback, outgoing_span / 2)
            incoming_length = max(
                0.0, min(maximum_incoming, incoming_length)
            )
            outgoing_length = max(
                0.0, min(maximum_outgoing, outgoing_length)
            )
            path.cubicTo(
                entry + incoming_unit * incoming_length,
                exit_point - outgoing_unit * outgoing_length,
                exit_point,
            )
        elif rounded[end_index][0] != rounded[end_index][1]:
            path.quadTo(
                QPointF(end_node.x, end_node.y), rounded[end_index][1]
            )

        corners[end_index] = path

    path.moveTo(rounded[0][1])
    for index in range(1, len(nodes)):
        segment(index - 1, index)
    if bound.closed:
        segment(len(nodes) - 1, 0)
    for index in range(segment_count):
        path.connectPath(edge_paths[index])
        path.connectPath(corners[(index + 1) % len(nodes)])
    if bound.closed:
        path.closeSubpath()
    edges = []
    for index in range(segment_count):
        following = (index + 1) % len(nodes)
        before = corners.get(index, QPainterPath())
        after = corners.get(following, QPainterPath())
        edge = path_slice(before, .5, 1)
        edge.connectPath(edge_paths[index])
        edge.connectPath(path_slice(after, 0, .5))
        edges.append(CompiledEdge(edge, index, nodes[index].node_id, nodes[following].node_id))
    return CompiledContour(path, tuple(edges), bound.closed)
