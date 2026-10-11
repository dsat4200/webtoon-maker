"""Live raster replacement owns each screen pixel once at fractional zoom."""
from dataclasses import replace

import pytest
from PySide6.QtCore import QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.ui.document_presentation import PresentedTile, draw_document_tiles


def pixels(image):
    return bytes(image.constBits())


def native_tiles():
    # Many tiles exceed the native mosaic admission cap in real widget views.
    # This exercises the bounded individual-tile presenter with no mosaic.
    tiles = []
    for y in range(7):
        for x in range(5):
            native = QImage(260, 260, QImage.Format_ARGB32_Premultiplied)
            native.fill(QColor(95 + x * 19, 80 + y * 17, 143))
            # Adjacent native gutters must contain the actual neighboring color.
            painter = QPainter(native)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    painter.fillRect(2 + dx * 256, 2 + dy * 256, 256, 256,
                        QColor(95 + (x + dx) * 19, 80 + (y + dy) * 17, 143))
            painter.end()
            tiles.append(PresentedTile((None, (x, y)), native,
                QRectF(x * 256, 49920 + y * 256, 256, 256), QRectF(2, 2, 256, 256)))
    return tiles


def target(ratio):
    image = QImage(round(780 * ratio), round(920 * ratio), QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(QColor('#242428'))
    return image


def present(image, tiles, camera):
    painter = QPainter(image)
    try:
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        return draw_document_tiles(painter, tiles, camera, QSizeF(780, 920), smooth=True)
    finally:
        painter.end()


@pytest.mark.parametrize('ratio', [1., 1.25, 2.])
@pytest.mark.parametrize('zoom', [.3, .57, .91, 1.25])
def test_replacing_identical_native_tiles_never_blends_old_frame_at_seams(qapp, ratio, zoom):
    tiles = native_tiles()
    original = [(tile.image.cacheKey(), pixels(tile.image), tile.image.size()) for tile in tiles]
    camera = QTransform(zoom, 0., 0., 0., zoom, 0., -89.35, -50003.625 * zoom, 1.)
    image = target(ratio)
    present(image, tiles, camera)
    expected = pixels(image)
    for _ in range(3):
        present(image, [replace(tile, key=('live', tile.key)) for tile in tiles], camera)
        assert pixels(image) == expected
    assert [(tile.image.cacheKey(), pixels(tile.image), tile.image.size()) for tile in tiles] == original
    assert all(tile.image.devicePixelRatio() == 1. for tile in tiles)


@pytest.mark.parametrize('ratio', [1., 1.25, 2.])
@pytest.mark.parametrize('zoom', [.3, .57, 1.25])
def test_partial_live_replacement_matches_fresh_current_native_batch(qapp, ratio, zoom):
    tiles = native_tiles()
    camera = QTransform(zoom, 0., 0., 0., zoom, 0., -89.35, -50003.625 * zoom, 1.)
    retained = target(ratio)
    present(retained, tiles, camera)
    changed = []
    for tile in tiles:
        x, y = tile.key[1]
        if x in (1, 2, 3) and y in (1, 2, 3, 4):
            image = tile.image.copy()
            painter = QPainter(image)
            # A native current-image patch, including erased foreground pixels.
            painter.fillRect(24, 19, 217, 219, QColor('#242428'))
            painter.fillRect(24, 19, 132, 123, QColor('#ff7020'))
            painter.end()
            changed.append(replace(tile, image=image))
    replacements = {tile.key: tile for tile in changed}
    current = [replacements.get(tile.key, tile) for tile in tiles]
    present(retained, changed, camera)
    exact = target(ratio)
    present(exact, current, camera)
    assert pixels(retained) == pixels(exact)


def test_axis_aligned_ownership_restores_painter_clip_transform_hints_and_opacity(qapp):
    image = target(1.25)
    painter = QPainter(image)
    try:
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        painter.setTransform(QTransform.fromTranslate(3.25, 4.75))
        painter.setClipRect(QRectF(12.25, 18.75, 119.5, 163.5))
        painter.setOpacity(.43)
        before = (painter.transform(), painter.clipPath(), painter.renderHints(), painter.opacity(),
                  painter.compositionMode())
        draw_document_tiles(painter, native_tiles(),
            QTransform(.57, 0., 0., 0., .57, 0., -89.35, -50003.625 * .57, 1.), QSizeF(780, 920))
        after = (painter.transform(), painter.clipPath(), painter.renderHints(), painter.opacity(),
                 painter.compositionMode())
        assert after == before
    finally:
        painter.end()


@pytest.mark.parametrize('transform', [QTransform().rotate(17.), QTransform().shear(.1, .2),
                                      QTransform.fromScale(-1., 1.)])
def test_non_axis_aligned_tiles_keep_original_qt_presentation(qapp, transform):
    tiles = native_tiles()[:2]
    camera = QTransform.fromTranslate(109.3, 117.7) * transform
    # Keep this small ordinary comparison near the origin.
    tiles = [replace(tile, world_rect=QRectF(tile.world_rect.x(), 0., 256., 256.)) for tile in tiles]
    actual = target(1.25)
    present(actual, tiles, camera)
    expected = target(1.25)
    painter = QPainter(expected)
    painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
    painter.setTransform(camera)
    for tile in tiles:
        painter.drawImage(tile.world_rect, tile.image, tile.source_rect)
    painter.end()
    assert pixels(actual) == pixels(expected)
