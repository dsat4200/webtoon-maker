"""Natural widget damage skips only disjoint native tile destinations."""
from dataclasses import replace
import hashlib
import math

import pytest
from PySide6.QtCore import QPoint, QRect, QRectF, QSizeF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QRegion, QTransform
from PySide6.QtWidgets import QApplication, QWidget

from comic_editor.ui.canvas import RasterCanvasWidget
from comic_editor.ui import document_presentation as presentation


def native_tiles(rows=7, image_format=QImage.Format_ARGB32_Premultiplied):
    tiles = []
    for y in range(rows):
        for x in range(5):
            image = QImage(260, 260, image_format)
            image.fill(Qt.transparent)
            painter = QPainter(image)
            for dy in (-1, 0, 1):
                for dx in (-1, 0, 1):
                    color = (QColor.fromRgba64(19001 + (x + dx) * 2301,
                        25303 + (y + dy) * 1703, 36107, 40307)
                        if image_format == QImage.Format_RGBA64_Premultiplied else
                        QColor(95 + (x + dx) * 12, 80 + (y + dy) * 13, 143, 173))
                    painter.fillRect(2 + dx * 256, 2 + dy * 256, 256, 256, color)
            if image_format == QImage.Format_RGBA64_Premultiplied:
                painter.setCompositionMode(QPainter.CompositionMode_Source)
                # Positive coverage below one8-bit alpha step, with native
                # channels deliberately not aligned to multiples of257.
                painter.fillRect(42, 44, 18, 19, QColor.fromRgba64(45111, 21307, 9513, 113))
            painter.end()
            tiles.append(presentation.PresentedTile((None, (x, y)), image,
                QRectF(x * 256, 49920 + y * 256, 256, 256), QRectF(2, 2, 256, 256)))
    return tiles


def native_state(tiles):
    return [(tile.image.format(), tile.image.size(), tile.image.devicePixelRatio(),
             hashlib.sha256(tile.image.constBits()).digest()) for tile in tiles]


class NativeWidget(RasterCanvasWidget):
    """Only the real QWidget paint engine; no document/input services."""
    def __init__(self, parent, tiles, zoom, calls):
        QWidget.__init__(self, parent)
        self.tiles = tiles
        self.base = None
        self.region_enabled = True
        self._document_presentation_paint_region = None
        self._native_raster_mosaic = None
        self.camera = QTransform(zoom, 0., 0., 0., zoom, 0., -89.35, -50003.625 * zoom, 1.)
        self.native_context = None
        self.native_bounds = QRectF(0, 49920, 1280, 1280)
        self.explicit_clip = False
        self.world_clip = None
        self.composition = QPainter.CompositionMode_SourceOver
        self.calls = calls
        self.last_draws = 0
        self.last_region = None
        self.setGeometry(37, 29, 640, 500)

    def event(self, event):
        return QWidget.event(self, event)

    def showEvent(self, event):
        QWidget.showEvent(self, event)

    def resizeEvent(self, event):
        QWidget.resizeEvent(self, event)

    def focusInEvent(self, event):
        QWidget.focusInEvent(self, event)

    def focusOutEvent(self, event):
        QWidget.focusOutEvent(self, event)

    def leaveEvent(self, event):
        QWidget.leaveEvent(self, event)

    def enterEvent(self, event):
        QWidget.enterEvent(self, event)

    def paintEvent(self, event):
        previous = self._document_presentation_paint_region
        self._document_presentation_paint_region = QRegion(event.region()) if self.region_enabled else None
        before = len(self.calls)
        painter = QPainter(self)
        try:
            painter.fillRect(self.rect(), QColor('#242428'))
            painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
            if self.explicit_clip:
                painter.setClipRect(self.rect())
            painter.setCompositionMode(self.composition)
            if self.base is not None:
                presentation.draw_document_tiles(painter, self.base, self.camera,
                    QSizeF(self.size()), owner=self)
            presentation.draw_document_tiles(painter, self.tiles, self.camera,
                QSizeF(self.size()), owner=self, native_context=self.native_context,
                native_bounds=self.native_bounds, clip_world=self.world_clip,
                replace_background=QColor('#242428') if self.base is not None else None)
        finally:
            painter.end()
            self._document_presentation_paint_region = previous
        self.last_draws = len(self.calls) - before
        self.last_region = QRegion(event.region())

    def closeEvent(self, event):
        QWidget.closeEvent(self, event)


@pytest.fixture
def widget_factory(qapp, monkeypatch):
    calls, hosts = [], []
    draw = presentation._draw_owned_raster_tile
    def record(*args, **kwargs):
        calls.append(args[1].key)
        return draw(*args, **kwargs)
    monkeypatch.setattr(presentation, '_draw_owned_raster_tile', record)
    def create(tiles, zoom):
        host = QWidget()
        host.resize(720, 580)
        canvas = NativeWidget(host, tiles, zoom, calls)
        hosts.append(host)
        host.show()
        for _ in range(3): qapp.processEvents()
        return canvas
    yield create
    for host in hosts:
        host.hide()
        host.close()
        host.deleteLater()


def backing(canvas):
    host = canvas.window()
    image = host.screen().grabWindow(host.winId()).toImage()
    ratio = image.devicePixelRatio()
    offset = canvas.mapTo(host, QPoint(0, 0))
    return image.copy(round(offset.x() * ratio), round(offset.y() * ratio),
        round(canvas.width() * ratio), round(canvas.height() * ratio)).convertToFormat(QImage.Format_ARGB32_Premultiplied)


def assert_fresh(canvas):
    actual = backing(canvas)
    canvas.region_enabled = False
    try:
        # QWidget.grab renders at a detached local origin. A child at(37,29)
        # has a fractional device offset atDPR1.25; repaint the same owned
        # surface fully instead so both captures retain its exact sample phase.
        canvas.update()
        QApplication.instance().processEvents()
        fresh = backing(canvas)
    finally:
        canvas.region_enabled = True
    assert actual.size() == fresh.size()
    assert bytes(actual.constBits()) == bytes(fresh.constBits())


def change_native(tiles, color):
    current = list(tiles)
    index = next(i for i, tile in enumerate(tiles) if tile.key[1] == (1, 1))
    tile = tiles[index]
    image = tile.image.copy()
    painter = QPainter(image)
    painter.setCompositionMode(QPainter.CompositionMode_Source)
    value = QColor(color)
    if image.format() == QImage.Format_RGBA64_Premultiplied:
        alpha = 0 if value.alpha() == 0 else 113 if value.alpha() == 176 else 29497
        value = QColor.fromRgba64(55713, 9041, 40113, alpha)
    painter.fillRect(66, 92, 90, 20, value)
    painter.end()
    current[index] = replace(tile, image=image)
    return current, current[index], QRectF(320, 50266, 90, 20)


def row_damage(canvas, world):
    mapped = canvas.camera.mapRect(world).adjusted(-3, -3, 3, 3)
    top, bottom = math.floor(mapped.top()), math.ceil(mapped.bottom())
    return QRegion(QRect(0, top, canvas.width(), bottom - top))


@pytest.mark.parametrize('zoom', [.3, .57, 1.25])
@pytest.mark.parametrize('color', ['#70ff5020', '#00000000', '#b04580ff'])
@pytest.mark.parametrize('replacement', [False, True])
@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied], ids=['argb32', 'rgba64'])
def test_natural_partial_backing_is_exact_and_draws_fewer_native_tiles(qapp, widget_factory, zoom, color, replacement, image_format):
    tiles = native_tiles(image_format=image_format)  #35 exceeds32-tile mosaic cap.
    original = native_state(tiles)
    if image_format == QImage.Format_RGBA64_Premultiplied:
        faint = tiles[0].image.pixelColor(44, 46).rgba64()
        assert faint.alpha() == 113 and 0 < faint.alpha() < 257
        assert any(channel % 257 for channel in (faint.red(), faint.green(), faint.blue()))
    canvas = widget_factory(tiles, zoom)
    assert_fresh(canvas)
    current, changed, world = change_native(tiles, color)
    current_native = native_state(current)
    assert current_native != original
    if replacement:
        canvas.base, canvas.tiles = tiles, [changed]
    else:
        canvas.tiles = current
    canvas.update(row_damage(canvas, world))
    qapp.processEvents()
    assert canvas.last_region != QRegion(canvas.rect())
    assert 0 < canvas.last_draws < len(tiles)
    assert canvas._document_presentation_paint_region is None
    assert_fresh(canvas)
    assert all(tile.image.devicePixelRatio() == 1. for tile in canvas.tiles)
    assert native_state(tiles) == original
    assert native_state(current) == current_native


def test_disconnected_damage_keeps_empty_middle_rows_unculled_only_when_needed(qapp, widget_factory):
    canvas = widget_factory(native_tiles(), .57)
    damage = QRegion(QRect(0, 20, 640, 8)).united(QRegion(QRect(0, 400, 640, 8)))
    canvas.update(damage)
    qapp.processEvents()
    assert canvas.last_region.rectCount() == 2
    assert 0 < canvas.last_draws < 20
    assert_fresh(canvas)


def test_complete_mosaic_admission_uses_whole_batch_before_damage(qapp, widget_factory):
    canvas = widget_factory(native_tiles(rows=5), .57)
    canvas.native_context = ('contact', 1)
    canvas.update()
    qapp.processEvents()
    assert canvas._native_raster_mosaic is not None
    current, _changed, world = change_native(canvas.tiles, '#907020ff')
    canvas.tiles, canvas.native_context = current, ('contact', 2)
    canvas.update(row_damage(canvas, world))
    qapp.processEvents()
    assert canvas._native_raster_mosaic is not None
    assert canvas._native_raster_mosaic[1].sizeInBytes() <= 4 * 1024 * 1024
    assert canvas.last_draws == 0  #Same whole native mosaic, not a tile fallback.
    assert_fresh(canvas)


@pytest.mark.parametrize('guard', ['explicit_clip', 'world_clip', 'composition'])
def test_unknown_clip_and_composition_keep_all_owned_draws(qapp, widget_factory, guard):
    canvas = widget_factory(native_tiles(), .57)
    if guard == 'explicit_clip': canvas.explicit_clip = True
    if guard == 'world_clip': canvas.world_clip = QRectF(0, 49920, 1280, 1792)
    if guard == 'composition':
        canvas.composition = QPainter.CompositionMode_Source
        # A Windows screen capture has opaque alpha. Keep this unsupported
        # composition fixture opaque too, so its retained/full byte oracle
        # compares identical output contracts rather than screen vs image alpha.
        opaque_tiles = []
        for tile in canvas.tiles:
            image = QImage(tile.image.size(), tile.image.format())
            image.fill(QColor('#242428'))
            painter = QPainter(image)
            painter.drawImage(0, 0, tile.image)
            painter.end()
            opaque_tiles.append(replace(tile, image=image))
        canvas.tiles = opaque_tiles
    canvas.update()
    qapp.processEvents()
    canvas.update(QRegion(QRect(0, 180, 640, 8)))
    qapp.processEvents()
    assert canvas.last_draws == 35
    assert_fresh(canvas)


def test_nonowned_image_painter_ignores_widget_damage(widget_factory):
    canvas = widget_factory(native_tiles(), .57)
    canvas._document_presentation_paint_region = QRegion(QRect(0, 180, 640, 8))
    outputs = []
    for owner in (None, canvas):
        image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('#242428'))
        painter = QPainter(image)
        painter.setRenderHints(QPainter.Antialiasing | QPainter.SmoothPixmapTransform)
        try:
            assert presentation._owned_raster_paint_damage(painter, owner, canvas.camera, None) is None
            presentation.draw_document_tiles(painter, canvas.tiles, canvas.camera, QSizeF(canvas.size()), owner=owner)
        finally:
            painter.end()
        outputs.append(bytes(image.constBits()))
    assert outputs[0] == outputs[1]


@pytest.mark.parametrize('camera', [QTransform().rotate(17), QTransform().shear(.1, .2), QTransform.fromScale(-1, 1)])
def test_unsupported_camera_does_not_cull_owned_widget(qapp, widget_factory, camera):
    canvas = widget_factory(native_tiles(), .57)
    canvas.camera = camera
    canvas.update()
    qapp.processEvents()
    canvas.update(QRegion(QRect(0, 180, 640, 8)))
    qapp.processEvents()
    assert canvas.last_draws == 0  #Existing original QPainter image fallback.
    assert_fresh(canvas)
