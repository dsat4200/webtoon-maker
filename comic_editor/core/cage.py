"""Serializable cage geometry and vectorized, interpolating mesh evaluation.

Coordinates are document coordinates, including for linked targets. Evaluation
uses displacement (rather than position) so an undeformed grid is exact.
"""
from __future__ import annotations

from dataclasses import dataclass, field
import math
import numpy as np


@dataclass
class CageGrid:
    frame: tuple[float, float, float, float] = (0., 0., 100., 100.)
    columns: int = 4
    rows: int = 4
    points: list[tuple[float, float]] = field(default_factory=list)
    smoothness: float = 50.
    interpolation: str = "bicubic"
    pivot: tuple[float, float] | None = None
    uniform: bool = False
    source_quad: list[tuple[float, float]] | None = None

    def validate_grid(self):
        self.frame = tuple(float(v) for v in self.frame)
        if len(self.frame) != 4 or not all(math.isfinite(v) for v in self.frame):
            raise ValueError("Cage frame must contain four finite numbers")
        if min(self.frame[2:]) <= 0:
            raise ValueError("Cage frame must have positive dimensions")
        self.columns = max(2, min(16, int(self.columns)))
        self.rows = max(2, min(16, int(self.rows)))
        self.smoothness = float(self.smoothness)
        if not math.isfinite(self.smoothness):
            raise ValueError("Cage smoothness must be finite")
        self.smoothness = max(0., min(100., self.smoothness))
        if self.interpolation not in {"nearest", "bilinear", "bicubic"}:
            raise ValueError("Unknown cage interpolation")
        if not self.points:
            self.points = [tuple(p) for p in self.rest_points()]
        if len(self.points) != self.columns * self.rows:
            raise ValueError("Cage point count does not match the lattice")
        self.points = [tuple(float(v) for v in p) for p in self.points]
        if any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in self.points):
            raise ValueError("Cage points must be finite pairs")
        if self.pivot is None:
            x, y, w, h = self.frame
            self.pivot = (x + w / 2, y + h / 2)
        self.pivot = tuple(float(v) for v in self.pivot)
        if len(self.pivot) != 2 or not all(math.isfinite(v) for v in self.pivot):
            raise ValueError("Cage pivot must be a finite pair")
        self.uniform = bool(self.uniform)
        if self.source_quad is not None:
            self.source_quad = [tuple(float(v) for v in p) for p in self.source_quad]
            if len(self.source_quad) != 4 or any(len(p) != 2 or not all(math.isfinite(v) for v in p) for p in self.source_quad):
                raise ValueError("Cage source quad must have four finite points")
            homography(self.source_quad)  # reject singular saved mappings

    def rest_points(self):
        x, y, w, h = self.frame
        xx, yy = np.meshgrid(np.linspace(x, x+w, self.columns),
                             np.linspace(y, y+h, self.rows))
        points = np.stack((xx, yy), axis=-1).reshape(-1, 2)
        if self.source_quad is not None:
            points = project(homography(self.source_quad), (points-(x, y))/(w, h))
        return points

    def grid_dict(self):
        self.validate_grid()
        return {"frame": list(self.frame), "columns": self.columns,
                "rows": self.rows, "points": [list(p) for p in self.points],
                "smoothness": self.smoothness, "interpolation": self.interpolation,
                "pivot": list(self.pivot), "uniform": self.uniform,
                "source_quad": [list(p) for p in self.source_quad] if self.source_quad is not None else None}

    @staticmethod
    def grid_kwargs(data):
        return {key: data[key] for key in (
            "frame", "columns", "rows", "points", "smoothness",
            "interpolation", "pivot", "uniform", "source_quad") if key in data}

    def resample(self, columns, rows):
        """Change lattice density without discarding the current deformation."""
        columns, rows = max(2, min(16, int(columns))), max(2, min(16, int(rows)))
        grid = CageGrid(frame=self.frame, columns=columns, rows=rows, source_quad=self.source_quad)
        points = map_points(self, grid.rest_points())
        self.columns, self.rows = columns, rows
        self.points = [tuple(p) for p in points]


def _cubic(a, b, c, d, t):
    return b + .5*t*(c-a + t*(2*a-5*b+4*c-d + t*(3*(b-c)+d-a)))


def homography(quad):
    matrix, values = [], []
    for (u, v), (x, y) in zip(((0, 0), (1, 0), (1, 1), (0, 1)), quad):
        matrix.extend(((u, v, 1, 0, 0, 0, -u*x, -v*x),
                       (0, 0, 0, u, v, 1, -u*y, -v*y)))
        values.extend((x, y))
    try:
        return np.append(np.linalg.solve(matrix, values), 1.).reshape(3, 3)
    except np.linalg.LinAlgError as error:
        raise ValueError("Cage source mapping is singular") from error


def project(matrix, points):
    points = np.asarray(points)
    homogeneous = np.concatenate((points, np.ones((*points.shape[:-1], 1))), axis=-1) @ matrix.T
    return homogeneous[..., :2]/homogeneous[..., 2:]


def map_points(grid: CageGrid, points):
    source = np.asarray(points, dtype=np.float64)
    if source.size == 0:
        return source.copy()
    shape = source.shape
    source = source.reshape(-1, 2)
    control = np.asarray(grid.points or grid.rest_points()).reshape(grid.rows, grid.columns, 2)
    displacement = control - grid.rest_points().reshape(control.shape)
    uv = (project(np.linalg.inv(homography(grid.source_quad)), source) if grid.source_quad is not None
          else (source - np.asarray(grid.frame[:2])) / np.asarray(grid.frame[2:]))
    uv *= np.asarray((grid.columns-1, grid.rows-1))
    uv = np.clip(uv, 0, (grid.columns-1, grid.rows-1))
    ij = np.minimum(np.floor(uv).astype(int), (grid.columns-2, grid.rows-2))
    u, v = (uv-ij).T
    i, j = ij.T
    u, v = u[:, None], v[:, None]
    linear = ((1-v)*((1-u)*displacement[j, i]+u*displacement[j, i+1])
              + v*((1-u)*displacement[j+1, i]+u*displacement[j+1, i+1]))
    # Linear extrapolation at endpoints preserves affine transforms exactly.
    if grid.smoothness > 0:
        padded = np.pad(displacement, ((1, 1), (1, 1), (0, 0)), mode="edge")
        padded[1:-1, 0] = 2*displacement[:, 0]-displacement[:, 1]
        padded[1:-1, -1] = 2*displacement[:, -1]-displacement[:, -2]
        padded[0] = 2*padded[1]-padded[2]
        padded[-1] = 2*padded[-2]-padded[-3]
        lines = [_cubic(*(padded[j+dy, i+dx] for dx in range(4)), u)
                 for dy in range(4)]
        cubic = _cubic(*lines, v)
        linear += (cubic-linear) * (grid.smoothness/100.)
    return (source+linear).reshape(shape)


def tessellate(grid, subdivisions=4):
    """Regular source topology; no triangulation or per-frame topology search."""
    nx, ny = (grid.columns-1)*subdivisions+1, (grid.rows-1)*subdivisions+1
    x, y, w, h = grid.frame
    xx, yy = np.meshgrid(np.linspace(x, x+w, nx), np.linspace(y, y+h, ny))
    source = np.stack((xx, yy), axis=-1).reshape(-1, 2)
    if grid.source_quad is not None:
        source = project(homography(grid.source_quad), (source-(x, y))/(w, h))
    corners = np.arange(nx*ny).reshape(ny, nx)[:-1, :-1].ravel()
    triangles = np.concatenate((np.stack((corners, corners+1, corners+nx+1), axis=1),
                                np.stack((corners, corners+nx+1, corners+nx), axis=1)))
    return source, map_points(grid, source), triangles


def deformed_bounds(grid):
    """Conservative Catmull–Rom bounds, including overshoot between controls."""
    points = np.asarray(grid.points or grid.rest_points())
    rest = grid.rest_points()
    delta = points-rest
    # Catmull–Rom's negative lobes total at most 1/4 in two dimensions.
    pad = np.ptp(delta, axis=0) * .3 * (grid.smoothness/100.) + 1
    low, high = points.min(axis=0)-pad, points.max(axis=0)+pad
    return (*low, *(high-low))
