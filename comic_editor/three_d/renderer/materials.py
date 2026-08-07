"""Drawing-side 3D material definitions without asset-library dependencies."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import math


class SurfaceMaterial(str, Enum):
    DIFFUSE = "Diffuse"
    DIFFUSE_BSDF = "Diffuse"
    TOON = "Toon"
    UNSHADED = "Unshaded"


@dataclass(frozen=True, slots=True)
class ToonRampStop:
    position: float
    color: tuple[float, float, float]

    def __post_init__(self) -> None:
        if not math.isfinite(float(self.position)) or not 0.0 <= self.position <= 1.0:
            raise ValueError("toon-ramp positions must be between zero and one")
        if len(self.color) != 3 or any(not math.isfinite(v) or not 0.0 <= v <= 2.0 for v in self.color):
            raise ValueError("toon-ramp colors must have three values between zero and two")


def _default_stops() -> tuple[ToonRampStop, ...]:
    return (
        ToonRampStop(0.0, (0.18, 0.18, 0.18)),
        ToonRampStop(0.16, (0.42, 0.42, 0.42)),
        ToonRampStop(0.38, (0.68, 0.68, 0.68)),
        ToonRampStop(0.68, (1.0, 1.0, 1.0)),
    )


@dataclass(frozen=True, slots=True)
class ToonRamp:
    stops: tuple[ToonRampStop, ...] = field(default_factory=_default_stops)

    def __post_init__(self) -> None:
        ordered = tuple(sorted(self.stops, key=lambda stop: stop.position))
        if not 2 <= len(ordered) <= 8:
            raise ValueError("toon ramps require between two and eight stops")
        if any(a.position >= b.position for a, b in zip(ordered, ordered[1:])):
            raise ValueError("toon-ramp positions must be unique")
        object.__setattr__(self, "stops", ordered)


@dataclass(frozen=True, slots=True)
class DrawingMaterial:
    """Renderer assignment owned by Webtoon Maker.

    ``source_material_id`` points back to Blender's material slot while the rest
    of this object is an app-owned presentation override.
    """

    material_id: str
    name: str = "Material"
    surface: SurfaceMaterial = SurfaceMaterial.DIFFUSE
    base_color: tuple[float, float, float, float] = (1.0, 1.0, 1.0, 1.0)
    use_base_color_texture: bool = True
    use_vertex_color: bool = True
    toon_ramp: ToonRamp = field(default_factory=ToonRamp)
    outline_enabled: bool = True
    outline_color: tuple[float, float, float, float] = (0.0, 0.0, 0.0, 0.8)
    outline_thickness_px: float = 2.0
    source_material_id: str | None = None

    def __post_init__(self) -> None:
        if not self.material_id:
            raise ValueError("material id must be non-empty")
        object.__setattr__(self, "surface", SurfaceMaterial(self.surface))
        for label, color in (("base", self.base_color), ("outline", self.outline_color)):
            if len(color) != 4 or any(not math.isfinite(v) or not 0.0 <= v <= 1.0 for v in color):
                raise ValueError(f"{label} color must contain four values between zero and one")
        if not math.isfinite(self.outline_thickness_px) or not 0.0 <= self.outline_thickness_px <= 32.0:
            raise ValueError("outline thickness must be between zero and 32 pixels")
