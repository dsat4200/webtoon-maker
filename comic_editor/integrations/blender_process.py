"""Managed Blender process and minimal authenticated viewport-state IPC."""
from __future__ import annotations

import json
import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Callable, Iterable, Mapping

from PySide6.QtCore import QObject, QProcess, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer, QTcpSocket


BLENDER_EXECUTABLE_ENV = "BLENDER_EXECUTABLE"
STARTUP_TIMEOUT_MS = 20_000


def _version_key(path: Path) -> tuple[int, ...]:
    matches = re.findall(r"(?<!\d)(\d+)(?:\.(\d+))?(?:\.(\d+))?", str(path))
    if not matches:
        return (0,)
    major, minor, patch = matches[-1]
    return int(major), int(minor or 0), int(patch or 0)


def _default_search_roots(environment: Mapping[str, str]) -> list[Path]:
    result: list[Path] = []
    for key in ("ProgramFiles", "ProgramW6432", "LOCALAPPDATA"):
        value = environment.get(key)
        if value:
            result.append(Path(value))
    return result


def discover_blender_executables(
    roots: Iterable[Path] | None = None,
    environment: Mapping[str, str] | None = None,
) -> list[Path]:
    environment = environment or os.environ
    candidates: set[Path] = set()
    for root in roots or _default_search_roots(environment):
        patterns = (
            "Blender Foundation/Blender */blender.exe",
            "Programs/Blender Foundation/Blender */blender.exe",
            "Steam/steamapps/common/Blender/blender.exe",
        )
        for pattern in patterns:
            candidates.update(path.resolve() for path in root.glob(pattern))
    command = shutil.which("blender")
    if command:
        candidates.add(Path(command).resolve())
    return sorted(
        (path for path in candidates if path.is_file()),
        key=lambda path: (_version_key(path), str(path).casefold()),
        reverse=True,
    )


def resolve_blender_executable(
    environment: Mapping[str, str] | None = None,
    roots: Iterable[Path] | None = None,
) -> Path:
    environment = environment or os.environ
    override = str(environment.get(BLENDER_EXECUTABLE_ENV, "")).strip()
    if override:
        candidate = Path(override).expanduser()
        if not candidate.is_file():
            raise FileNotFoundError(
                f"{BLENDER_EXECUTABLE_ENV} does not point to blender.exe: "
                f"{candidate}"
            )
        return candidate.resolve()
    candidates = discover_blender_executables(roots, environment)
    if not candidates:
        raise FileNotFoundError(
            "Blender was not found. Install Blender or set BLENDER_EXECUTABLE."
        )
    return candidates[0]


class BlenderProcessManager(QObject):
    """Own one lazy Blender process and its local command connection."""

    stateChanged = Signal(str)
    ready = Signal(int)
    failed = Signal(str)
    stopped = Signal()
    viewStateChanged = Signal(object)
    responseReceived = Signal(int, bool, object)

    def __init__(self, parent: QObject | None = None):
        super().__init__(parent)
        self.state = "stopped"
        self.pid = 0
        self.executable: Path | None = None
        self._token = ""
        self._next_request_id = 1
        self._callbacks: dict[int, Callable[[bool, object], None]] = {}
        self._socket: QTcpSocket | None = None
        self._buffer = bytearray()
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._accept_connection)
        self._process = QProcess(self)
        self._process.finished.connect(self._process_finished)
        self._process.errorOccurred.connect(self._process_error)
        self._startup_timer = QTimer(self)
        self._startup_timer.setSingleShot(True)
        self._startup_timer.timeout.connect(self._startup_timed_out)

    @property
    def process(self) -> QProcess:
        return self._process

    def _set_state(self, state: str) -> None:
        if self.state == state:
            return
        self.state = state
        self.stateChanged.emit(state)

    def ensure_started(self) -> None:
        if self.state in {"starting", "ready"}:
            return
        try:
            self.executable = resolve_blender_executable()
        except OSError as error:
            self._fail(str(error))
            return
        self._close_transport()
        if not self._server.listen(QHostAddress.SpecialAddress.LocalHost, 0):
            self._fail(self._server.errorString())
            return
        self._token = secrets.token_hex(24)
        bootstrap = Path(__file__).with_name("blender_bootstrap.py")
        arguments = [
            "--python", str(bootstrap), "--",
            "--port", str(self._server.serverPort()),
            "--token", self._token,
        ]
        self._set_state("starting")
        self._process.setProgram(str(self.executable))
        self._process.setArguments(arguments)
        self._process.start()
        self._startup_timer.start(STARTUP_TIMEOUT_MS)

    def restart(self) -> None:
        self.stop(force=True)
        QTimer.singleShot(0, self.ensure_started)

    def request(
        self, command: str, payload: object | None = None,
        callback: Callable[[bool, object], None] | None = None,
    ) -> int:
        if self.state != "ready" or self._socket is None:
            if callback is not None:
                QTimer.singleShot(0, lambda: callback(False, "Blender is not ready"))
            return 0
        request_id = self._next_request_id
        self._next_request_id += 1
        if callback is not None:
            self._callbacks[request_id] = callback
        message = {
            "token": self._token,
            "id": request_id,
            "command": str(command),
            "payload": payload,
        }
        self._socket.write(
            json.dumps(message, separators=(",", ":")).encode("utf-8") + b"\n"
        )
        self._socket.flush()
        return request_id

    def stop(self, force: bool = False) -> None:
        self._startup_timer.stop()
        self._close_transport()
        if self._process.state() == QProcess.ProcessState.NotRunning:
            self.pid = 0
            self._set_state("stopped")
            return
        self._set_state("stopping")
        if force:
            self._process.kill()
            self._process.waitForFinished(2000)
            return
        self._process.terminate()
        if not self._process.waitForFinished(5000):
            self._process.kill()
            self._process.waitForFinished(2000)

    def _accept_connection(self) -> None:
        while self._server.hasPendingConnections():
            candidate = self._server.nextPendingConnection()
            if self._socket is not None:
                candidate.disconnectFromHost()
                candidate.deleteLater()
                continue
            self._socket = candidate
            self._socket.readyRead.connect(self._read_messages)
            self._socket.disconnected.connect(self._socket_disconnected)

    def _read_messages(self) -> None:
        if self._socket is None:
            return
        self._buffer.extend(bytes(self._socket.readAll()))
        while b"\n" in self._buffer:
            line, _, remainder = self._buffer.partition(b"\n")
            self._buffer = bytearray(remainder)
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            if message.get("token") != self._token:
                continue
            event = message.get("event")
            if event == "READY":
                self.pid = int(message.get("pid", 0))
                self._startup_timer.stop()
                self._server.close()
                self._set_state("ready")
                self.ready.emit(self.pid)
                continue
            if event == "VIEW_STATE_CHANGED":
                self.viewStateChanged.emit(message.get("payload"))
                continue
            if "id" not in message:
                continue
            request_id = int(message["id"])
            ok = bool(message.get("ok", False))
            payload = message.get("payload")
            callback = self._callbacks.pop(request_id, None)
            if callback is not None:
                callback(ok, payload)
            self.responseReceived.emit(request_id, ok, payload)

    def _socket_disconnected(self) -> None:
        if self.state not in {"stopped", "stopping"}:
            self._fail("Blender command connection closed")

    def _startup_timed_out(self) -> None:
        self._fail("Blender did not become ready within 20 seconds")
        if self._process.state() != QProcess.ProcessState.NotRunning:
            self._process.kill()

    def _process_error(self, _error) -> None:
        if self.state not in {"stopped", "stopping"}:
            self._fail(self._process.errorString())

    def _process_finished(self, _code: int, _status) -> None:
        was_expected = self.state in {"stopped", "stopping"}
        self.pid = 0
        self._startup_timer.stop()
        self._close_transport()
        self._set_state("stopped" if was_expected else "failed")
        if was_expected:
            self.stopped.emit()
        else:
            self.failed.emit("Blender exited")

    def _fail(self, message: str) -> None:
        self._startup_timer.stop()
        self._set_state("failed")
        self.failed.emit(str(message))

    def _close_transport(self) -> None:
        self._server.close()
        if self._socket is not None:
            self._socket.blockSignals(True)
            self._socket.disconnectFromHost()
            self._socket.deleteLater()
            self._socket = None
        self._buffer.clear()
        callbacks, self._callbacks = self._callbacks, {}
        for callback in callbacks.values():
            callback(False, "Blender connection closed")

