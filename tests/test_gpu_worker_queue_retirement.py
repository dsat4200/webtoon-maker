"""Slow driver retirement cannot own the application's graphics queue lock."""
from collections import deque
from contextlib import contextmanager
from itertools import count
from threading import Condition, Event, Timer, get_ident
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QObject

from comic_editor.render.device import ScalarBlurStage
from comic_editor.render.gpu import worker as worker_module
from comic_editor.render.gpu.residency import GRAPHICS_RESIDENCY
from test_gpu_worker import wait_until


@pytest.mark.parametrize('pending', ['complete', 'cancel', 'close'])
def test_driver_retirement_keeps_gui_release_and_submission_available(qapp, monkeypatch, pending):
    gui = get_ident()
    entered, release, timeout = Event(), Event(), Event()
    driver_threads, deleted, renderers = [], [], []
    fences = count(1)

    class Sync:
        def __init__(self, _context):
            driver_threads.append(get_ident())

        def insert(self):
            driver_threads.append(get_ident())
            return next(fences)

        def delete(self, fence):
            driver_threads.append(get_ident())
            if not deleted:
                entered.set()
                assert release.wait(5), 'Controlled driver retirement did not unblock'
            deleted.append(fence)

    class Renderer:
        def __init__(self, **_options):
            self.context, self.available, self.reason = object(), True, ''
            self.leases, self.resources = {}, {}
            self.uploads = self.readbacks = self.draws = self.hits = self.compiles = self.bytes = 0
            self.residency_token = GRAPHICS_RESIDENCY.token()
            self.functions = SimpleNamespace(glFlush=lambda: driver_threads.append(get_ident()))
            self.closed = False
            renderers.append(self)

        @contextmanager
        def _current(self):
            driver_threads.append(get_ident())
            yield

        def segment(self, pixels, _stages, *, source_key, canonical_input):
            driver_threads.append(get_ident())
            key = ('source', source_key)
            self.resources[key] = SimpleNamespace(width=pixels.shape[1], height=pixels.shape[0], texture=1)
            return self.resources[key], key, canonical_input

        def close(self):
            driver_threads.append(get_ident())
            self.closed = True

    monkeypatch.setattr(worker_module, 'GpuPointChain', Renderer)
    monkeypatch.setattr('comic_editor.render.gpu.sync.GlSync', Sync)
    # Exercise the real queue, future, copied-input admission and QThread loop
    # without creating a native graphics surface or depending on a GPU driver.
    service = worker_module.GpuWorker.__new__(worker_module.GpuWorker)
    QObject.__init__(service)
    service.gpu_budget = service.queue_budget = service.legacy_gpu_budget = 1024 * 1024
    service.legacy_residency_token = GRAPHICS_RESIDENCY.token()
    service.queue, service.condition, service.releases = deque(), Condition(), set()
    service.share_context = service.render_context = service.prepared_context = service.surface = None
    service.ready = Event()
    service.queued_bytes = 0
    service.available = service.closed = False
    service.reason, service.stats = '', {}
    service.worker_thread = worker_module._GraphicsThread(service)
    service.worker_thread.start()
    watchdog = None
    images = []
    try:
        wait_until(qapp, service.ready.is_set)
        assert service.available
        pixels = np.array([[[.4, .2, .1, .8], [.6, .4, .2, 1.]]], np.float32)
        stages = (ScalarBlurStage(0),)
        for source_key in ('first', 'second'):
            future = service.submit_segment(pixels, stages, source_key=source_key, canonical_input=True)
            wait_until(qapp, lambda: future.done() and service.queued_bytes == 0)
            images.append(future.result())
        images[0].release()
        wait_until(qapp, entered.is_set)

        def driver_watchdog():
            timeout.set()
            release.set()
        watchdog = Timer(.5, driver_watchdog)
        watchdog.start()
        images[1].release()
        queued = service.submit_segment(pixels, stages, source_key='queued', canonical_input=True)
        assert queued is not None
        assert not timeout.is_set(), 'GUI queue work waited for driver fence retirement'
        assert not release.is_set(), 'GUI queue work finished only after driver retirement'
        assert service.queued_bytes == pixels.nbytes
        if pending == 'cancel':
            assert queued.cancel()
        elif pending == 'close':
            service.close()
            assert queued.cancelled()
        release.set()
        if pending != 'close':
            wait_until(qapp, lambda: queued.done() and service.queued_bytes == 0)
            if pending == 'complete':
                images.append(queued.result())
                images[-1].release()
            else:
                assert queued.cancelled()
            service.close()
        assert service.surface is None and not service.worker_thread.isRunning()
        assert renderers[0].closed and not renderers[0].leases
        assert sorted(deleted) == list(range(1, len(images) + 1))
        assert len(deleted) == len(set(deleted)), 'A graphics fence was retired twice'
        assert service.queued_bytes == 0 and not service.queue
        assert driver_threads and len(set(driver_threads)) == 1 and driver_threads[0] != gui
    finally:
        release.set()
        if watchdog is not None:
            watchdog.cancel()
            watchdog.join()
        for image in images:
            image.release()
        service.close()
        service.deleteLater()
