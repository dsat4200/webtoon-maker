"""Adaptive floor and volume-grid line generation."""

from __future__ import annotations

from dataclasses import dataclass
import math

import numpy as np
from numpy.typing import NDArray


@dataclass(frozen=True, slots=True)
class GridData:
    vertices: NDArray[np.float32]
    major_every: int
    spacing: float


@dataclass(frozen=True, slots=True)
class ColoredLineData:
    vertices: NDArray[np.float32]
    colors: NDArray[np.float32]


def nice_spacing(footprint: float) -> float:
    target = max(float(footprint) / 20.0, 1e-9)
    exponent = math.floor(math.log10(target))
    fraction = target / (10.0 ** exponent)
    nice = 1.0 if fraction <= 1.0 else 2.0 if fraction <= 2.0 else 5.0 if fraction <= 5.0 else 10.0
    return nice * (10.0 ** exponent)


def build_floor_grid(footprint: float, *, extent: float | None = None) -> GridData:
    spacing = nice_spacing(footprint)
    half = max(float(extent if extent is not None else footprint), spacing)
    count = max(1, min(int(math.ceil(half / spacing)), 500))
    lines: list[tuple[float, float, float]] = []
    for i in range(-count, count + 1):
        value = i * spacing
        lines.extend(((-half, 0.0, value), (half, 0.0, value), (value, 0.0, -half), (value, 0.0, half)))
    return GridData(np.asarray(lines, dtype=np.float32), 5, spacing)


def axis_lines(length: float = 10.0) -> ColoredLineData:
    value = max(float(length), 0.1)
    vertices = np.asarray([(-value,0,0),(value,0,0),(0,-value,0),(0,value,0),(0,0,-value),(0,0,value)], dtype=np.float32)
    colors = np.asarray([(0.75,0.2,0.2,1)]*2 + [(0.2,0.75,0.2,1)]*2 + [(0.2,0.4,0.9,1)]*2, dtype=np.float32)
    return ColoredLineData(vertices, colors)


def build_volume_grid(footprint: float) -> ColoredLineData:
    """Build a bounded, adaptive XYZ lattice centered on the world origin."""
    extent = max(abs(float(footprint)), 1.0e-6)
    spacing = nice_spacing(extent)
    cells = max(2, min(int(math.ceil(extent / spacing)), 32))
    coordinates = range(-cells, cells + 1)
    low, high = -cells * spacing, cells * spacing
    vertices: list[tuple[float, float, float]] = []
    colors: list[tuple[float, float, float, float]] = []
    minor = (0.45, 0.48, 0.52, 0.12)
    major = (0.55, 0.59, 0.64, 0.24)
    axes = (
        (0.75, 0.20, 0.20, 0.72),
        (0.20, 0.75, 0.20, 0.72),
        (0.20, 0.40, 0.90, 0.72),
    )

    def add(
        start: tuple[float, float, float],
        end: tuple[float, float, float],
        color: tuple[float, float, float, float],
    ) -> None:
        vertices.extend((start, end))
        colors.extend((color, color))

    for first in coordinates:
        for second in coordinates:
            a, b = first * spacing, second * spacing
            level_color = major if first % 5 == 0 or second % 5 == 0 else minor
            add((low, a, b), (high, a, b), axes[0] if first == second == 0 else level_color)
            add((a, low, b), (a, high, b), axes[1] if first == second == 0 else level_color)
            add((a, b, low), (a, b, high), axes[2] if first == second == 0 else level_color)
    return ColoredLineData(
        np.asarray(vertices, dtype=np.float32),
        np.asarray(colors, dtype=np.float32),
    )
