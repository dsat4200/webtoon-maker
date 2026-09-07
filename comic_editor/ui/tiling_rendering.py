"""Bounded premultiplied inverse sampling of regular tiles."""
import numpy as np
from collections import OrderedDict
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform, QPainter
from scipy.ndimage import distance_transform_edt

from comic_editor.core.tiling import map_arrays
from comic_editor.ui.effect_pipeline import empty_image
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _premultiplied_qimage


class RepeatMapCache:
    """Source-independent lookup strips, bounded separately from effect images."""
    def __init__(self, budget=64*1024*1024):
        self.budget = budget
        self.bytes = 0
        self.entries = OrderedDict()

    def get(self, key):
        value = self.entries.get(key)
        if value is not None:
            self.entries.move_to_end(key)
        return value

    def put(self, key, value):
        size = sum(array.nbytes for array in value if array is not None)
        if size > self.budget:
            return
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.bytes -= sum(a.nbytes for a in previous if a is not None)
        self.entries[key] = value
        self.bytes += size
        while self.bytes > self.budget:
            _, old = self.entries.popitem(last=False)
            self.bytes -= sum(a.nbytes for a in old if a is not None)


def repeat_image(image, bounds, geometry, output, local_to_world=None, nearest=False, cache=None,
                 output_to_world=None, repeat=True):
    mapping = local_to_world or QTransform()
    output_mapping = output_to_world if output_to_world is not None else mapping
    inverse, valid = mapping.inverted()
    if not valid or output.isEmpty():
        return empty_image(output)
    result = empty_image(output)
    source = _qimage_premultiplied(image)
    flat = np.concatenate((source.reshape(-1, 4), np.zeros((1, 4), dtype=np.float32)))
    sx, sy = image.width()/bounds.width(), image.height()/bounds.height()

    prefix = (geometry, tuple(bounds.getRect()), image.width(), image.height(), nearest,
              tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)),
              tuple(getattr(output_mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)), repeat)
    canonical_key = ("canonical", *prefix)
    canonical = cache.get(canonical_key) if cache is not None else None
    if canonical is None:
        valid_pixels = geometry.pixel_mask(bounds, image.width(), image.height(), mapping)
        canonical = tuple(distance_transform_edt(~valid_pixels, return_distances=False, return_indices=True))
        if cache is not None:
            cache.put(canonical_key, canonical)

    def indices(x, y, fold=True, inside=None):
        if fold and repeat:
            wx, wy = map_arrays(mapping, x, y)
            wx, wy = geometry.map(wx, wy)
            x, y = map_arrays(inverse, wx, wy)
        ix = np.floor((x-bounds.x())*sx).astype(np.int64)
        iy = np.floor((y-bounds.y())*sy).astype(np.int64)
        valid = (ix >= 0) & (iy >= 0) & (ix < image.width()) & (iy < image.height())
        ix, iy = np.clip(ix, 0, image.width()-1), np.clip(iy, 0, image.height()-1)
        cy, cx = canonical[0][iy, ix], canonical[1][iy, ix]
        if repeat:
            iy, ix = cy, cx
        elif inside is not None:
            iy, ix = np.where(inside, cy, iy), np.where(inside, cx, ix)
        return np.where(valid, iy*image.width()+ix, image.width()*image.height()).astype(np.int32)

    painter = QPainter(result)
    try:
        # Work memory is at most one 64-row strip, even for tiny tiles.
        for row in range(0, result.height(), 64):
            count = min(64, result.height()-row)
            key = (*prefix, output.x(), output.y()+row, result.width(), count)
            lookup = cache.get(key) if cache is not None else None
            if lookup is None:
                inside = None if repeat else geometry.pixel_mask(
                    QRectF(output.x(), output.y()+row, result.width(), count),
                    result.width(), count, output_mapping, ensure_nonempty=False)
                x, y = np.meshgrid(output.x()+np.arange(result.width())+.5,
                                   output.y()+row+np.arange(count)+.5)
                wx, wy = map_arrays(output_mapping, x, y)
                if repeat:
                    wx, wy = geometry.map(wx, wy)
                x, y = map_arrays(inverse, wx, wy)
                if nearest:
                    lookup = (indices(x, y, fold=False, inside=inside)[None, ...], None)
                else:
                    u, v = (x-bounds.x())*sx-.5, (y-bounds.y())*sy-.5
                    ix, iy = np.floor(u), np.floor(v)
                    fx, fy = u-ix, v-iy
                    x0, y0 = bounds.x()+(ix+.5)/sx, bounds.y()+(iy+.5)/sy
                    lookup = (np.stack((indices(x0, y0, inside=inside), indices(x0+1/sx, y0, inside=inside),
                                        indices(x0, y0+1/sy, inside=inside), indices(x0+1/sx, y0+1/sy, inside=inside))),
                              np.stack(((1-fx)*(1-fy), fx*(1-fy), (1-fx)*fy, fx*fy)).astype(np.float32))
                if cache is not None:
                    cache.put(key, lookup)
            taps, weights = lookup
            if weights is None:
                pixels = flat[taps[0]]
            else:
                pixels = sum(flat[taps[i]]*weights[i, ..., None] for i in range(4))
            # Round once at the output, so floating point filter weights cannot
            # turn constant translucent coverage into alternating alpha values.
            painter.drawImage(0, row, _premultiplied_qimage(pixels+.5/255))
    finally:
        painter.end()
    return result
