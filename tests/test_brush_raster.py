"""Rendering contracts: opacity, materials, tile seams, wet pickup and ribbons."""
import base64
import io
from dataclasses import replace

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput, BrushTexture, BrushTip
from comic_editor.core.brush_raster import RasterBrushStroke, image_pixels, pixels_image, composite_pixels
from comic_editor.core.tiles import TileStore


def png(array):
    stream = io.BytesIO()
    Image.fromarray(array).save(stream, "PNG")
    return base64.b64encode(stream.getvalue()).decode()


def rendered(brush, points=((20, 32), (108, 32)), *, color="black", tiles=None, seed=0):
    tiles = TileStore(64) if tiles is None else tiles
    before = {}
    stroke = RasterBrushStroke(tiles, "paint", brush, QColor(color), before, seed=seed)
    stroke.begin(BrushInput(*points[0], time=0))
    for index, point in enumerate(points[1:], 1):
        stroke.add(BrushInput(*point, time=index*.02))
    stroke.finish()
    return tiles, before, stroke


def atlas(tiles, bounds=(0, 0, 128, 64)):
    x0, y0, x1, y1 = bounds
    out = np.zeros((y1-y0, x1-x0, 4), np.float32)
    n = tiles.tile_size
    for (kx, ky), image in tiles.iter_tiles("paint"):
        left, top = max(x0, kx*n), max(y0, ky*n)
        right, bottom = min(x1, (kx+1)*n), min(y1, (ky+1)*n)
        if right > left and bottom > top:
            out[top-y0:bottom-y0, left-x0:right-x0] = image_pixels(image)[top-ky*n:bottom-ky*n, left-kx*n:right-kx*n]
    return out


def test_stroke_opacity_caps_dab_overlap_but_separate_strokes_accumulate():
    brush = BrushDefinition(size=20, opacity=.4, density=.6, spacing=.08)
    store, before, _ = rendered(brush, ((20,32),(108,32),(20,32)))
    alpha = atlas(store)[32, 32:97, 3]
    assert np.max(alpha) <= .405
    assert np.min(alpha) >= .395
    assert all(value is None for value in before.values())
    rendered(brush, tiles=store)
    assert .63 < atlas(store)[32,64,3] < .65


def test_same_path_packet_subdivision_preserves_pixels():
    brush = BrushDefinition(size=17, opacity=.7, density=.15, hardness=.3, spacing=.13)
    coarse, _, _ = rendered(brush, ((20,32),(108,32)))
    fine, _, _ = rendered(brush, [(x,32) for x in range(20,109)])
    np.testing.assert_array_equal(atlas(coarse), atlas(fine))


def test_stationary_airbrush_accumulates_density_without_motion():
    brush = BrushDefinition(size=30, density=.05, hardness=.1, continuous=True, continuous_rate=20)
    store, _, stroke = rendered(brush, ((64,32),))
    initial = atlas(store)[32,64,3]
    store = TileStore(64)
    stroke = RasterBrushStroke(store,"paint",brush,QColor("black"),{})
    stroke.begin(BrushInput(64,32,time=0))
    stroke.add(BrushInput(64,32,time=.5))
    stroke.finish()
    assert atlas(store)[32,64,3] > initial*4


def test_mask_color_and_dual_color_materials_preserve_padding_and_rgb():
    material = np.zeros((16,32,4), np.uint8)
    material[4:12,8:16] = (0,0,0,255)
    material[4:12,16:24] = (255,255,255,255)
    tip = BrushTip("Two colors",32,16,png(material),mode="dual_color",shape="image")
    brush = BrushDefinition(size=32,tips=(tip,),antialiasing=0,sub_color=(0,0,255,255))
    store, _, _ = rendered(brush, ((64,32),),color="red")
    image = atlas(store)
    np.testing.assert_array_equal(image[32,59], [1,0,0,1])
    np.testing.assert_array_equal(image[32,69], [0,0,1,1])
    assert image[32,50,3] == 0
    assert image[24,64,3] == 0
    fixed = replace(brush,tips=(replace(tip,mode="color"),))
    store, _, _ = rendered(fixed, ((64,32),),color="red")
    np.testing.assert_array_equal(atlas(store)[32,69], [1,1,1,1])


def test_world_texture_matches_at_tile_boundary_and_is_packet_invariant():
    grain = np.zeros((8,8,4),np.uint8)
    grain[:,:,:3] = (np.indices((8,8)).sum(axis=0)%2*255)[:,:,None]
    grain[:,:,3] = 255
    brush = BrushDefinition(size=18,spacing=.1,texture=BrushTexture(png=png(grain),scale=2))
    coarse, _, _ = rendered(brush)
    fine, _, _ = rendered(brush, [(x,32) for x in range(20,109)])
    np.testing.assert_array_equal(atlas(coarse),atlas(fine))
    alpha = atlas(coarse)[32,24:104,3]
    assert alpha.min() < .5 and alpha.max() > .5
    np.testing.assert_array_equal(alpha[32:40],alpha[40:48])


def test_per_dab_texture_differs_from_once_per_stroke():
    grain = np.full((8,8,4),128,np.uint8)
    grain[:,:,3] = 255
    texture = BrushTexture(png=png(grain),per_dab=False)
    brush = BrushDefinition(size=20,density=.4,spacing=.1,texture=texture)
    a, _, _ = rendered(brush)
    b, _, _ = rendered(replace(brush,texture=replace(texture,per_dab=True)))
    assert atlas(b)[32,64,3] > atlas(a)[32,64,3]+.2


def test_dual_brush_uses_secondary_coverage_as_mask():
    primary = BrushDefinition(size=28,spacing=.1)
    dual = replace(primary,dual=BrushDefinition(size=8,spacing=.1),dual_mode="multiply")
    store, _, _ = rendered(dual)
    image = atlas(store)
    assert image[32,64,3] > .99
    assert image[39,64,3] == 0
    assert image[28:36,64,3].min() > .7


def test_erase_restores_alpha_and_before_snapshots_support_exact_undo():
    original = np.zeros((64,64,4),np.float32)
    original[:] = (.2,.4,.8,1)
    store = TileStore(64)
    store.set_tile("paint",(0,0),pixels_image(original))
    initial = QImage(store.tile("paint",(0,0)))
    brush = BrushDefinition(size=20,opacity=.5,blending_mode="erase")
    _, before, _ = rendered(brush,((32,32),),tiles=store)
    assert .49 < atlas(store)[32,32,3] < .51
    assert before[(0,0)] == initial
    for key, image in before.items():
        store.set_tile("paint",key,image)
    assert store.tile("paint",(0,0)) == initial


def test_ribbon_deforms_connected_material_without_stamp_gaps():
    material = np.zeros((16,16,4),np.uint8)
    material[:,4:12] = (0,0,0,255)
    tip = BrushTip("Ribbon",16,16,png(material),shape="image")
    brush = BrushDefinition(size=20,ribbon=True,tips=(tip,),spacing=.75)
    path = [(20,32),(40,32),(60,40),(80,32),(108,32)]
    store, _, _ = rendered(brush,path)
    image = atlas(store)
    assert image[28:45,25:102,3].max(axis=0).min() > .99
    assert np.count_nonzero(image[...,3]) > 650


def test_ribbon_once_stops_after_material_cycle():
    material = np.full((16,16,4),255,np.uint8)
    tip = BrushTip("Solid",16,16,png(material),shape="image")
    brush = BrushDefinition(size=20,ribbon=True,tips=(tip,),repeat_mode="once")
    store, _, _ = rendered(brush)
    image = atlas(store)
    assert image[32,25,3] > .99
    assert image[32,50:,3].max() == 0


def test_wet_brush_picks_up_active_layer_color_across_tiles():
    store = TileStore(64)
    blue = np.zeros((64,64,4),np.float32)
    blue[:] = (0,0,1,1)
    store.set_tile("paint",(0,0),pixels_image(blue))
    store.set_tile("paint",(1,0),pixels_image(blue))
    brush = BrushDefinition(size=20,mixing_mode="blend",paint_amount=.5,paint_density=1,spacing=.2)
    rendered(brush,color="red",tiles=store)
    image = atlas(store)
    assert image[32,64,0] > .3 and image[32,64,2] > .05
    np.testing.assert_allclose(image[32,63],image[32,64],atol=.05)
    np.testing.assert_array_equal(image[2,64],[0,0,1,1])


def test_watercolor_edge_has_no_tile_border_seam():
    brush = BrushDefinition(size=24,opacity=.5,watercolor_edge=2,
                           watercolor_opacity=.4,watercolor_blur=1)
    store, _, stroke = rendered(brush,color="red")
    image = atlas(store)
    np.testing.assert_allclose(image[:,63],image[:,64],atol=1/255)
    assert image[20,64,3] > image[32,64,3]
    assert not stroke.finish().isValid()


@pytest.mark.parametrize("mode",["multiply","screen","subtract","color_burn","hard_mix","color"])
def test_blend_modes_do_not_tint_transparent_destination(mode):
    destination = np.zeros((1,1,4),np.float32)
    source = np.asarray([[[.2,.1,.05,.5]]],np.float32)
    np.testing.assert_allclose(composite_pixels(destination,source,mode),source)


def test_large_stroke_spills_working_tiles_without_changing_result(tmp_path):
    brush = BrushDefinition(size=20,opacity=.5,density=.4)
    points = [(20,32),(360,32),(20,32)]
    expected, _, _ = rendered(brush,points)
    store = TileStore(64)
    stroke = RasterBrushStroke(store,"paint",brush,QColor("black"),{})
    stroke.main.budget = 64*64*20  # One working tile; exercise reload on return.
    stroke._base_budget = 64*64*16
    stroke.begin(BrushInput(*points[0]))
    stroke.add(BrushInput(*points[1],time=1))
    assert stroke.main.spilled
    assert stroke.main.bytes <= stroke.main.budget
    assert stroke._base_bytes <= stroke._base_budget
    stroke.add(BrushInput(*points[2],time=2))
    stroke.finish()
    np.testing.assert_array_equal(atlas(expected,(0,0,384,64)),atlas(store,(0,0,384,64)))
    assert stroke.main.bytes == 0
    assert stroke.main._temporary is None


def test_material_cache_obeys_byte_budget():
    from comic_editor.core.brush_raster import _MaterialCache
    cache = _MaterialCache(budget=1600)
    data = np.full((16,16,4),255,np.uint8)
    cache.get(png(data))
    cache.get(png(data),premultiplied=True)
    assert cache.bytes <= cache.budget
    assert len(cache.values) == 1


def test_selection_clips_complete_stroke_once_and_does_not_snapshot_empty_tiles():
    pixels = np.ones((64,64,4),np.float32)
    pixels[:,32:,3] = 0
    pixels[:,31,3] = .5
    mask = pixels_image(pixels)
    store,before = TileStore(64),{}
    stroke = RasterBrushStroke(store,"paint",BrushDefinition(size=20),QColor("red"),before,
                               selection_tile=lambda key: mask if key == (0,0) else None)
    stroke.begin(BrushInput(20,32))
    stroke.add(BrushInput(108,32,time=1))
    stroke.add(BrushInput(20,32,time=2))
    stroke.finish()
    image = atlas(store)
    assert image[32,30,3] == 1
    assert .49 < image[32,31,3] < .51
    assert image[:,32:,3].max() == 0
    assert set(before) == {(0,0)}


def test_texture_density_dynamic_changes_grain_strength_and_spill_preserves_parameters():
    grain = np.full((8,8,4),128,np.uint8)
    grain[:,:,3] = 255
    brush = BrushDefinition(size=20,texture=BrushTexture(png=png(grain)),
                            dynamics={"texture_density":BrushDynamics(pressure=True)})
    def dot(pressure):
        store=TileStore(64)
        stroke=RasterBrushStroke(store,"paint",brush,QColor("black"),{})
        stroke.begin(BrushInput(32,32,pressure=pressure))
        stroke.finish()
        return atlas(store)[32,32,3]
    assert dot(.2) > dot(1)+.3
    store=TileStore(64)
    stroke=RasterBrushStroke(store,"paint",brush,QColor("black"),{})
    stroke.main.budget=64*64*28
    stroke.begin(BrushInput(20,32,pressure=.2))
    stroke.add(BrushInput(360,32,pressure=1,time=1))
    stroke.add(BrushInput(20,32,pressure=.2,time=2))
    stroke.finish()
    assert atlas(store,(0,0,384,64))[32,30,3] > .75


def test_paint_amount_dynamic_controls_wet_color_loading():
    brush=BrushDefinition(size=20,mixing_mode="blend",paint_amount=1,
                           dynamics={"paint_amount":BrushDynamics(pressure=True)})
    def paint(pressure):
        store=TileStore(64)
        background=QImage(64,64,QImage.Format_ARGB32_Premultiplied)
        background.fill(QColor("blue"))
        store.set_tile("paint",(0,0),background)
        stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),{})
        stroke.begin(BrushInput(32,32,pressure=pressure))
        stroke.finish()
        return atlas(store)[32,32,:3]
    assert paint(.1)[0] < .15
    assert paint(1)[0] > .99


def test_antialias_strengths_have_distinct_edge_coverage():
    results=[]
    for aa in range(4):
        store,_,_=rendered(BrushDefinition(size=19.5,antialiasing=aa),((32.3,32.2),))
        results.append(atlas(store))
    assert set(np.unique(results[0][...,3])) == {0,1}
    assert all(not np.array_equal(a,b) for a,b in zip(results,results[1:]))


def test_horizontal_ribbon_tip_rotated_ninety_preserves_aspect_ratio():
    material=np.zeros((8,32,4),np.uint8)
    material[2:6,:]=(0,0,0,255)
    tip=BrushTip("Horizontal ribbon",32,8,png(material),shape="image")
    brush=BrushDefinition(size=32,tips=(tip,),ribbon=True,angle=90)
    store,_,_=rendered(brush)
    alpha=atlas(store)[...,3]
    assert alpha[31:33,24:100].min() > .9
    assert alpha[24:26,24:100].max() == 0
    assert np.count_nonzero(alpha[:,64]) <= 8


def test_texture_color_dodge_changes_translucent_coverage():
    paper=np.full((8,8,4),128,np.uint8)
    paper[:,:,3]=255
    brush=BrushDefinition(size=20,opacity=.3,
        texture=BrushTexture(png=png(paper),mode="color_dodge"))
    textured,_,_=rendered(brush)
    plain,_,_=rendered(replace(brush,texture=None))
    assert atlas(textured)[32,64,3] > atlas(plain)[32,64,3]+.2
    assert atlas(textured)[0,0,3] == 0


def test_wet_dab_working_spill_preserves_pickup_and_pixels():
    brush=BrushDefinition(size=100,mixing_mode="running",paint_amount=.4,
                           color_stretch=.8,blur=.3)
    def draw(budget):
        store=TileStore(64)
        background=np.zeros((64,64,4),np.float32)
        background[:]=(0,0,1,1)
        for key in ((0,0),(1,0),(0,1),(1,1)):
            store.set_tile("paint",key,pixels_image(background))
        stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),{})
        stroke.main.budget=budget
        stroke.begin(BrushInput(50,50))
        stroke.add(BrushInput(70,70,time=.1))
        stroke.finish()
        return atlas(store,(0,0,128,128))
    np.testing.assert_array_equal(draw(32*1024*1024),draw(64*64*20))


def test_ribbon_flips_change_per_material_tile_instead_of_geometry_sample():
    material=np.zeros((16,16,4),np.uint8)
    material[:,:8]=(0,0,0,255)
    tip=BrushTip("Half",16,16,png(material),shape="image")
    brush=BrushDefinition(size=16,tips=(tip,),ribbon=True,flip_x="alternate")
    store,_,_=rendered(brush)
    alpha=atlas(store)[...,3]
    assert alpha[28,28] > .99 and alpha[36,28] == 0
    assert alpha[28,44] == 0 and alpha[36,44] > .99
    assert alpha[28,60] > .99 and alpha[36,60] == 0
    brush=replace(brush,tips=(tip,tip,tip),repeat_mode="pingpong",flip_x="reverse")
    store,_,_=rendered(brush)
    alpha=atlas(store)[...,3]
    assert alpha[28,60] > .99 and alpha[36,60] == 0
    assert alpha[28,76] == 0 and alpha[36,76] > .99


def test_random_ribbon_sequence_keeps_only_current_cycle_and_is_seeded():
    import random
    brush=BrushDefinition(ribbon=True,repeat_mode="random",tips=tuple(BrushTip(str(i)) for i in range(5)))
    stroke=RasterBrushStroke(TileStore(64),"paint",brush,QColor("black"),{},seed=99)
    expected=random.Random(99 ^ 919)
    for index in range(10000):
        stroke.main.ribbon_cycle=index
        choice=stroke._ribbon_index(stroke.main)
        assert choice == expected.randrange(5)
        assert stroke._ribbon_index(stroke.main) == choice
    assert stroke.main.random_cycle == []


def test_disabled_texture_dynamics_and_taper_do_not_allocate_parameter_tiles():
    from comic_editor.core.brush_raster import _StrokePlane
    brush=BrushDefinition(texture=BrushTexture(),dynamics={"texture_density":BrushDynamics()},
                          taper_parameters=("texture_density",))
    assert not _StrokePlane(brush,64).track_texture
    assert _StrokePlane(replace(brush,taper_start=12),64).track_texture
    assert _StrokePlane(replace(brush,dynamics={"texture_density":BrushDynamics(pressure=True)}),64).track_texture


@pytest.mark.parametrize("mode",("blend","running","smear"))
def test_wet_pigment_crosses_empty_canvas_and_changes_after_next_color(mode):
    def draw(stretch):
        store=TileStore(64)
        pixels=np.zeros((64,128,4),np.float32)
        pixels[:,0:32]=(0,0,1,1)
        pixels[:,80:96]=(1,1,0,1)
        for index in range(2):
            store.set_tile("paint",(index,0),pixels_image(pixels[:,index*64:(index+1)*64]))
        brush=BrushDefinition(size=12,mixing_mode=mode,paint_amount=0,color_stretch=stretch,spacing=.12)
        rendered(brush,((16,32),(120,32)),color="red",tiles=store)
        return atlas(store)
    carried,local=draw(1),draw(0)
    assert carried[32,60,3] > .2  # carried across the transparent gap
    assert carried[32,60,2] > .9
    assert carried[32,110,1] > .05 and carried[32,110,2] > .05
    assert carried[32,60,3] > local[32,60,3]+.1


def test_running_paint_keeps_canvas_color_in_dense_overlapping_dabs():
    store=TileStore(64)
    blue=np.zeros((64,64,4),np.float32)
    blue[:]=(0,0,1,1)
    store.set_tile("paint",(0,0),pixels_image(blue))
    brush=BrushDefinition(size=24,mixing_mode="running",paint_amount=.6,color_stretch=.7,spacing=.04)
    rendered(brush,((10,32),(110,32)),color="red",tiles=store)
    result=atlas(store)
    assert result[32,40,2] > .35  # pickup survives repeated foreground deposition
    assert result[32,85,2] > .15  # the same pigment travels beyond the patch


def test_wet_reservoir_is_bounded_packet_invariant_and_full_paint_matches_dry():
    def draw(points,amount=.4,mode="blend"):
        store=TileStore(64)
        pixels=np.zeros((64,64,4),np.float32)
        pixels[:,:32]=(0,0,1,1)
        store.set_tile("paint",(0,0),pixels_image(pixels))
        brush=BrushDefinition(size=18,mixing_mode=mode,paint_amount=amount,color_stretch=.7,spacing=.12)
        stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),{})
        stroke.begin(BrushInput(*points[0]))
        for index,point in enumerate(points[1:],1):
            stroke.add(BrushInput(*point,time=index*.02))
        if mode != "none":
            assert stroke.main.wet_pickup.nbytes == 64*64*4*4
        stroke.finish()
        assert stroke.main.wet_pickup is None
        return atlas(store)
    path=((16,32),(112,32))
    np.testing.assert_array_equal(draw(path),draw(tuple((x,32) for x in range(16,113))))
    np.testing.assert_array_equal(draw(path,1),draw(path,1,"none"))
