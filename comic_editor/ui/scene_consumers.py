"""Staged editor capture and detached work for auxiliary scene consumers."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from threading import Event
import time

from PySide6.QtCore import QObject, QTimer, Qt, Signal

from comic_editor.render.admission import (NATIVE_SOURCE_WORKSPACE, RENDER_ADMISSION,
    WorkCancelled, snapshot_working_bytes)


@dataclass
class SceneConsumer:
    key: tuple
    document: object
    capture: object
    compute: object
    arguments: tuple
    accept: object
    cancelled: Event
    future: object = None
    lane: tuple = ()
    ordered: bool = False
    valid: object = None
    discard: object = None
    detached: bool = False
    argument_capture: object = None
    captured_arguments: object = None
    owned_source: bool = False


def compute_snapshot(compute, snapshot, arguments, cancelled, stopped, *, native_source_budget=0):
    if native_source_budget:
        from comic_editor.render.input_sources import InputSourceSnapshot
        from comic_editor.ui.tile_input import prepare_input_tiles
        if (compute is prepare_input_tiles and type(snapshot) is InputSourceSnapshot
                and len(arguments) == 2):
            identifier, keys = arguments
            # The complete packet retains its native payload while later
            # one-key preparations run. Charge all of it with the largest
            # unchanged decode/alpha-scan scratch in one independent ticket.
            result_budget = min(int(native_source_budget), snapshot.tiles.residency.budget)
            quantum = snapshot.bounded_workspace(identifier, keys, result_budget,
                lambda: cancelled.is_set() or stopped.is_set())
            if keys and quantum is not None and 0 < quantum <= NATIVE_SOURCE_WORKSPACE:
                result = []
                images = {}
                with RENDER_ADMISSION.reserve('source-sampling', NATIVE_SOURCE_WORKSPACE, priority=0,
                        cancelled=lambda: cancelled.is_set() or stopped.is_set(), native_source=True):
                    for key in keys:
                        records = compute(snapshot, identifier, (key,))
                        if cancelled.is_set() or stopped.is_set():
                            raise WorkCancelled()
                        for _key,image,_bounds in records:
                            images[image.cacheKey()] = int(image.sizeInBytes())
                        if sum(images.values()) > result_budget or sum(images.values()) > quantum:
                            raise RuntimeError('Native source exceeded its immutable workspace proof')
                        result.extend(records)
                        del records
                    return tuple(result)
    estimate = getattr(compute, 'working_bytes', lambda *_a: 0)(snapshot, *arguments)
    snapshot_estimate = (getattr(compute, 'snapshot_working_bytes',
        lambda source, *_args: snapshot_working_bytes(source))(snapshot, *arguments)
        if snapshot is not None else 0)
    with RENDER_ADMISSION.reserve('source-sampling', snapshot_estimate+estimate,
                                  priority=getattr(compute, 'admission_priority', 2),
                                  cancelled=lambda: cancelled.is_set() or stopped.is_set()):
        if getattr(compute, 'accepts_cancelled', False):
            return compute(snapshot, *arguments, cancelled=cancelled)
        return compute(snapshot, *arguments)


class SceneConsumers(QObject):
    """One auxiliary worker; queued consumers hold no frozen source buffers."""
    LIMIT = 40
    workerFinished = Signal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.pending = OrderedDict()
        self.active = None
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='scene-consumer')
        self.stopped = Event()
        self.serial = 0
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        # Native source/material preparation often finishes within one frame.
        # Polling alone would hold its ready packets for a full timer interval.
        # A queued wakeup still publishes and validates on the document thread.
        self.workerFinished.connect(self.advance, Qt.ConnectionType.QueuedConnection)
        executor, stopped = self.executor, self.stopped
        self.destroyed.connect(lambda: (stopped.set(), executor.shutdown(wait=False, cancel_futures=True)))

    def request(self, key, compute, arguments, accept, *, ordered=False, valid=None, discard=None,
                capture_arguments=False, owned_source=False):
        if owned_source and valid is None:
            raise ValueError('Owned source work requires an explicit owner/generation validity guard')
        return self._request(key, compute, arguments, accept, ordered, valid, discard, False, capture_arguments, owned_source)

    def request_detached(self, key, compute, arguments, accept, *, valid=None, discard=None):
        """The arguments already own their sources; no live document is captured."""
        return self._request(key, compute, arguments, accept, False, valid, discard, True, False)

    def _request(self, key, compute, arguments, accept, ordered, valid, discard, detached, capture_arguments,
                 owned_source=False):
        canvas = self.canvas
        document = None if detached else canvas._render_document_state()
        lane = tuple(key)
        if ordered:
            self.serial += 1
            key = (*lane, self.serial)
        if self.active is not None and self.active.key == key:
            if self.active.document == document and self.active.arguments == tuple(arguments):
                self.active.accept = accept
                return
            self.active.cancelled.set()
        previous = self.pending.pop(key, None)
        if previous is not None:
            self._discard(previous)
        self.pending[key] = job = SceneConsumer(key, document, None, compute, tuple(arguments), accept,
            Event(), lane=lane, ordered=ordered, valid=valid, discard=discard, detached=detached,
            owned_source=owned_source)
        if capture_arguments:
            from comic_editor.render.scene import detached_slices
            job.argument_capture = detached_slices(tuple(arguments))
        self.pending.move_to_end(key)
        while len(self.pending) > self.LIMIT:
            # An ordered click/commit already accepted by the queue cannot be
            # silently dropped to make room for a newer click or latest probe.
            victim = next((identifier for identifier, pending in self.pending.items()
                           if not pending.ordered), None)
            if victim is None:
                self.pending.pop(key)
                self._accept(job, None, RuntimeError('Too many input operations are pending; wait for the current operations to finish'))
                break
            obsolete = self.pending.pop(victim)
            self._discard(obsolete)
        self.timer.start(0)
        return key

    @staticmethod
    def _discard(job):
        job.cancelled.set()
        if job.discard is not None:
            callback, job.discard = job.discard, None
            callback()

    def cancel(self, lane):
        lane = tuple(lane)
        if self.active is not None and self.active.lane == lane:
            self._discard(self.active)
        for key, job in list(self.pending.items()):
            if job.lane == lane:
                self.pending.pop(key)
                self._discard(job)

    def contains(self, lane):
        lane = tuple(lane)
        return (self.active is not None and self.active.lane == lane and not self.active.cancelled.is_set()
            or any(job.lane == lane for job in self.pending.values()))

    def _valid(self, job, current):
        return (not job.cancelled.is_set() and (job.detached or job.owned_source or current == job.document)
                and (job.valid is None or job.valid()))

    def _accept(self, job, result, error):
        before = self._document()
        job.discard = None
        job.accept(result, error)
        after = self._document()
        if job.ordered and error is None and after != before:
            # A later click follows this click's transaction. Unrelated edits
            # still invalidate both active and queued requests normally.
            for pending in self.pending.values():
                if pending.ordered and pending.lane == job.lane and pending.document == before:
                    pending.document = after

    def _document(self):
        return self.canvas._render_document_state() if self.canvas.chapter is not None else None

    def _capture_job(self, job):
        source_capture = getattr(job.compute, 'source_capture', None)
        guarded_cage = (getattr(job.compute, 'scene_capture_scope', None) == 'cage-originals'
                        and job.valid is not None and not job.detached)
        if callable(source_capture) and (job.owned_source or guarded_cage):
            # Owned contacts keep their positive source/version guard. Only
            # explicitly declared, guarded cage work opts into a focused scene
            # capture under ordinary full document/selection revision checks.
            # An arbitrary source_capture attribute is not sufficient.
            return source_capture(self.canvas, job.document, *job.arguments)
        return self.canvas._scene_snapshot_compiler.capture(self.canvas, job.document)

    def _submit(self, job, snapshot):
        options = {}
        budget = self._native_source_budget(job, snapshot)
        if budget:
            options['native_source_budget'] = budget
        job.future = self.executor.submit(compute_snapshot, job.compute, snapshot,
            job.captured_arguments or job.arguments, job.cancelled, self.stopped, **options)
        signal = self.workerFinished
        def completed(_future):
            try:
                signal.emit()
            except RuntimeError:
                # A retired document may have deleted its QObject while a
                # cancelled evaluator was finishing its current native block.
                pass
        job.future.add_done_callback(completed)

    def _native_source_budget(self, job, snapshot):
        """Freeze authorization only for this current gate's native source job."""
        from comic_editor.render.input_sources import InputSourceSnapshot
        from comic_editor.ui.tile_input import TileInputGate, prepare_input_tiles
        if (job.compute is not prepare_input_tiles or type(snapshot) is not InputSourceSnapshot
                or not job.owned_source or not job.ordered or job.detached
                or job.argument_capture is not None or job.captured_arguments is not None
                or job.valid is None or len(job.arguments) != 2 or len(job.lane) != 2
                or job.lane[0] != 'native-input' or self.active is not job or self.stopped.is_set()):
            return 0
        gates = getattr(self.canvas, '_native_input_gates', None)
        if not callable(gates):
            return 0
        gate = next((gate for gate in gates()
                     if isinstance(gate, TileInputGate) and gate.jobs is self
                     and id(gate) == job.lane[1]), None)
        if (gate is None or gate.canvas is not self.canvas or gate.chapter is not self.canvas.chapter
                or gate.tiles is not self.canvas.tiles or gate.identifier != job.arguments[0]
                or not gate.pending or not gate.current() or not self._valid(job, None)):
            return 0
        return min(gate.buffer_budget, snapshot.tiles.residency.budget)

    def _native_first_quantum_current(self, job):
        """Recognize only the current native gate's ordinary source capture."""
        from comic_editor.render.input_sources import InputSourceCapture
        from comic_editor.ui.tile_input import TileInputGate
        capture = job.capture
        if (not job.owned_source or not job.ordered or job.detached
                or job.argument_capture is not None or job.valid is None
                or getattr(job.compute, 'source_capture', None) is not InputSourceCapture
                or type(capture) is not InputSourceCapture
                or len(job.lane) != 2 or job.lane[0] != 'native-input'
                or len(job.arguments) != 2 or capture.owner is not self.canvas
                or self.active is not job or self.stopped.is_set()):
            return False
        gates = getattr(self.canvas, '_native_input_gates', None)
        if not callable(gates):
            return False
        gate = next((gate for gate in gates()
                     if isinstance(gate, TileInputGate) and gate.jobs is self
                     and id(gate) == job.lane[1]), None)
        return (gate is not None and gate.canvas is self.canvas
                and gate.chapter is self.canvas.chapter and gate.tiles is self.canvas.tiles
                and gate.identifier == job.arguments[0] == capture.identifier
                and gate.pending and gate.current() and self._valid(job, None))

    def _advance_capture(self, job, *, native_first=False):
        error = None
        try:
            finished = job.capture.advance(.004)
        except Exception as failure:
            finished, error = False, failure
        if native_first and not self._native_first_quantum_current(job):
            # Capture can reenter an owner/version cancellation. Do not submit
            # or publish an error for an input contact which has since retired.
            self._discard(job)
            if self.active is job:
                self.active = None
            return
        if error is not None:
            self.active = None
            self._accept(job, None, error)
            return
        if self.active is None or not finished:
            return
        if job.capture.stale:
            if job.owned_source and self._valid(job, self._document()):
                # A released native packet keeps its original source guard
                # across harmless presentation or unrelated edits.
                job.document = self._document()
                job.capture = self._capture_job(job)
            else:
                self._discard(job)
                self.active = None
        else:
            snapshot, job.capture = job.capture.result, None
            self._submit(job, snapshot)

    def advance(self):
        canvas = self.canvas
        if self.stopped.is_set():
            self.timer.stop()
            return
        job = self.active
        if job is not None:
            current = self._document()
            if not self._valid(job, current):
                self._discard(job)
            if job.future is not None:
                if not job.future.done():
                    self.timer.start(8)
                    return
                try:
                    result, error = job.future.result(), None
                except Exception as failure:
                    result, error = None, failure
                self.active = None
                if self._valid(job, current):
                    self._accept(job, result, error)
            elif job.cancelled.is_set():
                self.active = None
            elif job.argument_capture is not None:
                deadline = time.monotonic()+.004
                try:
                    while time.monotonic() < deadline:
                        next(job.argument_capture)
                except StopIteration as finished:
                    job.captured_arguments, job.argument_capture = finished.value, None
                except Exception as error:
                    self.active = None
                    self._accept(job, None, error)
                self.timer.start(8)
                return
            else:
                self._advance_capture(job)
                if self.active is not None:
                    self.timer.start(8)
                    return
        if self.pending:
            _key, job = self.pending.popitem(last=False)
            if self._valid(job, self._document()):
                self.active = job
                if job.detached:
                    self._submit(job, None)
                elif canvas.chapter is not None:
                    if job.owned_source:
                        job.document = self._document()
                    job.capture = self._capture_job(job)
                    if self._native_first_quantum_current(job):
                        # One metadata-only source quantum avoids a second
                        # zero-timer hop before a cold native packet's worker.
                        # Other consumers keep their existing staged capture.
                        self._advance_capture(job, native_first=True)
                else:
                    self._discard(job)
                    self.active = None
            else:
                self._discard(job)
            self.timer.start(0)
        else:
            self.timer.stop()

    def shutdown(self):
        self.stopped.set()
        self.timer.stop()
        if self.active is not None:
            self._discard(self.active)
            self.active = None
        for job in tuple(self.pending.values()):
            self._discard(job)
        self.pending.clear()
        self.executor.shutdown(wait=False, cancel_futures=True)


def scene_consumers(canvas):
    jobs = getattr(canvas, '_scene_consumers', None)
    if jobs is None:
        jobs = canvas._scene_consumers = SceneConsumers(canvas)
    return jobs
