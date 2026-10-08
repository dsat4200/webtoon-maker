"""Prepared native resources for presenting already painted raster contacts.

The ordinary detached kernels prepare the scene on either side of a simple
raster leaf. These provisional resources never enter an artwork/disk cache.
Source/effect grids are unchanged; only resident edited source tiles replace
the prepared source plane on the document thread.
"""
from dataclasses import dataclass, replace
import copy
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.models import RasterObject, GradientObject
from comic_editor.core.pixel_contract import LEGACY_PIXELS
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.render.service import DocumentRenderService, RenderRequest


@dataclass(frozen=True)
class RasterFeedbackTile:
    key: tuple
    bounds: tuple
    prefix: QImage
    source: QImage
    suffix: QImage
    source_keys: tuple = ()

    @property
    def byte_count(self):
        return sum(image.sizeInBytes() for image in (self.prefix, self.source, self.suffix))


@dataclass(frozen=True)
class RasterFeedback:
    document: object
    identifier: str
    origin: tuple
    tile_size: int
    gutter: int
    opacity: float
    clips: tuple
    tiles: tuple
    source_transform: tuple | None = None

    @property
    def byte_count(self):
        return sum(tile.byte_count for tile in self.tiles)


def _feedback_mapping_supported(entity):
    """Conservative native separability gate; never approximate a quad."""
    frame, quad = entity.transform_frame, entity.transform_quad
    if frame is None and quad is None:
        return True
    if frame is None or quad is None or len(frame) != 4 or len(quad) != 4:
        return False
    try:
        if not all(math.isfinite(value) for value in frame) or frame[2] <= 0 or frame[3] <= 0:
            return False
        if not all(len(point) == 2 and all(math.isfinite(value) for value in point) for point in quad):
            return False
        a, b, c, d = quad
        return (a[1] == b[1] and b[0] == c[0] and c[1] == d[1] and d[0] == a[0]
                and b[0] > a[0] and d[1] > a[1])
    except (TypeError, ValueError, OverflowError):
        return False


def _feedback_source_keys(bounds, inverse, side, limit=128):
    """Bound source metadata work before constructing a source-key set."""
    local = inverse.mapRect(QRectF(*bounds)).adjusted(-1., -1., 1., 1.)
    coordinates = local.left(), local.top(), local.right(), local.bottom()
    if not all(math.isfinite(value) for value in coordinates):
        return None
    left, top, right, bottom = (math.floor(value / side) for value in coordinates)
    if (right - left + 1) * (bottom - top + 1) > limit:
        return None
    return tuple((x, y) for y in range(top, bottom + 1) for x in range(left, right + 1))


def feedback_chain(snapshot, identifier):
    """Eligibility preserves scene order and separable source dependencies."""
    document, chapter = snapshot.document, snapshot.chapter
    obj = chapter.objects.get(identifier)
    if (not isinstance(obj, RasterObject) or not obj.visible or obj.mask_only
            or document.pixel_contract != LEGACY_PIXELS or document.overflow
            or document.underlay != ('', 0.)
            or obj.opacity_mask is not None or obj.modifier_ids
            or not _feedback_mapping_supported(obj)
            or obj.modifier_source_frame is not None or obj.ignore_parent_mask
            or obj.tile_size != snapshot.tiles.tile_size
            or obj.show_on_top
            or any(item.blend_mode != 'normal'
                   for item in chapter.objects.values())
            ):
        return None
    chain, parent = [], obj.parent_layer_id
    while parent:
        layer = chapter.layers.get(parent)
        if (layer is None or not layer.visible or layer.mask_only
                or layer.opacity != 1. or layer.opacity_mask is not None
                or layer.modifier_ids or layer.compound_enabled
                or layer.show_on_top
                or layer.layer_kind != 'bounded' or layer.ignore_parent_mask
                or not _feedback_mapping_supported(layer)
                or layer.bound is None):
            return None
        # These children have an extra pass outside the ordinary child order.
        for ref in layer.children:
            child = (chapter.layers if ref.kind == 'layer' else chapter.objects).get(ref.entity_id)
            if (child is None or child.ignore_parent_mask or
                    isinstance(child, GradientObject) and SceneKernels._is_outward_gradient(child)):
                return None
        chain.append(layer)
        parent = layer.parent_id
    if not chain or chain[-1].layer_id not in chapter.root_page_ids:
        return None
    ancestors = {layer.layer_id for layer in chain}
    # A linked mask or target-layer sampler makes other prepared scene pixels
    # depend on this source. Those graphs need ordinary detached reevaluation.
    if (any(kind == 'object' and target == identifier or kind == 'layer' and target in ancestors
            for mask in chapter.masks.values() for kind, target in mask.contributors)
            or any(getattr(modifier, 'target_layer_id', '') in ancestors
                   for modifier in chapter.modifiers.values())):
        return None
    origin = obj.x + sum(layer.translate_x for layer in chain), obj.y + sum(layer.translate_y for layer in chain)
    if not all(math.isfinite(value) for value in
               (obj.x, obj.y, *(value for layer in chain for value in (layer.translate_x, layer.translate_y)))):
        return None
    mapped = obj.transform_quad is not None or any(layer.transform_quad is not None for layer in chain)
    if not mapped and any(value != round(value) for value in origin):
        return None
    # Mapped sources use a native document grid. The source retains its original
    # sampling grid, and resident edits are mapped into these prepared planes.
    return obj, chain, (0, 0) if mapped else tuple(int(value) for value in origin)


def _split_snapshot(snapshot, obj, chain, *, suffix):
    chapter = copy.copy(snapshot.chapter)
    chapter.layers = dict(chapter.layers)
    branch_kind, branch_id = 'object', obj.object_id
    for original in chain:
        layer = copy.copy(original)
        layer.shape_style = copy.copy(original.shape_style)
        index = next(i for i, ref in enumerate(layer.children)
                     if (ref.kind, ref.entity_id) == (branch_kind, branch_id))
        children = original.children[:index] if suffix else original.children[index + 1:]
        layer.children = list(children)
        if branch_kind == 'layer':
            layer.children.insert(len(children) if suffix else 0, original.children[index])
        if suffix:
            layer.fill_color = None
        else:
            layer.border_width = 0
        chapter.layers[layer.layer_id] = layer
        branch_kind, branch_id = 'layer', layer.layer_id
    index = chapter.root_page_ids.index(chain[-1].layer_id)
    chapter.root_page_ids = ([*chapter.root_page_ids[:index], branch_id] if suffix
                            else [branch_id, *chapter.root_page_ids[index + 1:]])
    document = replace(snapshot.document, background='#00000000' if suffix else snapshot.document.background)
    return replace(snapshot, chapter=chapter, document=document, cache_spec=None, graphics_worker=None)


def feedback_keys(document, visible, origin, side, gutter=2, budget=32 * 1024 * 1024):
    """The bounded native resource coverage, shared by preparation and reuse."""
    extent = QRectF(*visible).intersected(document.bounds)
    if extent.isEmpty():
        return ()
    keys = [(x, y) for y in range(math.floor((extent.top() - origin[1]) / side),
                                 math.ceil((extent.bottom() - origin[1]) / side))
                    for x in range(math.floor((extent.left() - origin[0]) / side),
                                   math.ceil((extent.right() - origin[0]) / side))]
    center = extent.center()
    keys.sort(key=lambda key: (origin[0] + (key[0] + .5) * side - center.x()) ** 2
                             + (origin[1] + (key[1] + .5) * side - center.y()) ** 2)
    return tuple(keys[:max(1, budget // ((side + 2 * gutter) ** 2 * 4 * 3))])


def prepare_raster_feedback(snapshot, identifier, visible, *, caches=None,
                            cancelled=lambda: False, budget=32 * 1024 * 1024):
    """Run on the admitted scene owner, before a pointer contact needs it."""
    eligible = feedback_chain(snapshot, identifier)
    if eligible is None or snapshot.document.live_preview or visible is None:
        return None
    obj, chain, origin = eligible
    side, gutter = snapshot.tiles.tile_size, 2
    keys = feedback_keys(snapshot.document, visible, origin, side, gutter, budget)
    if not keys:
        return None
    before = DetachedSceneBackend(_split_snapshot(snapshot, obj, chain, suffix=False))
    after = DetachedSceneBackend(_split_snapshot(snapshot, obj, chain, suffix=True))
    source_backend = DetachedSceneBackend(replace(snapshot, cache_spec=None, graphics_worker=None))
    if not source_backend.scene._solo_content_visible('object', identifier):
        before.close()
        after.close()
        source_backend.close()
        return None
    raw_obj = copy.copy(obj)
    raw_obj.x = raw_obj.y = 0.
    raw_obj.transform_frame = raw_obj.transform_quad = None
    mapped = (obj.transform_quad is not None or any(layer.transform_quad is not None for layer in chain)
              or any(value != round(value) for value in
                     (obj.x + sum(layer.translate_x for layer in chain),
                      obj.y + sum(layer.translate_y for layer in chain))))
    source_transform = None
    try:
        mapping = (QTransform.fromTranslate(obj.x, obj.y)
                   * source_backend.scene._drawing_object_transform(obj)
                   * source_backend.scene.layer_world_transform(obj.parent_layer_id))
        inverse, valid = mapping.inverted()
        if mapped:
            coefficients = (mapping.m11(), mapping.m12(), mapping.m21(), mapping.m22(), mapping.dx(), mapping.dy())
            if (not valid or not mapping.isAffine() or not all(math.isfinite(value) for value in coefficients)
                    or coefficients[0] <= 0. or coefficients[3] <= 0.
                    or coefficients[1] != 0. or coefficients[2] != 0.):
                return None
            source_transform = coefficients
        if caches:
            before.scene.adopt_cache_state(caches)
            after.scene.adopt_cache_state(caches)
        services = [DocumentRenderService(before), DocumentRenderService(after)]
        for service in services:
            service.projection.revision = snapshot.document.revision
        clips = []
        for layer in reversed(chain):
            clips.append(source_backend.scene.layer_world_transform(layer.layer_id).map(
                source_backend.scene._layer_operand_path(layer)))
        has_top = bool(source_backend.scene._show_on_top_plan().entries)
        top_service = DocumentRenderService(source_backend) if has_top else None
        if top_service is not None:
            top_service.projection.revision = snapshot.document.revision
        tiles = []
        for key in keys:
            if cancelled():
                return None
            bounds = (origin[0] + key[0] * side - gutter,
                      origin[1] + key[1] * side - gutter, side + 2 * gutter, side + 2 * gutter)
            source_keys = _feedback_source_keys(bounds, inverse, side) if mapped else ()
            if source_keys is None:
                return None
            request = RenderRequest(bounds, 1., (side + 2 * gutter,) * 2,
                                    ('raster-feedback', identifier, *key), snapshot.document.revision)
            base_request = replace(request, phase='base') if has_top else request
            planes = [service.render_region(backend.snapshot.document, base_request)
                      for backend, service in zip((before, after), services)]
            if any(not result.exact for result in planes):
                return None
            # Promoted artwork is above every ordinary branch, regardless of
            # its original child position. Keep that ordinary shared top pass
            # on the prepared suffix rather than treating it as a back sibling.
            suffix = planes[1].image
            if top_service is not None:
                top = top_service.render_region(snapshot.document, replace(request, phase='top'))
                if not top.exact:
                    return None
                suffix = suffix.copy()
                suffix_painter = QPainter(suffix)
                try:
                    suffix_painter.drawImage(0, 0, top.image)
                finally:
                    suffix_painter.end()
            source = QImage(side + 2 * gutter, side + 2 * gutter, QImage.Format_ARGB32_Premultiplied)
            source.fill(Qt.transparent)
            painter = QPainter(source)
            try:
                painter.setTransform(mapping * QTransform.fromTranslate(-bounds[0], -bounds[1]) if mapped
                    else QTransform.fromTranslate(origin[0] - bounds[0], origin[1] - bounds[1]))
                local_visible = (inverse.mapRect(QRectF(*bounds)).adjusted(-1., -1., 1., 1.) if mapped
                    else QRectF(bounds[0] - origin[0], bounds[1] - origin[1], bounds[2], bounds[3]))
                with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
                    # The ordinary source kernel supplies unchanged neighboring
                    # pixels and gutters. No object clipping/effect is baked in.
                    source_backend.scene._render_raster_content(painter, raw_obj,
                        local_visible, use_transform_preview=False)
            finally:
                painter.end()
            tiles.append(RasterFeedbackTile(key, bounds, planes[0].image, source, suffix, source_keys))
        return RasterFeedback(snapshot.document, identifier, origin, side, gutter,
                              snapshot.chapter.effective_object_opacity(identifier), tuple(clips), tuple(tiles), source_transform)
    finally:
        before.close()
        after.close()
        source_backend.close()
