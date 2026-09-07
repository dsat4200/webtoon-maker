"""Alpha-weighted RGB guided filtering before hue classification.

Color guidance follows He, Sun and Tang, Guided Image Filtering (ECCV 2010),
equations 14–16: https://people.csail.mit.edu/kaiming/eccv10/index.html
Two passes suppress fine texture without turning strong edges into blur ramps.
"""
from __future__ import annotations

from collections import OrderedDict
import hashlib
import math
from threading import RLock

import numpy as np
from scipy.ndimage import uniform_filter


def simplify_padding(modifier) -> int:
    if not modifier.simplify_enabled or modifier.simplify_strength <= 0:
        return 0
    # Each guided pass averages local moments and then local coefficients.
    return 4 * math.ceil(modifier.simplify_radius)


def _guided_tile(rgba, radius, epsilon):
    rgb = rgba[..., :3]
    alpha = rgba[..., 3]
    size = 2 * radius + 1
    coverage = uniform_filter(alpha, size=size, mode="nearest")
    denominator = np.maximum(coverage, 1e-8)

    def mean(values):
        shape = (...,) + (None,) * (values.ndim - 2)
        weights = alpha[shape]
        return uniform_filter(values * weights,
            size=(size, size) + (1,) * (values.ndim - 2), mode="nearest") / denominator[shape]

    mu = mean(rgb)
    # Double precision keeps the covariance positive near almost-flat grays,
    # where tiny numerical color changes would be amplified by hue mapping.
    covariance = np.empty((*rgb.shape[:2], 3, 3), dtype=np.float64)
    for i in range(3):
        for j in range(i, 3):
            value = mean(rgb[..., i] * rgb[..., j]) - mu[..., i] * mu[..., j]
            covariance[..., i, j] = covariance[..., j, i] = value
    identity = np.eye(3)
    coefficients = identity - epsilon * np.linalg.inv(covariance + epsilon * identity)
    intercept = mu - np.einsum("...ij,...j->...i", coefficients, mu)
    return np.clip(np.einsum("...ij,...j->...i", mean(coefficients), rgb) + mean(intercept), 0., 1.)


def _smooth(rgba, radius, tolerance, strength):
    radius = max(1, math.ceil(radius))
    epsilon = max(1e-6, (tolerance / 400.) ** 2)
    current = rgba
    height, width = rgba.shape[:2]
    # Tile with the full dependency halo; no seams and bounded working memory.
    for _ in range(2):
        result = current.copy()
        halo = 2 * radius
        for top in range(0, height, 256):
            bottom = min(height, top + 256)
            for left in range(0, width, 256):
                right = min(width, left + 256)
                y0, y1 = max(0, top - halo), min(height, bottom + halo)
                x0, x1 = max(0, left - halo), min(width, right + halo)
                tile = current[y0:y1, x0:x1].astype(np.float64)
                filtered = _guided_tile(tile, radius, epsilon)
                result[top:bottom, left:right, :3] = filtered[top-y0:bottom-y0, left-x0:right-x0]
        current = result
    blend = strength / 100.
    current[..., :3] = rgba[..., :3] * (1. - blend) + current[..., :3] * blend
    # The editor stores 8-bit color. Don't let sub-byte chroma left by the
    # filter turn neutral regions into arbitrary hue speckles.
    current[..., :3] = np.rint(current[..., :3] * 255.) / 255.
    current[..., 3] = rgba[..., 3]
    return current


class SimplifyColorCache:
    """Reuse preprocessing while the user edits hue handles or output colors."""

    def __init__(self, budget=64 * 1024 * 1024):
        self.budget = budget
        self.bytes = 0
        self.computations = 0
        self._values = OrderedDict()
        self._lock = RLock()

    def smooth(self, rgba, radius, tolerance, strength):
        if radius <= 0 or strength <= 0 or not rgba.size:
            return rgba
        # A bounded histogram sample can have a fractional source-pixel radius.
        strength *= min(1., radius)
        pixels = np.ascontiguousarray(rgba, dtype=np.float32)
        key = (pixels.shape, math.ceil(radius), float(tolerance), float(strength),
               hashlib.blake2b(pixels.data, digest_size=16).digest())
        with self._lock:
            cached = self._values.pop(key, None)
            if cached is not None:
                self._values[key] = cached
                return cached
        result = _smooth(pixels, radius, tolerance, strength)
        result.setflags(write=False)
        with self._lock:
            self.computations += 1
            if result.nbytes <= self.budget and key not in self._values:
                while self._values and self.bytes + result.nbytes > self.budget:
                    _, old = self._values.popitem(last=False)
                    self.bytes -= old.nbytes
                self._values[key] = result
                self.bytes += result.nbytes
        return result


_cache = SimplifyColorCache()


def simplify_colors(rgba, modifier, pixel_scale=1.):
    if not modifier.simplify_enabled:
        return rgba
    return _cache.smooth(rgba, modifier.simplify_radius * pixel_scale,
                         modifier.simplify_tolerance, modifier.simplify_strength)
