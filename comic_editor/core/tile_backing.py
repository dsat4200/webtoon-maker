"""Lazy tile mappings with bounded residency for saved and edited pixels.

The owner uses these on its document thread. Evicted edits use private lossless
storage until their project save commits; eviction never discards paint.
"""
from collections import OrderedDict
from collections.abc import MutableMapping
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
import os
from threading import RLock
import tempfile
import uuid

from PySide6.QtGui import QImage


_snapshot_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix='tile-pins')
_reader_lock = RLock()
_reader_jobs = {}


class PrefetchedPins:
    def __init__(self, backing, records):
        self.backing, self.lock, self.values = backing, RLock(), {}
        self.records = {(identifier, key): (version, path) for identifier, key, version, path in records}
        self.future = _snapshot_executor.submit(self._run, records)

    def pin(self, identifier, key):
        address = identifier, key
        with self.lock:
            future = self.values.get(address)
            create = future is None
            if create:
                future = self.values[address] = Future()
        # Filesystem work stays outside the shared bookkeeping lock. A visible
        # tile can overtake unrelated queued I/O, while duplicate requests for
        # the same file share exactly one immutable pin.
        if create:
            try:
                future.set_result(self.backing.pin(self.records[address][1]))
            except Exception as error:
                future.set_exception(error)
        return future.result()

    def _run(self, records):
        return [(identifier, key, version, path, self.pin(identifier, key))
                for identifier, key, version, path in records]

    def done(self):
        return self.future.done()

    def ready_pin(self, identifier, key):
        """Read a completed address without starting or waiting for file IO."""
        with self.lock:
            future = self.values.get((identifier, key))
        if future is None or not future.done():
            return None
        return future.result()

    def result(self):
        return self.future.result()


def prefetch_pins(backing, records):
    """Detached filesystem work; no live mappings or pixel buffers are read."""
    job = PrefetchedPins(backing, records)
    future = job.future
    root = Path(os.path.commonpath([str(path.parent) for _, _, _, path in records])).resolve()
    with _reader_lock:
        _reader_jobs[future] = root
    def finished(job):
        with _reader_lock:
            _reader_jobs.pop(job, None)
    future.add_done_callback(finished)
    return job


def finish_revision_readers(root):
    """Before publishing, let earlier loads pin their immutable file revision."""
    root = root.resolve()
    with _reader_lock:
        futures = [job for job, source in _reader_jobs.items()
                   if source.is_relative_to(root) or root.is_relative_to(source)]
    for job in futures:
        job.result()


class SnapshotBacking:
    """A shared directory whose files live exactly as long as their pins."""
    def __init__(self):
        self.workspace = tempfile.TemporaryDirectory(prefix='comic-recovery-tiles-')
        self.root = Path(self.workspace.name)

    def pin(self, path):
        from .persistence import link_or_copy
        target = self.root / f'{uuid.uuid4().hex}.png'
        link_or_copy(str(path), str(target))
        return PinnedTile(self, target)


class PinnedTile:
    def __init__(self, backing, path):
        self.backing, self.path = backing, path

    def __del__(self):
        try:
            self.path.unlink(missing_ok=True)
        except OSError:
            # The directory owner also cleans up at final release.
            pass


class TileResidency:
    def __init__(self, budget=256 * 1024 * 1024):
        self.budget = max(0, int(budget))
        self.entries = OrderedDict()
        self.bytes = 0
        self.decodes = self.hits = self.evictions = 0
        self.prepare = lambda owner, key: None
        self._spill_cache = None

    def remove(self, owner, key):
        entry = self.entries.pop((owner, key), None)
        if entry is not None:
            self.bytes -= int(entry[0].sizeInBytes())

    def clear(self, *, discard=False):
        if discard:
            self.entries.clear()
            self.bytes = 0
        else:
            for owner, key in tuple(self.entries):
                self.evict(owner, key)

    def evict(self, owner, key):
        entry = self.entries.get((owner, key))
        if entry is None:
            return
        owner.adopt_edit(key, *entry)
        value = owner.entries.get(key)
        if isinstance(value, EditableTile):
            if self._spill_cache is None:
                from .tile_history import TileHistoryCache
                self._spill_cache = TileHistoryCache(0)
            if value.frozen is None:
                value.frozen = self._spill_cache.capture(entry[0])
        self.remove(owner, key)
        self.evictions += 1

    def retain(self, owner, key, image):
        self.remove(owner, key)
        self.entries[(owner, key)] = image, int(image.cacheKey())
        self.bytes += int(image.sizeInBytes())
        # A caller may be about to paint into the just-borrowed image. Retain
        # that one tile even with a zero test budget; all older tiles can spill.
        while self.bytes > self.budget and len(self.entries) > 1:
            previous_owner, previous_key = next(iter(self.entries))
            self.evict(previous_owner, previous_key)

    def get(self, owner, key, path):
        address = (owner, key)
        entry = self.entries.get(address)
        if entry is not None:
            self.hits += 1
            self.entries.move_to_end(address)
            return entry[0]
        if isinstance(path, EditableTile):
            if path.frozen is None:
                raise OSError('Edited raster tile has no recoverable pixels')
            image = path.frozen.image()
        else:
            image = QImage(str(path))
            if image.isNull():
                raise OSError(f'Unable to read raster tile {path}')
            from .pixel_arrays import image_has_high_precision
            if not image_has_high_precision(image):
                image = image.convertToFormat(QImage.Format_ARGB32_Premultiplied)
        self.decodes += 1
        self.retain(owner, key, image)
        return image


class EditableTile:
    """An edited revision; resident borrowers or a private immutable raw pin own its pixels."""
    def __init__(self, frozen=None):
        self.frozen = frozen


class DiskTileMap(MutableMapping):
    __hash__ = object.__hash__
    __eq__ = object.__eq__

    def __init__(self, object_id, residency, changed):
        self.object_id = object_id
        self.residency = residency
        self.changed = changed
        self.entries = {}
        self.versions = {}
        self.content_keys = {}
        self.snapshot_pins = {}
        self.pending_snapshot_pins = {}

    def register(self, key, path):
        self.snapshot_pins.pop(key, None)
        self.pending_snapshot_pins.pop(key, None)
        self.entries[key] = Path(path)
        self.versions[key] = 0

    def adopt_edit(self, key, image, original_key):
        content_key = int(image.cacheKey())
        if content_key != original_key and key in self.entries:
            self.snapshot_pins.pop(key, None)
            self.pending_snapshot_pins.pop(key, None)
            self.entries[key] = EditableTile()
            self.content_keys[key] = content_key
            self.versions[key] = self.versions.get(key, 0) + 1
            if (self, key) in self.residency.entries:
                self.residency.entries[(self, key)] = image, content_key
            self.changed((self.object_id, *key))

    def version(self, key):
        entry = self.residency.entries.get((self, key))
        if entry is not None:
            self.adopt_edit(key, *entry)
        return self.versions.get(key, 0), self.content_keys.get(key, 0)

    def commit(self, key, path):
        if key in self.entries:
            self.version(key)
            self.entries[key] = Path(path)
            self.residency.remove(self, key)

    def __getitem__(self, key):
        # A resident borrower already owns its pixels. Pending snapshot file
        # ownership must not delay painting or force filesystem work here.
        if (self, key) in self.residency.entries:
            return self.residency.get(self, key, self.entries[key])
        self.residency.prepare(self, key)
        value = self.entries[key]
        if isinstance(value, Path):
            pin = self.snapshot_pin(key)
            return self.residency.get(self, key, pin.path if pin is not None else value)
        return self.residency.get(self, key, value)

    def __setitem__(self, key, image):
        self.snapshot_pins.pop(key, None)
        self.pending_snapshot_pins.pop(key, None)
        self.entries[key] = EditableTile()
        self.content_keys[key] = int(image.cacheKey())
        self.residency.retain(self, key, image)
        self.versions[key] = self.versions.get(key, 0) + 1
        self.changed((self.object_id, *key))

    def __delitem__(self, key):
        self.snapshot_pins.pop(key, None)
        self.pending_snapshot_pins.pop(key, None)
        del self.entries[key]
        self.residency.remove(self, key)
        self.versions[key] = self.versions.get(key, 0) + 1
        self.changed((self.object_id, *key))

    def clear(self):
        # MutableMapping.clear() first reads each value through popitem().
        # Retiring a cold mapping must only discard its ownership; decoding
        # originals here would turn prepared replacement back into GUI IO.
        for key in tuple(self.entries):
            del self[key]

    def __iter__(self):
        return iter(self.entries)

    def __len__(self):
        return len(self.entries)

    def __contains__(self, key):
        return key in self.entries

    def snapshot_pin(self, key, *, ready_only=False):
        """Adopt a captured pin on the owner thread, only for matching pixels.

        The pending job keeps the original alive before save completion, even
        when the writer has already removed its recovery filename. Capture
        only adopts ready addresses; a cold visible read can prioritize its IO.
        """
        pin = self.snapshot_pins.get(key)
        if pin is not None:
            self.pending_snapshot_pins.pop(key, None)
            return pin
        pending = self.pending_snapshot_pins.get(key)
        if pending is None:
            return None
        version, path, job = pending
        if (self.version(key), self.entries.get(key)) != (version, path):
            self.pending_snapshot_pins.pop(key, None)
            return None
        if ready_only:
            pin = job.ready_pin(self.object_id, key)
            if pin is None:
                return None
        else:
            pin = job.pin(self.object_id, key)
        self.snapshot_pins[key] = pin
        self.pending_snapshot_pins.pop(key, None)
        return pin

    def backing(self, key):
        value = self.entries.get(key)
        if not isinstance(value, Path):
            return None
        pin = self.snapshot_pin(key)
        return pin.path if pin is not None else value
