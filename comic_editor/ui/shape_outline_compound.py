"""Style-independent attribution of compound boundary spans.

Split the final boundary at source segment endpoints/intersections, then match
collinear coverage at adaptive geometric precision. This avoids painting broad
source-edge envelopes over unrelated nearby boundaries.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QTransform

from comic_editor.core.models import BoundGeometry, PathContour, PathNode
from comic_editor.core.vector_geometry import flatten_cubic
from comic_editor.ui.shape_contours import (
    compile_bound, geometry_key, path_segments, path_slice, transform_stretch,
)
from comic_editor.ui.shape_outline import (
    _cached, _endpoint_tangent, _samples, clip_coverage, core_mesh, edge_stroke, native_stroke,
    path_key, positive_path, round_join,
)


@dataclass(frozen=True)
class OutlineSource:
    bound: object
    baseline: float
    mapping: QTransform
    # Transient ribbon surfaces may have a width discontinuity at an
    # intersection. Keep both endpoint widths per outgoing boundary span.
    edge_styles: tuple = ()
    owner_id: str = ""

    def style(self, contour, edge):
        if self.edge_styles:
            return self.edge_styles[contour][edge]
        nodes = list(self.bound.iter_contours())[contour].nodes
        first, last = nodes[edge], nodes[(edge+1) % len(nodes)]
        return first.outline_multiplier, last.outline_multiplier, first.outline_enabled


@dataclass(frozen=True)
class BoundarySpan:
    source: int
    contour: int
    edge: int
    start: float
    end: float


def transform_key(t):
    return (t.m11(), t.m12(), t.m13(), t.m21(), t.m22(), t.m23(),
            t.m31(), t.m32(), t.m33())


def _distance(a, b):
    return math.hypot((a-b).x(), (a-b).y())


def _project(point, a, b):
    direction = b-a
    square = QPointF.dotProduct(direction, direction)
    amount = QPointF.dotProduct(point-a, direction) / square if square > 1e-18 else 0.
    return max(0., min(1., amount))


def _lines(path, tolerance):
    for segment in path_segments(path):
        points = [QPointF(*p.point) for p in flatten_cubic(segment.cubic, tolerance)]
        for a, b in zip(points, points[1:]):
            if _distance(a, b) > 1e-9:
                yield a, b


def attribute_boundary(path, sources, compiled, tolerance):
    records = []
    for si, (source, contours) in enumerate(zip(sources, compiled)):
        for ci, contour in enumerate(contours):
            for edge in contour.edges:
                precision = tolerance / max(1., transform_stretch(source.mapping, edge.path.controlPointRect()))
                local_lines = list(_lines(edge.path, precision / 4))
                lengths = [_distance(a, b) for a, b in local_lines]
                total, offset = sum(lengths), 0.
                if total <= 1e-9:
                    continue
                for (a, b), length in zip(local_lines, lengths):
                    records.append((source.mapping.map(a), source.mapping.map(b),
                                    si, ci, edge.index, offset/total, (offset+length)/total))
                    offset += length
    spans, fallback = [], QPainterPath()
    for first, last, match in _boundary_sections(path, records, tolerance):
        if match is None:
            if fallback.isEmpty() or fallback.currentPosition() != first:
                fallback.moveTo(first)
            fallback.lineTo(last)
            continue
        si, ci, ei, x, y = match
        spans.append(BoundarySpan(si, ci, ei, min(x, y), max(x, y)))
    # Rejoin adjacent pieces before stroking: sampled curve points are not caps.
    groups = {}
    for span in spans:
        groups.setdefault((span.source, span.contour, span.edge), []).append((span.start, span.end))
    merged = []
    for key, intervals in groups.items():
        current = None
        for start, end in sorted(intervals):
            if current is not None and start <= current[1] + 1e-6:
                current = (current[0], max(current[1], end))
            else:
                if current is not None:
                    merged.append(BoundarySpan(*key, *current))
                current = (start, end)
        if current is not None:
            merged.append(BoundarySpan(*key, *current))
    return tuple(merged), fallback


def _boundary_sections(path, records, tolerance):
    """Yield ordered boundary intervals and their originating span, if any."""
    # Spatial buckets keep cold attribution local even for large compounds.
    extent = path.boundingRect()
    cell = max(8., max(extent.width(), extent.height()) / max(1., math.sqrt(len(records))))
    buckets = {}
    epsilon = max(1e-6, tolerance * 2)

    def cells(a, b):
        for x in range(math.floor((min(a.x(), b.x())-epsilon)/cell),
                       math.floor((max(a.x(), b.x())+epsilon)/cell)+1):
            for y in range(math.floor((min(a.y(), b.y())-epsilon)/cell),
                           math.floor((max(a.y(), b.y())+epsilon)/cell)+1):
                yield x, y

    for index, record in enumerate(records):
        for cell_id in cells(*record[:2]):
            buckets.setdefault(cell_id, []).append(index)
    for a, b in _lines(path, tolerance / 4):
        candidates = sorted({i for cell_id in cells(a, b) for i in buckets.get(cell_id, ())})
        direction = b-a
        square = QPointF.dotProduct(direction, direction)
        cuts = {0., 1.}
        overlaps = []
        for index in candidates:
            p, q = records[index][:2]
            start = QPointF.dotProduct(p-a, direction)/square
            end = QPointF.dotProduct(q-a, direction)/square
            lo, hi = max(0., min(start, end)), min(1., max(start, end))
            if hi-lo <= 1e-9:
                continue
            mid = a + direction * ((lo+hi)/2)
            projection = p + (q-p) * _project(mid, p, q)
            if _distance(mid, projection) <= epsilon:
                cuts.update((lo, hi))
                overlaps.append(index)
        cuts = sorted(cuts)
        for start, end in zip(cuts, cuts[1:]):
            if end-start < 1e-9:
                continue
            first, last = a+direction*start, a+direction*end
            mid = (first+last)/2
            match, best = None, epsilon
            for index in overlaps:
                p, q = records[index][:2]
                distance = _distance(mid, p+(q-p)*_project(mid, p, q))
                if distance < best:
                    match, best = index, distance
            if match is None:
                yield first, last, None
                continue
            p, q, si, ci, ei, f0, f1 = records[match]
            x = f0+(f1-f0)*_project(first, p, q)
            y = f0+(f1-f0)*_project(last, p, q)
            yield first, last, (si, ci, ei, x, y)


def ribbon_source(bound, baseline, mapping, base_width, start_cap, end_cap, cache=None):
    """Attribute an open core's *surface*, not its distant centerline.

    Match the normalized ribbon to its actual edge strips and corner sectors
    once. The cached template contains only geometry and source arc weights;
    outline styles can then change without rebuilding the ribbon or ownership.
    """
    geometry = geometry_key(bound)
    widths = tuple(tuple(n.width_multiplier for n in c.nodes) for c in bound.iter_contours())
    key = ("ribbon_surface", geometry, widths, base_width, start_cap, end_cap)

    def build():
        precision = .125  # The compound operand's core mesh precision.
        coverage = core_mesh(bound, base_width, start_cap=start_cap, end_cap=end_cap,
                             cache=cache, tolerance=precision)
        compiled = _cached(cache, ("contours", geometry), lambda: compile_bound(bound))
        records = []
        for ci, (contour, source) in enumerate(zip(compiled, bound.iter_contours())):
            for ei, edge in enumerate(contour.edges):
                if edge.path.isEmpty():
                    continue
                first, last = source.nodes[ei], source.nodes[(ei+1) % len(source.nodes)]
                a, b = base_width*first.width_multiplier/2, base_width*last.width_multiplier/2
                samples = _samples(edge.path, precision/4, max(a, b))
                lengths = [_distance(p, q) for p, q in zip(samples, samples[1:])]
                total, offset, center = sum(lengths), 0., []
                for p, q, length in zip(samples, samples[1:], lengths):
                    center.append((p, q, offset, length))
                    offset += length

                def fraction(point):
                    if total <= 1e-9:
                        return 0.
                    best, value = float("inf"), 0.
                    for p, q, offset, length in center:
                        t = _project(point, p, q)
                        distance = _distance(point, p+(q-p)*t)
                        if distance < best:
                            best, value = distance, (offset+length*t)/total
                    return value

                piece = edge_stroke(
                    edge.path, a, b, core_delta=b-a, cache=cache, tolerance=precision,
                    start_cap=start_cap if not contour.closed and ei == 0 else "butt",
                    end_cap=end_cap if not contour.closed and ei == len(contour.edges)-1 else "butt")
                for p, q in _lines(piece, precision/4):
                    records.append((p, q, 0, ci, ei, fraction(p), fraction(q)))
                if contour.closed or ei > 0:
                    previous = contour.edges[(ei-1) % len(contour.edges)]
                    if previous.path.isEmpty():
                        continue
                    incoming = _endpoint_tangent(previous.path, False)
                    outgoing = _endpoint_tangent(edge.path, True)
                    angle = math.atan2(incoming.x()*outgoing.y()-incoming.y()*outgoing.x(),
                                       QPointF.dotProduct(incoming, outgoing))/2
                    middle = QPointF(incoming.x()*math.cos(angle)-incoming.y()*math.sin(angle),
                                     incoming.x()*math.sin(angle)+incoming.y()*math.cos(angle))
                    for owner, f, begin, end in (
                        ((ei-1) % len(contour.edges), 1., incoming, middle),
                        (ei, 0., middle, outgoing),
                    ):
                        join = round_join(edge.path.pointAtPercent(0), a, begin, end)
                        for p, q in _lines(join, precision/4):
                            records.append((p, q, 0, ci, owner, f, f))
        contours, owners, nodes, spans = [], [], [], []
        for first, last, match in _boundary_sections(coverage, records, precision):
            nodes.append(PathNode(node_id=f"{bound.nodes[0].node_id}:surface:{len(contours)}:{len(nodes)}",
                                  x=first.x(), y=first.y()))
            spans.append(match)
            if _distance(last, QPointF(*nodes[0].position)) <= 1e-7:
                if len(nodes) >= 3:
                    contours.append(PathContour(nodes=nodes, closed=True))
                    owners.append(tuple(spans))
                nodes, spans = [], []
        if not contours:
            return None, ()
        surface = BoundGeometry(nodes=contours[0].nodes, closed=True, primitive="custom",
                                additional_contours=contours[1:])
        return surface, tuple(owners)

    surface, owners = _cached(cache, key, build)
    if surface is None:
        return None
    contours = list(bound.iter_contours())
    styles = []
    for spans in owners:
        edges = []
        for owner in spans:
            if owner is None:
                edges.append((1., 1., True))
                continue
            _, ci, ei, f0, f1 = owner
            nodes = contours[ci].nodes
            first, last = nodes[ei], nodes[(ei+1) % len(nodes)]
            delta = last.outline_multiplier-first.outline_multiplier
            edges.append((first.outline_multiplier+delta*f0,
                          first.outline_multiplier+delta*f1, first.outline_enabled))
        styles.append(tuple(edges))
    return OutlineSource(surface, baseline, mapping, tuple(styles))


def compound_outline(path, baseline, sources, cache=None, tolerance=.125, *, source_indices=None):
    geometry = tuple((geometry_key(s.bound), transform_key(s.mapping)) for s in sources)
    attribution_key = ("attribution", path_key(path), geometry, tolerance)
    styles = tuple((s.baseline, s.edge_styles or tuple(tuple((n.outline_multiplier, n.outline_enabled)
                                          for n in c.nodes) for c in s.bound.iter_contours()))
                   for s in sources)
    key = ("compound_outline", attribution_key, baseline, styles, source_indices)

    def build():
        compiled = [_cached(cache, ("contours", geometry_key(s.bound)),
                            lambda s=s: compile_bound(s.bound)) for s in sources]
        spans, fallback = _cached(cache, attribution_key,
                                 lambda: attribute_boundary(path, sources, compiled, tolerance))
        result = QPainterPath()
        result.setFillRule(Qt.WindingFill)
        if source_indices is None or -1 in source_indices:
            result.addPath(native_stroke(fallback, baseline, tolerance=tolerance))
        active = {}
        for span in spans:
            if source_indices is not None and span.source not in source_indices:
                continue
            source = sources[span.source]
            a, b, enabled = source.style(span.contour, span.edge)
            if enabled and max(a, b) > 0 and source.baseline > 0:
                active.setdefault((span.source, span.contour, span.edge), []).append(span)
        for span in spans:
            if source_indices is not None and span.source not in source_indices:
                continue
            source = sources[span.source]
            contour = list(source.bound.iter_contours())[span.contour]
            first, last, enabled = source.style(span.contour, span.edge)
            if not enabled or max(first, last) <= 0 or source.baseline <= 0:
                continue
            edge = compiled[span.source][span.contour].edges[span.edge]
            section = _cached(cache, ("boundary_section", path_key(edge.path), span.start, span.end),
                              lambda: path_slice(edge.path, span.start, span.end))
            a = first + (last-first)*span.start
            b = first + (last-first)*span.end
            count = len(compiled[span.source][span.contour].edges)
            previous = next((candidate for candidate in active.get(
                (span.source, span.contour, (span.edge-1) % count), ())
                if candidate.end >= 1-1e-6), None) if contour.closed or span.edge else None
            following = next((candidate for candidate in active.get(
                (span.source, span.contour, (span.edge+1) % count), ())
                if candidate.start <= 1e-6), None) if contour.closed or span.edge+1 < count else None
            connected_start = span.start <= 1e-6 and previous is not None
            connected_end = span.end >= 1-1e-6 and following is not None
            mesh = edge_stroke(section, source.baseline*a, source.baseline*b,
                               tolerance=tolerance / max(1., transform_stretch(
                                   source.mapping, section.controlPointRect())), cache=cache,
                               start_cap="butt" if connected_start else "round",
                               end_cap="butt" if connected_end else "round")
            result.addPath(positive_path(source.mapping.map(mesh)))
            if connected_start:
                previous_edge = compiled[span.source][span.contour].edges[previous.edge].path
                if not previous_edge.isEmpty() and not section.isEmpty():
                    join = round_join(section.pointAtPercent(0), source.baseline*a,
                                      _endpoint_tangent(previous_edge, False),
                                      _endpoint_tangent(section, True))
                    result.addPath(positive_path(source.mapping.map(join)))
        return clip_coverage(result, path, tolerance=tolerance)

    return _cached(cache, key, build)
