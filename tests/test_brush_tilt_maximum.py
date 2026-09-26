"""Tilt enlargement survives import, editing, storage and dab sampling."""
from dataclasses import replace
from pathlib import Path
import sqlite3
import struct

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.sut_import import _dynamics
from comic_editor.ui.brush_controls import BrushSettingsDialog


def test_legacy_dynamics_default_and_physical_maximum_roundtrip():
    legacy = BrushDynamics.from_dict({'tilt': True, 'tilt_minimum': .2})
    assert legacy.tilt_maximum == 1
    dynamics = replace(legacy, tilt_maximum=2.573456789,
                       tilt_curve=((0., 0.), (.25, .7), (1., 1.)))
    brush = BrushDefinition(dynamics={'size': dynamics})
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    assert BrushDynamics.from_dict({'tilt_maximum': 100}).tilt_maximum == 10
    assert BrushDynamics.from_dict({'tilt_maximum': 3}, signed=True).tilt_maximum == 1


def test_tilt_enlarges_size_and_multiplies_pressure_without_one_ratio_cap():
    brush = BrushDefinition(size=20, dynamics={'size': BrushDynamics(
        pressure=True, minimum=.1, tilt=True, tilt_minimum=1, tilt_maximum=3)})
    stroke = BrushStroke(brush)
    assert stroke._size(BrushInput(0, 0, pressure=1)) == 20
    assert stroke._size(BrushInput(0, 0, pressure=1, tilt_x=45)) == 40
    assert stroke._size(BrushInput(0, 0, pressure=1, tilt_y=90)) == 60
    assert stroke._size(BrushInput(0, 0, pressure=0, tilt_y=90)) == pytest.approx(6)
    samples = stroke.begin(BrushInput(0, 0, tilt_x=90)) + stroke.finish()
    assert samples[0].size == 60


def test_tilt_curve_and_disabled_maximum_keep_their_separate_effects():
    dynamic = BrushDynamics(tilt=True, tilt_minimum=.5, tilt_maximum=4,
                            tilt_curve=((0., 0.), (.5, .25), (1., 1.)))
    stroke = BrushStroke(BrushDefinition(dynamics={'particle_size': dynamic}))
    assert stroke._factor('particle_size', BrushInput(0, 0, tilt_x=45)) == 1.375
    stroke.definition = replace(stroke.definition,
                                dynamics={'particle_size': replace(dynamic, tilt=False)})
    assert stroke._factor('particle_size', BrushInput(0, 0, tilt_x=90)) == 1


@pytest.mark.parametrize('header_size', [44, 48])
def test_header_word_eleven_is_physical_tilt_maximum(header_size):
    words = [header_size, 240, 32, 0, 100, 0, 0, 0, 0, 0, 300]
    if header_size == 48:
        words.append(0)
    source = struct.pack('>' + str(len(words)) + 'I', *words)
    dynamic = _dynamics(source)
    assert dynamic.tilt and dynamic.tilt_minimum == 1 and dynamic.tilt_maximum == 3
    # Color-header enlargement has not been calibrated; preserve its warning
    # and prior behavior instead of applying a physical-size interpretation.
    assert _dynamics(source, signed=True).tilt_maximum == 1
    assert _dynamics(source, tilt_enlargement=False).tilt_maximum == 1


@pytest.mark.parametrize('filename,maximum,upright_size,tilted_size', [
    ('leaf-2.registered.reconstructed.sut', 3, 30, 10),
    ('sketching-pencil.current.reconstructed.sut', 2.57, .4, 25.7),
])
def test_actual_imported_physical_source_retains_tilt_enlargement(filename, maximum, upright_size, tilted_size):
    path = Path('.artifacts/brush-investigation/installed/dynamic-request-reconstructed') / filename
    if not path.exists():
        pytest.skip('Local third-party research fixtures are not distributed.')
    with sqlite3.connect(f'{path.resolve().as_uri()}?mode=ro', uri=True) as connection:
        rows = connection.execute('SELECT BrushSizeEffector FROM Variant').fetchall()
    source = next(row[0] for row in rows if row[0])
    dynamic = _dynamics(source)
    assert dynamic.tilt and dynamic.tilt_maximum == maximum
    stroke = BrushStroke(BrushDefinition(size=10, minimum_pixel=False, dynamics={'size': dynamic}))
    # Leaf has an explicitly descending curve; Sketch has an ascending curve.
    assert stroke._size(BrushInput(0, 0)) == pytest.approx(upright_size)
    assert stroke._size(BrushInput(0, 0, tilt_y=90)) == pytest.approx(tilted_size)


def test_settings_preserve_precision_and_edit_tilt_maximum_in_percent(qapp):
    dynamic = BrushDynamics(tilt=True, tilt_minimum=.123456789,
                            tilt_maximum=2.573456789,
                            tilt_curve=((0., 0.), (.27, .7), (1., 1.)))
    brush = BrushDefinition(dynamics={'size': dynamic})
    dialog = BrushSettingsDialog(brush)
    try:
        control = dialog.dynamic_controls['size']['tilt_maximum']
        assert control.suffix() == '%' and control.maximum() == 1000
        assert control.value() == 257.35
        assert dialog.result_definition().to_dict() == brush.to_dict()
        control.setValue(350)
        assert dialog.result_definition().dynamics['size'].tilt_maximum == 3.5
        assert dialog.preview._definition.dynamics['size'].tilt_maximum == 3.5
        assert dialog.result_definition().dynamics['size'].tilt_curve == dynamic.tilt_curve
        assert 'tilt_maximum' not in dialog.dynamic_controls['hue_shift']
    finally:
        dialog.close()
        dialog.deleteLater()
