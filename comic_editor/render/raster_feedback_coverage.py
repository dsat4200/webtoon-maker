"""Native coverage of current selected pixels during an owned raster contact.

A bounded feedback preparation need not cover unchanged artwork outside the
contact.  This guard proves that every visible changed native source footprint
has a prepared patch instead.  It does not render, decode, sample, or publish
anything.  The caller still owns typed dependency and contact-lifetime guards.
"""
import math

from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.render.raster_feedback import _feedback_projective_rect_supported


_NO_SOURCE_OWNER = object()


def feedback_contact_covers(prepared, document, visible, dirty_keys, *,
                            presentation_margin=0., source_owner=_NO_SOURCE_OWNER):
    """Prove coverage of all visible changed source tiles, including gutters.

    ``visible`` and ``presentation_margin`` are in canvas pixels.  The latter
    may account for one physical presentation-filter pixel at the current zoom;
    it never changes native patch or source resolution.  Passing ``source_owner``
    additionally proves that contributing changed tiles are resident or pruned,
    without touching the backing store.  Explicit ``None`` is unavailable.

    Older prepared revisions are intentional: the controller must have retained
    this owner only across positively validated selected-pixel changes, and must
    pass the complete dirty-address journal since its source capture.
    """
    if (prepared is None or not prepared.tiles or source_owner is None
            or prepared.document.identity != document.identity
            or prepared.document.configuration != document.configuration
            or prepared.document.pixel_contract != document.pixel_contract
            or prepared.document.bounds != document.bounds):
        return False
    try:
        side, world_side, gutter = prepared.tile_size, prepared.world_size, prepared.gutter
        origin = prepared.origin
        if (not isinstance(side, int) or not isinstance(world_side, int)
                or side <= 0 or world_side <= 0 or gutter < 0
                or len(origin) != 2 or not all(math.isfinite(value) for value in origin)
                or not math.isfinite(presentation_margin) or presentation_margin < 0.):
            return False
        dirty = set(dirty_keys)
        if (not dirty or any(not isinstance(key, tuple) or len(key) != 2
                or any(type(value) is not int for value in key) for key in dirty)):
            return False
        viewport = QRectF(*visible)
        if (not all(math.isfinite(value) for value in viewport.getRect())
                or viewport.width() <= 0. or viewport.height() <= 0.):
            return False
        viewport = viewport.adjusted(-presentation_margin, -presentation_margin,
            presentation_margin, presentation_margin).intersected(document.bounds)
        if viewport.isEmpty():
            return False
        mapped = prepared.source_transform is not None
        transform = (QTransform(*prepared.source_transform) if mapped
            else QTransform.fromTranslate(*origin))
        inverse, invertible = transform.inverted()
        if (not invertible or not all(math.isfinite(value) for value in
                (transform.m11(), transform.m12(), transform.m13(), transform.m21(),
                 transform.m22(), transform.m23(), transform.m31(), transform.m32(), transform.m33()))):
            return False
        tiles = {tile.key: tile for tile in prepared.tiles}
        if len(tiles) != len(prepared.tiles):
            return False
        # The prepared images retain two native gutter pixels.  Include their
        # neighboring core patches as well: a changed edge may be sampled by a
        # smooth display draw on either side of a native patch boundary.
        halo = max(1., float(gutter))
        required = set()
        for source_key in dirty:
            local = QRectF(source_key[0] * side, source_key[1] * side, side, side)
            if (not all(math.isfinite(value) for value in local.getRect())
                    or not _feedback_projective_rect_supported(transform, local)):
                return False
            footprint = transform.mapRect(local)
            if not all(math.isfinite(value) for value in footprint.getRect()):
                return False
            footprint = footprint.adjusted(-halo, -halo, halo, halo).intersected(viewport)
            if footprint.isEmpty():
                continue
            if not _feedback_projective_rect_supported(inverse, footprint):
                return False
            if source_owner is not _NO_SOURCE_OWNER:
                if (source_key in source_owner.entries
                        and (source_owner, source_key) not in source_owner.residency.entries):
                    return False
            left = math.floor((footprint.left() - origin[0]) / world_side)
            top = math.floor((footprint.top() - origin[1]) / world_side)
            right = math.ceil((footprint.right() - origin[0]) / world_side)
            bottom = math.ceil((footprint.bottom() - origin[1]) / world_side)
            # Bound address work before constructing a range for an extreme
            # transform, long stroke or tiny source-to-world scale.
            if (right - left) * (bottom - top) > len(tiles):
                return False
            for y in range(top, bottom):
                for x in range(left, right):
                    key = x, y
                    tile = tiles.get(key)
                    if tile is None:
                        return False
                    expected = (origin[0] + x * world_side - gutter,
                        origin[1] + y * world_side - gutter,
                        world_side + 2 * gutter, world_side + 2 * gutter)
                    if tuple(tile.bounds) != expected:
                        return False
                    if mapped and source_key not in tile.source_keys:
                        return False
                    if (tile.prefix.isNull() or tile.suffix.isNull()
                            or prepared.source_images is None and tile.source.isNull()):
                        return False
                    required.add(key)
                    if len(required) > len(tiles):
                        return False
        return True
    except (AttributeError, TypeError, ValueError, OverflowError):
        # Unknown ownership/geometry is never evidence of current artwork.
        return False
