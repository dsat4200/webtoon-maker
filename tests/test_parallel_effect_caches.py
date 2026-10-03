from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import numpy as np
import pytest

from comic_editor.ui.modifier_rendering import BlurPyramidCache, OutlineDistanceCache


def test_worker_pool_uses_one_preparation_budget_instead_of_one_per_thread(monkeypatch):
    from comic_editor.ui import distort_pipeline
    monkeypatch.setattr(distort_pipeline, '_worker_preparation', None)
    together = Barrier(4)
    def obtain(_):
        together.wait(timeout=5)
        return distort_pipeline._worker_preparation_cache()
    with ThreadPoolExecutor(4) as executor:
        caches = list(executor.map(obtain, range(4)))
    assert len({id(cache) for cache in caches}) == 1
    assert caches[0].budget == 256 * 1024 * 1024


def test_parallel_distort_preparation_and_eviction_keep_pixels_and_combined_budget():
    from PySide6.QtCore import QRectF
    from comic_editor.ui.distort_rendering import PreparedDistortCache, render_distort
    from test_distort_rendering import source_image, modifier
    sources = [source_image().scaled(57+i, 43+i) for i in range(8)]
    effect = modifier('twirl', angle=73)
    cache = PreparedDistortCache(256 * 1024)
    def render(index, preparation=None):
        image = sources[index % len(sources)]
        bounds = QRectF(0, 0, image.width(), image.height())
        return render_distort(image, bounds, effect, output_bounds=bounds,
                              preparation_cache=preparation)
    expected = [render(i) for i in range(len(sources))]
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(lambda i: render(i, cache), range(48)))
    assert all(result == expected[i % len(sources)] for i, result in enumerate(results))
    assert cache.bytes == sum(entry[1] for entry in cache._entries.values()) <= cache.budget
    assert cache.evictions > 0
    assert all(not prepared.pixels.flags.writeable for prepared, _ in cache._entries.values())


def test_duplicate_blur_computations_do_not_double_count_retained_memory(monkeypatch):
    source = np.full((129, 137, 4), .5, np.float32)
    cache = BlurPyramidCache(256 * 1024)
    build = cache._build
    together = Barrier(2)
    def simultaneous(pixels, algorithm):
        together.wait(timeout=5)
        return build(pixels, algorithm)
    monkeypatch.setattr(cache, '_build', simultaneous)
    with ThreadPoolExecutor(2) as executor:
        results = list(executor.map(lambda _: cache.pyramid(source), range(2)))
    assert cache.bytes == sum(level.nbytes for level in results[0]) <= cache.budget
    for first, second in zip(*results):
        np.testing.assert_array_equal(first, second)
        assert not first.flags.writeable and not second.flags.writeable


@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
def test_blur_pyramid_extends_only_needed_levels_without_changing_pixels(algorithm):
    source = np.random.default_rng(30).random((303, 397, 4), dtype=np.float32)
    source[..., :3] *= source[..., 3:4]
    cache = BlurPyramidCache()
    full = BlurPyramidCache(0).pyramid(source, algorithm)
    first = cache.pyramid(source, algorithm, max_level=2)
    assert len(first) == 3
    assert cache.bytes == sum(level.nbytes for level in first)
    assert cache.pyramid(source, algorithm, max_level=1) is first
    extended = cache.pyramid(source, algorithm, max_level=5)
    assert len(extended) == 6 and extended[0] is first[0]
    for before, after in zip(full, extended):
        np.testing.assert_array_equal(before, after)
        assert not after.flags.writeable
    assert cache.bytes == sum(level.nbytes for level in extended)


def test_shared_distance_cache_eviction_is_bounded_and_pixels_stay_exact():
    cache = OutlineDistanceCache(64 * 1024)
    sources = []
    for i in range(12):
        alpha = np.zeros((87, 93), np.float32)
        alpha[10+i:15+i, 30:36] = 1
        sources.append(alpha)
    expected = [OutlineDistanceCache(0).field(alpha)[0] for alpha in sources]
    with ThreadPoolExecutor(4) as executor:
        results = list(executor.map(cache.field, sources * 3))
    for i, result in enumerate(results):
        np.testing.assert_array_equal(result[0], expected[i % len(sources)])
        assert not result[0].flags.writeable
    assert 0 < cache.bytes <= cache.budget
    cache.clear()
    assert cache.bytes == 0
