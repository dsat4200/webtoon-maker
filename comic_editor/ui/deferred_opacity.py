"""Yield a native paint-only opacity output without reading live worker state."""
import math

import numpy as np
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.render.pixels import current_contract
from comic_editor.ui.async_projection import ProjectionPending, projection_result_or_pending
from comic_editor.ui.mask_paint import add_paint
from comic_editor.ui.modifier_rendering import apply_opacity_mask


MIN_PIXELS = 256 * 256


def _context(canvas):
    return (id(canvas.chapter), id(canvas.tiles), id(canvas.images),
            getattr(canvas, '_history_generation', 0), canvas._document_projection.revision,
            current_contract().signature, canvas.chapter.pixel_contract.signature)


def paint_only_output(canvas, image, bounds, mapping, binding, key, *, target=None):
    """Return None for unsupported sources; every admitted source is exact."""
    width, height = image.width(), image.height()
    mask = canvas.chapter.masks.get(binding.mask_id) if canvas.chapter else None
    if (mask is None or width * height < MIN_PIXELS or mask.contributors
            or mask.gradient is not None or mask.limited_gradients):
        return None
    from comic_editor.ui.attached_translation import effective_preview_mask
    mask = effective_preview_mask(canvas, mask)
    context = _context(canvas)
    signature = canvas._tone_mask_signature(binding.mask_id)
    endpoints = float(binding.black_value), float(binding.white_value)
    source_key = int(image.cacheKey())
    kind = 'layer' if target is not None and hasattr(target, 'layer_id') else 'object'
    identifier = (target.layer_id if kind == 'layer' else target.object_id) if target is not None else mask.mask_id
    geometry = tuple(bounds.getRect()), width, height
    scope = (*canvas._effect_request_scope(kind, identifier), 'opacity-output', context, geometry)
    # The handoff is not durable artwork. Only the ordinary semantic mask key
    # receives completed pixels after owner-thread context checks below.
    handoff = ('opacity-output-preview-handoff', context, key)
    completed = projection_result_or_pending(canvas, scope, handoff)
    if completed is not None:
        if (context != _context(canvas) or signature != canvas._tone_mask_signature(binding.mask_id)
                or endpoints != (float(binding.black_value), float(binding.white_value))
                or source_key != int(image.cacheKey())):
            raise ProjectionPending(scope, handoff)
        previous = canvas._modifier_render_cache.pop(handoff, None)
        if previous is not None:
            canvas._modifier_render_cache_bytes -= int(previous.sizeInBytes())
        return completed

    world_to_image = canvas._world_to_image_transform(mapping, bounds, width, height)
    visible = mapping.mapRect(bounds)
    offset, signed = tuple(mask.paint_offset), bool(mask.paint_has_subtractions)
    rectangle = visible.translated(-offset[0], -offset[1])
    left, right = math.floor(rectangle.left() / canvas.tiles.tile_size), math.floor(rectangle.right() / canvas.tiles.tile_size)
    top, bottom = math.floor(rectangle.top() / canvas.tiles.tile_size), math.floor(rectangle.bottom() / canvas.tiles.tile_size)
    owner = canvas.tiles._tiles.get(mask.mask_id, {})
    addresses = {address for address in owner if left <= address[0] <= right and top <= address[1] <= bottom}
    # No cold tile is decoded and no full field is allocated on the document
    # thread. Pins own unchanged files; Qt COW/frozen tiles own edited pixels.
    snapshot = canvas.tiles.detached_snapshot({mask.mask_id}, wait_for_prefetch=False,
                                              selected_keys={mask.mask_id: addresses})
    source, mask_id = QImage(image), str(mask.mask_id)
    transform, world = QTransform(world_to_image), QRectF(visible)
    add_alpha, signed_paint = type(canvas)._add_image_alpha_to_field, type(canvas)._signed_mask_paint
    contract = current_contract()

    def compute(cancelled):
        if cancelled():
            return None
        field = np.zeros((height, width), dtype=np.float32)
        if not add_paint(field, snapshot, mask_id, width, height, transform, world,
                         offset, signed, add_alpha, signed_paint, cancelled):
            return None
        np.clip(field, 0., 1., out=field)
        if cancelled():
            return None
        result = apply_opacity_mask(source, field, *endpoints)
        return None if cancelled() else result

    # Include pinned source, paint, field, output, and native float conversion
    # peaks. The same worker admission grants oversized outputs exclusivity.
    frame_bytes = width * height * (72 if contract.floating else 20)
    memory = int(source.sizeInBytes()) + frame_bytes + len(addresses) * snapshot.tile_size ** 2 * 4
    canvas._effect_jobs.request(scope, handoff, compute, memory,
                                allow_oversized=True, require_exact=True)
    raise ProjectionPending(scope, handoff)
