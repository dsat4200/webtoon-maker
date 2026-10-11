"""A local contact edit retires only native display tiles whose pixels changed."""
from types import SimpleNamespace

from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.ui.raster_feedback import RasterFeedbackPatchCache, _native_presentation_tiles


def test_native_block_edit_preserves_unchanged_texture_pixels_and_guarded_owner():
    canvas = SimpleNamespace(_document_projection=SimpleNamespace(tile_size=256))
    prepared = SimpleNamespace(gutter=2, world_size=1024)
    tile = SimpleNamespace(key=(0, 0), bounds=(-2, -2, 1028, 1028))
    visible = QRectF(0, 0, 1024, 1024)
    image = QImage(1028, 1028, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('blue'))
    cache = RasterFeedbackPatchCache()
    first = _native_presentation_tiles(canvas, prepared, tile, image, visible, cache)
    edited = image.copy()
    edited.setPixelColor(500, 500, QColor('red'))
    second = _native_presentation_tiles(canvas, prepared, tile, edited, visible, cache)
    changed = [after for before, after in zip(first, second)
        if before.image.cacheKey() != after.image.cacheKey()]
    assert len(first) == len(second) == 16
    assert len(changed) == 1
    assert changed[0].world_rect == QRectF(256, 256, 256, 256)
    assert changed[0].image.pixelColor(244, 244) == QColor('red')
    assert all(before.image == after.image for before, after in zip(first, second)
        if before.image.cacheKey() == after.image.cacheKey())
    # A gutter edit affects all four native tiles which sample that pixel.
    edited = edited.copy()
    edited.setPixelColor(256, 256, QColor('green'))
    third = _native_presentation_tiles(canvas, prepared, tile, edited, visible, cache)
    assert sum(before.image.cacheKey() != after.image.cacheKey()
        for before, after in zip(second, third)) == 4
    assert cache.byte_count <= cache.budget
    newer_owner = SimpleNamespace(gutter=2, world_size=1024)
    fourth = _native_presentation_tiles(canvas, newer_owner, tile, edited, visible, cache)
    assert all(before.image.cacheKey() != after.image.cacheKey()
        for before, after in zip(third, fourth))
