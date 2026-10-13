"""Bounded provisional replay of an ordinary native scene capture.

The scene kernels supply every drawing operation and native source image. Only
the selected finite-filter source image changes. Replay always keeps the full
ordinary capture device, its origin, clipping and operation order. These owned
resources are transient, and are never admitted to an exact or disk cache.
"""
from dataclasses import dataclass, replace
import copy
import math

from PySide6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, QSizeF
from PySide6.QtGui import (QBrush, QColor, QFont, QImage, QPainter, QPainterPath,
    QPen, QPicture, QPolygon, QPolygonF, QRegion, QTransform)

from comic_editor.core.models import (BrightnessContrastModifier, CurvesModifier,
    HueSaturationLightnessModifier, OutlineModifier)
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.raster_feedback import feedback_chain
from comic_editor.render.scene import DetachedSceneBackend, detached_value
from comic_editor.render.service import DocumentRenderService, RenderQuality
from comic_editor.render.tile_effects import footprint
from comic_editor.core.effect_geometry import outline_blur_padding
from comic_editor.render.modifier_rendering import modifier_render_settings


class FeedbackUnavailable(Exception):
    """The ordinary detached scene remains the fallback for this capture."""


# Bound complete native recipes and their retained source/scene progress.
FILTERED_PREPARED_BUDGET = 128 * 1024 * 1024

_VALUES = (QPoint, QPointF, QRect, QRectF, QSize, QSizeF, QBrush, QColor, QFont,
    QImage, QPainterPath, QPen, QPicture, QPolygon, QPolygonF, QRegion, QTransform)
_MUTATIONS = frozenset(('save', 'restore', 'setOpacity', 'setTransform', 'resetTransform',
    'setWorldTransform', 'translate', 'rotate', 'scale', 'shear', 'setRenderHint',
    'setRenderHints', 'setClipRect', 'setClipPath', 'setClipRegion', 'setClipping',
    'setCompositionMode', 'setPen', 'setBrush', 'setBrushOrigin', 'setFont',
    'fillRect', 'fillPath', 'strokePath', 'drawImage', 'drawPath', 'drawPolygon',
    'drawPolyline', 'drawEllipse', 'drawRect', 'drawRoundedRect', 'drawLine',
    'drawLines', 'drawPoint', 'drawPoints'))
_GETTERS = frozenset(('transform', 'worldTransform', 'deviceTransform', 'combinedTransform',
    'opacity', 'renderHints', 'testRenderHint', 'hasClipping', 'clipPath', 'clipBoundingRect',
    'clipRegion', 'compositionMode', 'pen', 'brush', 'brushOrigin', 'font',
    'device', 'isActive'))
_DRAWS = frozenset(name for name in _MUTATIONS if name.startswith('draw')
    or name in ('fillRect', 'fillPath', 'strokePath'))


@dataclass(frozen=True)
class NativePainterOperation:
    name: str
    args: tuple
    kwargs: tuple

    def apply(self, painter):
        getattr(painter, self.name)(*self.args, **dict(self.kwargs))


@dataclass(frozen=True)
class NativeTextOperation:
    """Owned Qt paint commands; no text document or layout crosses workers."""
    picture: QPicture
    name: str = 'native_text_document'

    def apply(self, painter):
        painter.drawPicture(QPointF(), self.picture)


class _Payload:
    """Charge shared native storage once; bound command and geometry metadata."""
    def __init__(self, budget):
        self.budget = budget
        self.images = {}
        self.metadata = 0

    @property
    def byte_count(self):
        return sum(self.images.values()) + self.metadata

    def own(self, value):
        self.metadata += 64
        if isinstance(value, QImage):
            self.images[value.cacheKey()] = value.sizeInBytes()
        elif isinstance(value, QPicture):
            self.metadata += value.size()
        elif isinstance(value, QPen):
            self.own(value.brush())
        elif isinstance(value, QBrush):
            image = value.textureImage()
            if not image.isNull():
                self.own(image)
            gradient = value.gradient()
            if gradient is not None:
                self.metadata += 64 * len(gradient.stops())
        elif isinstance(value, QPainterPath):
            self.metadata += 64 * value.elementCount()
        elif isinstance(value, QRegion):
            self.metadata += 64 * value.rectCount()
        elif isinstance(value, (QPolygon, QPolygonF)):
            self.metadata += 64 * value.size()
        elif isinstance(value, str):
            self.metadata += len(value.encode('utf-8'))
        elif isinstance(value, (tuple, list)):
            if len(value) > 16384:
                raise FeedbackUnavailable('Oversized painter argument')
            result = tuple(self.own(item) for item in value)
            self.check()
            return result
        elif isinstance(value, dict):
            if len(value) > 16384:
                raise FeedbackUnavailable('Oversized painter argument')
            result = tuple((self.own(key), self.own(item)) for key, item in value.items())
            self.check()
            return result
        self.check()
        return type(value)(value) if isinstance(value, _VALUES) else detached_value(value)

    def check(self):
        if self.byte_count > self.budget:
            raise FeedbackUnavailable('Prepared native feedback exceeds its byte budget')


class _Recorder:
    def __init__(self, painter, payload, cancelled, *, suppress_draws=False):
        self.painter, self.payload, self.cancelled = painter, payload, cancelled
        self.suppress_draws = suppress_draws
        self.phase = 'before'
        self.operations = []
        self.selected_operations = []
        self.prefix = None
        self.boundary = None

    def _draw_native_text_document(self, document, context):
        """Let the ordinary Qt layout draw through a real native painter.

        Prefix pixels already belong to the retained complete device. A suffix
        retains Qt's ordered native text commands, rather than a transparent
        raster plane or a mutable QObject from this preparation thread.
        """
        if self.cancelled():
            raise FeedbackUnavailable('Native feedback preparation cancelled')
        if self.phase == 'before' and not self.suppress_draws:
            document.documentLayout().draw(self.painter, context)
            return
        picture = QPicture()
        device = self.painter.device()
        if (device.devicePixelRatioF() != 1.
                or device.logicalDpiX() != picture.logicalDpiX()
                or device.logicalDpiY() != picture.logicalDpiY()):
            raise FeedbackUnavailable('Native text command device changed its DPI')
        writer = QPainter(picture)
        try:
            # Placement and clipping remain the original ordered operations on
            # the native scene device. Qt records untransformed text commands.
            writer.setRenderHints(writer.renderHints(), False)
            writer.setRenderHints(self.painter.renderHints())
            writer.setPen(self.painter.pen())
            writer.setBrush(self.painter.brush())
            writer.setFont(self.painter.font())
            writer.setOpacity(self.painter.opacity())
            writer.setCompositionMode(self.painter.compositionMode())
            document.documentLayout().draw(writer, context)
        finally:
            writer.end()
        if len(self.operations) + len(self.selected_operations) >= 16384:
            raise FeedbackUnavailable('Too many ordinary painter operations')
        self.payload.metadata += 1024
        operation = NativeTextOperation(self.payload.own(picture))
        (self.selected_operations if self.phase == 'selected' else self.operations).append(operation)
        if not self.suppress_draws:
            document.documentLayout().draw(self.painter, context)

    def __getattr__(self, name):
        if name in _GETTERS:
            return getattr(self.painter, name)
        if name not in _MUTATIONS:
            raise FeedbackUnavailable('Unsupported ordinary painter operation: ' + name)
        def record(*args, **kwargs):
            if self.cancelled():
                raise FeedbackUnavailable('Native feedback preparation cancelled')
            retain = self.suppress_draws or self.phase == 'selected' or self.phase == 'after' or name not in _DRAWS
            if retain:
                if len(self.operations) + len(self.selected_operations) >= 16384:
                    raise FeedbackUnavailable('Too many ordinary painter operations')
                self.payload.metadata += 1024
                operation = NativePainterOperation(name, self.payload.own(args), self.payload.own(kwargs))
                (self.selected_operations if self.phase == 'selected' else self.operations).append(operation)
            if not self.suppress_draws or name not in _DRAWS:
                return getattr(self.painter, name)(*args, **kwargs)
        return record


@dataclass(frozen=True)
class NativeOutlineSampling:
    """Witness for the unchanged ordinary parent-local byte outline route."""
    complete_frame: tuple
    window: tuple
    effect_region: tuple
    native_mapping: tuple
    parent_mapping: tuple
    stages: tuple


def _outline_sampling(backend, obj, complete, window, parent, modifiers, image):
    # This first route keeps the ordinary optimized single-outline kernel.
    # Mask fields, outline blur and multiple-outline precision need separate
    # native region proofs; they retain the existing ordinary fallback.
    if (len(modifiers) != 1 or not isinstance(modifiers[0], OutlineModifier)
            or modifiers[0].style != 'solid' or modifiers[0].parameter_masks
            or outline_blur_padding(modifiers[0]) != 0):
        raise FeedbackUnavailable('Unsupported ordinary outline source witness')
    native = QTransform.fromTranslate(obj.x, obj.y) * backend.scene._drawing_object_transform(obj)
    effect_region = backend.scene._modifier_viewport_region(QRectF())
    values = (*complete.getRect(), *window.getRect(), *effect_region.getRect())
    if (not all(math.isfinite(value) for value in values)
            or complete != QRectF(complete.toAlignedRect())
            or window != QRectF(window.toAlignedRect()) or window.isEmpty() or effect_region.isEmpty()
            or not complete.contains(window)
            or not native.isAffine() or not native.isInvertible()
            or not parent.isAffine() or not parent.isInvertible()
            or image.isNull() or image.devicePixelRatio() != 1.
            or image.width() != int(window.width()) or image.height() != int(window.height())):
        raise FeedbackUnavailable('Ordinary outline lost its native source grid')
    mappings = tuple(tuple(getattr(mapping, f'm{i}{j}')() for i in range(1, 4) for j in range(1, 4))
        for mapping in (native, parent))
    if not all(math.isfinite(value) for mapping in mappings for value in mapping):
        raise FeedbackUnavailable('Ordinary outline source mapping is nonfinite')
    return NativeOutlineSampling(tuple(complete.getRect()), tuple(window.getRect()), tuple(effect_region.getRect()),
        *mappings, tuple(repr(modifier_render_settings(modifier)) for modifier in modifiers))


@dataclass(frozen=True)
class NativeFeedbackRecipe:
    document: object
    identifier: str
    request: object
    prefix: QImage
    initial: QImage
    operations: tuple
    image_index: int
    byte_count: int
    source_sampling: tuple | None
    outline_sampling: NativeOutlineSampling | None = None

    @property
    def source_origin(self):
        return self.operations[self.image_index].args[0]

    @property
    def source_image(self):
        return self.operations[self.image_index].args[1]


def _single_image(operations):
    draws = [(index, operation) for index, operation in enumerate(operations) if operation.name in _DRAWS]
    if len(draws) != 1:
        raise FeedbackUnavailable('Selected filtered source requires one ordinary native image')
    index, operation = draws[0]
    if (operation.name != 'drawImage' or len(operation.args) != 2 or operation.kwargs
            or not isinstance(operation.args[0], (QPoint, QPointF))
            or not isinstance(operation.args[1], QImage) or operation.args[1].isNull()):
        raise FeedbackUnavailable('Unsupported selected native image placement')
    return index, operation.args[0], operation.args[1]


def filtered_feedback_eligible(snapshot, identifier):
    """The finite source lane shares the ordinary dependency/separability gate."""
    obj = snapshot.chapter.objects.get(identifier)
    if obj is None or not obj.modifier_ids or snapshot.document.live_preview:
        return False
    active = []
    for mid in obj.modifier_ids:
        modifier = snapshot.chapter.modifiers.get(mid)
        if modifier is None:
            return False
        if modifier.muted or (modifier.intensity <= 0 and 'intensity' not in modifier.parameter_masks):
            continue
        if not (isinstance(modifier, (BrightnessContrastModifier, CurvesModifier, HueSaturationLightnessModifier))
                or isinstance(modifier, OutlineModifier) and modifier.style == 'solid'):
            return False
        halo = footprint(modifier)
        if halo is None or not math.isfinite(halo) or halo < 0:
            return False
        active.append(modifier)
    if not active:
        return False
    # The ordinary gate owns all other graph restrictions. Do not remove or
    # independently reinterpret masks, ancestor dependencies or source frames.
    chapter = copy.copy(snapshot.chapter)
    chapter.objects = dict(chapter.objects)
    plain = copy.copy(obj)
    plain.modifier_ids = []
    chapter.objects[identifier] = plain
    if feedback_chain(replace(snapshot, chapter=chapter), identifier) is None:
        return False
    backend = DetachedSceneBackend(replace(snapshot, cache_spec=None, graphics_worker=None))
    try:
        mapping = (QTransform.fromTranslate(obj.x, obj.y)
            * backend.scene._drawing_object_transform(obj)
            * backend.scene.layer_world_transform(obj.parent_layer_id))
        return (mapping.isAffine() and mapping.isInvertible()
            and all(math.isfinite(value) for value in (mapping.m11(), mapping.m12(),
                mapping.m21(), mapping.m22(), mapping.dx(), mapping.dy())))
    finally:
        backend.close()


def capture_native_feedback_recipe(snapshot, identifier, request, *,
        budget=64 * 1024 * 1024, cancelled=lambda: False, caches=None):
    """Run on a detached owner. Reject unsupported captures before publication."""
    bounds = request.bounds
    if (not filtered_feedback_eligible(snapshot, identifier) or request.scale != 1.
            or request.output_transform is not None or request.target is not None
            or request.phase is not None or request.quality is not RenderQuality.EXACT
            or bounds != QRectF(bounds.toAlignedRect())
            or request.pixel_size != (int(bounds.width()), int(bounds.height()))
            or not isinstance(budget, int) or budget <= 0):
        raise FeedbackUnavailable('Unsupported native feedback capture')
    if bounds.width() * bounds.height() * 12 > budget:
        raise FeedbackUnavailable('Native feedback capture cannot fit its minimum resources')
    backend = DetachedSceneBackend(replace(snapshot, cache_spec=None, graphics_worker=None))
    backend.artwork_scale = 1.
    if caches is not None:
        backend.scene.adopt_cache_state(caches)
    recorder = None
    payload = _Payload(budget)
    source_sampling = []
    outline_sampling = []
    observed_source_kinds = []
    original_object, original_paint = backend.scene._render_object, backend.paint

    def render_object(painter, obj, parent_opacity, visible):
        selected = painter is recorder and obj.object_id == identifier and recorder.phase == 'before'
        if selected:
            device = recorder.painter.device()
            if not isinstance(device, QImage):
                raise FeedbackUnavailable('Selected source is not on the ordinary native device')
            recorder.prefix = payload.own(device.copy())
            recorder.boundary = len(recorder.operations)
            recorder.phase = 'selected'
        try:
            return original_object(painter, obj, parent_opacity, visible)
        finally:
            if selected:
                recorder.phase = 'after'

    def paint(painter, visible, **kwargs):
        nonlocal recorder
        recorder = _Recorder(painter, payload, cancelled)
        return original_paint(recorder, visible, **kwargs)

    def source_frame(source_identity, frame, required, mapping, float_pipeline):
        if recorder is not None and recorder.phase == 'selected':
            observed_source_kinds.append((source_identity[:3], tuple(frame.getRect()), tuple(required.getRect())))
        if (recorder is not None and recorder.phase == 'selected'
                and source_identity[:3] == ('local-tile-source', 'object', identifier)):
            source_sampling.append((tuple(required.getRect()), tuple(mapping), bool(float_pipeline),
                tuple(QRectF(frame.intersected(required).toAlignedRect()).getRect())))

    backend.scene._render_object = render_object
    backend.scene._filtered_native_frame_guard = source_frame
    def outline_frame(obj, complete, window, parent, modifiers, image):
        if recorder is not None and recorder.phase == 'selected' and obj.object_id == identifier:
            outline_sampling.append(_outline_sampling(backend, obj, complete, window, parent, modifiers, image))
    backend.scene._filtered_native_outline_identifier = identifier
    backend.scene._filtered_native_outline_guard = outline_frame
    backend.paint = paint
    try:
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        result = service.render_region(snapshot.document, request)
        if (not result.exact or recorder is None or recorder.phase != 'after'
                or recorder.prefix is None or cancelled()):
            raise FeedbackUnavailable(result.error or 'Ordinary selected capture is unavailable')
        index, _origin, _image = _single_image(recorder.selected_operations)
        placement = QRectF(_origin.x(), _origin.y(), _image.width(), _image.height())
        ordinary_outline = (len(outline_sampling) == 1 and not source_sampling
            and QRectF(*outline_sampling[0].window) == placement)
        ordinary_tile = (len(source_sampling) == 1 and not outline_sampling
            and QRectF(*source_sampling[0][3]) == placement)
        if not (ordinary_tile or ordinary_outline):
            raise FeedbackUnavailable('Selected source has no stable shared native output frame: '
                + repr((observed_source_kinds, source_sampling, _origin, _image.size()))[:1200])
        operations = (*recorder.operations[:recorder.boundary], *recorder.selected_operations,
            *recorder.operations[recorder.boundary:])
        initial = payload.own(result.image)
        # Reserve the serial worker's retained updated source and full scene
        # planes within the same prepared cap; these coexist with the recipe.
        retained_bytes = payload.byte_count + _image.sizeInBytes() + initial.sizeInBytes()
        if retained_bytes > budget:
            raise FeedbackUnavailable('Native feedback progress cannot fit its prepared byte budget')
        return NativeFeedbackRecipe(snapshot.document, identifier, request, recorder.prefix,
            initial, operations, recorder.boundary + index, retained_bytes,
            source_sampling[0] if ordinary_tile else None,
            outline_sampling[0] if ordinary_outline else None)
    finally:
        backend.scene._render_object = original_object
        backend.paint = original_paint
        backend.close()


@dataclass(frozen=True)
class FilteredRasterFeedback:
    snapshot: object
    identifier: str
    recipes: tuple

    @property
    def document(self):
        return self.snapshot.document

    @property
    def byte_count(self):
        return sum(recipe.byte_count for recipe in self.recipes)


def prepare_filtered_raster_feedback(snapshot, identifier, requests, visible, *,
        caches=None, cancelled=lambda: False, budget=FILTERED_PREPARED_BUDGET):
    """Prepare bounded ordinary native blocks with their original exact demand."""
    from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
    from comic_editor.render.service import RenderRequest
    if not filtered_feedback_eligible(snapshot, identifier) or visible is None:
        return None
    groups = {}
    for request in requests:
        if request.address.level != 0 or request.tile_size != 256 or request.gutter != 2:
            return None
        key = (0, request.address.x // 4, request.address.y // 4)
        groups.setdefault(key, []).append(request)
    center = QRectF(*visible).center()
    recipes = []
    used = 0
    for key in sorted(groups, key=lambda item:
            ((item[1]+.5)*1024-center.x())**2 + ((item[2]+.5)*1024-center.y())**2):
        if cancelled():
            return None
        requested = QRectF()
        for request in groups[key]:
            requested = requested.united(request.capture_rect)
        bounds = QRectF(key[1]*1024-2, key[2]*1024-2, 1028, 1028)
        if 3*1028*1028*4 > budget-used:
            break
        capture = RenderRequest(tuple(bounds.getRect()),1.,(1028,1028),key,
            snapshot.document.revision,tuple(requested.getRect()))
        try:
            recipe = capture_native_feedback_recipe(snapshot,identifier,capture,
                budget=budget-used,cancelled=cancelled,caches=caches)
        except FeedbackUnavailable:
            continue
        recipes.append(recipe)
        used += recipe.byte_count
    return FilteredRasterFeedback(snapshot,identifier,tuple(recipes)) if recipes else None


@dataclass(frozen=True)
class NativeFeedbackUpdate:
    source: QImage
    image: QImage
    revision: int
    dirty: tuple


def update_native_feedback_recipe(recipe, snapshot, dirty, *, previous_source=None,
        backend=None, cancelled=lambda: False):
    """Evaluate finite native demand, then compose on the unchanged full device.

    The caller owns dependency/revision admission and the serial worker. A
    complete immutable selected plane carries earlier coalesced changes. No
    new dirty composition clip or reduced device changes native edge rounding.
    """
    dirty = QRectF(dirty)
    if (recipe.document.identity != snapshot.document.identity
            or recipe.document.configuration != snapshot.document.configuration
            or not all(math.isfinite(value) for value in dirty.getRect()) or dirty.isEmpty()):
        raise FeedbackUnavailable('Native feedback update lost its document or finite demand')
    dirty = QRectF(dirty.toAlignedRect()).intersected(recipe.request.bounds)
    if dirty.isEmpty():
        raise FeedbackUnavailable('Native contact is outside this prepared block')
    original = recipe.source_image if previous_source is None else previous_source
    if (original.size() != recipe.source_image.size() or original.format() != recipe.source_image.format()
            or original.colorSpace() != recipe.source_image.colorSpace()
            or original.devicePixelRatio() != recipe.source_image.devicePixelRatio()):
        raise FeedbackUnavailable('Selected native source contract changed')
    owned_backend = backend is None
    if backend is None:
        backend = DetachedSceneBackend(replace(snapshot, cache_spec=None, graphics_worker=None))
    elif (backend.scene.chapter is not snapshot.chapter or backend.scene.tiles is not snapshot.tiles
            or backend.scene.images is not snapshot.images
            or backend.scene._persistent_render_cache is not None):
        raise FeedbackUnavailable('Native feedback update must retain its private source owner')
    backend.snapshot = replace(snapshot, cache_spec=None, graphics_worker=None)
    backend.scene.snapshot = backend.snapshot
    backend.scene._render_bounds.invalidate('object', recipe.identifier)
    backend.artwork_scale = 1.
    backend.native_preview = True
    backend.scene._filtered_native_contact_patch = True
    full_request = replace(recipe.request, revision=snapshot.document.revision)
    composed = recipe.prefix.copy()
    payload = _Payload(64 * 1024 * 1024)
    source_frames = []
    outline_frames = []
    def source_frame(source_identity, frame, required, mapping, float_pipeline):
        if source_identity[:3] != ('local-tile-source', 'object', recipe.identifier):
            return
        if recipe.source_sampling is None:
            raise FeedbackUnavailable('Ordinary outline changed its selected render route')
        original_required, original_mapping, original_float, original_output = recipe.source_sampling
        current_output = tuple(QRectF(frame.intersected(QRectF(*original_required)).toAlignedRect()).getRect())
        if (tuple(mapping) != original_mapping or bool(float_pipeline) != original_float
                or current_output != original_output):
            raise FeedbackUnavailable('The ordinary complete selected source frame changed')
        source_frames.append(current_output)
    backend.scene._filtered_native_frame_guard = source_frame
    def outline_frame(obj, complete, window, parent, modifiers, image):
        if obj.object_id != recipe.identifier:
            return
        original_sampling = recipe.outline_sampling
        sampling = _outline_sampling(backend, obj, complete, window, parent, modifiers, image)
        if (original_sampling is None
                or sampling != original_sampling):
            raise FeedbackUnavailable('The ordinary outline source frame or stage changed')
        outline_frames.append(sampling)
    backend.scene._filtered_native_outline_identifier = recipe.identifier
    backend.scene._filtered_native_outline_guard = outline_frame
    try:
        with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
            # The compatibility outline sampler keeps its original native
            # source window. A smaller window changes Qt fractional-source
            # rounding and its outer halo is not a valid replacement plane.
            # Only the selected object is evaluated; prefix/suffix replay still
            # avoids reevaluating unrelated scene objects.
            effect_region = (QRectF(*recipe.outline_sampling.effect_region) if recipe.outline_sampling is not None
                else dirty.adjusted(-2., -2., 2., 2.).intersected(snapshot.document.bounds))
            with backend.capture(snapshot.document, full_request, effect_region):
                painter = QPainter(composed)
                try:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
                    painter.setTransform(full_request.transform)
                    painter.setClipRect(snapshot.document.bounds)
                    recorder = _Recorder(painter, payload, cancelled, suppress_draws=True)
                    backend.paint_entity(recorder, full_request.bounds, ('object', recipe.identifier))
                finally:
                    painter.end()
                _index, origin, patch = _single_image(recorder.operations)
                ordinary_outline = (recipe.outline_sampling is not None and len(outline_frames) == 1
                    and not source_frames)
                ordinary_tile = (recipe.outline_sampling is None and len(source_frames) == 1
                    and not outline_frames)
                if not (ordinary_tile or ordinary_outline):
                    raise FeedbackUnavailable('The finite native patch lost its shared sampling frame')
                if (ordinary_outline and QRectF(*outline_frames[0].window)
                        != QRectF(origin.x(), origin.y(), patch.width(), patch.height())):
                    raise FeedbackUnavailable('Ordinary outline output placement changed')
                if (patch.format() != original.format() or patch.colorSpace() != original.colorSpace()
                        or patch.devicePixelRatio() != original.devicePixelRatio() or patch.devicePixelRatio() != 1.):
                    raise FeedbackUnavailable('Filtered native patch contract changed')
                delta = origin - recipe.source_origin
                if delta.x() != round(delta.x()) or delta.y() != round(delta.y()):
                    raise FeedbackUnavailable('Filtered native source grid changed')
                patch_bounds = QRect(round(delta.x()), round(delta.y()), patch.width(), patch.height())
                overlap = patch_bounds.intersected(original.rect())
                if overlap.isEmpty():
                    raise FeedbackUnavailable('Filtered native patch missed its original extent')
                if overlap != patch_bounds:
                    patch = patch.copy(overlap.translated(-patch_bounds.topLeft()))
                edited = original.copy()
                writer = QPainter(edited)
                try:
                    writer.setCompositionMode(QPainter.CompositionMode_Source)
                    writer.drawImage(overlap.topLeft(), patch)
                finally:
                    writer.end()
                painter = QPainter(composed)
                try:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
                    painter.setTransform(full_request.transform)
                    painter.setClipRect(snapshot.document.bounds)
                    for index, operation in enumerate(recipe.operations):
                        if cancelled():
                            raise FeedbackUnavailable('Native feedback update cancelled')
                        if index == recipe.image_index:
                            painter.drawImage(operation.args[0], edited)
                        else:
                            operation.apply(painter)
                finally:
                    painter.end()
        return NativeFeedbackUpdate(edited, composed, snapshot.document.revision, tuple(dirty.getRect()))
    finally:
        backend.scene.__dict__.pop('_filtered_native_frame_guard', None)
        backend.scene.__dict__.pop('_filtered_native_outline_guard', None)
        backend.scene.__dict__.pop('_filtered_native_outline_identifier', None)
        if owned_backend:
            backend.close()
