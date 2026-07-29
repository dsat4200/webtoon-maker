"""Small, drawing-only application preferences."""
from __future__ import annotations

import json
import dataclasses
from dataclasses import asdict, dataclass, field
from pathlib import Path

from PySide6.QtCore import QStandardPaths
from comic_editor.core.pressure import BrushPreset, default_pencil_presets


def default_hotkeys() -> dict[str, str]:
    return {
        "raster_pencil": "P",
        "raster_eraser": "E",
        "object_select": "S",
        "transform": "T",
        "shape_edit": "B",
        "save": "Ctrl+S",
        "undo": "Ctrl+Z",
        "redo": "Ctrl+Shift+Z",
        "reset_view": "Ctrl+0",
        "toggle_grid": "Alt+G",
    }


@dataclass
class TextPreset:
    name: str = "Default"
    font_family: str = "Segoe UI"
    font_size: float = 32.0
    bold: bool = False
    italic: bool = False
    kerning: float = 0.0
    layout_mode: str = "strict"
    horizontal_alignment: str = "center"
    vertical_alignment: str = "middle"
    margin: float = 24.0
    transform_snap: bool = True

    def clamp(self) -> None:
        self.name = self.name.strip() or "Preset"
        self.font_size = max(6.0, min(288.0, float(self.font_size)))
        self.kerning = max(-20.0, min(100.0, float(self.kerning)))
        if self.layout_mode not in {"free", "strict"}:
            self.layout_mode = "strict"
        if self.horizontal_alignment not in {"left", "center", "right"}:
            self.horizontal_alignment = "center"
        if self.vertical_alignment not in {"top", "middle", "bottom"}:
            self.vertical_alignment = "middle"
        self.margin = max(0.0, min(500.0, float(self.margin)))

    def to_dict(self) -> dict:
        self.clamp()
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict | None) -> "TextPreset":
        fields = {item.name for item in dataclasses.fields(cls)}
        preset = cls(**{
            key: value for key, value in (data or {}).items() if key in fields
        })
        preset.clamp()
        return preset


def default_text_presets() -> list[dict]:
    return [TextPreset().to_dict()]


@dataclass
class EditorSettings:
    settings_version: int = 5
    tablet_mode: bool = False
    brush_size: int = 12
    eraser_size: int = 28
    brush_color: str = "#111111"
    eraser_square: bool = False
    snap_to_grid: bool = True
    page_scope_select: bool = True
    canvas_renderer: str = "auto"
    predictive_ink: bool = True
    transform_mode: str = "free"
    transform_snap_to_grid: bool = True
    rectangle_edit_mode: str = "normal"
    text_presets: list[dict] = field(default_factory=default_text_presets)
    active_text_preset: str = "Default"
    pencil_presets: list[dict] = field(default_factory=default_pencil_presets)
    active_pencil_preset: str = "Linear"
    pencil_size_px: dict[str, int] = field(default_factory=lambda: {
        "small": 4, "medium": 12, "large": 22,
    })
    eraser_size_px: dict[str, int] = field(default_factory=lambda: {
        "small": 8, "medium": 28, "large": 44,
    })
    pencil_default_size: str = "medium"
    eraser_default_size: str = "medium"
    active_pencil_size: str = "medium"
    active_eraser_size: str = "medium"
    hotkeys: dict[str, str] = field(default_factory=default_hotkeys)
    recent_series: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        # Settings are also constructed directly by tests and UI helpers, so
        # keep the invariants on the value object instead of relying on every
        # caller to remember a separate normalization step.
        self.clamp()

    def clamp(self) -> None:
        self.settings_version = 5
        self.brush_size = max(1, min(200, int(self.brush_size)))
        self.eraser_size = max(2, min(400, int(self.eraser_size)))
        defaults = {
            "pencil": {"small": 4, "medium": self.brush_size, "large": 22},
            "eraser": {"small": 8, "medium": self.eraser_size, "large": 44},
        }
        for field_name, minimum, maximum in (
            ("pencil_size_px", 1, 200), ("eraser_size_px", 2, 400),
        ):
            supplied = getattr(self, field_name, None) or {}
            kind = "pencil" if field_name.startswith("pencil") else "eraser"
            setattr(self, field_name, {
                key: max(minimum, min(maximum, int(supplied.get(
                    key, defaults[kind][key]
                ))))
                for key in ("small", "medium", "large")
            })
        self.brush_size = self.pencil_size_px["medium"]
        self.eraser_size = self.eraser_size_px["medium"]
        for field_name in (
            "pencil_default_size", "eraser_default_size",
            "active_pencil_size", "active_eraser_size",
        ):
            if getattr(self, field_name) not in {"small", "medium", "large"}:
                setattr(self, field_name, "medium")
        pencil_presets: list[dict] = []
        pencil_names: set[str] = set()
        for item in self.pencil_presets or []:
            preset = BrushPreset.from_dict(item if isinstance(item, dict) else None)
            if preset.name.casefold() == "linear":
                preset.name = "Linear"
            if preset.name.casefold() in pencil_names:
                continue
            pencil_names.add(preset.name.casefold())
            pencil_presets.append(preset.to_dict())
        linear_index = next((
            index for index, item in enumerate(pencil_presets)
            if item["name"] == "Linear"
        ), -1)
        if linear_index < 0:
            pencil_presets.insert(0, BrushPreset().to_dict())
        elif linear_index > 0:
            pencil_presets.insert(0, pencil_presets.pop(linear_index))
        self.pencil_presets = pencil_presets
        if self.active_pencil_preset not in {
            item["name"] for item in pencil_presets
        }:
            self.active_pencil_preset = "Linear"
        if self.canvas_renderer not in {"auto", "gpu", "raster"}:
            self.canvas_renderer = "auto"
        if self.transform_mode not in {"free", "uniform"}:
            self.transform_mode = "free"
        if self.rectangle_edit_mode not in {"normal", "free"}:
            self.rectangle_edit_mode = "normal"
        self.transform_snap_to_grid = bool(self.transform_snap_to_grid)
        presets: list[dict] = []
        names: set[str] = set()
        for item in self.text_presets or []:
            preset = TextPreset.from_dict(item if isinstance(item, dict) else None)
            if preset.name.casefold() == "default":
                preset.name = "Default"
            if preset.name.casefold() in names:
                continue
            names.add(preset.name.casefold())
            presets.append(preset.to_dict())
        default_index = next(
            (index for index, item in enumerate(presets) if item["name"] == "Default"),
            -1,
        )
        if default_index < 0:
            presets.insert(0, TextPreset().to_dict())
        elif default_index > 0:
            presets.insert(0, presets.pop(default_index))
        self.text_presets = presets
        available = {item["name"] for item in presets}
        if self.active_text_preset not in available:
            self.active_text_preset = "Default"
        supplied = self.hotkeys or {}
        self.hotkeys = {
            key: str(supplied.get(key, sequence))
            for key, sequence in default_hotkeys().items()
        }
        self.recent_series = list(dict.fromkeys(self.recent_series or []))[:12]

    def pencil_size(self) -> int:
        return self.pencil_size_px[self.active_pencil_size]

    def active_brush_preset(self) -> BrushPreset:
        return BrushPreset.from_dict(next(
            (
                item for item in self.pencil_presets
                if item.get("name") == self.active_pencil_preset
            ),
            self.pencil_presets[0],
        ))

    def active_eraser_pixels(self) -> int:
        return self.eraser_size_px[self.active_eraser_size]


def settings_path() -> Path:
    root = Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation))
    return root / "settings.json"


def load_settings() -> EditorSettings:
    path = settings_path()
    if path.is_file():
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise TypeError("Settings must be an object")
            if int(raw.get("settings_version", 1)) < 2:
                raw.setdefault("page_scope_select", True)
                raw.setdefault("transform_mode", "free")
                raw.setdefault("transform_snap_to_grid", True)
            if int(raw.get("settings_version", 1)) < 3:
                raw.setdefault("pencil_size_px", {
                    "small": 4, "medium": int(raw.get("brush_size", 12)), "large": 22,
                })
                raw.setdefault("eraser_size_px", {
                    "small": 8, "medium": int(raw.get("eraser_size", 28)), "large": 44,
                })
                raw.setdefault("pencil_presets", default_pencil_presets())
                raw.setdefault("active_pencil_preset", "Linear")
            if int(raw.get("settings_version", 1)) < 4:
                hotkeys = raw.get("hotkeys")
                if isinstance(hotkeys, dict):
                    if "shape_edit" not in hotkeys and "bound_edit" in hotkeys:
                        hotkeys["shape_edit"] = hotkeys["bound_edit"]
                    hotkeys.pop("bound_edit", None)
            if int(raw.get("settings_version", 1)) < 5:
                raw.setdefault("rectangle_edit_mode", "normal")
            raw["settings_version"] = 5
            valid = {item.name for item in dataclasses.fields(EditorSettings)}
            result = EditorSettings(**{
                key: value for key, value in raw.items() if key in valid
            })
            result.clamp()
            return result
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return EditorSettings()


def save_settings(settings: EditorSettings) -> None:
    settings.clamp()
    path = settings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(asdict(settings), indent=2), encoding="utf-8")
    temporary.replace(path)
