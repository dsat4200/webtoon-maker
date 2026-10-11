"""Present native edited pixels using detached scene resources only."""
from collections import OrderedDict
from dataclasses import replace
import math
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPaintEngine, QPainter, QPen, QTransform
from comic_editor.core.tools import ToolKind
from comic_editor.ui.document_presentation import (PresentedTile, draw_document_tiles,
    _NATIVE_RASTER_MOSAIC_BYTES, _NATIVE_RASTER_MOSAIC_SIDE, _NATIVE_RASTER_MOSAIC_TILES)


_GUTTER_FORMATS = frozenset((QImage.Format_ARGB32, QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBX8888, QImage.Format_RGBA8888, QImage.Format_RGBA8888_Premultiplied,
    QImage.Format_RGBX64, QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied,
    QImage.Format_RGBX16FPx4, QImage.Format_RGBA16FPx4, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBX32FPx4, QImage.Format_RGBA32FPx4, QImage.Format_RGBA32FPx4_Premultiplied))


class NativeGutterDependency:
    """Owned native pixels which a neighboring tile contributes to a patch."""
    def __init__(self, source_key, rectangle, pixels):
        self.source_key, self.rectangle, self.pixels = source_key, rectangle, pixels

    def __eq__(self, other):
        # Qt compares active native row bytes, format and colorSpace for these
        # formats, including float bits. Copies retain DPR; eligibility is DPR1.
        return (isinstance(other, NativeGutterDependency)
                and self.rectangle == other.rectangle and self.pixels == other.pixels)


class RasterFeedbackPatchCache:
    """Bounded GUI presentation reuse; never an artwork or durable cache."""
    def __init__(self, budget=32 * 1024 * 1024):
        self.budget = budget
        self.entries = OrderedDict()
        self.byte_count = 0

    def clear(self):
        self.entries.clear()
        self.byte_count = 0

    def get(self, key, signature):
        entry = self.entries.get(key)
        if entry is None or entry[0] != signature:
            return None
        self.entries.move_to_end(key)
        return entry[1]

    @staticmethod
    def _entry_bytes(entry):
        return entry[1].sizeInBytes() + sum(
            dependency.pixels.sizeInBytes() for _key, dependency in entry[0][2]
            if isinstance(dependency, NativeGutterDependency))

    def source_signature(self, prepared, tile, resident):
        """Compare every sampled gutter pixel without new invalidation rules."""
        if getattr(prepared, 'source_transform', None) is not None:
            # A world patch can sample arbitrary source tiles. Full owned image
            # tokens avoid assuming a source-grid gutter around the world key.
            return tuple((key, None if image is None else image.cacheKey())
                         for key, image in sorted(resident.items()))
        entry = self.entries.get(tile.key)
        previous = (dict(entry[0][2]) if entry is not None
                    and entry[0][0] == id(prepared) else {})
        side, gutter = prepared.tile_size, prepared.gutter
        native_size = side + 2 * gutter
        native_grid = (0 < gutter < side and tile.source.width() == native_size
            and tile.source.height() == native_size and tile.source.devicePixelRatio() == 1.)
        dependencies = []
        for key, image in sorted(resident.items()):
            dependency = None if image is None else image.cacheKey()
            if (key != tile.key and image is not None and native_grid
                    and not image.isNull() and image.width() == side and image.height() == side
                    and image.devicePixelRatio() == 1. and image.format() in _GUTTER_FORMATS):
                left = (key[0] - tile.key[0]) * side + gutter
                top = (key[1] - tile.key[1]) * side + gutter
                rectangle = QRect(-left, -top, native_size, native_size).intersected(image.rect())
                if not rectangle.isEmpty():
                    bounds = tuple(rectangle.getRect())
                    old = previous.get(key)
                    if (isinstance(old, NativeGutterDependency) and old.rectangle == bounds
                            and old.source_key == dependency
                            and old.pixels.format() == image.format()
                            and old.pixels.colorSpace() == image.colorSpace()):
                        dependency = old
                    else:
                        crop = image.copy(rectangle)
                        if not crop.isNull():
                            candidate = NativeGutterDependency(dependency, bounds, crop)
                            if candidate == old:
                                # Only the source token advances. The sampled
                                # pixels and cached presented image stay owned.
                                old.source_key = dependency
                                dependency = old
                            else:
                                dependency = candidate
            dependencies.append((key, dependency))
        return tuple(dependencies)

    def put(self, key, signature, image):
        previous = self.entries.pop(key, None)
        if previous is not None:
            self.byte_count -= self._entry_bytes(previous)
        self.entries[key] = signature, image
        self.byte_count += self._entry_bytes(self.entries[key])
        while self.byte_count > self.budget and self.entries:
            _key, entry = self.entries.popitem(last=False)
            self.byte_count -= self._entry_bytes(entry)


def _compose_tile(canvas, prepared, tile, resident, flatten=True, prediction=True):
    side, gutter = prepared.tile_size, prepared.gutter
    x, y = tile.key
    rebuild = prepared.source_images is not None
    if not rebuild:
        source = tile.source.copy()
        patch_source = QPainter(source)
        try:
            patch_source.setCompositionMode(QPainter.CompositionMode_Source)
            mapped = getattr(prepared, 'source_transform', None) is not None
            if mapped:
                patch_source.setRenderHint(QPainter.Antialiasing, False)
                patch_source.setRenderHint(QPainter.SmoothPixmapTransform, False)
                patch_source.setTransform(QTransform(*prepared.source_transform)
                    * QTransform.fromTranslate(-tile.bounds[0], -tile.bounds[1]))
            for key, image in resident.items():
                left, top = (key[0] * side, key[1] * side) if mapped else (
                    (key[0] - x) * side + gutter, (key[1] - y) * side + gutter)
                if image is None:
                    patch_source.fillRect(QRectF(left, top, side, side), Qt.transparent)
                else:
                    if mapped:
                        patch_source.fillRect(QRectF(left, top, side, side), Qt.transparent)
                        # Qt's Source image path rounds transformed samples
                        # differently. Clear first, then use the same SourceOver
                        # image path as the ordinary native raster kernel.
                        patch_source.setCompositionMode(QPainter.CompositionMode_SourceOver)
                    patch_source.drawImage(left, top, image)
                    if mapped:
                        patch_source.setCompositionMode(QPainter.CompositionMode_Source)
        finally:
            patch_source.end()
    # The prepared prefix already uses the native composition format. Copying
    # its pixels is identical to SourceOver onto an empty transparent image,
    # without another allocation/fill/draw on every changed patch.
    image = tile.prefix.copy()
    composed = QPainter(image)
    try:
        composed.setRenderHint(QPainter.Antialiasing, True)
        composed.save()
        composed.setTransform(QTransform.fromTranslate(-tile.bounds[0], -tile.bounds[1]))
        composed.setClipRect(prepared.document.bounds)
        if rebuild:
            for transform, clip in prepared.source_layers:
                composed.setTransform(QTransform(*transform), True)
                composed.setClipPath(clip, Qt.IntersectClip)
        else:
            for clip in prepared.clips:
                composed.setClipPath(clip, Qt.IntersectClip)
        composed.setOpacity(prepared.opacity)
        # Match the shared raster kernel's native source sampling. In Qt the
        # raster hint also controls how an already installed curved clip is
        # applied to an image, so it must change after installing the clips.
        composed.setRenderHint(QPainter.Antialiasing, False)
        composed.setRenderHint(QPainter.SmoothPixmapTransform, False)
        if rebuild:
            transform, left, top = prepared.source_object
            composed.setTransform(QTransform(*transform), True)
            composed.translate(left, top)
            # Draw onto the prefix with the ordinary native transform/opacity
            # phase. An intermediate mapped image would round translucent
            # samples before their native composition with the background.
            for key in tile.source_keys:
                native = resident.get(key, prepared.source_images.get(key))
                if native is not None:
                    composed.drawImage(key[0] * side, key[1] * side, native)
            composed.setTransform(QTransform.fromTranslate(-tile.bounds[0], -tile.bounds[1]))
        else:
            composed.drawImage(QRectF(*tile.bounds), source)
        if (prediction and canvas.settings.predictive_ink and canvas._predictive is not None
                and not canvas._stroke_erasing):
            composed.setRenderHint(QPainter.Antialiasing, True)
            paint_prediction(composed, canvas._predictive)
        composed.restore()
        composed.drawImage(0, 0, tile.suffix)
    finally:
        composed.end()
    if not flatten:
        return image
    # Flatten once at the presentation edge, after every scene component.
    presented = QImage(image.size(), QImage.Format_ARGB32_Premultiplied)
    presented.fill(QColor('#242428'))
    display = QPainter(presented)
    try:
        display.drawImage(0, 0, image)
    finally:
        display.end()
    return presented


def paint_prediction(painter, predictive):
    """The same transient pen, inside either ordinary or prepared clipping."""
    start, end, size, color = predictive
    preview = QColor(color)
    preview.setAlpha(round(110 * color.alphaF()))
    painter.setPen(QPen(preview, size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    painter.drawLine(start, end)


def _contact_gate(canvas):
    if canvas.tool == ToolKind.BRUSH:
        return getattr(canvas, '_paint_brush_tile_input', None)
    if canvas.tool == ToolKind.LASSO_BRUSH:
        state = getattr(canvas, '_lasso_brush', None)
        return state.get('input_gate') if state is not None else None
    return getattr(canvas, '_raster_tile_input', None)


def _native_presentation_tiles(canvas, prepared, tile, image, visible, cache):
    """Keep transformed native blocks on the ordinary display tile phase."""
    side = canvas._document_projection.tile_size
    gutter, world_side = prepared.gutter, prepared.world_size
    left, top = tile.bounds[0] + gutter, tile.bounds[1] + gutter
    if (world_side <= side or world_side % side != 0
            or left % side != 0 or top % side != 0
            or image.devicePixelRatio() != 1.
            or image.width() != world_side + 2 * gutter
            or image.height() != world_side + 2 * gutter):
        return [PresentedTile(('raster-feedback', id(prepared), tile.key), image,
            QRectF(left, top, world_side, world_side), QRectF(gutter, gutter, world_side, world_side))]
    output = []
    for y in range(0, world_side, side):
        for x in range(0, world_side, side):
            world = QRectF(left + x, top + y, side, side)
            if not world.intersects(visible):
                continue
            key = ('native-presentation', tile.key, x, y)
            signature = (id(prepared), image.cacheKey(), ())
            crop = cache.get(key, signature)
            if crop is None:
                crop = image.copy(QRect(x, y, side + 2 * gutter, side + 2 * gutter))
                if crop.isNull():
                    continue
                previous = cache.entries.get(key)
                if (previous is not None and previous[0][0] == id(prepared)
                        and previous[1] == crop):
                    # A native block revision need not replace every display
                    # texture. Verify the complete crop, including gutters,
                    # before keeping its owned pixel/texture identity.
                    crop = previous[1]
                cache.put(key, signature, crop)
            output.append(PresentedTile(('raster-feedback', id(prepared), key), crop, world,
                QRectF(gutter, gutter, side, side)))
    return output


def _native_replacement_supported(painter, camera):
    if (painter.paintEngine().type() != QPaintEngine.Raster
            or painter.compositionMode() != QPainter.CompositionMode_SourceOver
            or painter.opacity() != 1.):
        return False
    painter.save()
    try:
        painter.setTransform(camera)
        device = painter.deviceTransform()
        values = tuple(getattr(device, 'm' + str(row) + str(column))()
            for row in range(1, 4) for column in range(1, 4))
        return (all(math.isfinite(value) for value in values) and device.isAffine()
            and device.m11() > 0. and device.m22() > 0.
            and device.m12() == 0. and device.m21() == 0.)
    finally:
        painter.restore()


def _raster_feedback_tiles(canvas, prepared, dirty_keys, cache, replace_native, *, complete=False, prediction=True):
    owner = canvas.tiles._tiles.get(prepared.identifier)
    if owner is None:
        return []
    side, gutter = prepared.tile_size, prepared.gutter
    dirty_keys = set(dirty_keys)
    predictive = canvas._predictive if prediction and canvas.settings.predictive_ink and not canvas._stroke_erasing else None
    prediction_key = None if predictive is None else (
        predictive[0].x(), predictive[0].y(), predictive[1].x(), predictive[1].y(),
        predictive[2], predictive[3].rgba())
    prediction_bounds = None
    if predictive is not None:
        margin = predictive[2] / 2. + 2.
        prediction_bounds = QRectF(predictive[0], predictive[1]).normalized().adjusted(
            -margin, -margin, margin, margin)
    # Source gutters and one physical filtering pixel cover presentation at
    # fractional zoom/rotation, without composing obsolete off-screen patches.
    margin = max(gutter, 1. / max(.001, abs(canvas.scale) * canvas.devicePixelRatioF()))
    visible = canvas.visible_document_rect().adjusted(-margin, -margin, margin, margin)
    presented_tiles = []
    for tile in prepared.tiles:
        world_side = prepared.world_size
        world = QRectF(tile.bounds[0] + gutter, tile.bounds[1] + gutter, world_side, world_side)
        if not world.intersects(visible):
            continue
        # Changed neighboring source pixels also replace a filtering gutter.
        x, y = tile.key
        neighbors = (set(tile.source_keys) if getattr(prepared, 'source_transform', None) is not None else
            {(x + dx, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)})
        if not complete and not dirty_keys.intersection(neighbors):
            continue
        resident = {}
        missing = False
        for key in neighbors.intersection(dirty_keys):
            if key not in owner.entries:
                resident[key] = None  # An eraser may prune the entire tile.
                continue
            entry = owner.residency.entries.get((owner, key))
            if entry is None:
                # An evicted edited source waits for normal detached work.
                # Presentation never opens its backing file on the GUI.
                missing = True
                break
            resident[key] = entry[0]
        if missing:
            continue
        # Prediction only invalidates patches which contain its native AA
        # coverage. Leaving a patch changes its signature back to None and
        # removes the previous transient pen exactly once.
        local_prediction = (prediction_key if prediction_bounds is not None
            and QRectF(*tile.bounds).intersects(prediction_bounds) else None)
        signature = (id(prepared), (local_prediction, replace_native),
            cache.source_signature(prepared, tile, resident))
        presented = cache.get(tile.key, signature)
        if presented is None:
            presented = (_compose_tile(canvas, prepared, tile, resident, not replace_native)
                if prediction else _compose_tile(canvas, prepared, tile, resident, not replace_native, False))
            cache.put(tile.key, signature, presented)
        presented_tiles.extend(_native_presentation_tiles(canvas, prepared, tile,
            presented, visible, cache))
        contact = getattr(canvas, '_raster_contact_point', None)
        gate = _contact_gate(canvas)
        if (contact is not None and world.contains(contact)
                and not (gate is not None and gate.busy)):
            canvas._raster_feedback_contact_covered = True
    return presented_tiles


def replace_raster_feedback_batch(canvas, painter, document, requests, batch):
    """A small complete contact basis shares ordinary native presentation.

    Every visible image comes from the guarded prepared scene and current
    resident source journal. The retained batch supplies only validated native
    address/geometry metadata; stale source pixels never enter this live batch.
    """
    controller = canvas._scene_controller
    prepared = controller.feedback
    visible = canvas.visible_document_rect().intersected(document.bounds)
    if (prepared is None or controller._filtered_state is not None or not controller.feedback_dirty
            or prepared.identifier != canvas.selected_id
            or prepared.document.configuration != document.configuration
            or prepared.document.identity != document.identity
            or not _native_replacement_supported(painter, canvas.camera_transform())
            or not controller._feedback_covers(document, tuple(visible.getRect()))):
        return batch, 0, None
    gate = controller._covered_contact_gate(document, tuple(visible.getRect()))
    if gate is None or gate.busy:
        return batch, 0, None
    owner = canvas.tiles._tiles.get(prepared.identifier)
    if owner is None or any(key in owner.entries and (owner, key) not in owner.residency.entries
                           for key in controller.feedback_dirty):
        return batch, 0, None
    # Match the existing one-native-pixel mosaic cap before any composition.
    region = visible.toAlignedRect().adjusted(-1, -1, 1, 1).intersected(document.bounds.toAlignedRect())
    requests = tuple(request for request in requests if request.world_rect.intersects(QRectF(region)))
    if (region.isEmpty() or region.width() > _NATIVE_RASTER_MOSAIC_SIDE
            or region.height() > _NATIVE_RASTER_MOSAIC_SIDE
            or region.width() * region.height() * 4 > _NATIVE_RASTER_MOSAIC_BYTES
            or not 2 <= len(requests) <= _NATIVE_RASTER_MOSAIC_TILES):
        return batch, 0, None
    from comic_editor.ui.filtered_raster_feedback import FilteredRasterFeedbackState
    if not FilteredRasterFeedbackState.native_batch(batch):
        return batch, 0, None
    base = getattr(canvas, '_projection_completed_view', None)
    metadata = FilteredRasterFeedbackState._native_base(document, requests, base, 0)
    if metadata is None:
        return batch, 0, None
    # _native_base validates complete source rectangles, dimensions and phases.
    # Also reject mismatched supplied tile metadata or a device-only basis.
    supplied = {tile.key[1]: tile for tile in batch[0][1]}
    if any(request.address not in supplied or not isinstance(supplied[request.address].image, QImage)
            or supplied[request.address].world_rect != metadata[request.address].world_rect
            or supplied[request.address].source_rect != metadata[request.address].source_rect
            or supplied[request.address].image.width() != request.pixel_size
            or supplied[request.address].image.height() != request.pixel_size
            or supplied[request.address].image.devicePixelRatio() != 1.
            or supplied[request.address].pixel_contract != document.pixel_contract
            for request in requests):
        return batch, 0, None
    composed = _raster_feedback_tiles(canvas, prepared, controller.feedback_dirty,
        controller.feedback_patches, True, complete=True)
    current = {tuple(tile.world_rect.getRect()): tile for tile in composed}
    output = []
    for request in requests:
        image = current.get(tuple(request.world_rect.getRect()))
        if (image is None or image.source_rect != request.source_rect
                or image.image.width() != request.pixel_size
                or image.image.height() != request.pixel_size):
            return batch, 0, None
        output.append(replace(supplied[request.address], image=image.image))
    source_signature = tuple((key, None if key not in owner.entries else
        owner.residency.entries[(owner, key)][0].cacheKey()) for key in sorted(controller.feedback_dirty))
    predictive = canvas._predictive if canvas.settings.predictive_ink and not canvas._stroke_erasing else None
    prediction_key = None if predictive is None else (predictive[0].x(), predictive[0].y(),
        predictive[1].x(), predictive[1].y(), predictive[2], predictive[3].rgba())
    context = ('raster-contact', document.identity, document.configuration, document.revision,
        prepared.identifier, id(prepared), source_signature, prediction_key)
    return [(None, output)], len(output), context


def present_raster_feedback(canvas, painter, prepared, dirty_keys, cache):
    """No source decode, scene traversal, effects, color transform or waiting."""
    camera = canvas.camera_transform()
    # Preserve ordinary interpolation/alpha rounding on Raster widgets. The
    # owned display clip first replaces the old scene with its background;
    # the unchanged native alpha image is then sampled/composed exactly once.
    # Other presentation backends retain the opaque provisional overlay.
    replace_native = _native_replacement_supported(painter, camera)
    presented_tiles = _raster_feedback_tiles(canvas, prepared, dirty_keys, cache, replace_native)
    if presented_tiles:
        # Use the same retained native presenter as exact document tiles. Only
        # display geometry/textures are cached; these live pixels are never
        # admitted to the document or disk artwork cache.
        draw_document_tiles(painter, presented_tiles, camera, canvas.size(), owner=canvas,
            smooth=True, clip_world=None if replace_native else prepared.document.bounds,
            replace_background=QColor('#242428') if replace_native else None)
        controller = canvas._scene_controller
        basis = controller._presentation_batch
        if basis is not None and canvas._predictive is None:
            document, batch, view = basis
            controller.raster_continuity.remember(document, batch, presented_tiles,
                replace_background=replace_native, view=view)
    return len(presented_tiles)


def present_pending_raster_gesture(canvas, painter, document):
    """Show the received contact while its source/scene pixels are pending."""
    canvas._raster_feedback_pending_visible = False
    point = getattr(canvas, '_raster_contact_point', None)
    if point is None or canvas.tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER,
                                           ToolKind.BRUSH, ToolKind.LASSO_BRUSH}:
        return False
    gate = _contact_gate(canvas)
    queued = gate is not None and gate.busy
    preview = canvas._scene_controller.preview
    current_preview = preview is not None and preview[0] == document
    if not queued and (getattr(canvas, '_raster_feedback_contact_covered', False)
                       or current_preview or not canvas._projection_frame_pending):
        if not getattr(canvas, '_raster_contact_active', False) and not canvas._projection_frame_pending:
            canvas._raster_contact_point = None
        return False
    canvas._raster_feedback_pending_visible = True
    painter.save()
    try:
        painter.resetTransform()
        center = canvas.camera_transform().map(point)
        size = (canvas.settings.active_eraser_pixels() if canvas.tool == ToolKind.RASTER_ERASER
                else canvas.settings.brush_size_px if canvas.tool == ToolKind.BRUSH
                else canvas.settings.pencil_size())
        radius = max(4., min(72., size * abs(canvas.scale) / 2.))
        painter.setBrush(Qt.NoBrush)
        painter.setPen(QPen(QColor('#202024'), 3.))
        painter.drawEllipse(center, radius, radius)
        painter.setPen(QPen(QColor('#f3c56a'), 1.25, Qt.DashLine))
        painter.drawEllipse(center, radius, radius)
        label = QRectF(12., max(8., canvas.height() - 34.), 174., 22.)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor('#dd242428'))
        painter.drawRoundedRect(label, 4., 4.)
        painter.setPen(QColor('#f3c56a'))
        painter.drawText(label, Qt.AlignCenter, 'Updating drawing…')
    finally:
        painter.restore()
    return True
