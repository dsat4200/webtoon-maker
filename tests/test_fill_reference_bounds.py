"""Sparse fill never renders tiles outside its finite pixel frame."""
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.tiles import TileStore


@pytest.mark.parametrize("connected", [True, False])
@pytest.mark.parametrize("origin", [0, -64])
def test_reference_requests_stop_at_half_open_tile_aligned_frame(connected, origin):
    tiles, requested = TileStore(tile_size=32), []
    def reference(key):
        requested.append(key)
        image = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor("cyan"))
        return image
    tiles.advanced_fill("mask", QPointF(origin + 8, origin + 8),
        QRectF(origin, origin, 64, 64), QColor("white"),
        {"tolerance": 0, "antialiasing": False, "connected_pixels_only": connected},
        reference_tile=reference)
    expected = {(x, y) for y in range(origin // 32, origin // 32 + 2)
                for x in range(origin // 32, origin // 32 + 2)}
    assert set(requested) == expected and len(requested) == 4
    assert set(tiles.object_tiles("mask")) == expected
    assert all(image.pixelColor(31, 31) == QColor("white")
               for image in tiles.object_tiles("mask").values())
