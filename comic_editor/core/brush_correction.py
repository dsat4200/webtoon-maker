"""Portable, endpoint-preserving post-correction for brush input paths.

This is an independent arc-length Gaussian smoother, not a reconstruction of
CSP's undocumented correction algorithm. Sensor values and timestamps remain
attached to the original samples. Its resampling workspace is bounded.
"""
from __future__ import annotations

import math
from dataclasses import replace

import numpy as np
from scipy.ndimage import gaussian_filter1d

from .brushes import BrushInput, clamp


MAX_CORRECTION_GRID = 8193


def stroke_length(samples: list[BrushInput]) -> float:
    return math.fsum(math.hypot(b.x-a.x,b.y-a.y) for a,b in zip(samples,samples[1:]))


def corrected_samples(samples: list[BrushInput], strength: float) -> list[BrushInput]:
    """Smooth positions in distance space, preserving all input metadata.

    The input should already contain any live stabilization. Endpoints stay
    exact, and stationary samples remain present for continuous airbrush flow.
    At most 8193 resampling points are used regardless of path length.
    """
    strength=clamp(strength)
    if strength <= 0 or len(samples) < 3:
        return list(samples)
    points=np.asarray([(sample.x,sample.y) for sample in samples],dtype=np.float64)
    distances=np.concatenate(([0.],np.cumsum(np.linalg.norm(np.diff(points,axis=0),axis=1))))
    length=float(distances[-1])
    if not math.isfinite(length) or length <= 1e-9:
        return list(samples)
    count=min(MAX_CORRECTION_GRID,max(3,int(math.ceil(length))+1))
    uniform=np.linspace(0,length,count)
    path=np.stack([np.interp(uniform,distances,points[:,axis]) for axis in range(2)],axis=-1)
    sigma=min(count*.5,(.5+20*strength**1.5)/max(length/(count-1),1e-8))
    smoothed=gaussian_filter1d(path,sigma=sigma,axis=0,mode="nearest")
    # Remove endpoint drift without translating the whole line or changing its
    # time/pressure sequence. The correction is zero at both endpoint samples.
    weight=uniform/length
    smoothed+=(path[0]-smoothed[0])*(1-weight[:,None])+(path[-1]-smoothed[-1])*weight[:,None]
    output=np.stack([np.interp(distances,uniform,smoothed[:,axis]) for axis in range(2)],axis=-1)
    output=points+(output-points)*strength
    result=[replace(sample,x=float(point[0]),y=float(point[1])) for sample,point in zip(samples,output)]
    result[0],result[-1]=samples[0],samples[-1]
    return result


def resolved_taper(definition, length, *, _continuous_allowed=None):
    """Resolve percentage lengths once the completed, corrected path is known."""
    # The primary brush owns correction for both planes. Keep its continuous
    # gate after clearing post_correction for replay, without editing the preset.
    if _continuous_allowed is None:
        _continuous_allowed = definition.post_correction <= 0
    start,end=definition.taper_start,definition.taper_end
    if getattr(definition,"taper_mode","length") == "percentage":
        start=length*clamp(start,0,100)/100
        end=length*clamp(end,0,100)/100
    mode="fade" if getattr(definition,"taper_mode","length") == "fade" else "length"
    return replace(definition,post_correction=0.,taper_mode=mode,stabilization=0.,
                   continuous=definition.continuous and _continuous_allowed,
                   taper_start=start,taper_end=end,
                   dual=resolved_taper(definition.dual,length,_continuous_allowed=_continuous_allowed)
                   if definition.dual else None)
