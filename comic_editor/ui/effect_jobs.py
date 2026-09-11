"""Latest-request-wins effect work; workers never read mutable canvas state."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6.QtCore import QObject, QTimer

from comic_editor.ui.radial_blur import RadialRenderCancelled


class EffectJobs(QObject):
    def __init__(self, canvas, budget=256 * 1024 * 1024, retained_budget=256 * 1024 * 1024):
        super().__init__(canvas)
        self.canvas = canvas
        self.budget = budget
        self.pending = OrderedDict()
        self.running = None
        self.retry_on_release = False
        # The ordinary scene LRU also contains previews and source images.
        # Exact worker results and the latest completed stage need a separate
        # handoff so repainting another target cannot restart a whole stack.
        self.retained = OrderedDict()
        self.retained_bytes = 0
        self.retained_budget = retained_budget
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="effect-preview")
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
        return sum(job[4] for job in self.pending.values()) + (self.running[4] if self.running else 0)

    def retained_get(self, scope, key):
        from PySide6.QtGui import QImage
        entry = self.retained.get(scope)
        if entry is None or entry[0] != key:
            return None
        self.retained.move_to_end(scope)
        return QImage(entry[1]), entry[2]

    def retained_put(self, scope, key, image, state=None):
        from PySide6.QtGui import QImage
        self.retained_remove(scope)
        size = int(image.sizeInBytes())
        # As with the canvas cache, one oversized image may reside alone.
        # There is no new supported-image-size cutoff or per-target leak.
        while self.retained and self.retained_bytes + size > self.retained_budget:
            _, entry = self.retained.popitem(last=False)
            self.retained_bytes -= entry[3]
        self.retained[scope] = (key, QImage(image), state, size)
        self.retained_bytes += size

    def retained_remove(self, scope, key=None):
        entry = self.retained.get(scope)
        if entry is not None and (key is None or entry[0] == key):
            self.retained.pop(scope)
            self.retained_bytes -= entry[3]

    def result(self, scope, key):
        entry = self.retained_get(("result", scope), key)
        return entry[0] if entry is not None else None

    def request(self, scope, key, compute, size, *, allow_oversized=False):
        oversized = size > self.budget
        if oversized and not allow_oversized:
            return False
        if self.running and self.running[:2] == (scope, key) and not self.running[2].is_set():
            return True
        existing = self.pending.get(scope)
        if existing and existing[1] == key:
            return True
        if self.running and self.running[0] == scope:
            self.running[2].set()
        self.pending.pop(scope, None)
        if oversized:
            # A large fallback gets the worker exclusively. Its computation
            # already needed this working memory in the synchronous renderer;
            # the estimate must not force it back onto the UI thread. Never
            # retain a second large snapshot while an
            # old job is still unwinding or computing another visible target.
            self.pending.clear()
            if self.running:
                self.retry_on_release = True
                self.timer.start()
                return True
        while self.pending and self.bytes_in_flight + size > self.budget:
            self.pending.popitem(last=False)
        if not oversized and self.bytes_in_flight + size > self.budget:
            # The old worker still owns its snapshot until cancellation is
            # observed. Do not run the new expensive effect on the UI thread
            # just because both snapshots cannot fit at the same time.
            # Retain no new arrays; repaint after release asks for the latest
            # document state and can then enqueue it within the same budget.
            self.retry_on_release = True
            self.timer.start()
            return True
        # A canceled running job releases its arrays before the next starts.
        self.pending[scope] = (scope, key, Event(), compute, size)
        self._start()
        self.timer.start()
        return True

    def _start(self):
        if self.running is not None or not self.pending:
            return
        _, job = self.pending.popitem(last=False)
        scope, key, token, compute, size = job
        stopped = self.stopped
        cancelled = lambda: token.is_set() or stopped.is_set()
        self.running = (scope, key, token, self.executor.submit(compute, cancelled), size)
        self.submitted += 1

    def poll(self):
        job = self.running
        if job is not None and job[3].done():
            self.running = None
            try:
                result = job[3].result()
            except RadialRenderCancelled:
                result = None
            except Exception:
                # Keep exceptions visible for diagnostics without taking down
                # the UI event loop. Exports still render synchronously.
                import logging
                logging.getLogger(__name__).exception("Effect preview failed")
                result = None
            if result is not None and not job[2].is_set():
                self.retained_put(("result", job[0]), job[1], result)
                self.canvas._modifier_cache_put(job[1], result)
                self.canvas._invalidate_scene_cache()
                self.canvas.visualChanged.emit(None)
                self.canvas.update()
                self.completed += 1
            else:
                self.discarded += 1
            if self.retry_on_release:
                self.retry_on_release = False
                self.canvas._invalidate_scene_cache()
                self.canvas.visualChanged.emit(None)
                self.canvas.update()
        self._start()
        if self.running is None and not self.pending:
            self.timer.stop()

    def cancel(self):
        self.pending.clear()
        self.retry_on_release = False
        self.retained.clear()
        self.retained_bytes = 0
        if self.running:
            self.running[2].set()
        else:
            self.timer.stop()
