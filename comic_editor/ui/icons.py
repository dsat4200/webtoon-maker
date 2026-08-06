"""Local, theme-aware Iconoir icons used by the editor tool strip."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from PySide6.QtCore import QByteArray, QRectF, Qt
from PySide6.QtGui import QIcon, QPainter, QPixmap
from PySide6.QtSvg import QSvgRenderer


_ICON_ROOT = Path(__file__).with_name("icons") / "iconoir"


def _render_icon(name: str, color: str, size: int) -> QPixmap:
    source = (_ICON_ROOT / f"{name}.svg").read_text(encoding="utf-8")
    renderer = QSvgRenderer(QByteArray(source.replace(
        "currentColor", color
    ).encode("utf-8")))
    pixmap = QPixmap(size, size)
    pixmap.fill(Qt.GlobalColor.transparent)
    painter = QPainter(pixmap)
    renderer.render(painter, QRectF(0, 0, size, size))
    painter.end()
    return pixmap


@lru_cache(maxsize=128)
def iconoir(name: str, size: int = 20) -> QIcon:
    """Return a checked/disabled-aware icon from the vendored Iconoir set."""
    icon = QIcon()
    normal = _render_icon(name, "#E6E6E8", size)
    checked = _render_icon(name, "#80C8FF", size)
    disabled = _render_icon(name, "#5A5A62", size)
    icon.addPixmap(normal, QIcon.Mode.Normal, QIcon.State.Off)
    icon.addPixmap(checked, QIcon.Mode.Normal, QIcon.State.On)
    icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.Off)
    icon.addPixmap(disabled, QIcon.Mode.Disabled, QIcon.State.On)
    return icon
