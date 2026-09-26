"""Color behavior across device sampling, material modes, ribbons and wet paint."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QColor

from comic_editor.core.brushes import BrushDab,BrushDefinition,BrushDynamics,BrushInput,BrushTip
from comic_editor.core.brush_raster import RasterBrushStroke,_jitter_rgb,pixels_image
from comic_editor.core.brush_stroke import BrushStroke
from comic_editor.core.tiles import TileStore
from test_brush_raster import atlas,png,rendered


def tip(mode="mask",rgba=(255,0,0,255)):
    material=np.empty((8,8,4),np.uint8)
    material[:]=rgba
    if mode == "dual_color":
        material[:,:4]=(0,0,0,255)
        material[:,4:]=(255,255,255,255)
    return BrushTip("Color contract",8,8,png(material),mode=mode,shape="image")


def dabs(brush,points,seed=4):
    stroke=BrushStroke(brush,seed=seed)
    result=stroke.begin(points[0])
    for point in points[1:]:
        result.extend(stroke.add(point))
    return result+stroke.finish()


@pytest.mark.parametrize("changes,expected",(
    ({"hue":1/3},(0,1,0)),
    ({"hue":-1/3},(0,0,1)),
    ({"hue":4/3},(0,1,0)),
    ({"saturation":-.6},(1,.6,.6)),
    ({"saturation":-2},(1,1,1)),
    ({"luminosity":-.5},(.5,0,0)),
    ({"luminosity":-2},(0,0,0)),
))
def test_signed_offsets_wrap_hue_and_clamp_saturation_value_for_scalar_and_rgb_materials(changes,expected):
    dab=BrushDab(16,16,16,**changes)
    stroke=RasterBrushStroke(TileStore(64),"paint",BrushDefinition(),QColor("red"),{})
    scalar=stroke._foreground(dab)
    array=_jitter_rgb(np.asarray([[[1.,0,0]]],np.float32),dab.hue,dab.saturation,dab.luminosity)
    np.testing.assert_allclose(scalar[:3],expected,atol=1e-6)
    np.testing.assert_allclose(array[0,0],expected,atol=1e-6)
    assert scalar[3] == 1


def test_constant_offsets_do_not_randomize_and_pressure_changes_only_selected_color_channel():
    constant=BrushDefinition(size=8,spacing=1,hue_shift=.25,saturation_shift=-.4,luminosity_shift=.2,
                              sub_color_amount=.6)
    points=[BrushInput(0,0,0),BrushInput(32,0,1,time=1)]
    actual=dabs(constant,points)
    assert all((d.hue,d.saturation,d.luminosity,d.sub_color_mix)==(.25,-.4,.2,.6) for d in actual)
    pressure=replace(constant,dynamics={"hue_shift":BrushDynamics(pressure=True),
                                         "sub_color_amount":BrushDynamics(pressure=True)})
    actual=dabs(pressure,points)
    assert [d.hue for d in actual] == pytest.approx([0,.0625,.125,.1875,.25])
    assert [d.sub_color_mix for d in actual] == pytest.approx([0,.15,.3,.45,.6])
    assert all(d.saturation == -.4 and d.luminosity == .2 for d in actual)


def test_signed_random_response_stays_in_range_and_preserves_reproducible_jitter():
    brush=BrushDefinition(size=2,spacing=1,hue_shift=.3,saturation_shift=.5,
        dynamics={"hue_shift":BrushDynamics(random=-1),"saturation_shift":BrushDynamics(random=-1)})
    points=[BrushInput(0,0),BrushInput(512,0,time=2)]
    first,again=dabs(brush,points,17),dabs(brush,points,17)
    assert first == again
    assert all(-.3 <= d.hue <= .3 and -.5 <= d.saturation <= .5 for d in first)
    assert min(d.hue for d in first) < -.2 and max(d.hue for d in first) > .2
    assert min(d.saturation for d in first) < -.3 and max(d.saturation for d in first) > .3


def test_stroke_random_color_stays_fixed_while_tip_random_color_varies():
    brush=BrushDefinition(size=4,spacing=1,hue_shift=.15,stroke_hue_jitter=.2)
    points=[BrushInput(0,0),BrushInput(128,0,time=1)]
    fixed=dabs(brush,points)
    assert len({d.hue for d in fixed}) == 1
    assert -.05 <= fixed[0].hue <= .35
    assert fixed[0].hue != dabs(brush,points,seed=8)[0].hue
    varied=dabs(replace(brush,hue_jitter=.1),points)
    assert len({d.hue for d in varied}) > 20
    assert all(abs(d.hue-fixed[0].hue) <= .1 for d in varied)


def test_pressure_only_hue_changes_visible_stamp_color_without_randomness():
    brush=BrushDefinition(size=10,spacing=3,hue_shift=1/3,
                          dynamics={"hue_shift":BrushDynamics(pressure=True)})
    store=TileStore(64)
    stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),{})
    stroke.begin(BrushInput(16,32,0))
    stroke.add(BrushInput(46,32,.5,time=.1))
    stroke.add(BrushInput(76,32,1,time=.2))
    stroke.finish()
    pixels=atlas(store)
    for x,color in ((16,(1,0,0)),(46,(1,1,0)),(76,(0,1,0))):
        np.testing.assert_allclose(pixels[32,x,:3],color,atol=1/255)


@pytest.mark.parametrize("mode",("mask","color"))
def test_image_tip_receives_signed_hue_without_changing_registered_alpha(mode):
    brush=BrushDefinition(size=16,tips=(tip(mode,(255,0,0,128)),),hue_shift=-1/3,antialiasing=0)
    store,_,_=rendered(brush,((64,32),),color="red")
    pixel=atlas(store)[32,64]
    np.testing.assert_allclose(pixel[:3],(0,0,1),atol=1/255)
    assert pixel[3] == pytest.approx(128/255,abs=1/255)


@pytest.mark.parametrize("target,left,right",(
    ("main",(0,1,0),(0,0,1)),
    ("sub",(1,0,0),(1,0,0)),
    ("both",(0,1,0),(1,0,0)),
))
@pytest.mark.parametrize("ribbon",(False,True))
def test_two_color_target_preserves_untargeted_main_or_sub_material_regions(target,left,right,ribbon):
    brush=BrushDefinition(size=16,tips=(tip("dual_color"),),hue_shift=1/3,
        color_change_target=target,sub_color=(0,0,255,255),ribbon=ribbon,antialiasing=0)
    points=((32,32),(96,32)) if ribbon else ((64,32),)
    store,_,_=rendered(brush,points,color="red")
    pixels=atlas(store)
    a,b=(pixels[28,64],pixels[36,64]) if ribbon else (pixels[32,60],pixels[32,68])
    np.testing.assert_allclose(a[:3],left,atol=1/255)
    np.testing.assert_allclose(b[:3],right,atol=1/255)


def test_color_targets_are_applied_before_secondary_color_blending():
    brush=BrushDefinition(size=16,hue_shift=1/3,sub_color_amount=.5,sub_color=(0,0,255,255))
    store,_,_=rendered(brush,((64,32),),color="red")
    np.testing.assert_allclose(atlas(store)[32,64,:3],(0,.5,.5),atol=1/255)


def test_perceptual_secondary_blending_uses_consistent_linear_light_approximation():
    brush=BrushDefinition(size=16,sub_color_amount=.5,sub_color=(0,0,255,255))
    standard,_,_=rendered(brush,((64,32),),color="red")
    perceptual,_,_=rendered(replace(brush,mixing_space="perceptual"),((64,32),),color="red")
    np.testing.assert_allclose(atlas(standard)[32,64,:3],(.5,0,.5),atol=1/255)
    np.testing.assert_allclose(atlas(perceptual)[32,64,:3],(.5**(1/2.2),0,.5**(1/2.2)),atol=1/255)
    assert atlas(perceptual)[32,64,3] == atlas(standard)[32,64,3]
    dual=replace(brush,sub_color_amount=0,dual=replace(brush,mixing_space="perceptual"),
                 dual_mode="normal",dual_apply_rgb=True)
    secondary,_,_=rendered(dual,((64,32),),color="red")
    np.testing.assert_array_equal(atlas(secondary)[32,64],atlas(perceptual)[32,64])


@pytest.mark.parametrize("ribbon",(False,True))
def test_secondary_brush_color_offset_affects_rgb_only_when_rgb_combining_is_enabled(ribbon):
    primary=BrushDefinition(size=16,tips=(tip("mask"),),ribbon=ribbon,hue_shift=1/3,
        dual=BrushDefinition(size=16,tips=(tip("color"),),ribbon=ribbon,hue_shift=-1/3),
        dual_mode="normal",dual_apply_rgb=True)
    points=((32,32),(96,32)) if ribbon else ((64,32),)
    with_rgb,_,_=rendered(primary,points,color="red")
    alpha_only,_,_=rendered(replace(primary,dual_apply_rgb=False),points,color="red")
    np.testing.assert_allclose(atlas(with_rgb)[32,64,:3],(0,0,1),atol=1/255)
    np.testing.assert_allclose(atlas(alpha_only)[32,64,:3],(0,1,0),atol=1/255)


def test_sub_only_target_does_not_recolor_an_authored_rgb_material():
    brush=BrushDefinition(size=16,tips=(tip("color"),),hue_shift=1/3,color_change_target="sub")
    store,_,_=rendered(brush,((64,32),),color="blue")
    np.testing.assert_allclose(atlas(store)[32,64,:3],(1,0,0),atol=1/255)


@pytest.mark.parametrize("mode",("mask","color"))
def test_wet_paint_mixes_shifted_fresh_pigment_with_existing_canvas_pigment(mode):
    store=TileStore(64)
    blue=np.zeros((64,64,4),np.float32)
    blue[:]=(0,0,1,1)
    store.set_tile("paint",(0,0),pixels_image(blue))
    brush=BrushDefinition(size=16,tips=(tip(mode),),hue_shift=1/3,
        mixing_mode="blend",paint_amount=.5,color_stretch=.7)
    rendered(brush,((16,32),(48,32)),color="red",tiles=store)
    np.testing.assert_allclose(atlas(store)[32,32,:3],(0,.5,.5),atol=1/255)
