"""Source particle-angle settings reach the sampler without whole-angle leakage."""
import math
import sqlite3

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.sut_import import import_sut
from comic_editor.core.brush_view import resolve_brush_view_size
from test_sut_import import _sut


def spray_fixture(tmp_path, flags, random_amount=100):
    path = _sut(tmp_path)
    with sqlite3.connect(path) as connection:
        for name in ('BrushUseSpray','BrushRotation','BrushRotationEffector',
                     'BrushRotationInSpray','BrushRotationEffectorInSpray',
                     'BrushRotationRandomInSpray','BrushSizeSyncViewScale'):
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} INTEGER')
        connection.execute('''UPDATE Variant SET BrushUseSpray=1,BrushRotation=120,
            BrushRotationEffector=67,BrushRotationInSpray=30,BrushRotationEffectorInSpray=?,
            BrushRotationRandomInSpray=?,BrushSizeSyncViewScale=1 WHERE VariantID=99''',
            (flags,random_amount))
    return path


def draw(brush):
    stroke = BrushStroke(brush,seed=42)
    return stroke.begin(BrushInput(0,0))+stroke.add(BrushInput(0,100,time=1))+stroke.finish()


@pytest.mark.parametrize('flags,expected', [(3,30),(65,120),(257,240)])
def test_imported_particle_angle_uses_only_the_selected_direction(tmp_path,flags,expected):
    brush = import_sut(spray_fixture(tmp_path,flags))
    assert brush.particle_angle_random == 0  # stored random amount is disabled
    assert {dab.angle for dab in draw(brush)} == {expected}
    assert BrushDefinition.from_dict(brush.to_dict()) == brush


def test_imported_center_particles_point_inward_with_their_numeric_offset(tmp_path):
    brush = import_sut(spray_fixture(tmp_path,513))
    assert any('particle direction flags' in note for note in brush.warnings)
    for dab in draw(brush):
        inward = math.degrees(math.atan2(dab.center_y-dab.y,dab.center_x-dab.x))
        assert ((dab.angle-inward-30+180)%360)-180 == pytest.approx(0,abs=1e-9)


def test_imported_particle_randomness_is_additive_and_bounded(tmp_path):
    brush = import_sut(spray_fixture(tmp_path,65|128,25))
    assert brush.particle_angle_random == .25
    angles = [dab.angle for dab in draw(brush)]
    assert min(angles) < 90 and max(angles) > 150
    assert all(75 <= angle <= 165 for angle in angles)


def test_screen_size_import_keeps_the_source_nominal_size_and_resolves_per_stroke(tmp_path):
    brush = import_sut(spray_fixture(tmp_path,3))
    assert brush.size_by_view and brush.size == 24
    resolved = resolve_brush_view_size(brush,.25)
    assert resolved.size == 96 and not resolved.size_by_view
    assert brush.size == 24 and brush.size_by_view
    assert BrushDefinition.from_dict(brush.to_dict()).size_by_view


def test_import_keeps_negative_spray_bias_and_ignores_dormant_ribbon(tmp_path):
    path = spray_fixture(tmp_path,3)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushSprayBias INTEGER')
        connection.execute('ALTER TABLE Variant ADD COLUMN BrushRibbon INTEGER')
        connection.execute('UPDATE Variant SET BrushSprayBias=-100,BrushRibbon=1 WHERE VariantID=99')
    brush = import_sut(path)
    assert brush.spray_deviation == -1 and brush.spray
    assert not brush.ribbon
    assert brush.source['variant']['BrushRibbon'] == 1
    assert any('Ribbon flag is inactive' in warning for warning in brush.warnings)
