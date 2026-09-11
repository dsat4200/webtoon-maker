"""Repeated-cell sampling must preserve every mark, color and tie-break."""
import numpy as np
import pytest

from comic_editor.core.models import HalftoneModifier
import comic_editor.ui.pattern_rendering as rendering


@pytest.mark.parametrize("grid", ["square", "hexagonal", "stippling"])
@pytest.mark.parametrize("style,mode", [("circle", "two"), ("polygon", "source"),
                                      ("blob", "gradient"), ("liquid", "target_layer")])
def test_cell_sampling_matches_per_pixel_neighborhoods(monkeypatch, grid, style, mode):
    rng = np.random.default_rng(908)
    values = rng.random((110, 160, 4), dtype=np.float32)
    values[..., :3] *= values[..., 3:4]
    source = rendering._image(values)
    colors = rendering._image(np.roll(values, 21, axis=1))
    modifier = HalftoneModifier(grid_type=grid, dot_style=style, color_mode=mode,
        base_resolution=160, spacing=15, rotation=29, size=1.7, blur=0,
        corner_rounding=.35, target_hue=24, target_saturation=-13)
    modifier.validate()
    table = rendering._cell_sample_table
    tables = []
    def record(*args):
        result = table(*args)
        tables.append(result)
        return result
    monkeypatch.setattr(rendering, "_cell_sample_table", record)
    optimized = rendering.apply_pattern_effect(source, modifier, color_source=colors)
    assert tables and tables[0] is not None
    monkeypatch.setattr(rendering, "_cell_sample_table", lambda *args: None)
    reference = rendering.apply_pattern_effect(source, modifier, color_source=colors)
    assert optimized == reference
