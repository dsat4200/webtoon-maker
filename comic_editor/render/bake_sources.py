"""Native source preparation for transactional editor bake actions."""
from dataclasses import dataclass, replace
import copy
import math

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, QRectF
from PySide6.QtGui import QPainter, QTransform

from comic_editor.render.effect_pipeline import aligned, empty_image, render_stages
from comic_editor.render.pixels import current_contract, export_image, import_image, pixel_scope
from comic_editor.render.source_sampling import input_backend
from comic_editor.core.document_patch import RecordSnapshot


@dataclass(frozen=True)
class BakeState:
    model: object
    images: dict
    tiles: dict
    alpha_bounds: dict


def _history_records(chapter, **selection):
    """Serializers validate mutable fields; never run them on frozen records."""
    private = copy.copy(chapter)
    for group, identifiers in selection.items():
        if group in {'scalars', 'attributes'}:
            continue
        collection = getattr(chapter, group)
        detached = dict(collection)
        for identifier in identifiers:
            if identifier in collection:
                detached[identifier] = copy.deepcopy(collection[identifier])
        setattr(private, group, detached)
    result = RecordSnapshot.capture(private, **selection)
    result.document_identity = id(chapter)
    return result


def clear_drawings_history(snapshot, identifiers):
    """Prepare only affected native originals and vector source metadata."""
    from comic_editor.core.models import RasterObject, VectorDrawingObject
    snapshot.finish_sources()
    chapter = snapshot.chapter
    raster = tuple(identifier for identifier in identifiers
                   if isinstance(chapter.objects[identifier], RasterObject))
    vectors = tuple(identifier for identifier in identifiers
                    if isinstance(chapter.objects[identifier], VectorDrawingObject))
    if len(raster) + len(vectors) != len(identifiers):
        raise ValueError('A clear target is no longer a drawing')
    model = RecordSnapshot.capture(chapter, objects=vectors,
        attributes={'objects': ('strokes', 'drawing_revision')})
    model.document_identity = snapshot.document.identity[0]
    tiles = {identifier: snapshot.tiles.object_tiles(identifier) for identifier in raster}
    bounds = {identifier: {key: snapshot.tiles._alpha_bbox(image) for key, image in values.items()}
              for identifier, values in tiles.items()}
    return BakeState(model, {}, tiles, bounds)


def _removed_metadata(chapter, remaining, changed_masks=()):
    modifiers = {mid for entity in remaining for mid in entity.modifier_ids}
    removed = set(chapter.modifiers)-modifiers
    masks = {entity.opacity_mask.mask_id for entity in remaining if entity.opacity_mask is not None}
    masks.update(binding.mask_id for mid in modifiers for binding in chapter.modifiers[mid].parameter_masks.values())
    removed_masks = {identifier for identifier, mask in chapter.masks.items()
                     if not mask.saved and identifier not in masks}
    return removed, removed_masks | set(changed_masks)


def _state(scene, model, identifiers):
    identifiers = set(identifiers)
    tiles = {identifier: scene.tiles.object_tiles(identifier) for identifier in identifiers}
    bounds = {identifier: {key: scene.tiles._alpha_bbox(image) for key, image in values.items()}
              for identifier, values in tiles.items()}
    return BakeState(model, {identifier: scene.images.source(identifier) for identifier in identifiers}, tiles, bounds)


def rasterize_history(scene, kind, identifier):
    from comic_editor.ui.baking import subtree
    chapter = scene.chapter
    members = subtree(scene, kind, identifier)
    target = (chapter.layers if kind == 'layer' else chapter.objects)[identifier]
    objects = {key for member_kind, key in members if member_kind == 'object'} | {identifier}
    layers = {key for member_kind, key in members if member_kind == 'layer'}
    layers.add(target.parent_id if kind == 'layer' else target.parent_layer_id)
    layers.update(key for key, layer in chapter.layers.items() if layer.last_raster_id in objects)
    remaining = [entity for group_kind, group in (('layer', chapter.layers), ('object', chapter.objects))
                 for key, entity in group.items() if (group_kind, key) not in members]
    modifiers, masks = _removed_metadata(chapter, remaining,
        (key for key, mask in chapter.masks.items() if (kind, identifier) in mask.contributors))
    model = _history_records(chapter, layers=layers, objects=objects,
        modifiers=modifiers, masks=masks, scalars=('modifier_preset_ids',))
    return _state(scene, model, objects)


def raster_prefix_history(scene, modifier_id, targets):
    chapter = scene.chapter
    identifiers = {identifier for _kind, identifier in targets}
    remaining = list(chapter.layers.values())
    for identifier, obj in chapter.objects.items():
        if identifier in identifiers:
            from copy import copy
            obj = copy(obj)
            prefix = obj.modifier_ids[:obj.modifier_ids.index(modifier_id)+1]
            baked = {mid for mid in prefix if not chapter.modifiers[mid].muted}
            obj.modifier_ids = [mid for mid in obj.modifier_ids if mid not in baked]
        remaining.append(obj)
    modifiers, masks = _removed_metadata(chapter, remaining)
    model = _history_records(chapter, objects=identifiers,
        modifiers=modifiers, masks=masks, scalars=('modifier_preset_ids',))
    return _state(scene, model, identifiers)


@dataclass(frozen=True)
class RasterizedSource:
    bounds: QRectF
    image: object
    encoded: bytes
    placement: tuple
    history: object = None


@dataclass(frozen=True)
class AppliedRasterSource:
    identifier: str
    baked: tuple
    bounds: QRectF
    tiles: dict
    placement: object
    alpha_bounds: object = None


@dataclass(frozen=True)
class ImageRasterSource:
    width: int
    height: int
    quad: tuple
    tiles: dict
    history: object = None
    alpha_bounds: object = None


@dataclass(frozen=True)
class AppliedRasterSources:
    sources: tuple
    history: object

    def __iter__(self):
        return iter(self.sources)


def image_raster_source(snapshot, identifier):
    backend = input_backend(snapshot)
    try:
        obj = backend.scene.chapter.objects[identifier]
        image = backend.scene.images.image(identifier)
        if image.isNull():
            raise ValueError('The embedded image cannot be decoded.')
        size = backend.scene.tiles.tile_size
        tiles = {(x, y): image.copy(x*size, y*size, min(size, image.width()-x*size),
                                   min(size, image.height()-y*size))
                 for y in range(math.ceil(image.height()/size)) for x in range(math.ceil(image.width()/size))}
        model = _history_records(backend.scene.chapter, objects=(identifier,))
        model.document_identity = snapshot.document.identity[0]
        history = _state(backend.scene, model, (identifier,))
        bounds = {key: backend.scene.tiles._alpha_bbox(tile) for key, tile in tiles.items()}
        return ImageRasterSource(image.width(), image.height(), tuple(backend.scene._image_local_quad(obj)), tiles, history, bounds)
    finally:
        backend.close()


def prepare_rasterize(scene, kind, identifier, *, image_factory=empty_image):
    from comic_editor.ui.baking import visual_bounds
    from comic_editor.ui.object_blending import suspend_object_blend
    target = (scene.chapter.layers if kind == 'layer' else scene.chapter.objects)[identifier]
    parent_id = target.parent_id if kind == 'layer' else target.parent_layer_id
    bounds = aligned(visual_bounds(scene, kind, identifier))
    image = image_factory(bounds)
    mapping = scene.layer_world_transform(parent_id)
    inverse, valid = mapping.inverted()
    if not valid:
        raise ValueError('Cannot rasterize a singular transform')
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.left(), -bounds.top())
    painter.setTransform(mapping, True)
    visible, mask_only = target.visible, target.mask_only
    references, interactive = scene._rendering_compound_references, scene._interactive_render
    try:
        target.visible, target.mask_only = True, False
        scene._interactive_render, scene._rendering_compound_references = False, True
        with scene.without_solo(), suspend_object_blend(scene, identifier if kind == 'object' else ''):
            if kind == 'layer':
                scene._render_layer(painter, target, 1., bounds)
            else:
                scene._render_object(painter, target, 1., inverse.mapRect(bounds))
    finally:
        target.visible, target.mask_only = visible, mask_only
        scene._rendering_compound_references, scene._interactive_render = references, interactive
        painter.end()
    # Image truth is the encoded PNG, including its source color profile. Keep
    # the decoded handle in that same source space for future working imports.
    image = export_image(image, current_contract())
    data, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(data)
    buffer.open(QIODevice.WriteOnly)
    try:
        if not image.save(buffer, 'PNG'):
            raise ValueError('Unable to encode the rasterized image')
    finally:
        buffer.close()
    return RasterizedSource(bounds, image, bytes(data), tuple(inverse.map(point).toTuple() for point in
        (bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft())))


def prepare_raster_modifiers(scene, modifier_id, targets):
    result = []
    chapter = scene.chapter
    for _, identifier in targets:
        obj = chapter.objects[identifier]
        prefix = obj.modifier_ids[:obj.modifier_ids.index(modifier_id)+1]
        baked = tuple(mid for mid in prefix if not chapter.modifiers[mid].muted)
        bounds = scene.tiles.content_bounds(identifier) or QRectF(*obj.interaction_rect)
        if obj.modifier_source_frame is not None:
            bounds = bounds.united(QRectF(*obj.modifier_source_frame))
        bounds = aligned(bounds)
        image = empty_image(bounds)
        painter = QPainter(image)
        try:
            for (x, y), tile in scene.tiles.iter_tiles(identifier):
                source = import_image(tile, current_contract())
                painter.drawImage(QPointF(x*obj.tile_size, y*obj.tile_size)-bounds.topLeft(), source)
        finally:
            painter.end()
        tiling, placement = scene._own_tiling(obj), None
        if tiling and tiling.modifier_id in baked:
            image, bounds = scene._tiling_stage(obj)
            image, bounds = render_stages(scene, image, bounds,
                [chapter.modifiers[mid] for mid in baked if mid != tiling.modifier_id], QTransform(), nearest=True)
            inverse, valid = scene.layer_world_transform(obj.parent_layer_id).inverted()
            if not valid:
                raise ValueError('Cannot apply tiling through a singular drawing transform')
            placement = tuple(inverse.map(point).toTuple() for point in
                (bounds.topLeft(), bounds.topRight(), bounds.bottomRight(), bounds.bottomLeft()))
        else:
            image, bounds = render_stages(scene, image, bounds, [chapter.modifiers[mid] for mid in baked],
                scene._drawing_local_to_world_transform(obj), nearest=True)
        # Editable Raster truth remains native encoded-sRGB bytes. Baking is a
        # source edge, not a display transform or a reusable float render tile.
        source_contract = replace(current_contract(), export_space='srgb') if current_contract().floating else current_contract()
        image = export_image(image, source_contract, high_precision=False)
        tiles, alpha_bounds, size = {}, {}, obj.tile_size
        for y in range(math.floor(bounds.top()/size), math.ceil(bounds.bottom()/size)):
            for x in range(math.floor(bounds.left()/size), math.ceil(bounds.right()/size)):
                from comic_editor.core.pixel_contract import LEGACY_PIXELS
                with pixel_scope(LEGACY_PIXELS):
                    tile = empty_image(QRectF(0, 0, size, size))
                painter = QPainter(tile)
                painter.drawImage(bounds.topLeft()-QPointF(x*size, y*size), image)
                painter.end()
                bbox = scene.tiles._alpha_bbox(tile)
                if bbox is not None:
                    tiles[x, y] = tile
                    alpha_bounds[x, y] = bbox
        result.append(AppliedRasterSource(identifier, baked, bounds, tiles, placement, alpha_bounds))
    return result


def rasterized_source(snapshot, kind, identifier):
    backend = input_backend(snapshot)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            history = rasterize_history(backend.scene, kind, identifier)
            history.model.document_identity = snapshot.document.identity[0]
            return replace(prepare_rasterize(backend.scene, kind, identifier), history=history)
    finally:
        backend.close()


def applied_raster_sources(snapshot, modifier_id, targets):
    backend = input_backend(snapshot)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            history = raster_prefix_history(backend.scene, modifier_id, targets)
            history.model.document_identity = snapshot.document.identity[0]
            return AppliedRasterSources(tuple(prepare_raster_modifiers(backend.scene, modifier_id, targets)), history)
    finally:
        backend.close()


def bake_working_bytes(snapshot, targets):
    from comic_editor.ui.baking import visual_bounds
    from comic_editor.render.scene import EvaluatedScene
    scene = EvaluatedScene(snapshot)
    class SourceExtents:
        def __init__(self, tiles):
            self.tiles = tiles
        def __getattr__(self, name):
            return getattr(self.tiles, name)
        def content_bounds(self, identifier):
            keys = tuple(self.tiles._tiles.get(identifier, ()))
            if not keys:
                return None
            xs, ys = zip(*keys)
            size = self.tiles.tile_size
            return QRectF(min(xs)*size, min(ys)*size,
                          (max(xs)-min(xs)+1)*size, (max(ys)-min(ys)+1)*size)
    # Admission cannot decode sources to determine the size of its own ticket.
    # Conservative signed tile extents preserve grids without reading pixels.
    scene.tiles = SourceExtents(scene.tiles)
    with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
        return sum(max(1, math.ceil(bounds.width()))*max(1, math.ceil(bounds.height()))*64
                   for bounds in (visual_bounds(scene, kind, identifier) for kind, identifier in targets))


rasterized_source.working_bytes = lambda snapshot, kind, identifier: bake_working_bytes(snapshot, ((kind, identifier),))
applied_raster_sources.working_bytes = lambda snapshot, modifier_id, targets: bake_working_bytes(snapshot, targets)
image_raster_source.working_bytes = lambda snapshot, identifier: (
    snapshot.chapter.objects[identifier].pixel_width*snapshot.chapter.objects[identifier].pixel_height*64)
