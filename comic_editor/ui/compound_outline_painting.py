"""Clip complex outline coverage at the destination's full pixel resolution."""
from dataclasses import dataclass
import math

from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QBrush, QImage, QPainter, QPixmap, QTransform
from PySide6.QtWidgets import QWidget


# Two temporary premultiplied images, at most 32 MiB together. Large exports
# keep the geometric path instead of allocating a document-sized scratch image.
MAX_RASTER_PIXELS = 4 * 1024 * 1024


def paint_closed_shape_outline(painter, bound, baseline, fill, color, *,
                               cache=None, tolerance=.125):
    """Avoid whole-path Boolean normalization for complex standalone outlines."""
    from comic_editor.ui.shape_outline import customized, outline_result

    raster = (prepare_outline_raster(painter, fill, cache=cache)
              if customized(bound) and painter.testRenderHint(QPainter.Antialiasing)
              else None)
    if raster is not None and raster.bounds.isEmpty():
        return
    result = outline_result(bound, baseline, fill, cache=cache,
                            tolerance=tolerance, clip=raster is None)
    coverage = result.coverage
    if raster is not None and coverage.elementCount() <= 128:
        raster = None
        coverage = outline_result(bound, baseline, fill, cache=cache,
                                  tolerance=tolerance).coverage
    if raster is None:
        painter.fillPath(coverage, color)
    else:
        if not raster.paint(painter, coverage, fill, color, cache_key=result.signature):
            # Allocation failure retains the pre-existing geometric fallback.
            coverage = outline_result(bound, baseline, fill, cache=cache,
                                      tolerance=tolerance).coverage
            painter.fillPath(coverage, color)


@dataclass
class OutlineRaster:
    bounds: QRect
    mapping: QTransform
    image: QImage
    mask: QImage
    cache: object = None

    def paint(self, painter, coverage, fill, color, *, cache_key=None):
        if self.bounds.isEmpty() or coverage.isEmpty():
            return True
        antialias = painter.testRenderHint(QPainter.Antialiasing)
        brush = QBrush(color)
        if self.cache is not None and brush.style() == Qt.SolidPattern:
            from comic_editor.ui.shape_outline import path_key

            mapping = self.mapping
            rgba = brush.color().rgba64()
            key = ("outline_raster",
                   cache_key if cache_key is not None else (path_key(coverage), path_key(fill)),
                   (mapping.m11(), mapping.m12(), mapping.m13(), mapping.m21(),
                    mapping.m22(), mapping.m23(), mapping.m31(), mapping.m32(), mapping.m33()),
                   self.bounds.width(), self.bounds.height(), bool(antialias),
                   (rgba.red(), rgba.green(), rgba.blue(), rgba.alpha()))
            def build():
                image = self._render(coverage, fill, color, antialias)
                if image is None or image.isNull():
                    # A temporary allocation failure must not become a cached
                    # blank result for the unchanged outline's next redraw.
                    raise MemoryError("Could not allocate outline scratch pixels")
                return image

            try:
                image = self.cache.get(key, build)
            except MemoryError:
                return False
            if image is None or image.isNull():
                return False
            # Never give later scratch painting mutable ownership of a cached
            # image. Qt's implicit sharing keeps this copy allocation-free.
            self.image = QImage(image)
        else:
            image = self._render(coverage, fill, color, antialias)
            if image is None or image.isNull():
                return False
        painter.save()
        painter.resetTransform()
        ratio = painter.device().devicePixelRatioF()
        painter.scale(1 / ratio, 1 / ratio)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        # Existing clip, composition and parent opacity apply once, here.
        painter.drawImage(self.bounds.topLeft(), self.image)
        painter.restore()
        return True

    def _render(self, coverage, fill, color, antialias):
        self.image = QImage(self.bounds.size(), QImage.Format_ARGB32_Premultiplied)
        self.mask = QImage(self.bounds.size(), QImage.Format_ARGB32_Premultiplied)
        if self.image.isNull() or self.mask.isNull():
            return None
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
        return self.image


def prepare_outline_raster(painter, fill, *, cache=None):
    """Preflight output bounds before requesting an unclipped mesh.

    None means use geometric clipping. Vector/high-depth output stays vector or
    high-depth, and composition modes that erase transparent pixels keep their
    original path semantics. Scratch pixels are allocated only on a raster
    cache miss, after determining whether this shape needs raster clipping.
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
        return OutlineRaster(QRect(), mapping, QImage(), QImage(), cache)
    bounds = area.toAlignedRect()
    if bounds.width()*bounds.height() > MAX_RASTER_PIXELS:
        return None
    mapping *= QTransform.fromTranslate(-bounds.left(), -bounds.top())
    return OutlineRaster(bounds, mapping, QImage(), QImage(), cache)
