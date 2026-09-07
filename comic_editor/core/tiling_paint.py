"""Transport full brush footprints, accumulating each dab only once."""
import numpy as np
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QColor


def folded_footprint(path, geometry, mapping):
    inverse = mapping.inverted()[0]
    world = mapping.map(path)
    center_cell = geometry.cell_transform(geometry.cell(world.boundingRect().center())).inverted()[0].map(geometry.path())
    if world.contains(center_cell):
        return inverse.map(geometry.path())
    result = QPainterPath()
    for _, transport, cell_path in geometry.cells_intersecting(world.boundingRect()):
        if world.contains(cell_path):
            return inverse.map(geometry.path())
        if world.intersects(cell_path):
            result = result.united(inverse.map(transport.map(world)))
    return result


def paint_samples(store, object_id, samples, color, context, *, erase, square, antialias, before):
    geometry, mapping = context
    clip = mapping.inverted()[0].map(geometry.path())
    size = store.tile_size
    dirty = QRectF()
    for point, diameter, opacity in samples:
        path = QPainterPath()
        rect = QRectF(point.x()-diameter/2, point.y()-diameter/2, diameter, diameter)
        if square:
            path.addRect(rect)
        else:
            path.addEllipse(rect)
        folded = folded_footprint(path, geometry, mapping)
        bounds = folded.boundingRect().intersected(clip.boundingRect())
        for key in store.keys_for_rect(bounds):
            previous = store._tiles.get(object_id, {}).get(key)
            if erase and previous is None:
                continue
            mask = QImage(size, size, QImage.Format_Alpha8)
            mask.fill(0)
            painter = QPainter(mask)
            painter.translate(-key[0]*size, -key[1]*size)
            painter.setClipPath(clip)
            painter.setRenderHint(QPainter.Antialiasing, antialias)
            painter.fillPath(folded, Qt.white)
            painter.end()
            coverage = np.frombuffer(mask.constBits(), np.uint8).reshape(size, mask.bytesPerLine())[:, :size]
            if not np.any(coverage):
                continue
            if before is not None and key not in before:
                before[key] = QImage(previous) if previous is not None else None
            image = QImage(previous) if previous is not None else store._empty(size)
            rgba = np.empty((size, size, 4), dtype=np.uint8)
            rgba[..., :3] = (color.red(), color.green(), color.blue())
            rgba[..., 3] = np.rint(coverage*(1. if erase else min(1., max(0., opacity)))).astype(np.uint8)
            source = QImage(rgba.data, size, size, size*4, QImage.Format_RGBA8888).copy()
            painter = QPainter(image)
            if erase:
                painter.setCompositionMode(QPainter.CompositionMode_DestinationOut)
            painter.drawImage(0, 0, source)
            painter.end()
            store.set_tile(object_id, key, image)
        dirty = bounds if dirty.isEmpty() else dirty.united(bounds)
    return dirty
