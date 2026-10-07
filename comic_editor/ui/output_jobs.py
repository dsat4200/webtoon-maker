"""Incremental owner-thread snapshot capture and serial detached exports."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from threading import Event

from PySide6.QtCore import QObject, QTimer

from comic_editor.render.outputs import OutputCancelled, write_export


@dataclass
class ExportJob:
    destination: Path
    image_format: str
    region: tuple
    binding: tuple
    document: object
    capture: object
    cancelled: Event
    future: object = None


class OutputJobs(QObject):
    """Bounded jobs retain their captured document, never read the UI in workers."""
    LIMIT = 4

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.jobs = deque()
        self.completed = deque(maxlen=32)
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='document-output')
        self.stopped = Event()
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        jobs, executor, stopped = self.jobs, self.executor, self.stopped
        def close():
            stopped.set()
            for job in jobs:
                job.cancelled.set()
            executor.shutdown(wait=False, cancel_futures=True)
        self.destroyed.connect(close)

    def binding(self):
        window = self.window
        return (id(window.active_session), window.chapter.chapter_id if window.chapter is not None else '',
                str(window.repository.root.resolve()) if window.repository is not None else '')

    @property
    def busy(self):
        return bool(self.jobs)

    def contains(self, destination):
        path = str(Path(destination).resolve()).casefold()
        return any(str(job.destination.resolve()).casefold() == path for job in self.jobs)

    def request(self, destination, image_format):
        if self.stopped.is_set() or len(self.jobs) >= self.LIMIT:
            raise ValueError('The export queue is full. Wait for an export to finish.')
        canvas = self.window.canvas
        region = tuple(canvas.export_source_rect().getRect())
        if region[2] <= 0 or region[3] <= 0:
            raise ValueError('There is no document to export')
        # A cage commit may still be preparing its native source. Retain only
        # the destination/binding here; capture its completed model when this
        # job becomes active and that transaction has settled.
        job = ExportJob(Path(destination), image_format, region, self.binding(), None, None, Event())
        self.jobs.append(job)
        self.timer.start(0)
        return True

    def advance(self):
        if not self.jobs or self.stopped.is_set():
            self.timer.stop()
            return
        # Keep only the active output's frozen sources in memory. Queued jobs
        # retain lightweight document bindings until the worker becomes free.
        for job in list(self.jobs):
            if job.future is not None and job.future.done():
                self.jobs.remove(job)
                try:
                    job.future.result()
                    error = None
                except Exception as failure:
                    error = failure
                self.completed.append((job, error))
                if job.binding == self.binding() and not isinstance(error, OutputCancelled):
                    self.window._export_completed(job.destination, error)
        if self.jobs:
            job = self.jobs[0]
            if job.future is not None:
                self.timer.start(8)
                return
            if job.binding != self.binding():
                job.cancelled.set()
                job.capture = None
                self.jobs.popleft()
                self.completed.append((job, OutputCancelled()))
                self.timer.start(8) if self.jobs else self.timer.stop()
                return
            try:
                canvas = self.window.canvas
                cage_error = getattr(canvas, '_cage_commit_error', None)
                if cage_error is not None:
                    raise ValueError(f'The cage transform could not be committed: {cage_error}') from cage_error
                if any(getattr(canvas, name, None) is not None for name in
                       ('_cage_session', '_cage_prepare', '_cage_commit_pending')):
                    # A snapshot of the preview would silently export the
                    # old source while its accepted commit is still running.
                    job.capture = None
                    self.timer.start(8)
                    return
                if job.capture is None:
                    job.region = tuple(canvas.export_source_rect().getRect())
                    if job.region[2] <= 0 or job.region[3] <= 0:
                        raise ValueError('There is no document to export')
                    job.document = canvas._render_document_state()
                    job.capture = canvas._scene_snapshot_compiler.capture(canvas, job.document)
                finished = job.capture.advance(.004)
            except Exception as error:
                self.jobs.popleft()
                job.capture = None
                self.completed.append((job, error))
                self.window._export_completed(job.destination, error)
                self.timer.start(8) if self.jobs else self.timer.stop()
                return
            if not finished:
                self.timer.start(8)
                return
            if job.capture.stale:
                # Editing during a capture retires that partial state. Capture
                # the next complete revision without blocking document input.
                canvas = self.window.canvas
                job.document = canvas._render_document_state()
                job.capture = canvas._scene_snapshot_compiler.capture(canvas, job.document)
                self.timer.start(8)
                return
            snapshot, job.capture = job.capture.result, None
            job.future = self.executor.submit(write_export, snapshot, job.destination, job.image_format,
                                              region=job.region, cancelled=job.cancelled)
            self.timer.start(8)
            return
        self.timer.start(8) if self.jobs else self.timer.stop()

    def shutdown(self):
        self.stopped.set()
        self.timer.stop()
        for job in self.jobs:
            job.cancelled.set()
        self.executor.shutdown(wait=False, cancel_futures=True)
