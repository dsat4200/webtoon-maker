"""Automatic and fixed Running-color blur retain independent size semantics."""
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDab, BrushDefinition, BrushDynamics, BrushInput
from comic_editor.core.brush_preview import fit_brush_for_preview
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels, pixels_image
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.brush_units import has_physical_lengths, with_import_dpi
from comic_editor.core.sut_import import import_sut
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_controls import BrushSettingsDialog
from test_brush_units import unit_sut


@pytest.mark.parametrize('dpi', [72, 300, 600])
@pytest.mark.parametrize('unit', [0, 2])
def test_fixed_width_keeps_source_scalar_and_resolves_independent_units(tmp_path, dpi, unit):
    path = unit_sut(tmp_path, BrushSize=13, BrushUseWaterColor=1, BrushWaterColor=1,
                    BrushBlur=25.4, BrushBlurUnit=unit, BrushBlurLinkSize=0)
    original = path.read_bytes()
    brush = import_sut(path, dpi=dpi)
    assert brush.blur_mode == 'fixed'
    assert brush.blur_width == pytest.approx(dpi if unit == 2 else 25.4)
    assert brush.size == 13
    assert brush.source['variant']['BrushBlur'] == 25.4
    assert brush.source['variant']['BrushBlurLinkSize'] == 0
    assert brush.source['length_units']['fields']['blur_width']['active']
    assert has_physical_lengths(brush) == (unit == 2)
    assert with_import_dpi(brush, 144).blur_width == pytest.approx(144 if unit == 2 else 25.4)
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    assert path.read_bytes() == original


@pytest.mark.parametrize('saved_width', [0, 5, 200])
def test_automatic_choice_is_independent_of_dormant_fixed_slider(tmp_path, saved_width):
    brush = import_sut(unit_sut(tmp_path, BrushUseWaterColor=1, BrushWaterColor=1,
        BrushBlur=saved_width, BrushBlurUnit=2, BrushBlurLinkSize=1))
    assert brush.blur_mode == 'automatic' and brush.blur == 1
    assert brush.blur_width == pytest.approx(saved_width*300/25.4)
    assert not has_physical_lengths(brush)
    assert not brush.source['length_units']['fields']['blur_width']['active']
    assert any('automatic radius' in warning for warning in brush.warnings)


@pytest.mark.parametrize('mixing,enabled,dual', [(0,1,0), (2,1,0), (1,0,0), (1,1,1)])
def test_dormant_fixed_width_does_not_request_resolution(tmp_path, mixing, enabled, dual):
    brush = import_sut(unit_sut(tmp_path, BrushUseWaterColor=enabled, BrushWaterColor=mixing,
        UseDualBrush=dual, BrushBlur=.3, BrushBlurUnit=2, BrushBlurLinkSize=0))
    assert brush.blur_width == pytest.approx(.3*300/25.4)
    assert not brush.source['length_units']['fields']['blur_width']['active']
    assert not has_physical_lengths(brush)


@pytest.mark.parametrize('flag', [None, 4])
def test_unrecognized_blur_choice_preserves_legacy_fallback_with_warning(tmp_path, flag):
    brush = import_sut(unit_sut(tmp_path, BrushUseWaterColor=1, BrushWaterColor=1,
        BrushBlur=25, BrushBlurLinkSize=flag))
    assert brush.blur_mode == 'automatic' and brush.blur == .25
    assert brush.blur_width == 25
    assert any('mode is missing or unknown' in warning for warning in brush.warnings)


def pickup_offsets(brush, size, response=1):
    stroke = RasterBrushStroke(TileStore(16), 'paint', brush, QColor('black'), {})
    centers = []
    def read(xx, yy, **kwargs):
        centers.append((float(xx.mean()), float(yy.mean())))
        return np.zeros((*xx.shape, 4), np.float32)
    stroke._read_pixels = read
    stroke._prepare_wet(stroke.main, BrushDab(16, 16, size, blur=response), size, size)
    return np.asarray(centers)-centers[0]


def test_fixed_pixel_width_is_not_capped_or_scaled_by_brush_size():
    brush = BrushDefinition(mixing_mode='running', blur_mode='fixed', blur_width=75, blur=0)
    small, large = pickup_offsets(brush, 8), pickup_offsets(brush, 300)
    expected = [[0,0],[75,0],[-75,0],[0,75],[0,-75]]
    np.testing.assert_allclose(small, expected)
    np.testing.assert_allclose(large, expected)


def test_automatic_legacy_strength_and_size_response_remain_unchanged():
    # A portable preset saved before blur_mode existed must keep its appearance.
    brush = BrushDefinition.from_dict({'mixing_mode':'running', 'blur':.35})
    assert brush.blur_mode == 'automatic' and brush.blur_width == 0
    for size in (10, 40, 200):
        radius = .35*min(20, size*.2)
        np.testing.assert_allclose(pickup_offsets(brush,size),
            [[0,0],[radius,0],[-radius,0],[0,radius],[0,-radius]],atol=1e-5)
    assert BrushDefinition.from_dict(brush.to_dict()) == brush


def test_fixed_blur_uses_its_pressure_response_and_zero_disables_extra_pickup():
    brush = BrushDefinition(size=20,mixing_mode='running',blur_mode='fixed',blur_width=12,
        dynamics={'blur':BrushDynamics(pressure=True)})
    dab = BrushStroke(brush).begin(BrushInput(16,16,pressure=.25))[0]
    np.testing.assert_allclose(pickup_offsets(brush,20,dab.blur),
                              [[0,0],[3,0],[-3,0],[0,3],[0,-3]])
    assert pickup_offsets(brush,20,0).shape == (1,2)


@pytest.mark.parametrize('mode', ['none', 'blend', 'smear'])
def test_running_blur_is_dormant_in_other_mixing_modes(mode):
    brush = BrushDefinition(mixing_mode=mode,blur_mode='fixed',blur_width=500)
    assert pickup_offsets(brush,20).shape == (1,2)


def test_fixed_blur_actually_picks_up_color_beyond_the_brush_tip():
    def draw(width):
        store = TileStore(64)
        pixels = np.empty((64,64,4),np.float32)
        pixels[:] = (1,0,0,1)
        pixels[:,46:52] = (0,0,1,1)
        store.set_tile('paint',(0,0),pixels_image(pixels))
        brush = BrushDefinition(size=6,mixing_mode='running',paint_amount=0,paint_density=0,
            color_stretch=0,blur_mode='fixed',blur_width=width)
        stroke = RasterBrushStroke(store,'paint',brush,QColor('black'),{})
        stroke.begin(BrushInput(32,32))
        stroke.finish()
        return image_pixels(store.tile('paint',(0,0)))[32,32]
    np.testing.assert_allclose(draw(0),[1,0,0,1])
    blurred = draw(16)
    assert blurred[2] > .15 and blurred[0] < .85 and blurred[3] == 1


def test_preview_spatial_fit_scales_fixed_width_without_mutating_preset():
    brush = BrushDefinition(size=300,blur_mode='fixed',blur_width=24,
        dual=BrushDefinition(size=60,blur_mode='fixed',blur_width=12))
    fitted = fit_brush_for_preview(brush,100)
    assert fitted.blur_width/brush.blur_width == pytest.approx(fitted.size/brush.size)
    assert fitted.dual.blur_width/brush.dual.blur_width == pytest.approx(fitted.size/brush.size)
    assert brush.blur_width == 24 and brush.dual.blur_width == 12


def test_settings_expose_native_mode_and_width_preserve_hidden_legacy_strength(qapp):
    brush = BrushDefinition(mixing_mode='running',blur=.37,blur_mode='fixed',blur_width=12.345)
    dialog = BrushSettingsDialog(brush)
    mode, width = dialog.controls['blur_mode'], dialog.controls['blur_width']
    assert mode.isEnabled() and width.isEnabled() and width.suffix() == ' px'
    assert 'blur' not in dialog.controls
    assert dialog.result_definition().blur == .37
    mode.setCurrentIndex(mode.findData('automatic'))
    assert not width.isEnabled()
    result = dialog.result_definition()
    assert result.blur_mode == 'automatic' and result.blur_width == 12.345 and result.blur == 1
    mixing = dialog.controls['mixing_mode']
    mixing.setCurrentIndex(mixing.findData('smear'))
    assert not mode.isEnabled() and not width.isEnabled()
    mixing.setCurrentIndex(mixing.findData('running'))
    mode.setCurrentIndex(mode.findData('fixed'))
    assert width.isEnabled()
    dialog.controls['ribbon'].setChecked(True)
    assert not mode.isEnabled() and not width.isEnabled()
    dialog.deleteLater()


def test_choosing_running_activates_automatic_blur_for_newly_configured_brush(qapp):
    dialog = BrushSettingsDialog(BrushDefinition())
    assert dialog.result_definition().blur == 0
    control = dialog.controls['mixing_mode']
    control.setCurrentIndex(control.findData('running'))
    assert dialog.result_definition().blur == 1
    assert dialog.result_definition().blur_mode == 'automatic'
    dialog.deleteLater()


def test_actual_watercolor_preserves_automatic_choice_and_200px_saved_width():
    path = Path('.artifacts/brush-investigation/installed/dynamic-request-reconstructed/guriguri-watercolor-blur.current.reconstructed.sut')
    if not path.exists():
        pytest.skip('Local read-only investigation fixture is unavailable')
    brush = import_sut(path)
    assert brush.mixing_mode == 'running'
    assert brush.blur_mode == 'automatic' and brush.blur == 1 and brush.blur_width == 200
    assert brush.source['variant']['BrushBlurLinkSize'] == 1
    assert brush.source['variant']['BrushBlur'] == 200
