"""Detached recovery snapshots written serially outside the GUI thread."""
from __future__ import annotations

from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from dataclasses import dataclass, field, fields, is_dataclass
import copy
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer, Signal

from comic_editor.core.assets import AssetManifest, AssetRepository
from comic_editor.core.images import ImageSource, ImageStore
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject, SeriesDocument
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.core.tile_backing import EditableTile, SnapshotBacking, prefetch_pins
from comic_editor.core.changes import GROUP_KINDS
from PySide6.QtGui import QImage


@dataclass
class RecoverySnapshot:
    root: Path
    chapter: dict
    tiles: dict
    tile_size: int
    images: dict[str, ImageSource]
    asset: dict | None = None
    tile_store: TileStore | None = None
    saved_tiles: dict = field(default_factory=dict)
    saved_images: dict = field(default_factory=dict)
    derived_bounds: dict = field(default_factory=dict)
    manual: bool = False
    model: ChapterDocument | None = None

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
        detached = tiles.detached_snapshot(raster_ids)
        return cls(Path(root), payload, detached._tiles,
                   tiles.tile_size, images.snapshot(image_ids), asset_payload,
                   tile_store=detached)

    def write(self):
        """Worker-only stores preserve the live document's dirty flags."""
        chapter = self.model if self.model is not None else ChapterDocument.from_dict(self.chapter)
        if self.tile_store is not None:
            # Publication may adopt destination paths. Keep the captured input
            # pinned separately so later writes cannot mutate this snapshot.
            tiles = self.tile_store.detached_snapshot(set(self.tiles))
        else:
            # Support explicitly constructed snapshots and older callers.
            tiles = TileStore(self.tile_size)
            for identifier, values in self.tiles.items():
                tiles._object_tiles(identifier).update(values)
        tiles._saved_versions = {key: dict(value) for key, value in self.saved_tiles.items()}
        images = ImageStore()
        images.restore(self.images)
        images._saved_sources = {key: dict(value) for key, value in self.saved_images.items()}
        if self.asset is None:
            SeriesRepository(self.root).save_chapter(chapter, tiles, images, autosave=not self.manual)
        else:
            asset = dict(self.asset)
            asset["document"] = chapter.to_dict()
            manifest = AssetManifest.from_dict(asset)
            manifest.document = chapter
            thumbnail = None
            if self.manual:
                from comic_editor.core.assets import entity_visual_bounds
                from comic_editor.core.models import BoundGeometry
                from comic_editor.render.outputs import capture_document, asset_thumbnail
                bounds = entity_visual_bounds(chapter, tiles, manifest.root_kind, manifest.root_id)
                manifest.visual_bounds = bounds.getRect()
                chapter.width = max(chapter.width, int(bounds.right() + 64.999))
                chapter.height = max(chapter.height, int(bounds.bottom() + 64.999))
                chapter.layers[chapter.root_page_ids[0]].bound = BoundGeometry.rectangle(0, 0, chapter.width, chapter.height)
                thumbnail = asset_thumbnail(capture_document(chapter, tiles, images), manifest)
            AssetRepository(self.root).save(manifest, tiles, thumbnail, images=images, autosave=not self.manual)
            self.asset = manifest.to_dict()
        self.chapter = chapter.to_dict()
        self.saved_tiles = {key: dict(value) for key, value in tiles._saved_versions.items()}
        self.saved_images = {key: dict(value) for key, value in images._saved_sources.items()}
        self.derived_bounds = {
            (identifier, *key): (owner.version(key), bounds)
            for identifier, owner in tiles._tiles.items()
            for key, bounds in tiles._alpha_bounds.get(identifier, {}).items()
            if key in owner and (identifier, *key) not in tiles._alpha_bounds_dirty
        }


def write_series_clone(root, destination, series, snapshots):
    """Worker transaction reuses captured originals and the ordinary writer."""
    def overlay(repository):
        for snapshot in snapshots:
            for job in getattr(snapshot, 'pin_jobs', ()):
                for identifier, key, _version, _path, pin in job.result():
                    owner = snapshot.tile_store._tiles[identifier]
                    owner.entries[key], owner.snapshot_pins[key] = pin.path, pin
            snapshot.root = repository.root
            snapshot.write()
            if snapshot.asset is None:
                for reference in series.chapters:
                    if reference.chapter_id == snapshot.model.chapter_id:
                        reference.name = snapshot.model.name
        repository.save_series(series)
    repository = SeriesRepository(root).clone_to(destination, series, overlay)
    for snapshot in snapshots:
        staging = snapshot.root
        def published(values):
            result = {}
            for directory, records in values.items():
                try:
                    relative = Path(directory).relative_to(staging)
                except ValueError:
                    continue
                result[str(repository.root / relative)] = records
            return result
        snapshot.saved_tiles = published(snapshot.saved_tiles)
        snapshot.saved_images = published(snapshot.saved_images)
        snapshot.root = repository.root
    return repository


def _staged_value(value):
    """Copy declared model data, yielding between every owned primitive."""
    from comic_editor.render.scene import detached_value
    if is_dataclass(value) and not isinstance(value, type):
        result = type(value).__new__(type(value))
        for attribute in fields(value):
            object.__setattr__(result, attribute.name,
                (yield from _staged_value(getattr(value, attribute.name))))
        return result
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            result[detached_value(key)] = yield from _staged_value(child)
        return result
    if isinstance(value, (list, tuple, set, frozenset)):
        values = []
        for child in value:
            values.append((yield from _staged_value(child)))
        return type(value)(values)
    result = detached_value(value)
    yield
    return result


def _staged_matches(value, captured):
    """Compare live preferences in bounded slices without invoking serializers."""
    if type(value) is not type(captured):
        return False
    if is_dataclass(value) and not isinstance(value, type):
        for attribute in fields(value):
            if not (yield from _staged_matches(getattr(value, attribute.name),
                                               getattr(captured, attribute.name))):
                return False
    elif isinstance(value, dict):
        if len(value) != len(captured):
            return False
        for key, child in value.items():
            if key not in captured or not (yield from _staged_matches(child, captured[key])):
                return False
    elif isinstance(value, (list, tuple)):
        if len(value) != len(captured):
            return False
        for child, previous in zip(value, captured):
            if not (yield from _staged_matches(child, previous)):
                return False
    else:
        yield
        return value == captured
    return True


class SeriesCapture:
    """Capture and verify shared preferences while ordinary editing continues."""
    def __init__(self, source, previous=None, *, valid=lambda: True):
        self.source, self.previous = source, previous
        self.result, self.stale, self.matches_previous = None, False, False
        self.valid = valid
        self.iterator = self._capture()

    def _capture(self):
        result = yield from _staged_value(self.source)
        if not (yield from _staged_matches(self.source, result)):
            self.stale = True
            return
        self.matches_previous = self.previous is not None and (
            yield from _staged_matches(result, self.previous))
        self.result = result

    def advance(self, seconds=.004):
        if not self.valid():
            self.stale = True
            return True
        deadline = time.perf_counter() + seconds
        while True:
            try:
                next(self.iterator)
            except StopIteration:
                return True
            except RuntimeError:
                # A preset mapping was resized between owner-thread slices.
                self.stale = True
                return True
            if time.perf_counter() >= deadline:
                return False


class RecoveryCapture:
    """Time-sliced model/resource ownership capture with no file decoding.

    Register revision pin batches while still on the owner thread. File IO
    completes on the existing revision-reader lane before the detached writer
    publishes replacements. A concurrent edit retires the entire partial view.
    """
    def __init__(self, root, chapter, tiles, images, asset=None, *, valid=lambda: True, manual=True):
        self.root, self.chapter, self.tiles, self.images, self.asset = root, chapter, tiles, images, asset
        self.valid, self.stale, self.result = valid, False, None
        self.manual = manual
        self.iterator = self._capture()
        self.pin_jobs = []

    def advance(self, seconds=.004):
        # Owner mutations occur between slices. Checking once avoids repeating
        # session lookup and tool-state checks for every vector coordinate.
        if not self.valid():
            self.stale = True
            return True
        deadline = time.perf_counter() + seconds
        while True:
            try:
                next(self.iterator)
            except StopIteration:
                return True
            if time.perf_counter() >= deadline:
                return False

    def _capture(self):
        chapter = type(self.chapter).__new__(type(self.chapter))
        for attribute in fields(chapter):
            value = getattr(self.chapter, attribute.name)
            if attribute.name in GROUP_KINDS:
                records = {}
                for identifier, record in value.items():
                    records[identifier] = yield from _staged_value(record)
                    yield
                setattr(chapter, attribute.name, records)
            else:
                setattr(chapter, attribute.name, (yield from _staged_value(value)))
                yield
        ids, image_ids = set(), set()
        for identifier, obj in chapter.objects.items():
            if isinstance(obj, RasterObject):
                ids.add(identifier)
            elif isinstance(obj, ImageObject):
                image_ids.add(identifier)
            yield
        for identifier in chapter.masks:
            ids.add(identifier)
            yield
        snapshot = TileStore(self.tiles.tile_size)
        pending, backing = [], None
        for identifier in ids:
            source = self.tiles._tiles.get(identifier)
            if source is None:
                continue
            target = snapshot._object_tiles(identifier)
            for key in source:
                source.version(key)
                value = source.entries[key]
                if isinstance(value, Path):
                    pin = source.snapshot_pin(key, ready_only=True)
                    if pin is not None:
                        target.entries[key], target.snapshot_pins[key] = pin.path, pin
                    else:
                        target.entries[key] = value
                        pending.append((identifier, key, source.version(key), value))
                elif value.frozen is not None:
                    target.entries[key] = EditableTile(value.frozen)
                else:
                    target[key] = QImage(source.residency.entries[(source, key)][0])
                target.versions[key] = source.versions.get(key, 0)
                if key in source.content_keys:
                    target.content_keys[key] = source.content_keys[key]
                if len(pending) >= 128:
                    backing = backing or SnapshotBacking()
                    job = prefetch_pins(backing, tuple(pending))
                    self.tiles.retain_snapshot_pins(job)
                    self.pin_jobs.append(job)
                    pending.clear()
                yield
            snapshot._alpha_bounds[identifier] = dict(self.tiles._alpha_bounds.get(identifier, {}))
        if pending:
            backing = backing or SnapshotBacking()
            job = prefetch_pins(backing, tuple(pending))
            self.tiles.retain_snapshot_pins(job)
            self.pin_jobs.append(job)
        snapshot._alpha_bounds_dirty = set(self.tiles._alpha_bounds_dirty)
        snapshot.dirty = set(self.tiles.dirty)
        asset = None if self.asset is None else {
            "asset_schema_version": self.asset.schema_version, "id": self.asset.asset_id,
            "name": self.asset.name, "root": {"kind": self.asset.root_kind, "id": self.asset.root_id},
            "visual_bounds": list(self.asset.visual_bounds)}
        result = RecoverySnapshot(Path(self.root), {}, snapshot._tiles, snapshot.tile_size,
            self.images.snapshot(image_ids), asset, tile_store=snapshot, manual=self.manual, model=chapter)
        result.saved_tiles = {key: dict(value) for key, value in self.tiles._saved_versions.items()}
        result.saved_images = {key: dict(value) for key, value in self.images._saved_sources.items()}
        result.pin_jobs = tuple(self.pin_jobs)
        self.result = result


@dataclass
class RecoveryRequest:
    scope: tuple
    revision: int
    owner: object
    snapshot: RecoverySnapshot
    name: str
    manual: bool = False
    finished: bool = False
    error: object = None
    series_supplier: object = None
    series_version_supplier: object = None
    series_payload: SeriesDocument | None = None
    series_capture: SeriesCapture | None = None
    series_written: bool = False
    preferences_only: bool = False
    preferences_root: Path | None = None


class AutosaveJobs(QObject):
    completed = Signal(object, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="recovery-save")
        self.pending = OrderedDict()
        self.running = None
        self.closed = False
        self.submitted = 0
        self.saved_resources = {}
        self.series_versions = {}
        self.captures = OrderedDict()
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.poll)
        executor = self.executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))

    def contains(self, scope, revision, *, manual=None):
        captured = self.captures.get(scope)
        if captured is not None and captured[0].revision == revision and (manual is None or captured[0].manual == manual):
            return True
        if self.running and self.running[0].scope == scope and self.running[0].revision == revision and (manual is None or self.running[0].manual == manual):
            return True
        return any(request.scope == scope and request.revision == revision and (manual is None or request.manual == manual) for request in self.pending.values())

    def submit_owned_write(self, compute, *arguments):
        """Explicit clone transactions share the serial publication owner."""
        if self.closed:
            raise RuntimeError('The save coordinator is closed')
        return self.executor.submit(compute, *arguments)

    def supersede(self, scope):
        """Retire older queued captures without waiting for an active writer.

        Asset replacement queues on this same serial owner. An active writer
        therefore completes first; unfinished queued captures must not follow
        that replacement with the old library contents.
        """
        for key, request in tuple(self.pending.items()):
            if request.scope == scope:
                self.pending.pop(key)
                request.finished, request.error = True, ValueError('Save was superseded')
        captured = self.captures.pop(scope, None)
        if captured is not None:
            captured[0].finished, captured[0].error = True, ValueError('Save was superseded')
        self.saved_resources.pop(scope, None)

    def submit(self, request):
        if self.closed or self.contains(request.scope, request.revision, manual=request.manual):
            return False
        if not request.manual and self.contains(request.scope, request.revision, manual=True):
            return False
        if request.manual:
            for key, old in tuple(self.pending.items()):
                if old.scope == request.scope and old.revision <= request.revision:
                    self.pending.pop(key)
                    old.finished, old.error = True, ValueError("Save was superseded")
        # Keep only the newest queued snapshot for each document.
        self.pending[(request.scope, request.manual)] = request
        self._start()
        self.timer.start()
        return True

    def capture_manual(self, request, source):
        return self.capture_request(request, source)

    def capture_request(self, request, source):
        """source() returns current session-owned inputs and edit revision."""
        if self.closed:
            return False
        previous = self.captures.get(request.scope)
        if previous is not None:
            if previous[0].manual and not request.manual:
                return False
            previous[0].finished, previous[0].error = True, ValueError("Save was superseded")
        self.captures[request.scope] = request, source, None
        self.timer.start(8)
        return True

    def manual_request(self, scope, revision):
        captured = self.captures.get(scope)
        requests = ([captured[0]] if captured is not None else []) + list(self.pending.values())
        if self.running is not None:
            requests.append(self.running[0])
        return next((request for request in requests if request.manual and request.scope == scope
                     and request.revision == revision and not request.finished), None)

    def save_preferences(self, root, series):
        """Coalesce shared metadata on the same publication lane as documents."""
        if self.closed:
            return None
        scope = (str(Path(root)), 'preferences', series.series_id)
        self.series_versions[scope] = self.series_versions.get(scope, 0) + 1
        requests = list(self.pending.values())
        if self.running is not None:
            requests.append(self.running[0])
        existing = next((item for item in requests if item.scope == scope), None)
        if existing is not None:
            existing.series_supplier = lambda: series
            return existing
        request = RecoveryRequest(scope, 0, None, None, series.name,
            series_supplier=lambda: series, series_version_supplier=lambda: self.series_versions[scope],
            preferences_only=True, preferences_root=Path(root))
        self.submit(request)
        return request

    def series_version(self, root, series):
        return self.series_versions.get((str(Path(root)), 'preferences', series.series_id), 0)

    def _advance_capture(self, seconds=.004):
        try:
            self._advance_capture_impl(seconds)
        except Exception as error:
            if self.captures:
                _scope, (request, _source, _capture) = self.captures.popitem(last=False)
                request.finished, request.error = True, error
                self.completed.emit(request, error)

    def _advance_capture_impl(self, seconds):
        if not self.captures:
            return
        scope, (request, source, capture) = next(iter(self.captures.items()))
        values = source()
        if values is None:
            self.captures.pop(scope)
            request.finished, request.error = True, ValueError("Save owner is no longer open")
            self.completed.emit(request, request.error)
            return
        root, chapter, tiles, images, asset, revision = values[:6]
        if len(values) > 6 and not values[6]:
            # A new direct-pixel/model tool transaction started after Save.
            # Never publish its unfinished state or retain a mixed partial view.
            self.captures[scope] = request, source, None
            self.captures.move_to_end(scope)
            return
        if capture is None:
            request.revision = revision
            identities = id(chapter), id(tiles), id(images), revision
            def valid():
                current = source()
                return current is not None and (len(current) <= 6 or current[6]) and (id(current[1]), id(current[2]), id(current[3]), current[5]) == identities
            capture = RecoveryCapture(root, chapter, tiles, images, asset, valid=valid, manual=request.manual)
            self.captures[scope] = request, source, capture
        if capture.advance(seconds):
            if capture.stale:
                self.captures[scope] = request, source, None
            else:
                request.snapshot = capture.result
                self.captures.pop(scope)
                if not self.submit(request):
                    request.finished, request.error = True, ValueError("Save was superseded")
        if scope in self.captures:
            self.captures.move_to_end(scope)

    def _start(self):
        if self.closed or self.running is not None or not self.pending:
            return
        _, request = self.pending.popitem(last=False)
        saved = self.saved_resources.get(request.scope)
        if saved is not None and not request.preferences_only:
            request.snapshot.saved_tiles.update(saved[0])
            request.snapshot.saved_images.update(saved[1])
        def write():
            if request.preferences_only:
                return
            for job in getattr(request.snapshot, "pin_jobs", ()):
                for identifier, key, version, path, pin in job.result():
                    owner = request.snapshot.tile_store._tiles[identifier]
                    owner.entries[key], owner.snapshot_pins[key] = pin.path, pin
            request.snapshot.write()
        self.running = request, self.executor.submit(write)
        if not request.preferences_only:
            self.submitted += 1

    def poll(self):
        self._advance_capture()
        if self.running is not None and self.running[1].done():
            request, future = self.running
            error = None
            try:
                future.result()
            except Exception as caught:
                error = caught
            if error is None and (request.manual or request.preferences_only) and request.series_supplier is not None:
                source = request.series_supplier()
                capture = request.series_capture
                if capture is None or capture.source is not source:
                    version = request.series_version_supplier() if request.series_version_supplier is not None else None
                    valid = lambda: (request.series_version_supplier is None
                                     or request.series_version_supplier() == version)
                    capture = request.series_capture = SeriesCapture(source, request.series_payload, valid=valid)
                if not capture.advance():
                    return
                request.series_capture = None
                if capture.stale:
                    return
                if not request.series_written or not capture.matches_previous:
                    # Capture/verify current shared preferences in owner slices;
                    # validation, serialization and publication stay detached.
                    payload = request.series_payload = capture.result
                    request.series_written = True
                    root = request.preferences_root if request.preferences_only else request.snapshot.root
                    def write_series():
                        detached = deepcopy(payload)
                        if not request.preferences_only:
                            for reference in detached.chapters:
                                if reference.chapter_id == request.scope[2]:
                                    reference.name = request.snapshot.chapter['name']
                        SeriesRepository(root).save_series(detached)
                    self.running = request, self.executor.submit(write_series)
                    return
            self.running = None
            if error is None and not request.preferences_only:
                self.saved_resources[request.scope] = (
                    request.snapshot.saved_tiles, request.snapshot.saved_images)
                live_tiles = getattr(request.owner, 'tiles', None)
                if live_tiles is not None:
                    live_tiles.adopt_snapshot_bounds(request.snapshot.derived_bounds)
            request.finished, request.error = True, error
            self.completed.emit(request, error)
        self._start()
        if self.running is None and not self.pending and not self.captures:
            self.timer.stop()

    def wait(self, request):
        """Only destructive close/switch flows wait for actual publication."""
        while not request.finished:
            # Released async tools publish via queued owner callbacks. Drain
            # those before freezing their source, without accepting new input.
            QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            self._advance_capture(float("inf"))
            self._start()
            if self.running is not None:
                try:
                    self.running[1].result()
                except Exception:
                    pass
            self.poll()
            if self.running is None and not request.finished:
                time.sleep(.002)
        return request.error is None

    def drain(self, scope=None):
        """Before explicit save/delete, discard queued work and finish its writer."""
        for key, request in tuple(self.pending.items()):
            if scope is None or request.scope == scope:
                self.pending.pop(key)
        for key, (request, _source, _capture) in tuple(self.captures.items()):
            if scope is None or key == scope:
                self.captures.pop(key)
                request.finished, request.error = True, ValueError("Save was discarded")
        if self.running is not None and (scope is None or self.running[0].scope == scope):
            _, future = self.running
            # A write already publishing files must finish its transaction.
            # Explicit Save/Close may wait; ordinary editing never waits here.
            try:
                future.result()
            except Exception:
                pass  # The normal save/open path handles any recovery marker.
            self.running = None
        if scope is None:
            self.saved_resources.clear()
        else:
            self.saved_resources.pop(scope, None)
        self._start()
        if self.running is None and not self.pending:
            self.timer.stop()

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        self.pending.clear()
        self.captures.clear()
        self.timer.stop()
        self.executor.shutdown(wait=True, cancel_futures=True)
        self.running = None
        self.saved_resources.clear()
