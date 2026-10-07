from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import RasterCanvasWidget
from comic_editor.ui.native_artwork import capture_scene, paint_scene


def _canvas():
    chapter = ChapterDocument(height=8000)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 8000))
    page.fill_color = "#FFF8F0E0"
    layer = chapter.add_layer(page.layer_id, "Panel", BoundGeometry.rectangle(20, 30, 900, 900))
    layer.fill_color = "#880066CC"
    layer.border_width = 9
    obj = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 1000, 1000)))
    tiles = TileStore()
    for x in (80, 430, 820):
        tiles.paint_dab(obj.object_id, QPointF(x, 480), 240, QColor("#88CC3366"))
    canvas = RasterCanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False))
    canvas.resize(1000, 800)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y = 540., 500.
    return canvas


def _image(canvas, ratio):
    image = QImage(round(canvas.width() * ratio), round(canvas.height() * ratio), QImage.Format_ARGB32_Premultiplied)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.transparent)
    return image


@pytest.mark.parametrize("scale,rotation,ratio", [(.75, 0., 1.5), (.25, 0., 1.), (2., 27., 2.), (.75, 31., 1.5)])
def test_dirty_preview_matches_complete_capture_with_fewer_blocks(qapp, monkeypatch, scale, rotation, ratio):
    canvas = _canvas()
    canvas.scale, canvas.rotation = scale, rotation
    monkeypatch.setattr(canvas, "devicePixelRatioF", lambda: ratio)
    captured = []
    original = canvas._render_service.render_region
    def track(document, request):
        captured.append(request)
        return original(document, request)
    monkeypatch.setattr(canvas._render_service, "render_region", track)
    full = _image(canvas, ratio)
    painter = QPainter(full)
    try:
        paint_scene(canvas, painter, canvas.visible_document_rect())
    finally:
        painter.end()
    full_count = len(captured)
    captured.clear()
    dirty = QRectF(410, 370, 35, 29)
    actual = _image(canvas, ratio)
    painter = QPainter(actual)
    try:
        painter.setClipRect(dirty)
        paint_scene(canvas, painter, canvas.visible_document_rect())
    finally:
        painter.end()
    expected = _image(canvas, ratio)
    painter = QPainter(expected)
    try:
        painter.setClipRect(dirty)
        painter.drawImage(0, 0, full)
    finally:
        painter.end()
    assert bytes(actual.constBits()) == bytes(expected.constBits())
    assert len(captured) < full_count if full_count > 1 else len(captured) == 1
    # Full source capture rectangles and pixel sizes preserve the same native
    # grid; only the requested output hint is narrowed by the dirty clip.
    assert all(request.pixel_size == (1028, 1028) for request in captured)
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def test_transient_capture_caps_pixels_and_document_density(qapp, monkeypatch):
    canvas = _canvas()
    monkeypatch.setattr(canvas, "devicePixelRatioF", lambda: 3.)
    for scale in (.25, 1., 4.):
        canvas.scale = scale
        image = capture_scene(canvas, max_pixels=250_000)
        assert image.width() * image.height() <= 250_000
        assert image.devicePixelRatio() * scale <= 1.000001
        assert image.width() / image.devicePixelRatio() >= canvas.width()
        assert image.height() / image.devicePixelRatio() >= canvas.height()
        assert image.format() == canvas.chapter.pixel_contract.image_format
    assert canvas._document_projection.snapshot()["tiles"] == 0
    canvas._effect_jobs.cancel()
    canvas.deleteLater()
