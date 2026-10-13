"""Optional durable backing for the existing exact render caches.

This module stores values and semantic keys; it never renders or decides which
artwork depends on which source. Only explicit recording scopes write results.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import Mapping, MutableMapping
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import copy
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
import time
import uuid
import zlib
from weakref import WeakSet, WeakValueDictionary

import numpy as np
from PySide6.QtGui import QImage, QColorSpace
from PySide6.QtCore import QRectF, QPoint

from .service import RenderPending


CACHE_VERSION = 1
# Shared native source/visibility semantics for the detached scene architecture.
RENDERER_VERSION = "native-artwork-20261008-refactor-sector-full-frame-2"
MAX_PAYLOAD = 512 * 1024 * 1024
WRITE_BUDGET = 64 * 1024 * 1024
READ_BUDGET = 64 * 1024 * 1024
READY_BUDGET = 64 * 1024 * 1024
MAGIC = b"WTRCACHE1"

_MANIFEST_COOPERATIVE_THRESHOLD = 1024 * 1024
_MANIFEST_SLICE_SECONDS = .002
_MANIFEST_SLICE_RECORDS = 128


def _load_cache_manifest(text):
    """Decode the ordinary JSON index, yielding between completed objects.

    One stdlib scanner owns the grammar, values and shared field-name memo.
    Its callback returns each dictionary unchanged; validation and atomic
    adoption remain in _committed_index. Nothing is published during a yield.
    """
    if len(text) < _MANIFEST_COOPERATIVE_THRESHOLD or text.startswith('\ufeff'):
        try:
            return json.loads(text)
        except RecursionError:
            # This helper adds one call frame to loads(). Decode directly to
            # retain loads -> decode -> raw_decode's original recursion budget.
            return json.JSONDecoder().decode(text)
    deadline = time.perf_counter() + _MANIFEST_SLICE_SECONDS
    records = 0

    def cooperate(value):
        nonlocal deadline, records
        records += 1
        if (records >= _MANIFEST_SLICE_RECORDS
                or time.perf_counter() >= deadline):
            # Zero seconds relinquishes this worker's timeslice and Python GIL.
            # No GUI objects, partial publication or cache identity is involved.
            time.sleep(0)
            deadline = time.perf_counter() + _MANIFEST_SLICE_SECONDS
            records = 0
        return value

    try:
        return json.JSONDecoder(object_hook=cooperate).decode(text)
    except RecursionError:
        # The callback can exhaust the stack at an otherwise valid deeply
        # nested object. The rare retry uses the original scanner with the
        # same Python call depth as loads -> decode -> raw_decode.
        return json.JSONDecoder().decode(text)


class _OwnedManifestRecords(MutableMapping):
    """Independent mutable metadata, copying a private record on first read.

    Keys and ordering belong to this owner. Every public value access uses
    __getitem__, so nested records never escape the immutable committed base.
    Explicit publication/inspection may materialize an ordinary dict.
    """
    def __init__(self, base):
        self._base = base
        self._keys = dict.fromkeys(base)
        self._owned = {}

    def __getitem__(self, key):
        if key not in self._keys:
            raise KeyError(key)
        if key not in self._owned:
            self._owned[key] = copy.deepcopy(self._base[key])
        return self._owned[key]

    def __setitem__(self, key, value):
        self._keys[key] = None
        self._owned[key] = value

    def __delitem__(self, key):
        del self._keys[key]
        self._owned.pop(key, None)

    def __iter__(self):
        return iter(self._keys)

    def __reversed__(self):
        return reversed(self._keys)

    def __len__(self):
        return len(self._keys)

    def __contains__(self, key):
        return key in self._keys

    def clear(self):
        self._keys.clear()
        self._owned.clear()
        self._base = {}

    def copy(self):
        return dict(self.items())

    def popitem(self):
        if not self:
            raise KeyError('popitem(): dictionary is empty')
        key = next(reversed(self._keys))
        return key, self.pop(key)

    def __or__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        result = self.copy()
        result.update(other)
        return result

    def __ror__(self, other):
        if not isinstance(other, Mapping):
            return NotImplemented
        result = dict(other)
        result.update(self)
        return result

    def __ior__(self, other):
        self.update(other)
        return self


@dataclass(frozen=True)
class _CommittedManifest:
    stamp: tuple
    epoch: int
    generation: int
    versions: tuple
    entries: dict
    sources: dict


class _RootState:
    def __init__(self):
        self.lock = RLock()
        self.ownership_lock = RLock()
        self.epoch = 0
        self.generation = 0
        self.owners = WeakSet()
        # Privately owned records are never mutated or returned directly.
        # A reader's lazy maps keep the base alive if this root is retired.
        self.committed = None


_ROOTS = WeakValueDictionary()
_ROOTS_LOCK = RLock()


def _root_state(root):
    key = os.path.normcase(str(root.resolve()))
    with _ROOTS_LOCK:
        state = _ROOTS.get(key)
        if state is None:
            state = _RootState()
            _ROOTS[key] = state
        return state


IDENTITY_BUDGET = 16 * 1024 * 1024
IDENTITY_LIMIT = 8192

# Only descriptor hashes are reused here. Entries and their dependency/seal
# validation remain live, including after recording, invalidation, and reopen.
_identities = OrderedDict()
_identity_bytes = 0
_identity_lock = RLock()


def _immutable_identity_token(value):
    """An immutable, typed token plus a conservative retained-byte estimate.

    Python tuple equality merges bool/int/float and the two signed zeroes;
    canonical JSON does not. Mutable public semantic values must keep taking
    the ordinary digest path, even when their containing descriptor is frozen.
    """
    value_type = type(value)
    if value_type is tuple:
        items, size = [], 112 + 8 * len(value)
        for item in value:
            result = _immutable_identity_token(item)
            if result is None:
                return None
            token, retained = result
            items.append(token)
            size += retained
        return (tuple, tuple(items)), size
    if value_type is str:
        return (str, value), 112 + 4 * len(value)
    if value_type is bool:
        return (bool, value), 84
    if value_type is int:
        return (int, value), 84 + 4 * ((value.bit_length() + 29) // 30)
    if value_type is float:
        representation = value.hex()
        return (float, representation), 112 + len(representation)
    if value is None:
        return (type(None),), 64
    if isinstance(value, np.generic):
        # canonical() also converts a NumPy scalar to its Python scalar.
        scalar = value.item()
        return None if isinstance(scalar, np.generic) else _immutable_identity_token(scalar)
    return None


def _descriptor_identity(value):
    global _identity_bytes
    result = _immutable_identity_token(value)
    if result is None:
        return digest(value)
    token, size = result
    size += 256  # Digest string and bounded-LRU bookkeeping.
    if size > IDENTITY_BUDGET or IDENTITY_LIMIT <= 0:
        return digest(value)
    with _identity_lock:
        previous = _identities.get(token)
        if previous is not None:
            _identities.move_to_end(token)
            return previous[0]
    # Serialize/hash the original value, preserving the existing disk identity.
    identity = digest(value)
    with _identity_lock:
        if token not in _identities:
            _identities[token] = identity, size
            _identity_bytes += size
            while _identity_bytes > IDENTITY_BUDGET or len(_identities) > IDENTITY_LIMIT:
                _, (_, retained) = _identities.popitem(last=False)
                _identity_bytes -= retained
    return identity


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
        return _descriptor_identity((CACHE_VERSION, RENDERER_VERSION, self.kind, self.key,
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

    def __init__(self, root, *, contract=(), environment=(), resident=None, defer_index=False):
        self.root = Path(root)
        self._root_state = _root_state(self.root)
        with self._root_state.ownership_lock:
            self._root_state.owners.add(self)
        self._root_epoch = self._root_state.epoch
        self.contract, self.environment = tuple(contract), tuple(environment)
        self.resident = resident
        self.entries = {}
        self.source_digests = {}
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="render-cache-io")
        self.values_lock = self._root_state.lock
        self.reads, self.writes, self.verifications = {}, {}, {}
        self.staged = {}
        self.publication = None
        self._reload = None
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
        if defer_index:
            # GUI binding adopts the same validated manifest through poll().
            # The owning backing and root generation guard late completions.
            if (self.root / "index.json").is_file():
                self._reload = self.executor.submit(self._reload_index)
        else:
            self.entries, self.source_digests = self._committed_index()
        if resident is not None:
            self.source_digests.update(resident.source_digests())

    @property
    def index_loading(self):
        return self._reload is not None

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
        self._sync_epoch()
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
        self._sync_epoch()
        if not self.entries:
            return False
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
        self._sync_epoch()
        if self.resident is not None:
            result = self.resident.lookup(self.descriptor(kind, key))
            if result is not None:
                self.hits += 1
                # An explicit disk recording can reuse exact RAM values.
                self.retain(kind, key, result, state=self.resident.state(self.descriptor(kind, key)))
                return result
        if self.index_loading:
            if not wait and not self._reload.done():
                raise RenderPending("Saved cache index is loading")
            if wait:
                self._reload.result()
            self.poll()
            if self.index_loading:
                raise RenderPending("Saved cache index is loading")
        if not self.entries and not self.ready:
            # Unpublished entries cannot satisfy reads. Synchronize sibling
            # clears first, then avoid hashing a definite empty-index miss.
            self.misses += 1
            return None
        identity, entry = self._entry(kind, key)
        result = self.ready.pop(identity, None)
        if result is not None:
            self.ready_bytes -= int(result.sizeInBytes() if isinstance(result, QImage) else result.nbytes)
            if entry is not None:
                self.hits += 1
                self.retain(kind, key, result, state=entry.get('state'))
                return result
        if entry is None:
            self.misses += 1
            return None
        future = self.reads.get(identity)
        if future is None:
            # A restarted capture can need an earlier source before reaching
            # its completed effect read. Hand finished reads off to a bounded
            # LRU instead of discarding them to make room for that source:
            # source-LRU eviction would otherwise restart the same IO forever.
            reserved = sum(self.entries.get(item, {}).get("raw_size", MAX_PAYLOAD)
                           for item in self.reads)
            for old, previous in list(self.reads.items()):
                if len(self.reads) < 8 and reserved + entry.get("raw_size", MAX_PAYLOAD) <= READ_BUDGET:
                    break
                if previous.done() or wait:
                    self.reads.pop(old)
                    reserved -= self.entries.get(old, {}).get("raw_size", MAX_PAYLOAD)
                    value = self._finish_read(old, previous)
                    if value is not None:
                        self._ready_put(old, value)
            if self.reads and (len(self.reads) >= 8 or reserved + entry.get("raw_size", MAX_PAYLOAD) > READ_BUDGET):
                raise RenderPending("Saved artwork is loading")
            future = self.reads[identity] = self.executor.submit(self._read, dict(entry))
        if not wait and not future.done():
            raise RenderPending("Saved artwork is loading")
        self.reads.pop(identity, None)
        result = self._finish_read(identity, future)
        if result is not None:
            self.retain(kind, key, result, state=entry.get('state'))
        return result

    def retained_state(self, kind, key):
        descriptor = self.descriptor(kind, key)
        if self.resident is not None and self.resident.has(descriptor):
            return self.resident.state(descriptor)
        return self.entries.get(descriptor.identity, {}).get('state')

    def remember_pixel_identity(self, object_id, address, version, path, known):
        if self.resident is not None:
            self.resident.remember_pixel_identity(object_id, address, version, path, known)

    def remember_source(self, logical, record):
        if self.resident is not None:
            self.resident.remember_source(logical, record)

    def pixel_identity(self, object_id, address, version, path):
        previous = self.resident.pixel_identity(object_id, address) if self.resident is not None else None
        return previous if previous is not None and previous[0] == version else None

    def _finish_read(self, identity, future):
        try:
            result = future.result()
        except Exception:
            self.entries.pop(identity, None)
            self.verified.discard(identity)
            self.invalidated = True
            self.misses += 1
            return None
        self.verified.add(identity)
        self.loads += 1
        return result

    def _ready_put(self, identity, value):
        size = int(value.sizeInBytes() if isinstance(value, QImage) else value.nbytes)
        previous = self.ready.pop(identity, None)
        if previous is not None:
            self.ready_bytes -= int(previous.sizeInBytes() if isinstance(previous, QImage) else previous.nbytes)
        self.ready[identity] = value
        self.ready_bytes += size
        # Keep one oversized completion consumable, as the owning caches do.
        # Other completed reads remain bounded independently of in-flight IO.
        while self.ready_bytes > max(READY_BUDGET, size):
            _, old = self.ready.popitem(last=False)
            self.ready_bytes -= int(old.sizeInBytes() if isinstance(old, QImage) else old.nbytes)

    def retain(self, kind, key, value, *, state=None):
        if self.resident is not None and not self.closed:
            self.resident.retain(self.descriptor(kind, key), value, state)
        if not self.recording or self.closed:
            return
        self._sync_epoch()
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
        epoch = self.epoch,self._root_state.epoch
        def write():
            payload = encode_value(frozen)
            blob = hashlib.sha256(payload).hexdigest()
            path = self.root / "values" / (blob + ".cache")
            with self.values_lock:
                if epoch != (self.epoch,self._root_state.epoch):
                    return None
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
        self._sync_epoch()
        changed, self.invalidated = self.invalidated, False
        if self._reload is not None and self._reload.done():
            future,self._reload = self._reload,None
            try:
                entries,sources,epoch,generation = future.result()
                if epoch == self._root_state.epoch and generation == self._root_state.generation:
                    # Source aliases captured since initialization are still
                    # owned by the ordinary resident dependency metadata.
                    if self.resident is not None:
                        sources.update(self.resident.source_digests())
                    self.entries,self.source_digests = entries,sources
                    changed = True
                else:
                    self._reload = self.executor.submit(self._reload_index)
            except Exception as error:
                self.error = str(error)
        if self.publication is not None and self.publication[0].done():
            future, snapshot, epoch = self.publication
            self.publication = None
            try:
                manifest = future.result()
                if epoch == (self.epoch,self._root_state.epoch) and manifest is not None:
                    self.entries,self.source_digests = manifest
                    for identity, entry in snapshot.items():
                        self.entries[identity] = entry
                        self.staged.pop(identity, None)
                        self.verified.add(identity)
                        self.saved += 1
                    changed = True
                else:
                    for identity in snapshot:
                        self.staged.pop(identity,None)
            except Exception as error:
                self.error = str(error)
        for identity, (future, size, epoch) in list(self.writes.items()):
            if not future.done():
                continue
            self.writes.pop(identity)
            self.write_bytes -= size
            try:
                entry = future.result()
                if epoch == (self.epoch,self._root_state.epoch) and entry is not None:
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
            epoch = self.epoch,self._root_state.epoch
            self.publication = (self.executor.submit(self._publish_index,snapshot,dict(self.source_digests),epoch),
                                snapshot,epoch)
        return changed

    def _sync_epoch(self):
        if self._root_epoch != self._root_state.epoch:
            # A sibling owner cleared this root. A GUI cache reader drops
            # metadata immediately and never waits on the IO owner's lock.
            self._root_epoch = self._root_state.epoch
            self.entries.clear()
            self.verified.clear()
            self.ready.clear()
            self.ready_bytes = 0
            self.reads.clear()
            self.verifications.clear()
            self.staged.clear()
            self.invalidated = True
            if not self.closed:
                self._reload = self.executor.submit(self._reload_index)

    def _reload_index(self):
        with self.values_lock:
            entries,sources = self._committed_index()
            return entries,sources,self._root_state.epoch,self._root_state.generation

    def _committed_index(self):
        path = self.root / 'index.json'
        versions = CACHE_VERSION, RENDERER_VERSION
        def stamp():
            value = path.stat()
            return (value.st_dev, value.st_ino, value.st_size,
                    value.st_mtime_ns, value.st_ctime_ns)
        with self.values_lock:
            state = self._root_state
            try:
                before = stamp()
            except OSError:
                before = None
            committed = state.committed
            if (before is not None and committed is not None
                    and (committed.stamp, committed.epoch, committed.generation, committed.versions)
                    == (before, state.epoch, state.generation, versions)):
                entries, sources = committed.entries, committed.sources
            else:
                state.committed = None
                entries, sources = {}, {}
                try:
                    document = _load_cache_manifest(path.read_text(encoding='utf-8'))
                    if document.get('version') == CACHE_VERSION and document.get('renderer') == RENDERER_VERSION:
                        values, aliases = document.get('entries', {}), document.get('sources', {})
                        if isinstance(values, dict) and isinstance(aliases, dict):
                            entries = {key: value for key, value in values.items()
                                       if self._valid_entry(key, value)}
                            sources = aliases
                            if before is not None and before == stamp():
                                state.committed = _CommittedManifest(before, state.epoch,
                                    state.generation, versions, entries, sources)
                except (OSError, ValueError, TypeError, AttributeError):
                    pass
        # Readers own their mutable records and key order. Source aliases,
        # bad-entry removal and nested state edits cannot mutate this base.
        return _OwnedManifestRecords(entries), _OwnedManifestRecords(sources)

    def _publish_index(self,snapshot,sources,epoch):
        with self.values_lock:
            if epoch != (self.epoch,self._root_state.epoch):
                return None
            entries,previous_sources = self._committed_index()
            entries.update(snapshot)
            previous_sources.update(sources)
            data = json.dumps(dict(version=CACHE_VERSION,renderer=RENDERER_VERSION,
                entries=dict(entries),sources=dict(previous_sources)),sort_keys=True,separators=(',',':')).encode()
            _atomic_bytes(self.root/'index.json',data)
            self._root_state.generation += 1
            self._root_state.committed = None
            return entries,previous_sources

    def _index_bytes(self, entries):
        return json.dumps(dict(version=CACHE_VERSION, renderer=RENDERER_VERSION,
            entries=dict(entries), sources=dict(self.source_digests)), sort_keys=True,
            separators=(",", ":")).encode()

    def flush(self):
        with self.values_lock:
            _atomic_bytes(self.root / "index.json", self._index_bytes(self.entries))
            self._root_state.generation += 1
            self._root_state.committed = None

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
        with self.values_lock:
            self._root_state.epoch += 1
            self._root_state.committed = None
            self._root_epoch = self._root_state.epoch
            self.entries,self.source_digests = self._committed_index()
            for identity,entry in list(self.entries.items()):
                if predicate is None or predicate(entry):
                    self.entries.pop(identity,None)
                    self.verified.discard(identity)
            self.ready.clear()
            self.ready_bytes = 0
            self.reads.clear()
            self.verifications.clear()
            self.flush()
            keep = {entry['blob'] for entry in self.entries.values()}
            for path in (self.root/'values').glob('*.cache'):
                if path.stem not in keep:
                    path.unlink(missing_ok=True)

    @property
    def disk_bytes(self):
        unique = {entry["blob"]: entry["size"] for entry in self.entries.values()}
        return sum(unique.values())

    @property
    def pending(self):
        return bool(self.reads or self.writes or self.verifications or self.staged or self.publication or self._reload)

    def close(self):
        if self.closed:
            return
        self.drain()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.closed = True
        with self._root_state.ownership_lock:
            self._root_state.owners.discard(self)
            if not self._root_state.owners:
                self._root_state.committed = None
        self.reads.clear()
        self.verifications.clear()
        self.ready.clear()
        self.ready_bytes = 0
