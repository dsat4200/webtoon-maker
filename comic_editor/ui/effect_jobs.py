"""Latest-request-wins effect work; workers never read mutable canvas state."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from dataclasses import dataclass, field
from threading import Event

from PySide6.QtCore import QObject, QTimer

from comic_editor.ui.radial_blur import RadialRenderCancelled


_MISSING = object()


def _navigator_job_relevant(scope, key=None):
    """Only known channel-local artwork can be irrelevant to Navigator.

    Original source decode/color handoffs are shared. Unknown scopes remain
    conservative, while the existing canvas/navigator stage namespaces retain
    their ordinary semantic pixel dependencies and cache keys.
    """
    if isinstance(scope, tuple) and scope and isinstance(scope[0], str):
        if scope[0] == 'source-image-decode':
            return True
        if (scope[0] in {'object', 'layer'} and len(scope) >= 3
                and isinstance(scope[2], str)):
            return scope[2] not in {'canvas', 'overflow', 'posterize-statistics'}
        if (scope[0] in {'tile-graph', 'tile-frame', 'mesh-region', 'pattern-frame'}
                and len(scope) > 1 and isinstance(scope[1], tuple)):
            return _navigator_job_relevant(scope[1], key)
    return True


@dataclass
class _RegionAssembly:
    image: object
    metadata_bytes: int
    covered: set = field(default_factory=set)
    complete: bool = False
    bounds: object = None
    tile_size: int | None = None


class _RegionStore:
    """Owning-thread, context-checked access to private exact frame progress."""
    def __init__(self, jobs, context, current):
        self.jobs, self.context, self.current_context = jobs, context, current
        self.generation = jobs._region_generation

    def _check(self):
        if (self.generation != self.jobs._region_generation
                or self.context != self.jobs._region_context
                or self.context != self.current_context()):
            from comic_editor.ui.async_projection import ProjectionPending
            raise ProjectionPending('stale-frame-assembly', self.context)

    def validate(self):
        self._check()

    def get(self, key):
        self._check()
        entry = self.jobs._regions.get(key)
        if entry is not None:
            self.jobs._regions.move_to_end(key)
        return entry

    def current(self, key, entry):
        self._check()
        return self.jobs._regions.get(key) is entry

    def begin(self, key, requested, image_format, tile_count, *, tile_size=None):
        self._check()
        return self.jobs._region_begin(key, requested, image_format, tile_count, tile_size=tile_size)

    def crop(self, key, entry, requested):
        """Detach only a fully covered native rectangle from private progress.

        The producer declares its canonical grid when it begins the frame. A
        crop proves coverage itself; a caller cannot supply an incomplete set
        of addresses or promote the unfinished full image to an exact result.
        """
        import math
        from PySide6.QtCore import QRectF
        from comic_editor.ui.async_projection import ProjectionPending
        self._check()
        if self.jobs._regions.get(key) is not entry:
            raise ProjectionPending('displaced-frame-assembly', key)
        rect = QRectF(requested)
        if (entry.tile_size is None or entry.tile_size <= 0 or rect.isEmpty()
                or rect != QRectF(rect.toAlignedRect()) or not entry.bounds.contains(rect)):
            raise ValueError('Private crops require a declared native grid and an aligned in-frame rectangle')
        step = entry.tile_size
        for y in range(math.floor(rect.top() / step), math.ceil(rect.bottom() / step)):
            for x in range(math.floor(rect.left() / step), math.ceil(rect.right() / step)):
                if (x, y) not in entry.covered:
                    raise ProjectionPending('incomplete-frame-crop', key)
        # QImage.copy detaches pixels. The still-incomplete parent stays private
        # and budgeted; only this independently proven exact rectangle escapes.
        relative = rect.translated(-entry.bounds.topLeft()).toAlignedRect()
        result = entry.image.copy(relative)
        if result.isNull():
            raise MemoryError('Could not allocate exact frame crop')
        return result

    def finish(self, key, entry):
        from PySide6.QtGui import QImage
        self._check()
        if self.jobs._regions.get(key) is not entry:
            from comic_editor.ui.async_projection import ProjectionPending
            raise ProjectionPending('displaced-frame-assembly', key)
        if not entry.complete:
            # Until complete, the mutable image is private and accounted as a
            # working buffer. Only now may a COW handle reach a stage worker.
            entry.complete = True
            size, storage = int(entry.image.sizeInBytes()), int(entry.image.cacheKey())
            self.jobs.retained_bytes -= size
            self.jobs._retain_storage(storage, size)
        return QImage(entry.image)


class EffectJobs(QObject):
    def __init__(self, canvas, budget=256 * 1024 * 1024, retained_budget=256 * 1024 * 1024,
                 retained_limit=512, *, workers=1):
        super().__init__(canvas)
        self.canvas = canvas
        self.budget = budget
        self.pending = OrderedDict()
        self.waiting = OrderedDict()
        self.worker_limit = max(1, int(workers))
        self._running = OrderedDict()
        self._job_serial = 0
        self.retry_on_release = False
        self._navigator_retry_relevance = None
        self._navigator_derived_relevance = None
        self._navigator_derived_dependency = None
        self._navigator_derived_retry = False
        self.exact_failures = OrderedDict()
        # The ordinary scene LRU also contains previews and source images.
        # Exact worker results and the latest completed stage need a separate
        # handoff so repainting another target cannot restart a whole stack.
        self.retained = OrderedDict()
        self.retained_bytes = 0
        self.retained_budget = retained_budget
        self.retained_limit = max(1, int(retained_limit))
        self._retained_images = {}
        # Partial and completed native frame assemblies share the retained
        # pool's byte and record limits. Partials have no semantic cache entry
        # and are never sent to disk, signals, or detached stage workers.
        self._regions = OrderedDict()
        self._region_context = None
        self._region_generation = 0
        # Shared stage prefixes have many regional consumers. Protect a bounded
        # portion of the existing pool from one-use viewport captures, rather
        # than growing the pool or pinning a whole frame without a byte limit.
        self._retained_shared = OrderedDict()
        self._retained_shared_images = {}
        self.retained_shared_bytes = 0
        self.executor = ThreadPoolExecutor(max_workers=self.worker_limit, thread_name_prefix="effect-preview")
        self.stopped = Event()
        self.timer = QTimer(self)
        self.timer.setInterval(16)
        self.timer.timeout.connect(self.poll)
        # The callback captures only the executor, not a deleted QObject.
        executor = self.executor
        stopped = self.stopped
        self.destroyed.connect(lambda: (stopped.set(), executor.shutdown(wait=False, cancel_futures=True)))
        self.submitted = self.completed = self.discarded = 0

    @property
    def bytes_in_flight(self):
        return sum(job[4] for job in self.pending.values()) + sum(job[4] for job in self._running.values())

    @property
    def running(self):
        """Compatibility observation of the oldest job; use running_jobs to inspect all."""
        return next(iter(self._running.values()), None)

    @property
    def running_jobs(self):
        return tuple(self._running.values())

    @property
    def has_finished(self):
        return any(job[3].done() for job in self._running.values())

    def has_running(self, scope=_MISSING, key=_MISSING):
        return any(not job[2].is_set() and (scope is _MISSING or job[0] == scope)
                   and (key is _MISSING or job[1] == key) for job in self._running.values())

    def retained_get(self, scope, key):
        from PySide6.QtGui import QImage
        from comic_editor.ui.cache_dependencies import cache_get, cache_put, backing_for
        entry = self.retained.get(scope)
        if entry is None or entry[0] != key:
            image = cache_get(self.canvas, "retained", ("retained", scope, key))
            if image is None:
                return None
            backing = backing_for(self.canvas)
            descriptor = backing.descriptor("retained", ("retained", scope, key))
            from comic_editor.render.cache import restore_state
            state = restore_state(backing.entries.get(descriptor.identity, {}).get("state"))
            self.retained_put(scope, key, image, state)
            return QImage(image), state
        self.retained.move_to_end(scope)
        if scope in self._retained_shared:
            self._retained_shared.move_to_end(scope)
        cache_put(self.canvas, "retained", ("retained", scope, key), entry[1], state=entry[2])
        return QImage(entry[1]), entry[2]

    def _retained_unprotect(self, scope):
        storage = self._retained_shared.pop(scope, None)
        if storage is not None:
            self._retained_shared_images[storage][1] -= 1
            if not self._retained_shared_images[storage][1]:
                self.retained_shared_bytes -= self._retained_shared_images.pop(storage)[0]

    def _retain_storage(self, storage, size):
        if storage in self._retained_images:
            self._retained_images[storage][1] += 1
        else:
            self._retained_images[storage] = [size, 1]
            self.retained_bytes += size

    def _release_storage(self, storage):
        self._retained_images[storage][1] -= 1
        if not self._retained_images[storage][1]:
            self.retained_bytes -= self._retained_images.pop(storage)[0]

    def private_regions(self, context, current):
        if context != self._region_context:
            self._regions_clear()
            self._region_context = context
        return _RegionStore(self, context, current)

    def _region_remove(self, key):
        entry = self._regions.pop(key)
        self.retained_bytes -= entry.metadata_bytes
        if entry.complete:
            self._release_storage(int(entry.image.cacheKey()))
        else:
            self.retained_bytes -= int(entry.image.sizeInBytes())

    def _regions_clear(self):
        for key in tuple(self._regions):
            self._region_remove(key)
        self._region_context = None
        self._region_generation += 1

    def _evict_retained(self, *, force=False):
        # Keep unfinished demanded work ahead of one-use output aliases. A
        # complete predecessor can leave this pool once its caller owns a COW
        # handle; detached workers charge that source in snapshot admission.
        oldest = next((scope for scope in self.retained
                       if scope not in self._retained_shared), _MISSING)
        if oldest is not _MISSING:
            self.retained_remove(oldest)
            return True
        complete = next((key for key, entry in self._regions.items()
                         if entry.complete), _MISSING)
        if complete is not _MISSING:
            self._region_remove(complete)
            return True
        if force and self._retained_shared:
            self.retained_remove(next(iter(self._retained_shared)))
            return True
        if force and self._regions:
            self._region_remove(next(iter(self._regions)))
            return True
        return False

    def _region_begin(self, key, requested, image_format, tile_count, *, tile_size=None):
        from PySide6.QtCore import Qt, QRectF
        from PySide6.QtGui import QImage
        previous = self._regions.get(key)
        if previous is not None:
            return previous
        width, height = max(1, int(requested.width())), max(1, int(requested.height()))
        depth = QImage(1, 1, image_format).depth()
        # Address tuples, set slots, and Python integers also consume memory.
        # Reserve conservatively up front so coverage growth is byte bounded.
        metadata = 256 + 192 * tile_count
        size = ((width * depth + 31) // 32) * 4 * height + metadata
        budget = max(self.retained_budget, size)  # Existing exclusive oversized rule.
        while (self.retained_bytes + size > budget
               or len(self.retained) + len(self._regions) >= self.retained_limit):
            if not self._evict_retained(force=True):
                break
        image = QImage(width, height, image_format)
        if image.isNull():
            raise MemoryError('Could not allocate exact frame assembly')
        image.fill(Qt.transparent)
        entry = _RegionAssembly(image, metadata, bounds=QRectF(requested), tile_size=tile_size)
        self._regions[key] = entry
        self.retained_bytes += int(image.sizeInBytes()) + metadata
        return entry

    def retained_consume(self, image):
        """Retire raw result aliases only after an exact checkpoint owns them."""
        storage = int(image.cacheKey())
        for scope, entry in tuple(self.retained.items()):
            if (isinstance(scope, tuple) and scope[:1] == ('result',)
                    and int(entry[1].cacheKey()) == storage):
                self.retained_remove(scope)

    def retained_put(self, scope, key, image, state=None, *, shared=False, force=False):
        from PySide6.QtGui import QImage
        from comic_editor.ui.cache_dependencies import cache_put
        cache_put(self.canvas, "retained", ("retained", scope, key), image, state=state)
        self.retained_remove(scope)
        size = int(image.sizeInBytes())
        storage = int(image.cacheKey())
        shared_budget = max(0, self.retained_budget // 2)
        shared_limit = max(1, self.retained_limit // 2)
        shared = bool(shared and size <= shared_budget)
        extra_shared = lambda: size if shared and storage not in self._retained_shared_images else 0
        while self._retained_shared and (
            self.retained_shared_bytes + extra_shared() > shared_budget
            or len(self._retained_shared) + int(shared) > shared_limit
        ):
            self._retained_unprotect(next(iter(self._retained_shared)))
        # A one-use oversized image must not flush every reusable prefix. The
        # caller still owns its exact output when admission is declined. Keep
        # the established single-oversized-entry behavior in an unprotected pool.
        budget = self.retained_budget if self._retained_shared and not force else max(self.retained_budget, size)
        if (not force and self._retained_shared and storage not in self._retained_shared_images
                and self.retained_shared_bytes + size > budget):
            return False
        # QImage copies share pixels until edited. A completed worker image
        # often becomes a pipeline checkpoint before its result scope is
        # removed; counting that handoff twice evicts unrelated exact artwork.
        # Bound both unique pixel storage and the number of scope records.
        while (self.retained or self._regions) and (
            self.retained_bytes + (0 if storage in self._retained_images else size)
                > budget
            or len(self.retained) + len(self._regions) >= self.retained_limit
        ):
            if not self._evict_retained(force=force):
                return False
        self.retained[scope] = (key, QImage(image), state, size)
        self._retain_storage(storage, size)
        if shared:
            self._retained_shared[scope] = storage
            if storage in self._retained_shared_images:
                self._retained_shared_images[storage][1] += 1
            else:
                self._retained_shared_images[storage] = [size, 1]
                self.retained_shared_bytes += size
        return True

    def retained_remove(self, scope, key=None):
        entry = self.retained.get(scope)
        if entry is not None and (key is None or entry[0] == key):
            self.retained.pop(scope)
            self._retained_unprotect(scope)
            storage = int(entry[1].cacheKey())
            self._release_storage(storage)

    def result(self, scope, key):
        if any(job[:2] == (scope, key) and job[3].done() for job in self._running.values()):
            # A paint may arrive between worker completion and the 16 ms timer.
            # Adopt its exact pixels now instead of flashing another draft.
            self.poll()
        failure = self.exact_failures.get(scope)
        if failure is not None and failure[0] == key:
            from comic_editor.ui.async_projection import ProjectionFailed
            raise ProjectionFailed(scope, key, failure[1])
        entry = self.retained_get(("result", scope), key)
        return entry[0] if entry is not None else None

    def request(self, scope, key, compute, size, *, allow_oversized=False, require_exact=False):
        size = int(size)
        if size < 0:
            raise ValueError("Effect snapshot size cannot be negative")
        self.waiting.pop(scope, None)
        failure = self.exact_failures.get(scope)
        if failure is not None:
            if failure[0] == key and require_exact:
                from comic_editor.ui.async_projection import ProjectionFailed
                raise ProjectionFailed(scope, key, failure[1])
            self.exact_failures.pop(scope)
        oversized = size > self.budget
        if oversized and not allow_oversized:
            return False
        for identifier, job in self._running.items():
            if job[:2] == (scope, key) and not job[2].is_set():
                if require_exact:
                    self._running[identifier] = (*job[:5], True)
                return True
        existing = self.pending.get(scope)
        if existing and existing[1] == key:
            if require_exact:
                self.pending[scope] = (*existing[:5], True)
            return True
        for job in self._running.values():
            if job[0] == scope:
                job[2].set()
        self.pending.pop(scope, None)
        if self._running and sum(job[4] for job in self._running.values()) + size > self.budget:
            # This request cannot start until the running snapshot releases.
            # Keep unrelated queued work instead of evicting it for a snapshot
            # we cannot retain yet. A repaint asks for the latest state later.
            relevance = _navigator_job_relevant(scope, key)
            if not self.retry_on_release:
                self._navigator_retry_relevance = relevance
            elif relevance:
                self._navigator_retry_relevance = True
            self.retry_on_release = True
            if require_exact:
                self.waiting[scope] = key
                while len(self.waiting) > self.retained_limit:
                    self.waiting.popitem(last=False)
            self.timer.start()
            return True
        if oversized:
            # A large fallback gets the worker exclusively. Its computation
            # already needed this working memory in the synchronous renderer;
            # the estimate must not force it back onto the UI thread. Never
            # retain a second large snapshot while an
            # old job is still unwinding or computing another visible target.
            self.pending.clear()
        while self.pending and self.bytes_in_flight + size > self.budget:
            self.pending.popitem(last=False)
        # A canceled running job releases its arrays before the next starts.
        # Contexts carry immutable render policy, never the live canvas. Each
        # admitted request owns a separate context, including when it queues.
        context = copy_context()
        detached_compute = lambda cancelled: context.run(compute, cancelled)
        self.pending[scope] = (scope, key, Event(), detached_compute, size, require_exact)
        self._start()
        self.timer.start()
        return True

    def _start(self):
        while len(self._running) < self.worker_limit and self.pending:
            job = next(iter(self.pending.values()))
            scope, key, token, compute, size, require_exact = job
            # An oversized fallback owns the worker pool exclusively, rather
            # than adding another large snapshot to already active work.
            if self._running and (size > self.budget
                    or any(active[4] > self.budget for active in self._running.values())):
                break
            self.pending.pop(scope)
            stopped = self.stopped
            cancelled = lambda token=token: token.is_set() or stopped.is_set()
            self._job_serial += 1
            self._running[self._job_serial] = (scope, key, token,
                self.executor.submit(compute, cancelled), size, require_exact)
            self.submitted += 1

    def _emit_derived_ready(self, scope=None, key=None, *, retry=False, retry_relevance=None):
        """Keep the global signal; expose Navigator relevance only while it emits.

        Direct standalone emissions have no metadata and retain conservative
        refresh. Restoring the previous value also preserves nested emissions.
        Scope/key metadata is borrowed only for this synchronous emission;
        it never retains pixels or creates a cache.
        """
        signal = getattr(self.canvas, 'derivedResultReady', None)
        previous = self._navigator_derived_relevance
        previous_dependency = self._navigator_derived_dependency
        previous_retry = self._navigator_derived_retry
        self._navigator_derived_retry = retry
        self._navigator_derived_dependency = None if retry else (scope, key)
        self._navigator_derived_relevance = (retry_relevance if retry
            else _navigator_job_relevant(scope, key))
        try:
            if signal is not None:
                signal.emit()
            else:
                self.canvas.visualChanged.emit(None)
        finally:
            self._navigator_derived_relevance = previous
            self._navigator_derived_dependency = previous_dependency
            self._navigator_derived_retry = previous_retry

    def poll(self):
        for identifier, job in tuple(self._running.items()):
            if not job[3].done():
                continue
            self._running.pop(identifier)
            self.waiting.clear()
            failure = "The exact effect returned no image"
            try:
                result = job[3].result()
            except RadialRenderCancelled:
                result = None
            except Exception as error:
                # Keep exceptions visible for diagnostics without taking down
                # the UI event loop. Exports still render synchronously.
                import logging
                logging.getLogger(__name__).exception("Effect preview failed")
                failure = str(error)[:300] or type(error).__name__
                result = None
            if job[5] and result is not None and result.isNull():
                result = None
                failure = "The exact effect returned an empty image"
            if result is not None and not job[2].is_set():
                # Exact projections cannot advance past a draft. Admission of
                # their newly completed result takes priority over an older
                # retained prefix, including the existing oversized-image case.
                self.retained_put(("result", job[0]), job[1], result, force=job[5])
                source_decode = (isinstance(job[0], tuple)
                    and job[0][:1] == ('source-image-decode',)
                    and isinstance(job[1], tuple)
                    and job[1][:1] == ('source-image-decode-preview',))
                # Original decodes use the retained source handoff and guarded
                # ImageStore adoption. They are not derived effect pixels: a
                # large source alias would flush current compact stage prefixes.
                if not source_decode:
                    self.canvas._modifier_cache_put(job[1], result)
                ready = getattr(self.canvas, "_effect_result_ready", None)
                if ready is not None:
                    ready(job[0], job[1])
                else:
                    self.canvas._invalidate_scene_cache()
                # Ready pixels improve the same document; they are not a new
                # edit that should abandon a Navigator's completed bands.
                self._emit_derived_ready(job[0], job[1])
                self.canvas.update()
                self.completed += 1
            else:
                self.discarded += 1
                if job[5] and not job[2].is_set():
                    self.exact_failures[job[0]] = (job[1], failure)
                    self.exact_failures.move_to_end(job[0])
                    while len(self.exact_failures) > self.retained_limit:
                        self.exact_failures.popitem(last=False)
                    ready = getattr(self.canvas, "_effect_result_ready", None)
                    if ready is not None:
                        ready(job[0], job[1])
                    self.canvas.update()
                elif job[5] and not self.retry_on_release:
                    # A canceled exact capture may have left the projection
                    # waiting on this worker. Release that wait even when no
                    # replacement snapshot could be queued before cancellation.
                    # Its obsolete pixels and failures must never be adopted.
                    ready = getattr(self.canvas, "_effect_result_ready", None)
                    if ready is not None:
                        ready(None, None)
                    self.canvas.update()
            if self.retry_on_release:
                retry_relevance = self._navigator_retry_relevance
                self.retry_on_release = False
                self._navigator_retry_relevance = None
                ready = getattr(self.canvas, "_effect_result_ready", None)
                if ready is not None:
                    ready(None, None)
                else:
                    self.canvas._invalidate_scene_cache()
                self._emit_derived_ready(retry=True, retry_relevance=retry_relevance)
                self.canvas.update()
        self._start()
        if not self._running and not self.pending:
            self.timer.stop()

    def cancel_exact(self):
        """Retire obsolete projection work without abandoning live previews.

        Running snapshots release their own memory cooperatively. Mark even
        finished futures before polling so they cannot publish obsolete pixels.
        Completed semantic checkpoints remain available to the current scene.
        Original image decodes depend only on immutable source bytes, so live
        poses can share them; full document cancellation still retires them.
        """
        def source_decode(scope):
            return isinstance(scope, tuple) and scope[:1] == ('source-image-decode',)

        changed = False
        self._regions_clear()
        for table in (self.waiting, self.exact_failures):
            for scope in tuple(table):
                if not source_decode(scope):
                    table.pop(scope)
                    changed = True
        for scope, job in tuple(self.pending.items()):
            if job[5] and not source_decode(scope):
                self.pending.pop(scope)
                changed = True
        for job in self._running.values():
            if job[5] and not source_decode(job[0]) and not job[2].is_set():
                job[2].set()
                changed = True
        if (not self.waiting and not any(not job[5] or source_decode(job[0])
                for job in (*self._running.values(), *self.pending.values()))):
            self.retry_on_release = False
            self._navigator_retry_relevance = None
        if not self._running and not self.pending:
            self.timer.stop()
        return changed

    def cancel(self, *, clear_retained=True):
        self._regions_clear()
        self.pending.clear()
        self.waiting.clear()
        self.retry_on_release = False
        self._navigator_retry_relevance = None
        self.exact_failures.clear()
        if clear_retained:
            self.retained.clear()
            self.retained_bytes = 0
            self._retained_images.clear()
            self._retained_shared.clear()
            self._retained_shared_images.clear()
            self.retained_shared_bytes = 0
        for job in self._running.values():
            job[2].set()
        if not self._running:
            self.timer.stop()
