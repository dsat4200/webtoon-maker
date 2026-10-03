"""Apply a completed effect's opacity mask only where it will be displayed."""
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.ui.modifier_rendering import apply_opacity_mask


def mask_output(canvas, image, bounds, mapping, binding, visible, painter, *, target=None, source_key=None):
    bounds = QRectF(bounds)
    original_key = int(image.cacheKey())
    crop = image.rect()
    # Intermediate captures must retain their complete source. Rotated and
    # projective QPainter coverage depends on the original image rectangle.
    viewport = (
        canvas._interactive_render and mapping.isAffine()
        and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
        and painter.combinedTransform().type().value <= QTransform.TransformationType.TxScale.value
        and not canvas._render_modifier_sources
        and not canvas._render_base_alpha
        and not canvas._rendering_mask_contributor
        and not canvas._render_cage_source
        and not getattr(canvas, "_rendering_halftone_source", False)
        and getattr(canvas, "_tiling_capture_geometry", None) is None
    )
    if viewport and not bounds.isEmpty():
        inverse, valid = painter.combinedTransform().inverted()
        if valid:
            guard = inverse.mapRect(QRectF(-2, -2, 4, 4))
            dx, dy = max(2., guard.width()), max(2., guard.height())
            needed = visible.adjusted(-dx, -dy, dx, dy).intersected(bounds)
            if needed.isEmpty():
                return None, bounds
            sx, sy = image.width() / bounds.width(), image.height() / bounds.height()
            crop = QRectF((needed.x() - bounds.x()) * sx,
                          (needed.y() - bounds.y()) * sy,
                          needed.width() * sx, needed.height() * sy).toAlignedRect()
            crop = crop.intersected(image.rect())
            bounds = QRectF(bounds.x() + crop.x() / sx, bounds.y() + crop.y() / sy,
                            crop.width() / sx, crop.height() / sy)
    key = ("placed-opacity-mask", original_key, tuple(crop.getRect()),
           tuple(bounds.getRect()), tuple(getattr(mapping, f"m{i}{j}")()
               for i in range(1, 4) for j in range(1, 4)),
           binding.black_value, binding.white_value,
           canvas._tone_mask_signature(binding.mask_id))
    from comic_editor.ui import translation_cache
    move_key = None
    if target is not None and source_key is not None:
        mask_key = translation_cache.output_key(canvas, target, bounds, mapping, [])
        if mask_key is not None:
            move_key = ("translated-opacity-output", source_key, mask_key)
    reused = translation_cache.get(canvas, move_key)
    if reused is not None:
        return reused, bounds
    cached = canvas._modifier_cache_get(key)
    if cached is not None:
        return cached, bounds
    if crop != image.rect():
        image = image.copy(crop)
    revision = getattr(canvas, "_effect_provisional_revision", 0)
    field = canvas.render_tone_mask_field(
        binding.mask_id, image.width(), image.height(),
        canvas._world_to_image_transform(mapping, bounds, image.width(), image.height()),
        mapping.mapRect(bounds),
    )
    result = apply_opacity_mask(image, field, binding.black_value, binding.white_value)
    if revision == getattr(canvas, "_effect_provisional_revision", 0):
        canvas._modifier_cache_put(key, result)
        translation_cache.put(canvas, move_key, result, revision)
    return result, bounds
