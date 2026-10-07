"""Yield cold original-image decoding in explicitly deferred exact captures.

This source edge uses ImageStore's original decoders and ordinary rendering
after the owner-thread handoff. Floating documents enter their explicit
working-color contract from native source pixels. Source/effect sampling grids,
artwork keys, legacy decoding and the synchronous ImageStore APIs are preserved.
"""
from PySide6.QtGui import QImage, QImageReader

from comic_editor.core.images import ImageStore
from comic_editor.ui.async_projection import (
    ProjectionPending, ProjectionFailed, projection_deferred, projection_result_or_pending,
)


def _stamp(path):
    stat = path.stat()
    return stat.st_dev, stat.st_ino, stat.st_size, stat.st_mtime_ns


def _decode_memory(encoded, *, bytes_per_pixel=32, size=None):
    """Read only the file header; reserve the native and display decode peak."""
    if size is None:
        reader = QImageReader(str(encoded.pin.path))
        reader.setAutoTransform(True)
        size = reader.size()
        del reader
        width, height = size.width(), size.height()
    else:
        width, height = size
    if width <= 0 or height <= 0:
        # Match the existing decoder's Pillow fallback without decoding pixels.
        from PIL import Image
        with Image.open(encoded.pin.path) as image:
            width, height = image.size
    # Native Qt readers can return 16-byte float pixels before display-format
    # conversion. Include both frames, row/codec temporaries and encoded bytes;
    # the existing worker admission admits one oversized decode exclusively.
    return max(1, width) * max(1, height) * bytes_per_pixel + encoded.pin.path.stat().st_size


def _discard_handoff(canvas, scope, key, *, retained=True):
    if retained:
        canvas._effect_jobs.retained_remove(('result', scope), key)
    previous = canvas._modifier_render_cache.pop(key, None)
    if previous is not None:
        canvas._modifier_render_cache_bytes -= int(previous.sizeInBytes())


def image_for_render(canvas, object_id):
    from comic_editor.render.pixels import current_contract
    contract = current_contract()
    if (contract.floating and canvas.chapter is not None
            and canvas.chapter.pixel_contract.floating):
        return _working_image_for_render(canvas, object_id, contract)
    from comic_editor.ui.acquired_source_preview import cached_or_acquired
    preview = cached_or_acquired(canvas, object_id)
    return preview if preview is not None else _display_image_for_render(canvas, object_id)


def _navigator_source_deferred(canvas):
    """Only the Navigator's owned band capture opts into source handoffs."""
    return bool(getattr(canvas, '_navigator_defer_sources', False)
                and canvas._interactive_render
                and getattr(canvas, '_effect_preview_channel', 'canvas') == 'navigator')


def navigator_source_handoff_current(canvas, scope, key):
    """Validate only an already awaited original decode, without reading pixels."""
    store = canvas.images
    chapter = canvas.chapter
    if (chapter is None or not isinstance(scope, tuple) or len(scope) != 4
            or scope[:1] != ('source-image-decode',)
            or scope[1:3] != (id(chapter), id(store))
            or not isinstance(key, tuple) or len(key) not in (5, 6)
            or key[:1] != ('source-image-decode-preview',)):
        return False
    source = store.source(scope[3])
    if (source is None or id(source._encoded) != key[1]
            or store._decode_generation != key[2]):
        return False
    context = (id(chapter), id(store), getattr(canvas, '_history_generation', 0))
    if len(key) == 6:
        contract = chapter.pixel_contract
        if not contract.floating or key[5] != 'native-working':
            return False
        context += (_working_representation(contract), contract.signature)
    if key[3] != context:
        return False
    try:
        return _stamp(source._encoded.pin.path) == key[4]
    except OSError:
        return False


def _display_image_for_render(canvas, object_id):
    store = canvas.images
    from comic_editor.ui.thumbnail_effects import live_effect_draft
    if (not projection_deferred(canvas) and not live_effect_draft(canvas)
            and not _navigator_source_deferred(canvas)):
        return store.image(object_id)
    context = (id(canvas.chapter), id(store), getattr(canvas, '_history_generation', 0))
    scope = ('source-image-decode', *context[:2], str(object_id))
    cached = store.cached_image(object_id)
    if cached is not None:
        handoff = canvas._effect_jobs.retained.get(('result', scope))
        if handoff is not None and handoff[0][:1] == ('source-image-decode-preview',):
            _discard_handoff(canvas, scope, handoff[0])
        return cached
    source = store.source(object_id)
    if source is None:
        return QImage()
    encoded, generation = source._encoded, store._decode_generation
    try:
        stamp = _stamp(encoded.pin.path)
    except OSError as error:
        raise ProjectionFailed(scope, None, str(error)) from error
    # This is a transient handoff of original pixels, not derived artwork.
    # The preview token also excludes every alias of it from disk recording.
    key = ('source-image-decode-preview', id(encoded), generation, context, stamp)
    completed = projection_result_or_pending(canvas, scope, key)
    if completed is not None:
        current = (id(canvas.chapter), id(canvas.images), getattr(canvas, '_history_generation', 0))
        try:
            adopted = (current == context and _stamp(encoded.pin.path) == stamp
                       and store.adopt_decoded(object_id, encoded, generation, completed, source_stamp=stamp))
        except OSError:
            adopted = False
        # A frame above the store's residency budget keeps the existing bounded
        # worker handoff, rather than decoding again for every scene tile.
        _discard_handoff(canvas, scope, key,
                         retained=not adopted or store.cached_image(object_id) is not None)
        if not adopted:
            # A result-ready signal can reenter the editor before this handoff.
            # The current capture must retry its new source/context instead.
            raise ProjectionPending(scope, key)
        return QImage(completed)

    def compute(cancelled=None):
        if cancelled is not None and cancelled():
            return None
        if _stamp(encoded.pin.path) != stamp:
            return None
        decoded, _detected = ImageStore._decode(encoded.data)
        if (cancelled is not None and cancelled()) or _stamp(encoded.pin.path) != stamp:
            return None
        return decoded

    try:
        memory = _decode_memory(encoded)
    except (OSError, ValueError) as error:
        raise ProjectionFailed(scope, key, str(error)) from error
    canvas._effect_jobs.request(scope, key, compute, memory,
                                allow_oversized=True, require_exact=True)
    raise ProjectionPending(scope, key)


def _working_representation(contract):
    from comic_editor.render.pixels import working_representation
    # The resolved configuration identity prevents a newly loaded color
    # configuration from reusing a preceding conversion under the same path.
    return working_representation(contract)


def _working_image_for_render(canvas, object_id, contract):
    """Use original source pixels and apply the explicit working-color edge.

    Decode/conversion share the existing latest-request worker and bounded
    ImageStore cache. Handoffs remain transient; downstream exact source keys
    continue to describe the immutable original and the document contract.
    """
    from comic_editor.render.pixels import import_image
    from comic_editor.ui.thumbnail_effects import live_effect_draft
    store = canvas.images
    source = store.source(object_id)
    if source is None:
        return QImage()
    representation = _working_representation(contract)
    context = (id(canvas.chapter), id(store), getattr(canvas, '_history_generation', 0),
               representation, canvas.chapter.pixel_contract.signature)
    scope = ('source-image-decode', *context[:2], str(object_id))
    cached = store.cached_working_image(object_id, representation)
    if cached is not None:
        handoff = canvas._effect_jobs.retained.get(('result', scope))
        if handoff is not None and handoff[0][:1] == ('source-image-decode-preview',):
            _discard_handoff(canvas, scope, handoff[0])
        return cached
    native = store.cached_native_image(object_id)
    if (not projection_deferred(canvas) and not live_effect_draft(canvas)
            and not _navigator_source_deferred(canvas)):
        native = native if native is not None else store.native_image(object_id)
        working = import_image(native, contract)
        store.cache_working_image(object_id, representation, working)
        return working
    encoded, generation = source._encoded, store._decode_generation
    try:
        stamp = _stamp(encoded.pin.path)
    except OSError as error:
        raise ProjectionFailed(scope, None, str(error)) from error
    key = ('source-image-decode-preview', id(encoded), generation, context, stamp, 'native-working')
    completed = projection_result_or_pending(canvas, scope, key)
    if completed is not None:
        current = (id(canvas.chapter), id(canvas.images), getattr(canvas, '_history_generation', 0),
                   _working_representation(contract),
                   canvas.chapter.pixel_contract.signature if canvas.chapter is not None else None)
        try:
            adopted = (current == context and _stamp(encoded.pin.path) == stamp
                       and completed.format() == contract.image_format
                       and store.adopt_working_image(object_id, encoded, generation, representation, completed))
        except OSError:
            adopted = False
        _discard_handoff(canvas, scope, key,
                         retained=not adopted or store.cached_working_image(object_id, representation) is not None)
        if not adopted:
            raise ProjectionPending(scope, key)
        return QImage(completed)

    def compute(cancelled=None):
        if cancelled is not None and cancelled():
            return None
        decoded = native
        if decoded is None:
            decoded, _detected = ImageStore._decode_native(encoded.data)
        if cancelled is not None and cancelled():
            return None
        working = import_image(decoded, contract)
        if cancelled is not None and cancelled():
            return None
        return working

    try:
        # Cover native codec data, integer normalization, ICC intermediates,
        # straight/premultiplied float arrays and the owned working result.
        size = (native.width(), native.height()) if native is not None else None
        memory = _decode_memory(encoded, bytes_per_pixel=128, size=size)
    except (OSError, ValueError) as error:
        raise ProjectionFailed(scope, key, str(error)) from error
    canvas._effect_jobs.request(scope, key, compute, memory,
                                allow_oversized=True, require_exact=True)
    raise ProjectionPending(scope, key)
