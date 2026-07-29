"""Low-resolution live chapter navigator with a viewport handle."""
from __future__ import annotations

from PySide6.QtCore import QRect, QRectF, Qt, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import QWidget


class ChapterPreview(QWidget):
    scrollRequested = Signal(float)

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self._cache = QImage()
        self._dirty_full = True
        self._dirty_bands: list[QRect] = []
        self.setFixedWidth(92)
        self.setMinimumHeight(200)
        self.setCursor(Qt.PointingHandCursor)
        canvas.documentChanged.connect(self.invalidate)
        canvas.hierarchyChanged.connect(self.invalidate_all)
        canvas.cameraChanged.connect(self.update)

    def invalidate_all(self) -> None:
        self._dirty_full = True
        self._dirty_bands.clear()
        self.update()

    def invalidate(self, world_rect) -> None:
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
            self._dirty_bands.append(QRect(0, top, self._cache.width(), max(1, bottom - top)))
        self.update()

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._dirty_full = True
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
        if (
            self._dirty_full or self._cache.size() != preview_rect.size()
            or self._cache.isNull()
        ):
            self._cache = QImage(
                max(1, preview_rect.width()), max(1, preview_rect.height()),
                QImage.Format_ARGB32_Premultiplied,
            )
            self.canvas.render_preview(self._cache)
            self._dirty_full = False
            self._dirty_bands.clear()
        elif self._dirty_bands:
            dirty = self._dirty_bands.pop()
            while self._dirty_bands:
                dirty = dirty.united(self._dirty_bands.pop())
            self.canvas.render_preview(self._cache, dirty)
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
