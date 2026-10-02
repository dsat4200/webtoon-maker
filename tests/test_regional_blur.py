"""Compare regional resampling with the independent, existing Pillow kernels."""
import numpy as np
import pytest

from comic_editor.render.blur_regions import RegionalBlur, BlurTileCache
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
