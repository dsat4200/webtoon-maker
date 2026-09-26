"""Exact, frame-addressed mesh output reused across document requests."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QPainter

from comic_editor.ui.effect_regions import _tiles, projection_requires_exact


def render_mesh_regions(canvas, image, bounds, frame, target, modifier,
                        local_to_world, stage_key, stage_base_scope):
    """Assemble native mesh pixels without making the viewport a dependency.

    The caller supplies the aligned complete stage frame and aligned requested
    target. The original source, placement, modifier frame, sampling settings,
    and mask dependencies remain in ``stage_key``; only its output rectangle is
    replaced with each canonical tile. All retained pixels share EffectJobs'
    existing global byte/count budget, with no second unbounded image cache.
    """
    from comic_editor.ui.distort_pipeline import render_distort_stage
    from comic_editor.ui.effect_pipeline import empty_image

    if not projection_requires_exact(canvas):
        raise ValueError("Mesh regions require exact projection rendering")
    frame, target = QRectF(frame), QRectF(target)
    if (frame != QRectF(frame.toAlignedRect())
            or target != QRectF(target.toAlignedRect())):
        raise ValueError("Mesh regions require native aligned output bounds")
    result = empty_image(target)
    requested = frame.intersected(target)
    if requested.isEmpty():
        return result, False
    provisional = False
    painter = QPainter(result)
    try:
        for address, tile_bounds in _tiles(frame, requested):
            key = (*stage_key[:3], canvas._rect_signature(tile_bounds), *stage_key[4:])
            scope = ("mesh-region", stage_base_scope, address)
            retained = canvas._effect_jobs.retained_get(scope, key)
            if retained is not None:
                tile = retained[0]
            else:
                base = empty_image(tile_bounds)
                base_painter = QPainter(base)
                try:
                    base_painter.drawImage(bounds.topLeft() - tile_bounds.topLeft(), image)
                finally:
                    base_painter.end()
                mapping = canvas._world_to_image_transform(
                    local_to_world, tile_bounds, base.width(), base.height())
                revision = getattr(canvas, "_effect_provisional_revision", 0)
                fields = canvas._modifier_mask_fields(
                    [modifier], base.width(), base.height(), mapping,
                    local_to_world.mapRect(tile_bounds))
                tile, unfinished = render_distort_stage(
                    canvas, image, base, bounds, tile_bounds, modifier,
                    local_to_world, fields, key, scope, False, False)
                unfinished |= revision != getattr(canvas, "_effect_provisional_revision", 0)
                provisional |= unfinished
                if not unfinished:
                    canvas._effect_jobs.retained_put(scope, key, tile)
            painter.drawImage(tile_bounds.topLeft() - target.topLeft(), tile)
    finally:
        painter.end()
    return result, provisional
