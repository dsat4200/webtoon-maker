"""Deterministic dithering and alpha-safe unsharp masking on premultiplied RGB."""
from functools import lru_cache

import numpy as np
from scipy.ndimage import gaussian_filter


def _rgb(source):
    return np.divide(source[..., :3], source[..., 3:4],
                     out=np.zeros_like(source[..., :3]), where=source[..., 3:4] > 1e-7)


@lru_cache(maxsize=3)
def bayer_matrix(size):
    matrix = np.zeros((1, 1), dtype=np.float32)
    while matrix.shape[0] < size:
        matrix = np.block([[matrix * 4, matrix * 4 + 2],
                           [matrix * 4 + 3, matrix * 4 + 1]])
    matrix = (matrix + .5) / (size * size) - .5
    matrix.setflags(write=False)
    return matrix


def dither(source, modifier, *, strength=None, levels=None, pixel_size=None, origin=(0, 0)):
    """Quantize channels with a stable pattern; no global random generator state."""
    strength = modifier.strength if strength is None else strength
    levels = modifier.levels if levels is None else levels
    pixel_size = modifier.pixel_size if pixel_size is None else pixel_size
    height, width = source.shape[:2]
    yy, xx = np.ogrid[:height, :width]
    xx = xx + origin[0]
    yy = yy + origin[1]
    spacing = np.clip(np.asarray(pixel_size, np.float32), 1., 32.)
    x = np.floor(xx / spacing).astype(np.int64)
    y = np.floor(yy / spacing).astype(np.int64)
    if modifier.method == "ordered":
        matrix = bayer_matrix(modifier.matrix_size)
        threshold = matrix[y % modifier.matrix_size, x % modifier.matrix_size]
    else:
        # A coordinate hash makes repeat renders and independent process jobs identical.
        value = (x.astype(np.uint64) * np.uint64(0x1f123bb5)
                 ^ y.astype(np.uint64) * np.uint64(0x5f356495)
                 ^ np.uint64(modifier.seed)) & np.uint64(0xffffffff)
        value ^= value >> np.uint64(16)
        value = (value * np.uint64(0x7feb352d)) & np.uint64(0xffffffff)
        value ^= value >> np.uint64(15)
        value = (value * np.uint64(0x846ca68b)) & np.uint64(0xffffffff)
        value ^= value >> np.uint64(16)
        threshold = value.astype(np.float64) / 4294967296. - .5
    rgb = _rgb(source)
    if modifier.monochrome:
        rgb = np.sum(rgb * np.asarray([.2126, .7152, .0722], np.float32), axis=2)[..., None]
    steps = np.rint(np.clip(np.asarray(levels, np.float32), 2, 256)) - 1
    if steps.ndim:
        steps = steps[..., None]
    offset = threshold * np.clip(np.asarray(strength, np.float32), 0., 100.) / 100.
    color = np.clip(np.floor(rgb * steps + .5 + offset[..., None]) / steps, 0., 1.)
    result = source.copy()
    result[..., :3] = color * source[..., 3:4]
    return result


def _normalized_blur(source, rgb, radius):
    if radius <= 0:
        return rgb
    blurred = gaussian_filter(source, (radius, radius, 0), mode="nearest")
    return np.divide(blurred[..., :3], blurred[..., 3:4], out=rgb.copy(),
                     where=blurred[..., 3:4] > 1e-7)


def sharpen(source, *, strength=100., radius=2., threshold=0.):
    """Unsharp masking with normalized alpha, including continuously masked radii."""
    strength = np.clip(np.asarray(strength, np.float32), 0., 500.) / 100.
    radius = np.clip(np.asarray(radius, np.float32), 0., 20.)
    if np.max(strength) <= 0 or np.max(radius) <= 0:
        return source.copy()
    rgb = _rgb(source)
    if radius.ndim == 0 or np.min(radius) == np.max(radius):
        blurred = _normalized_blur(source, rgb, float(np.max(radius)))
    else:
        # Only needed Gaussian levels are built. Two adjacent levels yield a
        # continuous spatial radius without a Gaussian convolution per pixel.
        radii = np.asarray([0., 1., 2., 4., 8., 12., 20.], np.float32)
        lower = np.clip(np.searchsorted(radii, radius, side="right") - 1, 0, len(radii) - 2)
        fraction = (radius - radii[lower]) / (radii[lower + 1] - radii[lower])
        blurred = np.zeros_like(rgb)
        for index, sigma in enumerate(radii):
            weight = np.where(lower == index, 1. - fraction, 0.)
            weight += np.where(lower + 1 == index, fraction, 0.)
            if np.any(weight > 0):
                blurred += _normalized_blur(source, rgb, float(sigma)) * weight[..., None]
    detail = rgb - blurred
    cutoff = np.clip(np.asarray(threshold, np.float32), 0., 100.) / 100.
    detail *= (np.max(np.abs(detail), axis=2) >= cutoff)[..., None]
    if strength.ndim:
        strength = strength[..., None]
    result = source.copy()
    result[..., :3] = np.clip(rgb + detail * strength, 0., 1.) * source[..., 3:4]
    return result
