"""Resolve view-dependent brush choices into a fixed, portable stroke snapshot.

The caller supplies a local-to-view scale captured at pen-down. The raster
engine and saved brush never depend on a camera or Qt widget.
"""
from __future__ import annotations

import math
from dataclasses import replace

from .brushes import BrushDefinition


def resolve_brush_view_size(brush: BrushDefinition, local_to_view_scale: float) -> BrushDefinition:
    """Resolve screen size once; linked secondary size follows the same ratio.

    Independent secondary sizes stay in raster-local pixels: CSP has no
    separate screen-size flag for the secondary brush. At scale 1 the size is
    unchanged, which is the preview convention. Nonuniform transforms use the
    caller's equal-area scale, preserving their existing anisotropy.
    """
    if not brush.size_by_view:
        return brush
    scale = float(local_to_view_scale)
    if not math.isfinite(scale) or scale <= 1e-8:
        scale = 1.0
    factor = 1.0 / scale
    dual = brush.dual
    if dual is not None and brush.dual_link_size and factor != 1:
        dual = replace(dual, size=dual.size * factor)
    return replace(brush, size=brush.size * factor, size_by_view=False, dual=dual)
