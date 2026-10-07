"""Keep only bounded deterministic byte-image drafts through metadata history.

These are unchanged entries in the ordinary modifier LRU. They remain
provisional, retain their complete semantic keys, and never enter a source,
prepared, retained-worker or durable cache.
"""
from PySide6.QtGui import QImage

from comic_editor.core.models import ImageObject
from comic_editor.core.pixel_contract import LEGACY_PIXELS


def _context(canvas):
    chapter = canvas.chapter
    if chapter is None or chapter.pixel_contract != LEGACY_PIXELS:
        return None
    return (id(chapter), chapter.chapter_id, id(canvas.tiles), id(canvas.images),
            chapter.pixel_contract.signature, chapter.width, chapter.height)


def _original_image(canvas, key):
    if (not isinstance(key, tuple) or len(key) != 3
            or key[:1] != ('live-effect-draft-stage',)
            or key[1] != LEGACY_PIXELS.signature):
        return None
    stage = key[2]
    if (not isinstance(stage, tuple) or len(stage) < 2
            or stage[0] != 'stage'):
        return None
    upstream = stage[1]
    if (not isinstance(upstream, tuple) or len(upstream) < 2
            or upstream[0] != 'stage-input'):
        return None
    source = upstream[1]
    if (not isinstance(source, tuple) or len(source) != 3
            or source[0] != 'live-effect-draft-source'):
        return None
    original = source[2]
    if (not isinstance(original, tuple) or len(original) < 3
            or original[:2] != ('mirror-source', 'object')):
        return None
    obj = canvas.chapter.objects.get(original[2])
    if not isinstance(obj, ImageObject) or obj.placement_mode == 'fit_parent':
        return None
    return obj


def _limit(canvas):
    return min(8 * 1024 * 1024, max(0, canvas._modifier_render_cache_budget // 4))


def snapshot(canvas):
    """Copy newest eligible handles before a focused history restore clears LRUs."""
    context = _context(canvas)
    if context is None:
        return None
    remaining, records = _limit(canvas), []
    for key in reversed(canvas._modifier_render_cache):
        if _original_image(canvas, key) is None:
            continue
        image = canvas._modifier_render_cache[key]
        size = int(image.sizeInBytes())
        if image.isNull() or image.format() != LEGACY_PIXELS.image_format or size > remaining:
            continue
        records.append((key, QImage(image)))
        remaining -= size
        if len(records) == 64:
            break
    return context, getattr(canvas, '_history_generation', 0) + 1, records


def restore(canvas, saved):
    """Reinsert unchanged keys after all synchronous restore notifications."""
    if saved is None:
        return
    context, generation, records = saved
    if (_context(canvas) != context
            or getattr(canvas, '_history_generation', 0) != generation):
        return
    # A restore callback may change the budget. Keep the same cap at admission,
    # and preserve the previous relative LRU order of the selected records.
    remaining, selected = _limit(canvas), []
    for key, image in records:
        size = int(image.sizeInBytes())
        if size <= remaining and _original_image(canvas, key) is not None:
            selected.append((key, image))
            remaining -= size
    for key, image in reversed(selected):
        if key not in canvas._modifier_render_cache:
            canvas._modifier_cache_put(key, image)
