"""Large particles and tilt responses must fit without editing source presets."""
from dataclasses import replace

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics
from comic_editor.core.brush_preview import fit_brush_for_preview


def test_relative_particles_include_their_coverage_outside_the_spray_radius():
    original = BrushDefinition(size=800,spray=True,particle_size=8,particle_size_relative=True)
    fitted = fit_brush_for_preview(original,120)
    assert fitted.size * (1+fitted.particle_size) == pytest.approx(48)
    assert fitted.particle_size == 8
    assert original.size == 800


def test_absolute_particles_and_secondary_geometry_scale_together():
    child = BrushDefinition(size=100,spray=True,particle_size=900,particle_size_relative=False)
    original = BrushDefinition(size=200,dual=child)
    fitted = fit_brush_for_preview(original,120)
    assert fitted.dual.size+fitted.dual.particle_size == pytest.approx(48)
    assert fitted.size/original.size == pytest.approx(fitted.dual.size/child.size)
    assert child.particle_size == 900


def test_enabled_tilt_maximum_affects_fit_without_double_scaling_dynamics():
    original = BrushDefinition(size=150,dynamics={"size":BrushDynamics(tilt=True,tilt_maximum=5)})
    fitted = fit_brush_for_preview(original,120)
    assert fitted.size * 5 == pytest.approx(48)
    assert fitted.dynamics == original.dynamics
    disabled = replace(original,dynamics={"size":BrushDynamics(tilt=False,tilt_maximum=5)})
    assert fit_brush_for_preview(disabled,120).size == pytest.approx(48)
