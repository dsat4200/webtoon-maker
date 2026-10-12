"""Application lifecycle for disk-published Blender ImageObjects."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QObject, QPointF, QRectF, QTimer, Qt, Signal
from PySide6.QtGui import QImageReader, QPolygonF

from comic_editor.core.images import ImageStore, PreparedDecodedImage
from comic_editor.core.models import BlenderComicViewSourceDescriptor, ImageObject
from comic_editor.integrations.blender_source import BlenderSourceClient, ComicViewInfo


MAX_FRAME_BYTES = 128 * 1024 * 1024


@dataclass(frozen=True)
class _FrameMetadata:
    project_uuid: str
    view_uuid: str
    name: str
    revision: int
    width: int
    height: int
    frame_path: str

    @classmethod
    def from_view(cls, view: ComicViewInfo):
        return cls(view.project_uuid, view.view_uuid, view.name, view.revision,
            view.width, view.height, view.frame_path)


@dataclass
class _Publication:
    view: _FrameMetadata
    generation: int
    chapter: object
    images: object
    objects: tuple
    future: object = None


def _publication_key(view):
    return view.project_uuid, view.view_uuid, view.revision, view.frame_path


def _publication_metadata(view):
    return *_publication_key(view), view.width, view.height, view.name


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


def load_published_png(view: ComicViewInfo | _FrameMetadata) -> tuple[bytes, object]:
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


def _prepare_published_frame(view: _FrameMetadata, encoded_cache) -> PreparedDecodedImage:
    raw, image = load_published_png(view)
    return ImageStore.prepare_decoded(raw, image, encoded_cache=encoded_cache)


class BlenderImageSourceController(QObject):
    viewsChanged = Signal(object)
    connectionStateChanged = Signal(str)
    providerInfoChanged = Signal(object)
    statusChanged = Signal(str)
    switchDecisionRequired = Signal(object)
    frameImported = Signal()
    errorOccurred = Signal(str)
    _frameFinished = Signal()

    def __init__(self, canvas, parent: QObject | None = None):
        super().__init__(parent)
        self.canvas = canvas
        self.client = BlenderSourceClient(self)
        self._views: dict[str, ComicViewInfo] = {}
        self._imported: set[tuple[str, str, int, str]] = set()
        self._failed_imports: set[tuple[str, str, int, str]] = set()
        # Only immutable publication metadata enters this one worker. Pending
        # views retain no image bytes; at most one decoded result awaits a safe
        # document-thread application while a received contact owns the scene.
        self._frame_executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="blender-published-png")
        self._pending_frames = OrderedDict()
        self._active_frame = None
        self._frame_generation = 0
        self._context_suspended = False
        self._frames_closed = False
        self._frame_timer = QTimer(self)
        self._frame_timer.setSingleShot(True)
        self._frame_timer.timeout.connect(self._advance_frames)
        self._frameFinished.connect(self._advance_frames, Qt.ConnectionType.QueuedConnection)
        canvas.interactionFinished.connect(self._advance_frames)
        executor = self._frame_executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))
        self.client.viewsChanged.connect(self._set_views)
        self.client.activeViewChanged.connect(self._active_view_changed)
        self.client.connectionStateChanged.connect(self._connection_changed)
        self.client.providerInfoChanged.connect(self.providerInfoChanged)
        self.client.switchDecisionRequired.connect(self.switchDecisionRequired)
        self.client.switchCanceled.connect(lambda: self._emit_selected_status("ready"))
        self.client.errorOccurred.connect(self._client_error)

    @property
    def views(self) -> list[ComicViewInfo]:
        return list(self._views.values())

    def connect_to_provider(self, host: str, port: int, token: str) -> None:
        self._retire_frames()
        self._views.clear()
        self._imported.clear()
        self._failed_imports.clear()
        self.viewsChanged.emit([])
        self.client.connect_to_provider(host, port, token)

    def disconnect(self) -> None:
        self._retire_frames()
        self._views.clear()
        self._imported.clear()
        self._failed_imports.clear()
        self.client.disconnect_from_provider()

    def shutdown(self) -> None:
        self.disconnect()
        self._frames_closed = True
        self._frame_timer.stop()
        self._frame_executor.shutdown(wait=False, cancel_futures=True)
        self._active_frame = None

    def _retire_frames(self):
        self._frame_generation += 1
        self._pending_frames.clear()
        self._frame_timer.stop()
        if self._active_frame is not None:
            self._active_frame.future.cancel()

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
        # Editor selection observes publications without changing Blender's scene.
        self._emit_selected_status("ready")

    def stop_for_context_change(self) -> None:
        self._context_suspended = True
        self._retire_frames()
        self._imported.clear()

    def resume_for_context(self) -> None:
        self._context_suspended = False
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
        if self._frames_closed or self._context_suspended:
            return
        for lane, publication in tuple(self._pending_frames.items()):
            if not self._frame_current(publication):
                self._pending_frames.pop(lane, None)
        for view in values:
            if not view.frame_path:
                continue
            objects = self._matching_objects(view.project_uuid, view.view_uuid)
            if not objects:
                continue
            key = _publication_key(view)
            needs_import = key not in self._imported and key not in self._failed_imports and any(
                isinstance(obj.source, BlenderComicViewSourceDescriptor)
                and view.revision >= obj.source.last_revision
                for obj in objects
            )
            if not needs_import:
                continue
            lane = view.project_uuid, view.view_uuid
            active = self._active_frame
            if (active is not None and self._frame_current(active)
                    and _publication_metadata(active.view) == _publication_metadata(view)):
                continue
            self._pending_frames[lane] = _Publication(_FrameMetadata.from_view(view), self._frame_generation,
                self.canvas.chapter, self.canvas.images, tuple((obj, obj.source) for obj in objects))
            self._pending_frames.move_to_end(lane)
            # One metadata record per linked view; newer revisions replace it.
            # The document's matching objects bound this queue, and only the
            # active worker owns decoded bytes while painting/read-only.
            self.statusChanged.emit("importing")
        self._advance_frames()

    def _frame_current(self, publication):
        view = self._views.get(publication.view.view_uuid)
        return (not self._frames_closed and not self._context_suspended
            and publication.generation == self._frame_generation
            and self.canvas.chapter is publication.chapter and self.canvas.images is publication.images
            and view is not None and _publication_metadata(view) == _publication_metadata(publication.view)
            and any(publication.chapter.objects.get(obj.object_id) is obj and obj.source is source
                and isinstance(source, BlenderComicViewSourceDescriptor)
                and source.project_uuid == view.project_uuid and source.view_uuid == view.view_uuid
                and view.revision >= source.last_revision for obj, source in publication.objects))

    def _frame_application_blocked(self):
        canvas = self.canvas
        received = getattr(canvas, "_has_received_painting_contact", None)
        predecessor = getattr(canvas, "_native_input_predecessor", None)
        return (getattr(canvas, "document_read_only", False)
            or getattr(canvas, "_drawing", False) or getattr(canvas, "_raster_contact_active", False)
            or getattr(canvas, "_lasso_brush", None) is not None
            or (callable(received) and received())
            or (callable(predecessor) and predecessor() is not None))

    def _advance_frames(self):
        if self._frames_closed:
            return
        publication = self._active_frame
        if publication is not None:
            if not publication.future.done():
                return  # Worker completion schedules a queued document-thread wakeup.
            if self._frame_current(publication) and self._frame_application_blocked():
                self._frame_timer.start(40)
                return  # Hold this one result; do not decode more queued views.
            self._active_frame = None
            if self._frame_current(publication):
                view, key = publication.view, _publication_key(publication.view)
                objects = [obj for obj, source in publication.objects
                    if publication.chapter.objects.get(obj.object_id) is obj and obj.source is source
                    and isinstance(source, BlenderComicViewSourceDescriptor)
                    and source.project_uuid == view.project_uuid and source.view_uuid == view.view_uuid
                    and view.revision >= source.last_revision]
                if objects:
                    try:
                        prepared = publication.future.result()
                        changed = self._apply_frame(view, objects, prepared)
                    except Exception as error:
                        self._failed_imports.add(key)
                        self.errorOccurred.emit(str(error))
                        self.statusChanged.emit("error")
                    else:
                        if changed:
                            self._imported.add(key)
                            self._failed_imports.discard(key)
                            self.frameImported.emit()
                            self._emit_selected_status("ready")
            # Stale successes and failures are detached from current UI/source
            # state; their native results are released on this completion edge.
        if self._context_suspended or self._frame_application_blocked():
            if self._pending_frames:
                self._frame_timer.start(40)
            return
        while self._pending_frames:
            _lane, publication = self._pending_frames.popitem(last=False)
            if not self._frame_current(publication):
                continue
            self._active_frame = publication
            publication.future = self._frame_executor.submit(_prepare_published_frame,
                publication.view, publication.images._encoded_cache)
            signal = self._frameFinished
            def completed(_future):
                try:
                    signal.emit()
                except RuntimeError:
                    pass  # Controller/context can retire before native decode returns.
            publication.future.add_done_callback(completed)
            break

    def _apply_frame(
        self, view: _FrameMetadata, objects: list[ImageObject], prepared: PreparedDecodedImage,
    ) -> bool:
        if getattr(self.canvas, "document_read_only", False):
            return False
        dirty = QRectF()
        changed = False
        image = prepared.native
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
            self.canvas.images.put_prepared_decoded(
                obj.object_id, "last-frame.png", prepared, "image/png"
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
        return changed

    def reconnect_selected(self) -> None:
        self._failed_imports.clear()
        self.client.refresh_views()
        self.handle_selection()
