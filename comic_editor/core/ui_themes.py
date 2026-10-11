"""Per-user interface palettes, independent of document and artwork colors.

Bundled palettes are read-only. Editing or importing one creates a custom
palette, and adjustment sliders always derive from its original colors.
"""
from __future__ import annotations

import colorsys
from copy import deepcopy
from dataclasses import dataclass, field
import json
import math
from pathlib import Path
import re
from uuid import uuid4


PALETTE_ROLES = {
    "window": "Window background",
    "panel": "Panels",
    "surface": "Raised surfaces",
    "input": "Input fields",
    "button": "Buttons",
    "hover": "Hover highlight",
    "border": "Borders and dividers",
    "text": "Main text and icons",
    "muted_text": "Secondary text",
    "accent": "Accent",
    "accent_text": "Text on accent",
    "selection": "Selected items",
    "selection_text": "Selected item text",
    "canvas": "Canvas surround",
    "warning": "Warnings",
    "danger": "Destructive actions",
}

DEFAULT_PALETTE = {
    "window": "#171b22",
    "panel": "#20252e",
    "surface": "#282f3b",
    "input": "#151a21",
    "button": "#303847",
    "hover": "#3a4659",
    "border": "#3c4655",
    "text": "#edf1f7",
    "muted_text": "#a4aebc",
    "accent": "#89b7ff",
    "accent_text": "#15263f",
    "selection": "#395679",
    "selection_text": "#ffffff",
    "canvas": "#101319",
    "warning": "#e8bc68",
    "danger": "#e68189",
}

LIGHT_PALETTE = {
    "window": "#edf0f5",
    "panel": "#f8f9fc",
    "surface": "#ffffff",
    "input": "#ffffff",
    "button": "#e2e7ef",
    "hover": "#d4ddeb",
    "border": "#c5cedb",
    "text": "#242c39",
    "muted_text": "#667184",
    "accent": "#346ac1",
    "accent_text": "#ffffff",
    "selection": "#d4e3fb",
    "selection_text": "#1b3967",
    "canvas": "#d8dee8",
    "warning": "#8a610c",
    "danger": "#ad3544",
}

ADJUSTMENT_RANGES = {
    "hue": (-180, 180),
    "saturation": (-100, 100),
    "brightness": (-100, 100),
    "contrast": (-100, 100),
}
_COLOR = re.compile(r"#[0-9a-fA-F]{6}\Z")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_THEME_FORMAT = "webtoon-maker-ui-theme"
_LIBRARY_FORMAT = "webtoon-maker-ui-theme-library"
_FORMAT_VERSION = 1


def _name(value: object) -> str:
    if not isinstance(value, str):
        raise ValueError("A theme name must be text.")
    value = value.strip()
    if not value or len(value) > 200 or any(ord(char) < 32 for char in value):
        raise ValueError("A theme name must contain 1 to 200 visible characters.")
    return value


@dataclass
class UITheme:
    id: str
    name: str
    palette: dict[str, str]
    adjustments: dict[str, int] = field(
        default_factory=lambda: dict.fromkeys(ADJUSTMENT_RANGES, 0)
    )

    def __post_init__(self) -> None:
        if not isinstance(self.id, str) or not _ID.fullmatch(self.id):
            raise ValueError("The theme ID is invalid.")
        self.name = _name(self.name)
        if not isinstance(self.palette, dict):
            raise ValueError("The theme palette must be an object.")
        if set(self.palette) != set(PALETTE_ROLES):
            raise ValueError("The theme palette must contain every interface color role.")
        palette = {}
        for role in PALETTE_ROLES:
            color = self.palette[role]
            if not isinstance(color, str) or not _COLOR.fullmatch(color):
                raise ValueError(f"The {PALETTE_ROLES[role].lower()} color must use #RRGGBB.")
            palette[role] = color.lower()
        if not isinstance(self.adjustments, dict):
            raise ValueError("Theme adjustments must be an object.")
        if set(self.adjustments) - set(ADJUSTMENT_RANGES):
            raise ValueError("The theme contains an unsupported adjustment.")
        adjustments = {}
        for key, (minimum, maximum) in ADJUSTMENT_RANGES.items():
            value = self.adjustments.get(key, 0)
            if (isinstance(value, bool) or not isinstance(value, (int, float))
                    or not minimum <= value <= maximum
                    or not math.isfinite(value) or int(value) != value):
                raise ValueError(f"{key.title()} must be an integer from {minimum} to {maximum}.")
            adjustments[key] = int(value)
        self.palette = palette
        self.adjustments = adjustments

    @classmethod
    def from_dict(cls, data: object) -> "UITheme":
        if not isinstance(data, dict):
            raise ValueError("A theme must be an object.")
        if set(data) - {"id", "name", "palette", "adjustments"}:
            raise ValueError("The theme contains unsupported fields.")
        if not {"id", "name", "palette"}.issubset(data):
            raise ValueError("A theme needs an ID, name, and palette.")
        return cls(data["id"], data["name"], deepcopy(data["palette"]),
                   deepcopy(data.get("adjustments", {})))

    def to_dict(self) -> dict:
        # Validate mutable editor drafts before they become durable preferences.
        theme = UITheme(self.id, self.name, self.palette, self.adjustments)
        return {"id": theme.id, "name": theme.name, "palette": theme.palette,
                "adjustments": theme.adjustments}

    def copy(self) -> "UITheme":
        return deepcopy(self)

    def effective_palette(self) -> dict[str, str]:
        """Apply HSV adjustments then RGB contrast, without altering base colors."""
        theme = UITheme.from_dict(self.to_dict())
        if not any(theme.adjustments.values()):
            return theme.palette.copy()
        hue = theme.adjustments["hue"] / 360.0
        saturation = 1.0 + theme.adjustments["saturation"] / 100.0
        brightness = theme.adjustments["brightness"] / 100.0
        contrast = 1.0 + theme.adjustments["contrast"] / 100.0

        def clamp(value: float) -> float:
            return max(0.0, min(1.0, value))

        result = {}
        for role, color in theme.palette.items():
            rgb = tuple(int(color[index:index + 2], 16) / 255.0 for index in (1, 3, 5))
            h, s, v = colorsys.rgb_to_hsv(*rgb)
            adjusted = colorsys.hsv_to_rgb((h + hue) % 1.0, clamp(s * saturation),
                                           clamp(v + brightness))
            channels = [round(clamp((value - 0.5) * contrast + 0.5) * 255)
                        for value in adjusted]
            result[role] = "#" + "".join(f"{value:02x}" for value in channels)
        return result


def _builtin_themes() -> list[UITheme]:
    fallback = [UITheme("dark", "Dark", DEFAULT_PALETTE),
                UITheme("light", "Light", LIGHT_PALETTE)]
    path = Path(__file__).resolve().parent.parent / "ui" / "themes.json"
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        records = raw.get("themes") if isinstance(raw, dict) else raw
        if not isinstance(records, list):
            return fallback
    except (OSError, ValueError):
        return fallback
    themes = []
    ids: set[str] = set()
    for record in records:
        try:
            theme = UITheme.from_dict(record)
        except (ValueError, TypeError):
            continue
        if theme.id not in ids:
            themes.append(theme)
            ids.add(theme.id)
    for theme in fallback:
        if theme.id not in ids:
            themes.insert(0 if theme.id == "dark" else 1, theme)
    return themes


@dataclass
class ThemeLibrary:
    custom_themes: list[UITheme] = field(default_factory=list)
    active_id: str = "dark"

    def __post_init__(self) -> None:
        self._builtins = _builtin_themes()
        supplied = self.custom_themes
        self.custom_themes = []
        ids = {theme.id for theme in self._builtins}
        for item in supplied:
            try:
                theme = UITheme.from_dict(item.to_dict())
            except (AttributeError, TypeError, ValueError):
                continue
            if theme.id in ids:
                continue
            theme.name = self.unique_name(theme.name)
            self.custom_themes.append(theme)
            ids.add(theme.id)
        if self.active_id not in ids:
            self.active_id = "dark"

    def all_themes(self) -> list[UITheme]:
        return [theme.copy() for theme in [*self._builtins, *self.custom_themes]]

    def get(self, theme_id: str) -> UITheme:
        return next((theme for theme in self.all_themes() if theme.id == theme_id),
                    next(theme.copy() for theme in self._builtins if theme.id == "dark"))

    def is_builtin(self, theme_id: str) -> bool:
        return any(theme.id == theme_id for theme in self._builtins)

    def unique_name(self, name: str, *, exclude_id: str | None = None) -> str:
        name = _name(name)
        names = {theme.name.casefold() for theme in [*self._builtins, *self.custom_themes]
                 if theme.id != exclude_id}
        if name.casefold() not in names:
            return name
        number = 2
        while f"{name} ({number})".casefold() in names:
            number += 1
        # Leave space for the suffix even when the supplied name has 200 characters.
        suffix = f" ({number})"
        candidate = name[:200 - len(suffix)] + suffix
        while candidate.casefold() in names:
            number += 1
            suffix = f" ({number})"
            candidate = name[:200 - len(suffix)] + suffix
        return candidate

    def save_theme(self, theme: UITheme) -> UITheme:
        theme = UITheme.from_dict(theme.to_dict())
        if self.is_builtin(theme.id):
            raise ValueError("Copy a built-in theme before saving changes.")
        theme.name = self.unique_name(theme.name, exclude_id=theme.id)
        for index, existing in enumerate(self.custom_themes):
            if existing.id == theme.id:
                self.custom_themes[index] = theme
                break
        else:
            self.custom_themes.append(theme)
        return theme.copy()

    def rename_theme(self, theme_id: str, name: str) -> UITheme:
        if self.is_builtin(theme_id):
            raise ValueError("Copy a built-in theme before renaming it.")
        theme = next((theme.copy() for theme in self.custom_themes if theme.id == theme_id), None)
        if theme is None:
            raise KeyError(theme_id)
        theme.name = _name(name)
        return self.save_theme(theme)

    def delete_theme(self, theme_id: str) -> bool:
        if self.is_builtin(theme_id):
            raise ValueError("Built-in themes cannot be deleted.")
        previous = len(self.custom_themes)
        self.custom_themes = [theme for theme in self.custom_themes if theme.id != theme_id]
        removed = len(self.custom_themes) != previous
        if removed and self.active_id == theme_id:
            self.active_id = "dark"
        return removed

    def copy_theme(self, theme: UITheme, name: str) -> UITheme:
        copy = UITheme.from_dict(theme.to_dict())
        copy.id = uuid4().hex
        copy.name = _name(name)
        return self.save_theme(copy)

    def import_theme(self, theme: UITheme) -> UITheme:
        """Imports always create a new theme, even when an exported ID exists."""
        return self.copy_theme(theme, theme.name)


def _preferences_path(path: Path | None) -> Path:
    if path is not None:
        return Path(path)
    from .settings import settings_path
    return settings_path().with_name("themes.json")


def _write_json(path: Path, data: dict) -> None:
    from .brush_storage import atomic_write
    encoded = (json.dumps(data, indent=2, ensure_ascii=False) + "\n").encode("utf-8")
    atomic_write(Path(path), lambda stream: stream.write(encoded))


def load_theme_library(path: Path | None = None) -> ThemeLibrary:
    """Recover valid custom records without rewriting a missing or damaged file."""
    path = _preferences_path(path)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(raw, dict) or raw.get("format") != _LIBRARY_FORMAT
                or type(raw.get("version")) is not int or raw["version"] != _FORMAT_VERSION
                or not isinstance(raw.get("custom_themes"), list)):
            return ThemeLibrary()
    except (OSError, ValueError):
        return ThemeLibrary()
    themes = []
    for record in raw["custom_themes"]:
        try:
            themes.append(UITheme.from_dict(record))
        except (TypeError, ValueError):
            continue
    active_id = raw.get("active_id", "dark")
    return ThemeLibrary(themes, active_id if isinstance(active_id, str) else "dark")


def save_theme_library(library: ThemeLibrary, path: Path | None = None) -> None:
    themes = [theme.to_dict() for theme in library.custom_themes]
    # Reject reserved or duplicated IDs rather than publishing an ambiguous library.
    ids = {theme["id"] for theme in themes}
    if len(ids) != len(themes) or any(library.is_builtin(theme_id) for theme_id in ids):
        raise ValueError("The custom library contains duplicate or built-in theme IDs.")
    _write_json(_preferences_path(path), {
        "format": _LIBRARY_FORMAT, "version": _FORMAT_VERSION,
        "active_id": library.get(library.active_id).id, "custom_themes": themes,
    })


def import_theme(path: Path) -> UITheme:
    """Read the portable theme format; callers add it with library.import_theme()."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as error:
        raise ValueError(f"Could not read the theme file: {error}") from error
    if (not isinstance(raw, dict) or raw.get("format") != _THEME_FORMAT
            or type(raw.get("version")) is not int or raw["version"] != _FORMAT_VERSION):
        raise ValueError("Choose a Webtoon Maker theme file with format version 1.")
    return UITheme.from_dict(raw.get("theme"))


def export_theme(theme: UITheme, path: Path) -> None:
    _write_json(Path(path), {
        "format": _THEME_FORMAT, "version": _FORMAT_VERSION, "theme": theme.to_dict(),
    })
