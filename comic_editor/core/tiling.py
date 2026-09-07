"""Regular tilings shared by rendering, brush transport and periodic fill.

Coordinates are document pixels. Cell maps always map a repeated cell back to
the canonical polygon; they never depend on the viewport or input history.
"""
from dataclasses import dataclass
import math

import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QPainterPath, QPolygonF, QTransform, QImage, QPainter

H = math.sqrt(3) / 2


def _snap_lattice(value):
    integer = np.rint(value)
    return np.where(np.abs(value-integer) < 1e-10, integer, value)


def map_arrays(transform, x, y):
    denominator = transform.m13()*x + transform.m23()*y + transform.m33()
    return ((transform.m11()*x + transform.m21()*y + transform.m31()) / denominator,
            (transform.m12()*x + transform.m22()*y + transform.m32()) / denominator)


def polygon_path(points):
    result = QPainterPath()
    result.addPolygon(QPolygonF([QPointF(*p) for p in points]))
    result.closeSubpath()
    return result


@dataclass(frozen=True)
class TilingGeometry:
    shape: str = "square"
    center: tuple = (0., 0.)
    side: float = 256.
    rotation: float = 0.

    @classmethod
    def from_modifier(cls, modifier):
        return cls(modifier.shape, tuple(modifier.center), modifier.side, modifier.rotation)

    def transform(self):
        result = QTransform()
        result.translate(*self.center)
        result.rotate(self.rotation)
        result.scale(self.side, self.side)
        return result

    def vertices(self):
        if self.shape == "square":
            points = [(-.5, -.5), (.5, -.5), (.5, .5), (-.5, .5)]
        elif self.shape == "hexagon":
            points = [(math.cos(i*math.pi/3), math.sin(i*math.pi/3)) for i in range(6)]
        else:
            points = [(-.5, -H/3), (.5, -H/3), (0., 2*H/3)]
        transform = self.transform()
        return [transform.map(QPointF(*p)).toTuple() for p in points]

    def path(self):
        return polygon_path(self.vertices())

    def bounds(self):
        return self.path().boundingRect()

    def pixel_mask(self, bounds, width, height, local_to_world=None, *, ensure_nonempty=True):
        """One shared rasterization rule for source texels and fill adjacency."""
        inverse = (local_to_world or QTransform()).inverted()[0]
        mask = QImage(width, height, QImage.Format_Alpha8)
        mask.fill(0)
        painter = QPainter(mask)
        painter.scale(width/bounds.width(), height/bounds.height())
        painter.translate(-bounds.x(), -bounds.y())
        painter.setTransform(inverse, True)
        painter.fillPath(self.path(), Qt.white)
        painter.end()
        valid = np.frombuffer(mask.constBits(), np.uint8).reshape(height, mask.bytesPerLine())[:, :width] > 0
        if ensure_nonempty and not valid.any():
            valid[height//2, width//2] = True
        return valid

    def _fold(self, x, y, *, cells=False):
        """Vectorized canonical mapping. Ties have stable, half-open ownership."""
        x, y = np.broadcast_arrays(np.asarray(x, dtype=float), np.asarray(y, dtype=float))
        if self.shape == "square":
            i, j = np.floor(_snap_lattice(x+.5)).astype(np.int64), np.floor(_snap_lattice(y+.5)).astype(np.int64)
            return (i, j, np.zeros_like(i)) if cells else (x-i, y-j)
        if self.shape == "hexagon":
            q = x/1.5
            r = y/(2*H)-q/2
            qi, ri = np.floor(q).astype(np.int64), np.floor(r).astype(np.int64)
            best = np.full(x.shape, np.inf)
            i, j = qi.copy(), ri.copy()
            # Ordered candidates provide deterministic ownership at Voronoi ties.
            for dq in (-1, 0, 1):
                for dr in (-1, 0, 1):
                    a, b = qi+dq, ri+dr
                    distance = (x-1.5*a)**2 + (y-H*a-2*H*b)**2
                    take = distance < best-1e-12
                    best = np.where(take, distance, best)
                    i, j = np.where(take, a, i), np.where(take, b, j)
            return (i, j, np.zeros_like(i)) if cells else (x-1.5*i, y-H*i-2*H*j)
        # Three-colour the vertices of the triangular lattice. Adjacent faces
        # then map by reflection, with identical values along their shared edge.
        xx, yy = x+.5, y+H/3
        a, b = _snap_lattice(xx-yy/(2*H)), _snap_lattice(yy/H)
        i, j = np.floor(a).astype(np.int64), np.floor(b).astype(np.int64)
        f, g = a-i, b-j
        upper = f+g > 1+1e-10
        if cells:
            return i, j, upper.astype(np.int64)
        weights = (np.where(upper, f+g-1, 1-f-g),
                   np.where(upper, 1-f, f), np.where(upper, 1-g, g))
        colors = ((i+2*j+np.where(upper, 3, 0)) % 3,
                  (i+2*j+np.where(upper, 2, 1)) % 3,
                  (i+2*j+np.where(upper, 1, 2)) % 3)
        px, py = np.zeros_like(x), np.zeros_like(y)
        vx, vy = np.array([-.5, .5, 0.]), np.array([-H/3, -H/3, 2*H/3])
        for weight, color in zip(weights, colors):
            px += weight*vx[color]
            py += weight*vy[color]
        return px, py

    def map(self, x, y):
        inverse = self.transform().inverted()[0]
        x, y = map_arrays(inverse, x, y)
        return map_arrays(self.transform(), *self._fold(x, y))

    def point(self, point):
        x, y = self.map(point.x(), point.y())
        return QPointF(float(x), float(y))

    def cell(self, point):
        p = self.transform().inverted()[0].map(point)
        return tuple(int(v) for v in self._fold(p.x(), p.y(), cells=True))

    def cell_transform(self, cell):
        """Affine transform from the named repeated cell to the source tile."""
        i, j, upper = cell
        if self.shape == "square":
            local = QTransform.fromTranslate(-i, -j)
        elif self.shape == "hexagon":
            local = QTransform.fromTranslate(-1.5*i, -H*i-2*H*j)
        else:
            lattice = [(i, j), (i+1, j), (i, j+1)] if not upper else [(i+1, j+1), (i, j+1), (i+1, j)]
            src = np.array([[a+b*.5-.5, H*b-H/3, 1.] for a, b in lattice])
            base = np.array([[-.5, -H/3], [.5, -H/3], [0., 2*H/3]])
            dst = np.array([base[(a+2*b) % 3] for a, b in lattice])
            m = np.linalg.solve(src, dst)
            local = QTransform(m[0, 0], m[0, 1], m[1, 0], m[1, 1], m[2, 0], m[2, 1])
        transform = self.transform()
        return transform.inverted()[0] * local * transform

    def cells_intersecting(self, bounds):
        """Yield only footprint-intersecting placements, independent of canvas size."""
        local = self.transform().inverted()[0].mapRect(bounds)
        corners = [local.topLeft(), local.topRight(), local.bottomLeft(), local.bottomRight()]
        if self.shape == "square":
            a, b = [p.x() for p in corners], [p.y() for p in corners]
        elif self.shape == "hexagon":
            a = [p.x()/1.5 for p in corners]
            b = [p.y()/(2*H)-p.x()/3 for p in corners]
        else:
            a = [p.x()+.5-(p.y()+H/3)/(2*H) for p in corners]
            b = [(p.y()+H/3)/H for p in corners]
        if self.shape == "square":
            ir = range(math.floor(min(a)+.5), math.floor(max(a)+.5)+1)
            jr = range(math.floor(min(b)+.5), math.floor(max(b)+.5)+1)
        else:
            padding = 1 if self.shape == "hexagon" else 0
            ir = range(math.floor(min(a))-padding, math.floor(max(a))+padding+1)
            jr = range(math.floor(min(b))-padding, math.floor(max(b))+padding+1)
        for i in ir:
            for j in jr:
                for upper in range(2 if self.shape == "triangle" else 1):
                    cell = (i, j, upper)
                    mapping = self.cell_transform(cell)
                    path = mapping.inverted()[0].map(self.path())
                    if path.intersects(bounds) or path.contains(bounds.center()):
                        yield cell, mapping, path
