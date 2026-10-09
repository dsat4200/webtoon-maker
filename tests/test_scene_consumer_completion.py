"""Ready input work wakes its document owner without waiting for polling."""
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QObject

from comic_editor.ui.scene_consumers import SceneConsumers


class Canvas(QObject):
    def __init__(self):
        super().__init__()
        self.chapter = object()
        self.document = (1,)

    def _render_document_state(self):
        return self.document


class Capture:
    def __init__(self, *_args):
        self.result, self.stale = object(), False

    def advance(self, _seconds):
        return True


@pytest.mark.parametrize('owned_source', [False, True])
@pytest.mark.parametrize('retirement', [None, 'cancel', 'invalid', 'shutdown'])
def test_worker_completion_publishes_on_owner_without_polling(qapp, owned_source, retirement):
    canvas = Canvas()
    jobs = SceneConsumers(canvas)
    release, entered = Event(), Event()
    owner, accepted, discarded = get_ident(), [], []
    current = [True]

    def compute(_snapshot):
        assert get_ident() != owner
        entered.set()
        assert release.wait(5)
        return 'ready native resource'

    compute.source_capture = Capture
    compute.snapshot_working_bytes = lambda *_args: 0
    compute.admission_priority = 0
    try:
        arguments = (('native-input',), compute, (),
            lambda result, error: accepted.append((get_ident(), result, error)))
        options = dict(valid=lambda: current[0], discard=lambda: discarded.append(True))
        if owned_source:
            jobs.request(*arguments, owned_source=True, ordered=True, **options)
        else:
            jobs.request_detached(*arguments, **options)
        jobs.advance()
        if owned_source:
            jobs.advance()
        future = jobs.active.future
        assert entered.wait(5)
        # Hold polling past the test deadline. The queued completion must be
        # what publishes the result, and must still revalidate retirement.
        jobs.timer.start(60000)
        if retirement == 'cancel':
            jobs.cancel(('native-input',))
        elif retirement == 'invalid':
            current[0] = False
        elif retirement == 'shutdown':
            jobs.shutdown()
        release.set()
        assert future.result(timeout=5) == 'ready native resource'
        qapp.processEvents()
        assert jobs.active is None
        if retirement is None:
            assert accepted == [(owner, 'ready native resource', None)]
            assert discarded == []
        else:
            assert accepted == []
            assert discarded == [True]
    finally:
        release.set()
        jobs.shutdown()
        jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()
