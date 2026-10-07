"""The native painted-mask kernel shared by ordinary and detached captures."""
from PySide6.QtCore import Qt
from PySide6.QtGui import QImage, QPainter


def add_paint(field, tiles, mask_id, width, height, mapping, visible, offset,
              signed, add_alpha, signed_paint, cancelled=None):
    paint = QImage(width, height, QImage.Format.Format_ARGB32_Premultiplied)
    if paint.isNull():
        raise MemoryError("Could not allocate tone mask image")
    paint.fill(Qt.GlobalColor.transparent)
    painter = QPainter(paint)
    try:
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
        painter.setTransform(mapping)
        painter.translate(*offset)
        for (tile_x, tile_y), tile in tiles.iter_tiles(
                mask_id, visible.translated(-offset[0], -offset[1])):
            if cancelled is not None and cancelled():
                return False
            painter.drawImage(tile_x * tiles.tile_size, tile_y * tiles.tile_size, tile)
    finally:
        painter.end()
    if cancelled is not None and cancelled():
        return False
    if signed:
        field += signed_paint(paint)
    else:
        add_alpha(field, paint)
    return True
