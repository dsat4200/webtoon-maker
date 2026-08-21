"""Authenticated loopback protocol and triple-buffered RGBA transport."""
from __future__ import annotations

import json
import queue
import socket
import struct
import threading
import time
import uuid
from multiprocessing import shared_memory
from typing import Any

import bpy

from . import renderer, viewport
from .state import (
    apply_state, capture_state, migrate_legacy_presentation, parse_state,
    state_digest, state_json, view_layer_for_state,
)


PROTOCOL_VERSION = 2
HEADER_SIZE = 256
SLOT_COUNT = 3
HEADER = struct.Struct("<8sIIIIII32s")
MAGIC = b"WCVRGBA\0"
MAX_CONTROL_MESSAGE = 4_194_304


class BridgeServer:
    """Small socket server. It never reads Blender data off the main thread."""

    def __init__(self, incoming: queue.Queue, outgoing: queue.Queue) -> None:
        self.incoming = incoming
        self.outgoing = outgoing
        self.host = "127.0.0.1"
        self.port = 47837
        self.token = ""
        self._server: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._client_lock = threading.Lock()
        self._client_active = False
        self._client_connection: socket.socket | None = None
        self._client_thread: threading.Thread | None = None
        self._client_sequence = 0

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stop.is_set()

    def start(self, port: int, token: str) -> None:
        self.stop()
        self.port = max(1024, min(65535, int(port)))
        self.token = str(token)
        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((self.host, self.port))
        server.listen(4)
        server.settimeout(0.2)
        self._server = server
        self._stop.clear()
        self._accept_thread = threading.Thread(
            target=self._accept_loop,
            name="WebtoonComicViewsServer",
            daemon=True,
        )
        self._accept_thread.start()

    def stop(self) -> None:
        self._stop.set()
        server, self._server = self._server, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread, self._accept_thread = self._accept_thread, None
        if thread is not None and thread.is_alive():
            thread.join(timeout=0.5)
        with self._client_lock:
            connection, self._client_connection = self._client_connection, None
            client_thread, self._client_thread = self._client_thread, None
            self._client_active = False
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass
        if client_thread is not None and client_thread.is_alive():
            client_thread.join(timeout=0.5)

    def _accept_loop(self) -> None:
        while not self._stop.is_set():
            server = self._server
            if server is None:
                return
            try:
                connection, _address = server.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            with self._client_lock:
                if self._client_active:
                    try:
                        connection.sendall(
                            b'{"type":"ERROR","code":"BUSY",'
                            b'"message":"A comic editor is already connected"}\n'
                        )
                    finally:
                        connection.close()
                    continue
                self._client_active = True
                self._client_connection = connection
                self._client_sequence += 1
                connection_id = self._client_sequence
            thread = threading.Thread(
                target=self._client_loop,
                args=(connection, connection_id),
                name="WebtoonComicViewsClient",
                daemon=True,
            )
            with self._client_lock:
                self._client_thread = thread
            thread.start()

    def _client_loop(self, connection: socket.socket, connection_id: int) -> None:
        connection.settimeout(0.05)
        buffer = bytearray()
        authorized = False
        try:
            while not self._stop.is_set():
                try:
                    chunk = connection.recv(65536)
                    if not chunk:
                        break
                    buffer.extend(chunk)
                except socket.timeout:
                    pass
                except OSError:
                    break
                if len(buffer) > MAX_CONTROL_MESSAGE and b"\n" not in buffer:
                    self._direct_send(connection, {
                        "type": "ERROR", "code": "MESSAGE_TOO_LARGE",
                        "message": "Control messages cannot exceed 4 MiB",
                    })
                    return
                while b"\n" in buffer:
                    raw, _, remainder = buffer.partition(b"\n")
                    buffer = bytearray(remainder)
                    if not raw.strip():
                        continue
                    if len(raw) > MAX_CONTROL_MESSAGE:
                        self._direct_send(connection, {
                            "type": "ERROR", "code": "MESSAGE_TOO_LARGE",
                            "message": "Control messages cannot exceed 4 MiB",
                        })
                        return
                    try:
                        message = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._direct_send(connection, {
                            "type": "ERROR", "code": "BAD_JSON",
                            "message": "Malformed JSON message",
                        })
                        continue
                    if not isinstance(message, dict):
                        self._direct_send(connection, {
                            "type": "ERROR", "code": "BAD_MESSAGE",
                            "message": "Control messages must be JSON objects",
                        })
                        continue
                    if not authorized:
                        try:
                            valid_protocol = (
                                int(message.get("protocol", 0))
                                == PROTOCOL_VERSION
                            )
                        except (TypeError, ValueError):
                            valid_protocol = False
                        if message.get("type") != "HELLO" or not valid_protocol \
                                or str(message.get("token", "")) != self.token:
                            self._direct_send(connection, {
                                "type": "ERROR", "code": "AUTHENTICATION_FAILED",
                                "message": "Invalid protocol or token",
                            })
                            return
                        while True:
                            try:
                                self.outgoing.get_nowait()
                            except queue.Empty:
                                break
                        authorized = True
                        self._direct_send(connection, {
                            "type": "HELLO", "protocol": PROTOCOL_VERSION,
                            "provider": "webtoon_comic_views",
                        })
                        self.incoming.put({
                            "type": "_CONNECTED",
                            "connection_id": connection_id,
                        })
                        continue
                    self.incoming.put(message)
                while authorized:
                    try:
                        outgoing = self.outgoing.get_nowait()
                    except queue.Empty:
                        break
                    if not self._direct_send(connection, outgoing):
                        return
        finally:
            try:
                connection.close()
            except OSError:
                pass
            with self._client_lock:
                if self._client_connection is connection:
                    self._client_active = False
                    self._client_connection = None
                    self._client_thread = None
            self.incoming.put({
                "type": "_DISCONNECTED",
                "connection_id": connection_id,
            })

    @staticmethod
    def _direct_send(connection: socket.socket, message: dict[str, Any]) -> bool:
        try:
            payload = json.dumps(
                message, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8") + b"\n"
            connection.sendall(payload)
            return True
        except (OSError, TypeError, ValueError):
            return False


class BridgeRuntime:
    def __init__(self) -> None:
        self.incoming: queue.Queue = queue.Queue()
        self.outgoing: queue.Queue = queue.Queue()
        self.server = BridgeServer(self.incoming, self.outgoing)
        self.connected = False
        self.connection_id = 0
        self.frame_dirty = False
        self.frame_kind = "committed"
        self.state_check_due = 0.0
        self.ignore_updates_until = 0.0
        self.scene_matches_snapshot = False
        self.active_stream_uuid = ""
        self.pending_switch: dict[str, Any] | None = None
        self._memory: shared_memory.SharedMemory | None = None
        self._slot_bytes = 0
        self._stream_width = 0
        self._stream_height = 0
        self._outstanding: dict[int, int] = {}
        self._sequence = 0
        self._pending_render: renderer.RenderFrame | None = None
        self.last_error = ""

    @property
    def streaming(self) -> bool:
        return self._memory is not None and bool(self.active_stream_uuid)

    def start_server(self, port: int, token: str) -> None:
        if not token:
            raise ValueError("A bridge token is required")
        self.stop_stream()
        self.server.start(port, token)
        self.last_error = ""

    def stop_server(self) -> None:
        self.stop_stream()
        self.server.stop()
        self.connected = False
        self.connection_id = 0

    def send(self, message: dict[str, Any]) -> None:
        if self.connected:
            self.outgoing.put(message)

    def mark_scene_dirty(self) -> None:
        now = time.monotonic()
        if now < self.ignore_updates_until:
            return
        self.scene_matches_snapshot = False
        self.state_check_due = now + 0.18

    def stop_stream(self) -> None:
        memory, self._memory = self._memory, None
        self.active_stream_uuid = ""
        self.frame_dirty = False
        self.frame_kind = "committed"
        self._slot_bytes = 0
        self._stream_width = 0
        self._stream_height = 0
        self._outstanding.clear()
        self._pending_render = None
        if memory is not None:
            try:
                memory.close()
            except (BufferError, OSError):
                pass
            try:
                memory.unlink()
            except (FileNotFoundError, OSError):
                pass

    @staticmethod
    def _scene() -> bpy.types.Scene | None:
        return getattr(bpy.context, "scene", None)

    @staticmethod
    def _settings(scene: bpy.types.Scene) -> object:
        return scene.webtoon_comic_settings

    @staticmethod
    def _views(scene: bpy.types.Scene) -> object:
        return scene.webtoon_comic_views

    def _view(self, scene: bpy.types.Scene, view_uuid: str) -> object | None:
        return next(
            (view for view in self._views(scene) if view.view_uuid == view_uuid),
            None,
        )

    def _active_view(self, scene: bpy.types.Scene) -> object | None:
        settings = self._settings(scene)
        views = self._views(scene)
        loaded_uuid = str(getattr(settings, "loaded_view_uuid", ""))
        if loaded_uuid:
            loaded = next(
                (view for view in views if view.view_uuid == loaded_uuid), None
            )
            if loaded is not None:
                return loaded
        index = int(settings.active_index)
        return views[index] if 0 <= index < len(views) else None

    def _select_view(self, scene: bpy.types.Scene, view: object) -> None:
        for index, candidate in enumerate(self._views(scene)):
            if candidate == view:
                self._settings(scene).active_index = index
                return

    def _view_payload(
        self, scene: bpy.types.Scene, view: object,
        *, include_thumbnail: bool = False,
    ) -> dict[str, Any]:
        result = {
            "project_uuid": self._settings(scene).project_uuid,
            "view_uuid": view.view_uuid,
            "name": view.name,
            "revision": int(view.revision),
            "width": int(view.published_width or view.width),
            "height": int(view.published_height or view.height),
            "dirty": bool(view.is_dirty),
        }
        if include_thumbnail:
            result["thumbnail_png"] = view.thumbnail_png
        return result

    def send_views(self, scene: bpy.types.Scene) -> None:
        self.send({
            "type": "VIEWS_CHANGED",
            "project_uuid": self._settings(scene).project_uuid,
            "views": [self._view_payload(scene, view) for view in self._views(scene)],
        })

    @staticmethod
    def _hide_overlays() -> bool:
        preferences = addon_preferences()
        return bool(
            preferences is not None
            and getattr(preferences, "always_hide_overlays", False)
        )

    def save_view_state(
        self, scene: bpy.types.Scene, view: object,
    ) -> list[str]:
        bounds = viewport.frame_bounds(view)
        width, height = viewport.derive_resolution(view.width, bounds, scene)
        viewport.set_working_resolution(view, width, height)
        captured, warnings = capture_state(
            scene, bpy.context.view_layer,
            stream_frame=bounds, output_resolution=(width, height),
        )
        serialized = state_json(captured)
        digest = state_digest(captured)
        if view.state_json and digest != view.state_hash:
            view.previous_state_json = view.state_json
            view.previous_state_hash = view.state_hash
        view.state_json = serialized
        view.state_hash = digest
        view.is_dirty = False
        view.updated_at = time.time()
        self.scene_matches_snapshot = True
        self.ignore_updates_until = time.monotonic() + 0.3
        self.state_check_due = 0.0
        self.send_views(scene)
        return warnings

    def capture_into_view(
        self, scene: bpy.types.Scene, view: object, *, thumbnail: bool = True,
    ) -> list[str]:
        """Compatibility alias for the new state-only Save operation."""
        del thumbnail
        return self.save_view_state(scene, view)

    def _apply_stored_view(
        self, scene: bpy.types.Scene, view: object,
    ) -> tuple[dict[str, Any], list[str]]:
        stored = parse_state(view.state_json)
        legacy_render_settings = None
        if stored.get("stream_frame_space") == "viewport_legacy":
            legacy_render_settings = {
                name: getattr(scene.render, name)
                for name in (
                    "resolution_x", "resolution_y",
                    "pixel_aspect_x", "pixel_aspect_y",
                )
            }
        view_layer = view_layer_for_state(
            scene, stored, getattr(bpy.context, "view_layer", None)
        )
        warnings = apply_state(scene, stored, view_layer)
        if stored.get("stream_frame_space") == "viewport_legacy":
            for name, value in legacy_render_settings.items():
                setattr(scene.render, name, value)
            stored, warning = migrate_legacy_presentation(scene, stored)
            view.state_json = state_json(stored)
            view.state_hash = state_digest(stored)
            if warning:
                warnings.append(warning)
        viewport.set_frame_bounds(view, stored.get("stream_frame"))
        resolution = stored.get("output_resolution", ())
        if isinstance(resolution, list) and len(resolution) == 2:
            viewport.set_working_resolution(
                view, *renderer.validate_resolution(*resolution)
            )
        return stored, warnings

    def load_view_state(
        self, scene: bpy.types.Scene, view: object,
    ) -> list[str]:
        if self.active_stream_uuid and self.active_stream_uuid != view.view_uuid:
            self.stop_stream()
        self.ignore_updates_until = time.monotonic() + 0.3
        _stored, warnings = self._apply_stored_view(scene, view)
        self._settings(scene).loaded_view_uuid = view.view_uuid
        view.is_dirty = False
        self.scene_matches_snapshot = True
        self.frame_dirty = False
        self.state_check_due = 0.0
        viewport.tag_redraw()
        self.send_views(scene)
        return warnings

    def revert_view_state(
        self, scene: bpy.types.Scene, view: object,
    ) -> list[str]:
        if not view.previous_state_json:
            raise ValueError("This Comic View has no previous save")
        current_json, current_hash = view.state_json, view.state_hash
        view.state_json = view.previous_state_json
        view.state_hash = view.previous_state_hash
        view.previous_state_json = current_json
        view.previous_state_hash = current_hash
        warnings = self.load_view_state(scene, view)
        view.updated_at = time.time()
        return warnings

    def _render_saved_snapshot(
        self, scene: bpy.types.Scene, view: object,
    ) -> tuple[renderer.RenderFrame, list[str]]:
        working_navigation = viewport.capture_viewport()
        working_bounds = viewport.frame_bounds(view)
        working_resolution = (int(view.width), int(view.height))
        working, _working_warnings = capture_state(
            scene, bpy.context.view_layer, repair_ids=False,
            stream_frame=working_bounds,
            output_resolution=working_resolution,
        )
        warnings: list[str] = []
        try:
            stored, warnings = self._apply_stored_view(scene, view)
            view_layer = view_layer_for_state(
                scene, stored, getattr(bpy.context, "view_layer", None)
            )
            width, height = renderer.validate_resolution(
                *stored["output_resolution"]
            )
            frame = renderer.render_active_camera(
                scene, view_layer, width, height,
                stream_frame=stored["stream_frame"],
                hide_overlays=self._hide_overlays(),
            )
        finally:
            restore_layer = view_layer_for_state(
                scene, working, getattr(bpy.context, "view_layer", None)
            )
            apply_state(scene, working, restore_layer)
            viewport.set_frame_bounds(view, working_bounds)
            viewport.set_working_resolution(view, *working_resolution)
            viewport.apply_viewport(working_navigation)
            self.ignore_updates_until = time.monotonic() + 0.3
        return frame, warnings

    def render_saved_view(
        self, scene: bpy.types.Scene, view: object,
    ) -> list[str]:
        frame, warnings = self._render_saved_snapshot(scene, view)
        renderer.update_thumbnail_image(
            view, renderer.thumbnail_from_frame(frame)
        )
        view.revision = max(1, int(view.revision) + 1)
        view.published_width, view.published_height = frame.width, frame.height
        view.updated_at = time.time()
        self.send_views(scene)
        if self.active_stream_uuid == view.view_uuid:
            self._open_stream(
                scene, view, frame.width, frame.height, "committed"
            )
            self._pending_render = frame
            self.frame_dirty = True
        return warnings

    def _activate(
        self, scene: bpy.types.Scene, view: object,
        request_id: object = None,
    ) -> None:
        self.stop_stream()
        self.ignore_updates_until = time.monotonic() + 0.3
        _stored_state, warnings = self._apply_stored_view(scene, view)
        self._select_view(scene, view)
        self._settings(scene).loaded_view_uuid = view.view_uuid
        view.is_dirty = False
        self.scene_matches_snapshot = True
        self.frame_dirty = False
        self.state_check_due = 0.0
        viewport.tag_redraw()
        self.send({
            "type": "ACTIVE_VIEW", "request_id": request_id,
            **self._view_payload(scene, view), "warnings": warnings,
        })

    def _request_activate(self, scene: bpy.types.Scene, message: dict[str, Any]) -> None:
        request_id = message.get("request_id")
        destination = self._view(scene, str(message.get("view_uuid", "")))
        if destination is None:
            self._error("VIEW_NOT_FOUND", "The requested Comic View no longer exists", request_id)
            return
        current = self._active_view(scene)
        if current is destination:
            self.send({
                "type": "ACTIVE_VIEW", "request_id": request_id,
                **self._view_payload(scene, destination), "warnings": [],
            })
            return
        if current is not None and current != destination and current.is_dirty:
            self.pending_switch = {
                "request_id": request_id,
                "destination_uuid": destination.view_uuid,
            }
            self.send({
                "type": "SWITCH_REQUIRES_DECISION",
                "request_id": request_id,
                "current_view_uuid": current.view_uuid,
                "current_name": current.name,
                "destination_view_uuid": destination.view_uuid,
                "destination_name": destination.name,
            })
            return
        self._activate(scene, destination, request_id)

    def _resolve_dirty(self, scene: bpy.types.Scene, message: dict[str, Any]) -> None:
        pending, self.pending_switch = self.pending_switch, None
        if pending is None:
            self._error("NO_PENDING_SWITCH", "No dirty Comic View switch is pending")
            return
        resolution = str(message.get("resolution", "cancel")).lower()
        if resolution == "cancel":
            self.send({
                "type": "SWITCH_CANCELED",
                "request_id": pending.get("request_id"),
            })
            return
        current = self._active_view(scene)
        if resolution in {"save", "update"} and current is not None:
            try:
                self.save_view_state(scene, current)
            except Exception as error:
                self._error("SAVE_FAILED", str(error), pending.get("request_id"))
                return
        elif resolution not in {"discard", "revert"}:
            self._error(
                "BAD_RESOLUTION", "Expected save, discard, or cancel"
            )
            return
        destination = self._view(scene, pending["destination_uuid"])
        if destination is None:
            self._error("VIEW_NOT_FOUND", "The destination Comic View was deleted")
            return
        self._activate(scene, destination, pending.get("request_id"))

    def _open_stream(
        self, scene: bpy.types.Scene, view: object, width: int, height: int,
        frame_kind: str,
    ) -> None:
        width, height = renderer.validate_resolution(width, height)
        if (
            self.streaming and self.active_stream_uuid == view.view_uuid
            and self._stream_width == width and self._stream_height == height
            and self.frame_kind == frame_kind
        ):
            return
        self.stop_stream()
        stride = width * 4
        self._slot_bytes = stride * height
        self._stream_width, self._stream_height = width, height
        total = HEADER_SIZE + SLOT_COUNT * self._slot_bytes
        memory = shared_memory.SharedMemory(create=True, size=total)
        self._memory = memory
        nonce = uuid.uuid4().hex.encode("ascii")
        HEADER.pack_into(
            memory.buf, 0, MAGIC, PROTOCOL_VERSION, width, height, stride,
            SLOT_COUNT, self._slot_bytes, nonce,
        )
        if HEADER.size < HEADER_SIZE:
            memory.buf[HEADER.size:HEADER_SIZE] = b"\0" * (HEADER_SIZE - HEADER.size)
        self.active_stream_uuid = view.view_uuid
        self.frame_kind = frame_kind
        self._outstanding.clear()
        self.send({
            "type": "STREAM_OPEN", "frame_kind": frame_kind,
            "project_uuid": self._settings(scene).project_uuid,
            "view_uuid": view.view_uuid,
            "revision": int(view.revision),
            "shared_memory": memory.name,
            "header_size": HEADER_SIZE,
            "width": width, "height": height, "stride": stride,
            "slot_count": SLOT_COUNT, "slot_bytes": self._slot_bytes,
            "pixel_format": "RGBA8_TOP_DOWN_STRAIGHT",
        })

    def _start_stream(self, scene: bpy.types.Scene, message: dict[str, Any]) -> None:
        view = self._view(scene, str(message.get("view_uuid", "")))
        if view is None or view != self._active_view(scene):
            self._error("VIEW_NOT_ACTIVE", "Activate the Comic View before streaming")
            return
        width = int(view.published_width or view.width)
        height = int(view.published_height or view.height)
        self._open_stream(scene, view, width, height, "committed")
        # Starting a stream always displays the saved revision.
        self.frame_dirty = True

    def request_committed_frame(self, scene: bpy.types.Scene, view: object) -> None:
        if self.active_stream_uuid != view.view_uuid:
            return
        self._open_stream(
            scene, view, int(view.published_width or view.width),
            int(view.published_height or view.height), "committed",
        )
        self.frame_dirty = True

    def request_preview_frame(self, scene: bpy.types.Scene) -> None:
        view = self._active_view(scene)
        if view is None or self.active_stream_uuid != view.view_uuid:
            self._error("VIEW_NOT_STREAMING", "Activate and stream a Comic View first")
            return
        width, height = renderer.validate_resolution(view.width, view.height)
        self._open_stream(scene, view, width, height, "preview")
        self.frame_dirty = True

    def _consume_frame(self, message: dict[str, Any]) -> None:
        try:
            slot = int(message.get("slot", -1))
            sequence = int(message.get("sequence", -1))
        except (TypeError, ValueError):
            return
        if self._outstanding.get(slot) == sequence:
            self._outstanding.pop(slot, None)

    def _render_if_needed(self, scene: bpy.types.Scene, now: float) -> None:
        if not self.streaming or not self.frame_dirty:
            return
        settings = self._settings(scene)
        free_slot = next(
            (slot for slot in range(SLOT_COUNT) if slot not in self._outstanding),
            None,
        )
        if free_slot is None:
            return
        view = self._view(scene, self.active_stream_uuid)
        if view is None:
            self.stop_stream()
            return
        try:
            if self._pending_render is not None:
                frame, self._pending_render = self._pending_render, None
                width, height = frame.width, frame.height
            elif self.frame_kind == "committed":
                frame, _warnings = self._render_saved_snapshot(scene, view)
                width, height = frame.width, frame.height
            else:
                bounds = viewport.frame_bounds(view)
                width, height = renderer.validate_resolution(view.width, view.height)
                frame = renderer.render_active_camera(
                    scene, bpy.context.view_layer, width, height,
                    stream_frame=bounds,
                    hide_overlays=self._hide_overlays(),
                )
            if len(frame.rgba) != self._slot_bytes:
                raise RuntimeError("The rendered frame size changed during streaming")
            memory = self._memory
            if memory is None:
                return
            offset = HEADER_SIZE + free_slot * self._slot_bytes
            memory.buf[offset:offset + self._slot_bytes] = frame.rgba
            self._sequence += 1
            sequence = self._sequence
            self._outstanding[free_slot] = sequence
            self.frame_dirty = False
            self.send({
                "type": "FRAME_READY", "frame_kind": self.frame_kind,
                "project_uuid": settings.project_uuid,
                "view_uuid": view.view_uuid,
                "revision": int(view.revision),
                "sequence": sequence,
                "slot": free_slot,
                "width": frame.width,
                "height": frame.height,
                "stride": frame.width * 4,
            })
        except Exception as error:
            self.frame_dirty = False
            self.last_error = str(error)
            self.send({
                "type": "ERROR", "code": "RENDER_FAILED",
                "message": str(error),
            })

    def _check_snapshot_dirty(self, scene: bpy.types.Scene, now: float) -> None:
        if not self.state_check_due or now < self.state_check_due:
            return
        self.state_check_due = 0.0
        view = self._active_view(scene)
        if view is None or not view.state_json:
            return
        try:
            captured, _warnings = capture_state(
                scene, bpy.context.view_layer, repair_ids=False,
                stream_frame=viewport.frame_bounds(view),
                output_resolution=(int(view.width), int(view.height)),
            )
            dirty = state_digest(captured) != view.state_hash
        except Exception:
            return
        if bool(view.is_dirty) != dirty:
            view.is_dirty = dirty
            self.send_views(scene)

    def _error(self, code: str, message: str, request_id: object = None) -> None:
        self.last_error = str(message)
        self.send({
            "type": "ERROR", "code": code, "message": str(message),
            "request_id": request_id,
        })

    def _handle(self, scene: bpy.types.Scene, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        if kind == "_CONNECTED":
            self.connected = True
            self.connection_id = int(message.get("connection_id", 0))
            self.send_views(scene)
        elif kind == "_DISCONNECTED":
            if int(message.get("connection_id", 0)) != self.connection_id:
                return
            self.connected = False
            self.connection_id = 0
            self.stop_stream()
        elif kind == "PING":
            self.send({"type": "PONG", "request_id": message.get("request_id")})
        elif kind == "GET_VIEWS":
            self.send_views(scene)
        elif kind == "GET_THUMBNAIL":
            view = self._view(scene, str(message.get("view_uuid", "")))
            if view is not None:
                self.send({
                    "type": "THUMBNAIL",
                    "project_uuid": self._settings(scene).project_uuid,
                    "view_uuid": view.view_uuid,
                    "revision": int(view.revision),
                    "thumbnail_png": view.thumbnail_png,
                })
        elif kind == "ACTIVATE_VIEW":
            self._request_activate(scene, message)
        elif kind == "RESOLVE_DIRTY":
            self._resolve_dirty(scene, message)
        elif kind == "START_STREAM":
            self._start_stream(scene, message)
        elif kind == "STOP_STREAM":
            self.stop_stream()
            self.send({"type": "STREAM_STATUS", "status": "stopped"})
        elif kind == "RENDER_ONCE":
            self.request_preview_frame(scene)
        elif kind == "FRAME_CONSUMED":
            self._consume_frame(message)
        else:
            self._error("UNKNOWN_MESSAGE", f"Unknown message type: {kind}")

    def tick(self) -> float:
        scene = self._scene()
        if scene is None:
            return 0.1
        for _index in range(64):
            try:
                message = self.incoming.get_nowait()
            except queue.Empty:
                break
            try:
                self._handle(scene, message)
            except Exception as error:
                self._error("COMMAND_FAILED", str(error), message.get("request_id"))
        now = time.monotonic()
        self._check_snapshot_dirty(scene, now)
        self._render_if_needed(scene, now)
        return 0.02


def addon_preferences() -> object | None:
    package = __package__
    addon = bpy.context.preferences.addons.get(package)
    return addon.preferences if addon is not None else None


RUNTIME = BridgeRuntime()
