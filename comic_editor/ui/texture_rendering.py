"""Document-addressed texture sampling for stable cropped/projective overlays."""
import math
import numpy as np
from scipy.ndimage import map_coordinates
from PySide6.QtCore import QPointF
from PySide6.QtGui import QPolygonF, QTransform

from comic_editor.core.texture_library import texture_image


def transformed_texture(modifier, width, height, origin, world_to_image):
    from comic_editor.ui.modifier_rendering import _qimage_premultiplied
    placement = world_to_image if world_to_image is not None else QTransform.fromTranslate(-origin[0], -origin[1])
    world_quad = QPolygonF([QPointF(*p) for p in modifier.texture_quad])
    quad = placement.map(world_quad)
    edge_w = max((quad[1] - quad[0]).manhattanLength(), (quad[2] - quad[3]).manhattanLength())
    edge_h = max((quad[3] - quad[0]).manhattanLength(), (quad[2] - quad[1]).manhattanLength())
    sample_w = max(1, min(4096, math.ceil(edge_w - 1e-6)))
    sample_h = max(1, min(4096, math.ceil(edge_h - 1e-6)))
    decoded = texture_image(modifier.texture_data, sample_w, sample_h)
    if decoded.isNull():
        return None, None
    source = _qimage_premultiplied(decoded)
    unit = QPolygonF([QPointF(0, 0), QPointF(1, 0), QPointF(1, 1), QPointF(0, 1)])
    inverse, valid = QTransform.quadToQuad(unit, world_quad).inverted()
    to_world, placement_valid = placement.inverted()
    if not valid or not placement_valid:
        return None, None
    texture = np.empty((height, width, 4), np.float32)
    coverage = np.empty((height, width, 1), np.float32)
    # Pixel-center sampling is independent of the output crop origin. Bounded
    # row chunks keep coordinate work small even for long raster drawings.
    xs = np.arange(width, dtype=np.float64)[None, :] + .5
    for first in range(0, height, max(1, 131072 // width)):
        last = min(height, first + max(1, 131072 // width))
        ys = np.arange(first, last, dtype=np.float64)[:, None] + .5
        world_denominator = to_world.m13() * xs + to_world.m23() * ys + to_world.m33()
        world_x = (to_world.m11() * xs + to_world.m21() * ys + to_world.dx()) / world_denominator
        world_y = (to_world.m12() * xs + to_world.m22() * ys + to_world.dy()) / world_denominator
        denominator = inverse.m13() * world_x + inverse.m23() * world_y + inverse.m33()
        u = np.divide(inverse.m11() * world_x + inverse.m21() * world_y + inverse.dx(), denominator,
                      out=np.full((last-first, width), -1.), where=np.abs(denominator) > 1e-12)
        v = np.divide(inverse.m12() * world_x + inverse.m22() * world_y + inverse.dy(), denominator,
                      out=np.full((last-first, width), -1.), where=np.abs(denominator) > 1e-12)
        inside = (u >= 0) & (u < 1) & (v >= 0) & (v < 1)
        coordinates = [np.clip(v * sample_h - .5, 0, sample_h - 1),
                       np.clip(u * sample_w - .5, 0, sample_w - 1)]
        for channel in range(4):
            texture[first:last, :, channel] = map_coordinates(source[..., channel], coordinates,
                order=1, mode="nearest", prefilter=False) * inside
        coverage[first:last, :, 0] = inside
    return texture, coverage
