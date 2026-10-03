"""Native integer resampling must retain all CPU-stage and final float bits."""
import numpy as np
import pytest

from comic_editor.ui.modifier_rendering import _variable_blur, BlurPyramidCache


def test_unknown_pillow_resampler_falls_back_before_graphics_access(monkeypatch):
    from comic_editor.render.gpu import blur
    monkeypatch.setattr(blur, 'PILLOW_VERSION', 'unverified-resampler')
    kernel = blur.GpuBlur.__new__(blur.GpuBlur)
    assert kernel.apply(np.zeros((3, 5, 4), np.uint8), 3., source_key='revision') is None


@pytest.fixture
def renderer(qapp):
    from comic_editor.render.gpu.point_chain import GpuPointChain
    result = GpuPointChain(budget=32 * 1024 * 1024)
    if not result.available:
        result.close()
        pytest.skip(result.reason)
    yield result
    result.close()


@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
@pytest.mark.parametrize('shape', [(37, 53), (1, 77), (83, 1), (257, 273)])
def test_gpu_blur_pyramid_matches_every_reference_float_bit(renderer, algorithm, shape):
    data = np.random.default_rng(81).integers(0, 256, (*shape, 4), np.uint8)
    data[..., :3] = np.minimum(data[..., :3], data[..., 3:4])
    original = data.astype(np.float32) / 255.
    cache = BlurPyramidCache()
    for radius in (.1, 1., 1.0000001, 2.375, 3., 7., 11.25, 15., 31., 63., 100., 101.):
        expected = _variable_blur(original, radius, cache, algorithm)
        actual = renderer.apply_blur(data, radius, source_key=(shape, algorithm), algorithm=algorithm)
        assert actual is not None
        np.testing.assert_array_equal(actual, expected, err_msg=f'{shape} {algorithm} radius {radius}')
        assert renderer.bytes <= renderer.budget


def test_gpu_blur_retains_levels_and_respects_shared_budget(renderer):
    data = np.random.default_rng(19).integers(0, 256, (271, 289, 4), np.uint8)
    actual = renderer.apply_blur(data, 11.25, source_key='immutable')
    before = renderer.uploads, renderer.draws
    np.testing.assert_array_equal(renderer.apply_blur(data, 11.25, source_key='immutable'), actual)
    assert (renderer.uploads, renderer.draws) == before
    renderer.apply_blur(data, 12.25, source_key='immutable')
    # A slider change retains both selected pyramid levels and their upscales.
    assert renderer.uploads == before[0] + 1  # Only the scalar blend table changes.
    assert renderer.draws == before[1] + 1
    renderer.budget = 1024
    assert renderer.apply_blur(data, 7, source_key='too-large') is None
    assert renderer.apply_blur(data, 0, source_key='identity') is None
    with pytest.raises(ValueError):
        renderer.apply_blur(data.astype(np.float32), 7, source_key='wrong-type')
