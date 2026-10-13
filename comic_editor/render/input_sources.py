"""Capture only native source tiles required by an admitted input packet."""
from dataclasses import dataclass, field
from pathlib import Path
import time

from PySide6.QtGui import QImage, QImageReader

from comic_editor.core.tile_backing import EditableTile, SnapshotBacking, prefetch_pins
from comic_editor.render.admission import NATIVE_SOURCE_WORKSPACE
from comic_editor.render.scene import SceneSnapshot, SceneTileStore, _SourcePreparation


@dataclass(frozen=True)
class InputSourceSnapshot:
    tiles: object
    pin_jobs: tuple
    _source_preparation: object = field(default_factory=_SourcePreparation,
                                        init=False, compare=False, repr=False)

    @property
    def state(self):
        return {}

    def finish_sources(self):
        # Use the existing immutable source-pin resolver. This is input
        # preparation, not another rendering or durable caching pipeline.
        return SceneSnapshot.finish_sources(self)

    _finish_sources = SceneSnapshot._finish_sources

    def bounded_workspace(self, identifier, keys, handoff_budget, cancelled):
        """Prove the complete native packet's payload and preparation scratch.

        Only the detached worker calls this. Resolve the ordinary immutable
        pins, then inspect resident/raw metadata or PNG headers without
        decoding. Noncanonical or unknown sources keep ordinary admission.
        Returned images borrow the same decoded source buffers; all packet
        payloads remain charged together until the whole packet is ready.
        """
        self.finish_sources()
        owner = self.tiles._tiles.get(identifier)
        payload = scratch = 0
        wide = {QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied,
            QImage.Format_RGBX64, QImage.Format_RGBA16FPx4,
            QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBX16FPx4,
            QImage.Format_RGBA32FPx4, QImage.Format_RGBA32FPx4_Premultiplied,
            QImage.Format_RGBX32FPx4}
        narrow = {QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied}
        for key in keys:
            if cancelled():
                from comic_editor.render.admission import WorkCancelled
                raise WorkCancelled()
            if owner is None or key not in owner:
                continue
            resident = owner.residency.entries.get((owner, key))
            if resident is not None:
                image = resident[0]
                width, height = image.width(), image.height()
                byte_count, format = int(image.sizeInBytes()), image.format()
            else:
                value = owner.entries.get(key)
                if isinstance(value, EditableTile):
                    frozen = value.frozen
                    if frozen is None:
                        return None
                    width, height = frozen.width, frozen.height
                    byte_count, format = frozen.stride*height, frozen.format
                elif isinstance(value, Path):
                    value = owner.backing(key)  # Existing per-address immutable pin.
                    reader = QImageReader(str(value))
                    size = reader.size()
                    if bytes(reader.format()) != b'png' or not size.isValid():
                        return None
                    width, height = size.width(), size.height()
                    format = reader.imageFormat()
                    # PNG stores at most four uint16 channels. Its Qt format
                    # option may be unknown; use that complete native upper
                    # bound rather than assuming an eight-bit saved original.
                    byte_count = ((width*8+3)//4*4)*height
                    if format in narrow:
                        byte_count = ((width*4+3)//4*4)*height
                else:
                    return None
            if (not 0 < width <= self.tiles.tile_size
                    or not 0 < height <= self.tiles.tile_size or byte_count <= 0):
                return None
            pixels = width*height
            if format in wide:
                # native_rgba_pixels owns one float32 RGBA array, followed by
                # an alpha occupancy array and small axis/coordinate arrays.
                temporary = pixels*18+4096
            elif format in narrow:
                temporary = pixels*4+4096
            elif format == QImage.Format_Grayscale16:
                temporary = pixels*26+4096
            elif format == QImage.Format_Invalid:
                temporary = pixels*32+4096
            else:
                # Other packed formats can require Qt conversion and owned
                # pixel/PIL buffers; the wide path may need two float arrays.
                from comic_editor.core.pixel_arrays import image_has_high_precision
                probe = QImage(1, 1, format)
                temporary = pixels*(32 if image_has_high_precision(probe) else 12)+4096
            payload += byte_count
            # Restoring a frozen raw revision can temporarily retain its byte
            # input while Qt copies it into the unchanged native QImage.
            scratch = max(scratch, temporary, byte_count)
            if payload > handoff_budget or payload+scratch > NATIVE_SOURCE_WORKSPACE:
                return None
        return payload+scratch


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
