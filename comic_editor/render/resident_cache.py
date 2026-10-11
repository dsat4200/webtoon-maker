"""Shared exact RAM values, using the ordinary render cache descriptors.

This tier neither renders nor defines dependency/invalidation keys. Private
preview values never reach it: callers retain through the existing exact gates.
"""
from collections import OrderedDict
from dataclasses import dataclass
from threading import RLock
import copy
import os

import numpy as np
from PySide6.QtGui import QImage


def default_memory_budget():
    """Use a bounded part of system RAM; shared GPU memory is the same RAM."""
    total = 8 * 1024**3
    try:
        if os.name == 'nt':
            import ctypes
            class MemoryStatus(ctypes.Structure):
                _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong),
                    ('physical', ctypes.c_ulonglong), ('available', ctypes.c_ulonglong),
                    ('page', ctypes.c_ulonglong), ('available_page', ctypes.c_ulonglong),
                    ('virtual', ctypes.c_ulonglong), ('available_virtual', ctypes.c_ulonglong),
                    ('extended', ctypes.c_ulonglong)]
            status = MemoryStatus()
            status.length = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                total = status.physical
        else:
            total = os.sysconf('SC_PAGE_SIZE') * os.sysconf('SC_PHYS_PAGES')
    except (AttributeError, OSError, ValueError):
        pass
    return max(256 * 1024**2, min(8 * 1024**3, int(total * .20)))


@dataclass(frozen=True)
class ResidentEntry:
    descriptor: object
    value: object
    state: object
    size: int


class ResidentRenderCache:
    """One bounded RAM budget across open projects and render workers."""
    def __init__(self, budget=None, *, metadata_limit=65536):
        self.budget = default_memory_budget() if budget is None else max(0, int(budget))
        self.entries = OrderedDict()
        self.bytes = 0
        self.lock = RLock()
        self.sources = {}
        self.pixels = OrderedDict()
        self.metadata_limit = min(65536, max(0, int(metadata_limit)))
        self.eligible = set()
        self.evictions = 0
        self.generations = {}

    def view(self, namespace):
        return ResidentCacheView(self, str(namespace))


class ResidentCacheView:
    def __init__(self, owner, namespace):
        self.owner, self.namespace = owner, namespace
        with owner.lock:
            self.generation = owner.generations.setdefault(namespace, 0)

    def _current(self):
        # Checked only while holding the owner lock, together with every read
        # or write. A late worker cannot publish into a reopened namespace.
        return self.owner.generations.get(self.namespace) == self.generation

    def _marker(self, descriptor):
        return self.namespace, descriptor.identity

    def has(self, descriptor):
        with self.owner.lock:
            return self._current() and self._marker(descriptor) in self.owner.entries

    def lookup(self, descriptor):
        with self.owner.lock:
            if not self._current():
                return None
            marker = self._marker(descriptor)
            entry = self.owner.entries.get(marker)
            if entry is None:
                return None
            self.owner.entries.move_to_end(marker)
            return QImage(entry.value) if isinstance(entry.value, QImage) else entry.value

    def state(self, descriptor):
        with self.owner.lock:
            if not self._current():
                return None
            entry = self.owner.entries.get(self._marker(descriptor))
            return copy.deepcopy(entry.state) if entry is not None else None

    def retain(self, descriptor, value, state=None):
        from .cache import _freeze, canonical
        marker = self._marker(descriptor)
        with self.owner.lock:
            if not self._current():
                return False
            if marker in self.owner.entries:
                self.owner.entries.move_to_end(marker)
                return True
            if isinstance(value, QImage):
                if value.isNull():
                    return False
                size = value.sizeInBytes()
            elif isinstance(value, np.ndarray) and not value.dtype.hasobject:
                size = value.nbytes
            else:
                return False
            if size <= 0 or size > self.owner.budget:
                return False
            # COW images and owned read-only arrays retain the original format,
            # sampling and color/precision data; no display conversion occurs.
            frozen = _freeze(value)
            while self.owner.bytes + size > self.owner.budget and self.owner.entries:
                _, previous = self.owner.entries.popitem(last=False)
                self.owner.bytes -= previous.size
                self.owner.evictions += 1
            self.owner.entries[marker] = ResidentEntry(descriptor, frozen, canonical(state), size)
            self.owner.bytes += size
            return True

    @property
    def bytes(self):
        with self.owner.lock:
            if not self._current():
                return 0
            return sum(entry.size for (namespace, _), entry in self.owner.entries.items()
                       if namespace == self.namespace)

    def snapshot(self):
        with self.owner.lock:
            if not self._current():
                return ()
            return tuple(entry for (namespace, _), entry in self.owner.entries.items()
                         if namespace == self.namespace)

    def clear(self, predicate=None):
        with self.owner.lock:
            if not self._current():
                return
            for marker, entry in list(self.owner.entries.items()):
                if marker[0] == self.namespace and (predicate is None or predicate(entry)):
                    self.owner.entries.pop(marker)
                    self.owner.bytes -= entry.size

    def remember_sources(self, sources):
        with self.owner.lock:
            if not self._current():
                return
            self.owner.sources.setdefault(self.namespace, {}).update(copy.deepcopy(sources))

    def remember_source(self, logical, record):
        """Publish one ordinarily sealed source record without copying a tree."""
        with self.owner.lock:
            if not self._current():
                return
            sources = self.owner.sources.setdefault(self.namespace, {})
            if sources.get(logical) != record:
                sources[logical] = copy.deepcopy(record)

    def source_digests(self):
        with self.owner.lock:
            if not self._current():
                return {}
            return copy.deepcopy(self.owner.sources.get(self.namespace, {}))

    def remember_pixel_identity(self, object_id, address, version, path, known):
        with self.owner.lock:
            if not self._current():
                return
            marker = self.namespace, object_id, tuple(address)
            self.owner.pixels[marker] = version, str(path), known
            self.owner.pixels.move_to_end(marker)
            while len(self.owner.pixels) > self.owner.metadata_limit:
                self.owner.pixels.popitem(last=False)

    def pixel_identity(self, object_id, address):
        with self.owner.lock:
            if not self._current():
                return None
            marker = self.namespace, object_id, tuple(address)
            previous = self.owner.pixels.get(marker)
            if previous is not None:
                self.owner.pixels.move_to_end(marker)
            return previous

    def clear_runtime(self):
        """Forget version-only pixel witnesses when source ownership changes.

        Runtime revisions can repeat after reopening a chapter. Sealed file
        records and exact descriptor values remain independently reusable under
        their ordinary content checks; pixel witnesses never cross that bind.
        """
        with self.owner.lock:
            if not self._current():
                return
            for marker in tuple(self.owner.pixels):
                if marker[0] == self.namespace:
                    self.owner.pixels.pop(marker)

    def retire(self):
        """Release a closed namespace and invalidate every outstanding view.

        Ordinary clear keeps the current lifetime. Retirement also drops source
        witnesses and changes the lifetime so old native or metadata workers
        cannot reinsert values after the project closes or reopens.
        """
        with self.owner.lock:
            if not self._current():
                return False
            self.clear()
            self.clear_runtime()
            self.owner.sources.pop(self.namespace, None)
            self.owner.eligible.discard(self.namespace)
            self.owner.generations[self.namespace] = self.generation + 1
            return True
