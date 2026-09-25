"""Independent channel-color and premultiplied transparency checks for Curves."""
from copy import deepcopy

import numpy as np
import pytest
from PySide6.QtGui import QImage

from comic_editor.core.curves import (
    CURVE_CHANNELS, apply_curves, blend_colors, cmyk_to_rgb, curve_graph_values,
    evaluate_curve, lab_to_rgb, rgb_to_cmyk, rgb_to_lab, validate_curve_points,
)
from comic_editor.core.models import CurvesModifier, ParameterMaskBinding, modifier_from_dict
from comic_editor.core.modifier_presets import apply_modifier_preset, preset_from_modifier
from comic_editor.ui.modifier_rendering import (
    _premultiplied_qimage, _qimage_premultiplied, apply_modifier_stack,
)


INVERT = [(0., 1.), (1., 0.)]


def rgba_image():
    rng = np.random.default_rng(847)
    values = rng.random((19, 23, 4), dtype=np.float32)
    values[..., :3] *= values[..., 3:4]
    values[0, :4] = 0
    return _premultiplied_qimage(values)


def test_two_point_curve_golden_and_flat_endpoint_extension():
    np.testing.assert_allclose(
        evaluate_curve([(.25, .1), (.75, .9)], [-.5, 0, .25, .375, .5, .75, 1, 2]),
        [.1, .1, .1, .3, .5, .9, .9, .9], atol=1e-7)
    np.testing.assert_allclose(evaluate_curve(INVERT, [0, .25, .5, 1]), [1, .75, .5, 0])


def test_spline_passes_anchors_without_overshooting_local_extrema():
    points = [(0., .1), (.25, .8), (.5, .2), (.75, .9), (1., .6)]
    np.testing.assert_allclose(evaluate_curve(points, [p[0] for p in points]), [p[1] for p in points])
    for (left, a), (right, b) in zip(points, points[1:]):
        values = evaluate_curve(points, np.linspace(left, right, 1001))
        assert values.min() >= min(a, b) - 1e-6
        assert values.max() <= max(a, b) + 1e-6


@pytest.mark.parametrize("mode", CURVE_CHANNELS)
@pytest.mark.parametrize("input_min,input_max", [(0, 1), (.2, .8), (0, 4)])
def test_identity_is_byte_exact_in_every_mode_and_graph_range(mode, input_min, input_max):
    image = rgba_image().convertToFormat(QImage.Format_RGBA8888)
    modifier = CurvesModifier(color_mode=mode, input_min=input_min, input_max=input_max,
                              curves={mode + ":master": [(0, 0), (.3, .3), (1, 1)]})
    actual = apply_modifier_stack(image, [modifier], (0, 0))
    assert actual.format() == image.format()
    assert bytes(actual.constBits()) == bytes(image.constBits())


def test_rgb_master_precedes_selected_channel_and_alpha_is_separate():
    source = np.array([[[.1, .2, .3, .5], [0, 0, 0, 0]]], np.float32)
    modifier = CurvesModifier(curves={"rgb:master": INVERT, "rgb:red": INVERT})
    result = apply_curves(source, modifier)
    np.testing.assert_allclose(result, [[[.1, .3, .2, .5], [0, 0, 0, 0]]], atol=1e-7)
    original = result.copy()
    modifier.channel = "blue"
    np.testing.assert_array_equal(apply_curves(source, modifier), original)
    modifier.curves["rgb:alpha"] = [(0, .8), (1, .8)]
    result = apply_curves(source, modifier)
    np.testing.assert_allclose(result, [[[.16, .48, .32, .8], [0, 0, 0, 0]]], atol=1e-7)


def test_range_rescales_both_axes_and_picker_uses_master_output():
    modifier = CurvesModifier(input_min=.2, input_max=.8, curves={"rgb:master": INVERT})
    source = np.array([[[.2, .5, .8, 1], [0, 1, .35, 1]]], np.float32)
    np.testing.assert_allclose(apply_curves(source, modifier)[..., :3],
                               [[[.8, .5, .2], [.8, .2, .65]]], atol=1e-7)
    master = curve_graph_values(source[..., :3], source[..., 3], modifier, "master")
    red = curve_graph_values(source[..., :3], source[..., 3], modifier, "red")
    np.testing.assert_allclose(master, [[[0, .5, 1], [0, 1, .25]]], atol=1e-7)
    np.testing.assert_allclose(red, [[1, 1]], atol=1e-7)


def test_cmyk_known_colors_and_round_trip():
    rgb = np.array([[0, 0, 0], [1, 1, 1], [1, 0, 0], [.2, .4, .6]], float)
    expected = [[0, 0, 0, 1], [0, 0, 0, 0], [0, 1, 1, 0], [2/3, 1/3, 0, .4]]
    np.testing.assert_allclose(rgb_to_cmyk(rgb), expected, atol=1e-12)
    np.testing.assert_allclose(cmyk_to_rgb(np.array(expected)), rgb, atol=1e-12)
    source = np.array([[[.2, .4, .6, 1]]], np.float32)
    modifier = CurvesModifier(color_mode="cmyk", curves={"cmyk:black": INVERT})
    np.testing.assert_allclose(apply_curves(source, modifier), [[[2/15, 4/15, .4, 1]]], atol=1e-7)


def test_cielab_d65_primary_goldens_and_round_trip():
    rgb = np.array([[0, 0, 0], [1, 1, 1], [1, 0, 0], [0, 1, 0], [0, 0, 1]], float)
    lab = rgb_to_lab(rgb) * [100, 255, 255] - [0, 128, 128]
    np.testing.assert_allclose(lab, [[0, 0, 0], [100, 0, 0], [53.2408, 80.0925, 67.2032],
                                    [87.7347, -86.1827, 83.1793], [32.2970, 79.1875, -107.8602]], atol=.001)
    np.testing.assert_allclose(lab_to_rgb(rgb_to_lab(rgb)), rgb, atol=2e-6)
    source = np.array([[[0, 0, 0, 1], [1, 1, 1, 1]]], np.float32)
    modifier = CurvesModifier(color_mode="lab", curves={"lab:lightness": [(0, .5), (1, .5)]})
    np.testing.assert_allclose(apply_curves(source, modifier)[..., :3], .4663266, atol=2e-6)


def test_gray_master_preserves_chromatic_ratios_and_maps_black_neutrally():
    source = np.array([[[.1, .2, .3, 1], [0, 0, 0, 1]]], np.float32)
    modifier = CurvesModifier(color_mode="gray", curves={"gray:master": [(0, .1), (1, .6)]})
    output = apply_curves(source, modifier)
    ratio = output[0, 0, :3] / source[0, 0, :3]
    np.testing.assert_allclose(ratio, ratio[0], atol=1e-7)
    np.testing.assert_allclose(output[0, 1], [.1, .1, .1, 1], atol=1e-7)


@pytest.mark.parametrize("mode,expected", [
    ("normal", .75), ("multiply", .1875), ("screen", .8125), ("overlay", .375),
    ("darken", .25), ("lighten", .75), ("soft_light", .375), ("hard_light", .625),
    ("difference", .5), ("exclusion", .625),
])
def test_blend_mode_goldens(mode, expected):
    np.testing.assert_allclose(blend_colors(np.array([.25]), np.array([.75]), mode), expected)


def test_intensity_mask_blends_premultiplied_alpha_without_empty_rectangles():
    image = rgba_image()
    original = _qimage_premultiplied(image)
    modifier = CurvesModifier(curves={"rgb:master": INVERT, "rgb:alpha": [(0, .7), (1, .7)]})
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    mask = np.tile(np.linspace(0, 1, image.width(), dtype=np.float32), (image.height(), 1))
    full = apply_curves(original, modifier)
    expected = original + (full - original) * mask[..., None]
    result = apply_modifier_stack(image, [modifier], (0, 0), {(modifier.modifier_id, "intensity"): mask})
    np.testing.assert_array_equal(_qimage_premultiplied(result), _qimage_premultiplied(_premultiplied_qimage(expected)))
    assert np.all(_qimage_premultiplied(result)[0, :4] == 0)


def test_color_curves_keep_alpha_bytes_and_input_unchanged():
    image = rgba_image()
    original = _qimage_premultiplied(image)
    modifier = CurvesModifier(curves={"rgb:master": INVERT}, intensity=37)
    result = _qimage_premultiplied(apply_modifier_stack(image, [modifier], (0, 0)))
    np.testing.assert_array_equal(result[..., 3], original[..., 3])
    np.testing.assert_array_equal(_qimage_premultiplied(image), original)


def test_curves_round_trip_presets_and_inactive_modes():
    modifier = CurvesModifier(color_mode="lab", channel="a", blend_mode="screen", input_max=2,
                              curves={"rgb:red": INVERT, "lab:a": [(0, .2), (1, .8)]})
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 100, 0)
    serialized = modifier.to_dict()
    assert modifier_from_dict(serialized).to_dict() == serialized
    preset = preset_from_modifier("Custom curve", modifier)
    loaded = apply_modifier_preset(CurvesModifier(), preset)
    assert loaded.curves == modifier.curves
    assert loaded.color_mode == "lab" and loaded.input_max == 2
    assert loaded.parameter_masks == {}
    before = deepcopy(modifier.curves)
    apply_curves(np.array([[[.2, .3, .4, 1]]], np.float32), modifier)
    assert modifier.curves == before
    inactive = CurvesModifier(curves={"lab:master": INVERT})
    image = rgba_image()
    assert apply_modifier_stack(image, [inactive], (0, 0)) == image


@pytest.mark.parametrize("points", [[], [(0, 0)], [(0, 0), (0, 1)],
                                    [(0, 0), (1, float("nan"))], [(0, 0), (1, 2)]])
def test_invalid_points_are_rejected(points):
    with pytest.raises(ValueError):
        validate_curve_points(points)


@pytest.mark.parametrize("kwargs", [{"color_mode": "xyz"}, {"blend_mode": "unknown"},
                                      {"input_min": 1, "input_max": 1},
                                      {"input_max": float("inf")}, {"curves": {"rgb:cyan": INVERT}}])
def test_invalid_settings_are_rejected(kwargs):
    with pytest.raises(ValueError):
        CurvesModifier(**kwargs).validate()
