"""Post-correction preserves sensors and replays against the original raster."""
from dataclasses import replace
import math

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.brushes import BrushDefinition,BrushDynamics,BrushInput
from comic_editor.core.brush_correction import corrected_samples,resolved_taper,stroke_length
from comic_editor.core.brush_raster import RasterBrushStroke,image_pixels
from comic_editor.core.tiles import TileStore
from test_brush_raster import atlas,rendered


def zigzag():
    return [BrushInput(16+i*8,32+(5 if i%2 else -5),pressure=.1+i*.08,
                       tilt_x=i,tilt_y=-i,rotation=15*i,time=i*.013) for i in range(13)]


def test_correction_smooths_distance_path_but_keeps_endpoints_and_sensor_sequence():
    original=zigzag()
    result=corrected_samples(original,.8)
    assert result[0] == original[0] and result[-1] == original[-1]
    assert stroke_length(result) < stroke_length(original)
    for a,b in zip(original,result):
        assert replace(a,x=b.x,y=b.y) == b
        assert math.hypot(a.x-b.x,a.y-b.y) < 10
    assert original == zigzag()


def test_zero_strength_stationary_and_two_point_inputs_are_exact_noops():
    points=zigzag()
    assert corrected_samples(points,0) == points
    assert corrected_samples(points[:2],1) == points[:2]
    stationary=[BrushInput(4,7,pressure=i*.1,time=i*.02) for i in range(10)]
    assert corrected_samples(stationary,1) == stationary
    tiny=[BrushInput(0,0),BrushInput(.000001,.000001),BrushInput(.000002,0)]
    assert all(math.isfinite(p.x) and math.isfinite(p.y) for p in corrected_samples(tiny,1))


def test_correction_resampling_workspace_is_bounded_for_long_coordinates(monkeypatch):
    import comic_editor.core.brush_correction as correction
    original=correction.gaussian_filter1d
    sizes=[]
    def checked(array,*args,**kwargs):
        sizes.append(len(array))
        return original(array,*args,**kwargs)
    monkeypatch.setattr(correction,"gaussian_filter1d",checked)
    points=[BrushInput(0,0),BrushInput(1000000,10),BrushInput(2000000,0)]
    assert corrected_samples(points,1)[-1] == points[-1]
    assert max(sizes) <= correction.MAX_CORRECTION_GRID


def test_default_renderer_does_not_buffer_input_or_repaint_at_finish():
    store=TileStore(64)
    stroke=RasterBrushStroke(store,"paint",BrushDefinition(size=6),QColor("red"),{})
    assert stroke._replay_inputs is None
    stroke.begin(BrushInput(10,32))
    stroke.add(BrushInput(110,32,time=1))
    before=atlas(store).copy()
    assert stroke.finish().isEmpty()
    np.testing.assert_array_equal(atlas(store),before)


def test_post_correction_removes_live_path_and_preserves_exact_undo_snapshots():
    store,before=TileStore(64),{}
    brush=BrushDefinition(size=5,post_correction=1)
    points=[BrushInput(16,40),BrushInput(40,40,time=.1),BrushInput(64,10,time=.2),
            BrushInput(88,40,time=.3),BrushInput(112,40,time=.4)]
    stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),before)
    stroke.begin(points[0])
    for point in points[1:]:
        stroke.add(point)
    assert atlas(store)[10,64,3] > .5
    original_dirty=stroke.bounds
    dirty=stroke.finish()
    assert atlas(store)[10,64,3] == 0
    assert dirty.contains(original_dirty)
    assert stroke._replay_inputs is None and stroke.main.bytes == 0
    for key,image in before.items():
        store.set_tile("paint",key,image)
    assert not store.object_tiles("paint")


def test_replay_uses_private_stroke_baseline_without_overwriting_older_transaction_snapshot():
    store=TileStore(64)
    actual=QImage(64,64,QImage.Format_ARGB32_Premultiplied)
    actual.fill(QColor("blue"))
    older=QImage(actual)
    older.fill(QColor("green"))
    store.set_tile("paint",(0,0),actual)
    before={(0,0):older}
    stroke=RasterBrushStroke(store,"paint",BrushDefinition(size=5,post_correction=1),QColor("red"),before)
    stroke.begin(BrushInput(10,32))
    stroke.add(BrushInput(30,10,time=.1))
    stroke.add(BrushInput(50,32,time=.2))
    stroke.finish()
    assert store.tile("paint",(0,0)).pixelColor(2,2) == QColor("blue")
    assert before[(0,0)] == older


def test_replay_preserves_pressure_and_random_seed():
    brush=BrushDefinition(size=14,post_correction=.6,spacing=.4,density=.5,
        dynamics={"size":BrushDynamics(pressure=True,minimum=.1,random=.5)})
    samples=zigzag()
    actual=TileStore(64)
    stroke=RasterBrushStroke(actual,"paint",brush,QColor("black"),{},seed=123)
    stroke.begin(samples[0])
    for sample in samples[1:]:
        stroke.add(sample)
    stroke.finish()
    expected=TileStore(64)
    corrected=corrected_samples(samples,.6)
    stroke=RasterBrushStroke(expected,"paint",replace(brush,post_correction=0),QColor("black"),{},seed=123)
    stroke.begin(corrected[0])
    for sample in corrected[1:]:
        stroke.add(sample)
    stroke.finish()
    np.testing.assert_array_equal(atlas(actual),atlas(expected))


def test_percentage_taper_uses_completed_length_and_removes_untapered_preview():
    brush=BrushDefinition(size=20,taper_mode="percentage",taper_start=25,taper_end=50)
    store=TileStore(64)
    stroke=RasterBrushStroke(store,"paint",brush,QColor("black"),{})
    stroke.begin(BrushInput(20,32))
    stroke.add(BrushInput(100,32,time=1))
    live=atlas(store).copy()
    stroke.finish()
    expected,_,_=rendered(replace(brush,taper_mode="length",taper_start=20,taper_end=40),((20,32),(100,32)))
    np.testing.assert_array_equal(atlas(store),atlas(expected))
    assert np.count_nonzero(atlas(store)[...,3]) < np.count_nonzero(live[...,3])
    assert resolved_taper(brush,160).taper_end == 80


def test_spacing_end_taper_replay_matches_known_length_and_packet_subdivision():
    brush=BrushDefinition(size=8,spacing=2,taper_end=40,taper_minimum=.15,
                          taper_parameters=("spacing",),density=.4)
    def draw(points, *, known_length=None):
        store=TileStore(64)
        stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),{},seed=13,
                                 _path_length=known_length)
        stroke.begin(points[0])
        for point in points[1:]:
            stroke.add(point)
        stroke.finish()
        assert stroke._replay_inputs is None
        return atlas(store)
    coarse=[BrushInput(16,32),BrushInput(112,32,time=1)]
    fine=[BrushInput(x,32,time=(x-16)/96) for x in range(16,113)]
    expected=draw(coarse,known_length=96)
    np.testing.assert_array_equal(draw(coarse),expected)
    np.testing.assert_array_equal(draw(fine),expected)


def test_secondary_percentage_taper_triggers_replay_and_resolves_completed_length():
    secondary=BrushDefinition(size=8,spacing=1.5,taper_mode="percentage",taper_end=70,
                              taper_minimum=.1,taper_parameters=("spacing",))
    brush=BrushDefinition(size=18,dual=secondary,dual_mode="multiply")
    actual,_,stroke=rendered(brush,((16,32),(112,32)))
    resolved=resolved_taper(brush,96)
    expected=TileStore(64)
    reference=RasterBrushStroke(expected,"paint",resolved,QColor("black"),{},_path_length=96)
    reference.begin(BrushInput(16,32))
    reference.add(BrushInput(112,32,time=.02))
    reference.finish()
    np.testing.assert_array_equal(atlas(actual),atlas(expected))
    assert stroke._replay_inputs is None


def test_post_correction_replay_preserves_fade_mode():
    brush=BrushDefinition(size=18,taper_mode="fade",taper_end=40,taper_minimum=.1,
                          post_correction=.5)
    actual,_,_=rendered(brush,((16,32),(112,32)))
    expected,_,_=rendered(replace(brush,post_correction=0),((16,32),(112,32)))
    np.testing.assert_array_equal(atlas(actual),atlas(expected))
    assert resolved_taper(brush,96).taper_mode == "fade"


@pytest.mark.parametrize("kind",("pen","wet","dual"))
def test_length_ending_is_visible_live_and_preserves_previous_final_pixels(kind):
    brush=BrushDefinition(size=18,taper_end=90,taper_start=8,stabilization=.2,
        density=.65,hue_jitter=.12,dynamics={"size":BrushDynamics(pressure=True,minimum=.2,random=.5)})
    if kind == "wet":
        brush=replace(brush,mixing_mode="running",paint_amount=.4,color_stretch=.7)
    if kind == "dual":
        brush=replace(brush,taper_end=0,dual=BrushDefinition(size=14,taper_end=90,stabilization=.6,
                      density=.8,dynamics={"size":BrushDynamics(pressure=True,minimum=.4)}))
    samples=[BrushInput(18,32,.3),BrushInput(40,25,.8,time=.1),BrushInput(64,35,.5,time=.2)]
    def draw(reference=False):
        store,before=TileStore(64),{}
        stroke=RasterBrushStroke(store,"paint",brush,QColor("red"),before,seed=52,
                                 _path_length=0. if reference else None)
        if reference:
            # Preserve the former renderer's held-tail scheduling as an oracle.
            stroke.scheduler.path_length=None
            if stroke.dual_scheduler:
                stroke.dual_scheduler.path_length=None
        stroke.begin(samples[0])
        for sample in samples[1:]:
            stroke.add(sample)
        if not reference:
            assert np.any(atlas(store)[...,3] > 0)
        stroke.finish()
        return store,before,stroke
    expected,_,_=draw(True)
    actual,before,stroke=draw()
    np.testing.assert_array_equal(atlas(actual),atlas(expected))
    assert stroke._replay_inputs is None
    for key,image in before.items():
        actual.set_tile("paint",key,image)
    assert not actual.object_tiles("paint")


def test_length_ending_live_snapshots_support_cancel_and_fade_needs_no_input_buffer():
    store,before=TileStore(64),{}
    original=QImage(64,64,QImage.Format_ARGB32_Premultiplied)
    original.fill(QColor("blue"))
    store.set_tile("paint",(0,0),original)
    stroke=RasterBrushStroke(store,"paint",BrushDefinition(size=18,taper_end=90),QColor("red"),before)
    stroke.begin(BrushInput(18,32))
    stroke.add(BrushInput(48,32,time=.1))
    assert store.tile("paint",(0,0)) != original
    for key,image in before.items():
        store.set_tile("paint",key,image)
    stroke._clear_working()
    assert store.tile("paint",(0,0)) == original
    for brush in (BrushDefinition(),BrushDefinition(taper_mode="fade",taper_end=90)):
        stroke=RasterBrushStroke(TileStore(64),"paint",brush,QColor("red"),{})
        assert stroke._replay_inputs is None
