"""Repeated graphics shutdown with real stopped/deleted C++ Qt owners."""
import threading

import numpy as np
import pytest
from shiboken6 import delete, isValid

from comic_editor.render.gpu.worker import GpuWorker
from comic_editor.render.gpu.point_chain import GpuPointChain


def native_worker(qapp):
    service = GpuWorker(gpu_budget=16 * 1024 * 1024, queue_budget=8 * 1024 * 1024)
    if service.worker_thread is None:
        service.close()
        pytest.skip(service.reason or 'Native background graphics thread unavailable')
    try:
        assert service.ready.wait(10), 'Real graphics owner must finish setup'
        assert isValid(service.worker_thread) and service.worker_thread.isRunning()
        assert service.surface is not None and isValid(service.surface)
        return service
    except BaseException:
        service.close()
        raise


def stop_without_gui_dispatch(service):
    """Finish the real owner while its GUI finished callback remains queued."""
    thread, surface = service.worker_thread, service.surface
    with service.condition:
        service.closed = True
        service.condition.notify_all()
    assert thread.wait(5000), 'Ordinary native thread shutdown must complete'
    assert not thread.isRunning()
    assert service.surface is surface, 'No GUI finished callback dispatched yet'
    return thread, surface


def test_close_after_cpp_deleted_finished_thread_is_idempotent(qapp, monkeypatch):
    owner_calls = []
    original_close = GpuPointChain.close
    def observed_close(renderer):
        owner_calls.append(threading.get_ident())
        return original_close(renderer)
    monkeypatch.setattr(GpuPointChain, 'close', observed_close)
    service = native_worker(qapp)
    thread = service.worker_thread
    try:
        service.close()
        assert service.closed and service.surface is None
        assert service.worker_thread is thread and isValid(thread)
        assert not thread.isRunning() and thread.service is None
        assert owner_calls and len(owner_calls) == 1
        assert owner_calls[0] != threading.get_ident()
        delete(thread)
        assert not isValid(thread)
        service.close()
        assert service.closed and service.surface is None and service.worker_thread is None
        assert service.share_context is None
        service.close()
        qapp.processEvents()
        assert len(owner_calls) == 1 and service.worker_thread is None
    finally:
        service.close()


def test_deleted_thread_before_queued_finished_callback_destroys_valid_surface_once(qapp, monkeypatch):
    service = native_worker(qapp)
    thread, surface = stop_without_gui_dispatch(service)
    calls = []
    original_destroy = surface.destroy
    def observed_destroy():
        assert isValid(surface)
        calls.append(threading.get_ident())
        return original_destroy()
    monkeypatch.setattr(surface, 'destroy', observed_destroy)
    try:
        delete(thread)
        assert not isValid(thread) and isValid(surface)
        service.close()
        assert service.surface is None and service.worker_thread is None
        assert calls == [threading.get_ident()]
        qapp.processEvents()
        service.close()
        assert calls == [threading.get_ident()]
        assert service.share_context is None and thread.service is None
    finally:
        service.close()


@pytest.mark.parametrize('delete_thread', [False, True])
def test_deleted_surface_and_stopped_thread_wrappers_retire_safely(qapp, delete_thread):
    service = native_worker(qapp)
    thread, surface = stop_without_gui_dispatch(service)
    try:
        delete(surface)
        assert not isValid(surface)
        if delete_thread:
            delete(thread)
            assert not isValid(thread)
        service.close()
        assert service.closed and service.surface is None and service.share_context is None
        assert thread.service is None
        if delete_thread:
            assert service.worker_thread is None
        else:
            assert service.worker_thread is thread and isValid(thread) and not thread.isRunning()
        qapp.processEvents()
        service.close()
        assert service.surface is None
    finally:
        service.close()


def test_live_worker_wait_timeout_preserves_owners_and_cancels_pending_copies(qapp, monkeypatch):
    started, release = threading.Event(), threading.Event()
    original_apply = GpuPointChain.apply_lut
    original_close = GpuPointChain.close
    owner_calls = []
    def blocked(renderer, *args, **kwargs):
        started.set()
        assert release.wait(10), 'Controlled live request must be released'
        return original_apply(renderer, *args, **kwargs)
    def observed_close(renderer):
        owner_calls.append(threading.get_ident())
        return original_close(renderer)
    monkeypatch.setattr(GpuPointChain, 'apply_lut', blocked)
    monkeypatch.setattr(GpuPointChain, 'close', observed_close)
    service = native_worker(qapp)
    if not service.available:
        service.close()
        pytest.skip(service.reason or 'Actual point shader unavailable')
    thread, surface = service.worker_thread, service.surface
    pixels = np.array([[[.4, .2, 0., .8]]], np.float32)
    palette = np.empty((256, 256, 4), np.float32)
    palette[..., :3] = np.arange(256, dtype=np.float32)[None, :, None] / 255.
    palette[..., 3] = np.arange(256, dtype=np.float32)[:, None] / 255.
    waits = []
    real_wait = thread.wait
    try:
        first = service.submit_lut(pixels, palette, source_key='running', palette_key='identity')
        assert first is not None and started.wait(5)
        pending = service.submit_lut(pixels, palette, source_key='pending', palette_key='identity')
        assert pending is not None
        with service.condition:
            assert len(service.queue) == 1
            pending_lease = service.queue[0].copy_lease
            assert pending_lease is not None and not pending_lease.released
        with monkeypatch.context() as timeout:
            def wait_timeout(milliseconds):
                waits.append(milliseconds)
                assert thread.isRunning() and isValid(surface)
                return False
            timeout.setattr(thread, 'wait', wait_timeout)
            service.close()
        assert waits == [5000]
        assert service.closed and pending.cancelled() and pending_lease.released
        assert not first.done() and not service.queue
        assert service.worker_thread is thread and service.surface is surface
        assert thread.isRunning() and isValid(surface) and surface.isValid()
        assert not owner_calls
    finally:
        release.set()
        try:
            assert real_wait(5000)
        finally:
            service.close()
    np.testing.assert_array_equal(first.result(), pixels)
    assert not first.result().flags.writeable
    assert service.queued_bytes == 0 and service.surface is None
    assert service.worker_thread is thread and not thread.isRunning() and thread.service is None
    assert len(owner_calls) == 1 and owner_calls[0] != threading.get_ident()
    service.close()
    assert len(owner_calls) == 1


def test_valid_thread_wait_failure_still_propagates_and_does_not_destroy_surface(qapp, monkeypatch):
    service = native_worker(qapp)
    thread, surface = service.worker_thread, service.surface
    waits = []
    try:
        with monkeypatch.context() as failing_wait:
            def fail(milliseconds):
                waits.append(milliseconds)
                raise ValueError('Native wait control failure')
            failing_wait.setattr(thread, 'wait', fail)
            with pytest.raises(ValueError, match='Native wait control failure'):
                service.close()
        assert waits == [5000] and service.closed
        assert service.worker_thread is thread and service.surface is surface and isValid(surface)
    finally:
        service.close()
    assert service.surface is None and service.worker_thread is thread
    assert not thread.isRunning() and thread.service is None
