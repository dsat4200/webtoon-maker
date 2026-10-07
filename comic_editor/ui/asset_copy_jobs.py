"""Incremental asset capture, detached preparation, and serial publication."""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
import time

from PySide6.QtCore import QCoreApplication, QEventLoop, QObject, QTimer

from comic_editor.render.asset_sources import prepare_asset, publish_asset


@dataclass
class AssetCopyJob:
    context: object
    kind: str
    identifier: str
    name: str
    existing_id: str
    folder_id: object
    binding: tuple
    destination_session: object
    destination_revision: int
    document: object = None
    capture: object = None
    future: object = None
    publishing: bool = False
    finished: bool = False
    error: object = None


class AssetCopyJobs(QObject):
    LIMIT = 4

    def __init__(self, window):
        super().__init__(window)
        self.window = window
        self.jobs = deque()
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix='asset-source')
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        self.stopped = False
        executor = self.executor
        self.destroyed.connect(lambda: executor.shutdown(wait=False, cancel_futures=True))

    def binding(self):
        return (id(self.window.active_session), id(self.window.canvas.chapter))

    @property
    def busy(self):
        return bool(self.jobs)

    def request(self, context, kind, identifier, name, existing, destination_session, folder_id):
        if self.stopped or len(self.jobs) >= self.LIMIT:
            raise ValueError('The asset queue is full. Wait for an asset to finish.')
        job = AssetCopyJob(context, kind, identifier, name,
            existing.asset_id if existing is not None else '', folder_id,
            self.binding(), destination_session,
            destination_session.edit_revision if destination_session is not None else 0)
        self.jobs.append(job)
        self.timer.start(0)
        return job

    def _complete(self, job, result=None, error=None):
        job.finished, job.error = True, error
        job.capture = None
        self.jobs.remove(job)
        self.window._asset_copy_completed(job, result, error)

    def advance(self):
        if not self.jobs or self.stopped:
            self.timer.stop()
            return
        job = self.jobs[0]
        if job.future is not None:
            if not job.future.done():
                self.timer.start(8)
                return
            try:
                result = job.future.result()
                if job.publishing:
                    self._complete(job, result)
                else:
                    # Accepted asset replacement follows every writer already
                    # publishing that asset. Older queued recovery snapshots
                    # must not later replace the accepted library contents.
                    if job.existing_id:
                        scope = (str(job.context.repository.root), 'asset', job.existing_id)
                        self.window._autosave_jobs.supersede(scope)
                    job.publishing = True
                    job.future = self.window._autosave_jobs.submit_owned_write(
                        publish_asset, job.context.repository.root, result, job.existing_id, job.folder_id)
            except Exception as error:
                self._complete(job, error=error)
            self.timer.start(8) if self.jobs else self.timer.stop()
            return
        # Once preparation starts, the accepted operation owns frozen sources
        # and can complete after a tab switch. A partial capture has no such
        # ownership and cannot borrow a newly selected chapter.
        if job.binding != self.binding():
            self._complete(job, error=ValueError('The source document changed before the asset was captured'))
            self.timer.start(8) if self.jobs else self.timer.stop()
            return
        canvas = self.window.canvas
        try:
            cage_error = getattr(canvas, '_cage_commit_error', None)
            if cage_error is not None:
                raise ValueError(f'The cage transform could not be committed: {cage_error}') from cage_error
            if any(getattr(canvas, name, None) is not None for name in
                   ('_cage_session', '_cage_prepare', '_cage_commit_pending')):
                job.capture = None
                self.timer.start(8)
                return
            if job.capture is None:
                job.document = canvas._render_document_state()
                job.capture = canvas._scene_snapshot_compiler.capture(canvas, job.document)
            if job.capture.advance(.004):
                if job.capture.stale:
                    job.capture = None
                else:
                    snapshot, job.capture = job.capture.result, None
                    job.future = self.executor.submit(prepare_asset, snapshot,
                        job.kind, job.identifier, job.name)
        except Exception as error:
            self._complete(job, error=error)
        self.timer.start(8) if self.jobs else self.timer.stop()

    def drain(self):
        # Explicit close preserves accepted publications. Ordinary Copy as
        # Asset returns before capture or rendering; only close waits here.
        while self.jobs:
            QCoreApplication.processEvents(QEventLoop.ExcludeUserInputEvents)
            self.advance()
            time.sleep(.002)

    def shutdown(self):
        self.stopped = True
        self.timer.stop()
        self.executor.shutdown(wait=False, cancel_futures=True)
