"""Immutable original-file pins with a bounded cache of encoded image bytes."""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
import uuid

from .tile_backing import SnapshotBacking, PinnedTile


class EncodedImageCache:
    def __init__(self, budget=64*1024*1024):
        self.budget, self.bytes = max(0, int(budget)), 0
        self._values, self._lock = OrderedDict(), RLock()
        self._backing = None

    @property
    def backing(self):
        with self._lock:
            if self._backing is None:
                self._backing = SnapshotBacking()
            return self._backing

    def retain(self, key, data):
        with self._lock:
            old = self._values.pop(key, None)
            if old is not None:
                self.bytes -= len(old)
            if len(data) <= self.budget:
                while self._values and self.bytes + len(data) > self.budget:
                    self.bytes -= len(self._values.popitem(last=False)[1])
                self._values[key] = data
                self.bytes += len(data)

    def read(self, pin):
        key = str(pin.path)
        with self._lock:
            result = self._values.get(key)
            if result is not None:
                self._values.move_to_end(key)
                return result
        # Pins own their backing through this read. File I/O never holds the
        # cache lock, so decoding one import cannot block an unrelated lookup.
        result = pin.path.read_bytes()
        self.retain(key, result)
        return result


@dataclass(frozen=True)
class EncodedImage:
    """Snapshots share this value; its original file bytes never change."""
    cache: EncodedImageCache
    pin: PinnedTile

    def __deepcopy__(self, memo):
        return self

    @classmethod
    def from_file(cls, path, cache):
        return cls(cache, cache.backing.pin(path))

    @classmethod
    def from_bytes(cls, data, cache):
        backing = cache.backing
        path = backing.root / f'{uuid.uuid4().hex}.encoded'
        # Private session storage is published only after the file is closed.
        # Project saves retain their transactional fsync/replace contract.
        path.write_bytes(data)
        pin = PinnedTile(backing, path)
        cache.retain(str(path), data)
        return cls(cache, pin)

    @property
    def data(self):
        return self.cache.read(self.pin)


DEFAULT_ENCODED_CACHE = EncodedImageCache()
