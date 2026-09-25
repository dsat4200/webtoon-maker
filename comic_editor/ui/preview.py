"""Low-resolution live chapter navigator with a viewport handle."""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QApplication, QWidget


class ChapterPreview(QWidget):
    scrollRequested = Signal(float)
    REFRESH_DELAY_MS = 120
    REFRESH_BAND_HEIGHT = 32

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._cache = QImage()
        self._cache_chapter = None
        self._dirty_full = True
        self._dirty_bands: list[QRect] = []
        self._pending_image = QImage()
        self._pending_chapter = None
        self._pending_region = QRect()
        self._pending_row = 0
        self._pending_full = False
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_cache)
        self.setFixedWidth(92)
        self.setMinimumHeight(200)
        self.setCursor(Qt.PointingHandCursor)
        canvas.documentChanged.connect(self.invalidate)
        canvas.visualChanged.connect(self.invalidate)
        canvas.hierarchyChanged.connect(self.invalidate_all)
        canvas.cameraChanged.connect(self.update)
        canvas.interactionFinished.connect(self._schedule_refresh)

    def invalidate_all(self) -> None:
        self._abandon_build()
        self._dirty_full = True
        self._dirty_bands.clear()
        self._schedule_refresh()

    def invalidate(self, world_rect) -> None:
        self._abandon_build()
        chapter = self.canvas.chapter
        if (
            chapter is None or self._cache.isNull() or world_rect is None
            or not hasattr(world_rect, "isEmpty") or world_rect.isEmpty()
        ):
            self._dirty_full = True
            self._dirty_bands.clear()
        else:
            top = max(0, int(world_rect.top() / chapter.height * self._cache.height()) - 2)
            bottom = min(
                self._cache.height(),
                int(world_rect.bottom() / chapter.height * self._cache.height()) + 3,
            )
            dirty = QRect(0, top, self._cache.width(), max(1, bottom - top))
            # A long stroke can produce thousands of updates before release.
            # Retain its union, rather than one allocation per input sample.
            if self._dirty_bands:
                dirty = dirty.united(self._dirty_bands[0])
            self._dirty_bands[:] = [dirty]
        self._schedule_refresh()

    def _schedule_refresh(self) -> None:
        if self.isVisible() and (self._dirty_full or self._dirty_bands or not self._pending_image.isNull()):
            self._refresh_timer.start(self.REFRESH_DELAY_MS)

    def _abandon_build(self) -> None:
        """Keep the uncommitted region dirty if content changes between bands."""
        if self._pending_image.isNull():
            return
        self._dirty_full |= self._pending_full
        if not self._dirty_full:
            dirty = self._pending_region
            if self._dirty_bands:
                dirty = dirty.united(self._dirty_bands[0])
            self._dirty_bands[:] = [dirty]
        self._pending_image = QImage()
        self._pending_chapter = None

    def _interaction_active(self) -> bool:
        # Mouse buttons also cover drags in property controls outside the
        # canvas. Tablet/touch contacts and direct canvas gestures use their
        # own state, so pausing the pointer while held does not start a render.
        if QApplication.mouseButtons() != Qt.NoButton:
            return True
        if any(getattr(self.canvas, field, None) for field in (
            "_drawing", "_pen_contact_active", "_nav_mode", "_touch_points",
            "_vector_gesture_mode", "_transform_drag_mode", "_bound_drag_mode",
            "_selection_transform_mode", "_fill_gesture_active", "_cage_drag",
            "_modifier_handle_drag", "_mask_gradient_drag", "_text_property_drag",
            "_shape_property_drag", "_active_gradient_control",
        )):
            return True
        wheel = getattr(self.canvas, "_wheel_zoom_timer", None)
        return wheel is not None and wheel.isActive()

    def _refresh_cache(self) -> None:
        self._refresh_timer.stop()
        if not self.isVisible() or self.canvas.chapter is None:
            return
        if self._interaction_active():
            self._schedule_refresh()
            return
        size = self.content_rect().size()
        if (not self._pending_image.isNull() and
                (self._pending_chapter is not self.canvas.chapter or self._pending_image.size() != size)):
            self._abandon_build()
            self._dirty_full = True
        if self._pending_image.isNull():
            full = (self._dirty_full or self._cache.isNull() or self._cache.size() != size
                    or self._cache_chapter is not self.canvas.chapter)
            if not full and not self._dirty_bands:
                return
            self._pending_full = full
            self._pending_image = (QImage(size, QImage.Format_ARGB32_Premultiplied)
                                   if full else QImage(self._cache))
            if full:
                self._pending_image.fill(QColor(getattr(self.canvas.chapter, "background", "#18181c")))
            self._pending_region = (self._pending_image.rect() if full
                                    else self._dirty_bands[0].intersected(self._pending_image.rect()))
            self._pending_row = self._pending_region.top()
            self._pending_chapter = self.canvas.chapter
            self._dirty_full = False
            self._dirty_bands.clear()
        image = self._pending_image
        dirty = QRect(self._pending_region.left(), self._pending_row,
                      self._pending_region.width(), min(self.REFRESH_BAND_HEIGHT,
                          self._pending_region.bottom() - self._pending_row + 1))
        try:
            self._render_live_preview(image, None if dirty == image.rect() else dirty)
        except Exception:
            self._abandon_build()
            self._dirty_full = True
            raise
        # A callback during rendering can replace the document or invalidate
        # this build. Never publish pixels from an older document revision.
        if image is not self._pending_image:
            return
        if self._pending_chapter is not self.canvas.chapter:
            self._abandon_build()
            self._dirty_full = True
            self._schedule_refresh()
            return
        self._pending_row += dirty.height()
        if self._pending_row <= self._pending_region.bottom():
            # Return to Qt between bands so queued input precedes more work.
            self._refresh_timer.start(1)
        else:
            self._cache = image
            self._cache_chapter = self._pending_chapter
            self._pending_image = QImage()
            self._pending_chapter = None
            self.update()

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._schedule_refresh()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._refresh_timer.stop()
        super().hideEvent(event)

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._abandon_build()
        self._dirty_full = True
        self._dirty_bands.clear()
        self._schedule_refresh()
        super().resizeEvent(event)

    def content_rect(self) -> QRect:
        available = self.rect().adjusted(8, 8, -8, -8)
        chapter = self.canvas.chapter
        if chapter is None or available.isEmpty():
            return available
        scale = min(
            available.width() / max(1, chapter.width),
            available.height() / max(1, chapter.height),
        )
        width = max(1, round(chapter.width * scale))
        height = max(1, round(chapter.height * scale))
        return QRect(
            available.center().x() - width // 2,
            available.center().y() - height // 2,
            width, height,
        )

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.fillRect(self.rect(), QColor("#18181c"))
        if self.canvas.chapter is None:
            return
        preview_rect = self.content_rect()
        # Paint only cached pixels. Scene rendering here competes directly
        # with input delivery, even for a tiny dirty navigator band.
        if not self._cache.isNull() and self._cache_chapter is self.canvas.chapter:
            painter.drawImage(preview_rect, self._cache)
        top_fraction, height_fraction = self.canvas.viewport_fraction()
        handle_height = min(
            preview_rect.height(),
            max(18, round(preview_rect.height() * height_fraction)),
        )
        handle_top = preview_rect.top() + round(
            (preview_rect.height() - handle_height)
            * top_fraction / max(0.0001, 1.0 - height_fraction)
        )
        handle = QRect(
            preview_rect.left(), handle_top, preview_rect.width(), handle_height
        ).intersected(preview_rect)
        painter.setPen(QPen(QColor("#80c8ff"), 2))
        painter.setBrush(QColor(128, 200, 255, 35))
        painter.drawRect(handle)

    def _render_live_preview(self, image, clip=None):
        previous = self.canvas._interactive_render
        channel = getattr(self.canvas, "_effect_preview_channel", "canvas")
        self.canvas._interactive_render = True
        self.canvas._effect_preview_channel = "navigator"
        try:
            self.canvas.render_preview(image, clip)
        finally:
            self.canvas._interactive_render = previous
            self.canvas._effect_preview_channel = channel

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            self._scroll(event.position().y())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            self._scroll(event.position().y())

    def _scroll(self, y: float) -> None:
        usable = self.content_rect()
        fraction = (y - usable.top()) / max(1, usable.height())
        self.scrollRequested.emit(max(0.0, min(1.0, fraction)))
