"""Raster results, document coordinates, and hostile equations for Distort."""
import base64

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QIODevice, QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.distort import DISTORT_TYPES
from comic_editor.core.models import DistortModifier
from comic_editor.ui.distort_equations import evaluate_equation, validate_equation
from comic_editor.ui.distort_rendering import _image, _mls, _rgba, distort_bounds, render_distort


BOUNDS = QRectF(0, 0, 64, 48)


def render_in_source_frame(image, bounds, mod):
    """Compare effect controls in a common frame, independently of expansion."""
    return render_distort(image, bounds, mod, output_bounds=bounds)


def source_image():
    x, y = np.meshgrid(np.arange(64), np.arange(48))
    alpha = np.where((x < 3) | (y < 3) | (x > 59) | (y > 43), 0., .5 + .5 * (x % 7) / 6.)
    rgb = np.stack((x / 63., y / 47., ((x // 4 + y // 4) % 2)), axis=-1)
    return _image(np.concatenate((rgb * alpha[..., None], alpha[..., None]), axis=-1))


def modifier(effect, **parameters):
    result = DistortModifier(modifier_type="distort_" + effect, frame=(0., 0., 64., 48.), center=(32., 24.), radius=24., parameters=parameters)
    result.validate()
    return result


@pytest.mark.parametrize("effect,parameters", [
    ("twirl", {"angle": 0}), ("pinch_punch", {"amount": 0}), ("spherical", {"amount": 0}),
    ("ripple", {"amount": 0}), ("lens_distortion", {"amount": 0}),
    ("lens_correction", {}), ("perspective", {}), ("deform", {}), ("mesh_warp", {}),
    ("affine", {}), ("shear", {}), ("equations", {}), ("displace", {"amount": 0}),
])
def test_neutral_effects_preserve_every_source_pixel(effect, parameters):
    source = source_image()
    mod = modifier(effect, **parameters)
    assert distort_bounds(BOUNDS, mod) == BOUNDS
    actual = render_distort(source, BOUNDS, mod)
    np.testing.assert_allclose(_rgba(actual), _rgba(source), atol=1 / 255.)


_ACTIVE = [
    ("twirl", {"angle": 130}), ("pinch_punch", {"amount": 65}), ("spherical", {"amount": -85}),
    ("ripple", {"amount": 7., "wavelength": 17.}), ("lens_distortion", {"amount": 65}),
    ("rectangular_to_polar", {}), ("polar_to_rectangular", {}), ("pixelate", {"size": 9}),
    ("displace", {"amount": 12., "method": "red_green"}), ("mirror", {"mirrors": 4, "input_angle": 20.}),
    ("affine", {"rotation": 20., "scale_x": 140., "offset_x": 20.}),
    ("shear", {"horizontal_curve": [[0, 0], [.4, .3], [1, 0]]}),
    ("equations", {"x_expression": "x+8*sin(y/6)", "y_expression": "y"}),
]


@pytest.mark.parametrize("effect,parameters", _ACTIVE)
def test_all_non_geometric_effects_change_pixels_and_keep_premultiplication(effect, parameters):
    source = source_image()
    result = _rgba(render_distort(source, BOUNDS, modifier(effect, **parameters), output_bounds=BOUNDS))
    original = _rgba(source)
    assert np.max(np.abs(result - original)) > .08
    assert result[..., 3].max() > .4
    assert np.all(result[..., :3] <= result[..., 3:4] + 1 / 255.)


@pytest.mark.parametrize("effect", ["deform", "perspective", "mesh_warp"])
def test_pin_quad_and_mesh_destination_handles_change_image(effect):
    mod = modifier(effect)
    if effect == "deform":
        mod.source_points = [(0, 0), (1, 0), (1, 1), (0, 1), (.5, .5)]
        mod.points = [(0, 0), (1, 0), (1, 1), (0, 1), (.7, .4)]
    else:
        mod.points[0] = (-.2, .3)
    result = _rgba(render_distort(source_image(), BOUNDS, mod, output_bounds=BOUNDS))
    assert np.max(np.abs(result - _rgba(source_image()))) > .3
    assert result[..., 3].sum() > 250
    assert np.all(result[..., :3] <= result[..., 3:4] + 1e-6)


@pytest.mark.parametrize("mode", list(DISTORT_TYPES["distort_glitch"]["parameters"]["mode"]["choices"]))
def test_every_glitch_mode_produces_a_deterministic_distinct_effect(mode):
    source = source_image()
    mod = modifier("glitch", mode=mode, amount=75, offset_x=12, offset_y=5, spacing=7, channel_order="bgr")
    if mode in {"blast", "blast_color", "shred", "shred_color", "warp"}:
        mod.parameters.update(horizontal_strength=45., vertical_strength=20.)
    first = _rgba(render_in_source_frame(source, BOUNDS, mod))
    second = _rgba(render_in_source_frame(source, BOUNDS, mod))
    np.testing.assert_array_equal(first, second)
    assert np.max(np.abs(first - _rgba(source))) > .01, mode
    assert np.all(first[..., :3] <= first[..., 3:4] + 1e-6), mode


@pytest.mark.parametrize("mode", list(DISTORT_TYPES["distort_glitch"]["parameters"]["mode"]["choices"]))
def test_glitch_zero_strength_is_identity(mode):
    source = source_image()
    result = render_distort(source, BOUNDS, modifier("glitch", mode=mode, amount=0))
    np.testing.assert_allclose(_rgba(result), _rgba(source), atol=1 / 255.)


def test_affine_translation_expands_bounds_and_translates_exact_pixels():
    mod = modifier("affine", offset_x=25, offset_y=25, interpolation="nearest")
    bounds = distort_bounds(BOUNDS, mod)
    assert bounds == QRectF(0, 0, 80, 60)
    actual = _rgba(render_distort(source_image(), BOUNDS, mod))
    np.testing.assert_array_equal(actual[12:60, 16:80], _rgba(source_image()))
    assert not actual[:12].any()
    assert not actual[:, :16].any()


def test_perspective_destination_translation_maps_pixels_exactly():
    mod = modifier("perspective", interpolation="nearest")
    mod.points = [(x + .25, y + .25) for x, y in mod.points]
    result = _rgba(render_distort(source_image(), BOUNDS, mod))
    np.testing.assert_array_equal(result[12:60, 16:80], _rgba(source_image()))


def test_deform_fixed_pins_and_similarity_transform_are_exact():
    pins = np.asarray(((0., 0.), (100., 0.), (100., 100.), (0., 100.)))
    dest = pins.copy()
    dest[2] += (12., -15.)
    np.testing.assert_allclose(_mls(pins, pins, dest), dest, atol=1e-6)
    query = np.asarray(((10., 15.), (20., 90.), (50., 50.)))
    np.testing.assert_allclose(_mls(query, pins, pins * 1.5 + (7., -3.), "similarity"), query * 1.5 + (7., -3.), atol=1e-6)


def test_pixelate_averages_color_and_alpha_instead_of_picking_one_pixel():
    pixels = np.zeros((4, 4, 4), np.float32)
    pixels[:2, :2] = (1., 0., 0., 1.)
    mod = modifier("pixelate", size=4)
    mod.frame = (0, 0, 4, 4)
    image = render_distort(_image(pixels), QRectF(0, 0, 4, 4), mod)
    expected = np.broadcast_to((.25, 0., 0., .25), (4, 4, 4))
    np.testing.assert_allclose(_rgba(image), expected, atol=1 / 255.)


@pytest.mark.parametrize("effect,parameters", [("twirl", {"angle": 200.}), ("affine", {"rotation": 20.}), ("equations", {"x_expression": "x+10*sin(y/8)"}), ("glitch", {"mode": "warp", "amount": 60.})])
def test_world_translation_does_not_change_relative_effect(effect, parameters):
    mod = modifier(effect, **parameters)
    baseline = _rgba(render_distort(source_image(), BOUNDS, mod, output_bounds=BOUNDS))
    transform = QTransform.fromTranslate(173., -57.)
    mod.frame = (173., -57., 64., 48.)
    mod.center = (205., -33.)
    shifted = _rgba(render_distort(source_image(), BOUNDS, mod, transform, output_bounds=BOUNDS))
    np.testing.assert_allclose(shifted, baseline, atol=1 / 255.)


@pytest.mark.parametrize("edge,expected", [("transparent", (0., 0., 0., 0.)), ("white", (1., 1., 1., 1.)), ("clamp", (1., 0., 0., 1.)), ("wrap", (1., 0., 0., 1.)), ("mirror", (1., 0., 0., 1.))])
def test_equation_outside_image_boundary_modes(edge, expected):
    image = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(0xffff0000)
    mod = modifier("equations", x_expression="x-16", edges=edge)
    mod.frame = (0., 0., 8., 8.)
    result = _rgba(render_distort(image, QRectF(0, 0, 8, 8), mod))
    np.testing.assert_allclose(result, np.broadcast_to(expected, result.shape), atol=1 / 255.)


def test_cartesian_and_polar_equations_identity_and_parameters():
    source = source_image()
    mod = modifier("equations", coordinates="polar", x_expression="r", y_expression="t")
    np.testing.assert_allclose(_rgba(render_distort(source, BOUNDS, mod)), _rgba(source), atol=1 / 255.)
    values = evaluate_equation("a*sin(x*pi/w)+max(b,c)", dict(x=np.asarray((0, 50, 100)), w=100, a=7, b=2, c=3))
    np.testing.assert_allclose(values, (3, 10, 3), atol=1e-6)


@pytest.mark.parametrize("expression", ["__import__('os').system('echo unsafe')", "x.__class__", "[x for x in [1]]", "open('file')", "().__class__.__bases__", "lambda: x", "'hello'", "unknown+1", "sin(x, **{})", "1e999"])
def test_equations_cannot_execute_python(expression):
    with pytest.raises(ValueError):
        validate_equation(expression)


def test_nonfinite_equations_are_transparent_without_invalid_array_indices():
    mod = modifier("equations", x_expression="x/0", y_expression="sqrt(-1)")
    result = _rgba(render_distort(source_image(), BOUNDS, mod))
    assert not result.any()


def test_displace_embedded_red_green_map_moves_expected_pixels_and_preserves_alpha():
    map_image = QImage(4, 4, QImage.Format.Format_RGBA8888)
    map_image.fill(QColor(255, 128, 128, 255))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    map_image.save(buffer, "PNG")
    mod = modifier("displace", amount=8, method="red_green", map_source="embedded", map_png=base64.b64encode(bytes(buffer.data())).decode())
    source = source_image()
    result = _rgba(render_in_source_frame(source, BOUNDS, mod))
    # Full red displaces X by eight document pixels; neutral green is near-zero.
    np.testing.assert_allclose(result[10:30, 10:40], _rgba(source)[10:30, 18:48], atol=.04)
    mod.parameters["preserve_alpha"] = True
    preserved = _rgba(render_in_source_frame(source, BOUNDS, mod))
    np.testing.assert_array_equal(preserved[..., 3], _rgba(source)[..., 3])


def test_cancellation_and_output_size_guard():
    mod = modifier("twirl", angle=80)
    assert render_distort(source_image(), BOUNDS, mod, cancelled=lambda: True) is None
    checks = []
    def cancel_after_first_strip():
        checks.append(True)
        return len(checks) > 2
    assert render_distort(source_image(), BOUNDS, mod, output_bounds=QRectF(0, 0, 64, 512), cancelled=cancel_after_first_strip) is None
    with pytest.raises(ValueError, match="too large"):
        render_distort(source_image(), BOUNDS, mod, output_bounds=QRectF(0, 0, 100000, 100000))


def test_draft_has_correct_world_grid_and_dimensions():
    mod = modifier("equations", x_expression="x+4", interpolation="nearest")
    source = source_image()
    result = _rgba(render_distort(source, BOUNDS, mod, pixel_scale=.5))
    assert result.shape == (24, 32, 4)
    np.testing.assert_array_equal(result[:, :29], _rgba(source)[1::2, 5:63:2])


@pytest.mark.parametrize("edge", ["transparent", "white", "clamp", "wrap", "mirror"])
def test_bicubic_neutral_warp_preserves_border_pixels(edge):
    rng = np.random.default_rng(128)
    pixels = rng.random((48, 64, 4), dtype=np.float32)
    pixels[..., :3] *= pixels[..., 3:4]
    source = _image(pixels)
    mod = modifier("twirl", angle=0, interpolation="bicubic", edges=edge)
    np.testing.assert_array_equal(_rgba(render_distort(source, BOUNDS, mod)), _rgba(source))


@pytest.mark.parametrize("points", [
    [(0, 0), (0, 0), (1, 1), (0, 1)],
    [(0, 0), (1, 1), (1, 0), (0, 1)],
    [(0, 0), (1000000000, 0), (1, 1), (0, 1)],
    [(0, 0), (1, 0), (.2, .2), (0, 1)],
])
def test_invalid_transient_perspective_is_neutral_and_has_safe_bounds(points):
    mod = modifier("perspective")
    mod.points = points
    assert distort_bounds(BOUNDS, mod) == BOUNDS
    np.testing.assert_array_equal(_rgba(render_distort(source_image(), BOUNDS, mod)), _rgba(source_image()))


def test_singular_affine_is_neutral_and_has_safe_bounds():
    mod = modifier("affine", shear_x=45., shear_y=45.)
    assert distort_bounds(BOUNDS, mod) == BOUNDS
    np.testing.assert_array_equal(_rgba(render_distort(source_image(), BOUNDS, mod)), _rgba(source_image()))


def test_actual_lens_profile_changes_pixels_using_focal_calibration():
    from comic_editor.core.lens_profiles import load_lens_catalog
    lenses = load_lens_catalog().lenses
    assert lenses
    # Profile records expose their actual coefficients, and all are genuine data.
    mod = modifier("lens_correction", lens_profile=lenses[0].id, focal_length=18.)
    result = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    assert np.max(np.abs(result - _rgba(source_image()))) > .02


def test_shred_and_blast_have_independent_horizontal_vertical_strengths():
    source = source_image()
    for mode in ("shred", "blast", "shred_color", "blast_color", "warp"):
        horizontal = modifier("glitch", mode=mode, amount=0, horizontal_strength=20., vertical_strength=0., spacing=8)
        vertical = modifier("glitch", mode=mode, amount=0, horizontal_strength=0., vertical_strength=20., spacing=8)
        result_h = _rgba(render_in_source_frame(source, BOUNDS, horizontal))
        result_v = _rgba(render_in_source_frame(source, BOUNDS, vertical))
        assert np.max(np.abs(result_h - _rgba(source))) > .1
        assert np.max(np.abs(result_v - _rgba(source))) > .1
        assert np.max(np.abs(result_h - result_v)) > .1


@pytest.mark.parametrize("effect", ["twirl", "pixelate", "displace", "mesh_warp"])
def test_draft_preprocessing_uses_at_most_512_source_edge(monkeypatch, effect):
    import comic_editor.ui.distort_rendering as rendering
    source = QImage(1200, 900, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor(170, 80, 220, 200))
    seen = []
    original = rendering._rgba
    def record_size(image):
        seen.append(max(image.width(), image.height()))
        return original(image)
    monkeypatch.setattr(rendering, "_rgba", record_size)
    mod = modifier(effect, interpolation="bicubic")
    mod.frame = (0., 0., 1200., 900.)
    mod.center = (600., 450.)
    mod.radius = 400.
    result = rendering.render_distort(source, QRectF(0, 0, 1200, 900), mod, output_bounds=QRectF(0, 0, 1200, 900), pixel_scale=.1)
    assert (result.width(), result.height()) == (120, 90)
    assert seen and max(seen) <= 512


def test_draft_embedded_map_downsizes_before_float_conversion(monkeypatch):
    import comic_editor.ui.distort_rendering as rendering
    map_image = QImage(1600, 800, QImage.Format.Format_ARGB32_Premultiplied)
    map_image.fill(QColor(190, 120, 50))
    buffer = QBuffer()
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    map_image.save(buffer, "PNG")
    mod = modifier("displace", map_source="embedded", map_png=base64.b64encode(bytes(buffer.data())).decode())
    seen = []
    original = rendering._rgba
    def record_size(image):
        seen.append(max(image.width(), image.height()))
        return original(image)
    monkeypatch.setattr(rendering, "_rgba", record_size)
    assert rendering.render_distort(source_image(), BOUNDS, mod, pixel_scale=.5) is not None
    assert len(seen) == 2 and max(seen) <= 512


def test_single_mirror_keeps_top_half_and_reflects_it_below():
    x, y = np.meshgrid(np.arange(64), np.arange(48))
    pixels = np.stack((x / 63., y / 47., (y >= 24).astype(float), np.ones_like(x)), axis=-1)
    source = _image(pixels)
    mod = modifier("mirror", mirrors=1)
    result = _rgba(render_in_source_frame(source, BOUNDS, mod))
    original = _rgba(source)
    np.testing.assert_array_equal(result[:24], original[:24])
    np.testing.assert_array_equal(result[24:], original[:24][::-1])


def test_combined_shear_bounds_include_sequential_displacement_and_all_pixels():
    source = source_image()
    mod = modifier("shear", horizontal=200., vertical=200.)
    bounds = distort_bounds(BOUNDS, mod)
    assert bounds == QRectF(-64., -144., 192., 336.)
    result = _rgba(render_distort(source, BOUNDS, mod))
    # Shears preserve area; no content should disappear at the expanded edges.
    np.testing.assert_allclose(result[..., 3].sum(), _rgba(source)[..., 3].sum(), rtol=.005)


def test_neutral_shear_does_not_expand_rotated_layers():
    transform = QTransform().rotate(35.)
    mod = modifier("shear")
    assert distort_bounds(BOUNDS, mod, transform) == BOUNDS
    np.testing.assert_allclose(_rgba(render_distort(source_image(), BOUNDS, mod, transform)), _rgba(source_image()), atol=1 / 255.)


def test_glitch_quantisation_spatially_averages_selected_channel_only():
    x, y = np.meshgrid(np.arange(8), np.arange(8))
    pixels = np.stack((x / 7., y / 7., ((x + y) % 2).astype(float), np.ones_like(x)), axis=-1)
    source = _image(pixels)
    mod = modifier("glitch", mode="quantisation", amount=100., spacing=4., channels=1, channel_order="rgb", offset_x=0., offset_y=0.)
    mod.frame = (0., 0., 8., 8.)
    result = _rgba(render_distort(source, QRectF(0, 0, 8, 8), mod))
    original = _rgba(source)
    np.testing.assert_allclose(result[:4, :4, 0], original[:4, :4, 0].mean(), atol=1 / 255.)
    np.testing.assert_array_equal(result[..., 1:], original[..., 1:])
    assert not np.array_equal(result[..., 0], original[..., 0])


@pytest.mark.parametrize("key,value", [("spacing", 12.), ("offset_x", 3.), ("offset_y", 4.), ("channels", 2), ("channel_order", "bgr")])
def test_quantisation_visible_controls_change_spatial_channel_result(key, value):
    mod = modifier("glitch", mode="quantisation", amount=90., spacing=7., channels=3, channel_order="rgb", offset_x=0., offset_y=0.)
    baseline = _rgba(render_distort(source_image(), BOUNDS, mod))
    mod.parameters[key] = value
    changed = _rgba(render_distort(source_image(), BOUNDS, mod))
    assert np.max(np.abs(changed - baseline)) > .01


def test_fuzz_channel_selection_order_and_strength_are_independent():
    source = _image(np.full((48, 64, 4), (.5, .5, .5, 1.), np.float32))
    mod = modifier("glitch", mode="fuzz", amount=100., channels=1, channel_order="rgb", offset_x=0., offset_y=0.)
    red = _rgba(render_distort(source, BOUNDS, mod))
    original = _rgba(source)
    assert red[..., 0].std() > .1
    np.testing.assert_array_equal(red[..., 1:], original[..., 1:])
    mod.parameters["channel_order"] = "grb"
    green = _rgba(render_distort(source, BOUNDS, mod))
    np.testing.assert_array_equal(green[..., 0], original[..., 0])
    assert green[..., 1].std() > .1
    mod.parameters.update(channels=3, channel_order="rgb")
    all_channels = _rgba(render_distort(source, BOUNDS, mod))
    deviations = all_channels[..., :3].std(axis=(0, 1))
    assert deviations[0] > deviations[1] > deviations[2] > .04
    np.testing.assert_array_equal(all_channels[..., 3], original[..., 3])


@pytest.mark.parametrize("key,value", [("offset_x", 25.), ("offset_y", 15.), ("seed", 9.)])
def test_fuzz_visible_offsets_and_seed_change_result(key, value):
    mod = modifier("glitch", mode="fuzz", amount=80., offset_x=8., offset_y=4.)
    baseline = _rgba(render_distort(source_image(), BOUNDS, mod))
    mod.parameters[key] = value
    changed = _rgba(render_distort(source_image(), BOUNDS, mod))
    assert np.max(np.abs(changed - baseline)) > .03


def test_channel_flip_inverts_one_selected_channel_and_strength_controls_contrast():
    source = source_image()
    original = _rgba(source)
    mod = modifier("glitch", mode="channel_flip", amount=100., channel_order="grb", channels=3)
    result = _rgba(render_distort(source, BOUNDS, mod))
    np.testing.assert_allclose(result[..., 1], original[..., 3] - original[..., 1], atol=1 / 255.)
    np.testing.assert_array_equal(result[..., [0, 2, 3]], original[..., [0, 2, 3]])
    mod.parameters["amount"] = 50.
    halfway = _rgba(render_distort(source, BOUNDS, mod))
    np.testing.assert_allclose(halfway[..., 1], original[..., 3] / 2., atol=1 / 255.)
    mod.parameters["amount"] = -50.
    contrast = _rgba(render_distort(source, BOUNDS, mod))
    np.testing.assert_allclose(contrast[..., 1], np.clip(2. * original[..., 1] - original[..., 3] / 2., 0., original[..., 3]), atol=1 / 255.)


def test_light_streaks_horizontal_offset_controls_streak_length():
    mod = modifier("glitch", mode="light_streaks", amount=90., offset_x=8., offset_y=4.)
    short = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    mod.parameters["offset_x"] = 28.
    long = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    assert np.max(np.abs(long - short)) > .05


@pytest.mark.parametrize("mode", ["slice", "slice_color"])
def test_slice_has_independent_horizontal_and_vertical_offsets(mode):
    mod = modifier("glitch", mode=mode, amount=90., offset_x=0., offset_y=0., slice_offset=20., spacing=8.)
    baseline = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    mod.parameters["offset_x"] = 12.
    horizontal = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    mod.parameters.update(offset_x=0., offset_y=12.)
    vertical = _rgba(render_in_source_frame(source_image(), BOUNDS, mod))
    assert np.max(np.abs(horizontal - baseline)) > .1
    assert np.max(np.abs(vertical - baseline)) > .1
    assert np.max(np.abs(vertical - horizontal)) > .1
    mod.parameters["slice_offset"] = 0.
    np.testing.assert_array_equal(_rgba(render_in_source_frame(source_image(), BOUNDS, mod)), baseline)


def test_data_blocks_add_colored_square_regions_to_neutral_source():
    source = _image(np.full((48, 64, 4), (.4, .4, .4, 1.), np.float32))
    mod = modifier("glitch", mode="data_blocks", amount=100., spacing=8., edges="clamp")
    result = _rgba(render_in_source_frame(source, BOUNDS, mod))
    assert np.max(np.ptp(result[..., :3], axis=-1)) > .25
    # The data block contribution is spatially constant inside each square.
    for y in range(0, 48, 8):
        for x in range(0, 64, 8):
            block = result[y:y + 8, x:x + 8]
            np.testing.assert_array_equal(block, np.broadcast_to(block[0, 0], block.shape))


def test_aberration_single_channel_does_not_union_unselected_channel_alpha():
    pixels = np.zeros((48, 64, 4), np.float32)
    pixels[20:28, 28:36] = (1., 0., 0., 1.)
    source = _image(pixels)
    mod = modifier("glitch", mode="aberration_offset", amount=100., offset_x=12., offset_y=0., channels=1, channel_order="rgb", bidirectional=True)
    result = _rgba(render_in_source_frame(source, BOUNDS, mod))
    assert not result[:, :28, 3].any()  # No alpha from the unused opposite shift.
    assert result[24, 44, 0] == 1.
    mod.parameters["bidirectional"] = False
    one_sided = _rgba(render_in_source_frame(source, BOUNDS, mod))
    assert one_sided[24, 20, 0] == 1.
    assert not np.array_equal(one_sided, _rgba(source))
