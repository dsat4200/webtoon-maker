"""Application chrome colors, independent of document colors and render caches."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QApplication

from comic_editor.core.ui_themes import DEFAULT_PALETTE, UITheme


def theme_color(role: str, fallback: str | None = None) -> QColor:
    app = QApplication.instance()
    colors = app.property("ui_theme_palette") if app is not None else None
    return QColor((colors or {}).get(role, fallback or DEFAULT_PALETTE[role]))


def apply_ui_theme(theme: UITheme) -> None:
    """Recolor widget chrome without touching source pixels or scene resources."""
    app = QApplication.instance()
    if app is None:
        return
    colors = theme.effective_palette()
    app.setProperty("ui_theme_palette", colors)
    app.setProperty("ui_theme_accent", QColor(colors["accent"]))
    if app.style().objectName().lower() != "fusion":
        app.setStyle("Fusion")
    palette = QPalette()
    roles = {
        QPalette.Window: "window", QPalette.WindowText: "text",
        QPalette.Base: "input", QPalette.AlternateBase: "surface",
        QPalette.Text: "text", QPalette.Button: "button",
        QPalette.ButtonText: "text", QPalette.BrightText: "accent_text",
        QPalette.Highlight: "selection", QPalette.HighlightedText: "selection_text",
        QPalette.ToolTipBase: "surface", QPalette.ToolTipText: "text",
        QPalette.Link: "accent", QPalette.LinkVisited: "accent",
        QPalette.Mid: "border", QPalette.Dark: "panel",
        QPalette.Light: "hover", QPalette.Shadow: "border",
        QPalette.PlaceholderText: "muted_text",
    }
    for qt_role, role in roles.items():
        palette.setColor(qt_role, QColor(colors[role]))
    for qt_role in (QPalette.Text, QPalette.WindowText, QPalette.ButtonText):
        palette.setColor(QPalette.Disabled, qt_role, QColor(colors["muted_text"]))
    app.setPalette(palette)
    stylesheet = Path(__file__).with_name("style.qss").read_text(encoding="utf-8")
    for role, value in colors.items():
        stylesheet = stylesheet.replace(f"@{role}@", value)
    if app.styleSheet() != stylesheet:
        app.setStyleSheet(stylesheet)
    for widget in app.allWidgets():
        widget.update()
