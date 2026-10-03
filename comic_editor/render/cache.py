"""Optional durable backing for the existing exact render caches.

This module stores values and semantic keys; it never renders or decides which
artwork depends on which source. Only explicit recording scopes write results.
"""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import base64
import json
import math
from threading import RLock
import os
from pathlib import Path
import struct
import sys
import uuid
import zlib

import numpy as np
from PySide6.QtGui import QImage, QColorSpace
from PySide6.QtCore import QRectF, QPoint

from .service import RenderPending


CACHE_VERSION = 1
RENDERER_VERSION = "native-artwork-1"
MAX_PAYLOAD = 512 * 1024 * 1024
WRITE_BUDGET = 64 * 1024 * 1024
READ_BUDGET = 64 * 1024 * 1024
MAGIC = b"WTRCACHE1"


def canonical(value):
    if isinstance(value, QRectF):
        return {"rect": list(value.getRect())}
    if isinstance(value, (tuple, list)):
        return [canonical(item) for item in value]
    if isinstance(value, dict):
        return {str(key): canonical(item) for key, item in sorted(value.items(), key=lambda p: str(p[0]))}
    if isinstance(value, np.generic):
        return value.item()
    if value is None or isinstance(value, (str, bool, int, float)):
        return value
    raise TypeError(f"Not a semantic cache value: {type(value).__name__}")


def restore_state(value):
    if isinstance(value, dict) and set(value) == {"rect"}:
        return QRectF(*value["rect"])
    if isinstance(value, list):
        return tuple(restore_state(item) for item in value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(canonical(value), sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


@dataclass(frozen=True)
class CacheDescriptor:
    kind: str
    key: tuple
    contract: tuple = ()
    environment: tuple = ()

    @property
    def identity(self):
        return digest((CACHE_VERSION, RENDERER_VERSION, self.kind, self.key,
                       self.contract, self.environment, sys.byteorder))


def _freeze(value):
    if isinstance(value, QImage):
        if value.isNull():
            raise ValueError("Cannot persist an empty image")
        return QImage(value)
    result = np.array(value, copy=True, order="C")
    if result.dtype.hasobject:
        raise ValueError("Object arrays cannot be persisted")
    result.flags.writeable = False
    return result


def encode_value(value):
    if isinstance(value, QImage):
        spec = dict(type="image", width=value.width(), height=value.height(),
                    stride=value.bytesPerLine(), format=value.format().value,
                    dpr=value.devicePixelRatio(), dpi=[value.dotsPerMeterX(), value.dotsPerMeterY()],
                    offset=[value.offset().x(), value.offset().y()])
        profile = bytes(value.colorSpace().iccProfile())
        if profile:
            spec["profile"] = base64.b64encode(profile).decode("ascii")
        raw = value.constBits()
    else:
        array = np.ascontiguousarray(value)
        if array.dtype.hasobject:
            raise ValueError("Object arrays cannot be persisted")
        spec = dict(type="array", dtype=array.dtype.str, shape=list(array.shape))
        raw = array.tobytes()
    if len(raw) > MAX_PAYLOAD:
        raise ValueError("Render cache value exceeds the payload limit")
    spec["bytes"] = len(raw)
    header = json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()
    return MAGIC + struct.pack("<I", len(header)) + header + zlib.compress(raw, 1)


def decode_value(payload):
    if not payload.startswith(MAGIC) or len(payload) < len(MAGIC) + 4:
        raise ValueError("Invalid render cache payload")
    size = struct.unpack_from("<I", payload, len(MAGIC))[0]
    start = len(MAGIC) + 4
    if size > 1024 * 1024 or start + size > len(payload):
        raise ValueError("Invalid render cache header")
    spec = json.loads(payload[start:start + size])
    expected = spec["bytes"]
    if type(expected) is not int or not 0 < expected <= MAX_PAYLOAD:
        raise ValueError("Invalid render cache size")
    decoder = zlib.decompressobj()
    raw = decoder.decompress(payload[start + size:], expected + 1)
    if len(raw) != expected or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail:
        raise ValueError("Invalid compressed render cache")
    if spec["type"] == "image":
        width, height, stride = (spec[name] for name in ("width", "height", "stride"))
        if any(type(v) is not int or v <= 0 for v in (width, height, stride)) or stride * height != expected:
            raise ValueError("Invalid image dimensions")
        result = QImage(raw, width, height, stride, QImage.Format(spec["format"])).copy()
        if result.isNull() or result.sizeInBytes() != expected:
            raise ValueError("Invalid image format")
        dpr = spec.get("dpr", 1.)
        if not isinstance(dpr, (int, float)) or not math.isfinite(dpr) or not 0 < dpr <= 64:
            raise ValueError("Invalid image density")
        result.setDevicePixelRatio(dpr)
        if "dpi" in spec:
            x, y = spec["dpi"]
            result.setDotsPerMeterX(x)
            result.setDotsPerMeterY(y)
        if "offset" in spec:
            result.setOffset(QPoint(*spec["offset"]))
        if "profile" in spec:
            result.setColorSpace(QColorSpace.fromIccProfile(base64.b64decode(spec["profile"], validate=True)))
        return result
    if spec["type"] != "array":
        raise ValueError("Unknown render cache value")
    dtype = np.dtype(spec["dtype"])
    shape = spec["shape"]
    if dtype.hasobject or any(type(v) is not int or v <= 0 for v in shape):
        raise ValueError("Invalid numeric array")
    if int(np.prod(shape, dtype=object)) * dtype.itemsize != expected:
        raise ValueError("Invalid array dimensions")
    result = np.frombuffer(raw, dtype=dtype).reshape(shape).copy()
    result.flags.writeable = False
    return result


def _atomic_bytes(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f"{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        with temporary.open("wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


class PersistentRenderCache:
    """Non-blocking read-through backing; caller owns memory and dependencies."""

    def __init__(self, root, *, contract=(), environment=()):
        self.root = Path(root)
        self.contract, self.environment = tuple(contract), tuple(environment)
        self.entries = {}
        self.source_digests = {}
        try:
            document = json.loads((self.root / "index.json").read_text(encoding="utf-8"))
            if not isinstance(document, dict):
                raise ValueError("Invalid render cache index")
            if document.get("version") == CACHE_VERSION and document.get("renderer") == RENDERER_VERSION:
                self.entries = document.get("entries", {})
                self.source_digests = document.get("sources", {})
                if not isinstance(self.entries, dict) or not isinstance(self.source_digests, dict):
                    self.entries, self.source_digests = {}, {}
                self.entries = {identity: entry for identity, entry in self.entries.items()
                                if self._valid_entry(identity, entry)}
        except (OSError, ValueError, TypeError):
            pass
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="render-cache-io")
        self.values_lock = RLock()
        self.reads, self.writes, self.verifications = {}, {}, {}
        self.staged = {}
        self.publication = None
        self.verified = set()
        self.ready = OrderedDict()
        self.ready_bytes = 0
        self.recording = 0
        self.write_bytes = 0
        self.error = ""
        self.closed = False
        self.epoch = 0
        self.invalidated = False
        self.hits = self.misses = self.loads = self.saved = 0

    def descriptor(self, kind, key):
        return CacheDescriptor(kind, tuple(key), self.contract, self.environment)

    @staticmethod
    def _valid_entry(identity, entry):
        try:
            return (isinstance(identity, str) and isinstance(entry, dict)
                and isinstance(entry["kind"], str) and isinstance(entry["key"], list)
                and isinstance(entry["blob"], str) and len(entry["blob"]) == 64
                and all(c in "0123456789abcdef" for c in entry["blob"])
                and type(entry["raw_size"]) is int and 0 < entry["raw_size"] <= MAX_PAYLOAD
                and type(entry["size"]) is int and 0 < entry["size"] <= MAX_PAYLOAD * 1.01 + 4096
                and entry["seal"] == digest((identity, entry["blob"], entry.get("state"),
                                            entry["raw_size"], entry["size"])))
        except (KeyError, TypeError, ValueError):
            return False

    @contextmanager
    def record(self):
        self.recording += 1
        try:
            yield
        finally:
            self.recording -= 1

    def _path(self, entry):
        name = entry["blob"]
        if len(name) != 64 or any(c not in "0123456789abcdef" for c in name):
            raise ValueError("Invalid render cache filename")
        return self.root / "values" / (name + ".cache")

    def _read(self, entry, *, verify=False):
        with self.values_lock:
            return self._read_value(entry, verify=verify)

    def _read_value(self, entry, *, verify=False):
        path = self._path(entry)
        size = entry["size"]
        if type(size) is not int or not 0 < size <= MAX_PAYLOAD * 1.01 + 4096 or path.stat().st_size != size:
            raise ValueError("Invalid render cache file size")
        payload = path.read_bytes()
        if len(payload) != entry["size"] or hashlib.sha256(payload).hexdigest() != entry["blob"]:
            raise ValueError("Render cache checksum mismatch")
        result = decode_value(payload)
        return True if verify else result

    def _entry(self, kind, key):
        identity = self.descriptor(kind, key).identity
        entry = self.entries.get(identity)
        if entry is not None:
            try:
                valid = (entry["kind"] == kind and canonical(entry["key"]) == canonical(key)
                    and entry["seal"] == digest((identity, entry["blob"], entry.get("state"),
                                                entry["raw_size"], entry["size"])))
            except (KeyError, TypeError, ValueError):
                valid = False
            if not valid:
                self.entries.pop(identity, None)
                self.verified.discard(identity)
                self.invalidated = True
                entry = None
        return identity, entry

    def has(self, kind, key, *, verify=True):
        identity, entry = self._entry(kind, key)
        if entry is None:
            return False
        try:
            if not self._path(entry).is_file():
                self.entries.pop(identity, None)
                self.invalidated = True
                return False
        except (OSError, ValueError, KeyError, TypeError):
            self.entries.pop(identity, None)
            self.invalidated = True
            return False
        if verify and identity not in self.verified:
            if identity not in self.verifications and len(self.verifications) < 8:
                self.verifications[identity] = self.executor.submit(self._read, dict(entry), verify=True)
            return False
        return True

    def lookup(self, kind, key, *, wait=False):
        if self.closed:
            return None
        identity, entry = self._entry(kind, key)
        result = self.ready.pop(identity, None)
        if result is not None:
            self.ready_bytes -= int(result.sizeInBytes() if isinstance(result, QImage) else result.nbytes)
            self.hits += 1
            self.retain(kind, key, result)
            return result
        if entry is None:
            self.misses += 1
            return None
        future = self.reads.get(identity)
        if future is None:
            # Retire abandoned completed reads when the camera moves. Never
            # accumulate one decoded image per chapter tile in this adapter.
            reserved = sum(self.entries.get(item, {}).get("raw_size", MAX_PAYLOAD)
                           for item in self.reads)
            for old, previous in list(self.reads.items()):
                if len(self.reads) < 8 and reserved + entry.get("raw_size", MAX_PAYLOAD) <= READ_BUDGET:
                    break
                if previous.done() or wait:
                    if wait:
                        try:
                            previous.result()
                        except Exception:
                            pass
                    self.reads.pop(old)
                    reserved -= self.entries.get(old, {}).get("raw_size", MAX_PAYLOAD)
            if self.reads and (len(self.reads) >= 8 or reserved + entry.get("raw_size", MAX_PAYLOAD) > READ_BUDGET):
                raise RenderPending("Saved artwork is loading")
            future = self.reads[identity] = self.executor.submit(self._read, dict(entry))
        if not wait and not future.done():
            raise RenderPending("Saved artwork is loading")
        try:
            result = future.result()
        except Exception:
            self.entries.pop(identity, None)
            self.verified.discard(identity)
            self.invalidated = True
            self.misses += 1
            return None
        finally:
            self.reads.pop(identity, None)
        self.verified.add(identity)
        self.loads += 1
        self.retain(kind, key, result)
        return result

    def retain(self, kind, key, value, *, state=None):
        if not self.recording or self.closed:
            return
        descriptor = self.descriptor(kind, key)
        identity = descriptor.identity
        if identity in self.entries or identity in self.writes or identity in self.staged:
            return True
        size = int(value.sizeInBytes() if isinstance(value, QImage) else value.nbytes)
        if self.write_bytes and self.write_bytes + size > WRITE_BUDGET:
            return False
        if size > MAX_PAYLOAD:
            self.error = "A render result is too large to cache"
            return
        frozen = _freeze(value)
        snapshot, semantic_key = canonical(state), canonical(key)
        epoch = self.epoch
        def write():
            payload = encode_value(frozen)
            blob = hashlib.sha256(payload).hexdigest()
            path = self.root / "values" / (blob + ".cache")
            with self.values_lock:
                try:
                    existing = path.read_bytes()
                    valid = hashlib.sha256(existing).hexdigest() == blob
                except OSError:
                    valid = False
                if not valid:
                    _atomic_bytes(path, payload)
            return dict(kind=kind, key=semantic_key, blob=blob, size=len(payload), raw_size=size,
                        state=snapshot, seal=digest((identity, blob, snapshot, size, len(payload))))
        self.writes[identity] = self.executor.submit(write), size, epoch
        self.write_bytes += size
        return True

    def poll(self):
        changed, self.invalidated = self.invalidated, False
        if self.publication is not None and self.publication[0].done():
            future, snapshot, epoch = self.publication
            self.publication = None
            try:
                future.result()
                if epoch == self.epoch:
                    for identity, entry in snapshot.items():
                        self.entries[identity] = entry
                        self.staged.pop(identity, None)
                        self.verified.add(identity)
                        self.saved += 1
                    changed = True
            except Exception as error:
                self.error = str(error)
        for identity, (future, size, epoch) in list(self.writes.items()):
            if not future.done():
                continue
            self.writes.pop(identity)
            self.write_bytes -= size
            try:
                entry = future.result()
                if epoch == self.epoch:
                    self.staged[identity] = entry
            except Exception as error:
                self.error = str(error)
        for identity, future in list(self.verifications.items()):
            if not future.done():
                continue
            self.verifications.pop(identity)
            try:
                future.result()
                self.verified.add(identity)
            except Exception:
                self.entries.pop(identity, None)
            changed = True
        if self.staged and self.publication is None and not self.error:
            snapshot = dict(self.staged)
            data = self._index_bytes({**self.entries, **snapshot})
            self.publication = (self.executor.submit(_atomic_bytes, self.root / "index.json", data),
                                snapshot, self.epoch)
        return changed

    def _index_bytes(self, entries):
        return json.dumps(dict(version=CACHE_VERSION, renderer=RENDERER_VERSION,
            entries=entries, sources=self.source_digests), sort_keys=True,
            separators=(",", ":")).encode()

    def flush(self):
        _atomic_bytes(self.root / "index.json", self._index_bytes(self.entries))

    def drain(self):
        # Used only during shutdown/explicit clearing, never while painting.
        while self.writes or self.publication is not None or (self.staged and not self.error):
            for future, _, _ in tuple(self.writes.values()):
                try:
                    future.result()
                except Exception:
                    pass
            if self.publication is not None:
                try:
                    self.publication[0].result()
                except Exception:
                    pass
            self.poll()

    def clear(self, predicate=None):
        self.drain()
        self.epoch += 1
        for identity, entry in list(self.entries.items()):
            if predicate is None or predicate(entry):
                self.entries.pop(identity, None)
                self.verified.discard(identity)
        self.ready.clear()
        self.ready_bytes = 0
        self.reads.clear()
        self.verifications.clear()
        self.flush()
        # Values can be shared by many keys; collect only unreferenced blobs.
        keep = {entry["blob"] for entry in self.entries.values()}
        for path in (self.root / "values").glob("*.cache"):
            if path.stem not in keep:
                with self.values_lock:
                    path.unlink(missing_ok=True)

    @property
    def disk_bytes(self):
        unique = {entry["blob"]: entry["size"] for entry in self.entries.values()}
        return sum(unique.values())

    @property
    def pending(self):
        return bool(self.reads or self.writes or self.verifications or self.staged or self.publication)

    def close(self):
        if self.closed:
            return
        self.drain()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.closed = True
        self.reads.clear()
        self.verifications.clear()
