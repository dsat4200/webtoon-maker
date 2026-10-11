"""Preserved dense native Kuwahara math from the starting build.

Independent oracle for allocation changes; do not update with production.
Source commit: 8f704f4feb5cff07e3870d900f2bc09e94afc747.
"""
import math
import numpy as np

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
