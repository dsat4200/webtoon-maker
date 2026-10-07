"""Low-resolution live chapter navigator with a viewport handle."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRect, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter, QPen, QPolygonF
from PySide6.QtWidgets import QApplication, QWidget

from comic_editor.ui.async_projection import ProjectionPending


class ChapterPreview(QWidget):
    scrollRequested = Signal(float)
    REFRESH_DELAY_MS = 120
    REFRESH_BAND_HEIGHT = 32

    def __init__(self, canvas, parent=None):
        super().__init__(parent)
        self.canvas = canvas
        self.disk_cache = None
        self._cache_drag = None
        self._cache = QImage()
        self._cache_chapter = None
        self._dirty_full = True
        self._dirty_bands: list[QRect] = []
        self._pending_image = QImage()
        self._pending_chapter = None
        self._pending_region = QRect()
        self._pending_row = 0
        self._pending_full = False
        self._pending_context = None
        self._pending_derived_refresh = False
        self._pending_source_dependency = None
        self._refresh_timer = QTimer(self)
        self._refresh_timer.setSingleShot(True)
        self._refresh_timer.timeout.connect(self._refresh_cache)
        self.setFixedWidth(92)
        self.setMinimumHeight(200)
        self.setCursor(Qt.PointingHandCursor)
        canvas.documentChanged.connect(self.invalidate)
        canvas.visualChanged.connect(self.invalidate)
        canvas.derivedResultReady.connect(self._derived_ready)
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

    def _build_context(self):
        """Use the canvas' existing document/view contracts between bands."""
        chapter = self.canvas.chapter
        if chapter is None:
            return None
        configuration = getattr(self.canvas, '_projection_configuration', None)
        projection = getattr(self.canvas, '_document_projection', None)
        history = getattr(self.canvas, 'command_stack', None)
        contract = getattr(chapter, 'pixel_contract', None)
        return (id(chapter), id(getattr(self.canvas, 'tiles', None)),
                id(getattr(self.canvas, 'images', None)),
                chapter.width, chapter.height, getattr(chapter, 'background', None),
                getattr(contract, 'signature', None),
                getattr(projection, 'revision', None),
                getattr(self.canvas, '_history_generation', None),
                getattr(history, 'revision', None),
                configuration() if configuration is not None else (),
                getattr(getattr(self.canvas, "images", None), "_decode_generation", None))

    def _derived_ready(self) -> None:
        # Channel-local native completions cannot improve this separate
        # transient Navigator capture. Global readiness still reaches
        # Posterize/Canvas; source handoffs and unknown signals refresh.
        jobs = getattr(self.canvas, "_effect_jobs", None)
        if getattr(jobs, "_navigator_derived_relevance", None) is False:
            return
        # Worker completion may improve any band, but cannot change the model.
        # Finish this same-model provisional frame, then refresh once more.
        # Never debounce its active continuation timer with another completion.
        if not self._pending_image.isNull():
            if self._pending_context != self._build_context():
                self.invalidate_all()
                return
            dependency = self._pending_source_dependency
            if (dependency is not None and (
                    getattr(jobs, "_navigator_derived_retry", False) is True
                    or getattr(jobs, "_navigator_derived_dependency", None) == dependency)):
                from comic_editor.ui.source_images import navigator_source_handoff_current
                if navigator_source_handoff_current(self.canvas, *dependency):
                    # This band has never published or advanced past its
                    # original source. Completing that exact native decode
                    # or capacity-only release resumes its existing waiter.
                    # Earlier bands cannot
                    # improve from decoding the same immutable originals.
                    # A released worker has capacity now. Resume on the
                    # next Qt turn instead of letting another paint claim it
                    # during the ordinary 120 ms source wait. A still-running
                    # or rejected source returns to that bounded wait below.
                    if (not self._refresh_timer.isActive()
                            or self._refresh_timer.remainingTime() > 1):
                        self._refresh_timer.start(1)
                    return
            self._pending_derived_refresh = True
        else:
            self._dirty_full = True
            self._dirty_bands.clear()
        if not self._refresh_timer.isActive():
            self._schedule_refresh()

    def _abandon_build(self) -> None:
        """Keep the uncommitted region dirty if content changes between bands."""
        if self._pending_image.isNull():
            return
        self._dirty_full |= self._pending_full or self._pending_derived_refresh
        if not self._dirty_full:
            dirty = self._pending_region
            if self._dirty_bands:
                dirty = dirty.united(self._dirty_bands[0])
            self._dirty_bands[:] = [dirty]
        self._pending_image = QImage()
        self._pending_chapter = None
        self._pending_context = None
        self._pending_derived_refresh = False
        self._pending_source_dependency = None

    def _interaction_active(self) -> bool:
        # Mouse buttons also cover drags in property controls outside the
        # canvas. Tablet/touch contacts and direct canvas gestures use their
        # own state, so pausing the pointer while held does not start a render.
        if QApplication.mouseButtons() != Qt.NoButton:
            return True
        live_preview = getattr(self.canvas, '_projection_has_live_preview', None)
        if live_preview is not None and live_preview():
            return True
        if any(getattr(self.canvas, field, None) for field in (
            "_drawing", "_pen_contact_active", "_nav_mode", "_touch_points",
            "_vector_gesture_mode", "_transform_drag_mode", "_bound_drag_mode",
            "_selection_transform_mode", "_fill_gesture_active", "_cage_drag",
            "_modifier_handle_drag", "_mask_gradient_drag", "_text_property_drag",
            "_shape_property_drag", "_active_gradient_control",
            "_text_editing", "_text_dragging", "_free_text_drag", "_text_placement",
            "_modifier_parameter_drag_id", "_overlay_color_preview",
        )):
            return True
        wheel = getattr(self.canvas, "_wheel_zoom_timer", None)
        return wheel is not None and wheel.isActive()

    def _refresh_cache(self) -> None:
        self._refresh_timer.stop()
        if not self.isVisible() or self.canvas.chapter is None:
            return
        if self.disk_cache is not None and self.disk_cache.building:
            self._refresh_timer.start(self.REFRESH_DELAY_MS)
            return
        if self._interaction_active():
            self._schedule_refresh()
            return
        size = self.content_rect().size()
        if (not self._pending_image.isNull() and
                (self._pending_chapter is not self.canvas.chapter or self._pending_image.size() != size
                 or self._pending_context != self._build_context())):
            self._abandon_build()
            self._dirty_full = True
        if self._pending_source_dependency is not None:
            scope, key = self._pending_source_dependency
            jobs = getattr(self.canvas, '_effect_jobs', None)
            queued = jobs.pending.get(scope) if jobs is not None else None
            if jobs is not None and (jobs.has_running(scope, key)
                    or queued is not None and queued[1] == key
                    or jobs.waiting.get(scope) == key):
                # Wait only on existing job metadata. Failure, cancellation or
                # eviction must release this private wait too, even when no
                # successful derived-ready signal can arrive.
                self._refresh_timer.start(self.REFRESH_DELAY_MS)
                return
            self._pending_source_dependency = None
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
            self._pending_context = self._build_context()
            self._pending_derived_refresh = False
            self._dirty_full = False
            self._dirty_bands.clear()
        image = self._pending_image
        dirty = QRect(self._pending_region.left(), self._pending_row,
                      self._pending_region.width(), min(self.REFRESH_BAND_HEIGHT,
                          self._pending_region.bottom() - self._pending_row + 1))
        try:
            self._render_live_preview(image, None if dirty == image.rect() else dirty)
        except ProjectionPending as dependency:
            # The current private band is unfinished. Retry its background and
            # pixels when the existing source worker releases a dependency;
            # keep the committed cache, context and row unchanged meanwhile.
            # A bounded metadata wait also resumes terminal failed/canceled
            # jobs without changing global readiness or rerendering at1ms.
            if image is self._pending_image:
                self._pending_source_dependency = (dependency.scope, dependency.key)
                if not self._refresh_timer.isActive():
                    self._refresh_timer.start(self.REFRESH_DELAY_MS)
            return
        except Exception:
            self._abandon_build()
            self._dirty_full = True
            raise
        # A callback during rendering can replace the document or invalidate
        # this build. Never publish pixels from an older document revision.
        if image is not self._pending_image:
            return
        if (self._pending_chapter is not self.canvas.chapter
                or self._pending_context != self._build_context()):
            self._abandon_build()
            self._dirty_full = True
            self._schedule_refresh()
            return
        self._pending_row += dirty.height()
        if self._pending_row <= self._pending_region.bottom():
            # Return to Qt between bands so queued input precedes more work.
            self._refresh_timer.start(1)
        else:
            refresh_again = self._pending_derived_refresh
            self._cache = image
            self._cache_chapter = self._pending_chapter
            self._pending_image = QImage()
            self._pending_chapter = None
            self._pending_context = None
            self._pending_derived_refresh = False
            self._pending_source_dependency = None
            if refresh_again:
                self._dirty_full = True
                self._dirty_bands.clear()
                if not self._refresh_timer.isActive():
                    self._schedule_refresh()
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
        cache = self.disk_cache
        if cache is not None and cache.backing is not None:
            x = self.width() - 7
            height = self.canvas.chapter.height
            side = self.canvas._document_projection.tile_size
            for row in range((height + side - 1) // side):
                top = preview_rect.top() + row * side / height * preview_rect.height()
                bottom = preview_rect.top() + min(height, (row + 1) * side) / height * preview_rect.height()
                color = "#45da63" if cache.status.get(row, False) else "#ed4343"
                if cache.building and row in cache.selected_rows and not cache.status.get(row, False):
                    color = "#eab345"
                painter.fillRect(QRectF(x, top, 3, max(1., bottom - top)), QColor(color))
            painter.setPen(QPen(QColor("#eeeeee"), 1))
            painter.setBrush(QColor("#eeeeee"))
            start = preview_rect.top() + cache.start_y / height * preview_rect.height()
            end = preview_rect.top() + cache.end_y / height * preview_rect.height()
            painter.drawLine(QPointF(x + 5, start), QPointF(x + 5, end))
            for y in (start, end):
                painter.drawPolygon(QPolygonF([QPointF(x - 9, y), QPointF(x - 2, y - 5), QPointF(x - 2, y + 5)]))

    def _render_live_preview(self, image, clip=None):
        previous = self.canvas._interactive_render
        channel = getattr(self.canvas, "_effect_preview_channel", "canvas")
        missing = object()
        source_deferred = getattr(self.canvas, '_navigator_defer_sources', missing)
        self.canvas._interactive_render = True
        self.canvas._effect_preview_channel = "navigator"
        self.canvas._navigator_defer_sources = True
        try:
            self.canvas.render_preview(image, clip)
        finally:
            self.canvas._interactive_render = previous
            self.canvas._effect_preview_channel = channel
            if source_deferred is missing:
                del self.canvas._navigator_defer_sources
            else:
                self.canvas._navigator_defer_sources = source_deferred

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.LeftButton:
            cache = self.disk_cache
            if cache is not None and cache.backing is not None and event.position().x() >= self.width() - 20:
                if not cache.building:
                    rect = self.content_rect()
                    y = (event.position().y() - rect.top()) / max(1, rect.height()) * self.canvas.chapter.height
                    self._cache_drag = "start" if abs(y - cache.start_y) < abs(y - cache.end_y) else "end"
                    self._drag_cache(event.position().y())
                event.accept()
                return
            self._scroll(event.position().y())

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.buttons() & Qt.LeftButton:
            if self._cache_drag:
                self._drag_cache(event.position().y())
                return
            self._scroll(event.position().y())

    def mouseReleaseEvent(self, event):  # noqa: N802
        if self._cache_drag:
            self._drag_cache(event.position().y(), save=True)
            self._cache_drag = None

    def _drag_cache(self, y, *, save=False):
        cache = self.disk_cache
        rect = self.content_rect()
        value = max(0., min(self.canvas.chapter.height,
            (y - rect.top()) / max(1, rect.height()) * self.canvas.chapter.height))
        if self._cache_drag == "start":
            cache.set_range(min(value, cache.end_y - 1), cache.end_y, save=save)
        else:
            cache.set_range(cache.start_y, max(value, cache.start_y + 1), save=save)

    def _scroll(self, y: float) -> None:
        usable = self.content_rect()
        fraction = (y - usable.top()) / max(1, usable.height())
        self.scrollRequested.emit(max(0.0, min(1.0, fraction)))
