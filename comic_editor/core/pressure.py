"""Fast pressure-to-brush response carried over from the drawing application."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PressureCurve:
    minimum: float = 0.1
    maximum: float = 1.0
    control_x: float = 0.5
    control_y: float = 0.55
    _lookup: tuple[float, ...] | None = field(default=None, init=False, repr=False)

    @property
    def min_ratio(self) -> float:
        return self.minimum

    @min_ratio.setter
    def min_ratio(self, value: float) -> None:
        self.minimum = value
        self._lookup = None

    @property
    def max_ratio(self) -> float:
        return self.maximum

    @max_ratio.setter
    def max_ratio(self, value: float) -> None:
        self.maximum = value
        self._lookup = None

    def clamp(self) -> None:
        self.minimum = max(0.0, min(1.0, float(self.minimum)))
        self.maximum = max(0.0, min(1.0, float(self.maximum)))
        self.control_x = max(0.0, min(1.0, float(self.control_x)))
        self.control_y = max(0.0, min(1.0, float(self.control_y)))
        self._lookup = None

    def to_dict(self) -> dict:
        self.clamp()
        return {
            "min_ratio": self.minimum, "max_ratio": self.maximum,
            "control_x": self.control_x, "control_y": self.control_y,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "PressureCurve":
        data = data or {}
        return cls(
            minimum=float(data.get("min_ratio", data.get("minimum", 0.1))),
            maximum=float(data.get("max_ratio", data.get("maximum", 1.0))),
            control_x=float(data.get("control_x", 0.5)),
            control_y=float(data.get("control_y", 0.55)),
        )

    def evaluate(self, value: float) -> float:
        value = max(0.0, min(1.0, value))
        low, high = 0.0, 1.0
        for _ in range(24):
            t = (low + high) / 2
            u = 1 - t
            x = 3 * u * u * t * self.control_x + 3 * u * t * t * self.control_x + t ** 3
            if x < value:
                low = t
            else:
                high = t
        t, u = low, 1 - low
        result = (
            u ** 3 * self.minimum
            + 3 * u * u * t * self.control_y
            + 3 * u * t * t * self.control_y
            + t ** 3 * self.maximum
        )
        return max(0.0, min(1.0, result))

    def evaluate_fast(self, value: float) -> float:
        if self._lookup is None:
            self._lookup = tuple(self.evaluate(index / 255) for index in range(256))
        scaled = max(0.0, min(1.0, value)) * 255
        lower = int(scaled)
        if lower >= 255:
            return self._lookup[255]
        fraction = scaled - lower
        return self._lookup[lower] + (self._lookup[lower + 1] - self._lookup[lower]) * fraction


@dataclass
class BrushPreset:
    name: str = "Linear"
    size_curve: PressureCurve = field(default_factory=PressureCurve)
    opacity_curve: PressureCurve = field(default_factory=PressureCurve)
    pressure_size: bool = True
    pressure_opacity: bool = True
    stroke_start_ratio: float = 1.0
    stroke_end_ratio: float = 1.0
    density: float = 1.0
    antialiasing: bool = True

    @property
    def use_pressure_size(self) -> bool:
        return self.pressure_size

    @property
    def use_pressure_opacity(self) -> bool:
        return self.pressure_opacity

    def clamp(self) -> None:
        self.name = self.name.strip() or "Untitled"
        self.size_curve.clamp()
        self.opacity_curve.clamp()
        self.stroke_start_ratio = max(0.1, min(1.0, float(self.stroke_start_ratio)))
        self.stroke_end_ratio = max(0.1, min(1.0, float(self.stroke_end_ratio)))
        self.density = max(0.1, min(3.0, float(self.density)))

    def to_dict(self) -> dict:
        self.clamp()
        return {
            "name": self.name,
            "size_curve": self.size_curve.to_dict(),
            "opacity_curve": self.opacity_curve.to_dict(),
            "use_pressure_size": self.pressure_size,
            "use_pressure_opacity": self.pressure_opacity,
            "stroke_start_ratio": self.stroke_start_ratio,
            "stroke_end_ratio": self.stroke_end_ratio,
            "density": self.density,
            "antialiasing": self.antialiasing,
        }

    @classmethod
    def from_dict(cls, data: dict | None) -> "BrushPreset":
        data = data or {}
        preset = cls(
            name=str(data.get("name", "Linear")),
            size_curve=PressureCurve.from_dict(data.get("size_curve")),
            opacity_curve=PressureCurve.from_dict(data.get("opacity_curve")),
            pressure_size=bool(data.get(
                "use_pressure_size", data.get("pressure_size", True)
            )),
            pressure_opacity=bool(data.get(
                "use_pressure_opacity", data.get("pressure_opacity", True)
            )),
            stroke_start_ratio=float(data.get("stroke_start_ratio", 1.0)),
            stroke_end_ratio=float(data.get("stroke_end_ratio", 1.0)),
            density=float(data.get("density", 1.0)),
            antialiasing=bool(data.get("antialiasing", True)),
        )
        preset.clamp()
        return preset


def default_pencil_presets() -> list[dict]:
    return [BrushPreset().to_dict()]
