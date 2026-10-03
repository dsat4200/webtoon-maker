"""Frame-addressed RGBA8 blur levels with Pillow-compatible bilinear sampling.

Pillow's resize box accepts float32 coordinates, which can shift coefficients
when cropping a large odd frame. Compute the same normalized 22-bit coefficients
from global dimensions instead. Horizontal and vertical passes retain their
individual byte rounding, including the legacy RGBA premultiplication steps.
"""
import math
from collections import OrderedDict
from threading import RLock
import numpy as np
from PIL import Image


RADII = np.asarray((0., 1., 3., 7., 15., 31., 63., 127.), np.float32)


class BlurTileCache:
    """Immutable arrays shared safely with detached workers, under a byte budget."""
    def __init__(self, budget=32 * 1024 * 1024):
        self.budget, self.bytes = budget, 0
        self._values, self._lock = OrderedDict(), RLock()

    def get(self, key):
        with self._lock:
            result = self._values.pop(key, None)
            if result is not None:
                self._values[key] = result
            return result

    def put(self, key, value):
        value = np.ascontiguousarray(value)
        value.setflags(write=False)
        with self._lock:
            old = self._values.pop(key, None)
            if old is not None:
                self.bytes -= old.nbytes
            if value.nbytes > self.budget:
                return
            while self._values and self.bytes + value.nbytes > self.budget:
                self.bytes -= self._values.popitem(last=False)[1].nbytes
            self._values[key] = value
            self.bytes += value.nbytes


def _coefficients(source, destination, start, count):
    if source == destination:
        return np.arange(start, start + count)[:, None], np.full((count, 1), 1 << 22, np.int64)
    scale = source / destination
    support = max(1., scale)
    centers = (np.arange(start, start + count) + .5) * scale
    low = np.maximum(0, (centers - support + .5).astype(np.int64))
    high = np.minimum(source, (centers + support + .5).astype(np.int64))
    indexes = low[:, None] + np.arange(int(np.max(high - low)))
    weights = np.maximum(0., 1. - abs((indexes - centers[:, None] + .5) / support))
    weights *= indexes < high[:, None]
    weights /= weights.sum(axis=1)[:, None]
    return np.minimum(indexes, source - 1), np.floor(weights * (1 << 22) + .5).astype(np.int64)


def resize_region(fetch, source_size, destination_size, region, algorithm="normal"):
    x, y, width, height = region
    if source_size == destination_size:
        return fetch(region)
    ix, cx = _coefficients(source_size[0], destination_size[0], x, width)
    iy, cy = _coefficients(source_size[1], destination_size[1], y, height)
    left, top, right, bottom = int(ix.min()), int(iy.min()), int(ix.max()) + 1, int(iy.max()) + 1
    pixels = fetch((left, top, right - left, bottom - top))
    if algorithm == "legacy":
        pixels = np.asarray(Image.fromarray(pixels, "RGBA").convert("RGBa"))
    horizontal = np.full((bottom - top, width, 4), 1 << 21, np.int64)
    for sample in range(ix.shape[1]):
        horizontal += pixels[:, ix[:, sample] - left].astype(np.int64) * cx[None, :, sample, None]
    horizontal = np.clip(horizontal >> 22, 0, 255).astype(np.uint8)
    vertical = np.full((height, width, 4), 1 << 21, np.int64)
    for sample in range(iy.shape[1]):
        vertical += horizontal[iy[:, sample] - top].astype(np.int64) * cy[:, sample, None, None]
    result = np.clip(vertical >> 22, 0, 255).astype(np.uint8)
    if algorithm == "legacy":
        result = np.asarray(Image.fromarray(result, "RGBa").convert("RGBA")).copy()
    return result


class RegionalBlur:
    """Fetch only required pyramid tiles; cache storage is owned by the backend."""
    def __init__(self, size, fetch, get, put, algorithm="normal"):
        self.sizes = [tuple(size)]
        for _ in RADII[1:]:
            self.sizes.append(tuple(max(1, (v + 1) // 2) for v in self.sizes[-1]))
        self.fetch, self.get, self.put, self.algorithm = fetch, get, put, algorithm
        self._local = {}

    def detached(self, region, strength):
        """Resolve live source dependencies before submitting any worker work."""
        sources, seen, existing = {}, set(), {}
        def require_level(index, rect):
            if index == 0:
                if rect not in sources:
                    value = self.fetch(rect)
                    value.setflags(write=False)
                    sources[rect] = value
                return
            x, y, width, height = rect
            edge = max(4, 256 >> index)
            for ty in range(y // edge, math.ceil((y + height) / edge)):
                for tx in range(x // edge, math.ceil((x + width) / edge)):
                    key = (index, tx, ty)
                    if key in seen:
                        continue
                    seen.add(key)
                    cached = self.get(key)
                    if cached is not None:
                        existing[key] = cached
                        continue
                    bounds = (tx*edge, ty*edge, min(edge, self.sizes[index][0]-tx*edge),
                              min(edge, self.sizes[index][1]-ty*edge))
                    require_resize(index-1, self.sizes[index-1], self.sizes[index], bounds)
        def require_resize(index, source, destination, rect):
            if source == destination:
                require_level(index, rect)
                return
            x, y, width, height = rect
            ix, _ = _coefficients(source[0], destination[0], x, width)
            iy, _ = _coefficients(source[1], destination[1], y, height)
            needed = (int(ix.min()), int(iy.min()), int(ix.max()-ix.min())+1, int(iy.max()-iy.min())+1)
            require_level(index, needed)
        radii = np.clip(np.asarray(strength, np.float32), 0., 100.)
        lower = np.clip(np.searchsorted(RADII, radii, side="right")-1, 0, len(RADII)-2)
        # Keep the zero-strength fetch too, including all-zero parameter masks.
        require_level(0, region)
        for index in np.unique(lower):
            needed_levels = [int(index)]
            if np.ndim(strength) or float(radii) - float(RADII[int(index)]) > 1e-6:
                needed_levels.append(int(index)+1)
            for needed in needed_levels:
                require_resize(needed, self.sizes[needed], self.sizes[0], region)
        # Preserve cached levels even if a concurrent request evicts them before
        # the worker starts; otherwise it could need an uncaptured source region.
        publish = self.put
        def get(key):
            return existing.get(key)
        def put(key, value):
            existing[key] = value
            publish(key, value)
        return RegionalBlur(self.sizes[0], sources.__getitem__, get, put, self.algorithm), sum(v.nbytes for v in sources.values())

    def level(self, index, region):
        if index == 0:
            return self.fetch(region)
        x, y, width, height = region
        result = np.empty((height, width, 4), np.uint8)
        edge = max(4, 256 >> index)
        for ty in range(y // edge, math.ceil((y + height) / edge)):
            for tx in range(x // edge, math.ceil((x + width) / edge)):
                bounds = (tx * edge, ty * edge, min(edge, self.sizes[index][0] - tx * edge),
                          min(edge, self.sizes[index][1] - ty * edge))
                key = (index, tx, ty)
                tile = self._local.get(key)
                if tile is None:
                    tile = self.get(key)
                if tile is None:
                    tile = resize_region(lambda r: self.level(index - 1, r),
                        self.sizes[index - 1], self.sizes[index], bounds, self.algorithm)
                    self.put(key, tile)
                self._local[key] = tile
                l, t = max(x, bounds[0]), max(y, bounds[1])
                r, b = min(x + width, bounds[0] + bounds[2]), min(y + height, bounds[1] + bounds[3])
                result[t-y:b-y, l-x:r-x] = tile[t-bounds[1]:b-bounds[1], l-bounds[0]:r-bounds[0]]
        return result

    def apply(self, region, strength):
        scalar = np.ndim(strength) == 0
        values = np.asarray(strength, np.float32)
        radii = np.clip(values if scalar else np.broadcast_to(values, (region[3], region[2])), 0., 100.)
        if float(np.max(radii)) <= 1e-6:
            return self.fetch(region).astype(np.float32) / 255.
        lower = np.clip(np.searchsorted(RADII, radii, side="right") - 1, 0, len(RADII) - 2)
        mode = "RGBA" if self.algorithm == "legacy" else "RGBa"
        def upscaled(index):
            data = resize_region(lambda r: self.level(index, r), self.sizes[index], self.sizes[0], region, self.algorithm)
            return Image.fromarray(data, mode)
        if scalar:
            index = int(lower)
            blend = (float(radii) - float(RADII[index])) / float(RADII[index+1] - RADII[index])
            low = upscaled(index)
            mixed = low if blend <= 1e-6 else Image.blend(low, upscaled(index + 1), blend)
            return np.asarray(mixed, np.float32) / 255.
        blend = np.clip((radii - RADII[lower]) / (RADII[lower + 1] - RADII[lower]), 0., 1.)
        result = Image.new(mode, (region[2], region[3]))
        images = {}
        for raw_index in np.unique(lower):
            index = int(raw_index)
            for needed in (index, index + 1):
                if needed not in images:
                    images[needed] = upscaled(needed)
            selected = lower == index
            alpha = np.zeros(radii.shape, np.uint8)
            alpha[selected] = np.rint(blend[selected] * 255.).astype(np.uint8)
            mixed = Image.composite(images[index + 1], images[index], Image.fromarray(alpha, "L"))
            result.paste(mixed, (0, 0), Image.fromarray(selected.astype(np.uint8) * 255, "L"))
        return np.asarray(result, np.float32) / 255.
