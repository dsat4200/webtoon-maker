"""Real-driver thread ownership, detached inputs and queue admission."""
import threading
import time

import numpy as np
import pytest

from comic_editor.render.gpu.worker import GpuWorker
from comic_editor.render.gpu.point_chain import GpuPointChain


def wait_until(qapp, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while not predicate():
        assert time.monotonic() < deadline
        qapp.processEvents()
        time.sleep(.001)


@pytest.fixture
def worker(qapp):
    service = GpuWorker(gpu_budget=64*1024*1024, queue_budget=48*1024*1024)
    wait_until(qapp, service.ready.is_set)
    if not service.available:
        service.close()
        pytest.skip(service.reason)
    yield service
    service.close()
    assert service.surface is None and not service.worker_thread.isRunning()


def palette():
    values = np.empty((256, 256, 4), np.float32)
    values[..., :3] = np.arange(256, dtype=np.float32)[None, :, None] / 255.
    values[..., 3] = np.arange(256, dtype=np.float32)[:, None] / 255.
    return values


@pytest.mark.parametrize('module,name,fields',[
    ('comic_editor.ui.gpu_pattern_effects','GpuPatternRenderer',
     ('source','gradient','color_source_texture','mask','output','blur_x','blur_y',
      'triangle_output','program','blur_program','triangle_program','buffer','triangle_buffer','vao','functions')),
    ('comic_editor.ui.gpu_textures','GpuTextureRenderer',
     ('framebuffer','texture','program','buffer','vao','functions')),
])
def test_legacy_helper_context_loss_retires_guards_on_owner(module,name,fields):
    from concurrent.futures import ThreadPoolExecutor
    import importlib
    import weakref
    renderer_type = getattr(importlib.import_module(module),name)
    gui = threading.get_ident()
    retired = []
    class Guard:
        def destroy(self):
            pytest.fail('Lost context cannot explicitly destroy GL storage')
        def __del__(self):
            retired.append(threading.get_ident())
    class LostContext:
        def makeCurrent(self,_surface): return False
        def doneCurrent(self): pytest.fail('Context never became current')
    def retire():
        renderer = renderer_type.__new__(renderer_type)
        renderer.context,renderer.surface = LostContext(),object()
        guards = []
        for field in fields:
            value = Guard()
            setattr(renderer,field,value)
            guards.append(weakref.ref(value))
        del value
        renderer.close()
        assert renderer.context is None and renderer.surface is None
        return guards
    with ThreadPoolExecutor(1) as owner:
        guards = owner.submit(retire).result(5)
    assert all(reference() is None for reference in guards)
    assert len(retired) == len(fields) and all(thread != gui for thread in retired)


def test_worker_keeps_mutable_input_detached_and_runs_gl_on_one_owner(worker, monkeypatch, qapp):
    started, release = threading.Event(), threading.Event()
    apply = GpuPointChain.apply_lut
    threads = []

    def blocked(renderer, *args, **kwargs):
        threads.append(threading.get_ident())
        started.set()
        assert release.wait(5)
        return apply(renderer, *args, **kwargs)

    monkeypatch.setattr(GpuPointChain, 'apply_lut', blocked)
    pixels = np.array([[[.4, .2, 0., .8]], [[.6, .4, .2, 1.]]], np.float32)
    expected, table = pixels.copy(), palette()
    future = worker.submit_lut(pixels, table, source_key='revision', palette_key='identity')
    try:
        wait_until(qapp, started.is_set)
        pixels.fill(0)
        table.fill(0)
    finally:
        release.set()
    wait_until(qapp, lambda: future.done() and worker.queued_bytes == 0)
    np.testing.assert_array_equal(future.result(), expected)
    assert not future.result().flags.writeable
    assert threads == [threads[0]] and threads[0] != threading.get_ident()
    assert worker.stats['compiles'] == 2


def test_worker_bounds_inflight_and_queued_memory_and_cancels_pending(worker, monkeypatch, qapp):
    started, release = threading.Event(), threading.Event()
    apply = GpuPointChain.apply_lut

    def blocked(renderer, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return apply(renderer, *args, **kwargs)

    monkeypatch.setattr(GpuPointChain, 'apply_lut', blocked)
    pixels, table = np.zeros((128,128,4), np.float32), palette()
    size = pixels.nbytes*2 + table.nbytes
    worker.queue_budget = size*2
    first = worker.submit_lut(pixels, table, source_key=1, palette_key=1)
    try:
        wait_until(qapp, started.is_set)
        pending = worker.submit_lut(pixels, table, source_key=2, palette_key=1)
        assert pending is not None
        assert worker.queued_bytes == size*2
        assert worker.submit_lut(pixels, table, source_key=3, palette_key=1) is None
        assert pending.cancel()
    finally:
        release.set()
    wait_until(qapp, lambda: first.done() and worker.queued_bytes == 0)
    assert first.result() is not None and pending.cancelled()


def test_unavailable_platform_preserves_cpu_fallback(qapp, monkeypatch):
    from PySide6.QtGui import QOpenGLContext
    monkeypatch.setattr(QOpenGLContext, 'supportsThreadedOpenGL', lambda: False)
    worker = GpuWorker()
    assert worker.ready.is_set() and not worker.available
    assert worker.submit_lut(np.zeros((1,1,4),np.float32),palette(),source_key=0,palette_key=0) is None
    worker.close()


def test_canvas_point_jobs_use_gpu_without_blocking_gui_captures(worker, monkeypatch, qapp):
    from concurrent.futures import ThreadPoolExecutor
    from comic_editor.ui import point_lut
    from comic_editor.ui.modifier_rendering import apply_modifier_stack
    from test_point_chain import source_image, effects
    monkeypatch.setattr(point_lut, '_worker', worker)
    image, modifiers = source_image(1025,1025), effects('soft_light')
    expected = apply_modifier_stack(image, modifiers, (0,0), return_pixels=True, _point_lut=False)
    # This GUI request stays on the CPU even with the graphics worker ready.
    actual = apply_modifier_stack(image, modifiers, (0,0), return_pixels=True)
    np.testing.assert_array_equal(actual, expected)
    assert worker.stats == {}
    with ThreadPoolExecutor(1) as jobs:
        future = jobs.submit(apply_modifier_stack, image, modifiers, (0,0), return_pixels=True)
        wait_until(qapp, future.done)
        np.testing.assert_array_equal(future.result(), expected)
    assert worker.stats['draws'] == 1 and worker.stats['readbacks'] == 1


@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
def test_detached_scalar_blur_uses_gpu_and_preserves_all_float_bits(worker, monkeypatch, qapp, algorithm):
    from concurrent.futures import ThreadPoolExecutor
    from comic_editor.ui import point_lut
    from comic_editor.ui.modifier_rendering import _variable_blur
    monkeypatch.setattr(point_lut, '_worker', worker)
    data = np.random.default_rng(31).random((1025, 1025, 4), dtype=np.float32)
    data[..., :3] *= data[..., 3:4]
    expected = _variable_blur(data, 11.25, algorithm=algorithm)
    assert worker.stats == {}  # GUI remains independent of the graphics queue.
    threads = []
    original = GpuPointChain.apply_blur
    def measured(renderer, *args, **kwargs):
        threads.append(threading.get_ident())
        return original(renderer, *args, **kwargs)
    monkeypatch.setattr(GpuPointChain, 'apply_blur', measured)
    with ThreadPoolExecutor(1) as jobs:
        future = jobs.submit(_variable_blur, data, 11.25, algorithm=algorithm)
        wait_until(qapp, lambda: future.done() and worker.queued_bytes == 0)
        np.testing.assert_array_equal(future.result(), expected)
    assert threads and all(thread != threading.get_ident() for thread in threads)
    assert worker.stats['readbacks'] == 1 and worker.stats['draws'] > 1
    assert worker.stats['bytes'] <= worker.gpu_budget


def test_blur_submission_owns_bytes_and_shares_queue_admission(worker, monkeypatch, qapp):
    from comic_editor.ui.modifier_rendering import _variable_blur
    started, release = threading.Event(), threading.Event()
    original = GpuPointChain.apply_blur
    def blocked(renderer, *args, **kwargs):
        started.set()
        assert release.wait(5)
        return original(renderer, *args, **kwargs)
    monkeypatch.setattr(GpuPointChain, 'apply_blur', blocked)
    data = np.random.default_rng(37).integers(0, 256, (273, 271, 4), np.uint8)
    expected = _variable_blur(data.astype(np.float32)/255., 7)
    worker.queue_budget = data.nbytes * 5
    future = worker.submit_blur(data, 7, source_key='owned-revision')
    try:
        wait_until(qapp, started.is_set)
        data.fill(0)
        assert worker.submit_blur(data, 7, source_key='next-revision') is None
        assert worker.submit_lut(np.zeros((1,1,4),np.float32),palette(),source_key=0,palette_key=0) is None
        with pytest.raises(RuntimeError):
            worker.apply_blur(data, 7, source_key='gui')
    finally:
        release.set()
    wait_until(qapp, lambda: future.done() and worker.queued_bytes == 0)
    np.testing.assert_array_equal(future.result(), expected)
    assert not future.result().flags.writeable
