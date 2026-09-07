"""Tiled, premultiplied circular integration in document coordinates."""
from __future__ import annotations

import math
import numpy as np
from scipy.ndimage import map_coordinates
from PySide6.QtCore import QPointF


class RadialRenderCancelled(Exception):
    pass


def _map(transform, x, y):
    denominator = transform.m13()*x + transform.m23()*y + transform.m33()
    denominator = np.where(np.abs(denominator) < 1e-12, np.nan, denominator)
    return ((transform.m11()*x+transform.m21()*y+transform.m31())/denominator,
            (transform.m12()*x+transform.m22()*y+transform.m32())/denominator)


def radial_blur(original, center, angle, world_to_image, *, cancelled=None, spacing=.75,
                output_shape=None, output_origin=(0., 0.)):
    """Average a symmetric angular shutter; extra memory is bounded per tile.

    Sample density follows output-pixel arc length, not an arbitrary fixed
    sample cap. Out-of-source samples are transparent, never clamped edges.
    Coordinates refer to pixel centers through the complete projective mapping.
    """
    angle = np.clip(np.asarray(angle, dtype=np.float32), 0., 360.)
    if float(np.max(angle)) <= 1e-6 and output_shape is None and output_origin == (0., 0.):
        return original.copy()
    inverse, valid = world_to_image.inverted()
    if not valid:
        return original.copy()
    from comic_editor.ui.shape_contours import transform_stretch
    height, width = output_shape or original.shape[:2]
    result = np.zeros((height, width, 4), dtype=original.dtype)
    center_x, center_y = center
    for top in range(0, height, 96):
        for left in range(0, width, 96):
            if cancelled is not None and cancelled():
                raise RadialRenderCancelled()
            bottom, right = min(top+96, height), min(left+96, width)
            yy, xx = np.mgrid[top:bottom, left:right].astype(np.float64)
            world_x, world_y = _map(inverse, xx+.5+output_origin[0], yy+.5+output_origin[1])
            dx, dy = world_x-center_x, world_y-center_y
            angles = angle if angle.ndim == 0 else angle[top:bottom, left:right]
            radians = angles*(math.pi/180)
            radius = float(np.nanmax(np.hypot(dx, dy)))
            # Conservative affine/projective scale over this tile's world box.
            from PySide6.QtCore import QRectF
            bounds = QRectF(QPointF(float(np.nanmin(world_x)), float(np.nanmin(world_y))),
                            QPointF(float(np.nanmax(world_x)), float(np.nanmax(world_y))))
            stretch = transform_stretch(world_to_image, bounds)
            count = max(2, math.ceil(radius*float(np.max(radians))*stretch/spacing))
            tile = result[top:bottom, left:right]
            for sample in range(count):
                if sample % 16 == 0 and cancelled is not None and cancelled():
                    raise RadialRenderCancelled()
                theta = radians*((sample+.5)/count-.5)
                sine, cosine = np.sin(theta), np.cos(theta)
                sx, sy = _map(world_to_image, center_x+dx*cosine-dy*sine,
                             center_y+dx*sine+dy*cosine)
                coordinates = np.stack((sy-.5, sx-.5))
                for channel in range(4):
                    tile[..., channel] += map_coordinates(original[..., channel], coordinates,
                        order=1, mode="grid-constant", cval=0., prefilter=False)
            tile /= count
            # A zero-angle mask region is an exact identity, including its
            # floating-point values, regardless of neighboring sample counts.
            zero = np.broadcast_to(angles <= 1e-6, tile.shape[:2])
            if np.any(zero):
                coordinates = np.stack((yy+output_origin[1], xx+output_origin[0]))
                for channel in range(4):
                    identity = map_coordinates(original[..., channel], coordinates,
                        order=1, mode="grid-constant", cval=0., prefilter=False)
                    tile[..., channel][zero] = identity[zero]
    # Roundoff must never create invalid premultiplied output.
    result = np.clip(result, 0., 1.)
    result[..., :3] = np.minimum(result[..., :3], result[..., 3:4])
    return result
