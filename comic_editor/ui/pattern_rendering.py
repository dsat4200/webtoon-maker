"""Alpha-safe CPU fallback and cached resources for GPU pattern modifiers.

The public effect returns the full-strength result. The modifier pipeline owns
intensity and mask blending so both rendering paths use exactly one blend.
"""
from __future__ import annotations

from functools import lru_cache
import math

import numpy as np
from PySide6.QtGui import QColor, QImage
from scipy.ndimage import gaussian_filter, map_coordinates
from scipy.spatial import Delaunay, QhullError

from comic_editor.core.models import HalftoneModifier, PixelateModifier


def _rgba(image: QImage) -> np.ndarray:
    image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
    data = np.frombuffer(image.constBits(), dtype=np.uint8,
                         count=image.sizeInBytes()).reshape(image.height(), image.bytesPerLine())
    return data[:, :image.width() * 4].reshape(image.height(), image.width(), 4).astype(np.float32) / 255.


def _image(array: np.ndarray) -> QImage:
    data = np.ascontiguousarray(np.rint(np.clip(array, 0., 1.) * 255.).astype(np.uint8))
    height, width = data.shape[:2]
    return QImage(data.data, width, height, width * 4,
                  QImage.Format.Format_RGBA8888_Premultiplied).copy().convertToFormat(
                      QImage.Format.Format_ARGB32_Premultiplied)


def _straight(array: np.ndarray) -> np.ndarray:
    return np.divide(array[..., :3], array[..., 3:4],
                     out=np.zeros_like(array[..., :3]), where=array[..., 3:4] > 1e-6)


def _color(value: str) -> np.ndarray:
    color = QColor(value)
    return np.array(color.getRgbF(), dtype=np.float32)


def _srgb_oklab(rgb: np.ndarray) -> np.ndarray:
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    lms = linear @ np.array([[.4122214708, .2119034982, .0883024619],
                             [.5363325363, .6806995451, .2817188376],
                             [.0514459929, .1073969566, .6299787005]])
    return np.cbrt(lms) @ np.array([[.2104542553, 1.9779984951, .0259040371],
                                   [.7936177850, -2.4285922050, .7827717662],
                                   [-.0040720468, .4505937099, -.8086757660]])


def _oklab_srgb(lab: np.ndarray) -> np.ndarray:
    lms = lab @ np.array([[1., 1., 1.], [.3963377774, -.1055613458, -.0894841775],
                         [.2158037573, -.0638541728, -1.2914855480]])
    linear = (lms ** 3) @ np.array([[4.0767416621, -1.2684380046, -.0041960863],
                                   [-3.3077115913, 2.6097574011, -.7034186147],
                                   [.2309699292, -.3413193965, 1.7076147010]])
    return np.clip(np.where(linear <= .0031308, linear * 12.92,
                           1.055 * np.maximum(linear, 0.) ** (1. / 2.4) - .055), 0., 1.)


@lru_cache(maxsize=64)
def _gradient_lut_cached(stops: tuple, interpolation: str, count: int) -> np.ndarray:
    positions = np.array([stop[0] for stop in stops], dtype=np.float32)
    colors = np.array([_color(stop[1]) for stop in stops], dtype=np.float32)
    samples = np.linspace(0., 1., count, dtype=np.float32)
    # right-sided lookup also makes coincident stops a well-defined hard edge.
    indices = np.clip(np.searchsorted(positions, samples, side="right") - 1, 0, len(stops) - 2)
    low, high = positions[indices], positions[indices + 1]
    amount = np.clip((samples - low) / np.maximum(high - low, 1e-8), 0., 1.)[:, None]
    left, right = colors[indices], colors[indices + 1]
    result = left * (1. - amount) + right * amount
    if interpolation == "oklch":
        lab = _srgb_oklab(colors[:, :3])
        chroma = np.hypot(lab[:, 1], lab[:, 2])
        hue = np.arctan2(lab[:, 2], lab[:, 1])
        hue_a, hue_b = hue[indices].copy(), hue[indices + 1].copy()
        hue_a[chroma[indices] < 1e-5] = hue_b[chroma[indices] < 1e-5]
        hue_b[chroma[indices + 1] < 1e-5] = hue_a[chroma[indices + 1] < 1e-5]
        t = amount[:, 0]
        h = hue_a + ((hue_b - hue_a + np.pi) % (2. * np.pi) - np.pi) * t
        c = chroma[indices] * (1. - t) + chroma[indices + 1] * t
        light = lab[indices, 0] * (1. - t) + lab[indices + 1, 0] * t
        result[:, :3] = _oklab_srgb(np.column_stack((light, c * np.cos(h), c * np.sin(h))))
    result[samples <= positions[0]] = colors[0]
    result[samples >= positions[-1]] = colors[-1]
    result = np.asarray(result, dtype=np.float32)
    result.setflags(write=False)
    return result


def gradient_lut(stops, interpolation: str = "rgb", count: int = 256) -> np.ndarray:
    """Return a cached straight-RGBA lookup table, with shortest-hue OKLCH."""
    key = tuple(sorted((float(position), str(color)) for position, color in stops))
    if len(key) < 2:
        key = ((0., "#FF000000"), (1., "#FFFFFFFF"))
    return _gradient_lut_cached(key, interpolation, max(2, int(count)))


def halftone_unit(width: int, height: int, modifier) -> float:
    edge = {"short": min(width, height), "long": max(width, height),
            "width": width, "height": height}.get(modifier.fit_mode, min(width, height))
    return max(edge / max(1., float(modifier.base_resolution)), 1e-6)


def _geometry_key(width, height, modifier):
    unit = halftone_unit(width, height, modifier)
    spacing = max(.5, float(modifier.spacing) * unit)
    angle = math.radians(float(modifier.rotation))
    span_x = abs(math.cos(angle)) * width + abs(math.sin(angle)) * height
    span_y = abs(math.sin(angle)) * width + abs(math.cos(angle)) * height
    area = (span_x + 6 * spacing) * (span_y + 6 * spacing)
    if modifier.grid_type in {"radial", "ring"}:
        area = math.pi * (math.hypot(width, height) / 2. + 2 * spacing) ** 2
    # Bound *candidate* allocations, including rotated, extremely thin images.
    spacing = max(spacing, math.sqrt(area / 200000.))
    return (int(width), int(height), modifier.grid_type,
            spacing, float(modifier.rotation),
            int(modifier.stipple_seed), int(getattr(modifier, "smoothing_iterations", 100)),
            float(getattr(modifier, "collide_min", .25)), float(getattr(modifier, "collide_max", 1.)))


@lru_cache(maxsize=12)
def _points_cached(key: tuple) -> np.ndarray:
    width, height, grid, spacing, angle, seed, smoothing, collide_min, collide_max = key
    radius = math.hypot(width, height) / 2. + 2. * spacing
    # Offscreen and very fine topology must not allocate unbounded meshes.
    spacing = max(spacing, math.sqrt(width * height / 200000.))
    if grid in {"radial", "ring"}:
        rings = [np.zeros((1, 2), dtype=np.float32)]
        for row in range(1, math.ceil(radius / spacing) + 1):
            count = max(6, round(2. * math.pi * row))
            theta = np.arange(count, dtype=np.float32) * (2. * np.pi / count)
            rings.append(np.column_stack((np.cos(theta), np.sin(theta))) * row * spacing)
        points = np.concatenate(rings)
    else:
        step_y = spacing * (math.sqrt(.75) if grid == "hexagonal" else 1.)
        angle_radians = math.radians(angle)
        span_x = abs(math.cos(angle_radians)) * width / 2. + abs(math.sin(angle_radians)) * height / 2. + 2 * spacing
        span_y = abs(math.sin(angle_radians)) * width / 2. + abs(math.cos(angle_radians)) * height / 2. + 2 * spacing
        rows = np.arange(-math.ceil(span_y / step_y), math.ceil(span_y / step_y) + 1)
        cols = np.arange(-math.ceil(span_x / spacing), math.ceil(span_x / spacing) + 1)
        x, y = np.meshgrid(cols * spacing, rows * step_y)
        if grid == "hexagonal":
            x += (rows[:, None] % 2) * spacing * .5
        points = np.column_stack((x.ravel(), y.ravel())).astype(np.float32)
        if grid == "stippling":
            # Deterministic relaxed jitter: increasing smoothing decreases
            # displacement while collision distance controls point separation.
            relaxation = 1. / (1. + max(0., smoothing) / 100.)
            random = _stipple_hash(points[:, 0] / spacing, points[:, 1] / spacing, seed)
            collision = np.clip(collide_min + (collide_max - collide_min) * random[:, 1], 0., 2.)
            points += (random - .5) * spacing * (.5 + relaxation) / (1. + .4 * collision[:, None])
    angle = math.radians(angle)
    rotation = np.array([[math.cos(angle), math.sin(angle)],
                         [-math.sin(angle), math.cos(angle)]], dtype=np.float32)
    points = points @ rotation + np.array([width / 2., height / 2.], dtype=np.float32)
    margin = 2. * spacing
    points = points[(points[:, 0] >= -margin) & (points[:, 0] <= width + margin)
                    & (points[:, 1] >= -margin) & (points[:, 1] <= height + margin)]
    points = np.ascontiguousarray(points, dtype=np.float32)
    points.setflags(write=False)
    return points


def _stipple_hash(x, y, seed):
    """An integer hash is reproducible across NumPy and GLSL implementations."""
    x, y = np.asarray(x).astype(np.int64).astype(np.uint32), np.asarray(y).astype(np.int64).astype(np.uint32)
    seed_bits = np.uint32((int(seed) * 2246822519) & 0xFFFFFFFF)
    base = x * np.uint32(1664525) + y * np.uint32(1013904223) + seed_bits

    def hash_bits(bits):
        bits = (bits ^ (bits >> np.uint32(16))) * np.uint32(2246822519)
        bits = (bits ^ (bits >> np.uint32(13))) * np.uint32(3266489917)
        return bits ^ (bits >> np.uint32(16))

    return np.stack((hash_bits(base) & np.uint32(16777215),
                     hash_bits(base ^ np.uint32(1757159915)) & np.uint32(16777215)), axis=-1).astype(np.float32) / 16777216.


def halftone_points(width: int, height: int, modifier) -> np.ndarray:
    """Cached grid centers in image-pixel coordinates, including edge padding."""
    return _points_cached(_geometry_key(width, height, modifier))


@lru_cache(maxsize=12)
def _triangles_cached(key: tuple, max_edge: float) -> np.ndarray:
    width, height, grid, spacing, rotation, *_ = key
    if grid in {"square", "hexagonal", "line"}:
        # These regular lattices have known Delaunay edges. Constructing them
        # directly avoids a Qhull rebuild during every spacing-slider change.
        spacing = max(spacing, math.sqrt(width * height / 200000.))
        radius = math.hypot(width, height) / 2. + 2. * spacing
        step_y = spacing * (math.sqrt(.75) if grid == "hexagonal" else 1.)
        angle_radians = math.radians(rotation)
        span_x = abs(math.cos(angle_radians)) * width / 2. + abs(math.sin(angle_radians)) * height / 2. + 2 * spacing
        span_y = abs(math.sin(angle_radians)) * width / 2. + abs(math.cos(angle_radians)) * height / 2. + 2 * spacing
        rows = np.arange(-math.ceil(span_y / step_y), math.ceil(span_y / step_y) + 1)
        cols = np.arange(-math.ceil(span_x / spacing), math.ceil(span_x / spacing) + 1)
        x, y = np.meshgrid(cols * spacing, rows * step_y)
        if grid == "hexagonal":
            x += (rows[:, None] % 2) * spacing * .5
        lattice = np.stack((x, y), axis=-1).astype(np.float32)
        a, b = lattice[:-1, :-1], lattice[:-1, 1:]
        c, d = lattice[1:, :-1], lattice[1:, 1:]
        if grid == "hexagonal":
            odd = (rows[:-1, None, None] % 2).astype(bool)
            first = np.stack((a, b, np.where(odd, d, c)), axis=-2)
            second = np.stack((np.where(odd, a, b), c, d), axis=-2)
        else:
            first, second = np.stack((a, b, c), axis=-2), np.stack((b, c, d), axis=-2)
        triangles = np.concatenate((first.reshape(-1, 3, 2), second.reshape(-1, 3, 2)))
        angle = math.radians(rotation)
        matrix = np.array([[math.cos(angle), math.sin(angle)],
                           [-math.sin(angle), math.cos(angle)]], dtype=np.float32)
        triangles = triangles @ matrix + np.array([width / 2., height / 2.], dtype=np.float32)
        lower, upper = triangles.min(axis=1), triangles.max(axis=1)
        triangles = triangles[(lower[:, 0] <= width + spacing) & (lower[:, 1] <= height + spacing)
                              & (upper[:, 0] >= -spacing) & (upper[:, 1] >= -spacing)]
    else:
        points = _points_cached(key)
        if len(points) < 3:
            return np.empty((0, 3, 2), dtype=np.float32)
        try:
            triangles = points[Delaunay(points).simplices]
        except QhullError:
            return np.empty((0, 3, 2), dtype=np.float32)
    lengths = np.linalg.norm(triangles - np.roll(triangles, 1, axis=1), axis=2)
    triangles = triangles[np.max(lengths, axis=1) <= max_edge]
    triangles = np.ascontiguousarray(triangles, dtype=np.float32)
    triangles.setflags(write=False)
    return triangles


def delaunay_triangles(width: int, height: int, modifier) -> np.ndarray:
    """Cached unshrunk Delaunay faces; shading determines each face's size."""
    key = _geometry_key(width, height, modifier)
    return _triangles_cached(key, max(0., float(getattr(modifier, "max_edge_length", 10.))) * key[3])


def _blur(source: np.ndarray, amount: float) -> np.ndarray:
    if amount <= .01:
        return source
    return gaussian_filter(source, sigma=(amount, amount, 0), mode="nearest", truncate=3.)


def _sample(source: np.ndarray, x: np.ndarray, y: np.ndarray) -> np.ndarray:
    # Coordinates describe pixel centers (the first center is .5,.5).
    coords = [np.clip(y - .5, 0., source.shape[0] - 1.),
              np.clip(x - .5, 0., source.shape[1] - 1.)]
    return np.stack([map_coordinates(source[..., channel], coords, order=1,
                                     mode="nearest", prefilter=False)
                     for channel in range(4)], axis=-1)


def _tone(sample: np.ndarray, modifier) -> tuple[np.ndarray, np.ndarray]:
    rgb = _straight(sample)
    luminance = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
    luminance = np.clip((np.power(luminance, 1. / max(.01, modifier.gamma)) - .5)
                        * (2. ** modifier.contrast) + .5, 0., 1.)
    lo, hi = modifier.clamp_min, modifier.clamp_max
    luminance = np.clip((luminance - lo) / max(hi - lo, 1e-6), 0., 1.)
    level = luminance if modifier.invert else 1. - luminance
    visible = ((level >= modifier.level_min) & (level <= modifier.level_max)
               & (sample[..., 3] > 1e-6))
    return level.astype(np.float32), visible


def _target_hsl(rgb: np.ndarray, modifier) -> np.ndarray:
    """Apply target-color HSL offsets without touching the tone sample."""
    hue = float(getattr(modifier, "target_hue", 0.)) / 360.
    saturation = float(getattr(modifier, "target_saturation", 0.)) / 100.
    lightness = float(getattr(modifier, "target_lightness", 0.)) / 100.
    if hue == 0. and saturation == 0. and lightness == 0.:
        return rgb
    maximum, minimum = rgb.max(axis=-1), rgb.min(axis=-1)
    delta = maximum - minimum
    light = (maximum + minimum) * .5
    sat = np.divide(delta, 1. - np.abs(2. * light - 1.),
                    out=np.zeros_like(light), where=delta > 1e-7)
    denominator = np.maximum(delta, 1e-7)
    phase = np.where(maximum == rgb[..., 0],
                     (rgb[..., 1] - rgb[..., 2]) / denominator,
                     np.where(maximum == rgb[..., 1],
                              (rgb[..., 2] - rgb[..., 0]) / denominator + 2.,
                              (rgb[..., 0] - rgb[..., 1]) / denominator + 4.))
    phase = np.where(delta > 1e-7, phase / 6., 0.)
    phase = (phase + hue) % 1.
    sat = np.clip(sat + saturation, 0., 1.)
    light = np.clip(light + lightness, 0., 1.)
    amplitude = sat * np.minimum(light, 1. - light)
    k = (phase[..., None] * 12. + np.array([0., 8., 4.], dtype=np.float32)) % 12.
    triangle = np.maximum(-1., np.minimum(np.minimum(k - 3., 9. - k), 1.))
    return np.clip(light[..., None] - amplitude[..., None] * triangle, 0., 1.)


def _ink(sample: np.ndarray, level: np.ndarray, modifier,
         color_sample: np.ndarray | None = None) -> np.ndarray:
    if modifier.color_mode in {"source", "target_layer"}:
        rgb = _straight(sample)
        if modifier.color_mode == "target_layer" and color_sample is not None:
            rgb = np.where(color_sample[..., 3:4] > 1e-5, _straight(color_sample), rgb)
        if modifier.color_mode == "target_layer":
            rgb = _target_hsl(rgb, modifier)
        return np.concatenate((rgb, np.ones_like(sample[..., 3:4])), axis=-1)
    if modifier.color_mode == "gradient":
        lut = gradient_lut(modifier.gradient_stops, modifier.gradient_interpolation, 1024)
        return lut[np.minimum(1023, np.rint((1. - level) * 1023).astype(np.int32))]
    return np.broadcast_to(_color(modifier.foreground), sample.shape)


def _composite(source: np.ndarray, ink: np.ndarray, coverage: np.ndarray, modifier) -> np.ndarray:
    background = _color(modifier.background)
    if modifier.transparent_background:
        background = background.copy()
        background[3] = 0.
    ink_alpha = np.clip(coverage, 0., 1.) * ink[..., 3]
    alpha = ink_alpha + background[3] * (1. - ink_alpha)
    rgb = ink[..., :3] * ink_alpha[..., None] + background[:3] * background[3] * (1. - ink_alpha[..., None])
    return np.concatenate((rgb, alpha[..., None]), axis=-1) * source[..., 3:4]


def _pixelate(source: np.ndarray, modifier: PixelateModifier, scale: float) -> np.ndarray:
    height, width = source.shape[:2]
    size = max(1., float(modifier.pixel_size) * scale)
    yy, xx = np.indices((height, width), dtype=np.float32)
    x = (np.floor((xx + .5) / size) + .5) * size
    y = (np.floor((yy + .5) / size) + .5) * size
    sample = _sample(_blur(source, max(0., modifier.blur * scale)), x, y)
    rgb = _straight(sample)
    rgb = np.clip((rgb - .5) * (1. + modifier.contrast / 100.) + .5
                  + modifier.brightness / 100., 0., 1.)
    luminance = rgb @ np.array([.2126, .7152, .0722], dtype=np.float32)
    rgb = luminance[..., None] + (rgb - luminance[..., None]) * (1. + modifier.saturation / 100.)
    return np.concatenate((np.clip(rgb, 0., 1.) * sample[..., 3:4], sample[..., 3:4]), axis=-1)


def _edge_coverage(distance, antialias_distance=None, aa_width=None):
    if aa_width is None:
        field = distance if antialias_distance is None else antialias_distance
        dx = np.gradient(field, axis=1) if field.shape[1] > 1 else np.zeros_like(field)
        dy = np.gradient(field, axis=0) if field.shape[0] > 1 else np.zeros_like(field)
        aa_width = np.abs(dx) + np.abs(dy)
    aa = np.maximum(aa_width, .7)
    t = np.clip((distance + aa * .5) / aa, 0., 1.)
    return 1. - t * t * (3. - 2. * t)


def _dot_coverage(x, y, level, spacing, modifier):
    scale = np.sqrt(np.maximum(0., (1. - modifier.scale_factor) + modifier.scale_factor * level))
    radius = spacing * .5 * modifier.size * scale
    style = modifier.dot_style
    rotation = modifier.rotation if modifier.link_rotation else modifier.dot_rotation
    angle = math.radians(rotation)
    px, py = x * math.cos(angle) + y * math.sin(angle), -x * math.sin(angle) + y * math.cos(angle)
    if style in {"circle", "blob"}:
        radius *= math.sqrt(2.)
        wobble = 1.
        if style == "blob":
            theta = np.arctan2(py, px)
            wobble = 1. + .14 * np.sin(theta * 3. + modifier.stipple_seed) + .08 * np.cos(theta * 5. - modifier.stipple_seed)
        distance = np.hypot(px, py) - radius * wobble
    elif style == "incircle":
        distance = np.hypot(px, py) - radius
    elif style == "square":
        rounding = radius * modifier.corner_rounding
        qx, qy = np.abs(px) - radius + rounding, np.abs(py) - radius + rounding
        distance = np.hypot(np.maximum(qx, 0.), np.maximum(qy, 0.)) + np.minimum(np.maximum(qx, qy), 0.) - rounding
    elif style in {"polygon", "triangle"}:
        sides = 3 if style == "triangle" else modifier.sides
        sector = 2. * np.pi / sides
        theta = np.arctan2(py, px) + np.pi * .5
        distance = np.cos(np.floor(.5 + theta / sector) * sector - theta) * np.hypot(px, py) - radius * math.sqrt(2.) * math.cos(np.pi / sides)
        if modifier.corner_rounding:
            circle_distance = np.hypot(px, py) - radius
            distance = distance * (1. - modifier.corner_rounding) + circle_distance * modifier.corner_rounding
        if modifier.star and style == "polygon":
            wave = np.abs(((theta / (2. * np.pi) * sides + .5) % 1.) * 2. - 1.)
            distance = np.hypot(px, py) - radius * (1. + (modifier.star_inner - 1.) * wave)
    elif style == "line":
        distance = np.maximum(np.abs(py) - radius * .2, np.abs(px) - radius)
    elif style == "liquid":
        distance = (px ** 4 + py ** 4) ** .25 - radius
    else:
        distance = np.hypot(px, py) - radius
    aa = (np.abs(px) + np.abs(py)) / np.maximum(np.hypot(px, py), 1e-6)
    return _edge_coverage(distance, aa_width=aa) * (radius > 1e-6)


def _delaunay(source, prepared, modifier, color_source=None):
    height, width = source.shape[:2]
    triangles = delaunay_triangles(width, height, modifier)
    coverage = np.zeros((height, width), dtype=np.float32)
    ink = np.zeros((height, width, 4), dtype=np.float32)
    if not len(triangles):
        return _composite(source, ink, coverage, modifier)
    centers = np.mean(triangles, axis=1)
    sampled = _sample(prepared, centers[:, 0], centers[:, 1])
    levels, visible = _tone(sampled, modifier)
    color_sample = (_sample(color_source, centers[:, 0], centers[:, 1])
                    if color_source is not None else None)
    inks = _ink(sampled, levels, modifier, color_sample)
    sizes = np.sqrt(np.maximum(0., 1. - modifier.scale_factor + modifier.scale_factor * levels)) * modifier.size
    triangles = centers[:, None, :] + (triangles - centers[:, None, :]) * sizes[:, None, None]
    if not modifier.link_rotation:
        angle = math.radians(modifier.dot_rotation - modifier.rotation)
        rotation = np.array([[math.cos(angle), math.sin(angle)],
                             [-math.sin(angle), math.cos(angle)]], dtype=np.float32)
        triangles = centers[:, None, :] + (triangles - centers[:, None, :]) @ rotation
    for triangle, center, rgba, valid, size in zip(triangles, centers, inks, visible, sizes):
        if not valid or size <= 1e-6:
            continue
        x0, y0 = np.maximum(np.floor(triangle.min(axis=0) - 1), 0).astype(int)
        x1, y1 = np.minimum(np.ceil(triangle.max(axis=0) + 1), [width, height]).astype(int)
        if x1 <= x0 or y1 <= y0:
            continue
        yy, xx = np.mgrid[y0:y1, x0:x1].astype(np.float32)
        xx, yy = xx + .5, yy + .5
        signed = []
        edge_a, edge_b = triangle[1] - triangle[0], triangle[2] - triangle[0]
        winding = edge_a[0] * edge_b[1] - edge_a[1] * edge_b[0]
        for i in range(3):
            a, b = triangle[i], triangle[(i + 1) % 3]
            edge = b - a
            distance = ((xx - a[0]) * edge[1] - (yy - a[1]) * edge[0]) / max(np.linalg.norm(edge), 1e-6)
            signed.append(distance * (1. if winding > 0 else -1.))
        distance = np.maximum.reduce(signed)
        if modifier.corner_rounding:
            circle = np.hypot(xx - center[0], yy - center[1]) - np.min(np.linalg.norm(triangle - center, axis=1)) * .5
            distance = distance * (1. - modifier.corner_rounding) + circle * modifier.corner_rounding
        amount = np.clip(.5 - distance, 0., 1.)
        replace = amount > coverage[y0:y1, x0:x1]
        coverage[y0:y1, x0:x1] = np.maximum(coverage[y0:y1, x0:x1], amount)
        ink[y0:y1, x0:x1][replace] = rgba
    return _composite(source, ink, coverage, modifier)


def _halftone(source: np.ndarray, modifier: HalftoneModifier,
              color_source: np.ndarray | None = None) -> np.ndarray:
    height, width = source.shape[:2]
    unit = halftone_unit(width, height, modifier)
    spacing = max(.5, modifier.spacing * unit)
    prepared = _blur(source, modifier.blur * unit)
    if modifier.dot_style == "delaunay" and modifier.grid_type not in {"line", "ring"}:
        return _delaunay(source, prepared, modifier, color_source)
    yy, xx = np.indices((height, width), dtype=np.float32)
    xx, yy = xx + .5, yy + .5
    angle = math.radians(modifier.rotation)
    px, py = xx - width / 2., yy - height / 2.
    gx, gy = px * math.cos(angle) + py * math.sin(angle), -px * math.sin(angle) + py * math.cos(angle)
    grid = modifier.grid_type
    if grid in {"line", "ring"}:
        point_spacing = max(.25, getattr(modifier, "point_spacing", 5.) * unit)
        coordinate = gy if grid == "line" else np.hypot(gx, gy)
        band = np.floor(coordinate / spacing + .5) * spacing
        along = gx if grid == "line" else np.arctan2(gy, gx) * np.maximum(np.abs(band), spacing)
        along = np.floor(along / point_spacing + .5) * point_spacing
        if grid == "line":
            cx, cy = along, band
        else:
            theta = along / np.maximum(np.abs(band), spacing)
            cx, cy = np.cos(theta) * band, np.sin(theta) * band
        sx = cx * math.cos(angle) - cy * math.sin(angle) + width / 2.
        sy = cx * math.sin(angle) + cy * math.cos(angle) + height / 2.
        sampled = _sample(prepared, sx, sy)
        level, visible = _tone(sampled, modifier)
        line_level = np.clip(level * getattr(modifier, "line_level_scale", 1.), 0., 1.)
        thickness = spacing * .5 * getattr(modifier, "line_width", 1.) * (1. - modifier.scale_factor + modifier.scale_factor * line_level)
        coverage = _edge_coverage(np.abs(coordinate - band) - thickness, coordinate)
        coverage *= visible & (thickness > 1e-6)
        color_sample = _sample(color_source, sx, sy) if color_source is not None else None
        return _composite(source, _ink(sampled, level, modifier, color_sample), coverage, modifier)

    coverage = np.zeros((height, width), dtype=np.float32)
    chosen = _sample(prepared, xx, yy)
    chosen_tone = np.zeros((height, width), dtype=np.float32)
    chosen_color = _sample(color_source, xx, yy) if color_source is not None else None
    joined = np.full((height, width), 1e20, dtype=np.float32)
    joins = np.zeros((height, width), dtype=np.int16)
    cell_x = np.floor(gx / spacing + .5)
    cell_y = np.floor(gy / (spacing * math.sqrt(.75) if grid == "hexagonal" else spacing) + .5)
    radial_ring = np.floor(np.hypot(gx, gy) / spacing + .5)
    half_span = 2 if modifier.size > 1.5 else 1
    jitter_amount = .5 + 1. / (1. + modifier.smoothing_iterations / 100.)
    local_tone, local_valid = _tone(chosen, modifier)
    local_tone = np.where(local_valid, local_tone, -1.)
    for iy in range(-half_span, half_span + 1):
        for ix in range(-half_span, half_span + 1):
            index_x, index_y = cell_x + ix, cell_y + iy
            cx, cy = index_x * spacing, index_y * spacing
            candidate_valid = np.ones((height, width), dtype=bool)
            if grid == "hexagonal":
                cx = (index_x + (index_y % 2.) * .5) * spacing
                cy = index_y * spacing * math.sqrt(.75)
            elif grid == "radial":
                ring = np.maximum(0., radial_ring + iy)
                candidate_valid = (radial_ring + iy >= 0.) & ((ring != 0.) | (ix == 0))
                count = np.maximum(1., np.floor(2. * np.pi * ring + .5))
                theta = (np.floor(np.arctan2(gy, gx) / (2. * np.pi) * count + .5) + ix) * 2. * np.pi / count
                cx, cy = np.cos(theta) * ring * spacing, np.sin(theta) * ring * spacing
            elif grid == "stippling":
                base_x = cx * math.cos(angle) - cy * math.sin(angle) + width / 2.
                base_y = cx * math.sin(angle) + cy * math.cos(angle) + height / 2.
                density, density_valid = _tone(_sample(prepared, base_x, base_y), modifier)
                density = np.where(density_valid, density, 0.)
                collision = modifier.collide_min + (modifier.collide_max - modifier.collide_min) * density
                jitter = ((_stipple_hash(index_x, index_y, modifier.stipple_seed) - .5)
                          * spacing * jitter_amount / (1. + .4 * collision[..., None]))
                cx, cy = cx + jitter[..., 0], cy + jitter[..., 1]
            sx = cx * math.cos(angle) - cy * math.sin(angle) + width / 2.
            sy = cx * math.sin(angle) + cy * math.cos(angle) + height / 2.
            sampled = _sample(prepared, sx, sy)
            level, visible = _tone(sampled, modifier)
            visible &= candidate_valid
            mark = _dot_coverage(xx - sx, yy - sy, level, spacing, modifier) * visible
            if modifier.dot_style in {"blob", "liquid"}:
                radius = spacing * (.7071068 if modifier.dot_style == "blob" else .5) * modifier.size * np.sqrt(
                    np.maximum(0., 1. - modifier.scale_factor + modifier.scale_factor * level))
                distance = np.hypot(xx - sx, yy - sy) - radius
                smoothness = spacing * .18 * modifier.merge_strength * modifier.min_neck_width
                if modifier.dot_style == "liquid":
                    if modifier.even_merge_tone:
                        radius = spacing * .5 * modifier.size * np.sqrt(np.maximum(
                            0., 1. - modifier.scale_factor + modifier.scale_factor * local_tone))
                    rotation = modifier.rotation if modifier.link_rotation else modifier.dot_rotation
                    dot_angle = math.radians(rotation)
                    local_x = (xx - sx) * math.cos(dot_angle) + (yy - sy) * math.sin(dot_angle)
                    local_y = -(xx - sx) * math.sin(dot_angle) + (yy - sy) * math.cos(dot_angle)
                    qx, qy = np.abs(local_x) - radius * (1. - modifier.corner_rounding), np.abs(local_y) - radius * (1. - modifier.corner_rounding)
                    distance = np.hypot(np.maximum(qx, 0.), np.maximum(qy, 0.)) + np.minimum(np.maximum(qx, qy), 0.) - radius * modifier.corner_rounding
                    smoothness = spacing * .15 * modifier.corner_rounding
                eligible = visible & (radius > 1e-6)
                distance = np.where(eligible, distance, 1e20)
                h = np.clip(.5 + .5 * (distance - joined) / max(smoothness, 1e-5), 0., 1.)
                merged = distance * (1. - h) + joined * h - smoothness * h * (1. - h)
                merge = (joins < modifier.max_necks) | (modifier.dot_style == "liquid")
                joined = np.where(merge, merged, np.minimum(joined, distance))
                joins += (distance < spacing * .5).astype(np.int16)
            replace = mark > coverage
            coverage = np.maximum(coverage, mark)
            chosen = np.where(replace[..., None], sampled, chosen)
            chosen_tone = np.where(replace, level, chosen_tone)
            if color_source is not None:
                color_sample = _sample(color_source, sx, sy)
                chosen_color = np.where(replace[..., None], color_sample, chosen_color)
    if modifier.dot_style in {"blob", "liquid"}:
        coverage = _edge_coverage(joined)
    ink = _ink(chosen, chosen_tone, modifier, chosen_color)
    return _composite(source, ink, coverage, modifier)


def apply_pattern_effect(image: QImage, modifier, scale: float = 1.,
                         color_source: QImage | None = None) -> QImage:
    """Apply a complete pattern effect, preserving dimensions and image alpha."""
    if image.isNull():
        return image.copy()
    source = _rgba(image)
    if isinstance(modifier, PixelateModifier):
        result = _pixelate(source, modifier, max(float(scale), 1e-6))
    elif isinstance(modifier, HalftoneModifier):
        colors = None
        if (modifier.color_mode == "target_layer" and color_source is not None
                and not color_source.isNull() and color_source.size() == image.size()):
            colors = _rgba(color_source)
        result = _halftone(source, modifier, colors)
    else:
        return image.copy()
    return _image(result)
