"""Worker-only input captures through the document's original source grids."""
from dataclasses import dataclass, replace
import copy

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from comic_editor.core.clipboard import RasterSelectionClipboard, VectorSelectionClipboard
from comic_editor.core.models import RasterObject, VectorStroke
from comic_editor.core.tiles import TileStore
from comic_editor.render.admission import WorkCancelled
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.ui.point_lut import graphics_scope


def _check(cancelled):
    if cancelled is not None and cancelled.is_set():
        raise WorkCancelled()


def mask_wand_patch(snapshot, mask_id, point, remove, entities, tolerance, connected, *, cancelled=None):
    """The classifier's byte grid and mask paint offset match the live wand."""
    state = dict(snapshot.state, _mask_wand_sample_entities=entities, _render_base_alpha=False,
                 _render_cage_source=False, _cage_session=None, _text_editing=False)
    chapter = copy.copy(snapshot.chapter)
    chapter.objects = dict(chapter.objects)
    for obj in chapter.objects.values():
        if getattr(obj, 'reference_role', '') == 'uv_map' and obj.visible:
            private = copy.copy(obj)
            private.visible = False
            chapter.objects[obj.object_id] = private
    backend = DetachedSceneBackend(replace(snapshot, chapter=chapter, state=state))
    scene = backend.scene
    scene._interactive_render = False
    scene._exact_reference_render = True
    mask = chapter.masks[mask_id]
    size, offset = scene.tiles.tile_size, QPointF(*mask.paint_offset)
    selected = TileStore(tile_size=size)
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment), \
                graphics_scope(snapshot.graphics_worker):
            def reference(key):
                _check(cancelled)
                image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
                image.fill(QColor(chapter.background))
                painter = QPainter(image)
                try:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    source = QRectF(key[0]*size, key[1]*size, size, size).translated(offset)
                    painter.translate(-source.x(), -source.y())
                    scene._render_scene_layers(painter, source)
                finally:
                    painter.end()
                return image
            selected.advanced_fill(mask_id, QPointF(*point)-offset,
                QRectF(0, 0, chapter.width, chapter.height).translated(-offset), QColor('white'),
                {'tolerance': tolerance, 'connected_pixels_only': connected, 'antialiasing': False},
                reference_tile=reference, cancel_check=cancelled.is_set if cancelled is not None else None)
            _check(cancelled)
            before, after = {}, {}
            for key, coverage in selected.iter_tiles(mask_id):
                _check(cancelled)
                if remove:
                    painter = QPainter(coverage)
                    painter.setCompositionMode(QPainter.CompositionMode_SourceIn)
                    painter.fillRect(coverage.rect(), QColor('black'))
                    painter.end()
                original = scene.tiles.tile(mask_id, key)
                image = QImage(original) if original is not None else scene.tiles._empty(size)
                painter = QPainter(image)
                painter.drawImage(0, 0, coverage)
                painter.end()
                if image != original:
                    before[key] = QImage(original) if original is not None else None
                    after[key] = image
            return before, after
    finally:
        backend.close()


mask_wand_patch.accepts_cancelled = True
mask_wand_patch.working_bytes = lambda snapshot, *_a: snapshot.document.width*snapshot.document.height*40


def vector_fragments(drawing, selected):
    fragments = []
    for stroke in drawing.strokes:
        if not stroke.points:
            continue
        chosen = [point.point_id in selected for point in stroke.points]
        if not any(chosen):
            continue
        if all(chosen):
            fragments.append(copy.deepcopy(stroke))
            continue
        order = list(range(len(stroke.points)))
        if stroke.closed:
            first = chosen.index(False)
            order = [(first+1+offset) % len(order) for offset in range(len(order))]
        runs, current = [], []
        for index in order:
            if chosen[index]:
                current.append(copy.deepcopy(stroke.points[index]))
            elif current:
                runs.append(current)
                current = []
        if current:
            runs.append(current)
        for points in runs:
            fragments.append(VectorStroke(color=stroke.color, closed=False,
                start_cap=stroke.start_cap, end_cap=stroke.end_cap,
                clip_polygon=copy.deepcopy(stroke.clip_polygon), tiling_group=stroke.tiling_group,
                points=points))
    return fragments


@dataclass
class DrawingCut:
    payload: object
    before: object
    after: object
    installed: object
    frame: object
    bounds: QRectF
    alpha_bounds: object = None
    overlay: object = None


def drawing_selection(snapshot, identifier, path, selected_points, selected_strokes,
                      overlay=None, overlay_base=None, cut=False, frame_margin=24., *, cancelled=None):
    snapshot.finish_sources()
    obj = snapshot.chapter.objects[identifier]
    backend = DetachedSceneBackend(snapshot)
    try:
        scene = backend.scene
        mapping = QTransform(scene._drawing_local_to_world_transform(obj))
        if isinstance(obj, RasterObject):
            source_tiles = overlay if overlay is not None else scene.tiles._tiles.get(identifier, {})
            result = {}
            bounds = path.boundingRect()
            # Sparse occupied keys avoid reading unrelated source tiles or
            # enumerating a mostly empty, arbitrarily large selection frame.
            for key in source_tiles:
                _check(cancelled)
                rect = QRectF(key[0]*obj.tile_size, key[1]*obj.tile_size, obj.tile_size, obj.tile_size)
                if not bounds.intersects(rect) or not path.intersects(rect):
                    continue
                source = source_tiles[key]
                image = QImage(obj.tile_size, obj.tile_size, source.format())
                image.fill(Qt.transparent)
                painter = QPainter(image)
                painter.translate(-rect.x(), -rect.y())
                painter.setClipPath(path, Qt.IntersectClip)
                painter.drawImage(rect.topLeft(), source)
                painter.end()
                if not scene.tiles.is_empty(image):
                    result[key] = image
            payload = RasterSelectionClipboard(result, QPainterPath(path), mapping, obj.name, obj.tile_size) if result else None
            if not cut or payload is None:
                return payload
            before, after, alpha_bounds = {}, {}, {}
            originals = scene.tiles._tiles.get(identifier, {})
            if overlay_base is not None:
                for key in set(originals) | set(overlay_base):
                    _check(cancelled)
                    original, replacement = originals.get(key), overlay_base.get(key)
                    if original != replacement:
                        before[key] = QImage(original) if original is not None else None
                        after[key] = QImage(replacement) if replacement is not None else None
            else:
                for key in result:
                    _check(cancelled)
                    original = originals[key]
                    replacement = QImage(original)
                    painter = QPainter(replacement)
                    painter.setCompositionMode(QPainter.CompositionMode_Clear)
                    painter.translate(-key[0]*obj.tile_size, -key[1]*obj.tile_size)
                    painter.fillPath(path, Qt.black)
                    painter.end()
                    if replacement != original:
                        before[key], after[key] = QImage(original), replacement
            for key, image in after.items():
                alpha_bounds[key] = None if image is None else scene.tiles._alpha_bbox(image)
                scene.tiles.set_tile(identifier, key, image, _known_alpha_bounds=alpha_bounds[key])
            content = scene.tiles.content_bounds(identifier)
            frame = tuple(obj.interaction_rect)
            if content is not None:
                frame = content.adjusted(-frame_margin, -frame_margin, frame_margin, frame_margin).getRect()
            return DrawingCut(payload, before, after, None, frame, mapping.mapRect(bounds), alpha_bounds,
                (overlay_base, overlay, QPainterPath(path)) if overlay is not None else None)
        selected = set(selected_points)
        if not selected and selected_strokes:
            selected = {point.point_id for stroke in obj.strokes if stroke.stroke_id in selected_strokes
                        for point in stroke.points}
        _check(cancelled)
        strokes = vector_fragments(obj, selected)
        payload = VectorSelectionClipboard(strokes, mapping, obj.name) if strokes else None
        if not cut or payload is None:
            return payload
        before = copy.deepcopy(obj)
        after = copy.deepcopy(obj)
        remaining = []
        for stroke in after.strokes:
            _check(cancelled)
            points = [point for point in stroke.points if point.point_id not in selected]
            if len(points) != len(stroke.points):
                stroke.points = points
                stroke.closed = stroke.closed and len(points)>1
                stroke.touch_render_revision()
            if stroke.points:
                remaining.append(stroke)
        after.strokes = remaining
        after.touch_revision()
        return DrawingCut(payload, before, after, copy.deepcopy(after), None, scene.object_world_rect(identifier))
    finally:
        backend.close()


drawing_selection.accepts_cancelled = True
drawing_selection.working_bytes = lambda snapshot, identifier, *_a: sum(
    len(owner)*snapshot.tiles.tile_size**2*16 for key, owner in snapshot.tiles._tiles.items() if key == identifier)


def object_clipboard(snapshot, kind, identifier, *, cancelled=None):
    """Extract the editable subtree and linked mask sources from owned inputs."""
    from comic_editor.ui.clipboard_history import capture_object
    _check(cancelled)
    backend = DetachedSceneBackend(snapshot)
    try:
        payload = capture_object(backend.scene, kind, identifier)
        _check(cancelled)
        return payload
    finally:
        backend.close()


object_clipboard.accepts_cancelled = True
object_clipboard.working_bytes = lambda snapshot, *_a: sum(
    len(owner)*snapshot.tiles.tile_size**2*16 for owner in snapshot.tiles._tiles.values())


def clipboard_preview(_snapshot, kind, payload, *, cancelled=None):
    """History's payload owns original sources; rendering/size encoding stay here."""
    import json
    from comic_editor.core.models import ChapterDocument, VectorDrawingObject
    from comic_editor.render.outputs import capture_document, entity_crop
    from comic_editor.render.pixels import display_image
    from comic_editor.ui.clipboard_history import square_thumbnail
    _check(cancelled)
    if kind == 'drawing':
        document = ChapterDocument(document_kind='asset', background='#00000000')
        page = document.add_page()
        tiles = TileStore()
        if isinstance(payload, RasterSelectionClipboard):
            obj = document.add_object(page.layer_id, RasterObject(name=payload.source_name))
            tiles.replace_object_tiles(obj.object_id, payload.tiles)
            size = sum(image.sizeInBytes() for image in payload.tiles.values())
        else:
            obj = document.add_object(page.layer_id, VectorDrawingObject(
                name=payload.source_name, strokes=copy.deepcopy(payload.strokes)))
            size = len(json.dumps(obj.to_dict()).encode('utf-8'))
        snapshot = capture_document(document, tiles)
        root = ('object', obj.object_id)
    else:
        root = payload.manifest.root_kind, payload.manifest.root_id
        size = 0
        for component in [payload, *payload.dependencies]:
            _check(cancelled)
            size += len(json.dumps(component.manifest.to_dict()).encode('utf-8'))
            size += sum(image.sizeInBytes() for values in component.tiles._tiles.values() for image in values.values())
            size += sum(len(source.data) for source in component.images.snapshot().values())
        # Reject oversized history ownership before decoding/rendering a preview.
        from comic_editor.ui.clipboard_history import MAX_HISTORY_BYTES
        if size+72*72*4 > MAX_HISTORY_BYTES:
            return square_thumbnail(QImage()), size+72*72*4
        snapshot = capture_document(payload.manifest.document, payload.tiles, payload.images)
    image = entity_crop(snapshot, *root, maximum=72, cancelled=cancelled)
    _check(cancelled)
    with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
        image = display_image(image, snapshot.document.pixel_contract)
    return square_thumbnail(image), size+72*72*4


clipboard_preview.accepts_cancelled = True
def _clipboard_workspace(_snapshot, kind, payload):
    if kind == 'drawing':
        return len(payload.tiles)*payload.tile_size**2*32 if isinstance(payload, RasterSelectionClipboard) else 72*72*64
    return sum(len(owner)*component.tiles.tile_size**2*32 for component in [payload, *payload.dependencies]
               for owner in component.tiles._tiles.values()) + sum(
        max(1, getattr(record, 'pixel_width', 1))*max(1, getattr(record, 'pixel_height', 1))*64
        for record in payload.manifest.document.objects.values())


clipboard_preview.working_bytes = _clipboard_workspace


def cage_initial(snapshot, targets, *, cancelled=None):
    """Freeze the before state and derive the cage frame from original artwork."""
    from comic_editor.core.cage import CageGrid
    from comic_editor.ui.baking import visual_bounds
    backend = DetachedSceneBackend(snapshot)
    try:
        scene = backend.scene
        bounds, models, tiles = None, {}, {}
        for kind, identifier in targets:
            _check(cancelled)
            candidate = visual_bounds(scene, kind, identifier)
            bounds = candidate if bounds is None else bounds.united(candidate)
            models[identifier] = copy.deepcopy(scene.chapter.objects[identifier])
            if isinstance(models[identifier], RasterObject):
                tiles[identifier] = scene.tiles.object_tiles(identifier)
        if bounds is None or bounds.isEmpty():
            raise ValueError('The selected drawings contain no artwork to transform.')
        grid = CageGrid(frame=bounds.getRect())
        grid.validate_grid()
        return {'grid': grid, 'targets': list(targets), 'before_models': models,
                'before_tiles': tiles, 'sources': {}}
    finally:
        backend.close()


cage_initial.accepts_cancelled = True


def cage_commit(snapshot, targets, grid, *, cancelled=None):
    """Prepare every selected original source before the owner publishes any."""
    import math
    import numpy as np
    from comic_editor.ui.cage_rendering import warp_image
    from comic_editor.ui.cage_vectors import warp_vector
    from comic_editor.render.effect_pipeline import aligned, empty_image
    backend = DetachedSceneBackend(snapshot)
    try:
        scene = backend.scene
        prepared, tile_bounds = {}, {}
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            for _kind, identifier in targets:
                _check(cancelled)
                obj = scene.chapter.objects[identifier]
                mapping = scene._drawing_local_to_world_transform(obj)
                if not mapping.inverted()[1]:
                    raise ValueError(f'{obj.name} has a singular placement')
                if isinstance(obj, RasterObject):
                    bounds = aligned(scene.tiles.content_bounds(identifier) or QRectF(*obj.interaction_rect))
                    image = empty_image(bounds)
                    painter = QPainter(image)
                    try:
                        for (x, y), tile in scene.tiles.iter_tiles(identifier):
                            _check(cancelled)
                            painter.drawImage(QPointF(x*obj.tile_size, y*obj.tile_size)-bounds.topLeft(), tile)
                    finally:
                        painter.end()
                    result = warp_image(image, bounds, grid, mapping, cancelled=cancelled.is_set if cancelled is not None else None)
                    _check(cancelled)
                    if result is None:
                        raise WorkCancelled()
                    image, bounds = result
                    tiles, alpha = {}, {}
                    size = obj.tile_size
                    for y in range(math.floor(bounds.top()/size), math.ceil(bounds.bottom()/size)):
                        for x in range(math.floor(bounds.left()/size), math.ceil(bounds.right()/size)):
                            _check(cancelled)
                            tile = empty_image(QRectF(0, 0, size, size))
                            painter = QPainter(tile)
                            painter.drawImage(bounds.topLeft()-QPointF(x*size, y*size), image)
                            painter.end()
                            bbox = scene.tiles._alpha_bbox(tile)
                            if bbox is not None:
                                tiles[x, y], alpha[x, y] = tile, bbox
                    replacement = copy.deepcopy(obj)
                    replacement.interaction_rect = QRectF(*obj.interaction_rect).united(bounds).getRect()
                    if obj.modifier_source_frame is not None:
                        replacement.modifier_source_frame = bounds.getRect()
                    prepared[identifier] = replacement, tiles
                    tile_bounds[identifier] = alpha
                else:
                    prepared[identifier] = warp_vector(obj, grid, mapping), None
        return prepared, tile_bounds, {identifier: copy.deepcopy(record) for identifier, (record, _tiles) in prepared.items()}
    finally:
        backend.close()


cage_commit.accepts_cancelled = True
cage_commit.working_bytes = lambda snapshot, *_a: snapshot.document.width*snapshot.document.height*64
