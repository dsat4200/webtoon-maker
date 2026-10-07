"""Latest-request-wins effect work; workers never read mutable canvas state."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from contextvars import copy_context
from threading import Event

from PySide6.QtCore import QObject, QTimer

from comic_editor.ui.radial_blur import RadialRenderCancelled


_MISSING = object()


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
        self.exact_failures = OrderedDict()
        # The ordinary scene LRU also contains previews and source images.
        # Exact worker results and the latest completed stage need a separate
        # handoff so repainting another target cannot restart a whole stack.
        self.retained = OrderedDict()
        self.retained_bytes = 0
        self.retained_budget = retained_budget
        self.retained_limit = max(1, int(retained_limit))
        self._retained_images = {}
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
        while self.retained and (
            self.retained_bytes + (0 if storage in self._retained_images else size)
                > budget
            or len(self.retained) >= self.retained_limit
        ):
            oldest = next((item for item in self.retained if item not in self._retained_shared), _MISSING)
            if oldest is _MISSING:
                if not force:
                    return False
                oldest = next(iter(self._retained_shared))
                self._retained_unprotect(oldest)
            self.retained_remove(oldest)
        self.retained[scope] = (key, QImage(image), state, size)
        if storage in self._retained_images:
            self._retained_images[storage][1] += 1
        else:
            self._retained_images[storage] = [size, 1]
            self.retained_bytes += size
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
            self._retained_images[storage][1] -= 1
            if not self._retained_images[storage][1]:
                self.retained_bytes -= self._retained_images.pop(storage)[0]

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
        def admitted_compute(cancelled):
            from comic_editor.render.admission import RENDER_ADMISSION, WorkCancelled
            try:
                with RENDER_ADMISSION.reserve("effect", size, priority=0, cancelled=cancelled):
                    return compute(cancelled)
            except WorkCancelled:
                return None
        detached_compute = lambda cancelled: context.run(admitted_compute, cancelled)
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
                self.canvas._modifier_cache_put(job[1], result)
                ready = getattr(self.canvas, "_effect_result_ready", None)
                if ready is not None:
                    ready(job[0], job[1])
                else:
                    self.canvas._invalidate_scene_cache()
                self.canvas.visualChanged.emit(None)
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
                self.retry_on_release = False
                ready = getattr(self.canvas, "_effect_result_ready", None)
                if ready is not None:
                    ready(None, None)
                else:
                    self.canvas._invalidate_scene_cache()
                self.canvas.visualChanged.emit(None)
                self.canvas.update()
        self._start()
        if not self._running and not self.pending:
            self.timer.stop()

    def cancel(self, *, clear_retained=True):
        self.pending.clear()
        self.waiting.clear()
        self.retry_on_release = False
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

    def cancel_scopes(self, refs):
        """Cancel only work whose explicit entity scope was changed by history.

        Retained images keep their semantic keys, so unaffected and historical
        exact results can remain warm. Unknown scopes are retired conservatively.
        """
        refs = frozenset(refs)
        def changed(scope):
            if isinstance(scope, tuple):
                if len(scope) >= 2 and isinstance(scope[0], str) and isinstance(scope[1], str) and scope[:2] in refs:
                    return True
                if len(scope) >= 2 and isinstance(scope[0], str) and scope[0] in {"layer", "object", "mask", "modifier"}:
                    return False
                if scope and scope[0] == "output" and len(scope) > 1:
                    return changed(scope[1])
            return True
        for mapping in (self.pending, self.waiting, self.exact_failures):
            for scope in tuple(mapping):
                if changed(scope):
                    mapping.pop(scope, None)
        for job in self._running.values():
            if changed(job[0]):
                job[2].set()
        if not self._running and not self.pending:
            self.timer.stop()
