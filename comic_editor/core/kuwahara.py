"""Alpha-aware Kuwahara filters in bounded working tiles.

Original: four overlapping square quadrants, selecting the least RGB variance.
Papari et al. (2007): convolution-smoothed circular sector weights and soft
variance selection. Kyprianidis et al. (2010): polynomial sector weights on
structure-tensor-oriented ellipses, as discussed in Acerola's implementation.
References: https://www.kyprianidis.com/p/tpcg2010/
https://github.com/GarrettGunnell/Post-Processing/tree/main/Assets/Kuwahara%20Filter

The original uses summed-area tables, so large radii do not add sample loops.
Sector filters cache normalized kernels and use explicit quadrature quality;
sample counts are independent of the radius. All size fields change the
sampling support itself. Reduced processing resolution is an explicit saved
setting and never imposed silently during export.
"""
from __future__ import annotations

from collections import OrderedDict
from functools import lru_cache
import hashlib
import math
from threading import RLock

import numpy as np
from scipy.ndimage import gaussian_filter, map_coordinates, sobel, zoom


KUWAHARA_VARIANTS = {
    "original": "Original Kuwahara",
    "generalized": "Papari Generalized Kuwahara",
    "anisotropic": "Anisotropic Kuwahara",
}
KUWAHARA_QUALITY = {"draft": 4, "balanced": 6, "high": 10}


def _straight_rgb(rgba):
    return np.divide(rgba[..., :3], rgba[..., 3:4],
                     out=np.zeros_like(rgba[..., :3]), where=rgba[..., 3:4] > 1e-8)


def _moments(rgba):
    """Alpha weights keep transparent black out of color means/variances."""
    result = np.empty((*rgba.shape[:2], 7), dtype=np.float64)
    result[..., :3] = rgba[..., :3]
    result[..., 3:6] = rgba[..., :3] * _straight_rgb(rgba)
    result[..., 6] = rgba[..., 3]
    return result


def _original(rgba, size, cancelled=None):
    height, width = rgba.shape[:2]
    radii = np.broadcast_to(np.asarray(size, dtype=np.float32), (height, width))
    result = _straight_rgb(rgba)
    # Independent tiles bound the integral tables even on a whole chapter.
    for top in range(0, height, 192):
        bottom = min(height, top + 192)
        for left in range(0, width, 192):
            if cancelled is not None and cancelled():
                return None
            right = min(width, left + 192)
            radius = radii[top:bottom, left:right]
            if not np.any(radius > 0):
                continue
            halo = math.ceil(float(np.max(radius)))
            y0, y1 = max(0, top-halo), min(height, bottom+halo)
            x0, x1 = max(0, left-halo), min(width, right+halo)
            # Nearest extension matches the sector sampler at image edges.
            padding = ((max(0, halo-top), max(0, bottom+halo-height)),
                       (max(0, halo-left), max(0, right+halo-width)), (0, 0))
            tile = np.pad(rgba[y0:y1, x0:x1], padding, mode="edge")
            summed = np.pad(_moments(tile).cumsum(0).cumsum(1), ((1, 0), (1, 0), (0, 0)))
            yy, xx = np.mgrid[halo:halo+bottom-top, halo:halo+right-left]

            def at_radius(r):
                best = np.full(radius.shape, np.inf)
                colors = result[top:bottom, left:right].copy()
                for sy, sx in ((-1, -1), (-1, 1), (1, -1), (1, 1)):
                    xa, xb = (xx-r, xx+1) if sx < 0 else (xx, xx+r+1)
                    ya, yb = (yy-r, yy+1) if sy < 0 else (yy, yy+r+1)
                    moment = summed[yb, xb] - summed[ya, xb] - summed[yb, xa] + summed[ya, xa]
                    weight = moment[..., 6]
                    mean = moment[..., :3] / np.maximum(weight[..., None], 1e-12)
                    variance = np.maximum(0., moment[..., 3:6] / np.maximum(weight[..., None], 1e-12) - mean*mean).sum(2)
                    choose = (weight > 1e-8) & (variance < best)
                    colors[choose], best[choose] = mean[choose], variance[choose]
                return colors

            lower = np.floor(radius).astype(np.int32)
            low = at_radius(lower)
            fraction = radius - lower
            if np.any(fraction > 1e-6):
                low += (at_radius(np.ceil(radius).astype(np.int32)) - low) * fraction[..., None]
            result[top:bottom, left:right] = low
    return result


@lru_cache(maxsize=48)
def _sector_kernel(variant, quality, overlap):
    """Eight cached normalized sectors; positions have a unit disc support."""
    resolution = KUWAHARA_QUALITY[quality]
    y, x = np.mgrid[-resolution:resolution+1, -resolution:resolution+1] / resolution
    inside = x*x + y*y <= 1.000001
    x, y = x[inside].astype(np.float32), y[inside].astype(np.float32)
    weights = np.empty((8, x.size), dtype=np.float32)
    if variant == "generalized":
        # Papari's K_i = (characteristic sector_i * Gaussian_rho) Gaussian_sigma.
        # Construct once at high resolution, then sample the cached texture.
        extent, grid = 1.5, 129
        yy, xx = np.mgrid[-extent:extent:complex(grid), -extent:extent:complex(grid)]
        angle = np.arctan2(yy, xx)
        sigma = (.03 + .27 * overlap / 100.) * (grid-1) / (2*extent)
        coordinates = np.array([(y+extent)*(grid-1)/(2*extent),
                                (x+extent)*(grid-1)/(2*extent)])
        for sector in range(8):
            relative = (angle - sector*math.pi/4 + math.pi) % (2*math.pi) - math.pi
            characteristic = ((np.abs(relative) <= math.pi/8) & (xx*xx+yy*yy <= 1.0)).astype(np.float32)
            smooth = gaussian_filter(characteristic, sigma, mode="constant")
            weights[sector] = map_coordinates(smooth, coordinates, order=1, prefilter=False)
        weights *= np.exp(-2. * (x*x+y*y))[None, :]
    else:
        # Rotating a single polynomial produces the eight symmetric sectors.
        # The paper's support has radius .5 and zero crossing 3 pi / 16.
        zeta = .05 + .55 * overlap / 100.
        crossing = 3 * math.pi / 16
        eta = (zeta + math.cos(crossing)) / math.sin(crossing)**2
        for sector in range(8):
            angle = sector * math.pi/4
            vx = .5 * (x*math.cos(angle) - y*math.sin(angle))
            vy = .5 * (x*math.sin(angle) + y*math.cos(angle))
            weights[sector] = np.maximum(0., vy + zeta - eta*vx*vx)**2
        weights *= (np.exp(-3.125 * .25*(x*x+y*y)) / np.maximum(weights.sum(0), 1e-12))[None, :]
    weights = np.ascontiguousarray(weights)
    for array in (x, y, weights):
        array.setflags(write=False)
    return x, y, weights


def _orientation(rgba, smoothing):
    """RGB structure tensor; tangent is the minor-eigenvalue direction."""
    rgb = _straight_rgb(rgba)
    # Extend neighboring colors across transparency before differentiating,
    # without introducing an alpha boundary as an artificial black color edge.
    alpha = rgba[..., 3]
    coverage = gaussian_filter(alpha, .7, mode="nearest")
    for channel in range(3):
        filled = gaussian_filter(rgba[..., channel], .7, mode="nearest") / np.maximum(coverage, 1e-8)
        rgb[..., channel] = np.where(alpha > 1e-8, rgb[..., channel], filled)
    dx = sobel(rgb, axis=1, mode="nearest") / 8.
    dy = sobel(rgb, axis=0, mode="nearest") / 8.
    a = gaussian_filter(np.sum(dx*dx, axis=2), smoothing, mode="nearest")
    b = gaussian_filter(np.sum(dy*dy, axis=2), smoothing, mode="nearest")
    c = gaussian_filter(np.sum(dx*dy, axis=2), smoothing, mode="nearest")
    gap = np.sqrt(np.maximum(0., (a-b)**2 + 4*c*c))
    coherence = gap / np.maximum(a+b, 1e-8)
    angle = .5*np.arctan2(2*c, a-b) + math.pi/2
    return np.cos(angle), np.sin(angle), np.clip(coherence, 0., 1.)


def _sectors(rgba, size, modifier, cancelled=None, origin=(0, 0)):
    height, width = rgba.shape[:2]
    sx, sy, weights = _sector_kernel(modifier.variant, modifier.quality, modifier.overlap)
    radius = np.broadcast_to(np.asarray(size, dtype=np.float32), (height, width))
    result = _straight_rgb(rgba)
    if modifier.variant == "anisotropic" and modifier.anisotropy > 0:
        cosine, sine, coherence = _orientation(rgba, modifier.tensor_radius)
    else:
        cosine = sine = coherence = None
    # Fixed quadrature and tiles avoid a radius-squared per-pixel Python loop.
    # Largest high-quality tile uses ~21 MB of moment samples.
    for top in range(0, height, 48):
        bottom = min(height, top+48)
        for left in range(0, width, 48):
            if cancelled is not None and cancelled():
                return None
            right = min(width, left+48)
            radii = radius[top:bottom, left:right]
            if not np.any(radii > 0) or not np.any(rgba[top:bottom, left:right, 3] > 0):
                continue
            yy, xx = np.mgrid[top:bottom, left:right].astype(np.float32)
            yy += origin[1]
            xx += origin[0]
            u, v = sx[:, None, None]*radii, sy[:, None, None]*radii
            if cosine is not None:
                cs, sn = cosine[top:bottom, left:right], sine[top:bottom, left:right]
                stretch = 1 + coherence[top:bottom, left:right]*modifier.anisotropy/100.
                u, v = u*stretch, v/stretch
                u, v = u*cs-v*sn, u*sn+v*cs
            coordinates = np.stack((yy+v-origin[1], xx+u-origin[0]))
            samples = np.empty((*u.shape, 7), dtype=np.float32)
            for channel in range(3):
                samples[..., channel] = map_coordinates(rgba[..., channel], coordinates,
                    order=1, mode="nearest", prefilter=False)
            samples[..., 6] = map_coordinates(rgba[..., 3], coordinates,
                order=1, mode="nearest", prefilter=False)
            samples[..., 3:6] = 0.
            np.divide(samples[..., :3]**2, samples[..., 6:7],
                out=samples[..., 3:6], where=samples[..., 6:7] > 1e-8)
            moments = (weights @ samples.reshape(sx.size, -1)).reshape(8, *radii.shape, 7)
            normal = np.maximum(moments[..., 6:7], 1e-10)
            mean = moments[..., :3] / normal
            variance = np.maximum(0., moments[..., 3:6] / normal - mean*mean).sum(-1)
            # Stable soft minimum, equivalent to 1/(1+(1000 h variance)^(q/2)).
            exponent = .5*modifier.sharpness * np.log(np.maximum(
                1000*modifier.hardness*variance, 1e-20))
            log_selection = -np.logaddexp(0., exponent)
            valid_sector = moments[..., 6] > 1e-8
            log_selection = np.where(valid_sector, log_selection, -1e10)
            log_selection -= np.max(log_selection, axis=0)
            selection = np.exp(log_selection)*valid_sector
            total = selection.sum(0)
            colors = np.sum(mean*selection[..., None], axis=0) / np.maximum(total[..., None], 1e-12)
            valid = (total > 0) & (radii > 0)
            result[top:bottom, left:right][valid] = colors[valid]
    return result


def _resize(array, shape):
    if array.shape[:2] == shape:
        return array.copy()
    factors = (shape[0]/array.shape[0], shape[1]/array.shape[1]) + (1.,)*(array.ndim-2)
    return zoom(array, factors, order=1, mode="nearest", prefilter=False)


def _filtered(rgba, size, modifier, cancelled=None, origin=(0, 0)):
    scale = modifier.processing_scale / 100.
    original_shape = rgba.shape[:2]
    shape = tuple(max(1, round(value*scale)) for value in original_shape)
    current = _resize(rgba, shape) if shape != original_shape else rgba
    # Scaling uses actual dimensions, so tiny thumbnail frames stay sensible.
    size_scale = math.sqrt((shape[0]/original_shape[0])*(shape[1]/original_shape[1]))
    radius = _resize(size, shape)*size_scale if np.ndim(size) else float(size)*size_scale
    from copy import copy
    settings = copy(modifier)
    settings.tensor_radius *= size_scale
    for _ in range(modifier.iterations):
        rgb = (_original(current, radius, cancelled) if modifier.variant == "original"
               else _sectors(current, radius, settings, cancelled, origin))
        if rgb is None:
            return None
        result = current.copy()
        result[..., :3] = np.clip(rgb, 0., 1.)*current[..., 3:4]
        current = result
    if shape != original_shape:
        current = _resize(current, original_shape)
        current[..., :3] = _straight_rgb(current)*rgba[..., 3:4]
    current[..., 3] = rgba[..., 3]
    if np.ndim(size):
        current[np.asarray(size) <= 0] = rgba[np.asarray(size) <= 0]
    return current


class KuwaharaCache:
    """Bounded reusable filter results independent of strength/intensity ramps."""

    def __init__(self, budget=64*1024*1024):
        self.budget, self.bytes = budget, 0
        self._values = OrderedDict()
        self._lock = RLock()

    def filtered(self, rgba, size, modifier, cancelled=None, origin=(0, 0)):
        if cancelled is not None and cancelled():
            return None
        pixels = np.ascontiguousarray(rgba, dtype=np.float32)
        radius = np.ascontiguousarray(size, dtype=np.float32) if np.ndim(size) else float(size)
        radius_key = (radius.shape, hashlib.blake2b(radius.data, digest_size=16).digest()) if np.ndim(radius) else radius
        key = (pixels.shape, hashlib.blake2b(pixels.data, digest_size=16).digest(), radius_key, tuple(origin),
               *(getattr(modifier, name) for name in ("variant", "quality", "processing_scale",
                   "sharpness", "hardness", "overlap", "anisotropy", "tensor_radius", "iterations")))
        with self._lock:
            cached = self._values.pop(key, None)
            if cached is not None:
                self._values[key] = cached
                return cached
        result = _filtered(pixels, radius, modifier, cancelled, origin)
        if result is None or cancelled is not None and cancelled():
            return None
        result.setflags(write=False)
        with self._lock:
            if result.nbytes <= self.budget and key not in self._values:
                while self._values and self.bytes+result.nbytes > self.budget:
                    _, old = self._values.popitem(last=False)
                    self.bytes -= old.nbytes
                self._values[key] = result
                self.bytes += result.nbytes
        return result


_cache = KuwaharaCache()


def apply_kuwahara(rgba, modifier, *, size=None, strength=None, cancelled=None, origin=(0, 0)):
    """Return premultiplied float RGBA, keeping incoming alpha exactly."""
    size = modifier.size if size is None else size
    strength = modifier.strength if strength is None else strength
    if not rgba.size or np.max(size) <= 0 or np.max(strength) <= 0:
        return rgba
    filtered = _cache.filtered(rgba, size, modifier, cancelled, origin)
    if filtered is None:
        return None
    blend = np.clip(np.asarray(strength, dtype=np.float32)/100., 0., 1.)
    if blend.ndim:
        blend = blend[..., None]
    output = rgba.copy()
    output[..., :3] += (filtered[..., :3] - rgba[..., :3])*blend
    return output
