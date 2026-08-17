"""Application-level lifecycle for Blender-backed ImageObjects."""
from __future__ import annotations

from PySide6.QtCore import QObject, QRectF, QTimer, Signal
from PySide6.QtGui import QPolygonF
from PySide6.QtCore import QPointF

from comic_editor.core.models import (
    BlenderComicViewSourceDescriptor, ImageObject,
)
from comic_editor.integrations.blender_source import (
    BlenderSourceClient, ComicViewInfo,
)


def aspect_adjusted_quad(
    quad: list[tuple[float, float]], old_size: tuple[int, int],
    new_size: tuple[int, int],
) -> list[tuple[float, float]]:
    """Keep a quad's horizontal midline/width while changing its aspect."""
    if len(quad) != 4:
        return list(quad)
    old_width, old_height = old_size
    new_width, new_height = new_size
    factor = (
        float(old_width) * float(new_height)
        / max(1.0, float(old_height) * float(new_width))
    )
    result = [tuple(point) for point in quad]
    for first, second in ((0, 3), (1, 2)):
        midpoint = (
            (quad[first][0] + quad[second][0]) / 2.0,
            (quad[first][1] + quad[second][1]) / 2.0,
        )
        for index in (first, second):
            result[index] = (
                midpoint[0] + (quad[index][0] - midpoint[0]) * factor,
                midpoint[1] + (quad[index][1] - midpoint[1]) * factor,
            )
    return result


class BlenderImageSourceController(QObject):
    viewsChanged = Signal(object)
    connectionStateChanged = Signal(str)
    streamStatusChanged = Signal(str)
    switchDecisionRequired = Signal(object)
    cachePersisted = Signal()
    errorOccurred = Signal(str)

    def __init__(self, canvas, parent: QObject | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self.client = BlenderSourceClient(self)
        self._views: dict[str, ComicViewInfo] = {}
        self._pending_ids: set[str] = set()
        self._preview_ids: set[str] = set()
        self._idle_flush = QTimer(self)
        self._idle_flush.setSingleShot(True)
        self._idle_flush.setInterval(1000)
        self._idle_flush.timeout.connect(self.flush_pending_frames)
        self._maximum_flush = QTimer(self)
        self._maximum_flush.setSingleShot(True)
        self._maximum_flush.setInterval(5000)
        self._maximum_flush.timeout.connect(self.flush_pending_frames)
        self.client.viewsChanged.connect(self._set_views)
        self.client.frameReady.connect(self._frame_ready)
        self.client.connectionStateChanged.connect(self._connection_changed)
        self.client.streamStatusChanged.connect(self._stream_status_changed)
        self.client.switchDecisionRequired.connect(self.switchDecisionRequired)
        self.client.errorOccurred.connect(self.errorOccurred)

    @property
    def views(self) -> list[ComicViewInfo]:
        return list(self._views.values())

    def connect_to_provider(self, host: str, port: int, token: str) -> None:
        self.flush_pending_frames()
        self._views.clear()
        self.viewsChanged.emit([])
        self.client.connect_to_provider(host, port, token)

    def disconnect(self) -> None:
        self.flush_pending_frames()
        self.clear_previews()
        self.client.disconnect_from_provider()

    def shutdown(self) -> None:
        self.disconnect()

    def _connection_changed(self, state: str) -> None:
        if state != "connected":
            self.flush_pending_frames()
            self.clear_previews()
        self.connectionStateChanged.emit(state)

    def _stream_status_changed(self, status: str) -> None:
        if status in {"stopped", "frozen", "offline", "unavailable", "stale", "error"}:
            self.clear_previews()
        self.streamStatusChanged.emit(status)

    def _set_views(self, values: list[ComicViewInfo]) -> None:
        self._views = {view.view_uuid: view for view in values}
        self.viewsChanged.emit(list(values))
        self.handle_selection()

    def selected_linked_object(self) -> ImageObject | None:
        chapter = self.canvas.chapter
        if chapter is None or self.canvas.selected_kind != "object":
            return None
        obj = chapter.objects.get(self.canvas.selected_id)
        return obj if isinstance(obj, ImageObject) and obj.is_blender_linked else None

    def handle_selection(self, *_args) -> None:
        obj = self.selected_linked_object()
        if obj is None:
            self.clear_previews()
            self.client.stop_stream()
            return
        if not self.client.connected:
            self.clear_previews()
            self.client.stop_stream()
            self.streamStatusChanged.emit("offline")
            return
        source = obj.source
        if isinstance(source, BlenderComicViewSourceDescriptor):
            if self._preview_ids:
                preview_sources = {
                    candidate.source.view_uuid
                    for candidate in self._matching_objects(
                        source.project_uuid, source.view_uuid
                    )
                    if candidate.object_id in self._preview_ids
                }
                if source.view_uuid not in preview_sources:
                    self.clear_previews()
            view = self._views.get(source.view_uuid)
            if view is None or view.project_uuid != source.project_uuid:
                self.clear_previews()
                self.client.stop_stream()
                self.streamStatusChanged.emit("unavailable")
                return
            if view.revision < source.last_revision:
                self.clear_previews()
                self.client.stop_stream()
                self.streamStatusChanged.emit("stale")
                return
            self.client.activate_view(source.view_uuid)

    def stop_for_context_change(self) -> None:
        self.flush_pending_frames()
        self.clear_previews()
        self.client.stop_stream()

    def resume_for_context(self) -> None:
        self.handle_selection()

    def _matching_objects(
        self, project_uuid: str, view_uuid: str,
    ) -> list[ImageObject]:
        chapter = self.canvas.chapter
        if chapter is None:
            return []
        return [
            obj for obj in chapter.objects.values()
            if isinstance(obj, ImageObject)
            and isinstance(obj.source, BlenderComicViewSourceDescriptor)
            and obj.source.project_uuid == project_uuid
            and obj.source.view_uuid == view_uuid
        ]

    def _frame_ready(
        self, project_uuid: str, view_uuid: str, revision: int,
        _sequence: int, frame_kind: str, image,
    ) -> None:
        objects = [
            obj for obj in self._matching_objects(project_uuid, view_uuid)
            if isinstance(obj.source, BlenderComicViewSourceDescriptor)
            and int(revision) >= obj.source.last_revision
        ]
        if not objects:
            return
        if frame_kind == "preview":
            # A preview must never displace a committed frame that has not yet
            # been encoded into the project's last-good PNG.
            self.flush_pending_frames()
        dirty = QRectF()
        for obj in objects:
            old_quad = self.canvas.object_world_quad(obj.object_id)
            old_bounds = (
                QPolygonF([QPointF(*point) for point in old_quad]).boundingRect()
                if old_quad else QRectF()
            )
            local_quad = self.canvas._image_model_local_quad(obj)
            adjusted = aspect_adjusted_quad(
                local_quad, (obj.pixel_width, obj.pixel_height),
                (image.width(), image.height()),
            )
            if frame_kind == "preview":
                self.canvas.set_image_runtime_geometry(
                    obj.object_id, image.width(), image.height(), adjusted,
                )
                self._preview_ids.add(obj.object_id)
            else:
                self.canvas.clear_image_runtime_geometry(obj.object_id)
                self._preview_ids.discard(obj.object_id)
                if obj.placement_mode == "free":
                    obj.transform_quad = adjusted
                obj.pixel_width, obj.pixel_height = image.width(), image.height()
                obj.transform_frame = (
                    0.0, 0.0, float(image.width()), float(image.height())
                )
            self.canvas.images.set_runtime_frame(obj.object_id, image)
            if (
                frame_kind == "committed"
                and isinstance(obj.source, BlenderComicViewSourceDescriptor)
            ):
                obj.source.last_revision = max(
                    obj.source.last_revision, int(revision)
                )
                view = self._views.get(view_uuid)
                if view is not None:
                    obj.source.display_name = view.name
            if frame_kind == "committed":
                self._pending_ids.add(obj.object_id)
            quad = self.canvas.object_world_quad(obj.object_id)
            if quad:
                bounds = QPolygonF([QPointF(*point) for point in quad]).boundingRect()
                bounds = bounds.united(old_bounds)
                bounds = self.canvas.modifier_expanded_dirty(
                    obj.object_id, bounds
                )
                dirty = bounds if dirty.isEmpty() else dirty.united(bounds)
        if not dirty.isEmpty():
            self.canvas._queue_visual_dirty(dirty)
        if frame_kind == "committed":
            self._idle_flush.start()
            if not self._maximum_flush.isActive():
                self._maximum_flush.start()

    def clear_previews(self) -> None:
        if not self._preview_ids:
            return
        dirty = QRectF()
        for object_id in list(self._preview_ids):
            before = self.canvas.object_world_quad(object_id)
            self.canvas.images.clear_runtime_frame(object_id)
            self.canvas.clear_image_runtime_geometry(object_id)
            after = self.canvas.object_world_quad(object_id)
            for quad in (before, after):
                if quad:
                    bounds = QPolygonF(
                        [QPointF(*point) for point in quad]
                    ).boundingRect()
                    dirty = bounds if dirty.isEmpty() else dirty.united(bounds)
        self._preview_ids.clear()
        if not dirty.isEmpty():
            self.canvas._queue_visual_dirty(dirty)

    def flush_pending_frames(self) -> bool:
        self._idle_flush.stop()
        self._maximum_flush.stop()
        if not self._pending_ids:
            return False
        chapter = self.canvas.chapter
        changed = False
        for object_id in list(self._pending_ids):
            obj = chapter.objects.get(object_id) if chapter is not None else None
            if not isinstance(obj, ImageObject) or not obj.is_blender_linked:
                self._pending_ids.discard(object_id)
                continue
            if self.canvas.images.persist_runtime_frame(
                object_id, "last-frame.png"
            ):
                changed = True
            self._pending_ids.discard(object_id)
        if changed:
            self.cachePersisted.emit()
        return changed

    def render_once(self) -> None:
        if self.selected_linked_object() is not None:
            self.flush_pending_frames()
            self.client.render_once()

    def reconnect_selected(self) -> None:
        self.handle_selection()
