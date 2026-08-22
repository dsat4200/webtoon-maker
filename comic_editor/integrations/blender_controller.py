"""Application lifecycle for disk-published Blender ImageObjects."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QPointF, QRectF, Signal
from PySide6.QtGui import QImageReader, QPolygonF

from comic_editor.core.models import BlenderComicViewSourceDescriptor, ImageObject
from comic_editor.integrations.blender_source import BlenderSourceClient, ComicViewInfo


MAX_FRAME_BYTES = 128 * 1024 * 1024


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


def load_published_png(view: ComicViewInfo) -> tuple[bytes, object]:
    """Read and validate one immutable published frame without side effects."""
    path = Path(str(view.frame_path or ""))
    if not path.is_absolute():
        raise ValueError("Blender published a non-absolute frame path")
    if path.suffix.lower() != ".png":
        raise ValueError("Blender published a frame that is not a PNG")
    try:
        size = path.stat().st_size
    except OSError as error:
        raise ValueError("The published Comic View frame is unavailable") from error
    if size <= 0 or size > MAX_FRAME_BYTES:
        raise ValueError("The published Comic View PNG has an invalid file size")
    try:
        raw = path.read_bytes()
    except OSError as error:
        raise ValueError("The published Comic View frame could not be read") from error
    if len(raw) != size or len(raw) > MAX_FRAME_BYTES:
        raise ValueError("The published Comic View PNG changed while being read")
    payload = QByteArray(raw)
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.ReadOnly)
    reader = QImageReader(buffer)
    try:
        if bytes(reader.format()).lower() != b"png":
            raise ValueError("The published Comic View frame is not a valid PNG")
        decoded_size = reader.size()
        if (decoded_size.width(), decoded_size.height()) != (view.width, view.height):
            raise ValueError("The published Comic View dimensions do not match its metadata")
        image = reader.read()
        if image.isNull():
            raise ValueError(reader.errorString() or "The published Comic View PNG is invalid")
    finally:
        buffer.close()
    if (image.width(), image.height()) != (view.width, view.height):
        raise ValueError("The decoded Comic View dimensions do not match its metadata")
    return raw, image


class BlenderImageSourceController(QObject):
    viewsChanged = Signal(object)
    connectionStateChanged = Signal(str)
    statusChanged = Signal(str)
    switchDecisionRequired = Signal(object)
    frameImported = Signal()
    errorOccurred = Signal(str)

    def __init__(self, canvas, parent: QObject | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self.client = BlenderSourceClient(self)
        self._views: dict[str, ComicViewInfo] = {}
        self._imported: set[tuple[str, str, int, str]] = set()
        self._failed_imports: set[tuple[str, str, int, str]] = set()
        self.client.viewsChanged.connect(self._set_views)
        self.client.activeViewChanged.connect(self._active_view_changed)
        self.client.connectionStateChanged.connect(self._connection_changed)
        self.client.switchDecisionRequired.connect(self.switchDecisionRequired)
        self.client.switchCanceled.connect(lambda: self._emit_selected_status("ready"))
        self.client.errorOccurred.connect(self._client_error)

    @property
    def views(self) -> list[ComicViewInfo]:
        return list(self._views.values())

    def connect_to_provider(self, host: str, port: int, token: str) -> None:
        self._views.clear()
        self._imported.clear()
        self._failed_imports.clear()
        self.viewsChanged.emit([])
        self.client.connect_to_provider(host, port, token)

    def disconnect(self) -> None:
        self._views.clear()
        self._imported.clear()
        self._failed_imports.clear()
        self.client.disconnect_from_provider()

    def shutdown(self) -> None:
        self.disconnect()

    def _connection_changed(self, state: str) -> None:
        if state != "connected":
            self.statusChanged.emit("offline" if state == "disconnected" else state)
        else:
            self.statusChanged.emit("connected")
        self.connectionStateChanged.emit(state)

    def _client_error(self, message: str) -> None:
        self.statusChanged.emit("error")
        self.errorOccurred.emit(message)

    def _set_views(self, values: list[ComicViewInfo]) -> None:
        self._views = {view.view_uuid: view for view in values}
        self.viewsChanged.emit(list(values))
        self._reconcile_views(values)
        self.handle_selection()

    def _active_view_changed(self, _message: dict[str, object]) -> None:
        self._emit_selected_status("ready")

    def selected_linked_object(self) -> ImageObject | None:
        chapter = self.canvas.chapter
        if chapter is None or self.canvas.selected_kind != "object":
            return None
        obj = chapter.objects.get(self.canvas.selected_id)
        return obj if isinstance(obj, ImageObject) and obj.is_blender_linked else None

    def _selected_view(self) -> ComicViewInfo | None:
        obj = self.selected_linked_object()
        source = obj.source if obj is not None else None
        if not isinstance(source, BlenderComicViewSourceDescriptor):
            return None
        view = self._views.get(source.view_uuid)
        return view if view is not None and view.project_uuid == source.project_uuid else None

    def _emit_selected_status(self, fallback: str) -> None:
        view = self._selected_view()
        if view is None:
            self.statusChanged.emit(fallback)
        elif not view.frame_path:
            self.statusChanged.emit("needs-render")
        elif view.dirty:
            self.statusChanged.emit("unsaved")
        else:
            self.statusChanged.emit(fallback)

    def handle_selection(self, *_args) -> None:
        obj = self.selected_linked_object()
        if obj is None:
            self.statusChanged.emit("connected" if self.client.connected else "offline")
            return
        if not self.client.connected:
            self.statusChanged.emit("offline")
            return
        view = self._selected_view()
        if view is None:
            self.statusChanged.emit("unavailable")
            return
        source = obj.source
        if (
            isinstance(source, BlenderComicViewSourceDescriptor)
            and view.revision < source.last_revision
        ):
            self.statusChanged.emit("stale")
            return
        self._reconcile_views([view])
        key = (view.project_uuid, view.view_uuid, view.revision, view.frame_path)
        if view.frame_path and key in self._failed_imports:
            self.statusChanged.emit("error")
            return
        sent = self.client.activate_view(view.view_uuid)
        if sent:
            self.statusChanged.emit("activating")
        else:
            self._emit_selected_status("ready")

    def stop_for_context_change(self) -> None:
        self._imported.clear()

    def resume_for_context(self) -> None:
        self._reconcile_views(self.views)
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

    def _reconcile_views(self, values: list[ComicViewInfo]) -> None:
        for view in values:
            if not view.frame_path:
                continue
            objects = self._matching_objects(view.project_uuid, view.view_uuid)
            if not objects:
                continue
            key = (view.project_uuid, view.view_uuid, view.revision, view.frame_path)
            needs_import = key not in self._imported and any(
                isinstance(obj.source, BlenderComicViewSourceDescriptor)
                and view.revision >= obj.source.last_revision
                for obj in objects
            )
            if not needs_import:
                continue
            self.statusChanged.emit("importing")
            try:
                raw, image = load_published_png(view)
                self._apply_frame(view, objects, raw, image)
            except (OSError, RuntimeError, ValueError) as error:
                self._failed_imports.add(key)
                self.errorOccurred.emit(str(error))
                self.statusChanged.emit("error")
                continue
            self._imported.add(key)
            self._failed_imports.discard(key)
            self.frameImported.emit()
            self._emit_selected_status("ready")

    def _apply_frame(
        self, view: ComicViewInfo, objects: list[ImageObject], raw: bytes, image,
    ) -> None:
        dirty = QRectF()
        changed = False
        for obj in objects:
            source = obj.source
            if (
                not isinstance(source, BlenderComicViewSourceDescriptor)
                or view.revision < source.last_revision
            ):
                continue
            old_quad = self.canvas.object_world_quad(obj.object_id)
            old_bounds = (
                QPolygonF([QPointF(*point) for point in old_quad]).boundingRect()
                if old_quad else QRectF()
            )
            local_quad = self.canvas._image_model_local_quad(obj)
            adjusted = aspect_adjusted_quad(
                local_quad,
                (obj.pixel_width, obj.pixel_height),
                (image.width(), image.height()),
            )
            if obj.placement_mode == "free":
                obj.transform_quad = adjusted
            obj.pixel_width, obj.pixel_height = image.width(), image.height()
            obj.transform_frame = (
                0.0, 0.0, float(image.width()), float(image.height())
            )
            self.canvas.images.put_decoded(
                obj.object_id, "last-frame.png", raw, image, "image/png"
            )
            source.last_revision = max(source.last_revision, view.revision)
            source.display_name = view.name
            obj.sync_source_metadata()
            quad = self.canvas.object_world_quad(obj.object_id)
            if quad:
                bounds = QPolygonF([QPointF(*point) for point in quad]).boundingRect()
                bounds = self.canvas.modifier_expanded_dirty(
                    obj.object_id, bounds.united(old_bounds)
                )
                dirty = bounds if dirty.isEmpty() else dirty.united(bounds)
            changed = True
        if changed and not dirty.isEmpty():
            self.canvas._queue_visual_dirty(dirty)

    def reconnect_selected(self) -> None:
        self.client.refresh_views()
        self.handle_selection()
