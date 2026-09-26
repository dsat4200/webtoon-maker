"""Shared, Qt-free catalog and validation for raster distortion modifiers."""
from __future__ import annotations

import copy
import math

from comic_editor.core.smudge import default_tool_settings, validate_strokes, validate_tool_settings


def number(label, default, low, high, suffix="", decimals=2):
    return dict(label=label, default=default, minimum=low, maximum=high,
                suffix=suffix, decimals=decimals, kind="number")


def choice(label, default, choices):
    return dict(label=label, default=default, choices=choices, kind="choice")


def text(label, default):
    return dict(label=label, default=default, kind="text")


def effect(name, parameters=None, gizmo="center", description=""):
    return dict(name=name, parameters=parameters or {}, gizmo=gizmo, description=description)


DISTORT_TYPES = {
    "distort_smudge": effect("Smudge Modifier", {
        "opacity": number("Opacity", 100., 0., 100., "%"),
        "strokes": dict(label="Strokes", default=[], kind="smudge_strokes"),
        "tool_settings": dict(label="Smudge tool", default=default_tool_settings(), kind="smudge_settings"),
    }, "smudge", "Paint editable smudge strokes on the image."),
    "distort_deform": effect("Deform", {
        "amount": number("Amount", 100., 0., 100., "%"),
        "mode": choice("Constraints", "rigid", {"rigid": "Rigid", "similarity": "Similarity"}),
    }, "pins", "Add pins on the canvas, then drag them to reshape the image."),
    "distort_perspective": effect("Perspective", {}, "quad", "Drag the four corner handles to change perspective."),
    "distort_twirl": effect("Twirl", {"angle": number("Angle", 0., -720., 720., "°")}, "radius"),
    "distort_pinch_punch": effect("Pinch / Punch", {"amount": number("Pinch / Punch", 25., -100., 100., "%")}, "radius"),
    "distort_spherical": effect("Spherical", {"amount": number("Amount", 0., -100., 100., "%")}, "radius"),
    "distort_ripple": effect("Ripple", {
        "amount": number("Amount", 10., -100., 100., " px"),
        "wavelength": number("Wavelength", 32., 1., 1000., " px"),
    }),
    "distort_lens_distortion": effect("Lens Distortion", {"amount": number("Amount", 0., -100., 100., "%")}),
    "distort_lens_correction": effect("Lens Correction", {
        "camera_profile": dict(label="Camera", default="", kind="profile"),
        "lens_profile": dict(label="Lens", default="", kind="profile"),
        "focal_length": number("Focal length", 50., 1., 3000., " mm"),
        "profile_xml": dict(label="Lens profiles", default="", kind="data"),
    }, "none", "Choose a camera and lens calibration, or import a Lensfun XML profile."),
    "distort_rectangular_to_polar": effect("Rectangular to Polar", {}, "none"),
    "distort_polar_to_rectangular": effect("Polar to Rectangular", {}, "none"),
    "distort_pixelate": effect("Pixelate", {"size": number("Block size", 8., 1., 500., " px", 0)}, "none"),
    "distort_displace": effect("Displace", {
        "amount": number("Strength", 20., -500., 500., " px"),
        "map_source": choice("Displacement map", "source", {"source": "Current image", "embedded": "Embedded image"}),
        "map_png": dict(label="Map image", default="", kind="image"),
        "scale_to_fit": dict(label="Scale map to fit", default=True, kind="bool"),
        "method": choice("Method", "sobel", {"sobel": "Intensity (Sobel)", "red_green": "Red / Green channels"}),
        "preserve_alpha": dict(label="Preserve alpha", default=False, kind="bool"),
    }, "none"),
    "distort_glitch": effect("Glitch", {
        "mode": choice("Type", "aberration_offset", {
            "aberration_distortion": "Aberration (Distortion)", "aberration_offset": "Aberration (Offset)",
            **{key: key.title() for key in ("shred", "blast", "slice", "sawtooth", "distort", "ripple", "waves")},
            **{key + "_color": key.title() + " (Color)" for key in ("shred", "blast", "slice", "sawtooth", "distort", "ripple", "waves")},
            **{key: key.replace("_", " ").title() for key in ("quantisation", "scramble", "fuzz", "warp", "light_streaks", "channel_flip", "data_blocks")},
        }),
        "amount": number("Strength", 0., -100., 100., "%"),
        "seed": number("Seed", 0, 0, 1000000, decimals=0),
        "offset_x": number("Horizontal offset", 10., -500., 500., " px"),
        "offset_y": number("Vertical offset", 0., -500., 500., " px"),
        "horizontal_strength": number("Horizontal strength", 0., -100., 100., "%"),
        "vertical_strength": number("Vertical strength", 0., -100., 100., "%"),
        "spacing": number("Pixel spacing", 256., 1., 1024., " px", 0),
        "stagger": dict(label="Stagger", default=False, kind="bool"),
        "slice_offset": number("Slice offset", 20., -500., 500., " px"),
        "loops": number("Wave loops", 4., 1., 64.),
        "turbulence": number("Turbulence", 50., 0., 100., "%"),
        "channels": number("Channels", 3, 1, 3, decimals=0),
        "channel_order": choice("Channel order", "rgb", {key: key.upper() for key in ("rgb", "rbg", "grb", "gbr", "brg", "bgr")}),
        "bidirectional": dict(label="Bidirectional", default=True, kind="bool"),
    }),
    "distort_shear": effect("Shear", {
        "horizontal": number("Horizontal shear", 0., -200., 200., "%"),
        "vertical": number("Vertical shear", 0., -200., 200., "%"),
        "horizontal_curve": dict(label="Horizontal curve", default=[[0., 0.], [1., 0.]], kind="curve"),
        "vertical_curve": dict(label="Vertical curve", default=[[0., 0.], [1., 0.]], kind="curve"),
    }, "shear", "Drag curve points to bend the image horizontally or vertically."),
    "distort_mirror": effect("Mirror Distort", {
        "mirrors": number("Mirrors", 1, 1, 32, decimals=0),
        "input_angle": number("Input angle", 0., -180., 180., "°"),
        "output_angle": number("Output angle", 0., -180., 180., "°"),
    }),
    "distort_affine": effect("Affine", {
        "rotation": number("Rotation", 0., -180., 180., "°"),
        "scale_x": number("Scale X", 100., 1., 1000., "%"),
        "scale_y": number("Scale Y", 100., 1., 1000., "%"),
        "offset_x": number("Offset X", 0., -200., 200., "%"),
        "offset_y": number("Offset Y", 0., -200., 200., "%"),
        "shear_x": number("Shear X", 0., -80., 80., "°"),
        "shear_y": number("Shear Y", 0., -80., 80., "°"),
    }),
    "distort_equations": effect("Equations", {
        "coordinates": choice("Coordinates", "cartesian", {"cartesian": "Cartesian (x, y)", "polar": "Polar (r, t)"}),
        "x_expression": text("X / R expression", "x"),
        "y_expression": text("Y / T expression", "y"),
        "a": number("a", 0., -10., 10.),
        "b": number("b", 0., -10., 10.),
        "c": number("c", 0., -10., 10.),
    }, "none", "Expressions use x, y, w, h, r, t, a, b, c, pi, sin, cos, tan, sqrt, abs, min and max."),
    "distort_mesh_warp": effect("Mesh Warp", {
        "rows": number("Rows", 4, 2, 16, decimals=0),
        "columns": number("Columns", 4, 2, 16, decimals=0),
        "smoothness": number("Smoothness", 50., 0., 100., "%"),
    }, "mesh", "Drag grid handles to warp the image. Changing grid size resets the mesh."),
}

SAMPLING_PARAMETERS = {
    "interpolation": choice("Sampling", "bilinear", {"nearest": "Nearest neighbor", "bilinear": "Bilinear", "bicubic": "Bicubic"}),
    "edges": choice("Outside image", "transparent", {"transparent": "Transparent", "white": "White", "clamp": "Repeat edge", "wrap": "Wrap", "mirror": "Mirror"}),
}


def gizmo_kind(modifier_type, parameters=None):
    if modifier_type == "distort_glitch":
        mode = (parameters or {}).get("mode", "aberration_offset")
        return "center" if mode in {"aberration_distortion", "ripple", "ripple_color"} else "none"
    return DISTORT_TYPES[modifier_type]["gizmo"]


def parameter_specs(modifier_type):
    return {**DISTORT_TYPES[modifier_type]["parameters"], **SAMPLING_PARAMETERS}


def default_parameters(modifier_type):
    return {key: copy.deepcopy(spec["default"]) for key, spec in parameter_specs(modifier_type).items()}


def validate_parameters(modifier_type, parameters):
    if modifier_type not in DISTORT_TYPES:
        raise ValueError("Unknown distortion effect")
    if not isinstance(parameters, dict):
        raise ValueError("Distortion parameters must be an object")
    specs = parameter_specs(modifier_type)
    unknown = set(parameters) - set(specs)
    if unknown:
        raise ValueError("Unknown distortion parameters: " + ", ".join(sorted(unknown)))
    result = default_parameters(modifier_type)
    for key, value in parameters.items():
        spec = specs[key]
        if spec["kind"] == "number":
            value = float(value)
            if not math.isfinite(value):
                raise ValueError("Distortion parameters must be finite")
            value = max(spec["minimum"], min(spec["maximum"], value))
            if not spec["decimals"]:
                value = round(value)
        elif spec["kind"] == "choice":
            if value not in spec["choices"]:
                raise ValueError(f"Unknown distortion {key}")
        elif spec["kind"] == "bool":
            value = bool(value)
        elif spec["kind"] == "smudge_strokes":
            value = validate_strokes(value)
        elif spec["kind"] == "smudge_settings":
            value = validate_tool_settings(value)
        elif spec["kind"] in {"text", "image", "profile", "data"}:
            if not isinstance(value, str):
                raise ValueError(f"Distortion {key} must be text")
            if spec["kind"] == "text" and len(value) > 1024:
                raise ValueError("Distortion expressions are limited to 1024 characters")
        elif spec["kind"] == "curve":
            if not isinstance(value, (list, tuple)) or not 2 <= len(value) <= 64:
                raise ValueError("Shear curves require 2 to 64 points")
            points = []
            for point in value:
                if not isinstance(point, (list, tuple)) or len(point) != 2:
                    raise ValueError("Shear curve points require a position and offset")
                position, offset = map(float, point)
                if not all(math.isfinite(v) for v in (position, offset)):
                    raise ValueError("Shear curve values must be finite")
                points.append([max(0., min(1., position)), max(-2., min(2., offset))])
            value = sorted(points)
            if value[0][0] != 0. or value[-1][0] != 1.:
                raise ValueError("Shear curves must include both endpoints")
            if len({point[0] for point in value}) != len(value):
                raise ValueError("Shear curve positions must be unique")
        result[key] = value
    return result


def initial_points(modifier_type, parameters=None):
    if modifier_type == "distort_perspective":
        return [(0., 0.), (1., 0.), (1., 1.), (0., 1.)]
    if modifier_type == "distort_mesh_warp":
        values = parameters or default_parameters(modifier_type)
        rows, columns = int(values["rows"]), int(values["columns"])
        return [(column / (columns - 1), row / (rows - 1))
                for row in range(rows) for column in range(columns)]
    if modifier_type == "distort_shear":
        return [(0., 0.), (0., 1.)]
    return []
