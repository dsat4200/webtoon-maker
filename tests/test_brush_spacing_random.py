"""Random gap choices are made once per distance interval, never per event."""
from dataclasses import replace
import math
import struct

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput
from comic_editor.core.brush_stroke import BrushStroke, curve_value
from comic_editor.core.sut_import import _dynamics


def _draw(brush, samples, *, seed=42, path_length=None):
    stroke = BrushStroke(brush, seed, path_length=path_length)
    result = stroke.begin(samples[0])
    for sample in samples[1:]:
        result.extend(stroke.add(sample))
    result.extend(stroke.finish())
    return result


def _line(length, step=None, *, pressure=.8):
    xs = [0, length] if step is None else [*range(0, length, step), length]
    return [BrushInput(x, 0, pressure=pressure, time=x/500) for x in xs]


def _source_effector(header, curve=()):
    # Exact relevant records from the registered blood02 / blood08 fixtures.
    payload = struct.pack(">11I", *header)
    if curve:
        payload += struct.pack(">3I", 12, len(curve), 16)
        payload += b"".join(struct.pack(">2d", *point) for point in curve)
    return _dynamics(payload)


BLOOD02_PRIMARY = _source_effector((44, 496, 128, 0, 0, 0, 63, 0, 0, 0, 100))
BLOOD02_SECONDARY = _source_effector((44, 240, 128, 0, 100, 0, 27, 0, 0, 0, 500))
BLOOD08_PRIMARY = _source_effector((44, 240, 144, 37, 0, 0, 6, 0, 76, 0, 100),
    ((0., 1.), (.7454545454545455, .00909090909090909), (.7545454545454545, 0.), (1., 0.)))


@pytest.mark.parametrize("minimum", [0, .27, .63, .95])
def test_random_gaps_obey_bounds_vary_and_reproduce_for_a_seed(minimum):
    brush = BrushDefinition(size=40, spacing=1,
                            dynamics={"spacing": BrushDynamics(random=minimum)})
    first = _draw(brush, _line(4000))
    assert first == _draw(brush, _line(4000))
    assert first != _draw(brush, _line(4000), seed=43)
    gaps = [b.distance-a.distance for a, b in zip(first, first[1:])]
    assert min(gaps) >= max(.25, 40*minimum) - 1e-9
    assert max(gaps) <= 40 + 1e-9
    assert len({round(gap, 6) for gap in gaps}) > 20


def test_disabled_randomness_preserves_dab_positions_and_other_random_channels():
    brush = BrushDefinition(size=20, spacing=.5, hue_jitter=.3, flip_x="random")
    disabled = replace(brush, dynamics={"spacing": BrushDynamics(random=1)})
    assert _draw(brush, _line(300)) == _draw(disabled, _line(300))
    assert [dab.x for dab in _draw(disabled, _line(300))] == pytest.approx(list(range(0, 301, 10)))


def test_gap_rng_is_independent_of_particle_material_queries_and_color_channels():
    brush = BrushDefinition(size=40, spacing=.5,
                            dynamics={"spacing": BrushDynamics(random=.2)})
    changed = replace(brush, density_by_gap=True, hue_jitter=.4, flip_x="random")
    assert [dab.x for dab in _draw(brush, _line(500))] == [dab.x for dab in _draw(changed, _line(500))]
    one = replace(brush, spray=True, particle_density=1)
    many = replace(one, particle_density=12, particle_angle_random=1)
    centers = lambda dabs: list(dict.fromkeys(dab.distance for dab in dabs))
    assert centers(_draw(one, _line(500))) == centers(_draw(many, _line(500)))


@pytest.mark.parametrize("dynamic,size,spacing", [
    (BLOOD02_PRIMARY, 700, 3), (BLOOD02_SECONDARY, 700, 2.5), (BLOOD08_PRIMARY, 800, .446)])
def test_actual_blood_gap_records_vary_with_seed_and_keep_input_packet_phase(dynamic,size,spacing):
    brush = BrushDefinition(size=size, spacing=spacing, direction="stroke", dynamics={"spacing": dynamic})
    coarse = _draw(brush, _line(30000))
    fine = _draw(brush, _line(30000, 37))
    assert len(coarse) == len(fine)
    for a, b in zip(coarse, fine):
        assert (a.x, a.distance, a.time, a.pressure) == pytest.approx((b.x, b.distance, b.time, b.pressure))
    assert [dab.x for dab in coarse] != [dab.x for dab in _draw(brush, _line(30000), seed=43)]
    factor = dynamic.minimum+(1-dynamic.minimum)*curve_value(dynamic.pressure_curve, .8) if dynamic.pressure else 1
    for a, b in zip(coarse, coarse[1:]):
        gap = b.distance-a.distance
        assert max(.25, size*spacing*factor*dynamic.random)-1e-8 <= gap <= size*spacing*factor+1e-8


def test_pressure_profile_and_primary_secondary_phase_survive_packet_subdivision():
    primary = BrushDefinition(size=40, spacing=.8, dynamics={"spacing": BLOOD08_PRIMARY,
                              "size": BrushDynamics(pressure=True, minimum=.3)})
    secondary = replace(primary, size=25, spacing=1.2,
                         dynamics={"spacing": BLOOD02_SECONDARY})
    def samples(step):
        return [BrushInput(x, 0, pressure=.2+.6*(x/800), time=x/400) for x in range(0, 801, step)]
    for brush, seed in ((primary, 42), (secondary, 42 ^ 271828)):
        coarse, fine = _draw(brush, samples(800), seed=seed), _draw(brush, samples(1), seed=seed)
        assert len(coarse) == len(fine)
        for a, b in zip(coarse, fine):
            assert (a.distance, a.pressure, a.size) == pytest.approx((b.distance, b.pressure, b.size), abs=1e-8)
            assert a.pressure == pytest.approx(.2+.6*a.distance/800)


def test_start_end_and_fade_spacing_envelopes_do_not_reset_random_sequence():
    base = BrushDefinition(size=40, spacing=.5, taper_parameters=("spacing",),
                            taper_start=80, taper_end=100, taper_minimum=.25,
                            dynamics={"spacing": BrushDynamics(random=.3)})
    for brush in (base, replace(base, taper_mode="fade", taper_start=0)):
        coarse = _draw(brush, _line(400), path_length=400)
        fine = _draw(brush, _line(400, 1), path_length=400)
        assert [dab.distance for dab in coarse] == pytest.approx([dab.distance for dab in fine])
        assert all(math.isfinite(dab.distance) for dab in coarse)
        assert coarse[-1].distance <= 400


def test_stationary_events_and_deterministic_gap_queries_do_not_consume_interval_randomness():
    brush = BrushDefinition(size=20, spacing=1, dynamics={"spacing": BrushDynamics(random=.3)})
    stroke = BrushStroke(brush, 42)
    result = stroke.begin(BrushInput(0, 0))
    for i in range(40):
        stroke._spacing(BrushInput(0, 0), 0)  # density normalization must be observational
        result += stroke.add(BrushInput(0, 0, time=(i+1)/100))
    result += stroke.add(BrushInput(400, 0, time=1))
    result += stroke.finish()
    assert [dab.distance for dab in result] == [dab.distance for dab in _draw(brush, _line(400))]
