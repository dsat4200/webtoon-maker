"""World-anchored, alpha-safe raster distortions shared by preview and export.

The image represents ``bounds`` in object-local coordinates. Spatial controls
live in document coordinates; changing preview resolution never changes a warp.
The caller owns modifier intensity and masks. All sampling is premultiplied and
large outputs are processed in cancellable strips.
"""
from __future__ import annotations

import base64
from collections import OrderedDict
from dataclasses import dataclass
import math

import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QTransform
from scipy.interpolate import PchipInterpolator
from scipy.ndimage import map_coordinates, sobel, spline_filter

from comic_editor.core.cage import CageGrid, homography, map_points, project, tessellate
from comic_editor.ui.distort_equations import evaluate_equation


_MAX_PIXELS = 64 * 1024 * 1024
_QUAD = np.asarray(((0., 0.), (1., 0.), (1., 1.), (0., 1.)))


def _rgba(image):
    image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    data = np.frombuffer(image.constBits(), np.uint8, count=image.sizeInBytes()).reshape(image.height(), image.bytesPerLine())
    return data[:, :image.width() * 4].reshape(image.height(), image.width(), 4).astype(np.float32) / 255.


def _byte_pixels(array):
    array = np.nan_to_num(array, nan=0., posinf=1., neginf=0.)
    array[..., 3] = np.clip(array[..., 3], 0., 1.)
    array[..., :3] = np.clip(array[..., :3], 0., array[..., 3:4])
    return np.ascontiguousarray(np.rint(array * 255.).astype(np.uint8))


def _image(array):
    data = np.ascontiguousarray(array) if array.dtype == np.uint8 else _byte_pixels(array)
    height, width = data.shape[:2]
    return QImage(data.data, width, height, width * 4, QImage.Format.Format_RGBA8888_Premultiplied).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)


def _transform(transform, points):
    points = np.asarray(points, np.float64)
    x, y = points[..., 0], points[..., 1]
    with np.errstate(invalid="ignore", divide="ignore"):
        den = transform.m13() * x + transform.m23() * y + transform.m33()
        return np.stack(((transform.m11() * x + transform.m21() * y + transform.m31()) / den,
                         (transform.m12() * x + transform.m22() * y + transform.m32()) / den), axis=-1)


def _rect_points(bounds):
    return _QUAD * (bounds.width(), bounds.height()) + (bounds.x(), bounds.y())


def _geometry(bounds, modifier, transform):
    raw_frame = getattr(modifier, "frame", None)
    if raw_frame is None:
        world = _transform(transform, _rect_points(bounds))
        low, high = world.min(axis=0), world.max(axis=0)
        frame = np.asarray((*low, *(high - low)), np.float64)
    else:
        frame = np.asarray(raw_frame, np.float64)
    frame[2:] = np.maximum(frame[2:], 1e-6)
    raw_center = getattr(modifier, "center", None)
    center = np.asarray(raw_center if raw_center is not None else frame[:2] + frame[2:] / 2., np.float64)
    radius = float(getattr(modifier, "radius", 0.) or min(frame[2:]) / 2.)
    return frame, center, max(radius, 1e-6)


def _anchors(modifier, frame, count=None):
    src = getattr(modifier, "source_points", None)
    dst = getattr(modifier, "points", None)
    if not dst:
        dst = _QUAD if count == 4 else []
    if not src:
        src = _QUAD if count == 4 else dst
    source, destination = np.asarray(src, np.float64).reshape(-1, 2), np.asarray(dst, np.float64).reshape(-1, 2)
    if len(source) != len(destination) or (count is not None and len(source) != count):
        raise ValueError("Distortion source and destination point counts must match")
    return source * frame[2:] + frame[:2], destination * frame[2:] + frame[:2]


def _affine(parameters, center, frame):
    radians = math.radians(float(parameters.get("rotation", 0.)))
    c, s = math.cos(radians), math.sin(radians)
    scale = np.diag((float(parameters.get("scale_x", 100.)) / 100., float(parameters.get("scale_y", 100.)) / 100.))
    shear = np.asarray(((1., math.tan(math.radians(float(parameters.get("shear_x", 0.))))),
                        (math.tan(math.radians(float(parameters.get("shear_y", 0.)))), 1.)))
    matrix = np.asarray(((c, -s), (s, c))) @ shear @ scale
    offset = np.asarray((parameters.get("offset_x", 0.), parameters.get("offset_y", 0.))) * frame[2:] / 100.
    return matrix, center + offset - matrix @ center


def _perspective(source, destination, corners):
    """Keep a transient folded/singular handle drag at the last neutral mapping."""
    for quad in (source, destination):
        edges = np.roll(quad, -1, axis=0) - quad
        following = np.roll(edges, -1, axis=0)
        cross = edges[:, 0] * following[:, 1] - edges[:, 1] * following[:, 0]
        if not (np.all(cross > 1e-8) or np.all(cross < -1e-8)):
            return np.eye(3), np.eye(3)
    try:
        matrix = homography(destination) @ np.linalg.inv(homography(source))
        denominator = np.c_[corners, np.ones(4)] @ matrix[2]
        if np.min(denominator) <= 0 <= np.max(denominator):
            return np.eye(3), np.eye(3)
        mapped = project(matrix, corners)
        span = np.ptp(mapped, axis=0)
        if not np.isfinite(mapped).all() or np.max(np.abs(mapped)) > 1e9 or np.max(span) > 262144 or np.prod(span) > _MAX_PIXELS:
            return np.eye(3), np.eye(3)
        return matrix, np.linalg.inv(matrix)
    except (ValueError, np.linalg.LinAlgError, FloatingPointError):
        return np.eye(3), np.eye(3)


def _mesh(modifier, frame, parameters):
    rows, columns = int(parameters.get("rows", 4)), int(parameters.get("columns", 4))
    xx, yy = np.meshgrid(np.linspace(0., 1., columns), np.linspace(0., 1., rows))
    rest = np.stack((xx, yy), axis=-1).reshape(-1, 2)
    destinations = np.asarray(getattr(modifier, "points", None) or rest, np.float64)
    sources = np.asarray(getattr(modifier, "source_points", None) or rest, np.float64)
    if sources.shape != rest.shape or destinations.shape != rest.shape:
        raise ValueError("Mesh point count does not match its rows and columns")
    smoothness = float(parameters.get("smoothness", 50.))
    # Tessellate the source grid as well, so moving a source handle is meaningful.
    source_grid = CageGrid(frame=tuple(frame), columns=columns, rows=rows,
                          points=[tuple(point) for point in sources * frame[2:] + frame[:2]], smoothness=smoothness)
    destination_grid = CageGrid(frame=tuple(frame), columns=columns, rows=rows,
                               points=[tuple(point) for point in destinations * frame[2:] + frame[:2]], smoothness=smoothness)
    rest_world, destination, faces = tessellate(destination_grid, subdivisions=6 if smoothness > 0 else 1)
    source = map_points(source_grid, rest_world)
    return source, destination, faces


def _mls(query, source, destination, mode="rigid"):
    """Moving least squares pin warp; fixed pins constrain neighboring motion."""
    if len(source) == 0:
        return query.copy()
    if len(source) == 1:
        return query + destination[0] - source[0]
    source, destination = np.asarray(source), np.asarray(destination)
    shape = query.shape
    query = query.reshape(-1, 2)
    # Work on contiguous coordinate columns. The two-column reductions below
    # used to allocate and traverse several Nx2 arrays per pin, twice. Keeping
    # the same operation order in scalar columns preserves the float64 mapping
    # while substantially reducing allocation and memory traffic.
    x, y = np.ascontiguousarray(query[:, 0]), np.ascontiguousarray(query[:, 1])
    total = np.zeros(len(query))
    px, py, qx, qy = (np.zeros_like(x) for _ in range(4))
    for p, q in zip(source, destination):
        # Explicit array promotion also preserves the previous behavior when
        # callers supply float32 queries and float64 pin coordinates.
        dtype = np.result_type(query.dtype, p.dtype)
        dx, dy = x.astype(dtype, copy=False) - p[0], y.astype(dtype, copy=False) - p[1]
        weights = 1. / np.maximum(dx ** 2 + dy ** 2, 1e-10)
        total += weights
        px += weights * p[0]
        py += weights * p[1]
        qweights = weights.astype(np.result_type(weights.dtype, q.dtype), copy=False)
        qx += qweights * q[0]
        qy += qweights * q[1]
    del qweights
    px /= total
    py /= total
    qx /= total
    qy /= total
    real, imag, denominator = np.zeros(len(query)), np.zeros(len(query)), np.zeros(len(query))
    for p, q in zip(source, destination):
        dtype = np.result_type(query.dtype, p.dtype)
        dx, dy = x.astype(dtype, copy=False) - p[0], y.astype(dtype, copy=False) - p[1]
        weights = 1. / np.maximum(dx ** 2 + dy ** 2, 1e-10)
        ppx, ppy = p[0] - px.astype(dtype, copy=False), p[1] - py.astype(dtype, copy=False)
        qdtype = np.result_type(query.dtype, q.dtype)
        qqx, qqy = q[0] - qx.astype(qdtype, copy=False), q[1] - qy.astype(qdtype, copy=False)
        real += weights * (ppx * qqx + ppy * qqy)
        imag += weights * (ppx * qqy - ppy * qqx)
        denominator += weights * (ppx * ppx + ppy * ppy)
    normal = denominator if mode == "similarity" else np.hypot(real, imag)
    real = np.divide(real, normal, out=np.ones_like(real), where=normal > 1e-14)
    imag = np.divide(imag, normal, out=np.zeros_like(imag), where=normal > 1e-14)
    dx, dy = x - px, y - py
    result = np.stack((qx + (real * dx - imag * dy),
                       qy + (imag * dx + real * dy)), axis=-1)
    return result.reshape(shape)


def _curve(points, values):
    points = np.asarray(points or ((0., 0.), (1., 0.)), np.float64)
    points = points[np.argsort(points[:, 0])]
    unique, indices = np.unique(points[:, 0], return_index=True)
    if len(unique) < 2:
        return np.full_like(values, points[0, 1])
    return PchipInterpolator(unique, points[indices, 1], extrapolate=False)(np.clip(values, unique[0], unique[-1]))


def _curve_range(points, low, high, linear):
    """Conservative range of a shape-preserving curve plus a linear shear."""
    points = np.asarray(points or ((0., 0.), (1., 0.)), np.float64)
    positions = np.r_[low, high, points[(points[:, 0] >= low) & (points[:, 0] <= high), 0]]
    values = _curve(points.tolist(), positions)
    slope = linear * (np.asarray((low, high)) - .5)
    # PCHIP stays between its knots; taking component ranges separately also
    # bounds extrema introduced when the optional linear term is added.
    return float(values.min() + slope.min()), float(values.max() + slope.max())


def _radial_support(factor, source_radius, scale):
    """Outermost finite preimage of a source disk under r * factor(r).

    Negative lens coefficients can fold the mapping and create another visible
    ring. Include every real branch instead of clipping at the first root.
    Work in normalized radii to keep polynomial roots well conditioned.
    """
    polynomial = np.r_[0., factor]
    roots = []
    for sign in (-1., 1.):
        coefficients = polynomial.copy()
        coefficients[0] = sign * source_radius / scale
        roots.extend(root.real for root in np.polynomial.polynomial.polyroots(coefficients)
                     if abs(root.imag) <= 1e-7 * max(1., abs(root.real)) and root.real >= 0.)
    return max(roots, default=0.) * scale


def _glitch_extent(parameters, frame):
    """Bound the finite displacement terms used by _glitch_map."""
    mode = parameters.get("mode", "aberration_offset")
    base = mode.removesuffix("_color")
    strength = abs(float(parameters.get("amount", 0.))) / 100.
    spacing = max(1., float(parameters.get("spacing", 16.)))
    offset = np.abs((parameters.get("offset_x", 12.), parameters.get("offset_y", 0.)))
    channels = max(1, min(3, int(parameters.get("channels", 3))))
    multiplier = max(abs(ch - 1 if parameters.get("bidirectional", True) else ch + 1)
                     for ch in range(channels)) if mode.endswith("_color") or mode.startswith("aberration") else 1.
    strength *= multiplier
    directional = np.abs((parameters.get("horizontal_strength", 0.), parameters.get("vertical_strength", 0.))) / 100.
    if mode == "aberration_offset":
        return offset * strength
    if base in {"shred", "blast"}:
        return directional * frame[2:] * multiplier
    if base == "slice":
        return offset * strength * abs(float(parameters.get("slice_offset", 20.))) / 20.
    if base == "sawtooth":
        return (offset + (spacing, 0.)) * strength
    if base == "distort":
        return frame[2:] * (.15, .05) * strength
    if base == "ripple":
        return np.full(2, spacing * strength)
    if base == "waves":
        return (offset + spacing) * strength
    if mode == "scramble":
        return np.full(2, 3. * spacing * strength)
    if mode == "warp":
        return directional * frame[2:] * .15 * 1.875
    if mode == "light_streaks":
        return np.asarray((max(1., offset[0]) if offset[0] else 0., offset[1])) * strength / 2.
    if mode == "data_blocks":
        return np.asarray((4., 1.)) * spacing * strength
    # These modes change RGB while retaining the unwarped source alpha.
    return np.zeros(2)


def distort_bounds(bounds: QRectF, modifier, local_to_world: QTransform | None = None) -> QRectF:
    """Include the source and expanded geometry, so partial intensity never clips."""
    transform = local_to_world or QTransform()
    if modifier.modifier_type == "distort_smudge":
        from comic_editor.ui.smudge_rendering import smudge_bounds
        return smudge_bounds(bounds, modifier, transform)
    inverse, valid = transform.inverted()
    if not valid:
        raise ValueError("Cannot distort an object with a singular placement")
    frame, center, radius = _geometry(bounds, modifier, transform)
    parameters = getattr(modifier, "parameters", {})
    effect = modifier.modifier_type.removeprefix("distort_")
    corners = _transform(transform, _rect_points(bounds))
    destination = corners
    # A remapped pixel can sample just beyond the source rectangle. Include the
    # reconstruction kernel before deriving support, including cubic tails that
    # survive conversion to 8-bit alpha. Neutral effects retain exact bounds.
    guard = {"nearest": 0., "bilinear": .5, "bicubic": 8.}.get(parameters.get("interpolation", "bilinear"), .5)
    support = _transform(transform, _rect_points(bounds.adjusted(-guard, -guard, guard, guard)))
    source_radius = float(np.linalg.norm(support - center, axis=-1).max())
    disk_radius = None
    extent = np.zeros(2)
    if effect == "affine":
        matrix, offset = _affine(parameters, center, frame)
        if abs(np.linalg.det(matrix)) >= 1e-12:
            destination = corners @ matrix.T + offset
    elif effect == "perspective":
        source, destination = _anchors(modifier, frame, 4)
        matrix, _ = _perspective(source, destination, corners)
        destination = project(matrix, corners)
    elif effect == "mesh_warp":
        _, destination, _ = _mesh(modifier, frame, parameters)
        # Include spline overshoot between tessellated vertices.
        rows, columns = int(parameters.get("rows", 4)), int(parameters.get("columns", 4))
        xx, yy = np.meshgrid(np.linspace(0., 1., columns), np.linspace(0., 1., rows))
        rest = np.stack((xx, yy), axis=-1).reshape(-1, 2)
        delta = np.asarray(getattr(modifier, "points", None) or rest) - rest
        padding = np.ptp(delta, axis=0) * frame[2:] * .3 * float(parameters.get("smoothness", 50.)) / 100.
        destination = np.concatenate((destination - padding, destination + padding))
    elif effect == "deform":
        source, dest = _anchors(modifier, frame)
        if len(source):
            amount = float(parameters.get("amount", 100.)) / 100.
            dest = source + (dest - source) * amount
            grid_x, grid_y = np.meshgrid(np.linspace(bounds.left(), bounds.right(), 17), np.linspace(bounds.top(), bounds.bottom(), 17))
            query = _transform(transform, np.stack((grid_x, grid_y), axis=-1).reshape(-1, 2))
            destination = _mls(query, source, dest, parameters.get("mode", "rigid"))
    elif effect == "shear":
        # Sequential H then V curve shears have an exact inverse, even for folds.
        low, high = corners.min(axis=0), corners.max(axis=0)
        horizontal = _curve_range(parameters.get("horizontal_curve"), (low[1] - frame[1]) / frame[3], (high[1] - frame[1]) / frame[3], float(parameters.get("horizontal", 0.)) / 100.)
        shifted_x = (low[0] + horizontal[0] * frame[2], high[0] + horizontal[1] * frame[2])
        vertical = _curve_range(parameters.get("vertical_curve"), (shifted_x[0] - frame[0]) / frame[2], (shifted_x[1] - frame[0]) / frame[2], float(parameters.get("vertical", 0.)) / 100.)
        if any(abs(value) > 1e-12 for value in (*horizontal, *vertical)):
            world_rect = QRectF(float(shifted_x[0]), float(low[1] + vertical[0] * frame[3]),
                                float(shifted_x[1] - shifted_x[0]), float(high[1] - low[1] + (vertical[1] - vertical[0]) * frame[3]))
            destination = _rect_points(world_rect)
    elif effect in {"twirl", "pinch_punch", "spherical"}:
        amount = float(parameters.get("angle" if effect == "twirl" else "amount", 0.))
        if amount:
            normalized = min(source_radius / radius, 1.)
            if effect == "pinch_punch":
                normalized **= 1. / (2. ** (amount / 100.))
            elif effect == "spherical":
                low, high = 0., 1.
                for _ in range(48):
                    mid = (low + high) / 2.
                    warped = 2. / np.pi * math.asin(mid) if amount >= 0 else math.sin(mid * np.pi / 2.)
                    sampled = mid + abs(amount) / 100. * (warped - mid)
                    if sampled < normalized:
                        low = mid
                    else:
                        high = mid
                normalized = high
            disk_radius = normalized * radius
    elif effect == "lens_distortion":
        amount = float(parameters.get("amount", 0.)) / 100.
        if amount:
            disk_radius = _radial_support((1., 0., amount), source_radius, np.linalg.norm(frame[2:]) / 2.)
    elif effect == "lens_correction":
        from comic_editor.core.lens_profiles import correction_for_params
        profile = correction_for_params(parameters, image_aspect=max(frame[2:]) / min(frame[2:]))
        if profile:
            a, b, c = profile.coefficients
            factor = (1. - a - b - c, c, b, a) if profile.model == "ptlens" else (1., 0., a, 0., b) if profile.model == "poly5" else (1. - a, 0., a)
            if any(abs(value) > 1e-12 for value in factor[1:]):
                disk_radius = _radial_support(factor, source_radius, min(frame[2:]) / 2. / profile.radius_scale)
    elif effect == "ripple":
        extent[:] = abs(float(parameters.get("amount", 0.)))
    elif effect == "displace" and not parameters.get("preserve_alpha", False):
        # Red/green values and Sobel / 4 both lie in [-1, 1].
        extent[:] = abs(float(parameters.get("amount", 0.)))
    elif effect == "pixelate":
        size = max(1., float(parameters.get("size", 8.)))
        low = np.floor((corners.min(axis=0) - frame[:2]) / size) * size + frame[:2]
        high = np.ceil((corners.max(axis=0) - frame[:2]) / size) * size + frame[:2]
        destination = _QUAD * (high - low) + low
    elif effect == "mirror":
        disk_radius = source_radius
    elif effect == "rectangular_to_polar":
        # Input Y selects radius; signed radii are not used by this mapping.
        disk_radius = max(0., (support[:, 1].max() - frame[1]) / frame[3]) * min(frame[2:]) / 2.
    elif effect == "glitch":
        if parameters.get("mode") == "aberration_distortion":
            strength = float(parameters.get("amount", 0.)) / 100.
            if strength:
                channels = max(1, min(3, int(parameters.get("channels", 3))))
                scales = [1. + strength * .15 * (ch - 1 if parameters.get("bidirectional", True) else ch + 1)
                          for ch in range(channels)]
                destination = np.concatenate([center + (support - center) / scale for scale in scales])
        else:
            extent = _glitch_extent(parameters, frame)
    if disk_radius is not None:
        # The square contains the full world-space disk, including when the
        # object placement rotates or scales its local axes.
        destination = np.concatenate((corners, center + (_QUAD * 2. - 1.) * disk_radius))
    elif np.any(extent):
        destination = (support[:, None, :] + (_QUAD * 2. - 1.) * extent).reshape(-1, 2)
    local = _transform(inverse, destination)
    low = np.minimum(local.min(axis=0), (bounds.left(), bounds.top()))
    high = np.maximum(local.max(axis=0), (bounds.right(), bounds.bottom()))
    if not np.isfinite((low, high)).all() or np.max(np.abs((low, high))) > 1_000_000_000:
        # Check before converting to QRect: Qt integer overflow can otherwise
        # wrap a very distant lens branch back into a cropped source rectangle.
        raise ValueError("Distortion bounds are too large; reduce the effect or move its center closer to the artwork")
    # A world/local round trip can put an exact edge at -1e-14 or N+1e-14.
    # Remove this noise before outward integer alignment adds a whole pixel.
    low, high = np.round(low, 8), np.round(high, 8)
    result = QRectF(float(low[0]), float(low[1]), float(high[0] - low[0]), float(high[1] - low[1]))
    return QRectF(result.toAlignedRect())


class _Sampler:
    def __init__(self, pixels, interpolation="bilinear", edges="transparent"):
        self.pixels = pixels
        self.order = {"nearest": 0, "bilinear": 1, "bicubic": 3}.get(interpolation, 1)
        self.mode = {"transparent": "grid-constant", "white": "grid-constant", "clamp": "nearest", "wrap": "grid-wrap", "mirror": "reflect"}.get(edges, "grid-constant")
        self.fill = 1. if edges == "white" else 0.
        # SciPy's spline prefilter requires the same constant padding used by
        # map_coordinates(prefilter=True), otherwise neutral warps alter edges.
        self.padding = 12 if self.order == 3 and self.mode == "grid-constant" else 0
        self.filtered = []
        for channel in range(4):
            values = pixels[..., channel]
            if self.padding:
                values = np.pad(values, self.padding, mode="constant", constant_values=self.fill)
            self.filtered.append(spline_filter(values, order=3, mode=self.mode) if self.order == 3 else values)

    def __call__(self, coordinates):
        x, y = coordinates[..., 0], coordinates[..., 1]
        valid = np.isfinite(x) & np.isfinite(y) & (np.abs(x) < 1e15) & (np.abs(y) < 1e15)
        xy = [np.where(valid, y + self.padding, -1e9), np.where(valid, x + self.padding, -1e9)]
        result = np.stack([map_coordinates(ch, xy, order=self.order, mode=self.mode, cval=self.fill, prefilter=False) for ch in self.filtered], axis=-1)
        result[~valid] = self.fill
        return result


def _array_storage_bytes(arrays):
    """Count retained NumPy allocations once, including shared channel views."""
    owners = {}
    for array in arrays:
        owner = array
        while isinstance(owner.base, np.ndarray):
            owner = owner.base
        owners[id(owner)] = owner.nbytes
    return sum(owners.values())


@dataclass(frozen=True)
class PreparedDistort:
    pixels: np.ndarray
    sampler: _Sampler


class PreparedDistortCache:
    """Bounded immutable source/mesh setup reused by exact region requests.

    Owned and used by the GUI's synchronous projection path only; background
    jobs never receive this cache. Entries contain detached NumPy allocations,
    not Qt images or canvas state. Output coordinates and effect parameters
    remain per request, so region reuse cannot reuse another region's pixels.
    """

    def __init__(self, budget=96 * 1024 * 1024, *, entry_limit=64):
        self.budget = max(0, int(budget))
        self.entry_limit = max(1, int(entry_limit))
        self.bytes = self.hits = self.misses = self.evictions = 0
        self._entries = OrderedDict()

    def _get(self, key):
        entry = self._entries.pop(key, None)
        if entry is None:
            self.misses += 1
            return None
        self.hits += 1
        self._entries[key] = entry
        return entry[0]

    def _put(self, key, value, arrays):
        size = _array_storage_bytes(arrays)
        for array in arrays:
            array.setflags(write=False)
        if size > self.budget:
            return value
        while self._entries and (self.bytes + size > self.budget
                                 or len(self._entries) >= self.entry_limit):
            _, (_, removed) = self._entries.popitem(last=False)
            self.bytes -= removed
            self.evictions += 1
        self._entries[key] = value, size
        self.bytes += size
        return value

    def source(self, image, interpolation="bilinear", edges="transparent"):
        key = ("source", int(image.cacheKey()), image.width(), image.height(),
               image.format().value, interpolation, edges)
        prepared = self._get(key)
        if prepared is not None:
            return prepared
        pixels = _rgba(image)
        sampler = _Sampler(pixels, interpolation, edges)
        sampler.filtered = tuple(sampler.filtered)
        return self._put(key, PreparedDistort(pixels, sampler),
                         (pixels, *sampler.filtered))

    def mesh(self, modifier, frame, parameters):
        key = ("mesh", tuple(frame), int(parameters.get("rows", 4)),
               int(parameters.get("columns", 4)), float(parameters.get("smoothness", 50.)),
               tuple(tuple(point) for point in (getattr(modifier, "source_points", None) or ())),
               tuple(tuple(point) for point in (getattr(modifier, "points", None) or ())))
        prepared = self._get(key)
        if prepared is not None:
            return prepared
        prepared = _mesh(modifier, frame, parameters)
        return self._put(key, prepared, prepared)

    def clear(self):
        self._entries.clear()
        self.bytes = 0


def _triangle_map(query, source, destination, faces):
    """Inverse mesh with stable overlapping-face order and transparent holes."""
    result = np.full_like(query, np.nan)
    if not len(faces):
        return result
    flat = query.reshape(-1, 2)
    output = result.reshape(-1, 2)
    qlow, qhigh = flat.min(axis=0), flat.max(axis=0)
    blocks = None
    if query.ndim == 3 and len(flat) > 4096 and len(faces) > 16 and np.isfinite(query).all():
        # A tessellated 4x4 mesh has hundreds of faces. Scanning every pixel
        # for every face made small unchanged backgrounds take seconds. Bound
        # the exact query points in small grid blocks; only blocks intersecting
        # a face can contain its pixels. This also works for projective grids,
        # folded faces and overlap because it changes neither coordinates nor
        # the original face order.
        height, width = query.shape[:2]
        blocks, lows, highs = [], [], []
        for top in range(0, height, 32):
            for left in range(0, width, 32):
                bottom, right = min(top + 32, height), min(left + 32, width)
                points = query[top:bottom, left:right].reshape(-1, 2)
                lows.append(points.min(axis=0))
                highs.append(points.max(axis=0))
                blocks.append((np.arange(top, bottom)[:, None] * width
                               + np.arange(left, right)).ravel())
        lows, highs = np.asarray(lows), np.asarray(highs)
    # Reject unrelated faces together. Native tiles usually intersect only a
    # few faces; walking the whole tessellation in Python for every strip made
    # final mesh edits much more expensive than their actual sampling work.
    triangles = destination[faces]
    face_lows, face_highs = triangles.min(axis=1), triangles.max(axis=1)
    candidate_faces = np.flatnonzero(~(np.any(face_highs < qlow, axis=1)
                                       | np.any(face_lows > qhigh, axis=1)))
    for face_index in candidate_faces:
        face, tri = faces[face_index], triangles[face_index]
        low, high = face_lows[face_index], face_highs[face_index]
        if blocks is None:
            selected = np.flatnonzero(np.all((flat >= low - 1e-8) & (flat <= high + 1e-8), axis=1))
        else:
            intersecting = np.flatnonzero(np.all((highs >= low - 1e-8) & (lows <= high + 1e-8), axis=1))
            if not len(intersecting):
                continue
            candidates = np.concatenate([blocks[index] for index in intersecting])
            points = flat[candidates]
            selected = candidates[np.all((points >= low - 1e-8) & (points <= high + 1e-8), axis=1)]
        if not len(selected):
            continue
        matrix = np.column_stack((tri[1] - tri[0], tri[2] - tri[0]))
        if abs(np.linalg.det(matrix)) < 1e-12:
            continue
        uv = (flat[selected] - tri[0]) @ np.linalg.inv(matrix).T
        inside = (uv[:, 0] >= -1e-8) & (uv[:, 1] >= -1e-8) & (uv.sum(axis=1) <= 1. + 1e-8)
        weights = uv[inside]
        src = source[face]
        output[selected[inside]] = src[0] + weights[:, :1] * (src[1] - src[0]) + weights[:, 1:] * (src[2] - src[0])
    return result


def _pixel_blocks(pixels, bounds, transform, frame, size, cancelled=None):
    """Average cells in world space; alpha contributes only premultiplied color."""
    height, width = pixels.shape[:2]
    corners = np.floor((_transform(transform, _rect_points(bounds)) - frame[:2]) / size).astype(np.int64)
    low, high = corners.min(axis=0), corners.max(axis=0)
    span = high - low + 1
    # Accumulate bounded source strips. A sparse fallback handles very long,
    # rotated layers without allocating their mostly-empty rectangular atlas.
    total = int(np.prod(span))
    dense = total <= 4 * 1024 * 1024
    sums = np.zeros((total, 4), np.float64) if dense else []
    counts = np.zeros(total, np.int64) if dense else []
    identifiers = []
    for top in range(0, height, max(1, min(128, 262144 // width))):
        if cancelled is not None and cancelled():
            return None
        bottom = min(height, top + max(1, min(128, 262144 // width)))
        xx, yy = np.meshgrid(bounds.x() + (np.arange(width) + .5) * bounds.width() / width,
                             bounds.y() + (np.arange(top, bottom) + .5) * bounds.height() / height)
        world = _transform(transform, np.stack((xx, yy), axis=-1))
        cells = np.floor((world - frame[:2]) / size).astype(np.int64)
        ids = ((cells[..., 1] - low[1]) * span[0] + cells[..., 0] - low[0]).ravel()
        unique, inverse, count = np.unique(ids, return_inverse=True, return_counts=True)
        subtotal = np.stack([np.bincount(inverse, weights=pixels[top:bottom, :, channel].ravel()) for channel in range(4)], axis=-1)
        if dense:
            counts[unique] += count
            sums[unique] += subtotal
        else:
            identifiers.append(unique)
            counts.append(count)
            sums.append(subtotal)
    if dense:
        unique = np.flatnonzero(counts)
        means = sums[unique] / counts[unique, None]
    else:
        identifiers, counts, sums = np.concatenate(identifiers), np.concatenate(counts), np.concatenate(sums)
        unique, inverse = np.unique(identifiers, return_inverse=True)
        total_counts = np.bincount(inverse, weights=counts)
        means = np.stack([np.bincount(inverse, weights=sums[:, channel]) / total_counts for channel in range(4)], axis=-1)

    def sample(query):
        cells = np.floor((query - frame[:2]) / size).astype(np.int64)
        indices = (cells[..., 1] - low[1]) * span[0] + cells[..., 0] - low[0]
        found = np.searchsorted(unique, indices)
        clipped = np.clip(found, 0, len(unique) - 1)
        valid = (found < len(unique)) & (unique[clipped] == indices) & np.all((cells >= low) & (cells <= high), axis=-1)
        return means[clipped] * valid[..., None]
    return sample


def _displacement_map(parameters, pixels, displacement_image, source_native_size, *, draft=False):
    selected = parameters.get("map_source", "source")
    loaded = None
    if selected == "embedded":
        try:
            data = base64.b64decode(parameters.get("map_png", ""), validate=True)
            loaded = QImage.fromData(data)
        except (ValueError, TypeError):
            loaded = QImage()
        if loaded.isNull():
            loaded = None
    elif selected == "beneath" and displacement_image is not None and not displacement_image.isNull():
        loaded = displacement_image
    if loaded is None:
        return pixels, source_native_size
    native_size = (loaded.width(), loaded.height())
    if draft and max(native_size) > 512:
        loaded = loaded.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    return _rgba(loaded), native_size


def _hash(values, seed):
    """Coordinate hash; its result is independent of render strip and resolution."""
    values = np.asarray(values, np.float64)
    return np.mod(np.sin(values * 12.9898 + float(seed) * 78.233 + 19.19) * 43758.5453, 1.)


def _glitch_map(world, frame, center, parameters, channel=1):
    mode = parameters.get("mode", "aberration_offset")
    base = mode.removesuffix("_color")
    strength = float(parameters.get("amount", 25.)) / 100.
    seed = float(parameters.get("seed", 0))
    spacing = max(1., float(parameters.get("spacing", 16.)))
    loops = float(parameters.get("loops", 5.))
    turbulence = float(parameters.get("turbulence", 50.)) / 100.
    offset = np.asarray((parameters.get("offset_x", 12.), parameters.get("offset_y", 0.)), np.float64)
    relative = world - frame[:2]
    column, row = np.floor(relative[..., 0] / spacing), np.floor(relative[..., 1] / spacing)
    random = _hash(row + 313. * column, seed)
    strip = _hash(row, seed)
    channel_multiplier = float(channel - 1 if parameters.get("bidirectional", True) else channel + 1)
    if mode.endswith("_color") or mode.startswith("aberration"):
        strength *= channel_multiplier
    delta = np.zeros_like(world)
    if mode == "aberration_distortion":
        delta = (world - center) * strength * .15
    elif mode == "aberration_offset":
        delta[...] = offset * strength
    elif base == "shred":
        horizontal = float(parameters.get("horizontal_strength", 0.)) / 100.
        vertical = float(parameters.get("vertical_strength", 0.)) / 100.
        channel_scale = channel_multiplier if mode.endswith("_color") else 1.
        delta[..., 0] = (strip * 2. - 1.) * horizontal * frame[2] * channel_scale
        delta[..., 1] = (_hash(column, seed + 1) * 2. - 1.) * vertical * frame[3] * channel_scale
    elif base == "blast":
        active = strip >= .5 if parameters.get("stagger", False) else np.ones_like(strip)
        horizontal = float(parameters.get("horizontal_strength", 0.)) / 100.
        vertical = float(parameters.get("vertical_strength", 0.)) / 100.
        channel_scale = channel_multiplier if mode.endswith("_color") else 1.
        delta[..., 0] = active * (strip * 2. - 1.) * horizontal * frame[2] * channel_scale
        delta[..., 1] = active * (_hash(column, seed + 5.) * 2. - 1.) * vertical * frame[3] * channel_scale
    elif base == "slice":
        horizontal_slices = np.mod(row, 2.) * 2. - 1.
        vertical_slices = np.mod(column, 2.) * 2. - 1.
        displacement = float(parameters.get("slice_offset", 20.)) / 20.
        delta[..., 0] = horizontal_slices * strength * offset[0] * displacement
        delta[..., 1] = vertical_slices * strength * offset[1] * displacement
    elif base == "sawtooth":
        wave = np.mod(relative[..., 1] / frame[3] * loops, 1.) * 2. - 1.
        delta[..., 0] = wave * strength * (spacing + abs(offset[0]))
        delta[..., 1] = wave * strength * offset[1]
    elif base == "distort":
        delta[..., 0] = (strip * 2. - 1.) * strength * frame[2] * .15
        delta[..., 1] = (_hash(column, seed + 2.) * 2. - 1.) * strength * frame[3] * .05
    elif base == "ripple":
        relative_center = world - center
        radius = np.linalg.norm(relative_center, axis=-1)
        wave = np.sin(radius / max(min(frame[2:]), 1.) * loops * 2. * np.pi)
        delta = relative_center / np.maximum(radius[..., None], 1e-8) * (wave * strength * spacing)[..., None]
    elif base == "waves":
        delta[..., 0] = np.sin(relative[..., 1] / frame[3] * loops * 2. * np.pi) * strength * (spacing + abs(offset[0]))
        delta[..., 1] = np.cos(relative[..., 0] / frame[2] * loops * 2. * np.pi) * strength * (spacing + abs(offset[1]))
    elif mode == "scramble":
        delta[..., 0] = np.round((_hash(row + 17. * column, seed) - .5) * 6.) * spacing * abs(strength)
        delta[..., 1] = np.round((_hash(row + 29. * column, seed + 4) - .5) * 6.) * spacing * abs(strength)
    elif mode == "fuzz":
        cell = np.floor(relative[..., 0]) + 65537. * np.floor(relative[..., 1])
        delta[..., 0] = (_hash(cell, seed) - .5) * spacing * strength
        delta[..., 1] = (_hash(cell, seed + 1.) - .5) * spacing * strength
    elif mode == "warp":
        x, y = relative[..., 0] / frame[2], relative[..., 1] / frame[3]
        dx, dy = np.zeros_like(x), np.zeros_like(y)
        for octave in range(4):
            frequency = 2. ** octave * loops * (.25 + turbulence * 7.75)
            phase = _hash(octave, seed) * 2. * np.pi
            dx += np.sin(x * frequency + np.cos(y * frequency * 1.73 + phase)) / 2. ** octave
            dy += np.cos(y * frequency + np.sin(x * frequency * 1.31 + phase)) / 2. ** octave
        horizontal = float(parameters.get("horizontal_strength", 0.)) / 100.
        vertical = float(parameters.get("vertical_strength", 0.)) / 100.
        delta[..., 0] = dx * horizontal * frame[2] * .15
        delta[..., 1] = dy * vertical * frame[3] * .15
    elif mode == "light_streaks":
        extent = max(1., abs(offset[0]))
        direction = -1. if offset[0] < 0 else 1.
        delta[..., 0] = (np.mod(relative[..., 0], extent) - extent / 2.) * abs(strength) * direction if offset[0] else 0.
        delta[..., 1] = (strip - .5) * strength * offset[1]
    elif mode == "data_blocks":
        delta[..., 0] = (random > .55) * np.round((_hash(row, seed + 2.) - .5) * 8.) * spacing * strength
        delta[..., 1] = (random > .8) * spacing * strength
    return world + delta


def render_distort(image: QImage, bounds: QRectF, modifier, local_to_world: QTransform | None = None,
                   output_bounds: QRectF | None = None, cancelled=None, pixel_scale=1., *,
                   displacement_image: QImage | None = None, preparation_cache=None):
    """Return a full-strength distortion, or ``None`` when cancelled.

    ``pixel_scale`` controls output resolution only. The source image always
    spans bounds, whether it came from a full-resolution or draft capture.
    """
    if image.isNull() or bounds.isEmpty():
        return QImage(image)
    if cancelled is not None and cancelled():
        return None
    transform = local_to_world or QTransform()
    inverse, valid = transform.inverted()
    if not valid:
        raise ValueError("Cannot distort an object with a singular placement")
    frame, center, radius = _geometry(bounds, modifier, transform)
    parameters = getattr(modifier, "parameters", {})
    effect = modifier.modifier_type.removeprefix("distort_")
    output_bounds = QRectF(output_bounds if output_bounds is not None else distort_bounds(bounds, modifier, transform))
    pixel_scale = max(1e-6, float(pixel_scale))
    width, height = max(1, math.ceil(output_bounds.width() * pixel_scale)), max(1, math.ceil(output_bounds.height() * pixel_scale))
    if width * height > _MAX_PIXELS:
        raise ValueError("Distortion result is too large; reduce the deformation or layer size")
    if effect == "smudge":
        from comic_editor.ui.smudge_rendering import render_smudge
        return render_smudge(image, bounds, modifier, transform, output_bounds, cancelled,
                             pixel_scale=pixel_scale, preparation_cache=preparation_cache)
    source_native_size = (image.width(), image.height())
    # Drag previews need bounded preprocessing as well as a small output. The
    # capture still spans exactly the same local bounds after downsampling.
    if pixel_scale < 1. and max(source_native_size) > 512:
        image = image.scaled(512, 512, Qt.AspectRatioMode.KeepAspectRatio, Qt.TransformationMode.SmoothTransformation)
    interpolation, edges = parameters.get("interpolation", "bilinear"), parameters.get("edges", "transparent")
    if preparation_cache is None:
        pixels = _rgba(image)
        sampler = _Sampler(pixels, interpolation, edges)
    else:
        prepared = preparation_cache.source(image, interpolation, edges)
        pixels, sampler = prepared.pixels, prepared.sampler
    output = np.zeros((height, width, 4), np.uint8)
    image_scale = np.asarray((image.width() / bounds.width(), image.height() / bounds.height()))

    def sample_world(world):
        local = _transform(inverse, world)
        return sampler((local - (bounds.x(), bounds.y())) * image_scale - .5)

    context = {}
    if effect == "perspective":
        src, dst = _anchors(modifier, frame, 4)
        _, context["matrix"] = _perspective(src, dst, _transform(transform, _rect_points(bounds)))
    elif effect == "affine":
        matrix, offset = _affine(parameters, center, frame)
        if abs(np.linalg.det(matrix)) < 1e-12:
            matrix, offset = np.eye(2), np.zeros(2)
        context.update(matrix=np.linalg.inv(matrix), offset=offset)
    elif effect == "mesh_warp":
        context["mesh"] = (_mesh(modifier, frame, parameters) if preparation_cache is None
                           else preparation_cache.mesh(modifier, frame, parameters))
    elif effect == "deform":
        src, dst = _anchors(modifier, frame)
        context["pins"] = (src, src + (dst - src) * float(parameters.get("amount", 100.)) / 100.)
    elif effect == "pixelate":
        context["blocks"] = _pixel_blocks(pixels, bounds, transform, frame, max(1., float(parameters.get("size", 8.))), cancelled)
        if context["blocks"] is None:
            return None
    elif effect == "displace":
        maps, native_map_size = _displacement_map(parameters, pixels, displacement_image, source_native_size, draft=pixel_scale < 1.)
        alpha = maps[..., 3:4]
        straight = np.divide(maps[..., :3], alpha, out=np.zeros_like(maps[..., :3]), where=alpha > 1e-8)
        if parameters.get("method", "sobel") == "red_green":
            displacement = (straight[..., :2] - .5) * 2.
            displacement[alpha[..., 0] < 1e-8] = 0.
        else:
            luminance = straight @ np.asarray((.2126, .7152, .0722), np.float32)
            displacement = np.stack((sobel(luminance, axis=1, mode="nearest"), sobel(luminance, axis=0, mode="nearest")), axis=-1) / 4.
            displacement *= (maps.shape[1] / native_map_size[0], maps.shape[0] / native_map_size[1])
        context.update(map=displacement, map_size=(maps.shape[1], maps.shape[0]), native_map_size=native_map_size)
    elif effect == "lens_correction":
        from comic_editor.core.lens_profiles import correction_for_params
        context["lens"] = correction_for_params(parameters, image_aspect=max(frame[2:]) / min(frame[2:]))
    elif effect == "glitch" and parameters.get("mode") == "quantisation":
        # Quantisation is spatial pixelation of separate color channels. Offset
        # each channel's block grid; do not confuse it with RGB posterization.
        spacing = max(1., float(parameters.get("spacing", 16.)))
        offsets = np.asarray((parameters.get("offset_x", 10.), parameters.get("offset_y", 0.)))
        context["channel_blocks"] = []
        for channel in range(max(1, min(3, int(parameters.get("channels", 3))))):
            grid_frame = frame.copy()
            grid_frame[:2] += offsets * (channel - 1)
            blocks = _pixel_blocks(pixels, bounds, transform, grid_frame, spacing * (1. + channel * .5), cancelled)
            if blocks is None:
                return None
            context["channel_blocks"].append(blocks)

    strip_height = max(1, min(128, 262144 // width))
    for top in range(0, height, strip_height):
        if cancelled is not None and cancelled():
            return None
        bottom = min(height, top + strip_height)
        xx, yy = np.meshgrid(output_bounds.x() + (np.arange(width) + .5) * output_bounds.width() / width,
                             output_bounds.y() + (np.arange(top, bottom) + .5) * output_bounds.height() / height)
        world = _transform(transform, np.stack((xx, yy), axis=-1))
        uv = (world - frame[:2]) / frame[2:]
        delta = world - center
        distance = np.linalg.norm(delta, axis=-1)
        mapped = world.copy()
        if effect == "perspective":
            mapped = project(context["matrix"], world)
        elif effect == "affine":
            mapped = (world - context["offset"]) @ context["matrix"].T
        elif effect == "deform":
            src, dst = context["pins"]
            mapped = _mls(world, dst, src, parameters.get("mode", "rigid"))
        elif effect == "mesh_warp":
            mapped = _triangle_map(world, *context["mesh"])
        elif effect in {"twirl", "pinch_punch", "spherical"}:
            normalized = np.minimum(distance / radius, 1.)
            if effect == "twirl":
                angle = -math.radians(float(parameters.get("angle", 90.))) * (1. - normalized) ** 2
                c, s = np.cos(angle), np.sin(angle)
                mapped = center + np.stack((c * delta[..., 0] - s * delta[..., 1], s * delta[..., 0] + c * delta[..., 1]), axis=-1)
            else:
                strength = float(parameters.get("amount", 25.)) / 100.
                if effect == "pinch_punch":
                    # Positive punch expands the center, negative pinch contracts it.
                    target = normalized ** (2. ** strength)
                else:
                    convex = 2. / np.pi * np.arcsin(np.clip(normalized, 0., 1.))
                    concave = np.sin(normalized * np.pi / 2.)
                    target = normalized + abs(strength) * ((convex if strength >= 0 else concave) - normalized)
                ratio = np.divide(target, normalized, out=np.ones_like(target), where=normalized > 1e-8)
                mapped = center + delta * ratio[..., None]
        elif effect == "ripple":
            strength = float(parameters.get("amount", 10.))
            wavelength = max(1e-6, float(parameters.get("wavelength", 32.)))
            shift = strength * np.sin(distance * 2. * np.pi / wavelength)
            mapped = world + delta / np.maximum(distance[..., None], 1e-8) * shift[..., None]
        elif effect == "lens_distortion":
            normalized = distance / max(np.linalg.norm(frame[2:]) / 2., 1e-6)
            strength = float(parameters.get("amount", 0.)) / 100.
            mapped = center + delta * (1. + strength * normalized ** 2)[..., None]
        elif effect == "lens_correction":
            profile = context["lens"]
            if profile:
                normalized = distance / max(min(frame[2:]) / 2., 1e-6) * profile.radius_scale
                model = profile.model
                if model == "ptlens":
                    a, b, c = profile.coefficients
                    factor = a * normalized ** 3 + b * normalized ** 2 + c * normalized + 1. - a - b - c
                elif model == "poly5":
                    k1, k2, _ = profile.coefficients
                    factor = 1. + k1 * normalized ** 2 + k2 * normalized ** 4
                else:
                    k1 = profile.coefficients[0]
                    factor = 1. - k1 + k1 * normalized ** 2
                mapped = center + delta * factor[..., None]
        elif effect == "rectangular_to_polar":
            angle = np.mod(np.arctan2(delta[..., 1], delta[..., 0]) + np.pi / 2., 2. * np.pi)
            mapped = frame[:2] + np.stack((angle / (2. * np.pi), distance / (min(frame[2:]) / 2.)), axis=-1) * frame[2:]
        elif effect == "polar_to_rectangular":
            angle = uv[..., 0] * 2. * np.pi - np.pi / 2.
            radial = uv[..., 1] * min(frame[2:]) / 2.
            mapped = center + np.stack((np.cos(angle), np.sin(angle)), axis=-1) * radial[..., None]
        elif effect == "mirror":
            mirrors = max(1, int(parameters.get("mirrors", 1)))
            sector = np.pi / mirrors
            angle = np.arctan2(delta[..., 1], delta[..., 0]) - math.radians(float(parameters.get("output_angle", 0.)))
            folded = -np.abs(np.mod(angle + sector, sector * 2.) - sector) + math.radians(float(parameters.get("input_angle", 0.)))
            mapped = center + np.stack((np.cos(folded), np.sin(folded)), axis=-1) * distance[..., None]
        elif effect == "shear":
            # Undo vertical first, then horizontal. This remains invertible for curves.
            vertical = _curve(parameters.get("vertical_curve"), uv[..., 0]) + float(parameters.get("vertical", 0.)) / 100. * (uv[..., 0] - .5)
            mapped[..., 1] -= vertical * frame[3]
            y = (mapped[..., 1] - frame[1]) / frame[3]
            horizontal = _curve(parameters.get("horizontal_curve"), y) + float(parameters.get("horizontal", 0.)) / 100. * (y - .5)
            mapped[..., 0] -= horizontal * frame[2]
        elif effect == "equations":
            coordinates = uv * frame[2:]
            variables = dict(x=coordinates[..., 0], y=coordinates[..., 1], w=frame[2], h=frame[3], r=distance,
                             t=np.arctan2(delta[..., 1], delta[..., 0]), rx=uv[..., 0], ry=uv[..., 1], u=uv[..., 0], v=uv[..., 1],
                             a=float(parameters.get("a", 0.)), b=float(parameters.get("b", 0.)), c=float(parameters.get("c", 0.)))
            polar = parameters.get("coordinates", "cartesian") == "polar"
            first = evaluate_equation(parameters.get("x_expression", "r" if polar else "x"), variables)
            second = evaluate_equation(parameters.get("y_expression", "t" if polar else "y"), variables)
            first, second = np.broadcast_arrays(np.asarray(first, float), np.asarray(second, float), distance)[:2]
            if polar:
                mapped = center + np.stack((first * np.cos(second), first * np.sin(second)), axis=-1)
            else:
                mapped = frame[:2] + np.stack((first, second), axis=-1)
        elif effect == "displace":
            displacement = context["map"]
            if parameters.get("scale_to_fit", True):
                map_xy = uv * context["map_size"] - .5
            else:
                map_xy = (world - frame[:2]) * np.asarray(context["map_size"]) / context["native_map_size"] - .5
            offsets = np.stack([map_coordinates(displacement[..., ch], [map_xy[..., 1], map_xy[..., 0]], order=1, mode="nearest", prefilter=False) for ch in range(2)], axis=-1)
            mapped = world + offsets * float(parameters.get("amount", 20.))
        elif effect == "glitch":
            mode = parameters.get("mode", "aberration_offset")
            if mode in {"quantisation", "fuzz", "channel_flip"}:
                result = sample_world(world)
                alpha = result[..., 3]
                strength = float(parameters.get("amount", 25.)) / 100.
                mix = abs(strength)
                channels = 1 if mode == "channel_flip" else max(1, min(3, int(parameters.get("channels", 3))))
                order = parameters.get("channel_order", "rgb")
                seed = float(parameters.get("seed", 0.))
                offsets = np.asarray((parameters.get("offset_x", 10.), parameters.get("offset_y", 0.)))
                cell = np.floor(world[..., 0] - frame[0]) + 65537. * np.floor(world[..., 1] - frame[1])
                for channel in range(channels):
                    target = "rgb".index(order[channel])
                    channel_mix = strength if mode == "channel_flip" else mix * (1. - channel * .25) if mode == "fuzz" else mix
                    if mode == "channel_flip":
                        modified = alpha - result[..., target]
                    else:
                        if mode == "quantisation":
                            sampled = context["channel_blocks"][channel](world)
                        else:
                            jitter = np.stack((_hash(cell, seed + channel * 7.) - .5,
                                               _hash(cell, seed + channel * 7. + 1.) - .5), axis=-1)
                            sampled = sample_world(world + jitter * offsets * channel_mix)
                        # Leave unselected channels and alpha unchanged. Sample
                        # straight color, then restore the output pixel's alpha.
                        straight = np.divide(sampled[..., target], sampled[..., 3], out=np.zeros_like(alpha), where=sampled[..., 3] > 1e-8)
                        if mode == "fuzz":
                            straight += (_hash(cell, seed + channel * 7. + 2.) - .5) * .5
                        straight = np.clip(straight, 0., 1.)
                        modified = straight * alpha
                        if strength < 0:
                            modified = alpha - modified
                    result[..., target] = result[..., target] * (1. - channel_mix) + modified * channel_mix
            elif mode.endswith("_color") or mode.startswith("aberration"):
                channels = max(1, min(3, int(parameters.get("channels", 3))))
                order = parameters.get("channel_order", "rgb")
                colors = [sample_world(_glitch_map(world, frame, center, parameters, channel)) for channel in range(3)]
                result = sample_world(world)
                for channel in range(channels):
                    target = "rgb".index(order[channel])
                    result[..., target] = colors[channel][..., target]
                result[..., 3] = np.maximum.reduce([result[..., 3], *(color[..., 3] for color in colors[:channels])])
            else:
                result = sample_world(_glitch_map(world, frame, center, parameters))
                strength = float(parameters.get("amount", 25.)) / 100.
                spacing = max(1., float(parameters.get("spacing", 16.)))
                cell = np.floor((world - frame[:2]) / spacing)
                random = _hash(cell[..., 0] + cell[..., 1] * 313., parameters.get("seed", 0))
                if mode == "data_blocks":
                    cells = cell[..., 0] + cell[..., 1] * 313.
                    seed = parameters.get("seed", 0)
                    block_color = np.stack([_hash(cells, seed + channel * 17. + 3.) for channel in range(3)], axis=-1)
                    blend = ((random > .65) * abs(strength))[..., None]
                    result[..., :3] = result[..., :3] * (1. - blend) + block_color * result[..., 3:4] * blend
                elif mode == "light_streaks":
                    noise = (random - .5) * abs(strength)
                    noise = np.maximum(noise, 0.) * 2.
                    result[..., :3] += noise[..., None] * result[..., 3:4]
                if strength < 0 and mode == "data_blocks":
                    result[..., :3] = result[..., 3:4] - result[..., :3]
            output[top:bottom] = _byte_pixels(result)
            continue
        elif effect == "pixelate":
            output[top:bottom] = _byte_pixels(context["blocks"](world))
            continue
        else:
            if effect not in {"lens_correction"}:
                raise ValueError(f"Unknown distortion effect: {effect}")
        result = sample_world(mapped)
        if effect == "displace" and parameters.get("preserve_alpha", False):
            original_alpha = sample_world(world)[..., 3:4]
            straight = np.divide(result[..., :3], result[..., 3:4], out=np.zeros_like(result[..., :3]), where=result[..., 3:4] > 1e-8)
            result[..., :3] = straight * original_alpha
            result[..., 3:4] = original_alpha
        output[top:bottom] = _byte_pixels(result)
    return _image(output)
