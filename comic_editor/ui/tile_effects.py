"""Scene-kernel adapter for the renderer's general tile evaluator."""
import copy
import math
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform

from comic_editor.core.models import (
    ArrayModifier, MirrorModifier, CageTransformModifier, DistortModifier, RadialBlurModifier,
    BlurModifier, BrightnessContrastModifier, CurvesModifier, DitheringModifier,
    HueSaturationLightnessModifier, KuwaharaModifier, OutlineModifier, PosterizeModifier,
    SharpnessModifier,
)
from comic_editor.core.effect_geometry import (effect_bounds, outline_blur_padding,
    array_indices, array_transform, reflection_transform)
from comic_editor.render.tile_graph import TileGraph, TileNode, TileCacheMiss
from comic_editor.render.blur_regions import RegionalBlur, BlurTileCache
from comic_editor.render.geometry_cache import settings_signature
from comic_editor.ui.effect_regions import region_requests_enabled, projection_requires_exact
from comic_editor.ui.modifier_rendering import (
    apply_modifier_stack, modifier_render_settings, _qimage_premultiplied,
    _premultiplied_qimage, _parameter_field,
)


_CPU_DISTORT_DEMAND_TYPES = frozenset({
    'distort_twirl', 'distort_deform', 'distort_mesh_warp',
    'distort_lens_distortion', 'distort_pinch_punch',
})
_NATIVE_OUTPUT_BATCH = 512


def _maximum(modifier, name):
    value = getattr(modifier, name)
    binding = modifier.parameter_masks.get(name)
    return max(value, binding.black_value, binding.white_value) if binding else value


def _whole_frame_stage(canvas, callback, *arguments):
    # Keep the full-stage lookup and evaluation in the same request scope.
    # Region predicates, including exact-reference captures, all require this
    # flag; restoring it in finally covers Pending, Failed, and reentrancy.
    previous = canvas._effect_region_requests
    canvas._effect_region_requests = False
    try:
        return callback(*arguments)
    finally:
        canvas._effect_region_requests = previous


def _preparation_exceeds_cache(frame):
    from comic_editor.ui.distort_rendering import PREPARED_DISTORT_CACHE_BUDGET
    # The existing native sampler prepares four float32 channels, regardless
    # of the source QImage's byte format. Oversized preparations are not cached
    # and would otherwise be rebuilt for every demanded output tile.
    return 16 * frame.width() * frame.height() > PREPARED_DISTORT_CACHE_BUDGET


def _put_graph_result(canvas, request_scope, owner, key, result):
    # Whole-frame nodes feed many regional consumers. One-use child tiles and
    # viewport output aliases must not displace that large prefix from the
    # bounded protected partition; their private assembly keeps exact progress.
    admitted = canvas._effect_jobs.retained_put(("tile-graph", request_scope, owner), key, result,
                                               shared=owner[1] == 'frame')
    # Admit the graph handoff before the ordinary alias. Its matching semantic
    # key/storage lets the LRU avoid a duplicate oversized entry; rejection
    # still keeps the ordinary exclusive alias and the existing raw handoff.
    canvas._modifier_cache_put(key, result)
    if admitted:
        canvas._effect_jobs.retained_consume(result)


def _stage_scope(request_scope, modifier_id, batch_frame):
    scope = ('tile-frame', request_scope, modifier_id)
    if batch_frame is not None:
        scope += (('native-output-batch', tuple(batch_frame.getRect())),)
    return scope


def _native_batch_frame(output, frame):
    size = _NATIVE_OUTPUT_BATCH
    return QRectF(math.floor(output.left() / size) * size,
                  math.floor(output.top() / size) * size, size, size).intersected(frame)


def _batched_stage(canvas, delegate, output, frame, *arguments):
    batch = _native_batch_frame(output, frame)
    result = _whole_frame_stage(canvas,
        lambda: delegate(batch, *arguments, batch_frame=batch))
    # Even a whole clipped edge batch gets a distinct completed crop handle.
    # Graph admission must not consume the raw full batch on its first child;
    # siblings use the ordinary completed stage key and retained handoff.
    return None if result is None else result.copy(output.translated(-batch.topLeft()).toAlignedRect())


def footprint(modifier):
    """None is an explicit complete-frame dependency, never a guessed halo."""
    if isinstance(modifier, (BrightnessContrastModifier, CurvesModifier, HueSaturationLightnessModifier)):
        return 0
    if isinstance(modifier, PosterizeModifier) and not modifier.simplify_enabled:
        return 0
    if isinstance(modifier, DitheringModifier):
        return 0
    if isinstance(modifier, (ArrayModifier, MirrorModifier)):
        return 0
    if isinstance(modifier, SharpnessModifier):
        if {"radius", "strength"}.intersection(modifier.parameter_masks):
            # The reference kernel selects uniform/interpolated/identity modes
            # from extrema across its whole parameter field. A constant tile
            # inside a varying mask must not select a different algorithm.
            return None
        radius = min(20., _maximum(modifier, "radius"))
        return math.ceil(4 * radius) + 1
    if isinstance(modifier, KuwaharaModifier):
        from comic_editor.ui.effect_pipeline import _kuwahara_region_padding
        return _kuwahara_region_padding(modifier)
    if isinstance(modifier, OutlineModifier) and modifier.style == "solid":
        return math.ceil(_maximum(modifier, "thickness") + outline_blur_padding(modifier)) + 2
    if isinstance(modifier, BlurModifier):
        return 0  # The inner pyramid graph makes its own frame-addressed requests.
    return None


def eligible(canvas, required, request_scope, bounds, modifiers):
    stroke_regions = (getattr(canvas, '_stroke_projection_active', False)
                      and any(isinstance(m, (BrightnessContrastModifier, CurvesModifier,
                                            HueSaturationLightnessModifier)) for m in modifiers)
                      and all(isinstance(m, (BrightnessContrastModifier, CurvesModifier,
                                            HueSaturationLightnessModifier))
                              or isinstance(m, OutlineModifier) and m.style == 'solid'
                              for m in modifiers if not m.muted))
    return (required is not None and request_scope is not None
            and region_requests_enabled(canvas)
            and (projection_requires_exact(canvas) or stroke_regions)
            and bounds == QRectF(bounds.toAlignedRect()) and not bounds.isEmpty()
            and bounds.width() * bounds.height() > 256 * 256
            and any(footprint(m) is not None for m in modifiers if not m.muted))


def tile_output(canvas, image, bounds, modifiers, mapping, *, required, request_scope,
                source_identity, nearest=False, capture=None, float_pipeline=False):
    if not eligible(canvas, required, request_scope, bounds, modifiers):
        return None
    from comic_editor.ui.effect_pipeline import _color_signature, render_stages, cached_stage_output, aligned
    from comic_editor.ui.async_projection import projection_deferred, projection_result_or_pending, ProjectionPending
    from comic_editor.ui.radial_pipeline import _float_image, _float_pixels
    active = [m for m in modifiers if not m.muted and (m.intensity > 0 or "intensity" in m.parameter_masks)]
    if not active:
        return None
    if float_pipeline and (any(footprint(m) is None or isinstance(m, (ArrayModifier, MirrorModifier)) for m in active)
                           or any(isinstance(m, BlurModifier) and "strength" in m.parameter_masks for m in active)
                           or all(isinstance(m, OutlineModifier) for m in active)):
        return None
    transform = canvas._modifier_mapping_signature(mapping)
    tile_size = 64 if getattr(canvas, '_stroke_projection_active', False) and not projection_requires_exact(canvas) else 256
    placement = (tuple(bounds.getRect()), transform, nearest, float_pipeline, tile_size)
    signatures = tuple((settings_signature(m, modifier_render_settings),
        canvas._modifier_parameter_signature([m.modifier_id]), _color_signature(canvas, m)) for m in active)
    storage = QImage.Format_RGBA32FPx4_Premultiplied if float_pipeline else QImage.Format_ARGB32_Premultiplied
    deferred = projection_deferred(canvas)
    jobs = canvas._effect_jobs
    def assembly_context():
        from comic_editor.render.pixels import current_contract
        chapter = getattr(canvas, 'chapter', None)
        projection = getattr(canvas, '_document_projection', None)
        contract = getattr(chapter, 'pixel_contract', None)
        return (id(chapter), id(getattr(canvas, 'tiles', None)),
                id(getattr(canvas, 'images', None)),
                getattr(canvas, '_history_generation', 0),
                getattr(projection, 'revision', 0), current_contract().signature,
                getattr(contract, 'signature', None))
    region_store = (jobs.private_regions(assembly_context(), assembly_context)
                    if deferred and (image is not None or capture is not None) else None)
    nodes = [TileNode(QRectF(bounds), (source_identity, placement), None, None, format=storage)]
    from comic_editor.ui.point_lut import _supported, point_chain
    point_start = 0
    for index, modifier in enumerate(active):
        incoming = nodes[-1].frame
        frame = QRectF(bounds) if float_pipeline else aligned(effect_bounds(incoming, [modifier], mapping))
        identity = (source_identity, placement, signatures[:index + 1], tuple(frame.getRect()))
        padding = footprint(modifier)
        transforms = None
        if isinstance(modifier, (ArrayModifier, MirrorModifier)):
            inverse, valid = mapping.inverted()
            if valid and mapping.isAffine():
                operations = ([reflection_transform(modifier)] if isinstance(modifier, MirrorModifier)
                              else [array_transform(modifier, step) for step in array_indices(modifier)])
                candidates = tuple(mapping * operation * inverse for operation in operations)
                # Qt's affine image sampler rounds against the complete image
                # origin. Integer translations can fetch disjoint regions
                # exactly; rotated/reflected/scaled copies keep that origin.
                if all(abs(t.m11()-1) < 1e-12 and abs(t.m22()-1) < 1e-12
                       and abs(t.m12()) < 1e-12 and abs(t.m21()) < 1e-12
                       and abs(t.dx()-round(t.dx())) < 1e-12 and abs(t.dy()-round(t.dy())) < 1e-12
                       for t in candidates):
                    transforms = candidates
                else:
                    padding = None
            else:
                padding = None
        if isinstance(modifier, DitheringModifier) and not float_pipeline:
            # The compatibility stage renderer resets this lattice per stage.
            padding = None
        def required_input(output, pad=padding, source_frame=incoming, effect=modifier, copies=transforms):
            if copies is not None:
                # The first image is the unchanged source at the output. Each
                # transformed copy has its own inverse-mapped sampling region.
                return (output, *(transform.inverted()[0].mapRect(output).adjusted(-2, -2, 2, 2).intersected(source_frame)
                                  for transform in copies))
            if pad is None:
                return source_frame
            # Generic float stacks have a fixed semantic frame, including for
            # outline blur: outside coverage must not enter its convolution.
            # Compatibility stages instead expand the outline's actual frame.
            needed = output.adjusted(-pad, -pad, pad, pad)
            return (needed if isinstance(effect, (OutlineModifier, BlurModifier)) and not float_pipeline
                    else needed.intersected(source_frame))
        def evaluate(output, source, source_bounds, effect=modifier, stage=index,
                     source_frame=incoming, output_frame=frame, prefix=identity, halo=padding, copies=transforms,
                     *, batch_frame=None):
            if copies is not None:
                from comic_editor.ui.effect_pipeline import empty_image
                result = empty_image(output)
                painter = QPainter(result)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
                painter.setRenderHint(QPainter.Antialiasing, not nearest)
                try:
                    for transform, image_part, rect in zip(copies, source[1:], source_bounds[1:]):
                        if rect.isEmpty():
                            continue
                        painter.setTransform(QTransform.fromTranslate(rect.x(), rect.y()) * transform
                                             * QTransform.fromTranslate(-output.x(), -output.y()))
                        painter.drawImage(0, 0, image_part)
                finally:
                    painter.end()
                pixel_mapping = canvas._world_to_image_transform(mapping, output, result.width(), result.height())
                revision = getattr(canvas, "_effect_provisional_revision", 0)
                fields = canvas._modifier_mask_fields([effect], result.width(), result.height(), pixel_mapping, mapping.mapRect(output))
                if revision != getattr(canvas, "_effect_provisional_revision", 0):
                    raise TileCacheMiss()
                amount = np.asarray(_parameter_field(effect, "intensity", effect.intensity,
                    (result.height(), result.width()), fields)) / 100.
                if amount.ndim == 2:
                    amount = amount[..., None]
                if isinstance(effect, MirrorModifier) or np.ndim(amount) != 0 or float(amount) != 1.:
                    result = _premultiplied_qimage(_qimage_premultiplied(result) * amount)
                painter = QPainter(result)
                painter.drawImage(0, 0, source[0])
                painter.end()
                return result
            if halo is None:
                return render_stages(canvas, source, source_bounds, [effect], mapping,
                    nearest=nearest, source_key=("tile-frame-input", source_identity, placement, signatures[:stage]),
                    required=output, request_scope=_stage_scope(request_scope, effect.modifier_id, batch_frame),
                    tile_evaluation=False)[0]
            work_mapping = canvas._world_to_image_transform(mapping, source_bounds, source.width(), source.height())
            revision = getattr(canvas, "_effect_provisional_revision", 0)
            fields = canvas._modifier_mask_fields([effect], source.width(), source.height(), work_mapping, mapping.mapRect(source_bounds))
            if revision != getattr(canvas, "_effect_provisional_revision", 0):
                raise TileCacheMiss()
            scope = ("tile-graph", request_scope, (stage + 1, (math.floor(output.x()/256), math.floor(output.y()/256))))
            key = ("effect-tile", prefix, tuple(output.getRect()))
            if isinstance(effect, BlurModifier):
                key_prefix = ("blur-level", source_identity, placement, signatures[:stage], tuple(output_frame.getRect()), effect.algorithm)
                def fetch(region):
                    x, y, width, height = region
                    rect = QRectF(output_frame.x()+x, output_frame.y()+y, width, height)
                    fetched = graph.region(stage, rect)
                    pixels = _float_pixels(fetched) if float_pipeline else _qimage_premultiplied(fetched)
                    return np.ascontiguousarray(np.clip(pixels * 255., 0., 255.).astype(np.uint8))
                cache = getattr(canvas, "_regional_blur_cache", None)
                if cache is None:
                    cache = canvas._regional_blur_cache = BlurTileCache()
                def get_level(address):
                    return cache.get((key_prefix, address))
                def put_level(address, pixels):
                    cache.put((key_prefix, address), pixels)
                blur = RegionalBlur((int(output_frame.width()), int(output_frame.height())), fetch, get_level, put_level, effect.algorithm)
                original = _float_pixels(source) if float_pipeline else _qimage_premultiplied(source)
                strength = _parameter_field(effect, "strength", effect.strength, original.shape[:2], fields)
                if np.ndim(strength) == 0 and float(strength) <= 1e-6:
                    return _float_image(original) if float_pipeline else _premultiplied_qimage(original)
                region = (int(output.x()-output_frame.x()), int(output.y()-output_frame.y()), source.width(), source.height())
                amount = np.asarray(_parameter_field(effect, "intensity", effect.intensity, original.shape[:2], fields), np.float32) / 100.
                if effect.mode == "focal":
                    origin = mapping.map(output_frame.topLeft())
                    # The compatibility kernel adds image coordinates to its
                    # world origin, including when the parent is rotated.
                    xx = np.arange(source.width(), dtype=np.float32) + int(source_bounds.x()-output_frame.x())
                    yy = np.arange(source.height(), dtype=np.float32) + int(source_bounds.y()-output_frame.y())
                    xx = xx + origin.x() + .5
                    yy = yy + origin.y() + .5
                    distance = np.hypot(xx[None, :] - effect.focal_center[0], yy[:, None] - effect.focal_center[1])
                    inner = effect.focal_radius * effect.focal_ramp
                    amount = amount * np.clip((distance-inner)/max(1e-6, effect.focal_radius-inner), 0., 1.)
                if amount.ndim == 2:
                    amount = amount[..., None]
                memory = 0
                if projection_deferred(canvas):
                    blur, memory = blur.detached(region, strength)
                    original = original.copy()
                    strength = np.array(strength, copy=True) if np.ndim(strength) else strength
                def compute_blur(cancelled=None):
                    if cancelled is not None and cancelled():
                        return None
                    filtered = blur.apply(region, strength)
                    result = filtered if amount.ndim == 0 and float(amount) == 1. else original*(1.-amount)+filtered*amount
                    if cancelled is not None and cancelled():
                        return None
                    return _float_image(result) if float_pipeline else _premultiplied_qimage(np.clip(result, 0., 1.))
                if projection_deferred(canvas):
                    canvas._effect_jobs.request(scope, key, compute_blur, memory + 16*int(source.sizeInBytes()),
                        allow_oversized=True, require_exact=True)
                    raise ProjectionPending(scope, key)
                return compute_blur()
            effect_copy = copy.deepcopy(effect)
            cached = projection_result_or_pending(canvas, scope, key) if projection_deferred(canvas) else None
            if cached is not None:
                return cached
            def compute(cancelled=None):
                origin = mapping.map(output_frame.topLeft())
                world_origin = (origin.x()+source_bounds.x()-output_frame.x(), origin.y()+source_bounds.y()-output_frame.y())
                result = apply_modifier_stack(source, [effect_copy], world_origin, fields,
                    world_to_image=work_mapping, nearest=nearest, cancelled=cancelled,
                    original_pixels=_float_pixels(source) if float_pipeline else None,
                    return_pixels=float_pipeline,
                    pixel_origin=(source_bounds.x()-source_frame.x(), source_bounds.y()-source_frame.y()))
                if float_pipeline and result is not None:
                    result = _float_image(result)
                crop = QRectF(output)
                crop.translate(-source_bounds.topLeft())
                return None if result is None else result.copy(crop.toAlignedRect())
            if projection_deferred(canvas) and halo > 0:
                canvas._effect_jobs.request(scope, key, compute, 16*int(source.sizeInBytes()),
                    allow_oversized=True, require_exact=True)
                raise ProjectionPending(scope, key)
            return compute()
        regional_spatial = isinstance(modifier, (ArrayModifier, MirrorModifier, CageTransformModifier, RadialBlurModifier, DistortModifier))
        if isinstance(modifier, (ArrayModifier, MirrorModifier)) and transforms is None:
            regional_spatial = False
        if isinstance(modifier, DistortModifier) and modifier.modifier_type == "distort_smudge":
            regional_spatial = False
        input_index = None
        cached_output = None
        if padding is None and transforms is None:
            def cached_output(output, effect=modifier, stage=index, source_frame=incoming, *, batch_frame=None):
                completed = cached_stage_output(canvas, source_frame, [effect], mapping,
                    nearest=nearest, source_key=("tile-frame-input", source_identity, placement, signatures[:stage]),
                    required=output, request_scope=_stage_scope(request_scope, effect.modifier_id, batch_frame),
                    tile_evaluation=False)
                return None if completed is None else completed[0]
        if _supported(modifier):
            # Float intermediates after another effect need their exact input
            # values; a byte-indexed table is eligible only at the source.
            if index - point_start >= 1 and (not float_pipeline or point_start == 0):
                effects = tuple(copy.deepcopy(m) for m in active[point_start:index + 1])
                input_index = point_start
                def evaluate_points(output, source, source_bounds, fused=effects,
                                    prefix=identity, source_stage=point_start):
                    pixels = _float_pixels(source) if float_pipeline else _qimage_premultiplied(source)
                    result = point_chain(source, pixels, fused, quantize_stages=not float_pipeline,
                        canonical_input=float_pipeline,
                        source_key=('tile-point-input', source_identity, placement,
                                    signatures[:source_stage], tuple(source_bounds.getRect())))
                    if result is None:
                        for operation in fused:
                            if float_pipeline:
                                pixels = apply_modifier_stack(source, [operation], (0, 0),
                                    original_pixels=pixels, return_pixels=True, _point_lut=False)
                            else:
                                source = apply_modifier_stack(source, [operation], (0, 0), _point_lut=False)
                        return _float_image(pixels) if float_pipeline else source
                    return _float_image(result) if float_pipeline else _premultiplied_qimage(result)
                evaluate = evaluate_points
                cached_output = None
                required_input = lambda output: output
        else:
            point_start = index + 1
        # A partial output plus its complete spatial predecessor must fit in
        # the existing retained pool. A later complete-source stage also needs
        # this entire output: if native source preparation cannot be retained,
        # evaluating its tiles separately repeatedly converts that full input.
        # Its complete output is also the next stage's sampling preparation:
        # a small-input, expanded Lens frame must be produced once rather than
        # assembling hundreds of output tiles before that larger preparation.
        # Promote these exact demands to the established whole-frame kernel
        # and exclusive worker admission. The
        # native source/effect grid and settings remain identical; only demand
        # granularity changes. Once a worker owns a completed COW predecessor,
        # the private checkpoint can leave the pool rather than pin ancestors.
        pair_bytes = int((frame.width()*frame.height() + incoming.width()*incoming.height())
                         * (16 if float_pipeline else 4))
        pressure_frame = bool(deferred and padding is None and regional_spatial
                              and transforms is None
                              # Radial's 96px output-local integration blocks
                              # choose sample counts from each block's extent.
                              # A larger demand would change native pixels.
                              and not isinstance(modifier, RadialBlurModifier)
                              # Cage's native GPU normalizes vertex positions
                              # by each FBO extent; exact demand equivalence has
                              # only been proved for these CPU distort kernels.
                              and isinstance(modifier, DistortModifier)
                              and modifier.modifier_type in _CPU_DISTORT_DEMAND_TYPES
                              and frame.width()*frame.height() <= 64 * 1024 * 1024
                              and (pair_bytes + 262144 > jobs.retained_budget
                                   or (any(footprint(later) is None for later in active[index + 1:])
                                       and (_preparation_exceeds_cache(incoming)
                                            or _preparation_exceeds_cache(frame)))))
        if pressure_frame:
            ordinary_evaluate = evaluate
            def evaluate(output, source, needed, delegate=ordinary_evaluate):
                # The promoted full stage must not re-enter a second regional
                # mesh assembler. Use the same existing exact whole kernel;
                # restore policy even when its detached worker yields/fails.
                return _whole_frame_stage(canvas, delegate, output, source, needed)
            if cached_output is not None:
                ordinary_cached = cached_output
                def cached_output(output, delegate=ordinary_cached):
                    return _whole_frame_stage(canvas, delegate, output)
        elif (deferred and padding is None and regional_spatial and transforms is None
              and isinstance(modifier, DistortModifier)
              and modifier.modifier_type in _CPU_DISTORT_DEMAND_TYPES
              and not getattr(canvas, '_last_modifier_provisional', False)
              and _preparation_exceeds_cache(incoming)
              and frame.width()*frame.height() <= 64 * 1024 * 1024):
            # Reuse one bounded native 512px output when this sampler's full
            # input preparation cannot fit its unchanged cache. Canonical graph
            # addresses, exact tile keys, source grids and pixel density stay
            # unchanged; only a completed stage demand is cropped for siblings.
            ordinary_evaluate = evaluate
            def evaluate(output, source, needed, delegate=ordinary_evaluate, node_frame=QRectF(frame)):
                return _batched_stage(canvas, delegate, output, node_frame, source, needed)
            if cached_output is not None:
                ordinary_cached = cached_output
                def cached_output(output, delegate=ordinary_cached, node_frame=QRectF(frame)):
                    return _batched_stage(canvas, delegate, output, node_frame)
        nodes.append(TileNode(frame, identity, required_input, evaluate,
                              shared_frame=padding is None and (not regional_spatial or pressure_frame), format=storage,
                              input_index=input_index, cached_output=cached_output))
    def get(owner, key):
        scope = ("tile-graph", request_scope, owner)
        retained = canvas._effect_jobs.retained_get(scope, key)
        result = retained[0] if retained is not None else canvas._modifier_cache_get(key)
        if result is None and projection_deferred(canvas) and key[0] == "effect-tile":
            result = projection_result_or_pending(canvas, scope, key)
        return result
    def put(owner, key, result):
        _put_graph_result(canvas, request_scope, owner, key, result)
    def source(region):
        if capture is not None:
            result = capture(region)
        else:
            crop = QRectF(region)
            crop.translate(-bounds.topLeft())
            result = image.copy(crop.toAlignedRect())
        return _float_image(_qimage_premultiplied(result)) if float_pipeline else result
    def continue_pending():
        jobs = canvas._effect_jobs
        # Queue only enough independent neighbors to occupy existing workers.
        # Snapshot admission still owns the combined byte limit, including an
        # exclusive oversized job. Contact previews never enter this path.
        count = len(jobs.running_jobs) + len(jobs.pending)
        return (count < jobs.worker_limit and jobs.bytes_in_flight < jobs.budget
                and not any(job[4] > jobs.budget for job in jobs.running_jobs))
    graph = TileGraph(nodes, source, get, put, cache_only=image is None and capture is None,
                      tile_size=tile_size,
                      continue_pending=continue_pending if deferred else None,
                      region_store=region_store)
    try:
        output_bounds = QRectF(nodes[-1].frame.intersected(required).toAlignedRect())
        output_owner = ("output", tuple(output_bounds.getRect()))
        output_key = ("tile-output", nodes[-1].identity, tuple(output_bounds.getRect()))
        completed = get(output_owner, output_key)
        if completed is not None:
            return completed, output_bounds
        result, region = graph.output(required)
        if float_pipeline:
            result = _premultiplied_qimage(np.clip(_float_pixels(result), 0., 1.))
        put(output_owner, output_key, result)
        return result, region
    except TileCacheMiss:
        return None


def generic_target_output(canvas, target, bounds, modifiers, mapping, visible):
    """Capture only demanded scene tiles without changing generic-stack rounding."""
    from comic_editor.core.models import LayerNode, VectorDrawingObject
    from comic_editor.ui.effect_pipeline import empty_image
    from comic_editor.ui.modifier_rendering import apply_opacity_mask
    layer = isinstance(target, LayerNode)
    kind, identifier = ("layer", target.layer_id) if layer else ("object", target.object_id)
    inverse, valid = mapping.inverted()
    if not valid:
        return False
    required = (inverse.mapRect(canvas._modifier_viewport_region(visible)) if layer
                else inverse.mapRect(canvas._modifier_viewport_region(QRectF()))
                if not canvas._modifier_viewport_region(QRectF()).isEmpty() else visible)
    scope = canvas._effect_request_scope(kind, identifier)
    if not eligible(canvas, required, scope, bounds, modifiers):
        return False
    if (any(footprint(m) is None or isinstance(m, (ArrayModifier, MirrorModifier)) for m in modifiers if not m.muted)
            or any(isinstance(m, BlurModifier) and "strength" in m.parameter_masks for m in modifiers if not m.muted)
            or all(isinstance(m, OutlineModifier) for m in modifiers)):
        return False
    signature = canvas._modifier_layer_signature(identifier) if layer else canvas._modifier_object_signature(target)
    source_identity = ("local-tile-source", kind, identifier, signature[0], signature[3], signature[4],
                       canvas._render_exclude_text, canvas._modifier_mapping_signature(mapping))
    from comic_editor.ui.scene_render_backend import source_capture_key
    source_identity = source_capture_key(canvas, source_identity)
    def capture(region):
        revision = getattr(canvas, "_effect_provisional_revision", 0)
        result = empty_image(region)
        source = QPainter(result)
        source.setRenderHint(QPainter.Antialiasing, True)
        source.translate(-region.x(), -region.y())
        canvas._render_modifier_sources.add((kind, identifier))
        try:
            if layer:
                canvas._render_layer(source, target, 1., mapping.mapRect(region))
                drawing = canvas._active_vector_drawing()
                ancestors = ([item.layer_id for item in canvas.chapter.ancestor_layers(drawing.parent_layer_id)
                              if canvas._has_active_modifiers(item.modifier_ids)] if drawing is not None else [])
                if drawing is not None and not canvas._has_active_modifiers(drawing.modifier_ids) and ancestors and ancestors[-1] == identifier:
                    canvas._render_modified_vector_pencil_preview(source, target.parent_id or "")
            else:
                canvas._render_object_content(source, target, region)
                if isinstance(target, VectorDrawingObject):
                    canvas._render_modified_vector_pencil_preview(source, target.parent_layer_id)
        finally:
            canvas._render_modifier_sources.discard((kind, identifier))
            source.end()
        if revision != getattr(canvas, "_effect_provisional_revision", 0):
            raise TileCacheMiss()
        return result
    rendered = tile_output(canvas, None, bounds, modifiers, mapping, required=required,
        request_scope=scope, source_identity=source_identity, capture=capture, float_pipeline=True)
    if rendered is None:
        return False
    processed, output = rendered
    if target.opacity_mask is not None:
        binding = target.opacity_mask
        processed = apply_opacity_mask(processed, canvas.render_tone_mask_field(binding.mask_id,
            processed.width(), processed.height(), canvas._world_to_image_transform(mapping, output,
                processed.width(), processed.height()), mapping.mapRect(output)), binding.black_value, binding.white_value)
    return processed, output
