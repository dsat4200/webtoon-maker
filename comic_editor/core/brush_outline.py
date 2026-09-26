"""Closed alpha contours painted by the same material brush engine as ink.

Brush definitions are embedded in the modifier, so imported tips, textures and
dual materials travel with chapters and presets. Geometry is cached separately
from paint parameters; offscreen strokes publish their tiles only once.
"""
from __future__ import annotations

from collections import OrderedDict
from dataclasses import replace
import hashlib
import math
import threading

import numpy as np
from PySide6.QtGui import QColor, QImage

from .brushes import BrushDefinition, BrushInput
from .brush_raster import RasterBrushStroke
from .brush_stroke import BrushStroke
from .tiles import TileStore


_contours = OrderedDict()
_contour_bytes = 0
_contour_lock = threading.Lock()
_CONTOUR_BUDGET = 16 * 1024 * 1024


def alpha_contours(alpha):
    """Trace pixel-cell boundaries, keeping disconnected regions and holes.

    Only transitions create graph entries. Collinear vertices are removed, so
    long horizontal/vertical edges need two samples, not thousands of inputs.
    The right-turn rule separates diagonally touching components consistently.
    """
    global _contour_bytes
    inside = np.ascontiguousarray(alpha > 1e-6, dtype=np.uint8)
    key = inside.shape, hashlib.blake2b(inside, digest_size=16).digest()
    with _contour_lock:
        if key in _contours:
            _contours.move_to_end(key)
            return _contours[key]
    padded = np.pad(inside, 1)
    edges = {}

    def add(mask, offset, delta):
        rows, columns = np.nonzero(mask)
        for y, x in zip(rows.tolist(), columns.tolist()):
            start = x + offset[0], y + offset[1]
            end = start[0] + delta[0], start[1] + delta[1]
            edges.setdefault(start, []).append(end)

    add(inside & (1-padded[:-2, 1:-1]), (0, 0), (1, 0))
    add(inside & (1-padded[1:-1, 2:]), (1, 0), (0, 1))
    add(inside & (1-padded[2:, 1:-1]), (1, 1), (-1, 0))
    add(inside & (1-padded[1:-1, :-2]), (0, 1), (0, -1))
    paths = []
    while edges:
        start = next(iter(edges))
        point = start
        previous = None
        path = [start]
        while point in edges:
            candidates = edges[point]
            if previous is not None and len(candidates) > 1:
                dx, dy = point[0]-previous[0], point[1]-previous[1]
                # Clockwise/right turn first, straight second, left third.
                target = max(candidates, key=lambda p: (
                    dx*(p[1]-point[1])-dy*(p[0]-point[0]),
                    dx*(p[0]-point[0])+dy*(p[1]-point[1])))
            else:
                target = candidates[0]
            candidates.remove(target)
            if not candidates:
                del edges[point]
            if len(path) >= 2 and (path[-1][0]-path[-2][0], path[-1][1]-path[-2][1]) == (
                    target[0]-point[0], target[1]-point[1]):
                path[-1] = target
            elif len(path) >= 2 and (
                    (path[-1][0]-path[-2][0])*(target[1]-point[1]) ==
                    (path[-1][1]-path[-2][1])*(target[0]-point[0])):
                path[-1] = target
            else:
                path.append(target)
            previous, point = point, target
            if point == start:
                break
        if len(path) > 2:
            array = np.asarray(path, np.float32)
            array.setflags(write=False)
            paths.append(array)
    result = tuple(paths)
    cost = 128 + sum(path.nbytes + 128 for path in paths)
    if cost <= _CONTOUR_BUDGET:
        with _contour_lock:
            # Another renderer may already have published this same geometry.
            if key not in _contours:
                _contours[key] = result
                _contour_bytes += cost
            while _contours and _contour_bytes > _CONTOUR_BUDGET:
                _, removed = _contours.popitem(last=False)
                _contour_bytes -= 128 + sum(path.nbytes + 128 for path in removed)
    return result


def _field_value(field, sample):
    if np.ndim(field) == 0:
        return float(field)
    # Pixel centers lie half a pixel inside/outside a cell contour. Bilinear
    # sampling avoids thickness steps as a contour crosses a mask ramp.
    x = min(field.shape[1]-1., max(0., sample.x-.5))
    y = min(field.shape[0]-1., max(0., sample.y-.5))
    x0, y0 = int(x), int(y)
    x1, y1 = min(x0+1, field.shape[1]-1), min(y0+1, field.shape[0]-1)
    u, v = x-x0, y-y0
    return float((field[y0, x0]*(1-u)+field[y0, x1]*u)*(1-v)
                 + (field[y1, x0]*(1-u)+field[y1, x1]*u)*v)


class _ContourScheduler(BrushStroke):
    def __init__(self, definition, thickness, maximum, seed):
        super().__init__(definition, seed)
        self.thickness_field, self.maximum = thickness, maximum

    def _size(self, sample, *, randomized=True):
        return max(.1, super()._size(sample, randomized=randomized)
                   * _field_value(self.thickness_field, sample) / self.maximum)


def _prepared_brush(brush, modifier, diameter):
    brush = brush.with_size(diameter)
    dual = (_prepared_brush(brush.dual, modifier, brush.dual.size)
            if brush.dual is not None else None)
    return replace(
        brush, size_by_view=False, minimum_pixel=False, dual=dual,
        angle=brush.angle+modifier.brush_angle,
        direction="stroke" if modifier.brush_follow_contour else "fixed",
        spacing=brush.spacing*modifier.brush_spacing/100.,
        antialiasing=brush.antialiasing if modifier.antialiasing else 0,
        # Closed contours have no pen-down taper or stationary dwell. Mixing
        # and erasing need an existing painted surface; an outline is new ink.
        taper_start=0., taper_end=0., stabilization=0., post_correction=0.,
        continuous=False, blending_mode="normal", mixing_mode="none",
    )


def scale_outline_brush(modifier, scale):
    """Scale fixed material distances for reduced-resolution previews only."""
    if modifier.style != "brush" or modifier.brush is None:
        return

    def scaled(brush):
        return replace(
            brush, size=brush.size*scale,
            spacing=brush.spacing*(scale if brush.spacing_mode == "fixed" else 1),
            particle_size=brush.particle_size*(1 if brush.particle_size_relative else scale),
            texture=replace(brush.texture, scale=brush.texture.scale*scale) if brush.texture else None,
            watercolor_edge=brush.watercolor_edge*scale,
            watercolor_blur=brush.watercolor_blur*scale,
            dual=scaled(brush.dual) if brush.dual is not None else None,
        )

    modifier.brush = scaled(BrushDefinition.from_dict(modifier.brush)).to_dict()


def render_brush_outline(alpha, thickness, modifier):
    """Return premultiplied brush ink; caller clips/composites it outside alpha."""
    output = np.zeros((*alpha.shape, 4), np.float32)
    maximum = float(np.max(thickness))
    if maximum <= 0:
        return output
    brush = _prepared_brush(BrushDefinition.from_dict(modifier.brush), modifier, maximum*2)
    tiles = TileStore(64)
    for index, contour in enumerate(alpha_contours(alpha)):
        seed = (modifier.brush_seed + index*2654435761) % (2**32)
        stroke = RasterBrushStroke(tiles, "outline", brush, QColor(modifier.color), {},
                                   seed=seed, defer_flush=True)
        stroke.scheduler = _ContourScheduler(brush, thickness, maximum, seed)
        if brush.dual is not None:
            stroke.dual_scheduler = _ContourScheduler(brush.dual, thickness, maximum, seed ^ 271828)
        distance = 0.
        stroke.begin(BrushInput(*contour[0]))
        for start, end in zip(contour, contour[1:]):
            length = math.hypot(*(end-start))
            # A spatial mask can change within a straight contour segment.
            count = max(1, math.ceil(length/2)) if np.ndim(thickness) else 1
            for step in range(1, count+1):
                point = start+(end-start)*(step/count)
                stroke.add(BrushInput(*point, time=(distance+length*step/count)/1000.))
            distance += length
        stroke.finish()
    height, width = alpha.shape
    for (x, y), image in tiles.iter_tiles("outline"):
        left, top = max(0, x*64), max(0, y*64)
        right, bottom = min(width, (x+1)*64), min(height, (y+1)*64)
        if right <= left or bottom <= top:
            continue
        image = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        pixels = np.frombuffer(image.constBits(), np.uint8).reshape(64, image.bytesPerLine())[:, :256].reshape(64, 64, 4)
        output[top:bottom, left:right] = pixels[top-y*64:bottom-y*64, left-x*64:right-x*64]/255.
    return output
