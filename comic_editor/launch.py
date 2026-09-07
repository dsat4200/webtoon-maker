"""Per-user local file handoff to the running editor for this installation."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from PySide6.QtCore import QElapsedTimer, QLockFile, QObject, QStandardPaths, QThread, Signal
from PySide6.QtNetwork import QLocalServer, QLocalSocket


class FileLaunchBroker(QObject):
    files_requested = Signal(list)

    def __init__(self, parent=None, *, name: str | None = None):
        super().__init__(parent)
        identity = str(Path(__file__).resolve().parent) + str(Path.home())
        self.name = name or "webtoon-maker-" + hashlib.sha256(identity.encode()).hexdigest()[:24]
        lock_root = Path(QStandardPaths.writableLocation(QStandardPaths.TempLocation))
        self.lock = QLockFile(str(lock_root / f"{self.name}.lock"))
        self.lock.setStaleLockTime(0)
        self.server = QLocalServer(self)
        self.server.setSocketOptions(QLocalServer.UserAccessOption)
        self.server.newConnection.connect(self._accept)
        self.buffers: dict[QLocalSocket, bytearray] = {}

    def start(self, paths: list[str]) -> bool:
        """Return True for the owning editor, False after a successful handoff."""
        if not self.lock.tryLock(0):
            socket = QLocalSocket()
            timer = QElapsedTimer()
            timer.start()
            while timer.elapsed() < 5000:
                socket.connectToServer(self.name)
                if socket.waitForConnected(200):
                    break
                socket.abort()
                QThread.msleep(25)
            else:
                raise OSError("The running editor is not ready. Try opening the image again.")
            socket.write(json.dumps(paths).encode("utf-8") + b"\n")
            socket.flush()
            timer.restart()
            response = bytearray()
            while b"\n" not in response and timer.elapsed() < 15000:
                if socket.bytesAvailable() or socket.waitForReadyRead(15000 - timer.elapsed()):
                    response.extend(bytes(socket.readAll()))
                else:
                    break
            if response != b"OK\n":
                raise OSError("The running editor did not acknowledge the file. Try again when it responds.")
            socket.disconnectFromServer()
            return False
        QLocalServer.removeServer(self.name)
        if not self.server.listen(self.name):
            self.lock.unlock()
            raise OSError(self.server.errorString())
        return True

    def _accept(self) -> None:
        while self.server.hasPendingConnections():
            socket = self.server.nextPendingConnection()
            self.buffers[socket] = bytearray()
            socket.readyRead.connect(lambda s=socket: self._read(s))
            socket.disconnected.connect(lambda s=socket: self._release(s))
            self._read(socket)

    def _release(self, socket: QLocalSocket) -> None:
        self.buffers.pop(socket, None)
        socket.deleteLater()

    def _read(self, socket: QLocalSocket) -> None:
        buffer = self.buffers.get(socket)
        if buffer is None:
            return
        buffer.extend(bytes(socket.readAll()))
        if len(buffer) > 1024 * 1024:
            socket.disconnectFromServer()
            return
        if b"\n" not in buffer:
            return
        try:
            paths = json.loads(bytes(buffer).split(b"\n", 1)[0])
            if not isinstance(paths, list) or not all(
                isinstance(path, str) and Path(path).is_absolute() for path in paths
            ):
                raise ValueError("Invalid file request")
        except (ValueError, UnicodeError):
            socket.disconnectFromServer()
            return
        self.buffers.pop(socket, None)
        socket.write(b"OK\n")
        socket.flush()
        socket.disconnectFromServer()
        self.files_requested.emit(paths)

    def close(self) -> None:
        self.server.close()
        self.lock.unlock()
