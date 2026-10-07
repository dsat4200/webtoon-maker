"""Fixed whole-source Legacy previews for explicitly owned widget presentation.

No decoder, exact renderer, source transformation, durable cache, or private
pixel pool belongs here. Original acquisition remains the ImageStore workflow.
"""
from contextlib import contextmanager
import math

from PySide6.QtCore import Qt, qVersion
from PySide6.QtGui import QImage

from comic_editor.render.pixels import current_contract
from comic_editor.render.source_context import source_color_context
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.thumbnail_effects import live_effect_draft


_MISSING = object()


@contextmanager
def presentation_scope(canvas, enabled):
    previous = getattr(canvas, '_acquired_preview_presentation_owner', _MISSING)
    canvas._acquired_preview_presentation_owner = bool(enabled)
    try:
        yield
    finally:
        if previous is _MISSING:
            del canvas._acquired_preview_presentation_owner
        else:
            canvas._acquired_preview_presentation_owner = previous


def widget_presentation(original):
    """Explicit raster-cache presentation also owns its QImage paint device."""
    def paint(canvas, painter, *, live_ink=False, interactive=False):
        owner = painter.device() is canvas or (interactive and bool(
            getattr(canvas, '_acquired_preview_presentation_owner', False)))
        with presentation_scope(canvas, owner):
            return original(canvas, painter, live_ink=live_ink, interactive=interactive)
    return paint


def context(canvas, object_id=None):
    if (not getattr(canvas, '_acquired_preview_presentation_owner', False)
            or not live_effect_draft(canvas)
            or getattr(canvas, '_rendering_compound_references', False)
            or getattr(canvas, '_rendering_outward_gradient', False)
            or getattr(canvas, '_posterize_statistics_capture', False)
            or canvas.chapter is None or canvas.chapter.pixel_contract.floating
            or current_contract().floating):
        return None
    object_id = object_id or canvas.selected_object_id
    if object_id != canvas.selected_object_id:
        return None
    obj = canvas.chapter.objects.get(object_id)
    if obj is None or obj.object_type != 'image' or obj.is_blender_linked:
        return None
    source = canvas.images.source(object_id)
    if source is None or obj.pixel_width <= 0 or obj.pixel_height <= 0:
        return None
    encoded = source._encoded
    try:
        stat = encoded.pin.path.stat()
        stamp = stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns
    except OSError as error:
        raise ProjectionFailed(('acquired-source-preview', object_id), None, str(error)) from error
    width, height = int(obj.pixel_width), int(obj.pixel_height)
    ratio = min(1., 256 / max(width, height), math.sqrt(32768 / (width * height)))
    extent = max(1, math.floor(width * ratio)), max(1, math.floor(height * ratio))
    token = ('acquired-source-preview', id(source), id(encoded), canvas.images._decode_generation,
        (id(canvas.chapter), id(canvas.images), getattr(canvas, '_history_generation', 0)), stamp,
        canvas.chapter.pixel_contract.signature, current_contract().signature,
        source_color_context(canvas.chapter.pixel_contract), qVersion(), (width, height), extent,
        'Qt.FastTransformation/whole-display-v1')
    key = ('acquired-source-preview', id(canvas), object_id, token)
    normal, retained = canvas.images._decoded.get(str(object_id)), canvas.images._decoded.get(key)
    expected = (id(encoded), canvas.images._decode_generation,
                int(normal.cacheKey()) if normal is not None else None, stamp)
    trusted = (normal is not None and canvas.images.display_decode_provenance(object_id) == expected
        and (normal.width(), normal.height()) == (width, height)
        and normal.format() == QImage.Format_ARGB32_Premultiplied
        and (retained is None or normal.colorSpace() == retained.colorSpace()))
    acquired = trusted or normal is None and retained is not None
    if not acquired:
        # The source/effect cache key must identify fallback BEFORE any source
        # lookup can reuse an acquired prefix. Trusted completions/normal LRU
        # eviction retain the stable acquired identity; untrusted frames do not.
        resident = None if normal is None else (int(normal.cacheKey()), normal.width(),
                                                normal.height(), normal.format().value)
        token += (('ordinary-source-fallback', resident),)
    return dict(object_id=object_id, encoded=encoded, stamp=stamp, native_extent=(width, height),
                extent=extent, token=token, key=key, acquired=bool(acquired))


def cached_or_acquired(canvas, object_id):
    """Return a COW preview, or None so ordinary acquisition keeps control."""
    current = context(canvas, object_id)
    if current is None:
        return None
    store, key = canvas.images, current['key']
    if not current['acquired']:
        for previous in tuple(store._decoded):
            if isinstance(previous, tuple) and previous[:3] == key[:3]:
                store._forget_decoded(previous)
        return None
    cached = store._decoded.get(key)
    normal = store.cached_image(object_id)
    if normal is not None:
        expected = (id(current['encoded']), store._decode_generation,
                    int(normal.cacheKey()), current['stamp'])
        trusted = (store.display_decode_provenance(object_id) == expected
            and (normal.width(), normal.height()) == current['native_extent']
            and normal.format() == QImage.Format_ARGB32_Premultiplied
            and (cached is None or normal.colorSpace() == cached.colorSpace()))
        if not trusted:
            # Generic/admitted frames and bounded metadata eviction cannot
            # manufacture a decode job. Use the existing ordinary route.
            for previous in tuple(store._decoded):
                if isinstance(previous, tuple) and previous[:3] == key[:3]:
                    store._forget_decoded(previous)
            return None
    if cached is not None:
        store._decoded.move_to_end(key)
        return QImage(cached)
    if normal is None:
        # No full native/display decode, thumbnail codec, or source warming.
        return None
    image = normal.scaled(*current['extent'], Qt.IgnoreAspectRatio, Qt.FastTransformation)
    if image.colorSpace() != normal.colorSpace():
        raise ProjectionFailed(('acquired-source-preview', object_id), current['token'],
                               'Preview changed the ordinary source color profile')
    renewed = context(canvas, object_id)
    if renewed is None or renewed['token'] != current['token']:
        raise ProjectionPending(('acquired-source-preview', object_id), current['token'])
    for previous in tuple(store._decoded):
        if isinstance(previous, tuple) and previous[:3] == key[:3] and previous != key:
            store._forget_decoded(previous)
    # Existing total decoded byte budget and ordinary eviction, not a new pool.
    store._cache_decoded(key, QImage(image))
    return QImage(image)
