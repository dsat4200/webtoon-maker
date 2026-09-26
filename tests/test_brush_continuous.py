"""Continuous paint uses elapsed stroke time while moving and while stationary."""
from dataclasses import asdict, replace
import hashlib
import json
import math

import pytest

from comic_editor.core.brushes import BrushDefinition, BrushDynamics, BrushInput, BrushTip
from comic_editor.core.brush_stroke import BrushStroke, interpolate


def draw(brush, samples, *, seed=42):
    stroke = BrushStroke(brush, seed)
    result = stroke.begin(samples[0])
    for sample in samples[1:]:
        result.extend(stroke.add(sample))
    result.extend(stroke.finish())
    return result


def subdivide(samples, count):
    return [samples[0]] + [interpolate(a,b,index/count)
        for a,b in zip(samples,samples[1:]) for index in range(1,count+1)]


def test_slower_moving_stroke_adds_more_paint_over_the_same_path():
    brush = BrushDefinition(size=10, spacing=2, density=.1, continuous=True, continuous_rate=20)
    fast = draw(brush,[BrushInput(0,0),BrushInput(100,0,time=.25)])
    slow = draw(brush,[BrushInput(0,0),BrushInput(100,0,time=1)])
    assert len(fast) == 11 and len(slow) == 26
    assert all(dab.density == .1 for dab in slow+fast)
    assert sum(dab.density for dab in slow) > 2*sum(dab.density for dab in fast)
    assert slow[0].x == fast[0].x == 0
    assert slow[-1].x == fast[-1].x == 100


def test_moving_and_stationary_transitions_keep_one_clock_and_gap_phase():
    brush = BrushDefinition(size=7,spacing=1,continuous=True,continuous_rate=10)
    samples = [BrushInput(0,0),BrushInput(4,0,time=.16),
               BrushInput(4,0,time=.26),BrushInput(10,0,time=.5)]
    result = draw(brush,samples)
    assert [dab.time for dab in result] == pytest.approx([0,.1,.2,.3,.38,.4,.5])
    assert [dab.x for dab in result] == pytest.approx([0,2.5,4,5,7,7.5,10])
    fine = draw(brush,subdivide(samples,20))
    for a,b in zip(result,fine):
        assert asdict(a) == pytest.approx(asdict(b),abs=1e-8)
    assert len(result) == len(fine)


@pytest.mark.parametrize("spray",[False,True])
@pytest.mark.parametrize("direction",["fixed","stroke"])
def test_clock_random_channels_and_tip_sequence_are_input_subdivision_invariant(spray,direction):
    brush = BrushDefinition(size=12,spacing=.7,continuous=True,continuous_rate=29,
        spray=spray,particle_density=3,particle_angle_random=.4,direction=direction,
        repeat_mode="pingpong",flip_x="alternate",hue_jitter=.3,density_by_gap=True,
        tips=tuple(BrushTip(str(i)) for i in range(3)),
        dynamics={"size":BrushDynamics(pressure=True,minimum=.3),
                  "spacing":BrushDynamics(pressure=True,minimum=.4,random=.3),
                  "density":BrushDynamics(velocity=True,velocity_minimum=.3,velocity_scale=200),
                  "thickness":BrushDynamics(tilt=True,tilt_minimum=.2),
                  "angle":BrushDynamics(random=.8)})
    samples = [BrushInput(0,0,.2,10,20,350,0),BrushInput(25,0,.6,30,10,5,.23),
               BrushInput(25,0,.8,45,0,20,.43),BrushInput(25,30,.4,20,30,40,.83)]
    coarse,fine = draw(brush,samples),draw(brush,subdivide(samples,31))
    assert len(coarse) == len(fine)
    for a,b in zip(coarse,fine):
        assert asdict(a) == pytest.approx(asdict(b),abs=1e-8)
    assert [dab.time for dab in coarse] == sorted(dab.time for dab in coarse)


def test_stationary_ticks_interpolate_pressure_tilt_and_wrapped_rotation():
    brush = BrushDefinition(size=20,continuous=True,continuous_rate=10,direction="rotation",
        dynamics={"size":BrushDynamics(pressure=True),
                  "thickness":BrushDynamics(tilt=True)})
    samples = [BrushInput(4,7,.2,0,0,350,0),BrushInput(4,7,.8,90,0,10,.5)]
    result = draw(brush,samples)
    assert [dab.pressure for dab in result] == pytest.approx([.2,.32,.44,.56,.68,.8])
    assert [dab.size for dab in result] == pytest.approx([4,6.4,8.8,11.2,13.6,16])
    assert [dab.angle for dab in result] == pytest.approx([350,354,358,2,6,10])
    assert [dab.thickness for dab in result] == pytest.approx([.01,.2,.4,.6,.8,1])
    assert len(result) == len(draw(brush,subdivide(samples,25)))


@pytest.mark.parametrize("origin",[1_000_000.037,6_000_000.037])
@pytest.mark.parametrize("rate",[29,240])
def test_nonzero_device_clock_origin_preserves_flow_and_packet_subdivision(origin,rate):
    # Live timestamps come from monotonic/device clocks, rather than starting
    # from zero as most recorded preview paths do. Preserve brush randomness
    # and gap phase despite the larger floating-point timestamp magnitudes.
    brush = BrushDefinition(size=11.3,spacing=.85,continuous=True,continuous_rate=rate,
        direction="stroke",hue_jitter=.3,flip_x="alternate",
        dynamics={"spacing":BrushDynamics(random=.25),
                  "size":BrushDynamics(pressure=True,minimum=.3)})
    samples = [BrushInput(0,0,.2,time=0),BrushInput(27,0,.7,time=.23),
               BrushInput(27,0,.3,time=.43),BrushInput(27,32,.9,time=.83)]
    expected = draw(brush,samples)
    shifted = [replace(sample,time=sample.time+origin) for sample in samples]
    for packets in (shifted,subdivide(shifted,31)):
        actual = draw(brush,packets)
        assert len(actual) == len(expected)
        for baseline,dab in zip(expected,actual):
            normalized = replace(dab,time=dab.time-origin)
            assert asdict(normalized) == pytest.approx(asdict(baseline),abs=2e-6)


def test_direction_sensitive_stationary_start_is_committed_once_in_time_order():
    brush = BrushDefinition(size=10,spacing=1,continuous=True,continuous_rate=10,
                            direction="stroke",angle=20)
    result = draw(brush,[BrushInput(0,0),BrushInput(0,0,time=.2),BrushInput(0,10,time=.4)])
    assert [dab.time for dab in result] == pytest.approx([0,.1,.2,.3,.4,.4])
    assert [dab.angle for dab in result] == pytest.approx([20,20,20,110,110,110])
    assert len([dab for dab in result if dab.time == 0]) == 1


def test_coincident_clock_and_distance_events_keep_both_paint_contributions():
    brush = BrushDefinition(size=10,spacing=1,continuous_rate=10,hue_jitter=.5,
                            flip_x="random",dynamics={"angle":BrushDynamics(random=.1)})
    samples = [BrushInput(0,0),BrushInput(100,0,time=1)]
    result = draw(replace(brush,continuous=True),samples)
    assert len(result) == 21
    assert [dab.x for dab in result] == [0]+[x for x in range(10,101,10) for _ in range(2)]
    # Slowing down to a speed where both grids line up must not reduce paint.
    faster = draw(replace(brush,continuous=True),[samples[0],replace(samples[1],time=.9)])
    assert len(faster) == 20
    assert len(result) > len(faster)


def test_timed_events_do_not_consume_or_reset_the_random_spacing_stream():
    brush = BrushDefinition(size=20,spacing=1,continuous_rate=17,
        dynamics={"spacing":BrushDynamics(random=.25)})
    samples = [BrushInput(0,0),BrushInput(400,0,time=1)]
    ordinary = draw(brush,samples)
    timed = draw(replace(brush,continuous=True),samples)
    timed_only = [dab for dab in timed if dab.time>0 and
                  abs(dab.time*brush.continuous_rate-round(dab.time*brush.continuous_rate)) < 1e-8]
    remaining = [dab for dab in timed if dab not in timed_only]
    assert [dab.distance for dab in remaining] == pytest.approx([dab.distance for dab in ordinary])
    assert len(timed_only) == 17


@pytest.mark.parametrize("moving",[False,True])
def test_long_pause_catchup_is_bounded_to_latest_second_and_retains_clock_phase(moving):
    brush = BrushDefinition(size=10,spacing=4096,spacing_mode="fixed",
                            continuous=True,continuous_rate=240)
    start,end = .037,1000000.073
    stroke = BrushStroke(brush)
    stroke.begin(BrushInput(0,0,time=start))
    result = stroke.add(BrushInput(100 if moving else 0,0,time=end))
    assert 239 <= len(result) <= 241
    assert all(end-1-1e-8 <= dab.time <= end+1e-8 for dab in result)
    for dab in result:
        assert (dab.time-start)*240 == pytest.approx(round((dab.time-start)*240),abs=1e-6)
        assert dab.x == pytest.approx(100*(dab.time-start)/(end-start) if moving else 0)
    following = stroke.add(BrushInput(100 if moving else 0,0,time=end+.01))
    assert 2 <= len(following) <= 3
    assert following[0].time > result[-1].time


def test_backward_time_input_cannot_replay_past_ticks_or_reverse_event_order():
    brush = BrushDefinition(size=10,spacing=1,continuous=True,continuous_rate=10)
    result = draw(brush,[BrushInput(0,0),BrushInput(0,0,time=.25),
                        BrushInput(20,0,time=.1),BrushInput(20,0,time=.4)])
    assert [dab.time for dab in result] == pytest.approx([0,.1,.2,.25,.25,.3,.4])
    assert all(math.isfinite(dab.time) for dab in result)


@pytest.mark.parametrize("kind,digest",[
    ("normal","65bd34271f9da67dbb851ffd309c1f9538170885d08c2845481b6daa83805b86"),
    ("spray","bd7b346b54e66d353c5704392be5c771149ba7ac961ec09e58c03c44f77ae11e"),
    ("ribbon","1e09677bad770b98d80447c38cb3e9c74d89eae0479da2265b62a5a64963e7cd"),
])
def test_disabled_continuous_retains_complete_prechange_dab_stream(kind,digest):
    # Captured before the continuous scheduler change: all dab fields, not only
    # positions, including material sequence, random colors and angle responses.
    brush = BrushDefinition(size=20,spacing=.6,density=.4,opacity=.7,hue_jitter=.2,
        flip_x="random",tips=(BrushTip("A"),BrushTip("B")),repeat_mode="pingpong",
        dynamics={"size":BrushDynamics(pressure=True,minimum=.3),
                  "spacing":BrushDynamics(random=.3),"angle":BrushDynamics(random=.8)})
    if kind == "spray":
        brush = replace(brush,spray=True,particle_density=3,particle_angle_random=.7)
    elif kind == "ribbon":
        brush = replace(brush,ribbon=True)
    samples = [BrushInput(0,0,.2,time=0),BrushInput(23,0,.4,time=.07),
               BrushInput(23,0,.5,time=.4),BrushInput(70,0,.8,time=.9),BrushInput(100,0,1,time=1.1)]
    result = json.dumps([asdict(dab) for dab in draw(brush,samples)],sort_keys=True,separators=(",",":"))
    assert hashlib.sha256(result.encode()).hexdigest() == digest


@pytest.mark.parametrize("restriction",["post_correction","ribbon"])
def test_incompatible_continuous_setting_is_dormant_without_editing_preset(restriction):
    brush = BrushDefinition(size=10,continuous=True,**{restriction:.6 if restriction=="post_correction" else True})
    samples = [BrushInput(0,0),BrushInput(20,0,time=.5),BrushInput(20,0,time=1)]
    assert draw(brush,samples) == draw(replace(brush,continuous=False),samples)
    assert brush.continuous
