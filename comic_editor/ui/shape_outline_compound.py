"""Style-independent attribution of compound boundary spans.

Split the final boundary at source segment endpoints/intersections, then match
collinear coverage at adaptive geometric precision. This avoids painting broad
source-edge envelopes over unrelated nearby boundaries.
"""
from __future__ import annotations

import math
import hashlib
import pickle
from dataclasses import dataclass
import numpy as np

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QTransform

from comic_editor.core.models import BoundGeometry, PathContour, PathNode
from comic_editor.core.vector_geometry import flatten_cubic
from comic_editor.ui.shape_contours import (
    compile_bound, geometry_key, path_slice, transform_stretch,
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


def _cache_fingerprint(value):
    # These signatures contain only primitive model/path values. Keep the
    # complete content identity without retaining and recursively accounting
    # for thousands of repeated geometry/style tuples in every cache entry.
    return hashlib.sha256(pickle.dumps(value, protocol=5)).digest()


def _distance(a, b):
    return math.hypot((a-b).x(), (a-b).y())


def _project(point, a, b):
    direction = b-a
    square = QPointF.dotProduct(direction, direction)
    amount = QPointF.dotProduct(point-a, direction) / square if square > 1e-18 else 0.
    return max(0., min(1., amount))


def _lines(path, tolerance):
    # Dense deformed contours contain line segments already. Keep their exact
    # endpoints instead of manufacturing a cubic and flattening it again.
    current = QPointF()
    index = 0
    while index < path.elementCount():
        element = path.elementAt(index)
        target = QPointF(element.x, element.y)
        if element.isLineTo():
            if _distance(current, target) > 1e-9:
                yield current, target
        elif element.isCurveTo():
            second, last = path.elementAt(index+1), path.elementAt(index+2)
            cubic = (current.toTuple(), target.toTuple(), (second.x, second.y), (last.x, last.y))
            points = [QPointF(*p.point) for p in flatten_cubic(cubic, tolerance)]
            for a, b in zip(points, points[1:]):
                if _distance(a, b) > 1e-9:
                    yield a, b
            target = QPointF(last.x, last.y)
            index += 2
        current = target
        index += 1


def _source_boundary_records(source, contours, tolerance, cache):
    affine = source.mapping.isAffine()
    precision = tolerance / max(1., transform_stretch(source.mapping)) if affine else None
    key = ("attribution_source", _cache_fingerprint(geometry_key(source.bound)), precision,
           None if affine else (tolerance, transform_key(source.mapping)))

    def build():
        records = []
        for ci, contour in enumerate(contours):
            for edge in contour.edges:
                local_precision = precision if affine else tolerance / max(
                    1., transform_stretch(source.mapping, edge.path.controlPointRect()))
                local_lines = list(_lines(edge.path, local_precision / 4))
                lengths = [_distance(a, b) for a, b in local_lines]
                total, offset = sum(lengths), 0.
                if total <= 1e-9:
                    continue
                for (a, b), length in zip(local_lines, lengths):
                    records.append((a, b, ci, edge.index, offset/total, (offset+length)/total))
                    offset += length
        return tuple(records)

    return _cached(cache, key, build)


def attribute_boundary(path, sources, compiled, tolerance, cache=None):
    records = []
    for si, (source, contours) in enumerate(zip(sources, compiled)):
        for a, b, ci, edge, start, end in _source_boundary_records(source, contours, tolerance, cache):
            records.append((source.mapping.map(a), source.mapping.map(b), si, ci, edge, start, end))
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

    def cells(ax, ay, bx, by):
        for x in range(math.floor((min(ax, bx)-epsilon)/cell),
                       math.floor((max(ax, bx)+epsilon)/cell)+1):
            for y in range(math.floor((min(ay, by)-epsilon)/cell),
                           math.floor((max(ay, by)+epsilon)/cell)+1):
                yield x, y

    coordinates = []
    for index, record in enumerate(records):
        p, q = record[:2]
        px, py, qx, qy = p.x(), p.y(), q.x(), q.y()
        rx, ry = qx-px, qy-py
        coordinates.append((px, py, qx, qy, rx, ry, rx*rx+ry*ry))
        for cell_id in cells(px, py, qx, qy):
            buckets.setdefault(cell_id, []).append(index)
    for a, b in _lines(path, tolerance / 4):
        ax, ay, bx, by = a.x(), a.y(), b.x(), b.y()
        candidates = sorted({i for cell_id in cells(ax, ay, bx, by) for i in buckets.get(cell_id, ())})
        dx, dy = bx-ax, by-ay
        square = dx*dx+dy*dy
        cuts = {0., 1.}
        overlaps = []
        for index in candidates:
            px, py, qx, qy, rx, ry, r_square = coordinates[index]
            start = ((px-ax)*dx+(py-ay)*dy)/square
            end = ((qx-ax)*dx+(qy-ay)*dy)/square
            lo, hi = max(0., min(start, end)), min(1., max(start, end))
            if hi-lo <= 1e-9:
                continue
            amount = (lo+hi)/2
            mx, my = ax+dx*amount, ay+dy*amount
            t = max(0., min(1., ((mx-px)*rx+(my-py)*ry)/r_square)) if r_square > 1e-18 else 0.
            if math.hypot(mx-(px+rx*t), my-(py+ry*t)) <= epsilon:
                cuts.update((lo, hi))
                overlaps.append(index)
        cuts = sorted(cuts)
        for start, end in zip(cuts, cuts[1:]):
            if end-start < 1e-9:
                continue
            fx, fy, lx, ly = ax+dx*start, ay+dy*start, ax+dx*end, ay+dy*end
            mx, my = (fx+lx)/2, (fy+ly)/2
            match, best = None, epsilon
            for index in overlaps:
                px, py, _, _, rx, ry, r_square = coordinates[index]
                t = max(0., min(1., ((mx-px)*rx+(my-py)*ry)/r_square)) if r_square > 1e-18 else 0.
                distance = math.hypot(mx-(px+rx*t), my-(py+ry*t))
                if distance < best:
                    match, best = index, distance
            first, last = QPointF(fx, fy), QPointF(lx, ly)
            if match is None:
                yield first, last, None
                continue
            _, _, si, ci, ei, f0, f1 = records[match]
            px, py, _, _, rx, ry, r_square = coordinates[match]
            x = f0+(f1-f0)*(max(0., min(1., ((fx-px)*rx+(fy-py)*ry)/r_square)) if r_square > 1e-18 else 0.)
            y = f0+(f1-f0)*(max(0., min(1., ((lx-px)*rx+(ly-py)*ry)/r_square)) if r_square > 1e-18 else 0.)
            yield first, last, (si, ci, ei, x, y)


def _centerline_fractions(points, center, total):
    """Project ribbon vertices onto sampled source edges in bounded batches."""
    if not points or not center or total <= 1e-9:
        return np.zeros(len(points))
    starts = np.asarray([p.toTuple() for p, _, _, _ in center])
    directions = np.asarray([q.toTuple() for _, q, _, _ in center])-starts
    squares = np.sum(directions*directions, axis=1)
    offsets = np.asarray([offset for _, _, offset, _ in center])
    lengths = np.asarray([length for _, _, _, length in center])
    values = np.asarray([point.toTuple() for point in points])
    # The input surface contains both endpoints of each line. Repeated
    # vertices share their projection without changing first-match tie order.
    unique, inverse = np.unique(values, axis=0, return_inverse=True)
    projected = np.empty(len(unique))
    batch_size = max(1, min(256, 65536 // len(center)))
    for first in range(0, len(unique), batch_size):
        batch = unique[first:first+batch_size, None, :]-starts
        numerator = np.sum(batch*directions, axis=2)
        fractions = np.clip(np.divide(numerator, squares,
            out=np.zeros_like(numerator), where=squares > 1e-18), 0., 1.)
        distance = unique[first:first+len(batch), None, :]-(starts+directions*fractions[..., None])
        nearest = np.argmin(np.hypot(distance[..., 0], distance[..., 1]), axis=1)
        selected = fractions[np.arange(len(batch)), nearest]
        projected[first:first+len(batch)] = (offsets[nearest]+lengths[nearest]*selected)/total
    return projected[inverse]


def ribbon_source(bound, baseline, mapping, base_width, start_cap, end_cap, cache=None):
    """Attribute an open core's *surface*, not its distant centerline.

    Match the normalized ribbon to its actual edge strips and corner sectors
    once. The cached template contains only geometry and source arc weights;
    outline styles can then change without rebuilding the ribbon or ownership.
    """
    geometry = geometry_key(bound)
    widths = tuple(tuple(n.width_multiplier for n in c.nodes) for c in bound.iter_contours())
    key = ("ribbon_surface", _cache_fingerprint((geometry, widths)), base_width, start_cap, end_cap)

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

                piece = edge_stroke(
                    edge.path, a, b, core_delta=b-a, cache=cache, tolerance=precision,
                    start_cap=start_cap if not contour.closed and ei == 0 else "butt",
                    end_cap=end_cap if not contour.closed and ei == len(contour.edges)-1 else "butt")
                lines = list(_lines(piece, precision/4))
                fractions = _centerline_fractions([point for line in lines for point in line], center, total)
                for index, (p, q) in enumerate(lines):
                    records.append((p, q, 0, ci, ei, fractions[index*2], fractions[index*2+1]))
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


def _uniform_compound_outline(baseline, sources, source_indices):
    """A complete boundary with one mapped radius needs no owner attribution."""
    if source_indices is not None and not set(range(-1, len(sources))).issubset(source_indices):
        return False
    for source in sources:
        mapping = source.mapping
        if not mapping.isAffine():
            return False
        x2 = mapping.m11()**2+mapping.m12()**2
        y2 = mapping.m21()**2+mapping.m22()**2
        dot = mapping.m11()*mapping.m21()+mapping.m12()*mapping.m22()
        if not math.isclose(x2, y2, rel_tol=1e-12, abs_tol=1e-12) or abs(dot) > 1e-12*max(1., x2, y2):
            return False
        radius = source.baseline*math.sqrt(max(0., x2))
        styles = (style for contour in source.edge_styles for style in contour) if source.edge_styles else (
            (node.outline_multiplier, node.outline_multiplier, node.outline_enabled)
            for contour in source.bound.iter_contours() for node in contour.nodes)
        if any(not enabled or not math.isclose(radius*a, baseline, rel_tol=1e-12, abs_tol=1e-12)
               or not math.isclose(radius*b, baseline, rel_tol=1e-12, abs_tol=1e-12)
               for a, b, enabled in styles):
            return False
    return True


def _preserves_path_winding(mapping):
    return mapping.isAffine() and mapping.m11()*mapping.m22()-mapping.m12()*mapping.m21() > 0


def compound_outline(path, baseline, sources, cache=None, tolerance=.125, *, source_indices=None, clip=True):
    """Build positive winding coverage, optionally retaining its unclipped mesh."""
    if _uniform_compound_outline(baseline, sources, source_indices):
        def uniform():
            result = native_stroke(path, baseline, tolerance=tolerance)
            if clip:
                return clip_coverage(result, path, tolerance=tolerance)
            result.setFillRule(Qt.WindingFill)
            return result
        return _cached(cache, ("compound_uniform_outline", path_key(path), baseline, tolerance, clip), uniform)
    geometry = tuple((geometry_key(s.bound), transform_key(s.mapping)) for s in sources)
    attribution_key = ("attribution", _cache_fingerprint((path_key(path), geometry, tolerance)))
    styles = tuple((s.baseline, s.edge_styles or tuple(tuple((n.outline_multiplier, n.outline_enabled)
                                          for n in c.nodes) for c in s.bound.iter_contours()))
                   for s in sources)
    key = ("compound_outline", attribution_key, baseline, _cache_fingerprint(styles), source_indices, clip)

    def build():
        compiled = [_cached(cache, ("contours", geometry_key(s.bound)),
                            lambda s=s: compile_bound(s.bound)) for s in sources]
        orientation_preserved = [_preserves_path_winding(s.mapping) for s in sources]
        spans, fallback = _cached(cache, attribution_key,
                                 lambda: attribute_boundary(path, sources, compiled, tolerance, cache))
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
            mapped = source.mapping.map(mesh)
            result.addPath(mapped if orientation_preserved[span.source] else positive_path(mapped))
            if connected_start:
                previous_edge = compiled[span.source][span.contour].edges[previous.edge].path
                if not previous_edge.isEmpty() and not section.isEmpty():
                    join = round_join(section.pointAtPercent(0), source.baseline*a,
                                      _endpoint_tangent(previous_edge, False),
                                      _endpoint_tangent(section, True))
                    mapped = source.mapping.map(join)
                    result.addPath(mapped if orientation_preserved[span.source] else positive_path(mapped))
        return clip_coverage(result, path, tolerance=tolerance) if clip else result

    return _cached(cache, key, build)
