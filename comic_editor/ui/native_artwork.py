"""Bounded native artwork captures for transient presentation paths."""
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.render.sampling import artwork_density


def captures(canvas, visible):
    density = artwork_density(abs(canvas.scale) * canvas.devicePixelRatioF())
    side = 1024 / density
    for y in range(math.floor(visible.top() / side), math.ceil(visible.bottom() / side)):
        for x in range(math.floor(visible.left() / side), math.ceil(visible.right() / side)):
            world = QRectF(x * side, y * side, side, side)
            region = world.adjusted(-2 / density, -2 / density, 2 / density, 2 / density)
            yield world, region, density


def paint_scene(canvas, painter, visible):
    """Preview kernels run on native/below-native devices, then get enlarged."""
    from comic_editor.render.service import RenderRequest, RenderQuality
    document = canvas._render_document_state()
    painter.save()
    try:
        painter.setTransform(canvas.camera_transform())
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for world, region, density in captures(canvas, visible):
            request = RenderRequest(tuple(region.getRect()), density, (1028, 1028),
                ("native-preview", world.x(), world.y()), document.revision,
                requested_region=tuple(visible.intersected(region).getRect()),
                quality=RenderQuality.INTERACTIVE)
            result = canvas._render_service.render_region(document, request)
            if not result.image.isNull():
                painter.drawImage(world, result.image, QRectF(2, 2, 1024, 1024))
    finally:
        painter.restore()


def paint_overlay(canvas, painter, visible, callback):
    """Return true after replacing a high-density live artwork draw."""
    transform = painter.deviceTransform()
    density = max(math.hypot(transform.m11(), transform.m12()), math.hypot(transform.m21(), transform.m22()))
    if density <= 1.000001:
        return False
    painter.save()
    try:
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        for world, region, scale in captures(canvas, visible):
            image = QImage(1028, 1028, canvas.chapter.pixel_contract.image_format)
            image.fill(Qt.transparent)
            local = QPainter(image)
            try:
                local.setRenderHint(QPainter.Antialiasing, True)
                local.setTransform(QTransform(scale, 0, 0, scale, -region.x() * scale, -region.y() * scale))
                local.setClipRect(QRectF(0, 0, canvas.chapter.width, canvas.chapter.height))
                callback(local, region)
            finally:
                local.end()
            painter.drawImage(world, image, QRectF(2, 2, 1024, 1024))
    finally:
        painter.restore()
    return True
