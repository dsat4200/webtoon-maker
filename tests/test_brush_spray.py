"""Spray directions are distinct from the whole-brush orientation."""
from dataclasses import replace
import math

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput
from comic_editor.core.brush_stroke import BrushStroke


def _draw(brush, samples=None, seed=17):
    samples = samples or [BrushInput(0, 0), BrushInput(0, 80, time=1)]
    stroke = BrushStroke(brush, seed)
    result = stroke.begin(samples[0])
    for sample in samples[1:]:
        result += stroke.add(sample)
    return result + stroke.finish()


def _spray(**kwargs):
    return replace(BrushDefinition(size=40, spacing=.5, spray=True,
                                   particle_density=8), **kwargs)


def _delta(angle, reference):
    return (angle - reference + 180) % 360 - 180


def test_negative_spray_deviation_moves_particles_to_edge_inside_footprint():
    mean_radii = []
    for bias in (-1,0,1):
        brush = _spray(spray_deviation=bias,particle_density=100)
        assert BrushDefinition.from_dict(brush.to_dict()).spray_deviation == bias
        dabs = _draw(brush)
        radii = [math.hypot(dab.x-dab.center_x,dab.y-dab.center_y) for dab in dabs]
        assert all(0 <= radius <= brush.size*.5+1e-10 for radius in radii)
        mean_radii.append(sum(radii)/len(radii))
    assert mean_radii[0] > mean_radii[1]+3
    assert mean_radii[1] > mean_radii[2]+3


def test_independent_particles_ignore_whole_brush_angle_and_its_dynamics():
    brush = _spray(angle=120, direction="stroke", particle_direction="fixed",
                   particle_angle=25, dynamics={"angle": BrushDynamics(random=0)})
    assert {dab.angle for dab in _draw(brush)} == {25}


def test_line_direction_is_applied_once_even_when_whole_spray_follows_line():
    brush = _spray(angle=50, direction="stroke", particle_direction="stroke",
                   particle_angle=15)
    assert {dab.angle for dab in _draw(brush)} == {105}


def test_first_particle_waits_for_line_direction_without_whole_angle_dynamics():
    brush = _spray(direction="fixed", particle_direction="stroke", particle_angle=5)
    stroke = BrushStroke(brush)
    assert stroke.begin(BrushInput(0, 0)) == []
    dabs = stroke.add(BrushInput(0, 20)) + stroke.finish()
    assert len(dabs) == 16
    assert {dab.angle for dab in dabs} == {95}
    assert {dab.angle for dab in _draw(brush, [BrushInput(5, 5)])} == {5}


def test_whole_spray_particles_inherit_resolved_angle_and_add_own_offset():
    brush = _spray(angle=20, direction="tilt", particle_direction="whole_spray",
                   particle_angle=15)
    dabs = _draw(brush, [BrushInput(0, 0, tilt_y=30), BrushInput(0, 20, tilt_y=30)])
    assert {dab.angle for dab in dabs} == {125}


@pytest.mark.parametrize("offset", [0, 90, -35])
def test_center_direction_points_from_each_particle_to_its_own_spray_center(offset):
    brush = _spray(angle=90, direction="stroke", particle_direction="center",
                   particle_angle=offset)
    for dab in _draw(brush):
        inward = math.degrees(math.atan2(dab.center_y-dab.y, dab.center_x-dab.x))
        assert _delta(dab.angle, inward+offset) == pytest.approx(0, abs=1e-9)


@pytest.mark.parametrize("mode", ["fixed", "stroke", "whole_spray", "center"])
def test_angle_randomness_adds_to_each_direction_and_is_seeded(mode):
    brush = _spray(particle_direction=mode, particle_angle=20, particle_angle_random=.1,
                   angle=30, direction="stroke", particle_density=64)
    samples = [BrushInput(0, 0), BrushInput(0, 40)]
    dabs = _draw(brush, samples)
    assert dabs == _draw(brush, samples)
    assert dabs != _draw(brush, samples, seed=18)
    offsets = []
    for dab in dabs:
        reference = 20
        if mode == "stroke":
            reference += 90
        elif mode == "whole_spray":
            reference += 120
        elif mode == "center":
            reference += math.degrees(math.atan2(dab.center_y-dab.y, dab.center_x-dab.x))
        offsets.append(_delta(dab.angle, reference))
    assert min(offsets) >= -18
    assert max(offsets) <= 18
    assert min(offsets) < -10 and max(offsets) > 10


def test_particle_randomness_remains_independent_of_input_packet_count():
    brush = _spray(particle_direction="center", particle_angle_random=.4)
    coarse = _draw(brush, [BrushInput(0, 0), BrushInput(0, 80, time=1)])
    fine = _draw(brush, [BrushInput(0, y, time=y/80) for y in range(81)])
    assert len(coarse) == len(fine)
    for a, b in zip(coarse, fine):
        assert (a.x, a.y, a.angle) == pytest.approx((b.x, b.y, b.angle), abs=1e-9)


def test_legacy_fixed_presets_migrate_without_changing_whole_spray_direction():
    legacy = {"spray": True, "angle": 70, "direction": "stroke", "particle_direction": "fixed"}
    migrated = BrushDefinition.from_dict(legacy)
    assert migrated.particle_direction == "whole_spray"
    assert {dab.angle for dab in _draw(migrated)} == {160}
    restored = BrushDefinition.from_dict(migrated.to_dict())
    assert restored == migrated
    independent = BrushDefinition.from_dict({**legacy, "particle_angle": 0})
    assert independent.particle_direction == "fixed"
    assert {dab.angle for dab in _draw(independent)} == {0}


def test_legacy_radial_and_random_modes_keep_whole_angle_behavior():
    radial = _spray(angle=40, particle_direction="radial")
    for dab in _draw(radial):
        outward = math.degrees(math.atan2(dab.y-dab.center_y, dab.x-dab.center_x))
        assert _delta(dab.angle, outward+40) == pytest.approx(0, abs=1e-9)
    random = _draw(replace(radial, particle_direction="random"))
    assert len({round(dab.angle) for dab in random}) > 10


def test_particle_settings_roundtrip_on_both_brushes_and_do_not_affect_dry_tips():
    primary = _spray(particle_angle=45, particle_direction="stroke", particle_angle_random=.3)
    secondary = _spray(particle_angle=90, particle_direction="center", particle_angle_random=.7)
    definition = replace(primary, dual=secondary)
    assert BrushDefinition.from_dict(definition.to_dict()) == definition
    assert _draw(primary) != _draw(secondary)
    dry = replace(primary, spray=False, angle=12)
    assert {dab.angle for dab in _draw(dry)} == {12}


def test_particle_angle_values_are_sanitized_when_loading_presets():
    brush = BrushDefinition.from_dict({"particle_angle": float("inf"), "particle_angle_random": -2})
    assert math.isfinite(brush.particle_angle)
    assert brush.particle_angle_random == 0
    assert BrushDefinition.from_dict({"particle_angle_random": 3}).particle_angle_random == 1
