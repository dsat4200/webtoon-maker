"""Capture only native source tiles required by an admitted input packet."""
from dataclasses import dataclass
from pathlib import Path
import time

from PySide6.QtGui import QImage

from comic_editor.core.tile_backing import EditableTile, SnapshotBacking, prefetch_pins
from comic_editor.render.scene import SceneSnapshot, SceneTileStore


@dataclass(frozen=True)
class InputSourceSnapshot:
    tiles: object
    pin_jobs: tuple

    @property
    def state(self):
        return {}

    def finish_sources(self):
        # Use the existing immutable source-pin resolver. This is input
        # preparation, not another rendering or durable caching pipeline.
        return SceneSnapshot.finish_sources(self)


class InputSourceCapture:
    """An owned input job needs source pixels, not a chapter render snapshot.

    SceneConsumers checks the packet's positive owner/version validity before
    every slice and before publication. Unrelated projection revisions do not
    restart a native source read. No file decode or filesystem wait runs here.
    """
    def __init__(self, owner, document, identifier, keys):
        self.owner, self.document = owner, document
        self.source_store = owner.tiles
        self.identifier, self.keys = identifier, tuple(keys)
        self.source = owner.tiles._tiles.get(identifier)
        self.tiles = SceneTileStore(owner.tiles.tile_size, cache_budget=64 * 1024 * 1024)
        self.jobs, self.pending = [], []
        self.backing = None
        self.result, self.stale = None, False
        self.iterator = self._capture()

    def _pin_pending(self):
        if not self.pending:
            return
        self.backing = self.backing or SnapshotBacking()
        job = prefetch_pins(self.backing, tuple(self.pending))
        self.jobs.append(job)
        self.source_store.retain_snapshot_pins(job)
        self.pending.clear()

    def advance(self, seconds=.004):
        deadline = time.perf_counter() + max(0., seconds)
        if (self.owner.chapter is None or
                (id(self.owner.chapter), id(self.owner.tiles), id(self.owner.images)) != self.document.identity
                or self.owner.tiles._tiles.get(self.identifier) is not self.source):
            self.stale = True
            return True
        while True:
            try:
                next(self.iterator)
            except StopIteration:
                return True
            if time.perf_counter() >= deadline:
                # Register revision readers before control returns to writers.
                self._pin_pending()
                return False

    def _capture(self):
        source = self.source
        if source is not None:
            target = self.tiles._object_tiles(self.identifier)
            for key in self.keys:
                if key not in source:
                    yield
                    continue
                version = source.version(key)
                value = source.entries[key]
                if isinstance(value, Path):
                    pin = source.snapshot_pin(key, ready_only=True)
                    if pin is None:
                        target.entries[key] = value
                        self.pending.append((self.identifier, key, version, value))
                    else:
                        target.entries[key] = pin.path
                        target.snapshot_pins[key] = pin
                elif value.frozen is not None:
                    target.entries[key] = EditableTile(value.frozen)
                else:
                    resident = source.residency.entries.get((source, key))
                    if resident is None:
                        raise RuntimeError('Edited input tile has no immutable or resident pixels')
                    target[key] = QImage(resident[0])
                target.versions[key] = source.versions.get(key, 0)
                if key in source.content_keys:
                    target.content_keys[key] = source.content_keys[key]
                if len(self.pending) >= 128:
                    self._pin_pending()
                yield
        self._pin_pending()
        self.result = InputSourceSnapshot(self.tiles, tuple(self.jobs))
