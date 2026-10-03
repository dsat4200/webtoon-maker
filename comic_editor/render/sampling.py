"""Artwork sampling is independent of camera zoom and screen pixel density.

Original source grids and effect precision remain unchanged. Display surfaces
and editing gizmos may use physical screen pixels; derived artwork may not gain
detail merely because the camera changes.
"""
import math


def artwork_density(requested=1.0):
    value = float(requested)
    return min(1.0, max(0.1, value)) if math.isfinite(value) else 1.0


NATIVE_DENSITY = 1.0
