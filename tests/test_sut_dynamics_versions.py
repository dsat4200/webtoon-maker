"""Versioned dynamics headers retain curves, flags and signed color ranges."""
import base64
from pathlib import Path
import sqlite3
import struct

import pytest

from comic_editor.core.sut_import import SutImportError, _dynamics, import_sut
from test_sut_import import _sut


def curve(points):
    return struct.pack('>III', 12, len(points), 16) + b''.join(struct.pack('>dd', *p) for p in points)


def blob(header_size=48, *, active=0xf0, extension=0, signed=False):
    pressure = curve(((0., 0.), (.4, .15), (1., 1.)))
    tilt = curve(((0., 1.), (.7, .25), (1., 0.)))
    minima = (-25, -40, -60, -100) if signed else (25, 40, 60, 20)
    words = [header_size, 496, active, *minima, 0, len(pressure), len(tilt), 100]
    if header_size == 48:
        words.append(extension)
    return struct.pack('>' + str(len(words)) + 'i', *words) + pressure + tilt


@pytest.mark.parametrize('header_size', [44, 48])
def test_known_header_versions_keep_separate_pressure_and_tilt_curves(header_size):
    diagnostics = []
    dynamic = _dynamics(blob(header_size), diagnostics=diagnostics)
    assert dynamic.pressure and dynamic.tilt and dynamic.velocity
    assert dynamic.pressure_curve == ((0., 0.), (.4, .15), (1., 1.))
    assert dynamic.tilt_curve == ((0., 1.), (.7, .25), (1., 0.))
    assert (dynamic.minimum, dynamic.tilt_minimum, dynamic.velocity_minimum, dynamic.random) == (.25, .4, .6, .2)
    assert not diagnostics


@pytest.mark.parametrize('header_size', [44, 48])
def test_signed_color_minima_and_inactive_random_survive_both_headers(header_size):
    dynamic = _dynamics(blob(header_size, signed=True), signed=True)
    assert (dynamic.minimum, dynamic.tilt_minimum, dynamic.velocity_minimum, dynamic.random) == (-.25, -.4, -.6, -1)
    assert _dynamics(blob(header_size, signed=True, active=0x10), signed=True).random == 1
    assert _dynamics(blob(header_size, active=0)).pressure is False


def changed_word(data, offset, value):
    return data[:offset] + struct.pack('>I', value) + data[offset + 4:]


@pytest.mark.parametrize('signed', [False, True])
def test_48_byte_header_length_maps_independent_velocity_curve(signed):
    points = ((0., .1), (.25, .85), (1., .4))
    velocity = curve(points)
    source = changed_word(blob(signed=signed), 44, len(velocity)) + velocity
    diagnostics = []
    dynamic = _dynamics(source, signed=signed, diagnostics=diagnostics)
    assert dynamic.velocity and dynamic.velocity_curve == points
    assert dynamic.velocity_minimum == (-.6 if signed else .6)
    assert dynamic.pressure_curve == ((0., 0.), (.4, .15), (1., 1.))
    assert dynamic.tilt_curve == ((0., 1.), (.7, .25), (1., 0.))
    assert not diagnostics


@pytest.mark.parametrize('data', [
    changed_word(blob(), 0, 52),
    blob()[:44],
    changed_word(blob(), 32, 0xffffffff),
    changed_word(blob(), 36, 0xffffffff),
    changed_word(blob(), 52, 257),
    changed_word(blob(), 48, 16),
    blob()[:-1],
    blob() + b'unknown extension',
    blob()[:60] + struct.pack('>d', float('nan')) + blob()[68:],
    changed_word(blob(), 44, 44),
    changed_word(blob(), 44, 44) + curve(((0., 1.), (1., 0.)))[:-1],
    changed_word(blob(), 44, 44) + curve(((0., 1.), (1., 0.))) + b'x',
    changed_word(blob(), 44, 44) + changed_word(curve(((0., 1.), (1., 0.))), 4, 3),
    changed_word(blob(), 44, 44) + curve(((0., 1.), (1., float('nan')))),
])
def test_malformed_headers_and_curve_bounds_fail_explicitly(data):
    with pytest.raises(SutImportError):
        _dynamics(data)


def test_unknown_extension_warns_without_losing_known_response_or_source(tmp_path):
    path = _sut(tmp_path)
    source = blob(extension=7, active=0x110)
    with sqlite3.connect(path) as connection:
        connection.execute('UPDATE Variant SET BrushSizeEffector=? WHERE VariantID=99', (source,))
    brush = import_sut(path)
    assert brush.dynamics['size'].pressure
    assert brush.dynamics['size'].pressure_curve == ((0., 0.), (.4, .15), (1., 1.))
    assert any('BrushSizeEffector' in text and '48-byte dynamics extension' in text for text in brush.warnings)
    assert any('Unknown active dynamics inputs' in text for text in brush.warnings)
    assert brush.source['variant']['BrushSizeEffector']['data'] == base64.b64encode(source).decode()


def test_signed_48_byte_color_header_uses_existing_import_path(tmp_path):
    path = _sut(tmp_path)
    source = blob(signed=True)
    with sqlite3.connect(path) as connection:
        for name, kind in [('BrushChangePatternColor', 'INTEGER'), ('BrushHueChange', 'REAL'), ('BrushHueChangeEffector', 'BLOB')]:
            connection.execute(f'ALTER TABLE Variant ADD COLUMN {name} {kind}')
        connection.execute('UPDATE Variant SET BrushChangePatternColor=1,BrushHueChange=-90,BrushHueChangeEffector=? WHERE VariantID=99', (source,))
    brush = import_sut(path)
    assert brush.hue_shift == -.25
    assert brush.dynamics['hue_shift'].minimum == -.25
    assert brush.dynamics['hue_shift'].random == -1


def test_real_sru_48_byte_pressure_curve_and_legacy_blood_still_import():
    folder = Path('.artifacts/brush-investigation/installed/dynamic-request-reconstructed')
    sru = folder / 'sru-karikari-line-pen.registered.reconstructed.sut'
    blood = folder / 'blood-02.registered.reconstructed.sut'
    if not sru.is_file() or not blood.is_file():
        pytest.skip('Local third-party research fixtures are not distributed.')
    brush = import_sut(sru)
    assert brush.dynamics['size'].pressure
    assert len(brush.dynamics['size'].pressure_curve) == 3
    assert brush.dynamics['size'].pressure_curve[0] == (0., 0.)
    assert brush.dynamics['size'].pressure_curve[-1] == (1., 1.)
    assert not any('Unsupported brush dynamics' in warning for warning in brush.warnings)
    legacy = import_sut(blood)
    assert legacy.dynamics['size'].pressure
    assert len(legacy.dynamics['size'].pressure_curve) == 4


@pytest.mark.parametrize('filename,channel,velocity,minimum', [
    ('2306072-01.registered.reconstructed.sut', 'size', True, .6),
    ('2306072-01.registered.reconstructed.sut', 'density', True, .3),
    ('2308021-01.registered.reconstructed.sut', 'opacity', False, 0),
])
def test_actual_bibibi_and_hazy_keep_all_three_response_curves(filename, channel, velocity, minimum):
    path = Path('.artifacts/brush-investigation/installed/additional-request-reconstructed') / filename
    if not path.is_file():
        pytest.skip('Local third-party research fixtures are not distributed.')
    brush = import_sut(path)
    dynamics = brush.dynamics[channel]
    assert dynamics.pressure and dynamics.velocity is velocity
    assert dynamics.velocity_minimum == minimum
    assert dynamics.velocity_curve == ((0., 1.), (1., 0.))
    assert len(dynamics.pressure_curve) == 3
    assert len(dynamics.tilt_curve) == 4
    assert not any('dynamics data' in text or '48-byte dynamics extension' in text for text in brush.warnings)
