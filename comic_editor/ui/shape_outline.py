"""Batched, source-attributed shape strokes with bounded, style-local caches.

Never boolean-union tessellation pieces: Qt's repeated path rewrites can drop
unrelated contours and are quadratic. Positive winding makes overlapping pieces
one coverage, painted once, with a single final clip to the fill (or open core).
"""
from __future__ import annotations

import math
import sys
from collections import OrderedDict
from dataclasses import dataclass

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF, QTransform

from comic_editor.core.vector_geometry import cubic_derivative, flatten_cubic, rdp_indices
from comic_editor.ui.shape_contours import compile_bound, geometry_key, path_segments


def customized(bound):
    return any(n.outline_multiplier != 1 or not n.outline_enabled
               for c in bound.iter_contours() for n in c.nodes)


def path_key(path):
    return (path.fillRule().value, tuple(
        (e.type.value, e.x, e.y)
        for e in (path.elementAt(i) for i in range(path.elementCount()))))


def _size(value):
    """Conservative accounting including Python keys and Qt path storage."""
    if isinstance(value, QPainterPath):
        return 256 + value.elementCount() * 32
    if isinstance(value, (tuple, list)):
        return sys.getsizeof(value) + sum(_size(v) for v in value)
    if hasattr(value, "__dataclass_fields__"):
        return sys.getsizeof(value) + sum(_size(getattr(value, k))
                                         for k in value.__dataclass_fields__)
    return sys.getsizeof(value)


class OutlineCache:
    def __init__(self, budget=64 * 1024 * 1024):
        self.budget = budget
        self.bytes = 0
        self.entries = OrderedDict()
        self.builds = {}
        self.hits = {}

    def clear(self):
        self.entries.clear()
        self.bytes = 0

    def get(self, key, build):
        cached = self.entries.get(key)
        group = key[0]
        if cached is not None:
            self.entries.move_to_end(key)
            self.hits[group] = self.hits.get(group, 0) + 1
            return cached[0]
        result = build()
        self.builds[group] = self.builds.get(group, 0) + 1
        size = _size(key) + _size(result) + 256
        if size <= self.budget:
            while self.entries and self.bytes + size > self.budget:
                _, (_, old_size) = self.entries.popitem(last=False)
                self.bytes -= old_size
            self.entries[key] = (result, size)
            self.bytes += size
        return result


@dataclass(frozen=True)
class OutlineResult:
    coverage: QPainterPath
    bounds: QRectF
    signature: tuple


def _cached(cache, key, build):
    return cache.get(key, build) if cache is not None else build()


def _area(points):
    return sum(a.x() * b.y() - b.x() * a.y()
               for a, b in zip(points, points[1:] + points[:1]))


def positive_path(path):
    """Orient native stroke exteriors like our strips, including reflections."""
    polygons = [list(p) for p in path.toSubpathPolygons()]
    if polygons and _area(max(polygons, key=lambda p: abs(_area(p)))) < 0:
        return path.toReversed()
    return path


def _polygon(result, points):
    if _area(points) < 0:
        points.reverse()
    result.addPolygon(QPolygonF(points))
    result.closeSubpath()


def native_stroke(path, radius, cap=Qt.RoundCap, tolerance=.125):
    if radius <= 0 or path.isEmpty():
        return QPainterPath()
    if path.elementCount() > 64:
        path = _simplify_line_runs(path, tolerance/4)
    stroker = QPainterPathStroker()
    stroker.setWidth(radius * 2)
    stroker.setCapStyle(cap)
    stroker.setJoinStyle(Qt.RoundJoin)
    stroker.setCurveThreshold(min(.25, tolerance / max(1., radius)))
    return positive_path(stroker.createStroke(path))


def _simplify_line_runs(path, tolerance):
    """Bounded-error reduction of redundant uniform-stroke tessellation only.

    Source anchors, widths, hit testing and cubic segments remain untouched.
    Round stroking is a distance expansion, so the centerline error bound also
    bounds the resulting constant-width stroke error.
    """
    result = QPainterPath()
    result.setFillRule(path.fillRule())
    pending = []
    subpath_start = None

    def flush():
        if not pending:
            return
        if result.isEmpty():
            result.moveTo(pending[0])
        for index in rdp_indices(pending, tolerance)[1:]:
            result.lineTo(pending[index])
        pending.clear()

    i = 0
    while i < path.elementCount():
        e = path.elementAt(i)
        point = QPointF(e.x, e.y)
        if e.isMoveTo():
            flush()
            result.moveTo(point)
            subpath_start = point
            pending.append(point)
        elif e.isLineTo():
            if not pending:
                pending.append(result.currentPosition())
            pending.append(point)
            if point == subpath_start:
                flush()
                result.closeSubpath()
        elif e.isCurveTo():
            flush()
            b, c = path.elementAt(i+1), path.elementAt(i+2)
            result.cubicTo(point, QPointF(b.x, b.y), QPointF(c.x, c.y))
            i += 2
        i += 1
    flush()
    return result


def clip_coverage(coverage, fill, core=None, tolerance=.125):
    # Qt's boolean operator flattens curves in its input coordinate system.
    # Work at output precision, not fixed document precision when zoomed in.
    scale = max(1., .25 / tolerance)
    mapping = QTransform.fromScale(scale, scale)
    # Canonicalize overlapping winding pieces ONCE before Qt's clipper. Its
    # intersection fast path otherwise loses lobes where wide caps overlap
    # their own strips. Incremental unions have the same failure, plus O(n²)
    # work; one batched normalization produces disjoint, stable boundaries.
    coverage = _snap_boolean_vertices(mapping.map(coverage)).simplified()
    result = (coverage.subtracted(_snap_boolean_vertices(mapping.map(core))) if core is not None
              else coverage.intersected(_snap_boolean_vertices(mapping.map(fill))))
    return QTransform.fromScale(1/scale, 1/scale).map(result)


def _snap_boolean_vertices(path):
    # Trigonometric arc endpoints and analytically derived offsets may differ
    # by 1e-14 at a shared join. Qt can misclassify that nearly-zero edge and
    # discard a whole lobe. Use a binary-exact grid, not decimal rounding:
    # decimal round-trip drift can itself reintroduce the near-zero edges.
    # The grid is far below one output pixel at our scaled clip precision.
    result = QPainterPath(path)
    for i in range(result.elementCount()):
        e = result.elementAt(i)
        result.setElementPositionAt(i, round(e.x*65536)/65536, round(e.y*65536)/65536)
    return result


def _samples(path, tolerance, radius, *, with_normals=False):
    points = []
    normals = []
    for segment in path_segments(path):
        # Offset error also depends on tangent variation, especially for wide
        # strokes. Tighten centerline flatness relative to the stroke radius.
        chord = math.dist(segment.cubic[0], segment.cubic[-1])
        error = tolerance * min(1., max(.01, chord / max(radius * 8, 1.)))
        for sample in flatten_cubic(segment.cubic, error)[bool(points):]:
            points.append(QPointF(*sample.point))
            tangent = QPointF(*cubic_derivative(segment.cubic, sample.t))
            if math.hypot(tangent.x(), tangent.y()) <= 1e-9:
                candidates = (segment.cubic[1:] if sample.t < .5
                              else reversed(segment.cubic[:-1]))
                for candidate in candidates:
                    tangent = (QPointF(*candidate)-points[-1] if sample.t < .5
                               else points[-1]-QPointF(*candidate))
                    if math.hypot(tangent.x(), tangent.y()) > 1e-9:
                        break
            length = max(1e-9, math.hypot(tangent.x(), tangent.y()))
            normals.append(QPointF(-tangent.y()/length, tangent.x()/length))
    filtered = []
    filtered_normals = []
    for point, normal in zip(points, normals):
        if not filtered or math.hypot((point-filtered[-1]).x(),
                                     (point-filtered[-1]).y()) > 1e-9:
            filtered.append(point)
            filtered_normals.append(normal)
    return (filtered, filtered_normals) if with_normals else filtered


def _cap(result, point, radius, tangent, kind):
    if radius <= 0 or kind == "butt":
        return
    normal = QPointF(-tangent.y(), tangent.x())
    if kind == "square":
        _polygon(result, [point + normal * radius,
                          point + (normal + tangent) * radius,
                          point + (tangent - normal) * radius,
                          point - normal * radius])
        return
    if kind == "round":
        cap = QPainterPath()
        cap.addEllipse(point, radius, radius)
        result.addPath(positive_path(cap))
    else:
        _polygon(result, [point + normal * radius,
                          point + tangent * radius, point - normal * radius])


def edge_stroke(path, first, last, *, tolerance=.125,
                start_cap="round", end_cap="round", cache=None, core_delta=0.):
    """An edge's width varies by arc distance, never by raw cubic t."""
    key = ("edge", path_key(path), first, last, tolerance, start_cap, end_cap, core_delta)

    def build():
        if first == last and not core_delta:
            if start_cap == end_cap == "round":
                return native_stroke(path, first, tolerance=tolerance)
            result = native_stroke(path, first, Qt.FlatCap, tolerance=tolerance)
            points = _samples(path, tolerance, first)
            if len(points) > 1:
                a, b = points[0]-points[1], points[-1]-points[-2]
                a /= math.hypot(a.x(), a.y())
                b /= math.hypot(b.x(), b.y())
                _cap(result, points[0], first, a, start_cap)
                _cap(result, points[-1], last, b, end_cap)
            return result
        result = QPainterPath()
        result.setFillRule(Qt.WindingFill)
        if max(first, last) <= 0:
            return result
        points, offsets = _samples(path, tolerance, max(first, last), with_normals=True)
        if len(points) < 2:
            return result
        lengths = [math.hypot((b-a).x(), (b-a).y())
                   for a, b in zip(points, points[1:])]
        total = sum(lengths)
        if core_delta:
            # Preserve the open core's eased width interpolation. Subdivide
            # even straight segments when the width profile itself curves.
            step = total / max(1., math.sqrt(6 * abs(core_delta) / (8*tolerance)))
            refined = [points[0]]
            refined_offsets = [offsets[0]]
            for a, b, length, na, nb in zip(points, points[1:], lengths, offsets, offsets[1:]):
                count = max(1, math.ceil(length / step))
                refined.extend(a+(b-a)*(i/count) for i in range(1, count+1))
                for i in range(1, count+1):
                    normal = na+(nb-na)*(i/count)
                    refined_offsets.append(normal / max(1e-9, math.hypot(normal.x(), normal.y())))
            points = refined
            offsets = refined_offsets
            lengths = [math.hypot((b-a).x(), (b-a).y())
                       for a, b in zip(points, points[1:])]
        distance = 0.
        widths = [first]
        for length in lengths:
            distance += length
            fraction = distance / total
            smooth = fraction*fraction*(3-2*fraction)
            widths.append(first + (last - first)*fraction + core_delta*(smooth-fraction))
        tangents = [(b-a) / length for a, b, length
                    in zip(points, points[1:], lengths)]
        # Continuous offsets: no circles at tessellation samples. Real source
        # corners and exposed endpoints get round joins/caps below.
        for i in range(len(lengths)):
            a, b = points[i], points[i+1]
            na, nb = offsets[i] * widths[i], offsets[i+1] * widths[i+1]
            _polygon(result, [a + na, b + nb, b - nb, a - na])
        _cap(result, points[0], first, -tangents[0], start_cap)
        _cap(result, points[-1], last, tangents[-1], end_cap)
        return result

    return _cached(cache, key, build)


def _endpoint_tangent(path, start):
    segments = path_segments(path)
    cubic = segments[0 if start else -1].cubic
    endpoint = QPointF(*cubic[0 if start else -1])
    for candidate in (cubic[1:] if start else reversed(cubic[:-1])):
        tangent = QPointF(*candidate)-endpoint if start else endpoint-QPointF(*candidate)
        length = math.hypot(tangent.x(), tangent.y())
        if length > 1e-9:
            return tangent/length
    return QPointF(1, 0)


def round_join(point, radius, incoming, outgoing):
    """Only the outer sector: a join must not bulge into a tapered segment."""
    cross = incoming.x()*outgoing.y()-incoming.y()*outgoing.x()
    angle = math.atan2(cross, QPointF.dotProduct(incoming, outgoing))
    if abs(angle) < 1e-8 or radius <= 0:
        return QPainterPath()
    side = -1 if angle > 0 else 1
    normal = QPointF(-incoming.y(), incoming.x())*side
    result = QPainterPath(point)
    result.lineTo(point+normal*radius)
    result.arcTo(QRectF(point.x()-radius, point.y()-radius, 2*radius, 2*radius),
                 math.degrees(math.atan2(-normal.y(), normal.x())), -math.degrees(angle))
    result.closeSubpath()
    return positive_path(result)


def _contour_strokes(contour, source, baseline, core_width, *,
                     outline, start_cap, end_cap, tolerance, cache):
    """Batch connected constant-width runs; retain variable edges separately.

    A 1000-anchor uniform contour is ONE native stroke, not 1000 overlapping
    capsules. Editing one width changes only its incident variable edges.
    """
    items = []
    for edge in contour.edges:
        if edge.path.isEmpty():
            items.append(None)
            continue
        first, last = source.nodes[edge.index], source.nodes[(edge.index+1) % len(source.nodes)]
        if outline and (not first.outline_enabled or
                        max(first.outline_multiplier, last.outline_multiplier) <= 0):
            items.append(None)
            continue
        a = baseline * first.outline_multiplier if outline else baseline
        b = baseline * last.outline_multiplier if outline else baseline
        a += core_width * first.width_multiplier / 2
        b += core_width * last.width_multiplier / 2
        delta = core_width * (last.width_multiplier-first.width_multiplier)/2
        items.append((edge.path, a, b, delta,
                      start_cap if not contour.closed and edge.index == 0 else "round",
                      end_cap if not contour.closed and edge.index == len(contour.edges)-1 else "round"))
    joins = QPainterPath()
    joins.setFillRule(Qt.WindingFill)
    for i, item in enumerate(items):
        if item is None:
            continue
        previous = items[(i-1) % len(items)] if contour.closed or i > 0 else None
        following = items[(i+1) % len(items)] if contour.closed or i+1 < len(items) else None
        path, a, b, delta, cap_a, cap_b = item
        if previous is not None:
            cap_a = "butt"
            # Native constant runs already supply their internal joins.
            if not (previous[1] == previous[2] == a == b and not previous[3] and not delta):
                joins.addPath(round_join(path.pointAtPercent(0), a,
                              _endpoint_tangent(previous[0], False),
                              _endpoint_tangent(path, True)))
        if following is not None:
            cap_b = "butt"
        items[i] = (path, a, b, delta, cap_a, cap_b)
    if contour.closed and items:
        # Start after a break or at a variable edge so the uniform run across
        # the contour's stored first node stays connected.
        pivot = next((i+1 for i, item in enumerate(items) if item is None), None)
        if pivot is None:
            pivot = next((i for i, item in enumerate(items)
                          if item[1] != item[2] or item[3]), 0)
        items = items[pivot:] + items[:pivot]
    groups = []
    current = None
    for item in items:
        if item is None:
            current = None
            continue
        path, a, b, delta, cap_a, cap_b = item
        if (current is not None and not delta and not current[3]
                and a == b == current[1] == current[2]):
            current[0].connectPath(path)
            current[5] = cap_b
        else:
            current = [QPainterPath(path), a, b, delta, cap_a, cap_b]
            groups.append(current)
    if (contour.closed and len(groups) == 1 and all(item is not None for item in items)
            and groups[0][1] == groups[0][2] and not groups[0][3]):
        groups[0][0].closeSubpath()
    result = QPainterPath(joins)
    result.setFillRule(Qt.WindingFill)
    for path, a, b, delta, cap_a, cap_b in groups:
        result.addPath(edge_stroke(path, a, b, tolerance=tolerance, cache=cache,
                                  start_cap=cap_a, end_cap=cap_b, core_delta=delta))
    return result


def outline_result(bound, baseline, fill_path, *, core=None, base_width=0,
                   clip=True, cache=None, tolerance=.125,
                   start_cap="round", end_cap="round"):
    geometry = geometry_key(bound)
    styles = tuple(tuple((n.outline_multiplier, n.outline_enabled,
                          n.width_multiplier if core is not None else 0)
                         for n in c.nodes) for c in bound.iter_contours())
    key = ("outline", geometry, styles, baseline, base_width, clip, tolerance,
           start_cap, end_cap, path_key(core) if core is not None else path_key(fill_path))

    def build():
        result = QPainterPath()
        result.setFillRule(Qt.WindingFill)
        if baseline <= 0:
            return OutlineResult(result, result.boundingRect(), key)
        compiled = _cached(cache, ("contours", geometry), lambda: compile_bound(bound))
        for contour, source in zip(compiled, bound.iter_contours()):
            if not contour.edges:
                result.addPath(native_stroke(contour.path, baseline, tolerance=tolerance))
                continue
            result.addPath(_contour_strokes(
                contour, source, baseline, base_width if core is not None else 0.,
                outline=True, start_cap=start_cap, end_cap=end_cap,
                tolerance=tolerance, cache=cache))
        if clip:
            result = clip_coverage(result, fill_path, core, tolerance)
        return OutlineResult(result, result.boundingRect(), key)

    return _cached(cache, key, build)


def outline_mesh(bound, baseline, fill_path, **kwargs):
    return outline_result(bound, baseline, fill_path, **kwargs).coverage


def core_mesh(bound, base_width, extra_width=0., start_cap="round",
              end_cap="round", *, cache=None, tolerance=.125):
    """Open-path core/mask ribbons share the same attributed centerline."""
    key = ("core", geometry_key(bound), base_width, extra_width, start_cap,
           end_cap, tolerance, tuple(tuple(n.width_multiplier for n in c.nodes)
                                    for c in bound.iter_contours()))
    def build():
        result = QPainterPath()
        result.setFillRule(Qt.WindingFill)
        if base_width <= 0 and extra_width <= 0:
            return result
        compiled = _cached(cache, ("contours", geometry_key(bound)), lambda: compile_bound(bound))
        for contour, source in zip(compiled, bound.iter_contours()):
            result.addPath(_contour_strokes(
                contour, source, extra_width/2, base_width,
                outline=False, start_cap=start_cap, end_cap=end_cap,
                tolerance=tolerance, cache=cache))
        # One coverage for translucency, masks and subsequent outline subtraction.
        scale = max(1., .25/tolerance)
        return QTransform.fromScale(1/scale, 1/scale).map(
            _snap_boolean_vertices(QTransform.fromScale(scale, scale).map(result)).simplified())
    return _cached(cache, key, build)


def remove_nodes(contour, identifiers):
    nodes = contour.nodes
    for i, node in enumerate(nodes):
        if node.node_id in identifiers:
            continue
        enabled = node.outline_enabled
        j = i + 1
        while j < i + len(nodes):
            if not contour.closed and j >= len(nodes):
                break
            following = nodes[j % len(nodes)]
            if following.node_id not in identifiers:
                break
            enabled = enabled and following.outline_enabled
            j += 1
        node.outline_enabled = enabled
    nodes[:] = [n for n in nodes if n.node_id not in identifiers]
