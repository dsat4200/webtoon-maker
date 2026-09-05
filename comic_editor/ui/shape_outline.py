"""Per-edge, variable-width shape outline meshes."""
import math
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QPolygonF


def customized(bound):
    return any(n.outline_multiplier != 1 or not n.outline_enabled for c in bound.iter_contours() for n in c.nodes)


def outline_mesh(bound, baseline, fill_path, *, core=None, base_width=0, clip=True, boundary_filter=None):
    if baseline <= 0:
        return QPainterPath()
    result = QPainterPath()
    result.setFillRule(Qt.WindingFill)
    for contour in bound.iter_contours():
        nodes = contour.nodes
        for index in range(len(nodes) if contour.closed else len(nodes) - 1):
            first, last = nodes[index], nodes[(index + 1) % len(nodes)]
            if not first.outline_enabled:
                continue
            curve = QPainterPath(QPointF(*first.position))
            curve.cubicTo(QPointF(*(first.outgoing or first.position)), QPointF(*(last.incoming or last.position)), QPointF(*last.position))
            steps = max(4, min(512, math.ceil(curve.length() / 2)))
            samples = []
            for i in range(steps + 1):
                fraction = i / steps
                point = curve.pointAtPercent(fraction)
                width = baseline * (first.outline_multiplier * (1 - fraction) + last.outline_multiplier * fraction)
                if core is not None:
                    width += base_width * (first.width_multiplier * (1 - fraction) + last.width_multiplier * fraction) / 2
                samples.append((point, width))
            for (a, wa), (b, wb) in zip(samples, samples[1:]):
                if boundary_filter is not None and not boundary_filter((a + b) / 2):
                    continue
                delta = b - a
                length = math.hypot(delta.x(), delta.y())
                if length < 1e-9:
                    continue
                normal = QPointF(-delta.y() / length, delta.x() / length)
                piece = QPainterPath()
                piece.addPolygon(QPolygonF([a + normal * wa, b + normal * wb, b - normal * wb, a - normal * wa]))
                piece.closeSubpath()
                result = result.united(piece)
            for point, width in samples:
                if width > 0 and (boundary_filter is None or boundary_filter(point)):
                    cap = QPainterPath()
                    cap.addEllipse(point, width, width)
                    result = result.united(cap)
    if not clip:
        return result
    return result.subtracted(core) if core is not None else result.intersected(fill_path)


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
