"""Portable smudge gestures, pressure responses and a single-cubic gesture fit."""
from __future__ import annotations

import bisect
import math
import uuid

from comic_editor.core.pressure import PressureCurve


PARAMETER_LIMITS = {"radius": (.1, 4096.), "flow": (0., 100.), "strength": (0., 100.)}
PARAMETER_DEFAULTS = {"radius": 32., "flow": 72., "strength": 50.}


def _number(value, label, limits=None):
    try:
        result = float(value)
    except (ValueError, TypeError, OverflowError) as error:
        raise ValueError(f"Smudge {label} must be a finite number") from error
    if not math.isfinite(result):
        raise ValueError(f"Smudge {label} must be a finite number")
    return max(limits[0], min(limits[1], result)) if limits else result


def _object(value, allowed, label):
    if not isinstance(value, dict) or any(not isinstance(key, str) for key in value):
        raise ValueError(f"Smudge {label} must be an object with named fields")
    unknown = set(value) - set(allowed)
    if unknown:
        raise ValueError(f"Unknown smudge {label}: " + ", ".join(sorted(unknown)))
    return value


def _position(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError("Smudge positions require two coordinates")
    return [_number(component, "coordinate") for component in value]


def default_tool_settings():
    """Return fresh settings; curves and future-stroke presets never share data."""
    return {**PARAMETER_DEFAULTS, "pressure_enabled": True,
            "pressure_radius": False, "pressure_flow": False, "pressure_strength": True,
            **{name + "_curve": PressureCurve().to_dict() for name in PARAMETER_DEFAULTS}}


def validate_tool_settings(settings):
    defaults = default_tool_settings()
    _object(settings, defaults, "tool settings")
    result = defaults
    for name, value in settings.items():
        if name in PARAMETER_LIMITS:
            result[name] = _number(value, name, PARAMETER_LIMITS[name])
        elif name.startswith("pressure_"):
            if not isinstance(value, bool):
                raise ValueError(f"Smudge {name} must be a boolean")
            result[name] = value
        else:
            _object(value, {"min_ratio", "max_ratio", "minimum", "maximum", "control_x", "control_y"}, "pressure curve")
            curve = {key: _number(component, "pressure curve") for key, component in value.items()}
            result[name] = PressureCurve.from_dict(curve).to_dict()
    return result


def _validate_pressure(samples):
    if not isinstance(samples, (list, tuple)) or not 2 <= len(samples) <= 65536:
        raise ValueError("Smudge pressure requires 2 to 65536 samples")
    result = []
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) != 2:
            raise ValueError("Smudge pressure samples require position and pressure")
        t, p = (_number(component, "pressure sample") for component in sample)
        if not 0. <= t <= 1. or not 0. <= p <= 1.:
            raise ValueError("Smudge pressure positions and values must be between zero and one")
        if result and t <= result[-1][0]:
            raise ValueError("Smudge pressure positions must be ordered and unique")
        result.append([t, p])
    if result[0][0] != 0. or result[-1][0] != 1.:
        raise ValueError("Smudge pressure must include both endpoints")
    return result


def validate_strokes(strokes):
    """Return validated detached strokes, including immutable gesture settings."""
    if not isinstance(strokes, (list, tuple)) or len(strokes) > 4096:
        raise ValueError("Smudge strokes must be a list of at most 4096 gestures")
    result, identifiers = [], set()
    for stroke in strokes:
        _object(stroke, {"id", "points", "pressure", "pressure_settings"}, "stroke")
        identifier = stroke.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise ValueError("Smudge strokes require unique nonempty IDs")
        identifiers.add(identifier)
        points = stroke.get("points")
        if not isinstance(points, (list, tuple)) or len(points) != 2:
            raise ValueError("Smudge strokes require exactly two editable points")
        canonical = []
        for point in points:
            _object(point, {"position", "handle", "point_type", *PARAMETER_DEFAULTS}, "point")
            position = _position(point.get("position"))
            kind = point.get("point_type", "bezier")
            if not isinstance(kind, str) or kind not in {"bezier", "vector"}:
                raise ValueError("Unknown smudge point type")
            canonical.append({"position": position,
                              "handle": _position(point.get("handle", position)),
                              "point_type": kind,
                              **{name: _number(point.get(name, default), name, PARAMETER_LIMITS[name])
                                 for name, default in PARAMETER_DEFAULTS.items()}})
        result.append({"id": identifier, "points": canonical,
                       "pressure": _validate_pressure(stroke.get("pressure", [[0., 1.], [1., 1.]])),
                       "pressure_settings": validate_tool_settings(stroke.get("pressure_settings", {}))})
    return result


def validate_tool_presets(presets):
    if not isinstance(presets, list):
        raise ValueError("Series smudge tool presets must be a list")
    result, identifiers = [], set()
    for preset in presets:
        _object(preset, {"id", "name", "settings"}, "tool preset")
        identifier = preset.get("id")
        if not isinstance(identifier, str) or not identifier.strip() or identifier in identifiers:
            raise ValueError("Smudge tool presets require unique nonempty IDs")
        name = preset.get("name", "Untitled")
        if not isinstance(name, str):
            raise ValueError("Smudge tool preset names must be text")
        identifiers.add(identifier)
        result.append({"id": identifier, "name": name.strip() or "Untitled",
                       "settings": validate_tool_settings(preset.get("settings", {}))})
    return result


def stroke_cubic(stroke):
    """Return four world-space controls; Vector points have collapsed handles."""
    first, last = stroke["points"]
    return (tuple(first["position"]),
            tuple(first["handle"] if first["point_type"] == "bezier" else first["position"]),
            tuple(last["handle"] if last["point_type"] == "bezier" else last["position"]),
            tuple(last["position"]))


def pressure_at(stroke, t):
    samples = stroke["pressure"]
    t = max(0., min(1., float(t)))
    index = bisect.bisect_right(samples, [t, float("inf")])
    if index == 0:
        return samples[0][1]
    if index >= len(samples):
        return samples[-1][1]
    a, b = samples[index - 1], samples[index]
    return a[1] + (b[1] - a[1]) * (t - a[0]) / (b[0] - a[0])


def pressure_response(settings, parameter, pressure):
    if parameter not in PARAMETER_LIMITS:
        raise ValueError("Unknown smudge pressure parameter")
    if not settings["pressure_enabled"] or not settings["pressure_" + parameter]:
        return 1.
    return PressureCurve.from_dict(settings[parameter + "_curve"]).evaluate(pressure)


def stroke_parameters(stroke, t):
    """Interpolate endpoint values, then apply the stroke's saved pressure curve."""
    t = max(0., min(1., float(t)))
    first, last = stroke["points"]
    pressure = pressure_at(stroke, t)
    return {name: (first[name] * (1. - t) + last[name] * t)
            * pressure_response(stroke["pressure_settings"], name, pressure)
            for name in PARAMETER_DEFAULTS}


def fit_smudge_stroke(samples, tool_settings):
    """Fit the whole sampled gesture to one cubic with fixed endpoint tangents.

    Chord-length parameterization makes fitting independent of event frequency.
    Pressure retains its own chord-length profile instead of becoming only two
    endpoint values. A click is a valid stationary gesture.
    """
    settings = validate_tool_settings(tool_settings)
    values = []
    for sample in samples:
        if not isinstance(sample, (list, tuple)) or len(sample) != 3:
            raise ValueError("Smudge input samples require x, y and pressure")
        x, y, p = (_number(component, "input sample") for component in sample)
        values.append((x, y, max(0., min(1., p))))
    if not values or len(values) > 65536:
        raise ValueError("Smudge gestures require 1 to 65536 input samples")
    positions = [values[0][:2]]
    distance = [0.]
    pressures = [values[0][2]]
    for x, y, pressure in values[1:]:
        step = math.dist(positions[-1], (x, y))
        if step <= 1e-9:
            pressures[-1] = pressure
            continue
        positions.append((x, y))
        distance.append(distance[-1] + step)
        pressures.append(pressure)
    p0, p3 = positions[0], positions[-1]
    length = distance[-1]
    if length <= 1e-9:
        c1, c2 = p0, p3
        pressure = [[0., pressures[0]], [1., pressures[-1]]]
    else:
        times = [value / length for value in distance]
        # Estimate endpoint tangents over a short arc to resist tablet jitter.
        a = min(len(positions) - 1, max(1, bisect.bisect_left(distance, length * .05)))
        b = max(0, min(len(positions) - 2, bisect.bisect_right(distance, length * .95) - 1))
        def unit(delta):
            magnitude = math.hypot(*delta)
            return (delta[0] / magnitude, delta[1] / magnitude) if magnitude > 1e-9 else (0., 0.)
        left = unit((positions[a][0] - p0[0], positions[a][1] - p0[1]))
        right = unit((positions[b][0] - p3[0], positions[b][1] - p3[1]))
        aa = ab = bb = ar = br = 0.
        for index, (point, t) in enumerate(zip(positions, times)):
            u = 1. - t
            b0, b1, b2, b3 = u ** 3, 3 * u * u * t, 3 * u * t * t, t ** 3
            # Arc weights prevent slow portions of the gesture dominating.
            weight = (times[min(index + 1, len(times) - 1)] - times[max(0, index - 1)]) / 2
            v1, v2 = (left[0] * b1, left[1] * b1), (right[0] * b2, right[1] * b2)
            residual = (point[0] - p0[0] * (b0 + b1) - p3[0] * (b2 + b3),
                        point[1] - p0[1] * (b0 + b1) - p3[1] * (b2 + b3))
            aa += weight * (v1[0] ** 2 + v1[1] ** 2)
            ab += weight * (v1[0] * v2[0] + v1[1] * v2[1])
            bb += weight * (v2[0] ** 2 + v2[1] ** 2)
            ar += weight * (v1[0] * residual[0] + v1[1] * residual[1])
            br += weight * (v2[0] * residual[0] + v2[1] * residual[1])
        determinant = aa * bb - ab * ab
        alpha = (ar * bb - br * ab) / determinant if abs(determinant) > 1e-12 else length / 3
        beta = (br * aa - ar * ab) / determinant if abs(determinant) > 1e-12 else length / 3
        alpha = alpha if 1e-6 <= alpha <= 2 * length else length / 3
        beta = beta if 1e-6 <= beta <= 2 * length else length / 3
        c1 = (p0[0] + left[0] * alpha, p0[1] + left[1] * alpha)
        c2 = (p3[0] + right[0] * beta, p3[1] + right[1] * beta)
        pressure = [[t, p] for t, p in zip(times, pressures)]
    points = [{"position": list(position), "handle": list(handle), "point_type": "bezier",
               **{name: settings[name] for name in PARAMETER_DEFAULTS}}
              for position, handle in ((p0, c1), (p3, c2))]
    return validate_strokes([{"id": uuid.uuid4().hex, "points": points,
                             "pressure": pressure, "pressure_settings": settings}])[0]


def transform_strokes(strokes, map_point):
    """Transform world-space strokes with a caller-supplied point mapping.

    Circular brush support scales by the mean of the two mapped local axes;
    translation/rotation preserve radius and uniform scaling is exact.
    """
    result = validate_strokes(strokes)
    for stroke in result:
        for point in stroke["points"]:
            x, y = point["position"]
            position = _position(map_point((x, y)))
            radius = point["radius"]
            x_axis = _position(map_point((x + radius, y)))
            y_axis = _position(map_point((x, y + radius)))
            point["radius"] = _number((math.dist(position, x_axis) + math.dist(position, y_axis)) / 2,
                                      "radius", PARAMETER_LIMITS["radius"])
            point["position"] = position
            point["handle"] = _position(map_point(tuple(point["handle"])))
    return result
