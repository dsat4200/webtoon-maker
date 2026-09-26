"""Native ink/dual mode codes and their visible raster consequences."""
from dataclasses import replace
from pathlib import Path
import sqlite3

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushInput
from comic_editor.core.brush_raster import RasterBrushStroke, composite_pixels, pixels_image
from comic_editor.core.sut_import import import_sut
from comic_editor.core.tiles import TileStore
from test_brush_raster import atlas, rendered
from test_sut_import import _sut


def ink_fixture(tmp_path, mode):
    path = _sut(tmp_path)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Variant ADD COLUMN CompositeMode INTEGER')
        connection.execute('UPDATE Variant SET CompositeMode=? WHERE VariantID=99', (mode,))
    return path


def test_native_multiply_code_darkens_colored_pixels_instead_of_adding_light(tmp_path):
    brush = import_sut(ink_fixture(tmp_path, 2))
    store = TileStore(64)
    base = np.ones((64,64,4), np.float32)
    base[..., :3] = (.4,.6,.8)
    store.set_tile('paint',(0,0),pixels_image(base))
    before = {}
    stroke = RasterBrushStroke(store,'paint',brush,QColor.fromRgbF(.8,.4,.2),before)
    stroke.begin(BrushInput(32,32))
    stroke.finish()
    expected = base[32,32,:3] * (1-brush.opacity + brush.opacity*np.asarray((.8,.4,.2)))
    np.testing.assert_allclose(atlas(store)[32,32,:3],expected,atol=2/255)
    assert brush.blending_mode == 'multiply'
    assert (0,0) in before


def test_native_eraser_code_removes_alpha_and_retains_undo_baseline(tmp_path):
    brush = import_sut(ink_fixture(tmp_path, 27))
    store = TileStore(64)
    base = np.ones((64,64,4),np.float32)
    base[...,:3] = (.2,.4,.8)
    original = pixels_image(base)
    store.set_tile('paint',(0,0),original)
    before = {}
    stroke = RasterBrushStroke(store,'paint',brush,QColor('red'),before)
    stroke.begin(BrushInput(32,32))
    stroke.finish()
    assert brush.blending_mode == 'erase'
    assert atlas(store)[32,32,3] == pytest.approx(1-brush.opacity,abs=1/255)
    assert bytes(before[(0,0)].constBits()) == bytes(original.constBits())


def test_unknown_ink_and_dual_codes_report_their_fallback(tmp_path):
    path = ink_fixture(tmp_path,913)
    with sqlite3.connect(path) as connection:
        connection.execute('ALTER TABLE Variant ADD COLUMN UseDualBrush INTEGER')
        connection.execute('ALTER TABLE Variant ADD COLUMN DualSize REAL')
        connection.execute('ALTER TABLE Variant ADD COLUMN DualBrushCompositeMode INTEGER')
        connection.execute('UPDATE Variant SET UseDualBrush=1,DualSize=12,DualBrushCompositeMode=914 WHERE VariantID=99')
    brush = import_sut(path)
    assert brush.blending_mode == 'normal' and brush.dual_mode == 'multiply'
    assert any('Ink blending mode 913' in note for note in brush.warnings)
    assert any('Dual blending mode 914' in note for note in brush.warnings)
    assert brush.source['variant']['CompositeMode'] == 913
    assert brush.source['variant']['DualBrushCompositeMode'] == 914


def test_official_wet_wash_fixture_uses_height_linear_not_soft_light():
    path = Path('.artifacts/brush-investigation/installed/reconstructed/CSP112-Wet-wash.reconstructed.sut')
    if not path.exists():
        pytest.skip('Optional private installed fixture')
    brush = import_sut(path)
    assert brush.source['variant']['DualBrushCompositeMode'] == 12
    assert brush.dual_mode == 'height_linear'
    assert brush.blending_mode == 'multiply'
    assert brush.mixing_mode == 'smear'


@pytest.mark.parametrize('mode,expected',[
    ('darker_color',(.2,.2,.2)), ('lighter_color',(.8,0,0)),
])
def test_brightness_comparison_selects_a_whole_color_not_individual_channels(mode,expected):
    back = np.asarray([[[.8,0,0,1]]],np.float32)
    front = np.asarray([[[.2,.2,.2,1]]],np.float32)
    result = composite_pixels(back,front,mode)
    np.testing.assert_allclose(result[0,0,:3],expected,atol=1e-6)
    assert result[0,0,3] == 1


@pytest.mark.parametrize('mixing_mode',('blend','running'))
def test_wet_blend_ignores_stored_canvas_blending_mode_without_losing_it(mixing_mode):
    brush = BrushDefinition(size=16,mixing_mode=mixing_mode,paint_amount=.5,
                            blending_mode='multiply')
    def draw(definition):
        store = TileStore(64)
        base = np.ones((64,64,4),np.float32)
        base[...,:3] = (.2,.4,.8)
        store.set_tile('paint',(0,0),pixels_image(base))
        return atlas(rendered(definition,((16,32),(48,32)),color='red',tiles=store)[0])
    np.testing.assert_array_equal(draw(brush),draw(replace(brush,blending_mode='normal')))
    assert brush.blending_mode == 'multiply'
    assert not np.array_equal(draw(replace(brush,mixing_mode='smear')),
                              draw(replace(brush,mixing_mode='smear',blending_mode='normal')))
