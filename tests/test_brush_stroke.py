from dataclasses import replace
import math

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput, BrushTip, default_brushes
from comic_editor.core.brush_stroke import BrushStroke


def render_samples(definition, samples, seed=42):
    stroke = BrushStroke(definition,seed)
    result = stroke.begin(samples[0])
    for sample in samples[1:]:
        result += stroke.add(sample)
    result += stroke.finish()
    return result


def test_spacing_carries_across_input_packets():
    brush = BrushDefinition(size=20,spacing=.3,density=.2)
    coarse = render_samples(brush,[BrushInput(0,0),BrushInput(120,0,time=1)])
    fine = render_samples(brush,[BrushInput(x,0,time=x/120) for x in range(121)])
    assert [(d.x,d.y,d.density) for d in coarse] == [(d.x,d.y,d.density) for d in fine]
    assert [d.x for d in coarse] == list(range(0,121,6))


def test_pressure_sampling_is_independent_of_packet_boundaries():
    brush = BrushDefinition(size=32,spacing=.2,dynamics={"size":BrushDynamics(pressure=True,minimum=.05)})
    coarse = render_samples(brush,[BrushInput(0,0,0),BrushInput(200,0,1,time=1)])
    fine = render_samples(brush,[BrushInput(x,0,x/200,time=x/200) for x in range(201)])
    assert len(coarse)==len(fine)
    for a,b in zip(coarse,fine):
        assert a.x == pytest.approx(b.x,abs=1e-9)
        assert a.size == pytest.approx(b.size,abs=1e-9)


def test_spray_randomness_is_repeatable_and_packet_independent():
    brush = BrushDefinition(size=50,spacing=.5,spray=True,particle_density=4,
                            dynamics={"particle_size":BrushDynamics(random=.1)})
    a=render_samples(brush,[BrushInput(0,0),BrushInput(200,0,time=1)])
    b=render_samples(brush,[BrushInput(x,0,time=x/200) for x in range(201)])
    assert len(a)==len(b)==36
    for x,y in zip(a,b):
        assert (x.x,x.y,x.size)==pytest.approx((y.x,y.y,y.size))
    assert a != render_samples(brush,[BrushInput(0,0),BrushInput(200,0,time=1)],seed=43)


def test_stroke_direction_includes_first_dab_and_single_click():
    brush = BrushDefinition(size=10,spacing=1,direction="stroke")
    dabs = render_samples(brush,[BrushInput(0,0),BrushInput(0,20)])
    assert [d.angle for d in dabs] == [90,90,90]
    assert len(render_samples(brush,[BrushInput(12,12)])) == 1


def test_opacity_and_density_are_distinct():
    brush = BrushDefinition(opacity=.4,density=.2)
    dab = render_samples(brush,[BrushInput(0,0)])[0]
    assert dab.opacity == .4
    assert dab.density == .2


@pytest.mark.parametrize("mode", ["blend", "running", "smear", "none"])
def test_opacity_dynamics_are_dormant_only_for_blend_and_running(mode):
    brush = BrushDefinition(opacity=.8, mixing_mode=mode,
                            dynamics={"opacity": BrushDynamics(pressure=True)})
    dab = render_samples(brush, [BrushInput(0, 0, pressure=.25)])[0]
    assert dab.opacity == pytest.approx(.8 if mode in {"blend", "running"} else .2)
    assert brush.dynamics["opacity"].pressure  # the saved setting is preserved


def test_single_tip_cycles_stop_and_nonrepeat_holds_last():
    brush = BrushDefinition(size=10,spacing=1,tips=tuple(BrushTip(str(i)) for i in range(3)))
    samples=[BrushInput(0,0),BrushInput(60,0)]
    assert [d.tip_index for d in render_samples(replace(brush,repeat_mode="once"),samples)]==[0,1,2]
    random_cycle=render_samples(replace(brush,repeat_mode="one_random"),samples)
    assert sorted(d.tip_index for d in random_cycle)==[0,1,2]
    assert [d.tip_index for d in render_samples(replace(brush,repeat_mode="hold_last"),samples)]==[0,1,2,2,2,2,2]
    assert len(render_samples(replace(brush,repeat_mode="once",ribbon=True),samples))>10


def test_tip_order_flip_and_padding_survive_serialization():
    brush = BrushDefinition(tips=tuple(BrushTip(str(i),100,50) for i in range(3)),
                            size=10,spacing=1,repeat_mode="pingpong",flip_x="alternate")
    restored = BrushDefinition.from_dict(brush.to_dict())
    assert restored == brush
    dabs=render_samples(restored,[BrushInput(0,0),BrushInput(50,0)])
    assert [d.tip_index for d in dabs]==[0,1,2,1,0,1]
    assert [d.flip_x for d in dabs]==[False,True,False,True,False,True]
    assert restored.tips[0].height==50


def test_end_taper_holds_only_tail_then_flushes():
    brush = BrushDefinition(size=10,spacing=1,taper_end=20)
    stroke=BrushStroke(brush)
    assert stroke.begin(BrushInput(0,0)) == []
    emitted=stroke.add(BrushInput(50,0))
    assert [d.x for d in emitted]==[0,10,20,30]
    tail=stroke.finish()
    assert [(d.x,d.size) for d in tail]==[(40,5),(50,1)]
    assert stroke.finish()==[]


def test_continuous_spray_uses_elapsed_time():
    brush=BrushDefinition(continuous=True,continuous_rate=10)
    dabs=render_samples(brush,[BrushInput(5,5,time=0),BrushInput(5,5,time=.5)])
    assert len(dabs)==6
    assert len(render_samples(replace(brush,continuous=False),[BrushInput(5,5),BrushInput(5,5,time=.5)]))==1


def test_input_pressure_compensation_precedes_each_brush_response():
    brush=BrushDefinition(size=40,global_pressure_curve=((0,0),(.5,1),(1,1)),
                          dynamics={"size":BrushDynamics(pressure=True,minimum=.2)})
    dab=render_samples(brush,[BrushInput(0,0,.25)])[0]
    assert dab.size==pytest.approx(24)
    assert dab.pressure==.25  # raw input survives replay and the dual brush
    assert BrushDefinition.from_dict(brush.to_dict())==brush


def test_fade_reaches_minimum_and_stays_there_without_holding_tail():
    brush=BrushDefinition(size=10,spacing=1,taper_mode="fade",taper_end=20,taper_minimum=.2)
    stroke=BrushStroke(brush)
    assert stroke.begin(BrushInput(0,0))[0].size==10
    dabs=stroke.add(BrushInput(40,0))
    assert [(d.x,round(d.size,5)) for d in dabs]==[(10,6),(20,2),(30,2),(40,2)]
    assert stroke.finish()==[]


def test_taper_each_parameter_has_its_own_minimum():
    brush=BrushDefinition(size=20,opacity=.8,taper_start=40,
                          taper_parameters=("size","opacity"),
                          taper_minima={"size":.25,"opacity":.75})
    dab=render_samples(brush,[BrushInput(0,0)])[0]
    assert dab.size==5
    assert dab.opacity==pytest.approx(.6)
    assert BrushDefinition.from_dict(brush.to_dict())==brush
    disabled=replace(brush,taper_parameters=())
    assert BrushDefinition.from_dict(disabled.to_dict()).taper_parameters==()


def test_spacing_taper_uses_final_length_and_preserves_packet_independence():
    brush=BrushDefinition(size=10,spacing=1,taper_end=30,taper_minimum=.25,
                          taper_parameters=("spacing",))
    def with_packets(points):
        stroke=BrushStroke(brush,path_length=60)
        result=stroke.begin(points[0])
        for point in points[1:]:
            result+=stroke.add(point)
        return result+stroke.finish()
    coarse=with_packets([BrushInput(0,0),BrushInput(60,0)])
    fine=with_packets([BrushInput(x,0) for x in range(61)])
    assert [d.x for d in coarse]==pytest.approx([d.x for d in fine])
    assert coarse[-1].x-coarse[-2].x < coarse[1].x-coarse[0].x
    assert all(d.size==10 for d in coarse)


def test_angle_velocity_tilt_and_wet_parameter_dynamics():
    brush=BrushDefinition(angle=10,dynamics={
        "angle":BrushDynamics(velocity=True,velocity_scale=100),
        "paint_amount":BrushDynamics(pressure=True,minimum=.2),
        "texture_density":BrushDynamics(pressure=True),
    })
    dabs=render_samples(brush,[BrushInput(0,0,.5),BrushInput(50,0,.5,time=1)])
    assert dabs[-1].angle==pytest.approx(190)
    assert dabs[-1].paint_amount==pytest.approx(.6)
    assert dabs[-1].texture_density==pytest.approx(.5)
    tilted=replace(brush,dynamics={"angle":BrushDynamics(tilt=True,tilt_minimum=.25)})
    dab=render_samples(tilted,[BrushInput(0,0,tilt_x=45)])[0]
    assert dab.angle==pytest.approx(235)


def test_spray_direction_taper_and_reverse_leg_flip():
    brush=BrushDefinition(size=30,spacing=1,spray=True,particle_density=8,
                          particle_direction="stroke",particle_size=4,
                          taper_start=60,taper_parameters=("size","particle_density"))
    dabs=render_samples(brush,[BrushInput(0,0),BrushInput(0,90)])
    assert not [dab for dab in dabs if dab.distance==0]
    assert len([dab for dab in dabs if dab.distance==30])==4
    assert all(dab.angle==90 and dab.size==4 for dab in dabs)
    tip_brush=BrushDefinition(size=10,spacing=1,repeat_mode="pingpong",flip_x="reverse",
                              tips=tuple(BrushTip(str(i)) for i in range(3)))
    dabs=render_samples(tip_brush,[BrushInput(0,0),BrushInput(60,0)])
    assert [dab.flip_x for dab in dabs]==[False,False,False,True,False,False,False]


def test_ribbon_emits_fine_path_and_exact_endpoint():
    brush=BrushDefinition(size=20,spacing=1,ribbon=True)
    dabs=render_samples(brush,[BrushInput(0,0),BrushInput(51,0)])
    assert len(dabs)>30
    assert dabs[-1].x==51


def test_presets_portable_and_inputs_finite():
    for brush in default_brushes():
        assert BrushDefinition.from_dict(brush.to_dict())==brush
    sample=BrushInput(float('nan'),float('inf'),2,120,-120,float('nan')).sanitized()
    assert all(math.isfinite(v) for v in (sample.x,sample.y,sample.rotation))
    assert sample.pressure==1
    assert (sample.tilt_x,sample.tilt_y)==(90,-90)


def test_signed_color_responses_roundtrip_without_allowing_negative_size():
    brush = BrushDefinition.from_dict({
        "hue_shift": -.5, "saturation_shift": -.3, "luminosity_shift": .25,
        "sub_color_amount": .4,
        "dynamics": {
            "hue_shift": {"pressure": True, "minimum": -.5, "random": -.35},
            "saturation_shift": {"tilt": True, "tilt_minimum": -.75},
            "luminosity_shift": {"velocity": True, "velocity_minimum": -1},
            "size": {"pressure": True, "minimum": -.5, "random": -.35},
        },
    })
    assert brush.dynamics["hue_shift"].minimum == -.5
    assert brush.dynamics["hue_shift"].random == -.35
    assert brush.dynamics["saturation_shift"].tilt_minimum == -.75
    assert brush.dynamics["luminosity_shift"].velocity_minimum == -1
    assert brush.dynamics["size"].minimum == brush.dynamics["size"].random == 0
    assert BrushDefinition.from_dict(brush.to_dict()) == brush


def test_constant_signed_color_offsets_do_not_randomize_between_dabs_or_strokes():
    brush = BrushDefinition(size=10, spacing=1, hue_shift=-.2,
                            saturation_shift=-.3, luminosity_shift=.1, sub_color_amount=.4)
    samples = [BrushInput(0, 0), BrushInput(100, 0, time=1)]
    for seed in (0, 17, 923):
        dabs = render_samples(brush, samples, seed)
        assert len(dabs) == 11
        assert {(d.hue, d.saturation, d.luminosity, d.sub_color_mix) for d in dabs} == {
            (-.2, -.3, .1, .4)}


def test_pressure_only_color_offsets_follow_curve_without_randomness():
    brush = BrushDefinition(size=10, spacing=1, hue_shift=.25, luminosity_shift=-.4,
                            dynamics={
                                "hue_shift": BrushDynamics(pressure=True, minimum=-1),
                                "luminosity_shift": BrushDynamics(pressure=True,
                                    pressure_curve=((0, 1), (1, 0))),
                            })
    samples = [BrushInput(0, 0, 0), BrushInput(100, 0, 1, time=1)]
    first = render_samples(brush, samples, seed=1)
    other = render_samples(brush, samples, seed=123)
    assert [(d.hue, d.luminosity) for d in first] == [(d.hue, d.luminosity) for d in other]
    assert [first[0].hue, first[5].hue, first[-1].hue] == pytest.approx([-.25, 0, .25])
    assert [first[0].luminosity, first[5].luminosity, first[-1].luminosity] == pytest.approx([-.4, -.2, 0])


def test_random_color_offsets_preserve_signed_range_and_packet_independence():
    brush = BrushDefinition(size=10, spacing=1, saturation_shift=-.4,
                            dynamics={"saturation_shift": BrushDynamics(random=-.5)})
    coarse = render_samples(brush, [BrushInput(0, 0), BrushInput(1000, 0, time=1)])
    fine = render_samples(brush, [BrushInput(x, 0, time=x/1000) for x in range(1001)])
    assert [d.saturation for d in coarse] == [d.saturation for d in fine]
    offsets = [d.saturation for d in coarse]
    assert all(-.4 <= offset <= .2 for offset in offsets)
    assert min(offsets) < -.3 and max(offsets) > .1
