"""Stage-wise effect rendering with explicit image placement."""
import math
import copy
from dataclasses import dataclass
import numpy as np
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QTransform
from comic_editor.core.models import (ArrayModifier, MirrorModifier, RadialBlurModifier,
    CageTransformModifier, PosterizeModifier, HalftoneModifier, PixelateModifier,
    OutlineModifier, DistortModifier, BlurModifier, TextureModifier)
from comic_editor.core.color_smoothing import simplify_padding
from comic_editor.core.models import KuwaharaModifier, DitheringModifier, SharpnessModifier
from comic_editor.core.effect_geometry import effect_bounds, reflection_transform, array_indices, array_transform, array_input_bounds, outline_blur_padding
from comic_editor.ui.modifier_rendering import apply_modifier_stack, _qimage_premultiplied, _premultiplied_qimage, _parameter_field, modifier_render_settings
from comic_editor.ui.effect_regions import (
    pointwise_output, region_scope, region_requests_enabled, projection_requires_exact,
    exact_reference_sampling,
)
from comic_editor.ui.async_projection import (
    ProjectionPending, projection_deferred, projection_result_or_pending,
)
from comic_editor.render.pixels import current_contract
from comic_editor.render.geometry_cache import settings_signature


REGIONAL_HALFTONE_MIN_PIXELS = 2_000_000


def _kuwahara_region_padding(modifier):
    # A native-scale, single-pass filter has finite support. Reduced-scale
    # filtering resamples against the complete source frame instead.
    if modifier.processing_scale != 100 or modifier.iterations != 1:
        return None
    binding = modifier.parameter_masks.get("size")
    size = max(modifier.size, binding.black_value, binding.white_value) if binding else modifier.size
    sample = size * (1 + modifier.anisotropy / 100 if modifier.variant == "anisotropic" else 1)
    orientation = (4 * modifier.tensor_radius + 5
                   if modifier.variant == "anisotropic" and modifier.anisotropy > 0 else 0)
    return math.ceil(max(sample + 2, orientation))


def aligned(bounds):
    # Qt's integer rectangles wrap outside this range. Report an oversized
    # bake instead of producing a corrupt image after cumulative array scale.
    if any(not math.isfinite(v) or abs(v) > 1_000_000_000 for v in
           (bounds.left(), bounds.top(), bounds.right(), bounds.bottom())):
        raise ValueError("Effect bounds are too large to render. Reduce the count, scale offset, or spacing.")
    return QRectF(bounds.toAlignedRect())


def _empty_image_size(bounds):
    width, height = max(1, math.ceil(bounds.width())), max(1, math.ceil(bounds.height()))
    if width * height > 64 * 1024 * 1024:
        raise ValueError("Effect bounds are too large to render. Reduce the effect or move its axis/center closer to the artwork.")
    return width, height


def empty_image(bounds):
    width, height = _empty_image_size(bounds)
    image = QImage(width, height, current_contract().image_format)
    if image.isNull():
        raise MemoryError("Could not allocate effect image")
    image.fill(Qt.transparent)
    return image


def _pattern_draft_source(source, modifier):
    """Choose the bounded working image before sampling any parameter maps."""
    complex_pattern = isinstance(modifier, HalftoneModifier) and (
        modifier.grid_type in {"radial", "stippling"} or modifier.size > 1.5
        or modifier.dot_style in {"blob", "liquid", "delaunay"})
    edge, pixels = (96, 4096) if complex_pattern else (128, 8192)
    scale = min(1., edge / max(source.width(), source.height()),
                math.sqrt(pixels / (source.width() * source.height())))
    width, height = max(1, round(source.width() * scale)), max(1, round(source.height() * scale))
    image = source.scaled(width, height, Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
    draft = copy.deepcopy(modifier)
    if isinstance(draft, PixelateModifier):
        draft.pixel_size *= scale
        draft.blur *= scale
    return image, draft


def _pattern_draft(source, modifier, fields, color_source):
    """Bound interactive fallback work while the exact CPU image is pending."""
    from comic_editor.ui.modifier_rendering import apply_pattern_modifier
    image, draft = _pattern_draft_source(source, modifier)
    width, height = image.width(), image.height()
    colors = (color_source.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
              if color_source is not None else None)
    # Pattern masks currently bind intensity. Preserve their source-frame
    # coordinates in the draft; the worker retains the original full field.
    xs = np.minimum(((np.arange(width) + .5) * source.width() / width).astype(int), source.width() - 1)
    ys = np.minimum(((np.arange(height) + .5) * source.height() / height).astype(int), source.height() - 1)
    masks = {key: value[ys[:, None], xs[None, :]] for key, value in fields.items()
             if np.shape(value) == (source.height(), source.width())}
    result = apply_pattern_modifier(image, draft, masks, color_source=colors)
    return result


def _color_signature(canvas, modifier):
    if isinstance(modifier, HalftoneModifier) and modifier.color_mode == "target_layer":
        if getattr(canvas, "_rendering_halftone_source", False):
            return ("incoming-colors",)
        from comic_editor.ui.halftone_source import source_signature
        return source_signature(canvas, modifier.target_layer_id)
    return ()


@dataclass(frozen=True)
class _StagePlan:
    signatures: tuple
    transform_signature: tuple
    placement: tuple
    geometry: tuple
    targets: tuple
    origins: tuple
    key: tuple


def _stage_plan(canvas, bounds, modifiers, local_to_world, source_identity, nearest, required):
    """Describe exact stage dependencies without allocating source pixels."""
    initial_bounds = canvas._rect_signature(bounds)
    transform_signature = tuple(getattr(local_to_world, f"m{i}{j}")()
                                for i in range(1, 4) for j in range(1, 4))
    signatures = tuple((settings_signature(modifier, modifier_render_settings),
                        canvas._modifier_parameter_signature([modifier.modifier_id]),
                        _color_signature(canvas, modifier)) for modifier in modifiers)
    placement = (initial_bounds, transform_signature, nearest)
    requirements = [None] * len(modifiers)
    if required is not None:
        needed = QRectF(required)
        for index in range(len(modifiers) - 1, -1, -1):
            requirements[index] = needed
            if isinstance(modifiers[index], KuwaharaModifier):
                padding = _kuwahara_region_padding(modifiers[index])
                if padding is not None:
                    needed = needed.adjusted(-padding, -padding, padding, padding)
                    continue
            if (isinstance(modifiers[index], TextureModifier)
                    or isinstance(modifiers[index], BlurModifier) and region_requests_enabled(canvas)
                    or isinstance(modifiers[index], (KuwaharaModifier, SharpnessModifier, DitheringModifier))
                    or isinstance(modifiers[index], OutlineModifier) and modifiers[index].style == "brush"):
                # The existing blur pyramid is phased by the full image
                # dimensions (including odd-size ceil-halving). A padded
                # crop is not equivalent. Kuwahara/sharpen need the complete
                # neighborhood and dithering needs a stable lattice origin.
                # Brush outlines must trace complete contours so cropping does
                # not invent boundaries or restart material repeat patterns.
                # Retain this stage and its incoming
                # frame intact; later pointwise stages may crop its output.
                requirements[index] = None
                break
            if isinstance(modifiers[index], (CageTransformModifier, HalftoneModifier, PixelateModifier, DistortModifier)):
                # A displaced cage can pull source pixels from anywhere in the
                # incoming stage. Pattern effects also need the full frame:
                # cropping first changes their grid origin and reference scale.
                # A smudge computes its full stroke history even for a crop.
                # Retain that output so a camera move can reuse the exact pixels.
                if (isinstance(modifiers[index], DistortModifier)
                        and modifiers[index].modifier_type == "distort_smudge"):
                    requirements[index] = None
                break
            if isinstance(modifiers[index], ArrayModifier) and not modifiers[index].muted:
                needed = array_input_bounds(needed, modifiers[index], local_to_world)
            else:
                needed = effect_bounds(needed, [modifiers[index]], local_to_world)
            if isinstance(modifiers[index], PosterizeModifier) and not modifiers[index].muted:
                padding = simplify_padding(modifiers[index])
                needed = needed.adjusted(-padding, -padding, padding, padding)
    # The camera request is not a pixel dependency when it leaves every actual
    # stage extent unchanged. Keep the concrete prefix extents instead: an
    # earlier crop can change later pixels even if a later warp expands back
    # to the same rectangle. Plan once, then use these rectangles for rendering.
    geometry, targets, origins = [], [], []
    incoming_bounds = QRectF(bounds)
    for index, modifier in enumerate(modifiers):
        # Cache placement signatures retain their established rounding, but
        # source sampling origins must keep the complete mapped coordinates.
        origins.append(tuple(local_to_world.map(incoming_bounds.topLeft()).toTuple()))
        skipped = (modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks
                   or isinstance(modifier, RadialBlurModifier) and modifier.angle <= 0
                   and "angle" not in modifier.parameter_masks)
        target = QRectF(incoming_bounds) if skipped else effect_bounds(incoming_bounds, [modifier], local_to_world)
        if not skipped:
            if requirements[index] is not None:
                target = target.intersected(requirements[index])
            target = aligned(target)
        geometry.append((canvas._rect_signature(incoming_bounds), canvas._rect_signature(target)))
        targets.append(target)
        incoming_bounds = target
    geometry = tuple(geometry)
    pipeline_key = ("stage-stack", source_identity, signatures, placement, geometry)
    return _StagePlan(signatures, transform_signature, placement, geometry, tuple(targets), tuple(origins), pipeline_key)


def _checkpoint_enabled(canvas, request_scope, provisional=False):
    return (request_scope is not None and (canvas._interactive_render or exact_reference_sampling(canvas))
            and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator"
            and not provisional and not canvas._render_base_alpha
            and canvas._rendering_mask_contributor <= 0)


def _reject_deferred_provisional(canvas, provisional, scope=None):
    # A live descendant or mask can make an exact capture's input provisional.
    # Waiting for its next complete capture is safe; forcing native kernels
    # inline cannot turn those draft pixels into an exact dependency.
    if (provisional and projection_requires_exact(canvas)
            and getattr(canvas, '_projection_defer_effects', False)
            and canvas._interactive_render):
        raise ProjectionPending(scope)


def _draft_image_prefix(canvas, source_key, modifiers):
    """Only a compact original-image capture proves provisional source pixels.

    A live descendant or contributor may improve without a model edit. Such
    inputs cannot reuse a provisional stage just because their model key is
    unchanged. Original image content and paint-only parameter fields have no
    pending derived dependency; their established semantic keys are sufficient.
    """
    from comic_editor.core.models import ImageObject
    from comic_editor.ui.thumbnail_effects import live_effect_draft, compact_effects_supported
    if (not live_effect_draft(canvas) or not compact_effects_supported(modifiers)
            or not isinstance(source_key, tuple) or len(source_key) != 3
            or source_key[0] != 'live-effect-draft-source'):
        return False
    original = source_key[2]
    obj = (canvas.chapter.objects.get(original[2])
           if isinstance(original, tuple) and len(original) > 2
           and original[:2] == ('mirror-source', 'object') else None)
    # Fitted images sample the parent's effective shape bounds. An edit can
    # change their destination quad without changing the aligned capture frame;
    # those sources need a fresh stage until that geometry is an explicit key.
    return (isinstance(original, tuple) and len(original) > 2
            and original[:2] == ('mirror-source', 'object')
            and isinstance(obj, ImageObject) and obj.placement_mode != 'fit_parent')


def _paint_only_stage_masks(canvas, modifier):
    if isinstance(modifier, HalftoneModifier) and modifier.color_mode == 'target_layer':
        return False
    for binding in modifier.parameter_masks.values():
        mask = canvas.chapter.masks.get(binding.mask_id)
        if (mask is None or mask.contributors or mask.gradient is not None
                or mask.limited_gradients):
            return False
    return True


def cached_stage_output(canvas, bounds, modifiers, local_to_world, *, nearest=False,
                        required=None, request_scope=None, source_key=None,
                        tile_evaluation=True):
    """Return a completed exact checkpoint before recapturing unchanged artwork.

    The caller must supply the same semantic source key and capture bounds used
    by render_stages. Partial checkpoints still require their incoming source;
    drafts and special-purpose captures never qualify for this fast path.
    """
    if source_key is None or not _checkpoint_enabled(canvas, request_scope):
        return None
    input_revision = getattr(canvas, '_effect_provisional_revision', 0)
    if tile_evaluation:
        from comic_editor.ui.tile_effects import tile_output
        tiled = tile_output(canvas, None, bounds, modifiers, local_to_world,
            required=required, request_scope=request_scope, source_identity=source_key, nearest=nearest)
        _reject_deferred_provisional(canvas,
            input_revision != getattr(canvas, '_effect_provisional_revision', 0), request_scope)
        if tiled is not None:
            return tiled
    plan = _stage_plan(canvas, bounds, modifiers, local_to_world, source_key, nearest, required)
    regional = pointwise_output(canvas, None, bounds, modifiers, local_to_world,
        required=required, signatures=plan.signatures, placement=plan.placement,
        source_identity=source_key, request_scope=request_scope, nearest=nearest)
    _reject_deferred_provisional(canvas,
        input_revision != getattr(canvas, '_effect_provisional_revision', 0), request_scope)
    if regional is not None:
        return regional
    checkpoint_scope = region_scope(canvas, request_scope, plan.targets[-1] if plan.targets else bounds)
    checkpoint = canvas._effect_jobs.retained_get(("pipeline", checkpoint_scope), plan.key)
    if checkpoint is not None:
        image, (completed, placement) = checkpoint
        if completed == len(modifiers):
            return image, QRectF(placement)
    # A spatial graph has not necessarily resumed far enough to write its
    # pipeline checkpoint. Its one-stage worker result still has the same
    # semantic key and can be adopted without capturing the complete input.
    if not tile_evaluation and len(modifiers) == 1:
        modifier, target = modifiers[0], plan.targets[0]
        if (not modifier.muted and (modifier.intensity > 0 or "intensity" in modifier.parameter_masks)
                and not target.isEmpty()):
            key = _stage_key(canvas, plan, source_key, 0, local_to_world, nearest)
            scope = region_scope(canvas, (*request_scope, modifier.modifier_id), target)
            cached = canvas._modifier_cache_get(key)
            if cached is None:
                cached = (projection_result_or_pending(canvas, scope, key) if projection_deferred(canvas)
                          else canvas._effect_jobs.result(scope, key))
            if cached is not None:
                return cached, QRectF(target)
    return None


def _stage_key(canvas, plan, source_identity, index, local_to_world, nearest):
    incoming, target = plan.geometry[index]
    upstream = ("stage-input", source_identity, plan.signatures[:index], plan.placement,
                plan.geometry[:index])
    return ("stage", upstream, incoming, target,
            plan.signatures[index][0], plan.signatures[index][1],
            plan.origins[index], nearest,
            plan.signatures[index][2], plan.transform_signature)


def render_stages(canvas, image, bounds, modifiers, local_to_world, *, nearest=False,
                  required=None, request_scope=None, provisional=False, source_key=None,
                  tile_evaluation=True):
    _reject_deferred_provisional(canvas, provisional, request_scope)
    input_revision = getattr(canvas, '_effect_provisional_revision', 0)
    bounds = QRectF(bounds)
    exact = projection_requires_exact(canvas)
    reference = exact_reference_sampling(canvas)
    deferred = projection_deferred(canvas)
    source_identity = source_key if source_key is not None else int(image.cacheKey())
    if tile_evaluation and not provisional:
        from comic_editor.ui.tile_effects import tile_output
        tiled = tile_output(canvas, image, bounds, modifiers, local_to_world,
            required=required, request_scope=request_scope, source_identity=source_identity, nearest=nearest)
        _reject_deferred_provisional(canvas,
            input_revision != getattr(canvas, '_effect_provisional_revision', 0), request_scope)
        if tiled is not None:
            return tiled
    plan = _stage_plan(canvas, bounds, modifiers, local_to_world, source_identity, nearest, required)
    if not provisional:
        regional = pointwise_output(canvas, image, bounds, modifiers, local_to_world,
            required=required, signatures=plan.signatures, placement=plan.placement,
            source_identity=source_identity, request_scope=request_scope, nearest=nearest)
        _reject_deferred_provisional(canvas,
            input_revision != getattr(canvas, '_effect_provisional_revision', 0), request_scope)
        if regional is not None:
            return regional
    signatures, transform_signature = plan.signatures, plan.transform_signature
    placement, geometry, targets = plan.placement, plan.geometry, plan.targets
    pipeline_key = plan.key
    navigator = (canvas._interactive_render
                 and getattr(canvas, "_effect_preview_channel", "canvas") == "navigator")
    contact = bool(getattr(canvas, "_stroke_projection_active", False))
    base_request_scope = request_scope
    request_scope = region_scope(canvas, request_scope, plan.targets[-1] if plan.targets else bounds)
    checkpoint_scope = ("pipeline", request_scope)
    checkpointing = _checkpoint_enabled(canvas, request_scope, provisional)
    draft_prefix = provisional and _draft_image_prefix(canvas, source_key, modifiers)
    start = 0
    inverse, valid = local_to_world.inverted()
    if checkpointing:
        checkpoint = canvas._effect_jobs.retained_get(checkpoint_scope, pipeline_key)
        if checkpoint is not None:
            image, (start, bounds) = checkpoint
            bounds = QRectF(bounds)
        else:
            canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (0, QRectF(bounds)))
    for index, modifier in enumerate(modifiers):
        if index < start:
            continue
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if isinstance(modifier, RadialBlurModifier) and modifier.angle <= 0 and "angle" not in modifier.parameter_masks:
            continue
        target = targets[index]
        if target.isEmpty():
            image, bounds = empty_image(QRectF(0, 0, 1, 1)), target
            continue
        draft_prefix = draft_prefix and _paint_only_stage_masks(canvas, modifier)
        # A semantic capture key survives source-LRU eviction. Only the exact
        # upstream prefix contributes, so editing a later slider still reuses
        # every earlier stage.
        upstream_key = ("stage-input", source_identity, signatures[:index], placement,
                        geometry[:index]) if source_key is not None else int(image.cacheKey())
        key = (_stage_key(canvas, plan, source_identity, index, local_to_world, nearest)
               if source_key is not None else
               ("stage", upstream_key, canvas._rect_signature(bounds), canvas._rect_signature(target),
                signatures[index][0], signatures[index][1],
                tuple(local_to_world.map(bounds.topLeft()).toTuple()), nearest, signatures[index][2],
                transform_signature))
        stage_base_scope = (*base_request_scope, modifier.modifier_id) if base_request_scope is not None else None
        stage_scope = region_scope(canvas, stage_base_scope, target)
        regional_halftone = (
            exact and region_requests_enabled(canvas) and required is not None
            and isinstance(modifier, HalftoneModifier)
            and modifier.grid_type in {"square", "hexagonal"}
            and modifier.dot_style != "delaunay"
            and modifier.color_mode != "target_layer"
            and not modifier.parameter_masks and target != bounds
            and image.width() * image.height() >= REGIONAL_HALFTONE_MIN_PIXELS
            and target.width() * target.height() < bounds.width() * bounds.height() / 2
        )
        pattern_frame_key = None
        pattern_frame_scope = None
        if (isinstance(modifier, (HalftoneModifier, PixelateModifier))
                and (region_requests_enabled(canvas) or reference)
                and not regional_halftone):
            # Pattern geometry and sampling depend on the complete incoming
            # frame. Share that exact computation across requested output
            # tiles instead of running the full kernel for every crop.
            pattern_frame_key = ("pattern-frame", upstream_key,
                canvas._rect_signature(bounds), signatures[index], transform_signature, nearest)
            shared_scope = region_scope(canvas, stage_base_scope, bounds)
            # The full-frame result has a different key from a stage result,
            # even when that stage happens to request the entire frame. Keep
            # separate scopes so retaining a stage cannot replace this image.
            pattern_frame_scope = (("pattern-frame", shared_scope)
                                   if shared_scope is not None else None)
        # Reassembled Qt handles do not invalidate deterministic compact image
        # prefixes. This separate key retains only fresh transient pixels; exact
        # requests and derived/contributor sources can never read these entries.
        live_prefix_key = ('live-effect-draft-stage', current_contract().signature, key) if draft_prefix else None
        cached = (canvas._modifier_cache_get(live_prefix_key) if live_prefix_key is not None
                  else None if provisional else canvas._modifier_cache_get(key))
        if cached is None and not provisional and stage_scope is not None:
            cached = (projection_result_or_pending(canvas, stage_scope, key) if deferred
                      else canvas._effect_jobs.result(stage_scope, key))
        if cached is None and not provisional and pattern_frame_key is not None:
            frame = canvas._modifier_cache_get(pattern_frame_key)
            if frame is None and pattern_frame_scope is not None:
                frame = (projection_result_or_pending(canvas, pattern_frame_scope, pattern_frame_key) if deferred
                         else canvas._effect_jobs.result(pattern_frame_scope, pattern_frame_key))
            if frame is not None:
                crop = QRectF(target)
                crop.translate(-bounds.topLeft())
                cached = frame.copy(crop.toAlignedRect()) if bounds != target else frame
        if cached is None and (navigator or contact) and not exact and isinstance(modifier, (HalftoneModifier, PixelateModifier)):
            # Keep cold pattern work bounded while the pen is down or the
            # destination is a thumbnail. Full-size GPU work also blocks the
            # GUI during readback. Reuse completed exact pixels above, and
            # keep this draft separate until an exact request follows.
            draft_key = ("navigator-pattern-draft" if navigator else "contact-pattern-draft", key)
            draft = canvas._modifier_cache_get(draft_key)
            if draft is None:
                from comic_editor.ui.modifier_rendering import apply_pattern_modifier
                from comic_editor.ui.halftone_source import render_color_source
                working, effect = _pattern_draft_source(image, modifier)
                mapping = canvas._world_to_image_transform(
                    local_to_world, bounds, working.width(), working.height())
                fields = canvas._modifier_mask_fields([modifier], working.width(),
                    working.height(), mapping, local_to_world.mapRect(bounds))
                revision = getattr(canvas, "_effect_provisional_revision", 0)
                colors = (render_color_source(canvas, modifier, working, bounds, local_to_world)
                          if isinstance(modifier, HalftoneModifier) else None)
                draft = apply_pattern_modifier(working, effect, fields, color_source=colors)
                if revision == getattr(canvas, "_effect_provisional_revision", 0):
                    canvas._modifier_cache_put(draft_key, draft)
            cached = draft.scaled(image.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
            if bounds != target:
                cropped = QRectF(target)
                cropped.translate(-bounds.topLeft())
                cached = cached.copy(cropped.toAlignedRect())
            image, bounds, provisional = cached, target, True
            continue
        if cached is None:
            if regional_halftone:
                from comic_editor.ui.pattern_rendering import (capture_halftone_region,
                    halftone_region, halftone_snapshot_working_bytes, render_halftone_snapshot)
                region = QRectF(target).translated(-bounds.topLeft()).toAlignedRect()
                effect = copy.deepcopy(modifier)
                snapshot = capture_halftone_region(image, effect, region)
                incoming = QImage(image) if snapshot is None else None
                size = (halftone_snapshot_working_bytes(snapshot, effect)
                        if snapshot is not None else 8 * int(incoming.sizeInBytes()))
                def compute(cancelled=None, incoming=incoming, effect=effect,
                            region=region, snapshot=snapshot):
                    result = (render_halftone_snapshot(snapshot, effect, cancelled=cancelled)
                              if snapshot is not None else
                              halftone_region(incoming, effect, region, cancelled=cancelled))
                    if effect.intensity < 100:
                        original = (snapshot.image.copy(*snapshot.output)
                                    if snapshot is not None else incoming.copy(region))
                        amount = max(0., effect.intensity / 100.)
                        result = _premultiplied_qimage(
                            _qimage_premultiplied(original) * (1. - amount)
                            + _qimage_premultiplied(result) * amount)
                    return result
                if deferred and not provisional and stage_scope is not None:
                    canvas._effect_jobs.request(stage_scope, key, compute,
                        size,
                        allow_oversized=True, require_exact=True)
                    raise ProjectionPending(stage_scope, key)
                cached = compute()
                if not provisional:
                    canvas._modifier_cache_put(key, cached)
                image, bounds = cached, target
                if checkpointing and not provisional:
                    canvas._effect_jobs.retained_put(("result", stage_scope), key, image,
                        shared=required is not None and index < len(modifiers) - 1)
                    canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key,
                        image, (index + 1, QRectF(bounds)))
                continue
            work_target = target
            pattern = isinstance(modifier, (HalftoneModifier, PixelateModifier))
            if pattern:
                work_target = bounds
            outline = isinstance(modifier, OutlineModifier)
            if outline:
                # Width, opacity, color and viewport edits share the same alpha
                # source and exact distance field. Crop only the finished stage;
                # changing its input padding would force another distance build.
                padding = 25 + outline_blur_padding(modifier)
                work_target = aligned(bounds.adjusted(-padding, -padding, padding, padding))
            if isinstance(modifier, PosterizeModifier):
                padding = simplify_padding(modifier)
                work_target = aligned(target.adjusted(-padding, -padding, padding, padding).intersected(bounds))
            if (isinstance(modifier, KuwaharaModifier)
                    and _kuwahara_region_padding(modifier) is not None):
                work_target = bounds
            # Keep the upstream image identity for cached GPU uploads and blur
            # passes when a pattern slider changes.
            omit_distort_base = False
            if isinstance(modifier, DistortModifier) and valid:
                from comic_editor.ui.distort_pipeline import can_omit_deferred_base
                omit_distort_base = can_omit_deferred_base(
                    canvas, image, target, modifier, stage_scope, provisional, navigator)
            source = image if pattern else None
            if outline:
                padding_key = ("outline-stage-source", upstream_key,
                              canvas._rect_signature(bounds),
                              canvas._rect_signature(work_target))
                source = None if provisional else canvas._modifier_source_cache_get(padding_key)
                if source is None:
                    source = empty_image(work_target)
                    painter = QPainter(source)
                    painter.drawImage(bounds.topLeft() - work_target.topLeft(), image)
                    painter.end()
                    if not provisional:
                        canvas._modifier_source_cache_put(padding_key, source)
            if source is None and not omit_distort_base:
                source = empty_image(work_target)
            source_size = (_empty_image_size(work_target) if omit_distort_base
                           else (source.width(), source.height()))
            mapping = canvas._world_to_image_transform(local_to_world, work_target, *source_size)
            fields = canvas._modifier_mask_fields([modifier], *source_size, mapping, local_to_world.mapRect(work_target))
            _reject_deferred_provisional(canvas,
                input_revision != getattr(canvas, '_effect_provisional_revision', 0), stage_scope)
            if pattern:
                from comic_editor.ui.gpu_pattern_effects import renderer_for
                from comic_editor.ui.modifier_rendering import apply_pattern_modifier
                from comic_editor.ui.halftone_source import render_color_source
                revision = getattr(canvas, "_effect_provisional_revision", 0)
                color_source = (render_color_source(canvas, modifier, source, work_target, local_to_world)
                                if isinstance(modifier, HalftoneModifier) else None)
                provisional |= getattr(canvas, "_effect_provisional_revision", 0) != revision
                # Preserve the established GPU sampling/rounding path on the
                # GUI thread. Only its CPU fallback can move to a worker;
                # swapping backends changes exact pattern pixels and cost.
                cached = apply_pattern_modifier(source, modifier, fields, renderer_for(canvas),
                                                color_source=color_source, allow_cpu_fallback=False)
                if cached is None:
                    asynchronous = (request_scope is not None and canvas._interactive_render and (not exact or deferred)
                        and not canvas._render_base_alpha
                        and canvas._rendering_mask_contributor <= 0 and not provisional
                        and not navigator
                        and source.width() * source.height() > 128 * 128)
                    if asynchronous:
                        # QImage copies are detached automatically on later UI
                        # writes; mutable models and NumPy mask fields must be
                        # copied explicitly before a worker can inspect them.
                        incoming, effect = QImage(source), copy.deepcopy(modifier)
                        colors = QImage(color_source) if color_source is not None else None
                        masks = {name: np.array(field, copy=True) for name, field in fields.items()}
                        crop = None
                        if work_target != target and pattern_frame_key is None:
                            crop = QRectF(target)
                            crop.translate(-work_target.topLeft())
                            crop = crop.toAlignedRect()
                        def compute(cancelled=None, incoming=incoming, effect=effect,
                                    colors=colors, masks=masks, crop=crop):
                            result = apply_pattern_modifier(incoming, effect, masks,
                                color_source=colors, cancelled=cancelled)
                            return result.copy(crop) if result is not None and crop is not None else result
                        # Includes working arrays used by the full-image CPU
                        # fallback, not only the retained RGBA8 input images.
                        size = (56 * int(incoming.sizeInBytes())
                                + (int(colors.sizeInBytes()) if colors is not None else 0)
                                + sum(field.nbytes for field in masks.values()))
                        asynchronous = canvas._effect_jobs.request(
                            pattern_frame_scope if pattern_frame_key is not None else stage_scope,
                            pattern_frame_key if pattern_frame_key is not None else key, compute, size,
                            allow_oversized=True, require_exact=deferred)
                        if deferred:
                            raise ProjectionPending(
                                pattern_frame_scope if pattern_frame_key is not None else stage_scope,
                                pattern_frame_key if pattern_frame_key is not None else key)
                    if not exact and (asynchronous or ((navigator or provisional) and canvas._interactive_render)):
                        draft_key = ("pattern-draft", key)
                        cached = canvas._modifier_cache_get(draft_key)
                        if cached is None:
                            cached = _pattern_draft(source, modifier, fields, color_source)
                            canvas._modifier_cache_put(draft_key, cached)
                        cached = cached.scaled(source.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                        provisional = True
                    else:
                        cached = apply_pattern_modifier(source, modifier, fields,
                                                        color_source=color_source)
                if pattern_frame_key is not None and not provisional:
                    canvas._modifier_cache_put(pattern_frame_key, cached)
                    if pattern_frame_scope is not None:
                        canvas._effect_jobs.retained_put(("result", pattern_frame_scope), pattern_frame_key, cached,
                                                        shared=required is not None)
                if work_target != target:
                    cropped = QRectF(target)
                    cropped.translate(-work_target.topLeft())
                    cached = cached.copy(cropped.toAlignedRect())
            elif isinstance(modifier, DistortModifier):
                if (exact and region_requests_enabled(canvas) and not provisional
                        and not navigator and source_key is not None
                        and stage_base_scope is not None and index == len(modifiers) - 1
                        and modifier.modifier_type == "distort_mesh_warp"):
                    # Projection blocks change native extents as the camera
                    # zooms. Finished mesh pixels have a stable native grid,
                    # so overlapping views can share them before resampling.
                    from comic_editor.ui.distort_regions import render_mesh_regions
                    frame = aligned(effect_bounds(bounds, [modifier], local_to_world))
                    cached, provisional = render_mesh_regions(
                        canvas, image, bounds, frame, target, modifier,
                        local_to_world, key, stage_base_scope)
                else:
                    from comic_editor.ui.distort_pipeline import render_distort_stage
                    if not omit_distort_base:
                        painter = QPainter(source)
                        painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                        painter.end()
                    cached, provisional = render_distort_stage(
                        canvas, image, source, bounds, target, modifier,
                        local_to_world, fields, key, stage_scope, provisional, navigator,
                        base_size=source_size if omit_distort_base else None)
            elif isinstance(modifier, CageTransformModifier) and valid:
                from comic_editor.ui.cage_rendering import warp_image
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft()-target.topLeft(), image)
                painter.end()
                shape = (source.height(), source.width())
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, shape, fields))/100
                if amount.ndim == 2:
                    amount = amount[..., None]
                incoming, base, cage = QImage(image), QImage(source), copy.deepcopy(modifier)
                source_bounds, output_bounds, cage_mapping = QRectF(bounds), QRectF(target), QTransform(local_to_world)
                def compute(cancelled=None, pixel_scale=1., incoming=incoming, base=base, cage=cage,
                            source_bounds=source_bounds, output_bounds=output_bounds, cage_mapping=cage_mapping, amount=amount):
                    result = warp_image(incoming, source_bounds, cage, cage_mapping, output_bounds, cancelled, pixel_scale=pixel_scale)
                    if result is None:
                        return None
                    warped = result[0]
                    blend_base, blend_amount = base, amount
                    if warped.size() != base.size():
                        if pixel_scale == 1.:
                            warped = warped.scaled(base.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                        else:
                            blend_base = base.scaled(warped.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                            if np.ndim(amount) >= 2:
                                xs = np.minimum((np.arange(warped.width()) * base.width() / warped.width()).astype(int), base.width() - 1)
                                ys = np.minimum((np.arange(warped.height()) * base.height() / warped.height()).astype(int), base.height() - 1)
                                blend_amount = amount[ys[:, None], xs[None, :]]
                    if np.ndim(blend_amount) == 0 and float(blend_amount) == 1.:
                        return warped
                    return _premultiplied_qimage(_qimage_premultiplied(blend_base)*(1-blend_amount)+_qimage_premultiplied(warped)*blend_amount)
                asynchronous = (request_scope is not None and canvas._interactive_render and (not exact or deferred)
                    and not canvas._render_base_alpha
                    and canvas._rendering_mask_contributor <= 0 and not provisional
                    and not navigator
                    and source.width()*source.height() > 128*128)
                from comic_editor.ui.gpu_textures import renderer_for
                gpu = renderer_for(canvas)
                warped = gpu.cage(incoming, source_bounds, cage, cage_mapping, output_bounds) if gpu is not None else None
                if warped is None and deferred and asynchronous:
                    canvas._effect_jobs.request(stage_scope, key, compute,
                        10*int(incoming.sizeInBytes())+4*int(source.sizeInBytes()),
                        allow_oversized=True, require_exact=True)
                    raise ProjectionPending(stage_scope, key)
                if warped is not None:
                    cached = warped if np.ndim(amount) == 0 and float(amount) == 1. else _premultiplied_qimage(
                        _qimage_premultiplied(base)*(1-amount)+_qimage_premultiplied(warped)*amount)
                elif ((asynchronous and canvas._effect_jobs.request(
                    stage_scope, key, compute,
                    10*int(incoming.sizeInBytes())+4*int(source.sizeInBytes()),
                    allow_oversized=True)) or ((navigator or provisional) and canvas._interactive_render and not exact)):
                    draft_key = ("cage-draft", key)
                    cached = canvas._modifier_cache_get(draft_key)
                    if cached is None:
                        cached = compute(pixel_scale=min(1., 192/max(source.width(), source.height())))
                        canvas._modifier_cache_put(draft_key, cached)
                    cached = cached.scaled(source.size(), Qt.IgnoreAspectRatio, Qt.FastTransformation)
                    provisional = True
                else:
                    cached = compute()
            elif isinstance(modifier, ArrayModifier) and valid:
                painter = QPainter(source)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
                painter.setRenderHint(QPainter.Antialiasing, not nearest)
                try:
                    for step in array_indices(modifier):
                        transform = local_to_world * array_transform(modifier, step) * inverse
                        if not transform.mapRect(bounds).intersects(target):
                            continue
                        painter.setTransform(QTransform.fromTranslate(bounds.left(), bounds.top())
                            * transform * QTransform.fromTranslate(-target.left(), -target.top()))
                        painter.drawImage(0, 0, image)
                finally:
                    painter.end()
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity,
                    (source.height(), source.width()), fields)) / 100
                if amount.ndim == 2:
                    amount = amount[..., None]
                cached = source if np.ndim(amount) == 0 and float(amount) == 1. else _premultiplied_qimage(
                    _qimage_premultiplied(source) * amount)
                painter = QPainter(cached)
                painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                painter.end()
            elif isinstance(modifier, MirrorModifier) and valid:
                painter = QPainter(source)
                painter.setRenderHint(QPainter.SmoothPixmapTransform, not nearest)
                painter.setRenderHint(QPainter.Antialiasing, not nearest)
                reflection = local_to_world * reflection_transform(modifier) * inverse
                painter.setTransform(QTransform.fromTranslate(bounds.left(), bounds.top()) * reflection * QTransform.fromTranslate(-target.left(), -target.top()))
                painter.drawImage(0, 0, image)
                painter.end()
                amount = np.asarray(_parameter_field(modifier, "intensity", modifier.intensity, (source.height(), source.width()), fields)) / 100
                if amount.ndim == 2:
                    amount = amount[..., None]
                cached = _premultiplied_qimage(_qimage_premultiplied(source) * amount)
                painter = QPainter(cached)
                painter.drawImage(bounds.topLeft() - target.topLeft(), image)
                painter.end()
            elif isinstance(modifier, RadialBlurModifier):
                from comic_editor.ui.radial_pipeline import render_radial_stage
                painter = QPainter(source)
                painter.drawImage(bounds.topLeft()-target.topLeft(), image)
                painter.end()
                # Keep the complete incoming image: rotations can pull content
                # from outside the requested output tile/viewport.
                image_mapping = canvas._world_to_image_transform(local_to_world, bounds, image.width(), image.height())
                origin = (target.x()-bounds.x(), target.y()-bounds.y())
                asynchronous = (
                    request_scope is not None and canvas._interactive_render and (not exact or deferred)
                    and not canvas._render_base_alpha
                    and canvas._rendering_mask_contributor <= 0
                    and not provisional
                    and not navigator
                )
                cached, provisional = render_radial_stage(canvas, image, source, modifier, fields,
                    image_mapping, origin, source_key=upstream_key, scope=stage_scope,
                    asynchronous=asynchronous, deferred=deferred, provisional=provisional,
                    navigator=navigator, exact=exact,
                    bounded_preview=(provisional and not exact and isinstance(source_key, tuple)
                                     and source_key[:1] == ('live-effect-draft-source',)))
            else:
                if not outline:
                    painter = QPainter(source)
                    painter.drawImage(bounds.topLeft() - work_target.topLeft(), image)
                    painter.end()
                if request_scope is not None or provisional or navigator:
                    from comic_editor.ui.interactive_effects import render_interactive_stack
                    work_key = ("stage-work", key) if work_target != target else key
                    cached, provisional = render_interactive_stack(
                        canvas, source, [modifier], local_to_world.map(work_target.topLeft()).toTuple(), fields,
                        cache_key=work_key, scope=stage_scope or ("provisional-stage", modifier.modifier_id),
                        world_to_image=mapping, nearest=nearest, upstream_provisional=provisional)
                else:
                    cached = apply_modifier_stack(
                        source, [modifier], local_to_world.map(work_target.topLeft()).toTuple(), fields,
                        world_to_image=mapping, nearest=nearest,
                        outline_distance_cache=canvas._outline_distance_cache,
                        blur_pyramid_cache=canvas._blur_pyramid_cache,
                    )
                if work_target != target:
                    cropped = QRectF(target)
                    cropped.translate(-work_target.topLeft())
                    cached = cached.copy(cropped.toAlignedRect())
            _reject_deferred_provisional(canvas, provisional or
                input_revision != getattr(canvas, '_effect_provisional_revision', 0), stage_scope)
            if not provisional:
                canvas._modifier_cache_put(key, cached)
            elif live_prefix_key is not None:
                canvas._modifier_cache_put(live_prefix_key, cached)
        image, bounds = cached, target
        if checkpointing and not provisional:
            if region_requests_enabled(canvas) or reference:
                # Adjacent output regions often share a full-frame warp prefix.
                # Advancing one region's checkpoint must not discard that exact
                # prefix and force another MLS/mesh pass after ordinary-LRU
                # pressure. Stage scopes describe their own output extent and
                # keys include the complete upstream dependencies. The retained
                # pool accounts shared QImage storage once and stays bounded.
                canvas._effect_jobs.retained_put(("result", stage_scope), key, image,
                    shared=required is not None and index < len(modifiers) - 1)
            canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (index + 1, QRectF(bounds)))
            if not (region_requests_enabled(canvas) or reference):
                canvas._effect_jobs.retained_remove(("result", stage_scope), key)
            canvas._effect_jobs.retained_remove(("result", stage_scope), ("stage-work", key))
    if checkpointing and not provisional:
        canvas._effect_jobs.retained_put(checkpoint_scope, pipeline_key, image, (len(modifiers), QRectF(bounds)))
    if provisional:
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
    return image, bounds
