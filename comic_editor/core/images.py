"""Embedded, original-byte image resources keyed by document object ID."""
from __future__ import annotations

import mimetypes
import os
import re
import shutil
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader


@dataclass(frozen=True)
class ImageSource:
    filename: str
    mime_type: str
    data: bytes


class ImageStore:
    """Own immutable imported files while caching decoded display frames."""

    def __init__(self) -> None:
        self._sources: dict[str, ImageSource] = {}
        self._decoded: dict[str, QImage] = {}
        self._runtime: dict[str, QImage] = {}
        self.dirty: set[str] = set()

    @staticmethod
    def safe_filename(filename: str) -> str:
        name = Path(str(filename or "image")).name.strip() or "image"
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
        return name or "image"

    @staticmethod
    def _decode(data: bytes) -> tuple[QImage, bytes]:
        payload = QByteArray(data)
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()
        detected = bytes(reader.format())
        buffer.close()
        if image.isNull():
            raise ValueError(reader.errorString() or "Unsupported or invalid image")
        return image.convertToFormat(QImage.Format_ARGB32_Premultiplied), detected

    def put(
        self, object_id: str, filename: str, data: bytes,
        mime_type: str = "",
    ) -> ImageSource:
        raw = bytes(data)
        image, detected = self._decode(raw)
        safe = self.safe_filename(filename)
        guessed = mimetypes.guess_type(safe)[0] or ""
        if not mime_type:
            mime_type = guessed or (
                f"image/{detected.decode('ascii', 'ignore').lower()}"
                if detected else "application/octet-stream"
            )
        source = ImageSource(safe, str(mime_type), raw)
        self._sources[str(object_id)] = source
        self._decoded[str(object_id)] = image
        self.dirty.add(str(object_id))
        return source

    def source(self, object_id: str) -> ImageSource | None:
        return self._sources.get(str(object_id))

    def image(self, object_id: str) -> QImage:
        object_id = str(object_id)
        runtime = self._runtime.get(object_id)
        if runtime is not None:
            return QImage(runtime)
        cached = self._decoded.get(object_id)
        if cached is not None:
            return QImage(cached)
        source = self._sources.get(object_id)
        if source is None:
            return QImage()
        image, _detected = self._decode(source.data)
        self._decoded[object_id] = image
        return QImage(image)

    def set_runtime_frame(self, object_id: str, image: QImage) -> None:
        """Install a transient frame without changing persistent bytes."""
        object_id = str(object_id)
        if image.isNull():
            self._runtime.pop(object_id, None)
            return
        self._runtime[object_id] = image.convertToFormat(
            QImage.Format_ARGB32_Premultiplied
        )

    def runtime_frame(self, object_id: str) -> QImage:
        image = self._runtime.get(str(object_id))
        return QImage(image) if image is not None else QImage()

    def clear_runtime_frame(self, object_id: str) -> None:
        self._runtime.pop(str(object_id), None)

    def clear_runtime_frames(self) -> None:
        self._runtime.clear()

    def persist_runtime_frame(
        self, object_id: str, filename: str = "last-frame.png",
    ) -> bool:
        """Encode the newest transient frame as the last-good PNG cache."""
        object_id = str(object_id)
        image = self._runtime.get(object_id)
        if image is None or image.isNull():
            return False
        payload = QByteArray()
        buffer = QBuffer(payload)
        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
        saved = image.save(buffer, "PNG")
        buffer.close()
        if not saved:
            return False
        raw = bytes(payload)
        safe = self.safe_filename(filename or "last-frame.png")
        self._sources[object_id] = ImageSource(safe, "image/png", raw)
        self._decoded[object_id] = QImage(image)
        self.dirty.add(object_id)
        return True

    def relabel(
        self, object_id: str, filename: str,
        mime_type: str | None = None,
    ) -> bool:
        """Change persistent resource metadata without touching its bytes."""
        object_id = str(object_id)
        source = self._sources.get(object_id)
        if source is None:
            return False
        self._sources[object_id] = ImageSource(
            self.safe_filename(filename),
            str(mime_type or source.mime_type or "application/octet-stream"),
            source.data,
        )
        self.dirty.add(object_id)
        return True

    def remove(self, object_id: str) -> None:
        object_id = str(object_id)
        self._runtime.pop(object_id, None)
        if object_id in self._sources:
            self._sources.pop(object_id, None)
            self._decoded.pop(object_id, None)
            self.dirty.add(object_id)

    def copy_source_to(self, object_id: str, target: "ImageStore", new_id: str) -> None:
        source = self.source(object_id)
        if source is not None:
            target.put(new_id, source.filename, source.data, source.mime_type)

    def clone(self, object_ids: set[str] | None = None) -> "ImageStore":
        result = ImageStore()
        identifiers = set(self._sources) if object_ids is None else set(object_ids)
        for object_id in identifiers:
            source = self._sources.get(object_id)
            if source is not None:
                result._sources[object_id] = ImageSource(
                    source.filename, source.mime_type, bytes(source.data)
                )
        result.dirty.clear()
        return result

    def snapshot(self, object_ids: set[str] | None = None) -> dict[str, ImageSource]:
        identifiers = set(self._sources) if object_ids is None else set(object_ids)
        return {
            object_id: ImageSource(item.filename, item.mime_type, bytes(item.data))
            for object_id in identifiers
            if (item := self._sources.get(object_id)) is not None
        }

    def restore(self, values: dict[str, ImageSource]) -> None:
        self._sources = {
            object_id: ImageSource(item.filename, item.mime_type, bytes(item.data))
            for object_id, item in values.items()
        }
        self._decoded.clear()
        self._runtime.clear()
        self.dirty.update(values)

    @staticmethod
    def _atomic_bytes(path: Path, data: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        with temporary.open("wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        temporary.replace(path)

    def save_directory(
        self, root: Path, object_ids: set[str], *, complete: bool = False,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for object_id in object_ids:
            source = self._sources.get(object_id)
            if source is None:
                continue
            directory = root / object_id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / self.safe_filename(source.filename)
            if complete or object_id in self.dirty or not target.is_file():
                self._atomic_bytes(target, source.data)
            for stale in directory.iterdir():
                if stale != target:
                    if stale.is_dir():
                        shutil.rmtree(stale)
                    else:
                        stale.unlink(missing_ok=True)
        for directory in list(root.iterdir()):
            if directory.is_dir() and directory.name not in object_ids:
                shutil.rmtree(directory)
        self.dirty.difference_update(object_ids)

    def load_directory(
        self, root: Path, metadata: dict[str, tuple[str, str]],
    ) -> None:
        self._sources.clear()
        self._decoded.clear()
        self._runtime.clear()
        if not root.is_dir():
            return
        for object_id, (filename, mime_type) in metadata.items():
            directory = root / object_id
            requested = directory / self.safe_filename(filename)
            candidates = [requested] if requested.is_file() else (
                [path for path in directory.iterdir() if path.is_file()]
                if directory.is_dir() else []
            )
            if not candidates:
                continue
            data = candidates[0].read_bytes()
            self._decode(data)
            self._sources[object_id] = ImageSource(
                self.safe_filename(filename or candidates[0].name),
                mime_type or mimetypes.guess_type(candidates[0].name)[0]
                or "application/octet-stream",
                data,
            )
        self.dirty.clear()
