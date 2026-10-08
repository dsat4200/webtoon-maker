"""Present native edited pixels using detached scene resources only."""
from collections import OrderedDict
from PySide6.QtCore import QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QTransform
from comic_editor.core.tools import ToolKind
from comic_editor.ui.document_presentation import PresentedTile, draw_document_tiles


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


def _compose_tile(canvas, prepared, tile, resident):
    side, gutter = prepared.tile_size, prepared.gutter
    x, y = tile.key
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
        for clip in prepared.clips:
            composed.setClipPath(clip, Qt.IntersectClip)
        composed.setOpacity(prepared.opacity)
        # Match the shared raster kernel's native source sampling. In Qt the
        # raster hint also controls how an already installed curved clip is
        # applied to an image, so it must change after installing the clips.
        composed.setRenderHint(QPainter.Antialiasing, False)
        composed.setRenderHint(QPainter.SmoothPixmapTransform, False)
        composed.drawImage(QRectF(*tile.bounds), source)
        if (canvas.settings.predictive_ink and canvas._predictive is not None
                and not canvas._stroke_erasing):
            composed.setRenderHint(QPainter.Antialiasing, True)
            paint_prediction(composed, canvas._predictive)
        composed.restore()
        composed.drawImage(0, 0, tile.suffix)
    finally:
        composed.end()
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


def present_raster_feedback(canvas, painter, prepared, dirty_keys, cache):
    """No source decode, scene traversal, effects, color transform or waiting."""
    owner = canvas.tiles._tiles.get(prepared.identifier)
    if owner is None:
        return 0
    side, gutter = prepared.tile_size, prepared.gutter
    dirty_keys = set(dirty_keys)
    predictive = canvas._predictive if canvas.settings.predictive_ink and not canvas._stroke_erasing else None
    prediction_key = None if predictive is None else (
        predictive[0].x(), predictive[0].y(), predictive[1].x(), predictive[1].y(),
        predictive[2], predictive[3].rgba())
    prediction_bounds = None
    if predictive is not None:
        margin = predictive[2] / 2. + 2.
        prediction_bounds = QRectF(predictive[0], predictive[1]).normalized().adjusted(
            -margin, -margin, margin, margin)
    camera = canvas.camera_transform()
    # Source gutters and one physical filtering pixel cover presentation at
    # fractional zoom/rotation, without composing obsolete off-screen patches.
    margin = max(gutter, 1. / max(.001, abs(canvas.scale) * canvas.devicePixelRatioF()))
    visible = canvas.visible_document_rect().adjusted(-margin, -margin, margin, margin)
    presented_tiles = []
    for tile in prepared.tiles:
        world = QRectF(tile.bounds[0] + gutter, tile.bounds[1] + gutter, side, side)
        if not world.intersects(visible):
            continue
        # Changed neighboring source pixels also replace a filtering gutter.
        x, y = tile.key
        neighbors = (set(tile.source_keys) if getattr(prepared, 'source_transform', None) is not None else
            {(x + dx, y + dy) for dy in (-1, 0, 1) for dx in (-1, 0, 1)})
        if not dirty_keys.intersection(neighbors):
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
        signature = (id(prepared), local_prediction,
            cache.source_signature(prepared, tile, resident))
        presented = cache.get(tile.key, signature)
        if presented is None:
            presented = _compose_tile(canvas, prepared, tile, resident)
            cache.put(tile.key, signature, presented)
        presented_tiles.append(PresentedTile(('raster-feedback', id(prepared), tile.key),
            presented, world, QRectF(gutter, gutter, side, side)))
        contact = getattr(canvas, '_raster_contact_point', None)
        gate = _contact_gate(canvas)
        if (contact is not None and world.contains(contact)
                and not (gate is not None and gate.busy)):
            canvas._raster_feedback_contact_covered = True
    if presented_tiles:
        # Use the same retained native presenter as exact document tiles. Only
        # display geometry/textures are cached; these live pixels are never
        # admitted to the document or disk artwork cache.
        draw_document_tiles(painter, presented_tiles, camera, canvas.size(), owner=canvas,
            smooth=True, clip_world=prepared.document.bounds)
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
