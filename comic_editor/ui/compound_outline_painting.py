"""Clip complex outline coverage at the destination's full pixel resolution."""
from dataclasses import dataclass
import math

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QWidget


# Two temporary premultiplied images, at most 32 MiB together. Large exports
# keep the geometric path instead of allocating a document-sized scratch image.
MAX_RASTER_PIXELS = 4 * 1024 * 1024


@dataclass
class OutlineRaster:
    bounds: QRect
    mapping: QTransform
    image: QImage
    mask: QImage

    def paint(self, painter, coverage, fill, color):
        if self.bounds.isEmpty() or coverage.isEmpty():
            return
        antialias = painter.testRenderHint(QPainter.Antialiasing)
        for image, path, brush in ((self.image, coverage, color),
                                   (self.mask, fill, Qt.white)):
            image.fill(Qt.transparent)
            source = QPainter(image)
            source.setRenderHint(QPainter.Antialiasing, antialias)
            if self.mapping.isAffine():
                source.setTransform(self.mapping)
                source.fillPath(path, brush)
            else:
                # Qt's projective painter conversion can discard interior
                # contours. Mapping the path first retains its fill rule.
                source.fillPath(self.mapping.map(path), brush)
            source.end()
        source = QPainter(self.image)
        source.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        # A whole image also clears coverage outside the fill; drawing just
        # the fill path with DestinationIn would leave that coverage intact.
        source.drawImage(0, 0, self.mask)
        source.end()
        painter.save()
        painter.resetTransform()
        ratio = painter.device().devicePixelRatioF()
        painter.scale(1 / ratio, 1 / ratio)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        # Existing clip, composition and parent opacity apply once, here.
        painter.drawImage(self.bounds.topLeft(), self.image)
        painter.restore()


def prepare_outline_raster(painter, fill):
    """Preflight output bounds and allocate before requesting an unclipped mesh.

    None means use geometric clipping. Vector/high-depth output stays vector or
    high-depth, and composition modes that erase transparent pixels keep their
    original path semantics.
    """
    device = painter.device()
    if not isinstance(device, (QImage, QPixmap, QWidget)):
        return None
    if painter.compositionMode() != QPainter.CompositionMode_SourceOver:
        return None
    if isinstance(device, QImage) and device.format() not in {
        QImage.Format_ARGB32_Premultiplied, QImage.Format_ARGB32,
        QImage.Format_RGBA8888_Premultiplied, QImage.Format_RGBA8888,
        QImage.Format_RGB32, QImage.Format_RGBX8888,
    }:
        return None
    mapping = painter.deviceTransform()
    if not mapping.isInvertible():
        return None
    area = mapping.map(fill).boundingRect().adjusted(-1, -1, 1, 1)
    if not all(math.isfinite(value) for value in
               (area.left(), area.top(), area.right(), area.bottom())):
        return None
    ratio = device.devicePixelRatioF() if isinstance(device, QWidget) else 1
    area = area.intersected(QRectF(0, 0, device.width()*ratio, device.height()*ratio))
    if painter.hasClipping():
        clip = mapping.mapRect(painter.clipBoundingRect()).adjusted(-1, -1, 1, 1)
        area = area.intersected(clip)
    if area.isEmpty():
        return OutlineRaster(QRect(), mapping, QImage(), QImage())
    bounds = area.toAlignedRect()
    if bounds.width()*bounds.height() > MAX_RASTER_PIXELS:
        return None
    image = QImage(bounds.size(), QImage.Format_ARGB32_Premultiplied)
    mask = QImage(bounds.size(), QImage.Format_ARGB32_Premultiplied)
    if image.isNull() or mask.isNull():
        return None
    mapping *= QTransform.fromTranslate(-bounds.left(), -bounds.top())
    return OutlineRaster(bounds, mapping, image, mask)
