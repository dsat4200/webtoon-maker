"""Bounded, vectorized closed-stroke sampling and periodic appearance effects."""
from dataclasses import dataclass
import math
import numpy as np
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QPainterPath, QPainterPathStroker, QPolygonF, QTransform

from .models import ScreamModifier, WobbleModifier


@dataclass
class StrokeLoop:
    points: np.ndarray
    width: float

    def mapped(self, transform):
        points = np.asarray([transform.map(QPointF(*p)).toTuple() for p in self.points])
        scale = math.sqrt(abs(transform.determinant()))
        return StrokeLoop(points, self.width * scale)


def distances(points):
    edges = np.roll(points, -1, axis=0) - points
    lengths = np.linalg.norm(edges, axis=1)
    return np.r_[0., np.cumsum(lengths)], lengths


def interpolate(points, positions):
    cumulative, _ = distances(points)
    length = cumulative[-1]
    wrapped = np.asarray(positions) % max(length, 1e-9)
    closed = np.vstack((points, points[0]))
    return np.column_stack([np.interp(wrapped, cumulative, closed[:, axis]) for axis in (0, 1)])


def normals(points):
    tangent = np.roll(points, -1, axis=0) - np.roll(points, 1, axis=0)
    tangent /= np.maximum(np.linalg.norm(tangent, axis=1, keepdims=True), 1e-9)
    area = np.sum(points[:, 0]*np.roll(points[:, 1], -1)-points[:, 1]*np.roll(points[:, 0], -1))
    return np.column_stack((tangent[:, 1], -tangent[:, 0])) * (1 if area >= 0 else -1)


def sample_path(path, width, *, inset=False):
    """Qt flattens cubics at subpixel tolerance; resampling has a fixed upper bound."""
    result = []
    for polygon in path.toSubpathPolygons(QTransform().scale(3, 3)):
        points = np.asarray([(p.x()/3, p.y()/3) for p in polygon], dtype=np.float64)
        if len(points) > 1 and np.allclose(points[0], points[-1]):
            points = points[:-1]
        if len(points) < 3:
            continue
        length = distances(points)[0][-1]
        if length < 1e-6:
            continue
        points = interpolate(points, np.linspace(0, length, min(8192, max(24, math.ceil(length/.75))), endpoint=False))
        if inset:
            points -= normals(points) * width/2
        result.append(StrokeLoop(points, max(.1, width)))
    return result


def loop_path(points):
    path = QPainterPath()
    path.addPolygon(QPolygonF([QPointF(*p) for p in points]))
    path.closeSubpath()
    return path


def perlin(x, y, seed):
    """Seeded 2D gradient noise with quintic interpolation (no scene object)."""
    permutation = np.random.default_rng(int(seed) % (2**32)).permutation(256)
    table = np.tile(permutation, 2)
    ix, iy = np.floor(x).astype(np.int64), np.floor(y).astype(np.int64)
    fx, fy = x-ix, y-iy
    u, v = fx**3*(fx*(fx*6-15)+10), fy**3*(fy*(fy*6-15)+10)
    gradients = np.array([[1, 0], [-1, 0], [0, 1], [0, -1],
                          [.70710678, .70710678], [-.70710678, .70710678],
                          [.70710678, -.70710678], [-.70710678, -.70710678]])
    def dot(dx, dy):
        g = gradients[table[table[(ix+dx) & 255]+((iy+dy) & 255)] & 7]
        return g[:, 0]*(fx-dx)+g[:, 1]*(fy-dy)
    a, b, c, d = dot(0, 0), dot(1, 0), dot(0, 1), dot(1, 1)
    return np.clip(((a*(1-u)+b*u)*(1-v)+(c*(1-u)+d*u)*v)*1.5, -1, 1)


def deform_loop(loop, modifier, parameter):
    """Return render samples, never modifying the document's editable points."""
    points = loop.points
    cumulative, lengths = distances(points)
    strength = parameter("intensity")/100
    opacity = np.ones(len(points))
    if isinstance(modifier, ScreamModifier):
        rates = lengths/np.maximum(4, parameter("width"))
        total = max(float(rates.sum()), 1e-9)
        count = max(1, round(total))
        phase = np.r_[0., np.cumsum(rates[:-1])] * count/total % 1
        triangle = 1-np.abs(phase*2-1)
        rounded = np.sqrt(np.maximum(0., 1-(phase*2-1)**2))
        roundness = parameter("roundness")/100
        offset = (triangle*(1-roundness)+rounded*roundness)*parameter("height")*strength
    elif isinstance(modifier, WobbleModifier):
        length = max(cumulative[-1], 1e-9)
        angle = 2*np.pi*(cumulative[:-1]+parameter("noise_offset"))/length
        radius = length/(2*np.pi*np.maximum(4, parameter("noise_scale")))
        x, y = radius*np.cos(angle)+17.31, radius*np.sin(angle)+39.73
        offset = perlin(x, y, modifier.seed)*parameter("position")*strength
        opacity = 1-(perlin(x, y, modifier.seed ^ 0x5F356495)+1)*.5*parameter("strength")/100*strength
    else:
        return loop, opacity
    return StrokeLoop(points+normals(points)*offset[:, None], loop.width), opacity


def stroke_path(points, width, roundness=100, closed=False):
    if len(points) < 2 or width <= 0:
        return QPainterPath()
    if not closed:
        delta = np.gradient(points, axis=0)
        delta /= np.maximum(np.linalg.norm(delta, axis=1, keepdims=True), 1e-9)
        normal = np.column_stack((-delta[:, 1], delta[:, 0]))
        lengths = np.r_[0., np.cumsum(np.linalg.norm(np.diff(points, axis=0), axis=1))]
        half = width/2
        radius = min(half*roundness/100, lengths[-1]/2)
        first, last = points[0], points[-1]
        start, end = delta[0], delta[-1]
        left, right = normal[0], normal[-1]
        path = QPainterPath(QPointF(*(first+left*(half-radius))))
        path.quadTo(QPointF(*(first+left*half)), QPointF(*(first+start*radius+left*half)))
        indexes = np.flatnonzero((lengths > radius) & (lengths < lengths[-1]-radius))
        for index in indexes:
            path.lineTo(QPointF(*(points[index]+normal[index]*half)))
        path.lineTo(QPointF(*(last-end*radius+right*half)))
        path.quadTo(QPointF(*(last+right*half)), QPointF(*(last+right*(half-radius))))
        path.lineTo(QPointF(*(last-right*(half-radius))))
        path.quadTo(QPointF(*(last-right*half)), QPointF(*(last-end*radius-right*half)))
        for index in indexes[::-1]:
            path.lineTo(QPointF(*(points[index]-normal[index]*half)))
        path.lineTo(QPointF(*(first+start*radius-left*half)))
        path.quadTo(QPointF(*(first-left*half)), QPointF(*(first-left*(half-radius))))
        path.closeSubpath()
        return path
    stroker = QPainterPathStroker()
    stroker.setWidth(width)
    stroker.setJoinStyle(Qt.RoundJoin)
    return stroker.createStroke(loop_path(points))


def dot_dash_path(loop, modifier, parameter, *, spacing_points=None):
    """Fit whole pattern repeats to a closed circumference; no seam fragment."""
    result = QPainterPath()
    result.setFillRule(Qt.WindingFill)
    pattern = modifier.pattern
    if not pattern or not pattern.strip():
        return result
    cumulative, edges = distances(loop.points if spacing_points is None else spacing_points)
    def locate(positions):
        if spacing_points is None:
            return interpolate(loop.points, positions)
        closed = np.vstack((loop.points, loop.points[0]))
        positions = np.asarray(positions) % max(cumulative[-1], 1e-9)
        return np.column_stack([np.interp(positions, cumulative, closed[:, axis]) for axis in (0, 1)])
    length = cumulative[-1]
    mark_lengths = parameter("length") if modifier.mode == "dash" else np.full(len(loop.points), loop.width)
    pitch = np.maximum(1, mark_lengths+parameter("distance"))
    phase = np.r_[0., np.cumsum(edges/pitch)]
    repeats = max(1, round(phase[-1]/len(pattern)))
    count = min(max(1, 8192//len(pattern)), repeats)*len(pattern)
    phase *= count/max(phase[-1], 1e-9)
    centers = np.interp(np.arange(count)+.5, phase, cumulative)
    for index, center in enumerate(centers):
        if pattern[index % len(pattern)] != "-":
            continue
        sample = min(len(loop.points)-1, np.searchsorted(cumulative, center, side="right")-1)
        roundness = float(parameter("roundness")[sample])
        width = loop.width
        if modifier.mode == "dot":
            x, y = locate([center])[0]
            from PySide6.QtCore import QRectF
            result.addRoundedRect(QRectF(x-width/2, y-width/2, width, width),
                                  width*.5*roundness/100, width*.5*roundness/100)
        else:
            span = min(float(mark_lengths[sample]), length/count*.98)
            positions = np.linspace(center-span/2, center+span/2,
                                    max(2, min(1024, math.ceil(span/.75))))
            result.addPath(stroke_path(locate(positions), width, roundness))
    return result
