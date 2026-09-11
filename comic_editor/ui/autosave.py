"""Detached recovery snapshots written serially outside the GUI thread."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, Signal

from comic_editor.core.assets import AssetManifest, AssetRepository
from comic_editor.core.images import ImageSource, ImageStore
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore


@dataclass
class RecoverySnapshot:
    root: Path
    chapter: dict
    tiles: dict
    tile_size: int
    images: dict[str, ImageSource]
    asset: dict | None = None

    @classmethod
    def capture(cls, root, chapter, tiles, images, asset=None):
        """Called on the GUI thread; QImage copies detach on subsequent edits."""
        payload = deepcopy(chapter.to_dict())
        raster_ids = {identifier for identifier, obj in chapter.objects.items()
                      if isinstance(obj, RasterObject)} | set(chapter.masks)
        image_ids = {identifier for identifier, obj in chapter.objects.items()
                     if isinstance(obj, ImageObject)}
        asset_payload = None
        if asset is not None:
            # A switched-away asset's manifest can still point at an older
            # chapter object. Always embed the chapter owned by this session.
            asset_payload = {
                "asset_schema_version": asset.schema_version,
                "id": asset.asset_id, "name": asset.name,
                "root": {"kind": asset.root_kind, "id": asset.root_id},
                "visual_bounds": list(asset.visual_bounds), "document": payload,
            }
        return cls(Path(root), payload,
                   {identifier: tiles.object_tiles(identifier) for identifier in raster_ids},
                   tiles.tile_size, images.snapshot(image_ids), asset_payload)

    def write(self):
        """Worker-only stores preserve the live document's dirty flags."""
        chapter = ChapterDocument.from_dict(self.chapter)
        tiles = TileStore(self.tile_size)
        # Saving does not need alpha bounds; avoid scanning every snapshot
        # tile just to construct a store that will only encode PNG files.
        tiles._tiles = self.tiles
        images = ImageStore()
        images.restore(self.images)
        if self.asset is None:
            SeriesRepository(self.root).save_chapter(chapter, tiles, images, autosave=True)
        else:
            manifest = AssetManifest.from_dict(self.asset)
            manifest.document = chapter
            AssetRepository(self.root).save(manifest, tiles, images=images, autosave=True)


@dataclass
class RecoveryRequest:
    scope: tuple
    revision: int
    owner: object
    snapshot: RecoverySnapshot
    name: str


class AutosaveJobs(QObject):
    completed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery-save")
        self.pending = OrderedDict()
        self.running = None
        self.closed = False
        self.submitted = 0
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)
        executor = self.executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))

    def contains(self, scope, revision):
        if self.running and self.running[0].scope == scope and self.running[0].revision == revision:
            return True
        pending = self.pending.get(scope)
        return pending is not None and pending.revision == revision

    def submit(self, request):
        if self.closed or self.contains(request.scope, request.revision):
            return False
        # Keep only the newest queued snapshot for each document.
        self.pending[request.scope] = request
        self._start()
        self.timer.start()
        return True

    def _start(self):
        if self.closed or self.running is not None or not self.pending:
            return
        _, request = self.pending.popitem(last=False)
        self.running = request, self.executor.submit(request.snapshot.write)
        self.submitted += 1

    def poll(self):
        if self.running is not None and self.running[1].done():
            request, future = self.running
            self.running = None
            error = None
            try:
                future.result()
            except Exception as caught:
                error = caught
            self.completed.emit(request, error)
        self._start()
        if self.running is None and not self.pending:
            self.timer.stop()

    def drain(self, scope=None):
        """Before explicit save/delete, discard queued work and finish its writer."""
        for key in list(self.pending):
            if scope is None or key == scope:
                self.pending.pop(key)
        if self.running is not None and (scope is None or self.running[0].scope == scope):
            _, future = self.running
            # A write already publishing files must finish its transaction.
            # Explicit Save/Close may wait; ordinary editing never waits here.
            try:
                future.result()
            except Exception:
                pass  # The normal save/open path handles any recovery marker.
            self.running = None
        self._start()
        if self.running is None and not self.pending:
            self.timer.stop()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.pending.clear()
        self.timer.stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.running = None
