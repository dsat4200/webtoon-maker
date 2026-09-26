"""Physical SUT lengths resolve once, with explicit DPI and source provenance."""
from copy import deepcopy
from pathlib import Path
import sqlite3
import struct

import pytest

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.brush_units import has_physical_lengths, with_import_dpi
from comic_editor.core.settings import EditorSettings
from comic_editor.core.sut_import import import_sut
from comic_editor.ui.brush_controls import BrushControls
from test_sut_import import _sut


def unit_sut(tmp_path, **values):
    path = _sut(tmp_path)
    with sqlite3.connect(path) as connection:
        columns = {row[1] for row in connection.execute('PRAGMA table_info(Variant)')}
        for key, value in values.items():
            if key not in columns:
                kind = 'BLOB' if isinstance(value, bytes) else 'REAL'
                connection.execute(f'ALTER TABLE Variant ADD COLUMN {key} {kind}')
            connection.execute(f'UPDATE Variant SET {key}=? WHERE VariantID=99', (value,))
    return path


def taper_fields(mode=0):
    return dict(BrushUseIn=1, BrushUseOut=1, BrushInOutType=mode,
                BrushInLength=25.4, BrushInLengthUnit=2,
                BrushOutLength=42, BrushOutLengthUnit=0,
                BrushInRatio=35, BrushOutRatio=60,
                BrushInOutTarget=struct.pack('>6I', 12, 1, 12, 1001, 1, 0))


@pytest.mark.parametrize('dpi', [72, 300, 600])
def test_explicit_units_convert_independently_and_retain_originals(tmp_path, dpi):
    path = unit_sut(tmp_path, BrushSize=1.5, BrushSizeUnit=2,
                    BrushUseSpray=1, BrushSpraySize=4, BrushSpraySizeUnit=0,
                    BrushUseWaterEdge=1, BrushWaterEdgeRadius=.1, BrushWaterEdgeRadiusUnit=2,
                    BrushWaterEdgeBlur=.1, BrushWaterEdgeBlurUnit=2, **taper_fields())
    before = path.read_bytes()
    brush = import_sut(path, dpi=dpi)
    assert brush.size == pytest.approx(1.5*dpi/25.4)
    assert brush.particle_size == 4
    assert brush.watercolor_edge == brush.watercolor_blur == pytest.approx(.1*dpi/25.4)
    assert brush.taper_start == pytest.approx(dpi) and brush.taper_end == 42
    assert brush.source['variant']['BrushSize'] == 1.5
    metadata = brush.source['length_units']
    assert metadata['dpi'] == dpi and metadata['has_millimeters']
    assert metadata['fields']['size']['source_value'] == 1.5
    assert metadata['fields']['size']['source_unit'] == 2
    assert metadata['fields']['particle_size']['unit'] == 'px'
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    assert path.read_bytes() == before


@pytest.mark.parametrize('mode', [1, 2])
def test_percentage_and_fade_tapers_use_only_their_respective_fields(tmp_path, mode):
    brush = import_sut(unit_sut(tmp_path, **taper_fields(mode)), dpi=600)
    if mode == 1:
        assert (brush.taper_start, brush.taper_end) == (35, 60)
        assert 'taper_start' not in brush.source['length_units']['fields']
        assert 'taper_end' not in brush.source['length_units']['fields']
        assert not has_physical_lengths(brush)
    else:
        assert brush.taper_start == 0 and brush.taper_end == 42
        assert 'taper_start' not in brush.source['length_units']['fields']


def test_secondary_and_particle_units_are_independent_and_ratios_stay_ratios(tmp_path):
    path = unit_sut(tmp_path, BrushSize=24, BrushSizeUnit=0, UseDualBrush=1,
                    DualSize=25.4, DualSizeUnit=2, DualUseSpray=1,
                    DualSpraySize=50, DualSpraySizeUnit=2, DualSpraySizeSyncBrushSize=1,
                    BrushUseSpray=1, BrushSpraySize=2.54, BrushSpraySizeUnit=2)
    brush = import_sut(path,dpi=300)
    assert brush.size == 24 and brush.particle_size == pytest.approx(30)
    assert brush.dual.size == pytest.approx(300)
    assert brush.dual.particle_size_relative and brush.dual.particle_size == .5
    assert 'particle_size' not in brush.dual.source['length_units']['fields']
    assert has_physical_lengths(brush)
    resized = with_import_dpi(brush,600)
    assert resized.size == 24 and resized.dual.size == pytest.approx(600)
    assert resized.dual.particle_size == .5
    assert resized.to_dict() == import_sut(path,dpi=600).to_dict()
    assert with_import_dpi(resized,300) == brush


def test_unknown_units_and_automatic_blur_approximation_are_explicit(tmp_path):
    path = unit_sut(tmp_path, BrushSize=3, BrushSizeUnit=9,
                    BrushUseWaterColor=1, BrushWaterColor=1,
                    BrushBlur=5, BrushBlurUnit=2, BrushBlurLinkSize=1)
    brush = import_sut(path)
    assert brush.size == 3 and brush.blur == 1 and brush.blur_mode == 'automatic'
    assert brush.blur_width == pytest.approx(5*300/25.4)
    assert any('unknown length-unit code 9' in note for note in brush.warnings)
    assert any('Automatic Running-color blur' in note and 'approximation' in note for note in brush.warnings)
    assert not has_physical_lengths(brush)


def test_converted_model_limits_warn_instead_of_silent_settings_clamp(tmp_path):
    path = unit_sut(tmp_path, BrushSize=1000, BrushSizeUnit=2)
    brush = import_sut(path,dpi=600)
    assert brush.size == 4096
    assert any('BrushSize converts to' in note and '4096 px' in note for note in brush.warnings)
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    small = with_import_dpi(brush,72)
    assert small.size == pytest.approx(1000*72/25.4)
    assert not any('BrushSize converts to' in note for note in small.warnings)


@pytest.mark.parametrize('dpi', [0, -1, 9601, float('nan'), float('inf'), None, 'unknown'])
def test_invalid_resolution_is_rejected_before_reading_source(dpi):
    with pytest.raises(ValueError, match='resolution'):
        import_sut('does-not-exist.sut',dpi=dpi)


def test_programmatic_import_does_not_prompt_and_different_dpi_is_a_safe_copy(qapp, tmp_path, monkeypatch):
    from comic_editor.ui import brush_controls
    path = unit_sut(tmp_path, BrushSize=25.4, BrushSizeUnit=2)
    controls = BrushControls(EditorSettings())
    monkeypatch.setattr(brush_controls.QInputDialog,'getDouble',lambda *a,**k: pytest.fail('Unexpected DPI prompt'))
    first = controls.import_path(path,dpi=300)
    original = deepcopy(controls.settings.brush_presets)
    second = controls.import_path(path,dpi=600)
    assert second.id != first.id and second.size == pytest.approx(600)
    assert controls.settings.brush_presets[:-1] == original
    assert controls.import_path(path,dpi=600).id == second.id
    assert controls.import_path(path,dpi=300).id == first.id
    controls.deleteLater()


@pytest.mark.parametrize('unit,accept,dpi,expected', [(0,True,600,24),(2,True,600,600),(2,False,600,None)])
def test_interactive_resolution_choice_happens_before_publish_and_decodes_once(qapp,tmp_path,monkeypatch,unit,accept,dpi,expected):
    from comic_editor.core import sut_import
    from comic_editor.ui import brush_controls
    path = unit_sut(tmp_path, BrushSize=25.4 if unit==2 else 24, BrushSizeUnit=unit)
    controls = BrushControls(EditorSettings())
    before = deepcopy(controls.settings.brush_presets)
    imports, prompts, changes = [], [], []
    original = sut_import.import_sut
    def decode(path, **kwargs):
        imports.append(kwargs)
        return original(path, **kwargs)
    def choose(*args, **kwargs):
        prompts.append(args)
        assert controls.settings.brush_presets == before
        return dpi, accept
    monkeypatch.setattr(sut_import,'import_sut',decode)
    monkeypatch.setattr(brush_controls.QFileDialog,'getOpenFileName',lambda *a,**k:(str(path),''))
    monkeypatch.setattr(brush_controls.QInputDialog,'getDouble',choose)
    monkeypatch.setattr(brush_controls.QMessageBox,'exec',lambda self:0)
    controls.settingsChanged.connect(lambda: changes.append(True))
    controls._import()
    assert len(imports)==1 and len(prompts)==(1 if unit==2 else 0)
    if expected is None:
        assert controls.settings.brush_presets == before and not changes
    else:
        assert controls.settings.brush_size_px == pytest.approx(expected)
        assert changes == [True]
    controls.deleteLater()


def test_actual_bibibi_millimeters_resolve_at_explicit_canvas_resolution():
    path=Path('.artifacts/brush-investigation/installed/additional-request-reconstructed/2306072-01.registered.reconstructed.sut')
    if not path.exists():
        pytest.skip('Local third-party research fixture is not distributed.')
    brush=import_sut(path,dpi=300)
    assert brush.size == pytest.approx(17.716535433070867)
    assert brush.watercolor_edge == brush.watercolor_blur == pytest.approx(1.1811023622047245)
    assert (brush.taper_start, brush.taper_end) == (35,42)
    assert brush.source['variant']['BrushInLength']==35
    assert brush.source['variant']['BrushOutLength']==42
    assert brush.source['variant']['BrushInLengthUnit']==0
    assert brush.source['variant']['BrushOutLengthUnit']==0
    assert brush.source['length_units']['dpi']==300
