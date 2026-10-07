"""Exact, frame-addressed mesh output reused across document requests."""
import math
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QPainter

from comic_editor.ui.effect_regions import TILE_SIZE, _tiles, projection_requires_exact


def _assembly_context(canvas):
    # Match tile_output's context: both paths share EffectJobs' single private
    # region pool. A mesh-specific pool context would invalidate its neighbors.
    from comic_editor.render.pixels import current_contract
    chapter = getattr(canvas, 'chapter', None)
    projection = getattr(canvas, '_document_projection', None)
    contract = getattr(chapter, 'pixel_contract', None)
    return (id(chapter), id(getattr(canvas, 'tiles', None)),
            id(getattr(canvas, 'images', None)),
            getattr(canvas, '_history_generation', 0),
            getattr(projection, 'revision', 0), current_contract().signature,
            getattr(contract, 'signature', None))


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
    from comic_editor.ui.async_projection import projection_deferred, ProjectionPending
    from comic_editor.render.pixels import current_contract
    from comic_editor.ui.distort_rendering import _MAX_PIXELS

    if not projection_requires_exact(canvas):
        raise ValueError("Mesh regions require exact projection rendering")
    frame, target = QRectF(frame), QRectF(target)
    if (frame != QRectF(frame.toAlignedRect())
            or target != QRectF(target.toAlignedRect())):
        raise ValueError("Mesh regions require native aligned output bounds")
    requested = frame.intersected(target)
    if requested.isEmpty():
        return empty_image(target), False
    coverage = list(_tiles(frame, requested))
    jobs = canvas._effect_jobs
    # One native frame shares already computed canonical pixels across
    # overlapping requests. Its pixels AND address metadata must fit the
    # unchanged pool budget; large stages keep per-request assembly instead.
    frame_count = ((math.ceil(frame.right() / TILE_SIZE) - math.floor(frame.left() / TILE_SIZE))
                   * (math.ceil(frame.bottom() / TILE_SIZE) - math.floor(frame.top() / TILE_SIZE)))
    depth = QImage(1, 1, current_contract().image_format).depth()
    frame_bytes = ((int(frame.width()) * depth + 31) // 32) * 4 * int(frame.height())
    shared = (projection_deferred(canvas) and frame.width() * frame.height() <= _MAX_PIXELS
              and frame_bytes + 256 + 192 * frame_count <= jobs.retained_budget)
    store = (jobs.private_regions(_assembly_context(canvas), lambda: _assembly_context(canvas))
             if shared or projection_deferred(canvas) and len(coverage) > 1 else None)
    assembly_bounds = frame if shared else target
    semantic_key = ((*stage_key[:3], canvas._rect_signature(frame), *stage_key[4:])
                    if shared else stage_key)
    assembly_key = ('mesh-frame-assembly' if shared else 'mesh-region-assembly',
                    stage_base_scope, semantic_key, tuple(frame.getRect()),
                    tuple(assembly_bounds.getRect()),
                    current_contract().signature)

    def shared_output(entry):
        image = store.crop(assembly_key, entry, requested)
        if requested == target:
            return image, False
        result = empty_image(target)
        painter = QPainter(result)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        try:
            painter.drawImage(requested.topLeft() - target.topLeft(), image)
        finally:
            painter.end()
        return result, False

    if store is not None:
        entry = store.get(assembly_key)
        if entry is not None and entry.complete:
            if shared:
                return shared_output(entry)
            return store.finish(assembly_key, entry), False

    def render_tile(address, tile_bounds):
        key = (*stage_key[:3], canvas._rect_signature(tile_bounds), *stage_key[4:])
        scope = ("mesh-region", stage_base_scope, address)
        retained = jobs.retained_get(scope, key)
        if retained is not None:
            return retained[0], False
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
        mask_pending = revision != getattr(canvas, "_effect_provisional_revision", 0)
        tile, unfinished = render_distort_stage(
            canvas, image, base, bounds, tile_bounds, modifier,
            local_to_world, fields, key, scope, mask_pending, False)
        unfinished |= mask_pending
        if not unfinished:
            jobs.retained_put(scope, key, tile)
        return tile, unfinished

    if store is not None:
        # Copy every exact tile before advancing to a dependency that may be
        # Pending. Ordinary retained-tile eviction can then leave this request
        # intact. Partial working pixels remain private and never reach caches,
        # callers or the disk backing as a completed result.
        for address, tile_bounds in coverage:
            entry = store.get(assembly_key)
            if entry is not None and address in entry.covered:
                continue
            tile, unfinished = render_tile(address, tile_bounds)
            store.validate()
            if unfinished:
                raise ProjectionPending('mesh-region-assembly', assembly_key)
            if entry is None or not store.current(assembly_key, entry):
                entry = store.begin(assembly_key, assembly_bounds,
                    current_contract().image_format, frame_count if shared else len(coverage),
                    tile_size=TILE_SIZE if shared else None)
            painter = QPainter(entry.image)
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            try:
                painter.drawImage(tile_bounds.topLeft() - assembly_bounds.topLeft(), tile)
            finally:
                painter.end()
            entry.covered.add(address)
        entry = store.get(assembly_key)
        if entry is None or any(address not in entry.covered for address, _ in coverage):
            raise ProjectionPending('displaced-mesh-region-assembly', assembly_key)
        if shared:
            if len(entry.covered) == frame_count:
                store.finish(assembly_key, entry)
            return shared_output(entry)
        return store.finish(assembly_key, entry), False

    result = empty_image(target)
    provisional = False
    painter = QPainter(result)
    try:
        for address, tile_bounds in coverage:
            tile, unfinished = render_tile(address, tile_bounds)
            provisional |= unfinished
            painter.drawImage(tile_bounds.topLeft() - target.topLeft(), tile)
    finally:
        painter.end()
    return result, provisional
