"""Ordered native input packets admitted only after their source tiles exist."""
from collections import deque, OrderedDict
from contextlib import contextmanager
import time

from PySide6.QtCore import QTimer
from PySide6.QtGui import QImage

from comic_editor.ui.scene_consumers import SceneConsumers


def prepare_input_tiles(snapshot, identifier, keys):
    snapshot.finish_sources()
    owner = snapshot.tiles._tiles.get(identifier)
    from comic_editor.core.tiles import TileStore
    result = []
    for key in keys:
        if owner is not None and key in owner:
            image = QImage(owner[key])
            result.append((key, image, TileStore._alpha_bbox(image)))
    return tuple(result)


prepare_input_tiles.admission_priority = 0
prepare_input_tiles.working_bytes = lambda snapshot, _identifier, keys: len(keys) * snapshot.tiles.tile_size ** 2 * 32
prepare_input_tiles.snapshot_working_bytes = lambda *_args: 0


def prepare_input_bounds(snapshot, identifier, keys):
    from comic_editor.core.tiles import TileStore
    snapshot.finish_sources()
    owner = snapshot.tiles._tiles.get(identifier)
    return tuple((key, owner.version(key), TileStore._alpha_bbox(owner[key]))
                 for key in keys if owner is not None and key in owner)


prepare_input_bounds.admission_priority = 0
prepare_input_bounds.working_bytes = lambda snapshot, *_args: snapshot.tiles.tile_size ** 2 * 32
prepare_input_bounds.snapshot_working_bytes = lambda *_args: 0


class TileInputGate:
    """Keep packet order and native buffers through release and LRU eviction.

    ``apply`` runs on the document thread. Its declared keys must cover every
    source tile it can touch. Empty/new tiles need no preparation. The gate's
    buffers are the same QImage borrowers edited by the native paint kernel;
    retaining them does not reconstruct or downsample native source pixels.
    """
    def __init__(self, canvas, identifier, *, valid=None, cancelled=None):
        self.canvas, self.identifier = canvas, identifier
        self.chapter = canvas.chapter
        self.tiles = canvas.tiles
        self.valid = valid or (lambda: True)
        self.on_cancel = cancelled
        self.queue, self.buffers = deque(), OrderedDict()
        self.jobs = SceneConsumers(canvas)
        self.timer = QTimer(self.jobs)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.advance)
        self.pending = False
        self.closed = False
        self.released = False
        self.error = None
        self.buffer_budget = max(self.tiles.tile_size ** 2 * 8, self.tiles.residency.budget)

    @property
    def busy(self):
        return not self.closed and (self.pending or bool(self.queue))

    def current(self):
        return (not self.closed and self.canvas.chapter is self.chapter
                and self.canvas.tiles is self.tiles and self.valid())

    def submit(self, keys, apply):
        if self.closed:
            return
        self.queue.append((keys if callable(keys) else set(keys), apply))
        self.advance()

    def finish(self, apply, *, keys=()):
        self.released = True
        self.submit(keys, apply)

    def prepare_bounds(self, keys):
        """Queue metadata-only native alpha scans before frame finalization."""
        def prepare():
            requested = tuple(keys() if callable(keys) else keys)
            if not requested:
                return
            self.pending = True
            def accept(result, error):
                if error is not None:
                    self.fail(error)
                    return
                def adopt(record):
                    key, version, bounds = record
                    owner = self.tiles._tiles.get(self.identifier)
                    if owner is not None and key in owner and owner.version(key) == version:
                        self.tiles._alpha_bounds.setdefault(self.identifier, {})[key] = bounds
                        self.tiles._alpha_bounds_dirty.discard((self.identifier, *key))
                self.publish(result, adopt)
            def discard():
                self.pending = False
                self.cancel()
            self.jobs.request(('native-input', id(self)), prepare_input_bounds,
                              (self.identifier, requested), accept, ordered=True,
                              valid=self.current, discard=discard, owned_source=True)
        self.submit((), prepare)

    def prepare_resources(self, evaluator, arguments, accept):
        """Prepare immutable packet resources before subsequent native samples.

        The evaluator receives ``None`` as its scene because its arguments
        already own their source bytes. It uses ordinary worker admission and
        the same document/source-owner validity guard as tile preparation.
        """
        arguments = tuple(arguments)
        def prepare():
            self.pending = True
            def completed(result, error):
                if error is not None:
                    self.fail(error)
                    return
                try:
                    accept(result)
                except Exception as failure:
                    self.fail(failure)
                    return
                self.pending = False
                self.advance()
                self.canvas.update()
            def discard():
                self.pending = False
                self.cancel()
            self.jobs.request_detached(('native-input', id(self)), evaluator,
                arguments, completed, valid=self.current, discard=discard)
        self.submit((), prepare)

    def fail(self, error):
        self.error = self.canvas._native_input_error = error
        self.pending = False
        self.cancel()
        self.canvas.operationError.emit('Drawing failed', str(error))

    def publish(self, result, adopt):
        """Publish owned handles/metadata in bounded document-thread slices."""
        records = iter(result)
        def advance():
            if not self.current():
                self.pending = False
                self.cancel()
                return
            deadline = time.monotonic()+.004
            while time.monotonic() < deadline:
                try:
                    record = next(records)
                except StopIteration:
                    self.pending = False
                    self.advance()
                    self.canvas.update()
                    return
                adopt(record)
            QTimer.singleShot(0, self.jobs, advance)
        advance()

    @contextmanager
    def resident_sources(self):
        residency = self.tiles.residency
        previous = residency.prepare
        def prepare(owner, key):
            if owner.object_id == self.identifier:
                if key in self.buffers:
                    self.buffers.move_to_end(key)
                    if (owner, key) not in residency.entries:
                        residency.retain(owner, key, self.buffers[key])
                    return
                if key in owner and (owner, key) not in residency.entries:
                    raise RuntimeError('Input packet touched an unprepared native tile')
                return
            previous(owner, key)
        residency.prepare = prepare
        try:
            yield
        finally:
            residency.prepare = previous

    def advance(self):
        if self.pending or self.closed:
            return
        if not self.current():
            self.cancel()
            return
        deadline = time.monotonic() + .004
        while self.queue:
            owner = self.tiles._tiles.get(self.identifier)
            keys, apply = self.queue[0]
            if callable(keys):
                keys = set(keys())
                self.queue[0] = keys, apply
            cold = []
            for key in keys:
                if owner is None or key not in owner or key in self.buffers:
                    continue
                entry = owner.residency.entries.get((owner, key))
                if entry is None:
                    cold.append(key)
                else:
                    self.buffers[key] = entry[0]
            if cold:
                versions = {key: owner.version(key) for key in cold}
                self.pending = True
                def valid():
                    current = self.tiles._tiles.get(self.identifier)
                    return (self.current() and current is owner and
                            all(key in owner and owner.version(key) == version
                                for key, version in versions.items()))
                def accept(result, error):
                    if error is not None:
                        self.fail(error)
                        return
                    def adopt(record):
                        key, image, bounds = record
                        self.buffers[key] = image
                        self.tiles._alpha_bounds.setdefault(self.identifier, {})[key] = bounds
                        self.tiles._alpha_bounds_dirty.discard((self.identifier, *key))
                    self.publish(result, adopt)
                def discard():
                    self.pending = False
                    self.cancel()
                self.jobs.request(('native-input', id(self)), prepare_input_tiles,
                                  (self.identifier, tuple(cold)), accept, ordered=True,
                                  valid=valid, discard=discard, owned_source=True)
                return
            self.queue.popleft()
            try:
                with self.resident_sources():
                    apply()
            except Exception as error:
                self.fail(error)
                return
            if self.closed or self.pending:
                return
            # Newly created tiles are now native owned buffers as well.
            owner = self.tiles._tiles.get(self.identifier)
            if owner is not None:
                for key in keys:
                    entry = owner.residency.entries.get((owner, key))
                    if entry is not None:
                        self.buffers[key] = entry[0]
                        self.buffers.move_to_end(key)
            # The store remains the recoverable owner of evicted edited
            # revisions. A later packet prepares a spilled tile on the worker
            # rather than retaining a whole long gesture's native buffers.
            occupied = sum(int(image.sizeInBytes()) for image in self.buffers.values())
            while self.buffers and occupied > self.buffer_budget:
                key, image = self.buffers.popitem(last=False)
                if owner is not None and key in owner:
                    owner.version(key)
                occupied -= int(image.sizeInBytes())
            if self.queue and time.monotonic() >= deadline:
                self.timer.start(0)
                return

    def cancel(self):
        if self.closed:
            return
        self.closed = True
        self.pending = False
        self.timer.stop()
        self.queue.clear()
        self.jobs.cancel(('native-input', id(self)))
        if self.on_cancel is not None:
            callback, self.on_cancel = self.on_cancel, None
            callback()
        self.buffers.clear()
        advance = getattr(self.canvas, '_advance_native_deferred_contacts', None)
        if advance is not None:
            QTimer.singleShot(0, self.canvas, advance)

    def retire(self):
        self.closed = True
        self.timer.stop()
        self.queue.clear()
        self.buffers.clear()
        self.jobs.shutdown()
        advance = getattr(self.canvas, '_advance_native_deferred_contacts', None)
        if advance is not None:
            QTimer.singleShot(0, self.canvas, advance)
