"""Client for Blender Comic Views control protocol version 3."""
from __future__ import annotations

import base64
import binascii
import json
import uuid
from dataclasses import dataclass

from PySide6.QtCore import QByteArray, QObject, QTimer, Signal
from PySide6.QtGui import QImage
from PySide6.QtNetwork import QAbstractSocket, QTcpSocket


PROTOCOL_VERSION = 3
MAX_AXIS = 4096
MAX_PIXELS = 16_777_216
MAX_CONTROL_MESSAGE = 4_194_304
MAX_THUMBNAIL_BYTES = 1_048_576
BLENDER_52_EXTENSION_VERSION = "0.6.0"


def canonical_uuid(value: object) -> str:
    try:
        return uuid.UUID(str(value)).hex
    except (ValueError, AttributeError, TypeError) as error:
        raise ValueError("Expected a valid UUID") from error


@dataclass(frozen=True)
class BlenderProviderInfo:
    extension_version: str = ""
    blender_version: str = ""
    capabilities: tuple[str, ...] = ()

    @classmethod
    def from_message(cls, message: dict[str, object]) -> "BlenderProviderInfo":
        def optional_text(key: str) -> str:
            value = message.get(key, "")
            return value.strip()[:120] if isinstance(value, str) else ""

        capabilities = message.get("capabilities", [])
        return cls(
            optional_text("extension_version"), optional_text("blender_version"),
            tuple(
                value for value in capabilities
                if isinstance(value, str) and len(value) <= 120
            ) if isinstance(capabilities, list) else (),
        )


@dataclass(frozen=True)
class ComicViewInfo:
    project_uuid: str
    view_uuid: str
    name: str
    revision: int
    width: int
    height: int
    dirty: bool
    thumbnail: QImage
    frame_path: str = ""

    @classmethod
    def from_message(
        cls, project_uuid: object, value: object,
    ) -> "ComicViewInfo":
        if not isinstance(value, dict):
            raise ValueError("Comic View metadata must be an object")
        width, height = int(value.get("width", 0)), int(value.get("height", 0))
        if (
            not 64 <= width <= MAX_AXIS or not 64 <= height <= MAX_AXIS
            or width * height > MAX_PIXELS
        ):
            raise ValueError("Comic View metadata contains an invalid resolution")
        thumbnail = QImage()
        encoded = str(value.get("thumbnail_png", ""))
        if encoded:
            try:
                thumbnail.loadFromData(base64.b64decode(encoded), "PNG")
            except (binascii.Error, ValueError, TypeError):
                thumbnail = QImage()
        return cls(
            canonical_uuid(project_uuid),
            canonical_uuid(value.get("view_uuid", "")),
            str(value.get("name", "Comic View")),
            max(0, int(value.get("revision", 0))),
            width,
            height,
            bool(value.get("dirty", False)),
            thumbnail,
            str(value.get("frame_path", "") or ""),
        )


class BlenderSourceClient(QObject):
    connectionStateChanged = Signal(str)
    providerInfoChanged = Signal(object)
    viewsChanged = Signal(object)
    activeViewChanged = Signal(object)
    switchDecisionRequired = Signal(object)
    switchCanceled = Signal()
    errorOccurred = Signal(str)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.socket = QTcpSocket(self)
        self.socket.connected.connect(self._socket_connected)
        self.socket.disconnected.connect(self._socket_disconnected)
        self.socket.readyRead.connect(self._read_messages)
        self.socket.errorOccurred.connect(self._socket_error)
        self._buffer = QByteArray()
        self._token = ""
        self._authorized = False
        self._state = "disconnected"
        self._provider_info: BlenderProviderInfo | None = None
        self._views: list[ComicViewInfo] = []
        self._requested_view_uuid = ""
        self._activation_request_id: int | None = None
        self._active_project_uuid = ""
        self._active_view_uuid = ""
        self._request_sequence = 0
        self._timeout = QTimer(self)
        self._timeout.setSingleShot(True)
        self._timeout.timeout.connect(self._connection_timeout)

    @property
    def state(self) -> str:
        return self._state

    @property
    def connected(self) -> bool:
        return self._authorized and self.socket.state() == QAbstractSocket.ConnectedState

    @property
    def views(self) -> list[ComicViewInfo]:
        return list(self._views)

    @property
    def provider_info(self) -> BlenderProviderInfo | None:
        return self._provider_info

    def _clear_provider_info(self) -> None:
        if self._provider_info is not None:
            self._provider_info = None
            self.providerInfoChanged.emit(None)

    def _set_state(self, value: str) -> None:
        if value == self._state:
            return
        self._state = value
        self.connectionStateChanged.emit(value)

    def connect_to_provider(self, host: str, port: int, token: str) -> None:
        self.disconnect_from_provider()
        host = str(host or "127.0.0.1").strip()
        if host not in {"127.0.0.1", "localhost"}:
            self.errorOccurred.emit("The Blender bridge must use the loopback host")
            return
        self._token = str(token or "").strip()
        if not self._token:
            self.errorOccurred.emit("Enter the token shown by the Blender extension")
            return
        self._authorized = False
        self._set_state("connecting")
        self.socket.connectToHost(host, max(1024, min(65535, int(port))))
        self._timeout.start(5000)

    def disconnect_from_provider(self) -> None:
        self._timeout.stop()
        self._authorized = False
        self._requested_view_uuid = ""
        self._activation_request_id = None
        self._active_project_uuid = self._active_view_uuid = ""
        self._clear_provider_info()
        self._buffer.clear()
        self.socket.abort()
        self._set_state("disconnected")

    def _socket_connected(self) -> None:
        self._send({
            "type": "HELLO", "protocol": PROTOCOL_VERSION,
            "token": self._token,
        }, require_authorized=False)

    def _socket_disconnected(self) -> None:
        self._timeout.stop()
        self._authorized = False
        self._requested_view_uuid = ""
        self._activation_request_id = None
        self._active_project_uuid = self._active_view_uuid = ""
        self._clear_provider_info()
        self._set_state("disconnected")

    def _fail_connection(self, message: str) -> None:
        # Disconnect first so its status update cannot hide upgrade guidance.
        self.disconnect_from_provider()
        self._set_state("error")
        self.errorOccurred.emit(message)

    def _socket_error(self, _error: object) -> None:
        if self.socket.error() == QAbstractSocket.RemoteHostClosedError:
            return
        self.errorOccurred.emit(self.socket.errorString())
        if not self.connected:
            self._set_state("error")

    def _connection_timeout(self) -> None:
        if not self.connected:
            self.socket.abort()
            self._set_state("error")
            self.errorOccurred.emit("Timed out connecting to the Blender bridge")

    def _send(
        self, message: dict[str, object], *, require_authorized: bool = True,
    ) -> bool:
        if require_authorized and not self.connected:
            return False
        if self.socket.state() != QAbstractSocket.ConnectedState:
            return False
        try:
            raw = json.dumps(
                message, separators=(",", ":"), ensure_ascii=False
            ).encode("utf-8") + b"\n"
        except (TypeError, ValueError):
            return False
        return self.socket.write(raw) == len(raw)

    def _next_request(self) -> int:
        self._request_sequence += 1
        return self._request_sequence

    def refresh_views(self) -> None:
        self._send({"type": "GET_VIEWS", "request_id": self._next_request()})

    def activate_view(self, view_uuid: str) -> bool:
        try:
            wanted = canonical_uuid(view_uuid)
        except ValueError as error:
            self.errorOccurred.emit(str(error))
            return False
        if wanted in {self._requested_view_uuid, self._active_view_uuid}:
            return False
        request_id = self._next_request()
        sent = self._send({
            "type": "ACTIVATE_VIEW", "view_uuid": wanted,
            "request_id": request_id,
        })
        if sent:
            self._requested_view_uuid = wanted
            self._activation_request_id = request_id
        return sent

    def resolve_dirty_switch(self, resolution: str) -> None:
        resolution = str(resolution).lower()
        resolution = {"update": "save", "revert": "discard"}.get(
            resolution, resolution
        )
        if resolution not in {"save", "discard", "cancel"}:
            raise ValueError("Expected save, discard, or cancel")
        self._send({
            "type": "RESOLVE_DIRTY", "resolution": resolution,
            "request_id": self._next_request(),
        })

    def _read_messages(self) -> None:
        self._buffer.append(self.socket.readAll())
        if self._buffer.size() > MAX_CONTROL_MESSAGE and self._buffer.indexOf(b"\n") < 0:
            self.errorOccurred.emit("Blender sent an oversized control message")
            self.disconnect_from_provider()
            return
        while True:
            index = self._buffer.indexOf(b"\n")
            if index < 0:
                break
            raw = bytes(self._buffer.left(index))
            self._buffer.remove(0, index + 1)
            if not raw.strip():
                continue
            if len(raw) > MAX_CONTROL_MESSAGE:
                self.errorOccurred.emit("Blender sent an oversized control message")
                self.disconnect_from_provider()
                return
            try:
                message = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                self.errorOccurred.emit("Blender sent malformed JSON")
                continue
            if isinstance(message, dict):
                self._handle(message)

    def _handle(self, message: dict[str, object]) -> None:
        kind = str(message.get("type", ""))
        if kind == "HELLO":
            try:
                protocol_matches = int(message.get("protocol", 0)) == PROTOCOL_VERSION
            except (TypeError, ValueError, OverflowError):
                protocol_matches = False
            if not protocol_matches:
                self._fail_connection(
                    "Blender uses a different Comic Views protocol; update the editor and extension"
                )
                return
            self._authorized = True
            self._timeout.stop()
            self._set_state("connected")
            self._provider_info = BlenderProviderInfo.from_message(message)
            self.providerInfoChanged.emit(self._provider_info)
            self.refresh_views()
        elif kind == "VIEWS_CHANGED":
            try:
                project_uuid = canonical_uuid(message.get("project_uuid", ""))
                values = message.get("views", [])
                if not isinstance(values, list):
                    raise ValueError("Comic View list is invalid")
                views = [ComicViewInfo.from_message(project_uuid, item) for item in values]
                if len({view.view_uuid for view in views}) != len(views):
                    raise ValueError("Blender sent duplicate Comic View UUIDs")
            except (TypeError, ValueError) as error:
                self.errorOccurred.emit(str(error))
                return
            self._views = views
            self.viewsChanged.emit(list(views))
            for view in views:
                self._send({
                    "type": "GET_THUMBNAIL", "view_uuid": view.view_uuid,
                    "request_id": self._next_request(),
                })
        elif kind == "THUMBNAIL":
            self._update_thumbnail(message)
        elif kind == "ACTIVE_VIEW":
            try:
                self._active_project_uuid = canonical_uuid(message.get("project_uuid", ""))
                self._active_view_uuid = canonical_uuid(message.get("view_uuid", ""))
            except (TypeError, ValueError) as error:
                self.errorOccurred.emit(str(error))
                return
            if message.get("request_id") in (None, self._activation_request_id):
                self._requested_view_uuid = self._active_view_uuid
                self._activation_request_id = None
            self.activeViewChanged.emit(dict(message))
        elif kind == "SWITCH_REQUIRES_DECISION":
            self.switchDecisionRequired.emit(dict(message))
        elif kind == "SWITCH_CANCELED":
            self._requested_view_uuid = self._active_view_uuid
            self._activation_request_id = None
            self.switchCanceled.emit()
        elif kind == "ERROR":
            code = str(message.get("code", "ERROR"))
            value = str(message.get("message", "Blender bridge error"))
            if code == "PROTOCOL_MISMATCH":
                self._fail_connection(
                    "Comic Views protocol mismatch; update Webtoon Maker and the Blender extension"
                )
            elif code == "AUTHENTICATION_FAILED":
                self._fail_connection(f"{code}: {value}")
            else:
                if self._activation_request_id is not None and (
                    message.get("request_id") == self._activation_request_id
                    or code in {"SAVE_FAILED", "NO_PENDING_SWITCH", "BAD_RESOLUTION"}
                ):
                    self._requested_view_uuid = self._active_view_uuid
                    self._activation_request_id = None
                if "'Action' object has no attribute 'fcurves'" in value:
                    value = (
                        "This Blender extension uses an animation API removed in Blender 5. "
                        f"Install Webtoon Comic Views {BLENDER_52_EXTENSION_VERSION} or later "
                        "in Blender Preferences > Add-ons, restart Blender, and reconnect. "
                        f"Details: {value}"
                    )
                self.errorOccurred.emit(f"{code}: {value}")
        elif kind == "PONG":
            return

    def _update_thumbnail(self, message: dict[str, object]) -> None:
        try:
            view_uuid = canonical_uuid(message.get("view_uuid", ""))
            matching = next(
                (view for view in self._views if view.view_uuid == view_uuid), None
            )
            if matching is None:
                return
            if canonical_uuid(message.get("project_uuid", "")) != matching.project_uuid:
                raise ValueError("Comic View thumbnail project does not match")
            if int(message.get("revision", -1)) != matching.revision:
                return
            encoded = str(message.get("thumbnail_png", ""))
            raw = base64.b64decode(encoded, validate=True) if encoded else b""
            if len(raw) > MAX_THUMBNAIL_BYTES:
                raise ValueError("Blender sent an oversized Comic View thumbnail")
            image = QImage()
            if raw and not image.loadFromData(raw, "PNG"):
                raise ValueError("Blender sent an invalid Comic View thumbnail")
        except (binascii.Error, TypeError, ValueError) as error:
            self.errorOccurred.emit(str(error))
            return
        changed = False
        values: list[ComicViewInfo] = []
        for view in self._views:
            if view.view_uuid == view_uuid:
                view = ComicViewInfo(
                    view.project_uuid, view.view_uuid, view.name, view.revision,
                    view.width, view.height, view.dirty, image, view.frame_path,
                )
                changed = True
            values.append(view)
        if changed:
            self._views = values
            self.viewsChanged.emit(list(values))
