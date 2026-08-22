"""Authenticated loopback control protocol and atomic PNG publication."""
from __future__ import annotations

import json
import os
import queue
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any

import bpy

from . import diagnostics, renderer, timeline, viewport
from .state import (
    apply_state, capture_state, migrate_legacy_presentation, parse_state,
    state_digest, state_json, view_layer_for_state,
)


PROTOCOL_VERSION = 3
MAX_CONTROL_MESSAGE = 4_194_304
FRAME_ROOT_PARTS = ("Webtoon Maker", "Comic View Frames")
FRAME_RETENTION = 2


def _canonical_uuid(value: object, label: str) -> str:
    try:
        return uuid.UUID(str(value)).hex
    except (AttributeError, TypeError, ValueError) as error:
        raise ValueError(f"{label} is not a valid UUID") from error


def published_frame_root() -> Path:
    """Return the stable Windows exchange directory shared with the editor."""
    override = os.environ.get("WEBTOON_COMIC_VIEW_FRAME_ROOT", "").strip()
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA", "").strip()
    if not local:
        raise RuntimeError("LOCALAPPDATA is unavailable; cannot publish Comic Views")
    return Path(local).joinpath(*FRAME_ROOT_PARTS)


def published_view_directory(project_uuid: object, view_uuid: object) -> Path:
    return published_frame_root() / _canonical_uuid(
        project_uuid, "Blender project UUID"
    ) / _canonical_uuid(view_uuid, "Comic View UUID")


def published_frame_path(
    project_uuid: object, view_uuid: object, revision: int,
) -> Path:
    return published_view_directory(project_uuid, view_uuid) / f"{int(revision)}.png"


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def _revision_for_path(path: Path) -> int:
    try:
        return int(path.stem) if path.suffix.lower() == ".png" else -1
    except ValueError:
        return -1


def prune_published_frames(directory: Path, *, keep: int = FRAME_RETENTION) -> None:
    try:
        candidates = sorted(
            (
                path for path in directory.iterdir()
                if path.is_file() and _revision_for_path(path) >= 0
            ),
            key=_revision_for_path,
            reverse=True,
        )
    except OSError:
        return
    for stale in candidates[max(1, int(keep)):]:
        try:
            stale.unlink(missing_ok=True)
        except OSError:
            pass


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
                    diagnostics.record(
                        "WARNING", "Bridge connection rejected because another editor is connected",
                    )
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
                        if message.get("type") != "HELLO":
                            self._direct_send(connection, {
                                "type": "ERROR", "code": "AUTHENTICATION_FAILED",
                                "message": "Expected an authenticated HELLO message",
                            })
                            return
                        if not valid_protocol:
                            diagnostics.record(
                                "WARNING", "Bridge protocol mismatch",
                                expected=PROTOCOL_VERSION,
                                received=message.get("protocol", "missing"),
                            )
                            self._direct_send(connection, {
                                "type": "ERROR", "code": "PROTOCOL_MISMATCH",
                                "protocol": PROTOCOL_VERSION,
                                "message": "Update Webtoon Maker or the Blender extension",
                            })
                            return
                        if str(message.get("token", "")) != self.token:
                            diagnostics.record(
                                "WARNING", "Bridge authentication failed",
                            )
                            self._direct_send(connection, {
                                "type": "ERROR", "code": "AUTHENTICATION_FAILED",
                                "message": "Invalid bridge token",
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
        self.state_check_due = 0.0
        self.ignore_updates_until = 0.0
        self.scene_matches_snapshot = False
        self.pending_switch: dict[str, Any] | None = None
        self.last_error = ""

    def start_server(self, port: int, token: str) -> None:
        if not token:
            raise ValueError("A bridge token is required")
        self.server.start(port, token)
        self.last_error = ""
        diagnostics.record("INFO", "Bridge listening", host="127.0.0.1", port=port)

    def stop_server(self) -> None:
        was_running = self.server.running
        self.server.stop()
        self.connected = False
        self.connection_id = 0
        if was_running:
            diagnostics.record("INFO", "Bridge stopped")

    def send(self, message: dict[str, Any]) -> None:
        if self.connected:
            self.outgoing.put(message)

    def mark_scene_dirty(self) -> None:
        now = time.monotonic()
        if now < self.ignore_updates_until:
            return
        self.scene_matches_snapshot = False
        self.state_check_due = now + 0.18

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
            "frame_path": str(getattr(view, "published_frame_path", "") or ""),
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
        transaction = timeline.prepare_bake(
            scene, list(self._views(scene)), view, captured
        )
        original_frame = int(scene.frame_current)
        original_subframe = float(scene.frame_subframe)
        old_metadata = (
            view.state_json, view.state_hash,
            view.previous_state_json, view.previous_state_hash,
            bool(view.is_dirty), float(view.updated_at),
        )
        try:
            scene.frame_set(transaction.target_frame)
            selected_layer = view_layer_for_state(
                scene, captured, getattr(bpy.context, "view_layer", None)
            )
            apply_warnings = apply_state(scene, captured, selected_layer)
            failures = timeline.verify_snapshot(scene, captured)
            if failures and transaction.cached:
                transaction.rollback()
                transaction = timeline.prepare_bake(
                    scene, list(self._views(scene)), view, captured, force=True
                )
                scene.frame_set(transaction.target_frame)
                apply_warnings = apply_state(scene, captured, selected_layer)
                failures = timeline.verify_snapshot(scene, captured)
            if failures:
                raise RuntimeError(
                    "Timeline bake did not restore: " + ", ".join(failures)
                )
            warnings.extend(apply_warnings)
            serialized = state_json(captured)
            digest = state_digest(captured)
            if view.state_json and digest != view.state_hash:
                view.previous_state_json = view.state_json
                view.previous_state_hash = view.state_hash
            view.state_json = serialized
            view.state_hash = digest
            view.is_dirty = False
            view.updated_at = time.time()
            transaction.commit()
        except Exception:
            transaction.rollback()
            (
                view.state_json, view.state_hash,
                view.previous_state_json, view.previous_state_hash,
                view.is_dirty, view.updated_at,
            ) = old_metadata
            try:
                scene.frame_set(original_frame, subframe=original_subframe)
                restore_layer = view_layer_for_state(
                    scene, captured, getattr(bpy.context, "view_layer", None)
                )
                apply_state(scene, captured, restore_layer)
            except Exception as restore_error:
                diagnostics.record_exception(
                    "Could not restore working state after Save failure",
                    restore_error,
                )
            raise
        self._settings(scene).loaded_view_uuid = view.view_uuid
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
        original_frame = int(scene.frame_current)
        original_subframe = float(scene.frame_subframe)
        working_bounds = viewport.frame_bounds(view)
        working_resolution = (int(view.width), int(view.height))
        old_state_json, old_state_hash = view.state_json, view.state_hash
        working, _working_warnings = capture_state(
            scene, bpy.context.view_layer, repair_ids=False,
            stream_frame=working_bounds,
            output_resolution=working_resolution,
        )
        transaction = timeline.prepare_bake(
            scene, list(self._views(scene)), view, stored
        )
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
        try:
            scene.frame_set(transaction.target_frame)
            warnings = apply_state(scene, stored, view_layer)
            failures = timeline.verify_snapshot(scene, stored)
            if failures and transaction.cached:
                transaction.rollback()
                transaction = timeline.prepare_bake(
                    scene, list(self._views(scene)), view, stored, force=True
                )
                scene.frame_set(transaction.target_frame)
                warnings = apply_state(scene, stored, view_layer)
                failures = timeline.verify_snapshot(scene, stored)
            if failures:
                raise RuntimeError(
                    "Comic View frame did not restore: " + ", ".join(failures)
                )
            if stored.get("stream_frame_space") == "viewport_legacy":
                for name, value in legacy_render_settings.items():
                    setattr(scene.render, name, value)
                stored, warning = migrate_legacy_presentation(scene, stored)
                view.state_json = state_json(stored)
                view.state_hash = state_digest(stored)
                transaction.update_bake_marker(view, stored)
                if warning:
                    warnings.append(warning)
            viewport.set_frame_bounds(view, stored.get("stream_frame"))
            resolution = stored.get("output_resolution", ())
            if isinstance(resolution, list) and len(resolution) == 2:
                viewport.set_working_resolution(
                    view, *renderer.validate_resolution(*resolution)
                )
            transaction.commit()
        except Exception:
            transaction.rollback()
            view.state_json, view.state_hash = old_state_json, old_state_hash
            try:
                scene.frame_set(original_frame, subframe=original_subframe)
                restore_layer = view_layer_for_state(
                    scene, working, getattr(bpy.context, "view_layer", None)
                )
                apply_state(scene, working, restore_layer)
                viewport.set_frame_bounds(view, working_bounds)
                viewport.set_working_resolution(view, *working_resolution)
            except Exception as restore_error:
                diagnostics.record_exception(
                    "Could not restore working state after Load failure",
                    restore_error,
                )
            raise
        return stored, warnings

    def load_view_state(
        self, scene: bpy.types.Scene, view: object,
    ) -> list[str]:
        self.ignore_updates_until = time.monotonic() + 0.3
        _stored, warnings = self._apply_stored_view(scene, view)
        self._settings(scene).loaded_view_uuid = view.view_uuid
        view.is_dirty = False
        self.scene_matches_snapshot = True
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
        replacement_json = view.previous_state_json
        replacement_hash = view.previous_state_hash
        replacement = parse_state(replacement_json)
        original_frame = int(scene.frame_current)
        original_subframe = float(scene.frame_subframe)
        working, _working_warnings = capture_state(
            scene, bpy.context.view_layer, repair_ids=False,
            stream_frame=viewport.frame_bounds(view),
            output_resolution=(int(view.width), int(view.height)),
        )
        transaction = timeline.prepare_bake(
            scene, list(self._views(scene)), view, replacement
        )
        try:
            scene.frame_set(transaction.target_frame)
            view_layer = view_layer_for_state(
                scene, replacement, getattr(bpy.context, "view_layer", None)
            )
            warnings = apply_state(scene, replacement, view_layer)
            failures = timeline.verify_snapshot(scene, replacement)
            if failures:
                raise RuntimeError(
                    "Reverted frame did not restore: " + ", ".join(failures)
                )
            view.state_json = replacement_json
            view.state_hash = replacement_hash
            view.previous_state_json = current_json
            view.previous_state_hash = current_hash
            view.updated_at = time.time()
            transaction.commit()
        except Exception:
            transaction.rollback()
            view.state_json, view.state_hash = current_json, current_hash
            view.previous_state_json = replacement_json
            view.previous_state_hash = replacement_hash
            try:
                scene.frame_set(original_frame, subframe=original_subframe)
                restore_layer = view_layer_for_state(
                    scene, working, getattr(bpy.context, "view_layer", None)
                )
                apply_state(scene, working, restore_layer)
            except Exception as restore_error:
                diagnostics.record_exception(
                    "Could not restore working state after Revert failure",
                    restore_error,
                )
            raise
        self._settings(scene).loaded_view_uuid = view.view_uuid
        view.is_dirty = False
        self.scene_matches_snapshot = True
        self.state_check_due = 0.0
        self.send_views(scene)
        return warnings

    def _render_saved_snapshot(
        self, scene: bpy.types.Scene, view: object,
    ) -> tuple[renderer.RenderFrame, list[str]]:
        working_navigation = viewport.capture_viewport()
        working_frame = int(scene.frame_current)
        working_subframe = float(scene.frame_subframe)
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
            scene.frame_set(working_frame, subframe=working_subframe)
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
        diagnostics.record(
            "INFO", "Render started", view=getattr(view, "name", ""),
            view_uuid=getattr(view, "view_uuid", ""),
            current_revision=int(getattr(view, "revision", 0)),
        )
        frame, warnings = self._render_saved_snapshot(scene, view)
        next_revision = max(1, int(view.revision) + 1)
        project_uuid = self._settings(scene).project_uuid
        destination = published_frame_path(
            project_uuid, view.view_uuid, next_revision
        )
        encoded = renderer.png_bytes(frame)
        _atomic_write(destination, encoded)
        renderer.update_thumbnail_image(view, renderer.thumbnail_from_frame(frame))
        view.revision = next_revision
        view.published_width, view.published_height = frame.width, frame.height
        view.published_frame_path = str(destination)
        view.updated_at = time.time()
        prune_published_frames(destination.parent)
        self.send_views(scene)
        self.last_error = ""
        diagnostics.record(
            "INFO", "Render published", view=getattr(view, "name", ""),
            revision=next_revision, width=frame.width, height=frame.height,
            path=destination, png_bytes=len(encoded),
        )
        return warnings

    def duplicate_published_frame(
        self, scene: bpy.types.Scene, source: object, target: object,
    ) -> None:
        """Copy a duplicate view's last render into its own managed directory."""
        source_path = Path(str(getattr(source, "published_frame_path", "") or ""))
        expected_directory = published_view_directory(
            self._settings(scene).project_uuid, source.view_uuid
        ).resolve()
        try:
            resolved_source = source_path.resolve(strict=True)
        except OSError:
            resolved_source = Path()
        if (
            not source_path.is_absolute()
            or resolved_source.parent != expected_directory
            or resolved_source.suffix.lower() != ".png"
            or not resolved_source.is_file()
        ):
            target.published_frame_path = ""
            return
        destination = published_frame_path(
            self._settings(scene).project_uuid,
            target.view_uuid,
            int(target.revision),
        )
        _atomic_write(destination, resolved_source.read_bytes())
        target.published_frame_path = str(destination)
        prune_published_frames(destination.parent)
        diagnostics.record(
            "INFO", "Published frame duplicated",
            source_view_uuid=getattr(source, "view_uuid", ""),
            target_view_uuid=getattr(target, "view_uuid", ""),
            path=destination,
        )

    def delete_published_frames(
        self, scene: bpy.types.Scene, view_uuid: str,
    ) -> None:
        """Best-effort cleanup constrained to one managed Comic View folder."""
        try:
            directory = published_view_directory(
                self._settings(scene).project_uuid, view_uuid
            )
            root = published_frame_root().resolve()
            resolved = directory.resolve()
            if resolved.parent.parent != root:
                return
            if not resolved.is_dir():
                return
            for child in resolved.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            resolved.rmdir()
            diagnostics.record(
                "INFO", "Published frames deleted", view_uuid=view_uuid,
                directory=resolved,
            )
        except (OSError, RuntimeError, ValueError):
            pass

    def _activate(
        self, scene: bpy.types.Scene, view: object,
        request_id: object = None,
    ) -> None:
        self.ignore_updates_until = time.monotonic() + 0.3
        _stored_state, warnings = self._apply_stored_view(scene, view)
        self._select_view(scene, view)
        self._settings(scene).loaded_view_uuid = view.view_uuid
        view.is_dirty = False
        self.scene_matches_snapshot = True
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
        diagnostics.record("ERROR", "Bridge command failed", code=code, message=message)
        self.send({
            "type": "ERROR", "code": code, "message": str(message),
            "request_id": request_id,
        })

    def _handle(self, scene: bpy.types.Scene, message: dict[str, Any]) -> None:
        kind = str(message.get("type", ""))
        if kind == "_CONNECTED":
            self.connected = True
            self.connection_id = int(message.get("connection_id", 0))
            diagnostics.record(
                "INFO", "Webtoon Maker connected",
                connection_id=self.connection_id,
            )
            self.send_views(scene)
        elif kind == "_DISCONNECTED":
            if int(message.get("connection_id", 0)) != self.connection_id:
                return
            self.connected = False
            self.connection_id = 0
            diagnostics.record("INFO", "Webtoon Maker disconnected")
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
                diagnostics.record_exception(
                    "Unhandled bridge command exception", error,
                    message_type=message.get("type", ""),
                )
                self._error("COMMAND_FAILED", str(error), message.get("request_id"))
        now = time.monotonic()
        self._check_snapshot_dirty(scene, now)
        return 0.02


def addon_preferences() -> object | None:
    package = __package__
    addon = bpy.context.preferences.addons.get(package)
    return addon.preferences if addon is not None else None


RUNTIME = BridgeRuntime()
