"""Optimized HSL interpolation against an independent scalar reference."""
import colorsys

import numpy as np
import pytest

from comic_editor.ui.modifier_rendering import _hsl_effect


@pytest.mark.parametrize("offsets", [(-180, 0, 0), (180, 0, 0), (0, 0, 0),
                                   (0, 100, 0), (73, -100, -100),
                                   (-59, 100, 100), (37, 29, -14), None])
def test_hsl_channels_match_scalar_reference_with_alpha_and_parameter_fields(offsets):
    rng = np.random.default_rng(320)
    source = rng.random((12, 15, 4), dtype=np.float32)
    source[0, :, :3] = .4  # Achromatic pixels exercise undefined source hue.
    source[1, :, :3] = 1.
    source[2, :, :3] = 0.
    source[3, :, 3] = 0.
    source[..., :3] *= source[..., 3:4]
    if offsets is None:
        offsets = (rng.uniform(-180, 180, (12, 15)).astype(np.float32),
                   rng.uniform(-100, 100, (12, 15)).astype(np.float32),
                   rng.uniform(-100, 100, (12, 15)).astype(np.float32))
    fields = [np.broadcast_to(value, source.shape[:2]) for value in offsets]
    expected = np.empty_like(source)
    for y, x in np.ndindex(source.shape[:2]):
        alpha = float(source[y, x, 3])
        rgb = source[y, x, :3].astype(float) / alpha if alpha else (0., 0., 0.)
        hue, light, saturation = colorsys.rgb_to_hls(*rgb)
        dh, ds, dl = (float(field[y, x]) for field in fields)
        saturation += (1 - saturation) * ds / 100 if ds >= 0 else saturation * ds / 100
        light += (1 - light) * dl / 100 if dl >= 0 else light * dl / 100
        converted = colorsys.hls_to_rgb((hue + dh / 360) % 1, light, saturation)
        expected[y, x, :3] = np.asarray(converted) * alpha
        expected[y, x, 3] = alpha
    actual = _hsl_effect(source, *offsets)
    np.testing.assert_allclose(actual, expected, atol=1e-6)
    np.testing.assert_array_equal(actual[..., 3], source[..., 3])
    assert np.all(actual[..., :3] >= -1e-6)
    assert np.all(actual[..., :3] <= actual[..., 3:4] + 1e-6)
    assert np.max(np.abs((actual * 255).astype(int) - (expected * 255).astype(int))) <= 1
