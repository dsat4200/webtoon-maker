"""Texture enum preservation and bounded density-space color-burn behavior."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDefinition, BrushTexture
from comic_editor.core.brush_raster import RasterBrushStroke
from comic_editor.core.sut_import import _build_definition
from comic_editor.core.tiles import TileStore
from test_brush_raster import atlas, png, rendered
from test_sut_import import _png, _reference, _tar


TEXTURE_MENU = ("normal", "multiply", "subtract", "compare", "outline", "overlay",
                "color_dodge", "color_burn", "hard_mix", "height")


def imported_texture(code, *, secondary=False):
    variant = {"BrushSize": 40, "TextureImage": _reference(), "TextureCompositeMode": code}
    if secondary:
        variant.update(UseDualBrush=1, DualSize=25, DualTextureImage=_reference(),
                       DualTextureCompositeMode=code)
    return _build_definition("texture.sut", "a"*64, {"NodeName":"Texture fixture"},
        variant, {"Version":145}, [{"OriginalPath":".:test:data:tip.png",
        "FileData":_tar([("data/tip.png", _png((128,128,128,255)))])}])


@pytest.mark.parametrize("code,mode", list(enumerate(TEXTURE_MENU)))
@pytest.mark.parametrize("secondary", [False, True])
def test_imported_texture_menu_order_and_original_code_survive_roundtrip(code, mode, secondary):
    brush = imported_texture(code, secondary=secondary)
    assert brush.texture.mode == mode
    assert brush.source["variant"]["TextureCompositeMode"] == code
    restored = BrushDefinition.from_dict(brush.to_dict())
    assert restored.texture.mode == mode
    if secondary:
        assert restored.dual.texture.mode == mode
        assert restored.source["variant"]["DualTextureCompositeMode"] == code
    assert any("exact texture formulas" in warning for warning in brush.warnings)
    assert not any("not mapped" in warning for warning in brush.warnings)


def test_unknown_texture_code_is_reported_without_losing_source():
    brush = imported_texture(123)
    assert brush.texture.mode == "multiply"
    assert brush.source["variant"]["TextureCompositeMode"] == 123
    assert any("Texture mode 123 is not mapped" in warning for warning in brush.warnings)


def test_normal_texture_explicitly_reports_existing_multiply_approximation():
    assert any("Normal texture currently uses the Multiply approximation" in warning
               for warning in imported_texture(0).warnings)


def texture_response(gray, alpha, *, density=1., density_factor=1.):
    material = np.full((1,1,4), gray, np.uint8)
    material[...,3] = 255
    texture = BrushTexture(png=png(material), mode="color_burn", density=density)
    stroke = RasterBrushStroke(TileStore(16),"paint",BrushDefinition(),QColor("black"),{})
    alpha = np.asarray(alpha, np.float32)[None,:]
    xx = np.arange(alpha.size, dtype=np.float32)[None,:]+.5
    return stroke._texture(alpha, texture, xx, np.full_like(xx,.5),
                           density_factor=density_factor)[0]


def test_color_burn_preserves_coverage_limits_and_is_not_multiply():
    alpha = np.array([0,.2,.6,.9,1], np.float32)
    result = texture_response(128, alpha)
    assert np.all(np.isfinite(result))
    assert result[0] == 0 and result[-1] == 1
    assert np.all(result <= alpha)
    assert result[1] == 0
    assert 0 < result[2] < alpha[2]*128/255
    assert np.all(np.diff(result) >= 0)


@pytest.mark.parametrize("gray", [0, 1, 128, 254, 255])
def test_color_burn_black_white_limits_and_partial_density(gray):
    alpha = np.array([0,.1,.4,.7,1], np.float32)
    full = texture_response(gray, alpha)
    half = texture_response(gray, alpha, density=.8, density_factor=.625)
    np.testing.assert_allclose(half, (alpha+full)/2, atol=1e-7)
    np.testing.assert_array_equal(texture_response(gray, alpha, density_factor=0), alpha)
    assert np.all(np.isfinite(full))
    assert full[0] == 0
    if gray == 255:
        np.testing.assert_allclose(full, alpha, atol=1e-7)
    if gray == 0:
        np.testing.assert_array_equal(full[:-1], np.zeros(4))


@pytest.mark.parametrize("per_dab", [False, True])
@pytest.mark.parametrize("dual", [False, True])
def test_color_burn_cross_tile_strokes_are_packet_invariant(per_dab, dual):
    grain = np.empty((4,4,4), np.uint8)
    grain[:,:,:3] = (np.indices((4,4)).sum(axis=0)%2*160+64)[:,:,None]
    grain[:,:,3] = 255
    brush = BrushDefinition(size=24, opacity=.65, density=.5, hardness=.2,
        texture=BrushTexture(png=png(grain),mode="color_burn",per_dab=per_dab))
    if dual:
        brush = replace(brush,dual=replace(brush,size=30),dual_mode="multiply")
    coarse,_,_ = rendered(brush, [(20,32),(108,32)])
    fine,_,_ = rendered(brush, [(x,32) for x in range(20,109)])
    pixels = atlas(coarse)
    np.testing.assert_array_equal(pixels,atlas(fine))
    assert pixels[...,3].sum() > 0
    assert np.isfinite(pixels).all()
    assert not pixels[0,:,3].any()
