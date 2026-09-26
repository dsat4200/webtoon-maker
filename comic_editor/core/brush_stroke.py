"""Deterministic distance/time brush sampling without a raster dependency.

The scheduler carries spacing across input packets. Dabs, device input and
material definitions stay separate so a future vector stroke can retain and
replay the same inputs without introducing vector editing into this tool.
"""
from __future__ import annotations

import math
import random
from collections import deque
from dataclasses import replace

from .brushes import (SIGNED_COLOR_DYNAMICS, BrushDab, BrushDefinition,
                      BrushDynamics, BrushInput, clamp)


def curve_value(knots: tuple[tuple[float, float], ...], x: float) -> float:
    x = clamp(x)
    if not knots:
        return x
    if x <= knots[0][0]:
        return clamp(knots[0][1])
    for (x0,y0),(x1,y1) in zip(knots,knots[1:]):
        if x <= x1:
            return clamp(y0+(y1-y0)*(x-x0)/max(1e-12,x1-x0))
    return clamp(knots[-1][1])


def interpolate(a: BrushInput, b: BrushInput, t: float) -> BrushInput:
    def lerp(u,v):
        return u+(v-u)*t
    rotation = a.rotation + ((b.rotation-a.rotation+180)%360-180)*t
    return BrushInput(lerp(a.x,b.x),lerp(a.y,b.y),lerp(a.pressure,b.pressure),
                      lerp(a.tilt_x,b.tilt_x),lerp(a.tilt_y,b.tilt_y),rotation,
                      lerp(a.time,b.time))


class BrushStroke:
    """Emit brush dabs, including spray particles, from normalized input.

    End taper keeps only its trailing distance pending; finish releases that
    tail with the proper final-length envelope. Continuous spraying merges a
    stroke-anchored clock with distance events, including stationary samples.
    """
    def __init__(self, definition: BrushDefinition, seed: int = 0, *, path_length: float | None = None):
        self.definition = definition
        self.rng = random.Random(seed)
        # Gap choices belong to distance intervals, independent of particle,
        # material, color and diagnostic-query randomness.
        self.spacing_rng = random.Random(seed ^ 0x53504143)
        self.seed = seed
        self.path_length = path_length
        self.last: BrushInput | None = None
        self.distance = 0.0
        self.next_distance = 0.0
        self.next_time = 0.0
        # Post correction owns the final path; CSP disables continuous paint
        # in that combination. Zero-length ribbon accumulation is unsupported.
        self.continuous_enabled = bool(definition.continuous and not definition.ribbon
                                       and definition.post_correction <= 0)
        self._time_origin = 0.0
        self._time_tick = 1
        self._time_rate = clamp(definition.continuous_rate,1,240)
        self.index = 0
        self.angle = definition.angle
        self.velocity = 0.0
        self.pending: deque[BrushDab] = deque()
        self.initial: BrushInput | None = None
        self.finished = False
        self.last_emitted: BrushInput | None = None
        self.random_cycle = list(range(max(1,len(definition.tips))))
        self.rng.shuffle(self.random_cycle)
        self.stroke_color = (
            self.rng.uniform(-1,1)*definition.stroke_hue_jitter,
            self.rng.uniform(-1,1)*definition.stroke_saturation_jitter,
            self.rng.uniform(-1,1)*definition.stroke_luminosity_jitter,
            self.rng.random()*definition.stroke_sub_color_mix,
        )

    def _factor(self, name: str, sample: BrushInput, *, randomized=True) -> float:
        dynamic = self.definition.dynamics.get(name)
        if dynamic is None:
            return 1.0
        value = 1.0
        if dynamic.pressure:
            value *= dynamic.minimum+(1-dynamic.minimum)*curve_value(
                dynamic.pressure_curve,curve_value(self.definition.global_pressure_curve,sample.pressure))
        if dynamic.tilt:
            tilt = min(1,math.hypot(sample.tilt_x,sample.tilt_y)/90)
            value *= dynamic.tilt_minimum+(dynamic.tilt_maximum-dynamic.tilt_minimum)*curve_value(
                dynamic.tilt_curve,tilt)
        if dynamic.velocity:
            value *= dynamic.velocity_minimum+(1-dynamic.velocity_minimum)*curve_value(
                dynamic.velocity_curve,self.velocity/max(1,dynamic.velocity_scale))
        if randomized and dynamic.random < 1:
            value *= self.rng.uniform(dynamic.random,1)
        maximum = max(1., dynamic.tilt_maximum) if dynamic.tilt else 1.
        return clamp(value, -1 if name in SIGNED_COLOR_DYNAMICS else 0, maximum)

    def _size(self,sample: BrushInput, *, randomized=True) -> float:
        minimum = 1 if self.definition.minimum_pixel else .1
        return max(minimum,self.definition.size*self._factor("size",sample,randomized=randomized))

    def _envelope(self, distance: float, final_length: float | None = None,
                  parameter: str | None = None) -> float:
        b = self.definition
        if b.taper_mode == "fade":
            envelope = 1-clamp(distance/b.taper_end) if b.taper_end>0 else 1.0
        else:
            envelope = min(1.,clamp(distance/b.taper_start)) if b.taper_start>0 else 1.0
            if final_length is not None and b.taper_end>0:
                envelope = min(envelope,clamp((final_length-distance)/b.taper_end))
        minimum = b.taper_minima.get(parameter,b.taper_minimum)
        return minimum+(1-minimum)*envelope

    def _spacing(self, sample: BrushInput, distance: float = 0., *, random_ratio: float = 1.) -> float:
        b = self.definition
        size = self._size(sample,randomized=False)
        step = b.spacing*(size if b.spacing_mode == "relative" else 1)
        step *= self._factor("spacing",sample,randomized=False)
        step *= random_ratio
        if "spacing" in b.taper_parameters:
            step *= self._envelope(distance,self.path_length,"spacing")
        if b.ribbon:
            step = min(step,size*.08)
        return max(.25,step)

    def _next_spacing(self, sample: BrushInput, distance: float) -> float:
        """Choose once per scheduled interval; retain next_distance across packets."""
        dynamic = self.definition.dynamics.get("spacing")
        ratio = (self.spacing_rng.uniform(dynamic.random,1)
                 if dynamic is not None and dynamic.random < 1 else 1.)
        return self._spacing(sample,distance,random_ratio=ratio)

    def _tip_index(self) -> int:
        count = max(1,len(self.definition.tips))
        mode = self.definition.repeat_mode
        if mode == "reverse":
            return count-1-self.index%count
        if mode == "random":
            return self.rng.randrange(count)
        if mode == "one_random":
            return self.random_cycle[self.index] if self.index < count else -1
        if mode == "once":
            return self.index if self.index < count else -1
        if mode == "hold_last":
            return min(self.index,count-1)
        if mode == "pingpong" and count>1:
            phase = self.index%(2*count-2)
            return phase if phase<count else 2*count-2-phase
        return self.index%count

    def _flip(self, mode: str) -> bool:
        if mode in ("fixed",True):
            return True
        if mode == "random":
            return bool(self.rng.getrandbits(1))
        if mode == "reverse":
            count=max(1,len(self.definition.tips))
            return (self.definition.repeat_mode=="pingpong" and count>1
                    and self.index%(2*count-2)>=count)
        return mode == "alternate" and bool(self.index%2)

    def _angle(self,sample: BrushInput, direction: float) -> float:
        b = self.definition
        angle = b.angle
        if b.direction == "stroke":
            angle += direction
        elif b.direction == "tilt":
            angle += math.degrees(math.atan2(sample.tilt_y,sample.tilt_x))
        elif b.direction == "rotation":
            angle += sample.rotation
        dynamics = b.dynamics.get("angle")
        if dynamics:
            if dynamics.pressure or dynamics.tilt or dynamics.velocity:
                angle += 360*self._factor("angle",sample,randomized=False)
            if dynamics.random < 1:
                angle += self.rng.uniform(-180,180)*(1-dynamics.random)
        return angle%360

    def _emit(self,sample: BrushInput,distance: float,direction: float) -> None:
        b = self.definition
        size = self._size(sample)
        # CSP keeps the stroke opacity value but disables its input dynamics
        # when Blend or Running color performs wet alpha mixing.
        opacity = clamp(b.opacity if b.mixing_mode in {"blend","running"}
                        else b.opacity*self._factor("opacity",sample))
        density = clamp(b.density*self._factor("density",sample))
        if b.density_by_gap:
            density = 1-(1-density)**max(.01,self._spacing(sample,distance)/max(.1,size*.1))
        angle = self._angle(sample,direction)
        thickness = max(.01,b.thickness*self._factor("thickness",sample))
        particles = min(256,max(1,round(b.particle_density*self._factor("particle_density",sample)))) if b.spray else 1
        for particle_index in range(particles):
            tip_index = 0 if b.ribbon else self._tip_index()
            if tip_index < 0:
                self.index += 1
                continue
            x,y,particle_size = sample.x,sample.y,size
            particle_angle = angle
            if b.spray:
                theta = self.rng.uniform(0,math.tau)
                # Negative deviation moves particles toward the edge without
                # a negative exponent sending them outside the brush radius.
                # The distribution is independent, pending CSP calibration.
                exponent = (.5+4*b.spray_deviation if b.spray_deviation >= 0
                            else .5/(1-8*b.spray_deviation))
                radius = (self.rng.random()**exponent)*size*.5
                x += math.cos(theta)*radius
                y += math.sin(theta)*radius
                particle_size = max(.1,b.particle_size*(size if b.particle_size_relative else 1)
                                    *self._factor("particle_size",sample))
                # Particle orientation is independent of the direction used for
                # the whole spray. Only whole_spray explicitly inherits it.
                particle_angle = b.particle_angle
                if b.particle_direction == "stroke":
                    particle_angle += direction
                elif b.particle_direction == "whole_spray":
                    particle_angle += angle
                elif b.particle_direction == "center":
                    particle_angle += math.degrees(math.atan2(sample.y-y,sample.x-x))
                elif b.particle_direction == "radial":
                    # Retain the outward direction of older authored presets.
                    particle_angle += angle+math.degrees(theta)
                elif b.particle_direction == "random":
                    particle_angle += angle+self.rng.uniform(0,360)
                if b.particle_angle_random:
                    particle_angle += self.rng.uniform(-180,180)*b.particle_angle_random
                particle_angle %= 360
            dab = BrushDab(
                x,y,particle_size,opacity,density,particle_angle,thickness,tip_index,
                self._flip(b.flip_x),self._flip(b.flip_y),
                self.stroke_color[0]+self.rng.uniform(-1,1)*b.hue_jitter*self._factor("hue",sample)
                +(b.hue_shift*self._factor("hue_shift",sample) if b.hue_shift else 0),
                self.stroke_color[1]+self.rng.uniform(-1,1)*b.saturation_jitter*self._factor("saturation",sample)
                +(b.saturation_shift*self._factor("saturation_shift",sample) if b.saturation_shift else 0),
                self.stroke_color[2]+self.rng.uniform(-1,1)*b.luminosity_jitter*self._factor("luminosity",sample)
                +(b.luminosity_shift*self._factor("luminosity_shift",sample) if b.luminosity_shift else 0),
                clamp(self.stroke_color[3]+self.rng.random()*b.sub_color_mix*self._factor("sub_color_mix",sample)
                      +(b.sub_color_amount*self._factor("sub_color_amount",sample) if b.sub_color_amount else 0)),
                distance,sample.time,sample.pressure,
                **{name:self._factor(name,sample) for name in (
                    "texture_density","paint_amount","paint_density","blur","hardness","color_stretch")},
                center_x=sample.x if b.spray else None,center_y=sample.y if b.spray else None,
                particle_index=particle_index,particle_count=particles)
            self.pending.append(dab)
            self.index += 1
        self.last_emitted = sample

    def _drain(self, final=False) -> list[BrushDab]:
        result = []
        b = self.definition
        tail = b.taper_end if b.taper_mode != "fade" else 0.
        while self.pending and (final or self.pending[0].distance <= self.distance-tail):
            dab = self.pending.popleft()
            final_length = self.distance if final else self.path_length
            def factor(parameter):
                return self._envelope(dab.distance,final_length,parameter)
            updates = {name:getattr(dab,name)*factor(name) for name in b.taper_parameters
                       if name in {"size","opacity","density","thickness","texture_density",
                                   "paint_amount","paint_density","blur","hardness","color_stretch"}}
            if b.spray:
                if "particle_density" in b.taper_parameters and dab.particle_index>=round(dab.particle_count*factor("particle_density")):
                    continue
                if "size" in b.taper_parameters:
                    updates["x"]=dab.center_x+(dab.x-dab.center_x)*factor("size")
                    updates["y"]=dab.center_y+(dab.y-dab.center_y)*factor("size")
                    if not b.particle_size_relative:
                        updates.pop("size",None)
                if "particle_size" in b.taper_parameters:
                    updates["size"]=updates.get("size",dab.size)*factor("particle_size")
            if b.minimum_pixel:
                updates["size"]=max(1.,updates.get("size",dab.size))
            result.append(replace(dab,**updates) if updates else dab)
        return result

    def begin(self,sample: BrushInput) -> list[BrushDab]:
        if self.last is not None:
            raise RuntimeError("Brush stroke already started")
        sample = sample.sanitized()
        self.last = sample
        self.initial = sample
        self.next_time = sample.time+1/max(1,self.definition.continuous_rate)
        self._time_origin = sample.time
        if self.continuous_enabled:
            self.next_time = sample.time+1/self._time_rate
        needs_direction = (self.definition.direction == "stroke" or
                           (self.definition.spray and self.definition.particle_direction == "stroke"))
        if not needs_direction and not self.definition.ribbon:
            self._emit(sample,0,0)
            self.initial = None
            self.next_distance = self._next_spacing(sample,0.)
        return self._drain()

    def _continuous_segment(self,last: BrushInput,sample: BrushInput,length: float,
                            direction: float) -> None:
        """Merge two persistent event streams, consuming gap RNG only at gaps.

        Clock events never reset the next distance event, and input packets
        never reset the clock. Coincident events retain both contributions,
        distance first: merging them would make paint decrease at speeds where
        the two grids happen to align. Delayed input catches up at most one
        second of timed paint, retaining clock phase.
        """
        dt = sample.time-last.time
        if dt > 0:
            cutoff = max(last.time,sample.time-1.)
            if self.next_time < cutoff-1e-9:
                skipped = (cutoff-self.next_time)*self._time_rate
                if math.isfinite(skipped):
                    self._time_tick += max(0,math.ceil(skipped-1e-9))
                    self.next_time = self._time_origin+self._time_tick/self._time_rate
                else:
                    # Timestamps beyond representable elapsed time cannot be
                    # interpolated. Resume the clock after this discontinuity.
                    self.next_time = math.nextafter(sample.time,math.inf)
        end_distance = self.distance+length
        timed_count = 0
        while True:
            has_distance = length>1e-9 and self.next_distance <= end_distance+1e-9
            has_time = (dt>0 and self.next_time <= sample.time+1e-9
                        and timed_count <= math.ceil(self._time_rate))
            if not has_distance and not has_time:
                break
            distance_t = (clamp((self.next_distance-self.distance)/length)
                          if has_distance else math.inf)
            time_t = clamp((self.next_time-last.time)/dt) if has_time else math.inf
            same = (has_distance and has_time
                    and abs(distance_t-time_t)*length <= 1e-9
                    and abs(distance_t-time_t)*dt <= 1e-9)
            take_distance = has_distance and (same or distance_t < time_t)
            take_time = has_time and not take_distance
            t = distance_t if take_distance else time_t
            at = interpolate(last,sample,t)
            if take_time or same:
                at = replace(at,time=self.next_time)
            distance = self.next_distance if take_distance else self.distance+length*t
            if self.initial is not None:
                # A direction-sensitive click waits for the first movement or
                # clock tick; commit it once before any later timed paint.
                self._emit(self.initial,0,0)
                self.next_distance = self._next_spacing(self.initial,0.)
                self.initial = None
                direction = self.angle = 0.
            self._emit(at,distance,direction)
            if take_distance:
                self.next_distance += self._next_spacing(at,self.next_distance)
            if take_time:
                timed_count += 1
                self._time_tick += 1
                next_time = self._time_origin+self._time_tick/self._time_rate
                self.next_time = (next_time if next_time>self.next_time
                                  else math.nextafter(self.next_time,math.inf))
        self.distance = end_distance
        if length>1e-9:
            self.angle = direction

    def add(self,sample: BrushInput) -> list[BrushDab]:
        if self.finished:
            raise RuntimeError("Brush stroke already finished")
        if self.last is None:
            return self.begin(sample)
        sample = sample.sanitized()
        last = self.last
        if self.continuous_enabled and sample.time < last.time:
            sample = replace(sample,time=last.time)
        dx,dy = sample.x-last.x,sample.y-last.y
        length = math.hypot(dx,dy)
        if self.definition.stabilization and length>0:
            follow = 1-math.exp(-length/max(.01,30*self.definition.stabilization))
            sample = replace(sample,x=last.x+dx*follow,y=last.y+dy*follow)
            dx,dy = sample.x-last.x,sample.y-last.y
            length = math.hypot(dx,dy)
        dt = sample.time-last.time
        if dt>1e-6:
            self.velocity = length/dt
        direction = math.degrees(math.atan2(dy,dx)) if length>1e-9 else self.angle
        if self.initial is not None and length>1e-9:
            self._emit(self.initial,0,direction)
            self.next_distance = self._next_spacing(self.initial,0.)
            self.initial = None
        if self.continuous_enabled:
            self._continuous_segment(last,sample,length,direction)
        elif length>1e-9:
            end_distance = self.distance+length
            # Worst-case sampling is bounded to four samples per document pixel.
            while self.next_distance <= end_distance+1e-9:
                at = interpolate(last,sample,clamp((self.next_distance-self.distance)/length))
                self._emit(at,self.next_distance,direction)
                self.next_distance += self._next_spacing(at,self.next_distance)
            self.distance = end_distance
            self.angle = direction
            self.next_time = sample.time+1/max(1,self.definition.continuous_rate)
        self.last = sample
        return self._drain()

    def finish(self) -> list[BrushDab]:
        if self.finished:
            return []
        self.finished = True
        if self.initial is not None:
            self._emit(self.initial,0,0)
            self.initial = None
        elif self.definition.ribbon and self.last is not None and self.last_emitted is not None:
            if math.hypot(self.last.x-self.last_emitted.x,self.last.y-self.last_emitted.y)>1e-6:
                self._emit(self.last,self.distance,self.angle)
        return self._drain(final=True)
