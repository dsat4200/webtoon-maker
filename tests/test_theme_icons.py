from __future__ import annotations

from collections import Counter
from pathlib import Path

from PySide6.QtCore import QSize
from PySide6.QtGui import QColor, QIcon, QPalette

from comic_editor.ui.icons import TOOLBAR_ICON_NAMES, iconoir, iconoir_tinted


def _solid_color(image):
    colors = Counter(
        image.pixelColor(x, y).name()
        for x in range(image.width())
        for y in range(image.height())
        if image.pixelColor(x, y).alpha() == 255
    )
    assert colors
    # SVG rasterization can round a premultiplied edge channel by one unit.
    dominant = colors.most_common(1)[0][0]
    reference = QColor(dominant)
    for value in colors:
        color = QColor(value)
        assert max(abs(a - b) for a, b in zip(color.getRgb(), reference.getRgb())) <= 1
    return dominant


def test_existing_icon_tracks_palette_and_checked_accent(qapp):
    palette_before = QPalette(qapp.palette())
    accent_before = qapp.property("ui_theme_accent")
    icon = iconoir("brush")
    try:
        palette = QPalette(palette_before)
        palette.setColor(QPalette.ButtonText, QColor("#182838"))
        palette.setColor(QPalette.Disabled, QPalette.ButtonText, QColor("#718191"))
        qapp.setPalette(palette)
        qapp.setProperty("ui_theme_accent", QColor("#2858a8"))
        assert _solid_color(icon.pixmap(QSize(40, 40)).toImage()) == "#182838"
        assert _solid_color(icon.pixmap(
            QSize(40, 40), QIcon.Normal, QIcon.On
        ).toImage()) == "#2858a8"
        assert _solid_color(icon.pixmap(
            QSize(40, 40), QIcon.Disabled, QIcon.On
        ).toImage()) == "#718191"

        palette.setColor(QPalette.ButtonText, QColor("#ddeeff"))
        qapp.setPalette(palette)
        assert _solid_color(icon.pixmap(QSize(40, 40)).toImage()) == "#ddeeff"
    finally:
        qapp.setPalette(palette_before)
        qapp.setProperty("ui_theme_accent", accent_before)


def test_explicit_icon_tint_keeps_semantic_color(qapp):
    palette_before = QPalette(qapp.palette())
    icon = iconoir_tinted("mask-square", "#f2a23a", 40)
    try:
        palette = QPalette(palette_before)
        palette.setColor(QPalette.ButtonText, QColor("#182838"))
        qapp.setPalette(palette)
        assert _solid_color(icon.pixmap(QSize(40, 40)).toImage()) == "#f2a23a"
    finally:
        qapp.setPalette(palette_before)


def test_tool_commands_have_unique_rendered_glyphs(qapp):
    assert len(TOOLBAR_ICON_NAMES) == len(set(TOOLBAR_ICON_NAMES.values()))
    images = {}
    for label, name in TOOLBAR_ICON_NAMES.items():
        icon = iconoir(name)
        image = icon.pixmap(QSize(24, 24)).toImage()
        assert not image.isNull(), label
        assert any(
            image.pixelColor(x, y).alpha()
            for x in range(image.width()) for y in range(image.height())
        ), label
        data = bytes(image.constBits())
        assert data not in images, (label, images.get(data))
        images[data] = label


def test_theme_icons_render_at_display_density(qapp):
    pixmap = iconoir("brush").pixmap(QSize(20, 20), 2.0)
    assert pixmap.size() == QSize(40, 40)
    assert pixmap.devicePixelRatio() == 2.0


def test_added_iconoir_assets_keep_license():
    from comic_editor.ui import icons
    root = Path(icons.__file__).with_name("icons") / "iconoir"
    assert "MIT License" in (root / "LICENSE").read_text(encoding="utf-8")
    assert "original Webtoon Maker" in (root / "README.md").read_text(encoding="utf-8")
