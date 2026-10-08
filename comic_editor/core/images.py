"""Embedded, original-byte image resources keyed by document object ID."""
from __future__ import annotations

import mimetypes
import os
import re
import shutil
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QImage, QImageReader, QColorSpace
from .encoded_images import EncodedImage, EncodedImageCache, DEFAULT_ENCODED_CACHE
from .pixel_arrays import image_has_high_precision


@dataclass(frozen=True, init=False, eq=False)
class ImageSource:
    filename: str
    mime_type: str
    _encoded: EncodedImage

    def __init__(self, filename, mime_type, data):
        object.__setattr__(self, 'filename', filename)
        object.__setattr__(self, 'mime_type', mime_type)
        object.__setattr__(self, '_encoded', data if isinstance(data, EncodedImage)
                           else EncodedImage.from_bytes(bytes(data), DEFAULT_ENCODED_CACHE))

    @property
    def data(self):
        return self._encoded.data

    def __deepcopy__(self, memo):
        return self

    def __eq__(self, other):
        if not isinstance(other, ImageSource):
            return NotImplemented
        return (self.filename == other.filename and self.mime_type == other.mime_type
                and (self._encoded is other._encoded or self.data == other.data))

    def __hash__(self):
        return hash((self.filename, self.mime_type, self.data))


class ImageStore:
    """Own immutable imported files while caching decoded display frames."""

    def __init__(self, *, decoded_budget: int = 256 * 1024 * 1024,
                 encoded_budget: int = 64 * 1024 * 1024) -> None:
        self._sources: dict[str, ImageSource] = {}
        self._decoded: OrderedDict[object, QImage] = OrderedDict()
        # Metadata only, bounded/retired by the existing decoded LRU.
        self._owned_decodes = {}
        self.decoded_budget = max(0, int(decoded_budget))
        self.decoded_bytes = 0
        self._encoded_cache = EncodedImageCache(encoded_budget)
        self._saved_sources = {}
        self._pending_saves = {}
        self.dirty: set[str] = set()

    def _remember_owned_decode(self, key, image, encoded):
        """Only ordinary decoder acquisition may establish ownership."""
        if self._decoded.get(key) is image:
            self._owned_decodes[key] = (encoded, int(image.cacheKey()),
                image.width(), image.height(), image.format().value)

    def _adopt_owned_decode(self, key, image, witness):
        """Transfer metadata for a proved COW alias; never infer it."""
        identifier = key if isinstance(key, str) else key[1]
        source = self._sources.get(identifier)
        if (source is not None and witness is not None
                and witness[0] is source._encoded
                and witness[1:] == (int(image.cacheKey()), image.width(),
                    image.height(), image.format().value)):
            self._remember_owned_decode(key, image, source._encoded)

    def _forget_decoded(self, object_id):
        self._owned_decodes.pop(object_id, None)
        previous = self._decoded.pop(object_id, None)
        if previous is not None:
            self.decoded_bytes -= int(previous.sizeInBytes())

    def _cache_decoded(self, object_id, image):
        self._forget_decoded(object_id)
        size = int(image.sizeInBytes())
        if size > self.decoded_budget:
            return
        while self._decoded and self.decoded_bytes + size > self.decoded_budget:
            self._forget_decoded(next(iter(self._decoded)))
        self._decoded[object_id] = image
        self.decoded_bytes += size

    def _forget_object_decoded(self, object_id):
        for key in tuple(self._decoded):
            if key == object_id or (isinstance(key, tuple) and len(key) > 1
                                    and key[1] == object_id):
                self._forget_decoded(key)

    @staticmethod
    def safe_filename(filename: str) -> str:
        name = Path(str(filename or "image")).name.strip() or "image"
        name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).rstrip(". ")
        return name or "image"

    @staticmethod
    def _decode_native(data: bytes) -> tuple[QImage, bytes]:
        # Own the QByteArray inside Qt instead of borrowing a Python-managed
        # QByteArray pointer. The reader and its plugin must also die before
        # their non-owned device, including on an unsuccessful decode.
        encoded = QByteArray(data)
        buffer = QBuffer()
        buffer.setData(encoded)
        buffer.open(QIODevice.OpenModeFlag.ReadOnly)
        reader = QImageReader(buffer)
        reader.setAutoTransform(True)
        image = reader.read()
        detected = bytes(reader.format())
        error_message = reader.errorString()
        del reader
        buffer.close()
        if image.isNull():
            # TGA has no reliable magic header for Qt's byte-buffer reader.
            # Pillow also covers formats absent from a particular Qt install.
            from io import BytesIO
            from PIL import Image
            try:
                with Image.open(BytesIO(data)) as decoded:
                    detected = (decoded.format or "").lower().encode("ascii")
                    profile = decoded.info.get('icc_profile', b'')
                    if decoded.mode.startswith('I;16'):
                        import numpy as np
                        values = np.ascontiguousarray(np.asarray(decoded), dtype=np.uint16)
                        image = QImage(values.data, decoded.width, decoded.height,
                                       values.strides[0], QImage.Format_Grayscale16).copy()
                    elif decoded.mode == 'F':
                        import numpy as np
                        values = np.ones((decoded.height, decoded.width, 4), np.float32)
                        values[..., :3] = np.asarray(decoded, np.float32)[..., None]
                        image = QImage(values.data, decoded.width, decoded.height,
                                       values.strides[0], QImage.Format_RGBA32FPx4).copy()
                    else:
                        rgba = decoded.convert("RGBA")
                        image = QImage(
                            rgba.tobytes(), rgba.width, rgba.height,
                            rgba.width * 4, QImage.Format_RGBA8888,
                        ).copy()
                    if profile:
                        image.setColorSpace(QColorSpace.fromIccProfile(QByteArray(profile)))
            except (OSError, ValueError) as error:
                raise ValueError(error_message or "Unsupported or invalid image") from error
        return image, detected

    @staticmethod
    def _decode(data: bytes) -> tuple[QImage, bytes]:
        image, detected = ImageStore._decode_native(data)
        return image.convertToFormat(QImage.Format_ARGB32_Premultiplied), detected

    @staticmethod
    def _verify(data: bytes) -> None:
        """Check stored image bytes without retaining a full decoded frame."""
        from io import BytesIO
        from PIL import Image

        try:
            with Image.open(BytesIO(data)) as image:
                image.verify()
        except (OSError, ValueError):
            # Qt supports some formats Pillow does not; retain its existing
            # decoder and error handling for those files.
            ImageStore._decode(data)

    def put(
        self, object_id: str, filename: str, data: bytes,
        mime_type: str = "",
    ) -> ImageSource:
        raw = bytes(data)
        safe = self.safe_filename(filename)
        guessed = mimetypes.guess_type(safe)[0] or ""
        previous = self._sources.get(str(object_id))
        known_mime = str(mime_type or guessed)
        if (previous is not None and known_mime
                and previous.filename == safe and previous.mime_type == known_mime
                and previous.data == raw):
            # The immutable payload has already passed validation. Preserve the
            # caller's dirty notification without decoding or pinning it again.
            self.dirty.add(str(object_id))
            return previous
        image, detected = self._decode(raw)
        if not mime_type:
            mime_type = guessed or (
                f"image/{detected.decode('ascii', 'ignore').lower()}"
                if detected else "application/octet-stream"
            )
        source = ImageSource(safe, str(mime_type), EncodedImage.from_bytes(raw, self._encoded_cache))
        self._sources[str(object_id)] = source
        self._forget_object_decoded(str(object_id))
        self._cache_decoded(str(object_id), image)
        self._remember_owned_decode(str(object_id), image, source._encoded)
        self.dirty.add(str(object_id))
        return source

    def put_decoded(
        self, object_id: str, filename: str, data: bytes, image: QImage,
        mime_type: str = "image/png",
    ) -> ImageSource:
        """Store already-validated bytes and their decoded display image."""
        if image.isNull():
            raise ValueError("Cannot store a null decoded image")
        object_id = str(object_id)
        source = ImageSource(
            self.safe_filename(filename), str(mime_type),
            EncodedImage.from_bytes(bytes(data), self._encoded_cache)
        )
        self._sources[object_id] = source
        self._forget_object_decoded(object_id)
        frames = [(('native', object_id), QImage(image)),
                  (object_id, image.convertToFormat(QImage.Format_ARGB32_Premultiplied))]
        if image_has_high_precision(image):
            frames.reverse()
        # A one-frame budget retains the immediately useful representation:
        # display pixels for byte sources, original precision for wide sources.
        for key, frame in frames:
            self._cache_decoded(key, frame)
        self.dirty.add(object_id)
        return source

    def source(self, object_id: str) -> ImageSource | None:
        return self._sources.get(str(object_id))

    def pixel_signature(self, object_id: str) -> tuple:
        """Identify immutable source bytes without decoding their display image."""
        fingerprint = getattr(self, "render_fingerprint", None)
        if fingerprint is not None:
            return fingerprint(self, object_id)
        source = self.source(object_id)
        return (str(source._encoded.pin.path),) if source is not None else ()

    def image(self, object_id: str, *, contract=None) -> QImage:
        """Return the legacy frame or an explicitly converted working source.

        Original bytes and decoded native pixels never adopt the document's
        color policy. Working frames use the same bounded residency budget and
        are invalidated when either source bytes or the color environment change.
        """
        object_id = str(object_id)
        if contract is not None and contract.floating:
            from comic_editor.render.pixels import import_image, color_environment
            key = ('working', object_id, contract.signature, color_environment(contract))
            cached = self._decoded.get(key)
            if cached is not None:
                self._decoded.move_to_end(key)
                return QImage(cached)
            image = import_image(self.native_image(object_id), contract)
            if not image.isNull():
                self._cache_decoded(key, image)
            return QImage(image)
        cached = self._decoded.get(object_id)
        if cached is not None:
            self._decoded.move_to_end(object_id)
            return QImage(cached)
        source = self._sources.get(object_id)
        if source is None:
            return QImage()
        image, _detected = self._decode(source.data)
        self._cache_decoded(object_id, image)
        self._remember_owned_decode(object_id, image, source._encoded)
        return QImage(image)

    def native_image(self, object_id: str) -> QImage:
        """Decode the immutable original without reducing precision or its ICC profile.

        Native and legacy display frames share the same residency budget. Color
        conversion belongs to the renderer's source edge, before composition.
        """
        object_id = str(object_id)
        key = ('native', object_id)
        cached = self._decoded.get(key)
        if cached is not None:
            self._decoded.move_to_end(key)
            return QImage(cached)
        source = self.source(object_id)
        if source is None:
            return QImage()
        image, _detected = self._decode_native(source.data)
        self._cache_decoded(key, image)
        self._remember_owned_decode(key, image, source._encoded)
        return QImage(image)

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
            source._encoded,
        )
        self.dirty.add(object_id)
        return True

    def remove(self, object_id: str) -> None:
        object_id = str(object_id)
        if object_id in self._sources:
            self._sources.pop(object_id, None)
            self._forget_object_decoded(object_id)
            self.dirty.add(object_id)

    def copy_source_to(self, object_id: str, target: "ImageStore", new_id: str) -> None:
        source = self.source(object_id)
        if source is not None:
            identifier = str(new_id)
            cached = self._decoded.get(str(object_id))
            target._sources[identifier] = source
            target._forget_object_decoded(identifier)
            native = self._decoded.get(('native', str(object_id)))
            frames = [(('native', identifier), native), (identifier, cached)]
            if native is not None and image_has_high_precision(native):
                frames.reverse()
            for key, frame in frames:
                if frame is not None:
                    target._cache_decoded(key, QImage(frame))
            target.dirty.add(identifier)

    def clone(self, object_ids: set[str] | None = None) -> "ImageStore":
        result = ImageStore()
        result._encoded_cache = self._encoded_cache
        identifiers = set(self._sources) if object_ids is None else set(object_ids)
        for object_id in identifiers:
            source = self._sources.get(object_id)
            if source is not None:
                result._sources[object_id] = source
        result.dirty.clear()
        return result

    def snapshot(self, object_ids: set[str] | None = None) -> dict[str, ImageSource]:
        identifiers = set(self._sources) if object_ids is None else set(object_ids)
        return {
            object_id: item
            for object_id in identifiers
            if (item := self._sources.get(object_id)) is not None
        }

    def restore(self, values: dict[str, ImageSource]) -> None:
        self._sources = {
            object_id: item
            for object_id, item in values.items()
        }
        self._decoded.clear()
        self._owned_decodes.clear()
        self.decoded_bytes = 0
        self.dirty.update(values)

    def apply_patch(self, values: dict[str, ImageSource | None]) -> None:
        """Restore changed immutable originals, retaining unrelated sources."""
        for identifier, source in values.items():
            self._forget_object_decoded(identifier)
            if source is None:
                self._sources.pop(identifier, None)
            else:
                self._sources[identifier] = source
            self.dirty.add(identifier)

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
        incremental: bool = False, transactional: bool = False,
    ) -> None:
        root.mkdir(parents=True, exist_ok=True)
        destination = str(root.resolve())
        known = self._saved_sources.get(destination, {})
        saved = {}
        for object_id in object_ids:
            source = self._sources.get(object_id)
            if source is None:
                continue
            directory = root / object_id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / self.safe_filename(source.filename)
            try:
                stat = target.stat()
                stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            except OSError:
                stamp = None
            if ((complete and not incremental) or known.get(object_id) != (source, stamp)
                    or stamp is None):
                self._atomic_bytes(target, source.data)
                stat = target.stat()
                stamp = (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns)
            saved[object_id] = (source, stamp)
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
        self._pending_saves[destination] = saved
        if not transactional:
            self.commit_directory(root)

    def commit_directory(self, root: Path) -> None:
        destination = str(root.resolve())
        saved = self._pending_saves.pop(destination, None)
        if saved is not None:
            self._saved_sources[destination] = saved

    def load_directory(
        self, root: Path, metadata: dict[str, tuple[str, str]],
    ) -> None:
        self._sources.clear()
        self._decoded.clear()
        self._owned_decodes.clear()
        self.decoded_bytes = 0
        self._saved_sources.clear()
        self._pending_saves.clear()
        if not root.is_dir():
            return
        saved = {}
        for object_id, (filename, mime_type) in metadata.items():
            directory = root / object_id
            requested = directory / self.safe_filename(filename)
            candidates = [requested] if requested.is_file() else (
                [path for path in directory.iterdir() if path.is_file()]
                if directory.is_dir() else []
            )
            if not candidates:
                continue
            encoded = EncodedImage.from_file(candidates[0], self._encoded_cache)
            self._verify(encoded.data)
            self._sources[object_id] = ImageSource(
                self.safe_filename(filename or candidates[0].name),
                mime_type or mimetypes.guess_type(candidates[0].name)[0]
                or "application/octet-stream",
                encoded,
            )
            stat = candidates[0].stat()
            saved[object_id] = (self._sources[object_id],
                (stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns))
        self.dirty.clear()
        self._saved_sources[str(root.resolve())] = saved
