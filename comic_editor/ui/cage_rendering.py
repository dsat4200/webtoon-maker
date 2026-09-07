"""Tiled inverse mesh sampling in premultiplied RGBA, shared by preview and bake."""
import math
import numpy as np
from scipy.ndimage import map_coordinates
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform
from comic_editor.core.cage import map_points
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _premultiplied_qimage


def warp_path(path, grid, tolerance=.2):
    """Adaptively transport path edges, including bends inside straight edges."""
    from PySide6.QtCore import QPointF
    from PySide6.QtGui import QPainterPath
    result = QPainterPath()
    result.setFillRule(path.fillRule())
    def segment(a, b, fa, fb, depth=0):
        ratios = np.asarray((.25, .5, .75))[:, None]
        interior = a+(b-a)*ratios
        mapped = map_points(grid, interior)
        linear = fa+(fb-fa)*ratios
        if np.linalg.norm(mapped-linear, axis=1).max() > tolerance and depth < 12:
            segment(a, interior[1], fa, mapped[1], depth+1)
            segment(interior[1], b, mapped[1], fb, depth+1)
        else:
            result.lineTo(QPointF(*fb))
    scale = QTransform.fromScale(4., 4.)
    for polygon in path.toSubpathPolygons(scale):
        points = np.asarray([(p.x()/4, p.y()/4) for p in polygon])
        if not len(points):
            continue
        mapped = map_points(grid, points)
        result.moveTo(QPointF(*mapped[0]))
        for i in range(1, len(points)):
            segment(points[i-1], points[i], mapped[i-1], mapped[i])
        result.closeSubpath()
    return result


def cubic_sample(pixels, mx, my):
    """Interpolating Catmull–Rom sampling, identical to the OpenGL shader."""
    ix, iy = np.floor(mx).astype(np.int32), np.floor(my).astype(np.int32)
    def weights(t):
        return (-.5*t+t*t-.5*t*t*t, 1-2.5*t*t+1.5*t*t*t,
                .5*t+2*t*t-1.5*t*t*t, -.5*t*t+.5*t*t*t)
    wx, wy = weights(mx-ix), weights(my-iy)
    result = np.zeros((*mx.shape, 4), np.float32)
    for j in range(4):
        yy = iy+j-1
        for i in range(4):
            xx = ix+i-1
            valid = (xx >= 0) & (xx < pixels.shape[1]) & (yy >= 0) & (yy < pixels.shape[0])
            weight = (wx[i]*wy[j]*valid)[..., None]
            result += pixels[np.clip(yy, 0, pixels.shape[0]-1), np.clip(xx, 0, pixels.shape[1]-1)]*weight
    return result


def nearest_sample(pixels, mx, my):
    # Share a tiny tie-break tolerance with the float32 GPU interpolator.
    x, y = np.floor(mx+.5001).astype(np.int32), np.floor(my+.5001).astype(np.int32)
    valid = (x >= 0) & (x < pixels.shape[1]) & (y >= 0) & (y < pixels.shape[0])
    return pixels[np.clip(y, 0, pixels.shape[0]-1), np.clip(x, 0, pixels.shape[1]-1)]*valid[..., None]


def transform_points(transform, points):
    points = np.asarray(points, dtype=np.float64)
    x, y = points[..., 0], points[..., 1]
    denominator = transform.m13()*x + transform.m23()*y + transform.m33()
    return np.stack(((transform.m11()*x+transform.m21()*y+transform.m31())/denominator,
                     (transform.m12()*x+transform.m22()*y+transform.m32())/denominator), axis=-1)


def mesh_for_image(grid, bounds, local_to_world, *, draft=False):
    inverse, valid = local_to_world.inverted()
    if not valid:
        raise ValueError("Cannot transform an object with a singular placement")
    # Subdivision handles smooth cages; a straight 2x2 cage is two triangles.
    subdivisions = (2 if draft else 4) if grid.smoothness > 0 else 1
    nx, ny = (grid.columns-1)*subdivisions+1, (grid.rows-1)*subdivisions+1
    xx, yy = np.meshgrid(np.linspace(bounds.left(), bounds.right(), nx),
                         np.linspace(bounds.top(), bounds.bottom(), ny))
    source = np.stack((xx, yy), axis=-1).reshape(-1, 2)
    destination = transform_points(inverse, map_points(grid, transform_points(local_to_world, source)))
    c = np.arange(nx*ny).reshape(ny, nx)[:-1, :-1].ravel()
    faces = np.concatenate((np.stack((c, c+1, c+nx+1), axis=1),
                            np.stack((c, c+nx+1, c+nx), axis=1)))
    return source, destination, faces


def warp_image(image, bounds, grid, local_to_world=None, output_bounds=None, cancelled=None, *, pixel_scale=1.):
    """Inverse rasterize triangles in bounded strips; folded faces use stable order.

    Work is proportional to covered destination pixels. Sampling uses compiled
    SciPy kernels and 128-row strips, avoiding a full-image float coordinate map.
    """
    local_to_world = local_to_world or QTransform()
    source, destination, faces = mesh_for_image(grid, bounds, local_to_world, draft=pixel_scale < 1.)
    if output_bounds is None:
        low, high = destination.min(axis=0), destination.max(axis=0)
        output_bounds = QRectF(*low, *(high-low))
    # Remove floating-point noise at exact integer edges before floor/ceil.
    output_bounds = QRectF(*(round(v, 8) for v in (output_bounds.x(), output_bounds.y(), output_bounds.width(), output_bounds.height())))
    output_bounds = QRectF(output_bounds.toAlignedRect())
    width, height = max(1, math.ceil(output_bounds.width()*pixel_scale)), max(1, math.ceil(output_bounds.height()*pixel_scale))
    if width*height > 64*1024*1024:
        raise ValueError("Cage result is too large. Reduce the deformation before applying it.")
    if pixel_scale < 1.:
        from PySide6.QtCore import Qt
        image = image.scaled(max(1, math.ceil(image.width()*pixel_scale)), max(1, math.ceil(image.height()*pixel_scale)), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    pixels = _qimage_premultiplied(image)
    output = np.zeros((height, width, 4), np.uint8)
    src = (source-np.array((bounds.x(), bounds.y()))) * (image.width()/bounds.width(), image.height()/bounds.height()) - .5
    dst = (destination - (output_bounds.x(), output_bounds.y())) * (width/max(1, output_bounds.width()), height/max(1, output_bounds.height()))
    triangles = dst[faces]
    lower, upper = triangles.min(axis=1), triangles.max(axis=1)
    order = {"nearest": 0, "bilinear": 1, "bicubic": 3}[grid.interpolation] if pixel_scale == 1. else 1
    filtered = [pixels[..., ch] for ch in range(4)]
    for top in range(0, height, 128):
        if cancelled is not None and cancelled():
            return None
        bottom = min(height, top+128)
        mx = np.full((bottom-top, width), -1e6, np.float32)
        my = np.full_like(mx, -1e6)
        candidates = np.flatnonzero((upper[:, 1] >= top) & (lower[:, 1] <= bottom))
        for face in candidates:
            x0 = max(0, math.floor(lower[face, 0]))
            x1 = min(width, math.ceil(upper[face, 0]))
            y0 = max(top, math.floor(lower[face, 1]))
            y1 = min(bottom, math.ceil(upper[face, 1]))
            if x1 <= x0 or y1 <= y0:
                continue
            a, b, c = triangles[face]
            ab, ac = b-a, c-a
            det = ab[0]*ac[1]-ab[1]*ac[0]
            if abs(det) < 1e-10:
                continue
            xx = np.arange(x0, x1, dtype=np.float32)[None, :] + .5-a[0]
            yy = np.arange(y0, y1, dtype=np.float32)[:, None] + .5-a[1]
            u = (xx*ac[1]-yy*ac[0])/det
            v = (ab[0]*yy-ab[1]*xx)/det
            inside = (u >= -1e-6) & (v >= -1e-6) & (u+v <= 1+1e-6)
            sa, sb, sc = src[faces[face]]
            sx, sy = sa[:, None, None] + (sb-sa)[:, None, None]*u + (sc-sa)[:, None, None]*v
            mx[y0-top:y1-top, x0:x1][inside] = sx[inside]
            my[y0-top:y1-top, x0:x1][inside] = sy[inside]
        coordinates = np.stack((my, mx))
        values = (cubic_sample(pixels, mx, my) if order == 3 else nearest_sample(pixels, mx, my) if order == 0 else np.stack([
            map_coordinates(channel, coordinates, order=order, mode="grid-constant", cval=0, prefilter=False)
            for channel in filtered], axis=-1))
        values = np.clip(values, 0, 1)
        values[..., :3] = np.minimum(values[..., :3], values[..., 3:4])
        output[top:bottom] = np.rint(values*255).astype(np.uint8)
    from PySide6.QtGui import QImage
    result = QImage(output.data, width, height, width*4, QImage.Format_RGBA8888_Premultiplied).copy()
    return result, output_bounds
