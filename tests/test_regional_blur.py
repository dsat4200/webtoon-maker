"""Compare regional resampling with the independent, existing Pillow kernels."""
import numpy as np
import pytest

from comic_editor.render.blur_regions import (
    RegionalBlur, BlurTileCache, _coefficient_extent, _coefficients,
)
from comic_editor.ui.modifier_rendering import _variable_blur


@pytest.mark.parametrize("algorithm", ["normal", "legacy"])
@pytest.mark.parametrize("size", [(877, 603), (81, 129), (1, 47), (30001, 31)])
@pytest.mark.parametrize("masked", [False, True])
def test_regional_blur_matches_existing_odd_frame_pyramid(algorithm, size, masked):
    width, height = size
    pixels = np.random.default_rng(17).integers(0, 256, (height, width, 4), dtype=np.uint8)
    pixels[..., :3] = np.minimum(pixels[..., :3], pixels[..., 3:4])
    original = pixels.astype(np.float32) / 255.
    yy, xx = np.mgrid[:height, :width]
    strength = ((xx + yy) % 101).astype(np.float32) if masked else 11.25
    full = _variable_blur(original, strength, algorithm=algorithm)
    def fetch(region):
        x, y, w, h = region
        return pixels[y:y+h, x:x+w]
    retained = {}
    blur = RegionalBlur(size, fetch, retained.get, retained.__setitem__, algorithm)
    for x, y, w, h in [(0, 0, min(width, 111), min(height, 99)),
                        (max(0, width-201), max(0, height-207), min(width, 201), min(height, 207))]:
        field = strength[y:y+h, x:x+w] if masked else strength
        assert np.array_equal(blur.apply((x, y, w, h), field), full[y:y+h, x:x+w])


def test_detached_blur_preserves_sources_and_levels_after_cache_eviction():
    pixels = np.random.default_rng(17).integers(0, 256, (401, 503, 4), dtype=np.uint8)
    cache = BlurTileCache(4096)
    def fetch(r):
        x, y, w, h = r
        return pixels[y:y+h, x:x+w].copy()
    blur = RegionalBlur((503, 401), fetch, cache.get, cache.put)
    region = (80, 61, 101, 93)
    expected = blur.apply(region, 11.25)
    detached, memory = blur.detached(region, 11.25)
    # A later generation can evict every shared level before this job starts.
    for i in range(20):
        cache.put(("unrelated", i), np.zeros((16, 16, 4), np.uint8))
    pixels.fill(0)
    assert np.array_equal(detached.apply(region, 11.25), expected)
    assert memory > 0 and cache.bytes <= cache.budget


def test_detached_dependency_extents_match_padded_sampling_indexes():
    rng = np.random.default_rng(117)
    sizes = [(1, 47), (47, 1), (1081, 541), (54897, 27449), (30001, 235), (235, 30001)]
    sizes.extend((int(a), int(b)) for a, b in rng.integers(1, 10000, size=(80, 2)))
    for source, destination in sizes:
        for start in (0, destination // 3, max(0, destination - 19)):
            count = min(19, destination - start)
            indexes, _weights = _coefficients(source, destination, start, count)
            assert _coefficient_extent(source, destination, start, count) == (
                int(indexes.min()), int(indexes.max()) + 1)


@pytest.mark.parametrize("algorithm", ["normal", "legacy"])
@pytest.mark.parametrize("masked", [False, True])
def test_dependency_planning_fetches_same_source_rectangles_and_final_pixels(monkeypatch, algorithm, masked):
    import comic_editor.render.blur_regions as module
    pixels = np.random.default_rng(14).integers(0, 256, (603, 877, 4), dtype=np.uint8)
    region = (611, 383, 201, 173)
    strength = (np.random.default_rng(42).uniform(0, 100, (173, 201)).astype(np.float32)
                if masked else 31.25)

    def plan():
        fetched = []
        def fetch(rect):
            fetched.append(rect)
            x, y, width, height = rect
            return pixels[y:y+height, x:x+width].copy()
        retained = {}
        blur = RegionalBlur((877, 603), fetch, retained.get, retained.__setitem__, algorithm)
        detached, memory = blur.detached(region, strength)
        return fetched, memory, detached.apply(region, strength)

    rectangles, memory, actual = plan()
    def reference_extent(source, destination, start, count):
        indexes, _weights = _coefficients(source, destination, start, count)
        return int(indexes.min()), int(indexes.max()) + 1
    monkeypatch.setattr(module, "_coefficient_extent", reference_extent)
    reference_rectangles, reference_memory, expected = plan()
    assert rectangles == reference_rectangles and memory == reference_memory
    np.testing.assert_array_equal(actual, expected)
