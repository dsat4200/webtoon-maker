"""Native CPU oracles and ownership guards for the resident blur API."""
from concurrent.futures import ThreadPoolExecutor
import time

import numpy as np
import pytest

from comic_editor.render.device import DeviceImage, ScalarBlurStage
from comic_editor.render.gpu.worker import GpuWorker
from comic_editor.render.modifier_rendering import BlurPyramidCache, _variable_blur
from comic_editor.render.pixels import LEGACY_PIXELS, pixel_scope


def wait(qapp, condition):
    deadline = time.monotonic()+15
    while not condition():
        assert time.monotonic() < deadline
        qapp.processEvents()
        time.sleep(.001)


@pytest.fixture
def worker(qapp):
    value = GpuWorker(gpu_budget=64*1024*1024, queue_budget=24*1024*1024)
    wait(qapp, value.ready.is_set)
    if not value.available:
        value.close()
        pytest.skip(value.reason)
    yield value
    value.close()
    assert value.surface is None and not value.worker_thread.isRunning()


def materialize(qapp, image):
    with ThreadPoolExecutor(1) as executor:
        future = executor.submit(image.materialize_pixels)
        wait(qapp, future.done)
        return future.result()


def reference(monkeypatch, original, radius, algorithm):
    # The expected path must not call the GPU implementation under test.
    with monkeypatch.context() as patch, pixel_scope(LEGACY_PIXELS):
        patch.setattr('comic_editor.ui.gpu_effects.scalar_blur', lambda *_args, **_kwargs:None)
        return _variable_blur(original, radius, BlurPyramidCache(), algorithm)


@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
@pytest.mark.parametrize('shape', [(1,77), (83,1), (37,53), (257,273)])
@pytest.mark.parametrize('radius', [.1, 1.0000001, 2.375, 11.25, 63., 100.])
def test_resident_original_grid_blur_matches_every_reference_float_bit(worker, qapp, monkeypatch, algorithm, shape, radius):
    data = np.random.default_rng(8181).integers(0,256,(*shape,4),np.uint8)
    data[..., :3] = np.minimum(data[..., :3], data[..., 3:4])
    original = data.astype(np.float32)/np.float32(255.)
    expected = reference(monkeypatch,original,radius,algorithm)
    before = worker.stats.get('readbacks', 0)
    future = worker.submit_segment(data, [ScalarBlurStage(radius,algorithm)],
        source_key=('resident-original-grid',shape,algorithm,radius), canonical_input=True)
    assert future is not None
    wait(qapp, lambda:future.done() and worker.queued_bytes == 0)
    image = future.result()
    assert isinstance(image, DeviceImage) and image.canonical
    assert image.contract==LEGACY_PIXELS
    assert image.width()==shape[1] and image.height()==shape[0]
    assert worker.stats['readbacks']==before
    np.testing.assert_array_equal(materialize(qapp,image),expected)
    assert worker.stats['readbacks']==before+1
    image.release()


@pytest.mark.parametrize('algorithm', ['normal','legacy'])
def test_next_resident_blur_keeps_owned_source_and_has_no_intermediate_readback(worker,qapp,monkeypatch,algorithm):
    data = np.random.default_rng(19).integers(0,256,(39,57,4),np.uint8)
    data[..., :3] = np.minimum(data[..., :3],data[..., 3:4])
    original = data.astype(np.float32)/np.float32(255.)
    first_reference = reference(monkeypatch,original,7.,algorithm)
    # A scalar blur output is canonical normalized-byte float storage. The
    # second reference executes the ordinary original algorithm on those bits.
    expected = reference(monkeypatch,first_reference,11.25,algorithm)
    before = worker.stats.get('readbacks', 0)
    first = worker.submit_segment(data,[ScalarBlurStage(7.,algorithm)],
        source_key=('first-resident',algorithm),canonical_input=True)
    assert first is not None
    data.fill(0)  # submit_segment must own its immutable input copy.
    wait(qapp,lambda:first.done() and worker.queued_bytes == 0)
    source = first.result()
    assert isinstance(source,DeviceImage)
    second = worker.submit_segment(source,[ScalarBlurStage(11.25,algorithm)],
        source_key=source.identity,canonical_input=True)
    assert second is not None
    wait(qapp,lambda:second.done() and worker.queued_bytes == 0)
    result = second.result()
    assert isinstance(result,DeviceImage)
    assert worker.stats['readbacks']==before
    source.release()
    np.testing.assert_array_equal(materialize(qapp,result),expected)
    assert worker.stats['readbacks']==before+1
    result.release()


@pytest.mark.parametrize('version', ['12.1.0', '12.3.1', '13.0.0'])
def test_untested_pillow_versions_decline_before_touching_graphics(monkeypatch, version):
    from comic_editor.render.gpu import blur
    class UntouchedRenderer:
        def __getattribute__(self, name):
            raise AssertionError(f'Untested resampler touched graphics: {name}')
    monkeypatch.setattr(blur, 'PILLOW_VERSION', version)
    renderer = UntouchedRenderer()
    kernel = blur.GpuBlur(renderer)
    assert kernel.apply(np.zeros((3, 5, 4), np.uint8), 7., source_key='unknown') is None
