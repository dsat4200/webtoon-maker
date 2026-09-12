"""Canvas-only overflow display and a persistent, independent export region."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPen, QTransform

from comic_editor.core.commands import CallbackCommand


class ViewFeatures:
    @property
    def export_rect_editing(self) -> bool:
        return bool(getattr(self, "_export_rect_editing", False))

    def export_source_rect(self) -> QRectF:
        if self.chapter is None:
            return QRectF()
        if self.chapter.export_rect_enabled and self.chapter.export_rect is not None:
            rect = QRectF(*self.chapter.export_rect)
            # Cover partial edge pixels without scaling the scene or truncating it.
            left, top = math.floor(rect.left()), math.floor(rect.top())
            return QRectF(left, top, math.ceil(rect.right()) - left,
                          math.ceil(rect.bottom()) - top)
        return QRectF(0, 0, self.chapter.width, self.chapter.height)

    def render_export_image(self) -> QImage:
        source = self.export_source_rect()
        if source.isEmpty():
            raise ValueError("There is no document to export")
        image = QImage(int(source.width()), int(source.height()),
                       QImage.Format.Format_ARGB32_Premultiplied)
        if image.isNull():
            raise MemoryError("Could not allocate the export image")
        if self.chapter.export_rect_enabled and self.chapter.export_rect is not None:
            self.render_preview(image, source_rect=source)
        else:
            self.render_preview(image)
        return image

    def _view_settings_changed(self) -> None:
        if self.export_rect_editing and self.chapter.export_rect is None:
            self._reset_export_rect_editor()
        self._invalidate_scene_cache()
        self.viewSettingsChanged.emit()
        self.documentChanged.emit(QRectF())
        self.update()

    def _reset_export_rect_editor(self) -> None:
        """Discard transient handles when undo replaces the underlying model."""
        self._export_rect_editing = False
        self._export_rect_drag = None
        self._export_rect_before = None
        self.unsetCursor()

    def _record_view_change(self, attribute, before, after, label) -> None:
        if before == after:
            return

        def apply(value):
            # Model-snapshot undo may replace the chapter instance; resolve it
            # at replay time just like the canvas's other model commands.
            setattr(self.chapter, attribute, value)
            self._view_settings_changed()

        self.command_stack.push(CallbackCommand(
            label, lambda: apply(after), lambda: apply(before),
        ), already_done=True)
        self._view_settings_changed()

    def set_view_overflow(self, value: float, *, record: bool = True) -> None:
        if self.chapter is None:
            return
        value = min(1.0, max(0.0, float(value)))
        before = self.chapter.view_overflow
        if before == value:
            return
        self.chapter.view_overflow = value
        if record:
            self._record_view_change("view_overflow", before, value, "Change overflow")
        else:
            # A slider drag redraws the viewport, but creates only one undo
            # entry and one document/autosave invalidation when released.
            self._invalidate_scene_cache()
            self.update()

    def set_export_rect_enabled(self, enabled: bool) -> None:
        if self.chapter is None:
            return
        if not enabled and self.export_rect_editing:
            self.set_export_rect_editing(False)
        before = self.chapter.export_rect_enabled
        self.chapter.export_rect_enabled = bool(enabled)
        self._record_view_change("export_rect_enabled", before, bool(enabled),
                                 "Toggle export rectangle")

    def set_export_rect_editing(self, editing: bool) -> bool:
        editing = bool(editing and self.chapter is not None)
        if editing == self.export_rect_editing:
            return editing
        if editing:
            if not self.commit_active_cage():
                return False
            self.commit_active_text_edit()
            self._export_rect_before = self.chapter.export_rect
            if self.chapter.export_rect is None:
                self.chapter.export_rect = (0.0, 0.0, float(self.chapter.width),
                                            float(self.chapter.height))
            self._export_rect_drag = None
            self._export_rect_editing = True
        else:
            self._export_rect_editing = False
            self._export_rect_drag = None
            if self.chapter is not None:
                self._record_view_change("export_rect", self._export_rect_before,
                                         self.chapter.export_rect, "Edit export rectangle")
            self.unsetCursor()
        self.viewSettingsChanged.emit()
        self.update()
        return editing

    def _export_rect_handles(self):
        if self.chapter is None or self.chapter.export_rect is None:
            return {}
        rect = QRectF(*self.chapter.export_rect)
        return {
            "nw": rect.topLeft(), "n": QPointF(rect.center().x(), rect.top()),
            "ne": rect.topRight(), "e": QPointF(rect.right(), rect.center().y()),
            "se": rect.bottomRight(), "s": QPointF(rect.center().x(), rect.bottom()),
            "sw": rect.bottomLeft(), "w": QPointF(rect.left(), rect.center().y()),
        }

    def _export_rect_hit(self, position):
        transform = self.camera_transform()
        for name, point in self._export_rect_handles().items():
            delta = transform.map(point) - position
            if abs(delta.x()) <= 9 and abs(delta.y()) <= 9:
                return name
        world = self.widget_to_document(position)
        if self.chapter is None or self.chapter.export_rect is None:
            return None
        return "move" if QRectF(*self.chapter.export_rect).contains(world) else None

    def _export_rect_pointer_press(self, position) -> None:
        mode = self._export_rect_hit(position)
        self._export_rect_drag = (
            mode, self.widget_to_document(position), QRectF(*self.chapter.export_rect)
        ) if mode else None
        self._export_rect_pointer_move(position)

    def _export_rect_pointer_move(self, position) -> None:
        drag = getattr(self, "_export_rect_drag", None)
        mode = drag[0] if drag else self._export_rect_hit(position)
        if drag:
            mode, start, initial = drag
            delta = self.widget_to_document(position) - start
            rect = QRectF(initial)
            if mode == "move":
                rect.translate(delta)
            else:
                if "w" in mode:
                    rect.setLeft(min(initial.right() - 1, initial.left() + delta.x()))
                if "e" in mode:
                    rect.setRight(max(initial.left() + 1, initial.right() + delta.x()))
                if "n" in mode:
                    rect.setTop(min(initial.bottom() - 1, initial.top() + delta.y()))
                if "s" in mode:
                    rect.setBottom(max(initial.top() + 1, initial.bottom() + delta.y()))
            self.chapter.export_rect = (rect.x(), rect.y(), rect.width(), rect.height())
            # Geometry only changes a light overlay, never the scene cache.
            self.update()
        cursor = {
            "move": Qt.CursorShape.ClosedHandCursor if drag else Qt.CursorShape.OpenHandCursor,
            "nw": Qt.CursorShape.SizeFDiagCursor, "se": Qt.CursorShape.SizeFDiagCursor,
            "ne": Qt.CursorShape.SizeBDiagCursor, "sw": Qt.CursorShape.SizeBDiagCursor,
            "n": Qt.CursorShape.SizeVerCursor, "s": Qt.CursorShape.SizeVerCursor,
            "e": Qt.CursorShape.SizeHorCursor, "w": Qt.CursorShape.SizeHorCursor,
        }.get(mode, Qt.CursorShape.ArrowCursor)
        self.setCursor(cursor)

    def _export_rect_pointer_release(self, position) -> None:
        drag = getattr(self, "_export_rect_drag", None)
        self._export_rect_pointer_move(position)
        self._export_rect_drag = None
        if drag is not None and QRectF(*self.chapter.export_rect) != drag[2]:
            self.documentChanged.emit(QRectF())

    def _draw_export_rect(self, painter: QPainter) -> None:
        if self.chapter is None or self.chapter.export_rect is None:
            return
        if not (self.chapter.export_rect_enabled or self.export_rect_editing):
            return
        painter.save()
        painter.setTransform(QTransform())
        path = QPainterPath()
        path.addRect(QRectF(*self.chapter.export_rect))
        mapped = self.camera_transform().map(path)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        painter.setPen(QPen(QColor("#ffbe55"), 1.5, Qt.PenStyle.DashLine))
        painter.drawPath(mapped)
        if self.export_rect_editing:
            painter.setPen(QPen(QColor("#292929"), 1))
            painter.setBrush(QColor("#ffbe55"))
            for point in self._export_rect_handles().values():
                point = self.camera_transform().map(point)
                painter.drawRect(QRectF(point.x() - 4, point.y() - 4, 8, 8))
        painter.restore()

    def _render_canvas_overflow(self, painter: QPainter, dirty, visible: QRectF) -> None:
        opacity = self.chapter.view_overflow
        if opacity <= 0:
            self._view_overflow_image = QImage()
            return
        page_area = QPainterPath()
        for page_id in self.chapter.root_page_ids:
            page = self.chapter.layers[page_id]
            if page.visible:
                page_area = page_area.united(self.layer_world_transform(page_id).map(
                    self.layer_effective_path(page_id)))
        chapter_area = QPainterPath()
        chapter_area.addRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        viewport = QPainterPath()
        viewport.addRect(visible)
        outside = viewport.subtracted(page_area.intersected(chapter_area))
        if outside.isEmpty():
            return
        ratio = self._scene_cache.devicePixelRatio()
        image = getattr(self, "_view_overflow_image", QImage())
        if image.size() != self._scene_cache.size() or image.devicePixelRatio() != ratio:
            image = QImage(self._scene_cache.size(), QImage.Format.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(ratio)
            image.fill(Qt.GlobalColor.transparent)
            self._view_overflow_image = image
        overflow = QPainter(image)
        overflow.setClipRect(dirty)
        overflow.setCompositionMode(QPainter.CompositionMode.CompositionMode_Source)
        overflow.fillRect(dirty, Qt.GlobalColor.transparent)
        overflow.setCompositionMode(QPainter.CompositionMode.CompositionMode_SourceOver)
        overflow.setRenderHint(QPainter.RenderHint.Antialiasing)
        overflow.setTransform(self.camera_transform())
        overflow.setClipPath(outside, Qt.ClipOperation.IntersectClip)
        previous = self._interactive_render
        previous_channel = getattr(self, "_effect_preview_channel", "canvas")
        self._interactive_render = True
        self._effect_preview_channel = "overflow"
        try:
            for page_id in reversed(self.chapter.root_page_ids):
                page = self.chapter.layers[page_id]
                if not page.visible or page.opacity <= 0:
                    continue
                # Pages permit opacity and translation but not modifiers,
                # compound operations or parameter masks. Preserve those root
                # properties and every descendant mask/effect, bypassing only
                # the page's outer clip.
                overflow.save()
                transform = self.layer_world_transform(page_id)
                overflow.setTransform(transform, True)
                inverse, valid = transform.inverted()
                local_visible = inverse.mapRect(visible) if valid else visible
                for child in reversed(page.children):
                    if child.kind == "layer":
                        self._render_layer(overflow, self.chapter.layers[child.entity_id],
                                           page.opacity, visible)
                    else:
                        self._render_object(overflow, self.chapter.objects[child.entity_id],
                                            page.opacity, local_visible)
                overflow.restore()
        finally:
            self._interactive_render = previous
            self._effect_preview_channel = previous_channel
            overflow.end()
        painter.save()
        painter.setTransform(QTransform())
        painter.setOpacity(opacity)
        painter.drawImage(0, 0, image)
        painter.restore()
