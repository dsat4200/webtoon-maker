"""Prepared native resources for presenting already painted raster contacts.

The ordinary detached kernels prepare the scene on either side of a simple
raster leaf. These provisional resources never enter an artwork/disk cache.
Source/effect grids are unchanged; only resident edited source tiles replace
the prepared source plane on the document thread.
"""
from dataclasses import dataclass, replace
from types import MappingProxyType
from typing import Mapping
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
    source_images: Mapping[tuple, QImage] | None = None
    source_layers: tuple = ()
    source_object: tuple | None = None
    patch_size: int = 0

    @property
    def world_size(self):
        return self.patch_size or self.tile_size

    @property
    def byte_count(self):
        sources = {} if self.source_images is None else self.source_images
        return sum(tile.byte_count for tile in self.tiles) + sum(
            image.sizeInBytes() for image in {image.cacheKey(): image for image in sources.values()}.values())


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
        roundoff_x = max(math.ulp(float(point[0])) for point in quad) * 8
        roundoff_y = max(math.ulp(float(point[1])) for point in quad) * 8
        # Use the shared kernel's exact affine admission tolerance. A failed
        # projective solver can return its identity fallback; that is not
        # evidence that the authored quad is an affine separable mapping.
        if (abs((b[0] - a[0]) - (c[0] - d[0])) > roundoff_x
                or abs((d[1] - a[1]) - (c[1] - b[1])) > roundoff_y
                or abs((d[0] - a[0]) - (c[0] - b[0])) > roundoff_x
                or abs((b[1] - a[1]) - (c[1] - d[1])) > roundoff_y):
            return False
        mapping = SceneKernels._quad_transform(QRectF(*frame), list(quad))
        return mapping.isAffine() and mapping.isInvertible()
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


def feedback_keys(document, visible, origin, side, gutter=2, budget=32 * 1024 * 1024, *, planes=3):
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
    return tuple(keys if budget is None else keys[:max(1, budget // ((side + 2 * gutter) ** 2 * 4 * planes))])


def prepare_raster_feedback(snapshot, identifier, visible, *, caches=None,
                            cancelled=lambda: False, budget=None):
    """Run on the admitted scene owner, before a pointer contact needs it."""
    eligible = feedback_chain(snapshot, identifier)
    if eligible is None or snapshot.document.live_preview or visible is None:
        return None
    obj, chain, origin = eligible
    side, gutter = snapshot.tiles.tile_size, 2
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
            if (not valid or not mapping.isAffine() or not all(math.isfinite(value) for value in coefficients)):
                return None
            source_transform = coefficients
        rebuild_source = (source_transform is not None and
            (coefficients[0] < 0. or coefficients[3] < 0. or coefficients[1] != 0. or coefficients[2] != 0.))
        source_images = {} if rebuild_source else None
        def affine_coefficients(transform):
            return (transform.m11(), transform.m12(), transform.m21(), transform.m22(), transform.dx(), transform.dy())
        source_layers = tuple((affine_coefficients(source_backend.scene._layer_parent_transform(layer)),
            source_backend.scene._layer_operand_path(layer)) for layer in reversed(chain)) if rebuild_source else ()
        source_object = (affine_coefficients(source_backend.scene._drawing_object_transform(obj)),
                         obj.x, obj.y) if rebuild_source else None
        patch_size = 4 * side if rebuild_source else side
        if budget is None:
            budget = (64 if rebuild_source else 32) * 1024 * 1024
        keys = feedback_keys(snapshot.document, visible, origin, patch_size, gutter, budget,
                             planes=2 if rebuild_source else 3)
        if not keys:
            return None
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
        retained_bytes, source_bytes, source_storage = 0, 0, set()
        for key in keys:
            if cancelled():
                return None
            bounds = (origin[0] + key[0] * patch_size - gutter,
                      origin[1] + key[1] * patch_size - gutter, patch_size + 2 * gutter, patch_size + 2 * gutter)
            source_keys = _feedback_source_keys(bounds, inverse, side) if mapped else ()
            if source_keys is None:
                return None
            request = RenderRequest(bounds, 1., (patch_size + 2 * gutter,) * 2,
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
            local_visible = (inverse.mapRect(QRectF(*bounds)).adjusted(-1., -1., 1., 1.) if mapped
                else QRectF(bounds[0] - origin[0], bounds[1] - origin[1], bounds[2], bounds[3]))
            if rebuild_source:
                # Replacing a rotated/mirrored tile by clearing its transformed
                # rectangle rounds edge coverage differently from drawing its
                # native pixels. Keep the bounded immutable original samples
                # instead, and redraw the complete source patch in source order.
                # These COW handles are provisional resources, not cache entries.
                for source_key, image in source_backend.scene.tiles.iter_tiles(identifier, local_visible):
                    if source_key not in source_images:
                        storage = image.cacheKey()
                        additional = 0 if storage in source_storage else image.sizeInBytes()
                        if retained_bytes + planes[0].image.sizeInBytes() + suffix.sizeInBytes() + source_bytes + additional > budget:
                            return None
                        source_storage.add(storage)
                        source_bytes += additional
                        source_images[source_key] = QImage(image)
                source = QImage()
            else:
                source = QImage(patch_size + 2 * gutter, patch_size + 2 * gutter, QImage.Format_ARGB32_Premultiplied)
                source.fill(Qt.transparent)
                painter = QPainter(source)
                try:
                    painter.setTransform(mapping * QTransform.fromTranslate(-bounds[0], -bounds[1]) if mapped
                        else QTransform.fromTranslate(origin[0] - bounds[0], origin[1] - bounds[1]))
                    with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
                        # Unchanged source pixels and gutters use the ordinary
                        # native source kernel without object clipping/effects.
                        source_backend.scene._render_raster_content(painter, raw_obj,
                            local_visible, use_transform_preview=False)
                finally:
                    painter.end()
            tile = RasterFeedbackTile(key, bounds, planes[0].image, source, suffix, source_keys)
            retained_bytes += tile.byte_count
            if retained_bytes + source_bytes > budget:
                return None
            tiles.append(tile)
        feedback = RasterFeedback(snapshot.document, identifier, origin, side, gutter,
            snapshot.chapter.effective_object_opacity(identifier), tuple(clips), tuple(tiles), source_transform,
            None if source_images is None else MappingProxyType(source_images), source_layers, source_object, patch_size)
        return feedback if feedback.byte_count <= budget else None
    finally:
        before.close()
        after.close()
        source_backend.close()
