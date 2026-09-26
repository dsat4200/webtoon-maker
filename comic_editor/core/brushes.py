"""Portable brush descriptions and device input, independent of raster storage.

Image materials retain their full canvas (including padding). PNGs are embedded
as base64 so imported presets remain usable after the source SUT is moved.
All distances are document pixels, angles degrees, times seconds and normalized
controls 0..1 unless their field documents otherwise. No Qt types live here.
"""
from __future__ import annotations

import base64
import io
import math
from dataclasses import asdict, dataclass, field, fields, replace
from functools import lru_cache
from typing import Any


# These are signed HSV offsets; physical quantities such as size and opacity
# must never inherit their negative response ranges.
SIGNED_COLOR_DYNAMICS = frozenset({"hue_shift", "saturation_shift", "luminosity_shift"})


def finite(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if math.isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def clamp(value: Any, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, finite(value, low)))


@dataclass(frozen=True)
class BrushInput:
    x: float
    y: float
    pressure: float = 1.0
    tilt_x: float = 0.0
    tilt_y: float = 0.0
    rotation: float = 0.0
    time: float = 0.0

    def sanitized(self) -> "BrushInput":
        return BrushInput(finite(self.x), finite(self.y), clamp(self.pressure),
                          clamp(self.tilt_x, -90, 90), clamp(self.tilt_y, -90, 90),
                          finite(self.rotation) % 360, finite(self.time))


@dataclass(frozen=True)
class BrushDynamics:
    """Independent response channels, multiplied together when combined.

    Curves are piecewise linear normalized knots. Random is a minimum ratio:
    random=1 is constant, random=0 permits the whole 0..1 range. Signed color
    offset channels also allow negative minima (random=-1 permits -1..1).
    Velocity is normalized against velocity_scale in document pixels per second.
    """
    pressure: bool = False
    minimum: float = 0.0
    pressure_curve: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 1.0))
    tilt: bool = False
    tilt_minimum: float = 0.0
    tilt_maximum: float = 1.0  # physical response can enlarge up to 1000%
    tilt_curve: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 1.0))
    velocity: bool = False
    velocity_minimum: float = 0.0
    velocity_curve: tuple[tuple[float, float], ...] = ((0.0, 1.0), (1.0, 0.0))
    velocity_scale: float = 2000.0
    random: float = 1.0

    @classmethod
    def from_dict(cls, data: dict | None, *, signed: bool = False) -> "BrushDynamics":
        data = data if isinstance(data, dict) else {}
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        for name in ("pressure_curve", "tilt_curve", "velocity_curve"):
            if name in values:
                try:
                    knots = sorted({(clamp(p[0]), clamp(p[1])) for p in values[name][:256]})
                    # Last control at an identical x wins, avoiding zero division.
                    values[name] = tuple(dict(knots).items()) or getattr(cls(), name)
                except (TypeError, ValueError, IndexError):
                    values.pop(name)
        for name in ("minimum", "tilt_minimum", "velocity_minimum", "random"):
            if name in values:
                values[name] = clamp(values[name], -1 if signed else 0, 1)
        if "tilt_maximum" in values:
            values["tilt_maximum"] = clamp(values["tilt_maximum"], 0, 1 if signed else 10)
        if "velocity_scale" in values:
            values["velocity_scale"] = clamp(values["velocity_scale"], 1, 100000)
        return cls(**values)


@dataclass(frozen=True)
class BrushTip:
    name: str = "Circle"
    width: int = 1
    height: int = 1
    png: str = ""
    mode: str = "mask"  # mask (alpha coverage), dual_color (black/main white/sub), color
    shape: str = "circle"  # circle, square, image

    @classmethod
    def from_dict(cls, data: dict | None) -> "BrushTip":
        data = data if isinstance(data, dict) else {}
        return cls(str(data.get("name", "Tip"))[:512],
                   int(clamp(data.get("width", 1), 1, 16384)),
                   int(clamp(data.get("height", 1), 1, 16384)),
                   str(data.get("png", "")),
                   data.get("mode", "mask") if data.get("mode", "mask") in
                   {"mask", "dual_color", "color"} else "mask",
                   data.get("shape", "circle") if data.get("shape", "circle") in
                   {"circle", "square", "image"} else "circle")


@dataclass(frozen=True)
class BrushTexture:
    name: str = "Texture"
    png: str = ""
    scale: float = 1.0
    angle: float = 0.0
    density: float = 1.0
    mode: str = "multiply"  # multiply, subtract, compare, overlay, height, outline
    brightness: float = 0.0
    contrast: float = 0.0
    invert: bool = False
    per_dab: bool = False
    emphasize_density: bool = False

    @classmethod
    def from_dict(cls, data: dict | None) -> "BrushTexture":
        data = data if isinstance(data, dict) else {}
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        for name, bounds in {"scale": (.01, 100), "angle": (-3600, 3600),
                             "density": (0, 1), "brightness": (-1, 1),
                             "contrast": (-1, 1)}.items():
            if name in values:
                values[name] = clamp(values[name], *bounds)
        return cls(**values)


@dataclass(frozen=True)
class BrushDefinition:
    id: str = "round-pen"
    name: str = "Round pen"
    size: float = 16.0
    size_by_view: bool = False  # keep nominal diameter at its 100% view size
    opacity: float = 1.0  # stroke opacity, separate from per-dab coverage
    density: float = 1.0
    hardness: float = 1.0
    thickness: float = 1.0
    thickness_axis: str = "horizontal"
    angle: float = 0.0
    direction: str = "fixed"  # fixed, stroke, tilt, rotation
    flip_x: str = "none"  # none, fixed, random, alternate, reverse
    flip_y: str = "none"
    antialiasing: int = 2  # 0 none, 1 weak, 2 medium, 3 strong
    minimum_pixel: bool = True
    spacing: float = .08  # diameter ratio or absolute pixels
    spacing_mode: str = "relative"
    density_by_gap: bool = False
    tips: tuple[BrushTip, ...] = field(default_factory=lambda: (BrushTip(),))
    repeat_mode: str = "forward"  # forward, reverse, random, pingpong, hold_last, once, one_random
    ribbon: bool = False
    continuous: bool = False
    continuous_rate: float = 60.0  # dabs/second while held
    correct_velocity: bool = False
    spray: bool = False
    particle_size: float = 4.0
    particle_size_relative: bool = False
    particle_density: float = 8.0  # particles per scheduled dab
    spray_deviation: float = .5
    particle_angle: float = 0.0  # degrees, independent of the whole brush angle
    particle_angle_random: float = 0.0  # additive spread: 1 spans a full turn
    particle_direction: str = "whole_spray"  # fixed, stroke, whole_spray, center; radial/random are legacy
    texture: BrushTexture | None = None
    dynamics: dict[str, BrushDynamics] = field(default_factory=dict)
    global_pressure_curve: tuple[tuple[float, float], ...] = ((0.0, 0.0), (1.0, 1.0))
    blending_mode: str = "normal"
    blend_tips: str = "normal"  # normal or darken
    mixing_mode: str = "none"  # none, blend, running, smear
    mixing_space: str = "standard"  # standard, perceptual
    paint_amount: float = .5
    paint_density: float = 1.0
    color_stretch: float = .5
    blur: float = 0.0  # normalized automatic strength; retained for legacy presets
    blur_mode: str = "automatic"  # automatic size-linked approximation, fixed pixel width
    blur_width: float = 0.0  # fixed Running-color pickup width in document pixels
    watercolor_edge: float = 0.0  # width in pixels
    watercolor_opacity: float = .5
    watercolor_darkness: float = .5
    watercolor_blur: float = 0.0
    watercolor_after: bool = True
    watercolor_mode: str = "standard"
    watercolor_strength: float = .5
    watercolor_vividness: float = .5
    dual: "BrushDefinition | None" = None
    dual_mode: str = "multiply"
    dual_link_size: bool = False
    dual_apply_rgb: bool = False
    hue_jitter: float = 0.0
    saturation_jitter: float = 0.0
    luminosity_jitter: float = 0.0
    sub_color_mix: float = 0.0
    # Deterministic offsets, optionally driven by their own response channels.
    # Hue uses turns; saturation and value use signed normalized HSV units.
    # Separate from authored jitter so existing saved presets keep their look.
    hue_shift: float = 0.0
    saturation_shift: float = 0.0
    luminosity_shift: float = 0.0
    sub_color_amount: float = 0.0
    color_change_target: str = "main"  # main, sub, both
    stroke_hue_jitter: float = 0.0
    stroke_saturation_jitter: float = 0.0
    stroke_luminosity_jitter: float = 0.0
    stroke_sub_color_mix: float = 0.0
    sub_color: tuple[int, int, int, int] = (255, 255, 255, 255)
    stabilization: float = 0.0
    post_correction: float = 0.0
    taper_mode: str = "length"  # length in px, percentage, or monotonic fade
    taper_start: float = 0.0  # document-pixel length
    taper_end: float = 0.0
    taper_minimum: float = 0.0
    taper_minima: dict[str, float] = field(default_factory=dict)
    taper_parameters: tuple[str, ...] = ("size",)
    source: dict = field(default_factory=dict)
    warnings: tuple[str, ...] = ()

    def with_size(self, size: float) -> "BrushDefinition":
        """Change drawing size, including only an explicitly linked second tip.

        Texture scale, fixed particle size, edge widths and other independent
        document distances do not follow the brush-size control. Preview zoom
        is a separate operation and must not be used to prepare drawing presets.
        """
        dual = self.dual
        if dual is not None and self.dual_link_size and size != self.size:
            dual = replace(dual, size=dual.size * size / max(.1, self.size))
        return replace(self, size=size, dual=dual)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None, *, _depth: int = 0) -> "BrushDefinition":
        data = data if isinstance(data, dict) else {}
        values = {f.name: data[f.name] for f in fields(cls) if f.name in data}
        limits = {
            "size": (.1, 4096), "opacity": (0, 1), "density": (0, 1),
            "hardness": (0, 1), "thickness": (.01, 10), "angle": (-36000, 36000),
            "spacing": (.001, 4096), "continuous_rate": (1, 240),
            "particle_size": (.001 if data.get("particle_size_relative") else .1, 4096), "particle_density": (1, 256),
            "particle_angle": (-36000, 36000), "particle_angle_random": (0, 1),
            "spray_deviation": (-1, 1), "paint_amount": (0, 1), "paint_density": (0, 1),
            "color_stretch": (0, 1), "blur": (0, 1), "blur_width": (0, 10000), "watercolor_edge": (0, 100),
            "watercolor_opacity": (0, 1), "watercolor_darkness": (0, 1),
            "watercolor_blur": (0, 100), "hue_jitter": (0, 1),
            "watercolor_strength": (0, 1), "watercolor_vividness": (0, 1),
            "saturation_jitter": (0, 1), "luminosity_jitter": (0, 1),
            "hue_shift": (-1, 1), "saturation_shift": (-1, 1),
            "luminosity_shift": (-1, 1), "sub_color_amount": (0, 1),
            "sub_color_mix": (0, 1), "stroke_hue_jitter": (0, 1),
            "stroke_saturation_jitter": (0, 1), "stroke_luminosity_jitter": (0, 1),
            "stroke_sub_color_mix": (0, 1), "stabilization": (0, 1), "post_correction": (0, 1),
            "taper_start": (0, 10000), "taper_end": (0, 10000), "taper_minimum": (0, 1),
        }
        for name, bounds in limits.items():
            if name in values:
                values[name] = clamp(values[name], *bounds)
        diagnostics = list(data.get("warnings") or [])
        choices = {
            "direction": {"fixed","stroke","tilt","rotation"},
            "thickness_axis": {"horizontal","vertical"},
            "flip_x": {"none","fixed","random","alternate","reverse"},
            "flip_y": {"none","fixed","random","alternate","reverse"},
            "repeat_mode": {"forward","reverse","random","pingpong","hold_last","once","one_random"},
            "spacing_mode": {"relative","fixed"},
            "particle_direction": {"fixed","stroke","whole_spray","center","radial","random"},
            "mixing_mode": {"none","blend","running","smear"},
            "mixing_space": {"standard","perceptual"},
            "blur_mode": {"automatic","fixed"},
            "watercolor_mode": {"standard","vivid"},
            "color_change_target": {"main","sub","both"},
            "taper_mode": {"length","percentage","fade"},
        }
        defaults = cls()
        # Early authored presets used "fixed" to mean the resolved whole-brush
        # angle. New presets always serialize particle_angle, even when it is 0.
        if "particle_angle" not in data and values.get("particle_direction") == "fixed":
            values["particle_direction"] = "whole_spray"
        for name, allowed in choices.items():
            if name in values and (not isinstance(values[name],str) or values[name] not in allowed):
                diagnostics.append(f"Unrecognized {name} {values[name]!r}; using {getattr(defaults,name)!r}.")
                values[name] = getattr(defaults,name)
        values["antialiasing"] = int(clamp(data.get("antialiasing", 2), 0, 3))
        values["size_by_view"] = bool(data.get("size_by_view", False))
        values["tips"] = tuple(BrushTip.from_dict(t) for t in
                               (data.get("tips") or [{}])[:1024])
        values["texture"] = BrushTexture.from_dict(data["texture"]) if data.get("texture") else None
        values["dynamics"] = {str(k): BrushDynamics.from_dict(v, signed=k in SIGNED_COLOR_DYNAMICS) for k, v in
                              (data.get("dynamics") or {}).items()}
        values["global_pressure_curve"] = BrushDynamics.from_dict({
            "pressure_curve": data.get("global_pressure_curve", defaults.global_pressure_curve)
        }).pressure_curve
        values["dual"] = (cls.from_dict(data["dual"], _depth=_depth+1)
                          if isinstance(data.get("dual"), dict) and _depth < 1 else None)
        values["warnings"] = tuple(dict.fromkeys(str(w)[:4096] for w in diagnostics[:256]))
        values["taper_parameters"] = tuple(data.get("taper_parameters", ("size",)) or ())
        values["taper_minima"] = {str(k):clamp(v) for k,v in
                                  (data.get("taper_minima") or {}).items()}
        try:
            values["sub_color"] = tuple(int(clamp(c, 0, 255)) for c in data.get("sub_color", (255,)*4))
            if len(values["sub_color"]) != 4:
                values["sub_color"] = (255,)*4
        except TypeError:
            values["sub_color"] = (255,)*4
        return cls(**values)


@dataclass(frozen=True)
class BrushDab:
    x: float
    y: float
    size: float
    opacity: float = 1.0
    density: float = 1.0
    angle: float = 0.0
    thickness: float = 1.0
    tip_index: int = 0
    flip_x: bool = False
    flip_y: bool = False
    hue: float = 0.0
    saturation: float = 0.0
    luminosity: float = 0.0
    sub_color_mix: float = 0.0
    distance: float = 0.0
    time: float = 0.0
    pressure: float = 1.0
    texture_density: float = 1.0
    paint_amount: float = 1.0
    paint_density: float = 1.0
    blur: float = 1.0
    hardness: float = 1.0
    color_stretch: float = 1.0
    center_x: float | None = None
    center_y: float | None = None
    particle_index: int = 0
    particle_count: int = 1


def _png(image) -> str:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return base64.b64encode(output.getvalue()).decode("ascii")


@lru_cache(maxsize=1)
def _builtin_materials() -> dict[str, BrushTip | BrushTexture]:
    """Small original materials; no CSP or asset-store artwork is bundled."""
    from PIL import Image, ImageDraw, ImageFilter
    import numpy as np
    rng = np.random.default_rng(31077)
    grain = rng.uniform(.2, 1, (128, 128))
    grain = np.asarray(Image.fromarray((grain*255).astype('uint8')).filter(
        ImageFilter.GaussianBlur(.35)))
    materials: dict[str, BrushTip | BrushTexture] = {
        "paper": BrushTexture(name="Drawing paper", png=_png(Image.fromarray(grain)),
                              density=.75, scale=.6, mode="multiply")}
    for index in range(3):
        image = Image.new("RGBA", (96, 96))
        draw = ImageDraw.Draw(image)
        draw.polygon([(48, 8+index*4), (75, 38), (70, 63), (48, 87), (23, 62), (21, 37)],
                     fill=(0, 0, 0, 255))
        draw.line([(48, 12), (48, 84)], fill=(0, 0, 0, 150), width=2)
        materials[f"leaf{index}"] = BrushTip(f"Leaf {index+1}",96,96,_png(image),shape="image")
    image = Image.new("RGBA", (64, 96))
    draw = ImageDraw.Draw(image)
    # Include the neighboring face-on links so the registered top and bottom
    # are the same phase of a periodic chain, with no cut link at the seam.
    for center in (0,96):
        draw.ellipse((14,center-38,50,center+38),outline=(0,0,0,255),width=7)
    draw.ellipse((25,10,39,86),outline=(0,0,0,255),width=6)
    materials["chain"] = BrushTip("Chain link",64,96,_png(image),shape="image")
    image = Image.new("RGBA", (128,128))
    draw = ImageDraw.Draw(image)
    for _ in range(80):
        x,y = rng.normal(64, 22, 2)
        radius = float(rng.uniform(.5,6))
        draw.ellipse((x-radius,y-radius,x+radius,y+radius),fill=(0,0,0,255))
    materials["splatter"] = BrushTip("Splatter",128,128,_png(image),shape="image")
    image=Image.new("RGBA",(96,96))
    draw=ImageDraw.Draw(image)
    for angle in range(0,360,72):
        x=48+20*math.cos(math.radians(angle))
        y=48+20*math.sin(math.radians(angle))
        draw.ellipse((x-17,y-17,x+17,y+17),fill=(205,69,135,255))
    draw.ellipse((35,35,61,61),fill=(246,190,51,255))
    materials["flower"] = BrushTip("Color flower",96,96,_png(image),mode="color",shape="image")
    yy,xx=np.mgrid[:8,:8]
    for name,pattern in {
        "pixel-dots": (xx%2==0)&(yy%2==0),
        "pixel-stripes": yy%4<2,
        "pixel-checker": (xx+yy)%2==0,
    }.items():
        materials[name]=BrushTexture(name=name.replace("-"," ").title(),
            png=_png(Image.fromarray((pattern*255).astype("uint8"))),
            density=1,scale=1,mode="multiply",per_dab=False)
    return materials


def default_brushes() -> list[BrushDefinition]:
    materials = _builtin_materials()
    pressure = {"size": BrushDynamics(pressure=True, minimum=.05)}
    return [
        BrushDefinition(dynamics=pressure),
        BrushDefinition(id="pencil",name="Graphite pencil",size=12,density=.55,hardness=.85,
                        spacing=.12,texture=materials["paper"],dynamics={
                            "size": BrushDynamics(pressure=True,minimum=.25),
                            "density": BrushDynamics(pressure=True,minimum=.15)}),
        BrushDefinition(id="airbrush",name="Soft airbrush",size=100,hardness=0,
                        density=.08,spacing=.12,continuous=True,dynamics={
                            "density":BrushDynamics(pressure=True,minimum=.05)}),
        BrushDefinition(id="watercolor",name="Watercolor wash",size=60,opacity=.6,
                        density=.45,hardness=.6,spacing=.12,mixing_mode="blend",
                        paint_amount=.4,color_stretch=.4,watercolor_edge=2,
                        watercolor_darkness=.2,texture=materials["paper"],dynamics=pressure),
        BrushDefinition(id="wet-paint",name="Wet paint",size=40,hardness=.75,
                        mixing_mode="running",paint_amount=.6,color_stretch=.7,
                        dynamics=pressure),
        BrushDefinition(id="chain",name="Chain ribbon",size=36,spacing=.75,
                        tips=(materials["chain"],),ribbon=True,direction="stroke"),
        BrushDefinition(id="foliage",name="Foliage",size=90,particle_size=28,
                        spray=True,particle_density=4,spacing=.4,tips=tuple(
                            materials[f"leaf{i}"] for i in range(3)),repeat_mode="random",
                        dynamics={"angle":BrushDynamics(random=0),
                                  "particle_size":BrushDynamics(random=.5)},
                        hue_jitter=.06,luminosity_jitter=.15),
        BrushDefinition(id="splatter",name="Blood / ink splatter",size=80,spacing=.8,
                        tips=(materials["splatter"],),dynamics={
                            "size":BrushDynamics(pressure=True,minimum=.4,random=.6),
                            "angle":BrushDynamics(random=0)}),
        BrushDefinition(id="calligraphy",name="Flat calligraphy",size=32,thickness=.2,
                        angle=45,dynamics=pressure),
        BrushDefinition(id="dual-pencil",name="Dual textured pencil",size=25,
                        density=.8,texture=materials["paper"],dynamics=pressure,
                        dual=BrushDefinition(size=25,hardness=.25,density=.7),
                        dual_link_size=True),
        BrushDefinition(id="color-stamp",name="Color stamp",size=42,spacing=1.2,
                        tips=(materials["flower"],),direction="stroke"),
        BrushDefinition(id="pixel-dots",name="Pixel dots",size=36,spacing=.2,
                        antialiasing=0,texture=materials["pixel-dots"]),
        BrushDefinition(id="pixel-stripes",name="Pixel stripes",size=36,spacing=.2,
                        antialiasing=0,texture=materials["pixel-stripes"]),
        BrushDefinition(id="pixel-checker",name="Pixel checker",size=36,spacing=.2,
                        antialiasing=0,texture=materials["pixel-checker"]),
    ]
