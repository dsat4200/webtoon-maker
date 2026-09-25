"""Smooth channel curves and portable sRGB/CMYK/CIELAB color coordinates.

RGB uses the document's encoded sRGB values. CMYK is an unprofiled algebraic
conversion; Lab is CIELAB with a D65 white point. No display/print ICC profile
is assumed. Completely transparent source pixels remain transparent.
"""
from functools import lru_cache
import math

import numpy as np


CURVE_CHANNELS = {
    "gray": ("master", "alpha"),
    "rgb": ("master", "red", "green", "blue", "alpha"),
    "cmyk": ("master", "cyan", "magenta", "yellow", "black", "alpha"),
    "lab": ("master", "lightness", "a", "b", "alpha"),
}
CURVE_BLEND_MODES = ("normal", "multiply", "screen", "overlay", "darken", "lighten",
                     "soft_light", "hard_light", "difference", "exclusion")
IDENTITY_CURVE = ((0.0, 0.0), (1.0, 1.0))
_RGB_XYZ = np.array([[.4124564, .3575761, .1804375],
                     [.2126729, .7151522, .0721750],
                     [.0193339, .1191920, .9503041]], dtype=np.float64)
_XYZ_RGB = np.linalg.inv(_RGB_XYZ)
_WHITE = np.array([.95047, 1., 1.08883])


def validate_curve_points(points):
    if not isinstance(points, (list, tuple)) or not 2 <= len(points) <= 256:
        raise ValueError("A curve needs between 2 and 256 points")
    result = []
    for point in points:
        if not isinstance(point, (list, tuple)) or len(point) != 2:
            raise ValueError("Curve points need X and Y values")
        x, y = (float(value) for value in point)
        if not all(math.isfinite(value) and 0 <= value <= 1 for value in (x, y)):
            raise ValueError("Curve coordinates must be finite values from 0 to 1")
        result.append((x, y))
    result.sort()
    if any(right[0] - left[0] < 1e-8 for left, right in zip(result, result[1:])):
        raise ValueError("Curve points must have distinct X coordinates")
    return result


def is_identity_curve(points):
    return not points or (tuple(points[0]) == (0., 0.) and tuple(points[-1]) == (1., 1.)
                          and all(x == y for x, y in points))


@lru_cache(maxsize=256)
def _interpolator(points):
    from scipy.interpolate import PchipInterpolator
    coordinates = np.asarray(points, dtype=np.float64)
    return PchipInterpolator(coordinates[:, 0], coordinates[:, 1], extrapolate=False)


def evaluate_curve(points, values):
    """Shape-preserving cubic interpolation with constant endpoint extension."""
    values = np.asarray(values)
    points = tuple(tuple(point) for point in (points or IDENTITY_CURVE))
    if is_identity_curve(points):
        return np.clip(values, 0., 1.)
    clipped = np.clip(values, points[0][0], points[-1][0])
    return np.clip(_interpolator(points)(clipped), 0., 1.).astype(np.float32)


def rgb_to_cmyk(rgb):
    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0., 1.)
    maximum = np.max(rgb, axis=-1, keepdims=True)
    cmy = np.divide(maximum - rgb, maximum, out=np.zeros_like(rgb), where=maximum > 1e-12)
    return np.concatenate((cmy, 1. - maximum), axis=-1)


def cmyk_to_rgb(cmyk):
    cmyk = np.clip(np.asarray(cmyk), 0., 1.)
    return (1. - cmyk[..., :3]) * (1. - cmyk[..., 3:4])


def rgb_to_lab(rgb):
    rgb = np.clip(np.asarray(rgb, dtype=np.float64), 0., 1.)
    linear = np.where(rgb <= .04045, rgb / 12.92, ((rgb + .055) / 1.055) ** 2.4)
    xyz = (linear @ _RGB_XYZ.T) / _WHITE
    delta = 6. / 29.
    value = np.where(xyz > delta ** 3, np.cbrt(xyz), xyz / (3 * delta ** 2) + 4. / 29.)
    lab = np.stack((116 * value[..., 1] - 16,
                    500 * (value[..., 0] - value[..., 1]),
                    200 * (value[..., 1] - value[..., 2])), axis=-1)
    return np.clip((lab + np.array([0., 128., 128.])) / np.array([100., 255., 255.]), 0., 1.)


def lab_to_rgb(lab):
    lab = np.asarray(lab, dtype=np.float64) * np.array([100., 255., 255.]) - np.array([0., 128., 128.])
    y = (lab[..., 0] + 16) / 116
    value = np.stack((y + lab[..., 1] / 500, y, y - lab[..., 2] / 200), axis=-1)
    delta = 6. / 29.
    xyz = np.where(value > delta, value ** 3, 3 * delta ** 2 * (value - 4. / 29.)) * _WHITE
    linear = xyz @ _XYZ_RGB.T
    rgb = np.where(linear <= .0031308, 12.92 * linear,
                   1.055 * np.maximum(linear, 0.) ** (1. / 2.4) - .055)
    return np.clip(rgb, 0., 1.)


def color_coordinates(rgb, mode):
    """Normalized coordinates for rendering, histograms, and canvas pickers."""
    rgb = np.asarray(rgb, dtype=np.float64)
    if mode in {"gray", "grey"}:
        return (rgb @ np.array([.2126, .7152, .0722]))[..., None]
    if mode == "cmyk":
        return rgb_to_cmyk(rgb)
    if mode == "lab":
        return rgb_to_lab(rgb)
    return rgb.copy()


def blend_colors(base, adjusted, mode):
    if mode == "multiply":
        return base * adjusted
    if mode == "screen":
        return 1 - (1 - base) * (1 - adjusted)
    if mode == "overlay":
        return np.where(base <= .5, 2 * base * adjusted, 1 - 2 * (1 - base) * (1 - adjusted))
    if mode == "hard_light":
        return np.where(adjusted <= .5, 2 * base * adjusted, 1 - 2 * (1 - base) * (1 - adjusted))
    if mode == "soft_light":
        curve = np.where(base <= .25, ((16 * base - 12) * base + 4) * base, np.sqrt(base))
        return np.where(adjusted <= .5, base - (1 - 2 * adjusted) * base * (1 - base),
                         base + (2 * adjusted - 1) * (curve - base))
    if mode == "darken":
        return np.minimum(base, adjusted)
    if mode == "lighten":
        return np.maximum(base, adjusted)
    if mode == "difference":
        return np.abs(base - adjusted)
    if mode == "exclusion":
        return base + adjusted - 2 * base * adjusted
    return adjusted


def curves_is_neutral(modifier):
    if modifier.blend_mode != "normal":
        return False
    prefix = modifier.color_mode + ":"
    return all(is_identity_curve(points) for key, points in modifier.curves.items()
               if key.startswith(prefix))


def _map_curve(points, values, minimum, maximum):
    if is_identity_curve(points):
        return values
    return np.clip(minimum + (maximum - minimum) * evaluate_curve(
        points, (values - minimum) / (maximum - minimum)), 0., 1.)


def curve_graph_values(rgb, alpha, modifier, channel=None):
    """Return normalized graph X coordinates before the selected curve.

    Master returns all color channels. An individual color channel includes
    the master adjustment; alpha is independent. Values outside the visible
    graph range are clipped to its edges for plotting and picker insertion.
    """
    mode, channel = modifier.color_mode, channel or modifier.channel
    if channel == "alpha":
        values = np.asarray(alpha)
    else:
        values = color_coordinates(rgb, mode)
        if channel != "master":
            values = _map_curve(modifier.curves.get(mode + ":master"), values,
                                modifier.input_min, modifier.input_max)
            values = values[..., CURVE_CHANNELS[mode][1:-1].index(channel)]
    return np.clip((values - modifier.input_min) / (modifier.input_max - modifier.input_min), 0., 1.)


def apply_curves(original, modifier):
    """Adjust premultiplied RGBA, keeping allocations bounded to image strips."""
    if curves_is_neutral(modifier):
        return original
    result = original.copy()
    mode = modifier.color_mode
    channels = CURVE_CHANNELS[mode]
    master = modifier.curves.get(mode + ":master")
    alpha_curve = modifier.curves.get(mode + ":alpha")
    color_changed = (not is_identity_curve(master)
                     or any(not is_identity_curve(modifier.curves.get(mode + ":" + channel))
                            for channel in channels[1:-1]))
    for top in range(0, original.shape[0], 128):
        incoming = original[top:top + 128]
        alpha = incoming[..., 3:4]
        rgb = np.divide(incoming[..., :3], alpha, out=np.zeros_like(incoming[..., :3]), where=alpha > 0)
        adjusted = rgb
        if color_changed:
            coordinates = color_coordinates(rgb, mode)
            values = _map_curve(master, coordinates, modifier.input_min, modifier.input_max)
            for index, channel in enumerate(channels[1:-1]):
                values[..., index] = _map_curve(modifier.curves.get(mode + ":" + channel),
                                                values[..., index], modifier.input_min, modifier.input_max)
            if mode == "cmyk":
                adjusted = cmyk_to_rgb(values)
            elif mode == "lab":
                adjusted = lab_to_rgb(values)
            elif mode == "gray":
                ratio = np.divide(values, coordinates, out=np.zeros_like(values), where=coordinates > 1e-12)
                adjusted = np.where(coordinates > 1e-12, rgb * ratio, values)
            else:
                adjusted = values
        adjusted = np.clip(blend_colors(rgb, np.clip(adjusted, 0., 1.), modifier.blend_mode), 0., 1.)
        if not is_identity_curve(alpha_curve):
            alpha = np.where(alpha > 0, _map_curve(alpha_curve, alpha,
                                                  modifier.input_min, modifier.input_max), 0.)
        result[top:top + 128, ..., :3] = adjusted * alpha
        result[top:top + 128, ..., 3:4] = alpha
    return result
