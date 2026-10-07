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


def _clipped_visible(canvas, painter, visible):
    visible = QRectF(visible)
    if painter.hasClipping():
        # The existing clip is expressed in the painter's current logical
        # coordinates, before the document camera is installed below. Keep a
        # conservative document-space bounding rectangle; a rotated/nonrect
        # clip remains enforced by the painter itself. This only skips fixed
        # capture blocks: their source frame, gutters, and sampling grid stay
        # identical to a complete preview.
        inverse, valid = canvas.camera_transform().inverted()
        if valid:
            widget_clip = painter.worldTransform().mapRect(painter.clipBoundingRect())
            visible = visible.intersected(inverse.mapRect(widget_clip))
    return visible


def paint_scene(canvas, painter, visible, *, presentation_owner=None):
    """Preview kernels run on native/below-native devices, then get enlarged."""
    from comic_editor.render.service import RenderRequest, RenderQuality
    from comic_editor.render.pixels import current_contract
    if (presentation_owner is canvas and getattr(canvas, '_acquired_preview_presentation_owner', False)
            and canvas._projection_has_live_preview()
            and not canvas.chapter.pixel_contract.floating and not current_contract().floating):
        return _paint_compact_live_scene(canvas, painter, visible)
    visible = _clipped_visible(canvas, painter, visible)
    if visible.isEmpty():
        return
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


def _paint_compact_live_scene(canvas, painter, visible):
    """One owned live request on a stable bounded below-native viewport grid."""
    from comic_editor.render.service import RenderRequest, RenderQuality, RenderStatus
    full = QRectF(visible)
    if full.isEmpty() or not all(math.isfinite(value) for value in full.getRect()):
        return
    density = min(1., artwork_density(abs(canvas.scale) * canvas.devicePixelRatioF()),
                  math.sqrt((1024 * 1024) / (full.width() * full.height())))
    width, height = max(1, math.floor(full.width() * density)), max(1, math.floor(full.height() * density))
    density = min(density, width / full.width(), height / full.height())
    region = full.adjusted(-2 / density, -2 / density, 2 / density, 2 / density)
    requested = _clipped_visible(canvas, painter, full).intersected(region)
    if requested.isEmpty():
        return
    document = canvas._render_document_state()
    request = RenderRequest(tuple(region.getRect()), density, (width + 4, height + 4),
        ('native-preview-compact', tuple(full.getRect()), density, width, height), document.revision,
        requested_region=tuple(requested.getRect()), quality=RenderQuality.INTERACTIVE,
        defer_effects=False)
    result = canvas._render_service.render_region(document, request)
    if (not canvas._render_service.current(document, request) or result.image.isNull()
            or result.status not in (RenderStatus.EXACT, RenderStatus.PROVISIONAL)):
        return
    painter.save()
    try:
        painter.setTransform(canvas.camera_transform())
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        painter.drawImage(full, result.image, QRectF(2, 2, full.width() * density, full.height() * density))
    finally:
        painter.restore()


def capture_scene(canvas, *, max_pixels=1024 * 1024):
    """Capture current artwork as a bounded, transient viewport presentation.

    The returned image has a device ratio, so drawing it at widget position
    (0, 0) retains the current logical viewport. Its resolution never exceeds
    one sample per document pixel and the supplied pixel budget. The ordinary
    native preview kernels and their grids are unchanged; no exact projection
    tiles or durable cache entries are created by this helper.
    """
    width, height = canvas.width(), canvas.height()
    if canvas.chapter is None or width <= 0 or height <= 0:
        return QImage()
    ratio = min(max(.01, canvas.devicePixelRatioF()),
                1. / max(abs(canvas.scale), .01))
    budget = max(1, int(max_pixels))
    if width * height * ratio * ratio > budget:
        ratio = math.sqrt(budget / (width * height))
    # Flooring keeps the strict allocation budget, even at fractional DPR.
    pixel_width, pixel_height = max(1, math.floor(width * ratio)), max(1, math.floor(height * ratio))
    ratio = min(pixel_width / width, pixel_height / height)
    image = QImage(pixel_width, pixel_height, canvas.chapter.pixel_contract.image_format)
    image.setDevicePixelRatio(ratio)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    try:
        painter.setClipRect(QRectF(0, 0, width, height))
        paint_scene(canvas, painter, canvas.visible_document_rect())
    finally:
        painter.end()
    return image


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
