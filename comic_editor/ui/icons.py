"""Local, theme-aware Iconoir icons used by the editor tool strip."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QIcon, QIconEngine, QPainter, QPalette, QPixmap
from PySide6.QtSvg import QSvgRenderer
from PySide6.QtWidgets import QApplication


_ICON_ROOT = Path(__file__).with_name("icons") / "iconoir"


# Distinct tools keep distinct silhouettes even when the narrow tool column
# hides their labels.  Category expand/collapse arrows remain shared controls.
TOOLBAR_ICON_NAMES = {
    "Object Select": "cursor-pointer",
    "Pencil": "design-pencil",
    "Brush": "brush",
    "Lasso Brush": "lasso-brush",
    "Eraser": "erase",
    "Fill": "fill-color",
    "Gradient": "contrast-circle",
    "Select": "selection",
    "Magic Wand": "magic-wand",
    "Text Edit": "text",
    "Transform": "frame-tool",
    "Cage Transform": "view-grid",
    "Shape Edit": "ease-curve-control-points",
    "Insert Page Gap": "split-square-dashed",
    "Rectangle": "plus-square-dashed",
    "Circle": "circle",
    "Free Shape": "path-arrow",
    "Draw Shape": "draw-polygon",
    "Rectangle Select": "select-window",
    "Lasso Select": "lasso-select",
    "Stroke Select": "frame-select",
    "Add Page": "page-plus",
    "Add Text": "text-square",
    "Free Text Container": "text-box",
    "Add Raster": "media-image-plus",
    "Add Vector Drawing": "curve-array",
}


@lru_cache(maxsize=128)
def _source(name: str) -> str:
    return (_ICON_ROOT / f"{name}.svg").read_text(encoding="utf-8")


def _renderer(name: str, color: str) -> QSvgRenderer:
    return QSvgRenderer(QByteArray(_source(name).replace(
        "currentColor", color
    ).encode("utf-8")))


@lru_cache(maxsize=512)
def _render_icon(name: str, color: str, width: int, height: int) -> QPixmap:
    renderer = _renderer(name, color)
    pixmap = QPixmap(width, height)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, width, height))
    painter.end()
    return pixmap


def _theme_color(mode: QIcon.Mode, state: QIcon.State) -> str:
    application = QApplication.instance()
    palette = QApplication.palette()
    if mode == QIcon.Mode.Disabled:
        color = palette.color(
            QPalette.ColorGroup.Disabled, QPalette.ColorRole.ButtonText
        )
    elif mode == QIcon.Mode.Selected:
        color = palette.color(QPalette.ColorRole.HighlightedText)
    elif state == QIcon.State.On:
        accent = application.property("ui_theme_accent") if application else None
        color = accent if isinstance(accent, QColor) and accent.isValid() else (
            palette.color(QPalette.ColorRole.Highlight)
        )
    else:
        color = palette.color(QPalette.ColorRole.ButtonText)
    return color.name(QColor.NameFormat.HexRgb)


class _ThemeIconEngine(QIconEngine):
    """Resolve colors at draw time so held icons follow live theme edits."""

    def __init__(self, name: str, size: int):
        super().__init__()
        self.name = name
        self.size = size

    def clone(self) -> QIconEngine:
        return _ThemeIconEngine(self.name, self.size)

    def key(self) -> str:
        return "WebtoonThemeSvg"

    def iconName(self) -> str:  # noqa: N802
        return self.name

    def isNull(self) -> bool:  # noqa: N802
        return False

    def availableSizes(self, mode=QIcon.Mode.Normal, state=QIcon.State.Off):  # noqa: N802
        return [QSize(self.size, self.size)]

    def paint(self, painter, rect, mode, state) -> None:
        _renderer(self.name, _theme_color(mode, state)).render(painter, QRectF(rect))

    def pixmap(self, size, mode, state) -> QPixmap:
        return QPixmap(_render_icon(
            self.name, _theme_color(mode, state), size.width(), size.height()
        ))

    def scaledPixmap(self, size, mode, state, scale) -> QPixmap:  # noqa: N802
        pixmap = QPixmap(_render_icon(
            self.name, _theme_color(mode, state),
            round(size.width() * scale), round(size.height() * scale),
        ))
        pixmap.setDevicePixelRatio(scale)
        return pixmap


@lru_cache(maxsize=128)
def iconoir(name: str, size: int = 20) -> QIcon:
    """Return a scalable icon that follows the current application palette."""
    _source(name)  # Fail at creation if an asset is missing, as before.
    return QIcon(_ThemeIconEngine(name, size))


@lru_cache(maxsize=64)
def iconoir_tinted(name: str, color: str, size: int = 20) -> QIcon:
    """Return one explicitly colored Iconoir glyph."""
    return QIcon(_render_icon(name, str(color), size, size))
