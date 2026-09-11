"""Ordered stroke stages. Geometry is transient; existing raster stages retain order.

Each stage carries closed contours alongside the image. Deformation transports the
incoming material, including earlier effects, instead of restarting from the source.
A separately processed fill allows dots and opacity noise to reveal the shape fill.
"""
import numpy as np
from scipy.ndimage import map_coordinates
from scipy.spatial import cKDTree
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter, QPainterPath, QTransform

from comic_editor.core.models import (
    LayerNode, StrokeModifier, DotDashModifier, MirrorModifier, ArrayModifier,
    CageTransformModifier,
)
from comic_editor.core.stroke_geometry import (
    StrokeLoop, sample_path, normals, deform_loop, dot_dash_path, loop_path,
)
from comic_editor.core.effect_geometry import effect_bounds, reflection_transform, array_indices, array_transform
from comic_editor.ui.effect_pipeline import render_stages, aligned, empty_image
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _premultiplied_qimage


def target_loops(canvas, target):
    if isinstance(target, LayerNode):
        path = canvas.bound_path(target.bound, target.vertex_radius)
        loops = sample_path(path, target.border_width, inset=True) if target.border_width > 0 else []
        transform = canvas._layer_parent_transform(target)
    else:
        loops = []
        for stroke in target.strokes:
            if not stroke.closed or len(stroke.points) < 3:
                continue
            points = stroke.points
            path = QPainterPath(QPointF(*points[0].position))
            for start, end in zip(points, points[1:]+points[:1]):
                path.cubicTo(QPointF(*(start.outgoing or start.position)),
                             QPointF(*(end.incoming or end.position)), QPointF(*end.position))
            path.closeSubpath()
            loops.extend(sample_path(path, float(np.mean([p.width for p in points]))))
        transform = QTransform().translate(target.x, target.y) * canvas._drawing_object_transform(target)
    return [loop.mapped(transform) for loop in loops]


def placed(image, bounds, target):
    if bounds == target:
        return image
    result = empty_image(target)
    painter = QPainter(result)
    painter.drawImage(bounds.topLeft()-target.topLeft(), image)
    painter.end()
    return result


def mask_parameters(canvas, modifier, loops, bounds, mapping):
    width, height = int(bounds.width()), int(bounds.height())
    fields = canvas._modifier_mask_fields([modifier], width, height,
        canvas._world_to_image_transform(mapping, bounds, width, height), mapping.mapRect(bounds))
    callbacks = []
    for loop in loops:
        values = {}
        coordinates = [loop.points[:, 1]-bounds.y()-.5, loop.points[:, 0]-bounds.x()-.5]
        for attribute in modifier.parameter_ranges():
            binding = modifier.parameter_masks.get(attribute)
            if binding is None:
                values[attribute] = np.full(len(loop.points), getattr(modifier, attribute), dtype=np.float64)
            else:
                field = fields[(modifier.modifier_id, attribute)]
                weights = map_coordinates(field, coordinates, order=1, mode="constant", cval=0)
                values[attribute] = binding.black_value+(binding.white_value-binding.black_value)*weights
        callbacks.append(values.__getitem__)
    return callbacks


def nearest_coordinates(loops, bounds):
    points = np.vstack([loop.points for loop in loops])
    tree = cKDTree(points)
    width, height = int(bounds.width()), int(bounds.height())
    # Bounded chunks keep the temporary query allocation independent of page size.
    for first in range(0, height, max(1, 131072//max(1, width))):
        last = min(height, first+max(1, 131072//max(1, width)))
        y, x = np.mgrid[first:last, :width]
        coordinates = np.column_stack((x.ravel()+bounds.x()+.5, y.ravel()+bounds.y()+.5))
        distance, indexes = tree.query(coordinates, workers=1)
        yield first, last, coordinates, distance, indexes


def warp_material(image, background, bounds, before, after):
    source, fill = _qimage_premultiplied(image), _qimage_premultiplied(background)
    output, back = np.empty_like(source), np.empty_like(fill)
    old = np.vstack([loop.points for loop in before])
    new = np.vstack([loop.points for loop in after])
    old_normals = np.vstack([normals(loop.points) for loop in before])
    displacement = np.linalg.norm(old-new, axis=1)
    if np.max(displacement, initial=0) < 1e-8:
        return image, background
    widths = np.concatenate([np.full(len(loop.points), loop.width) for loop in after])
    interior = empty_image(bounds)
    painter = QPainter(interior)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.x(), -bounds.y())
    for loop in after:
        painter.fillPath(loop_path(loop.points), Qt.white)
    painter.end()
    signs = np.where(_qimage_premultiplied(interior)[..., 3] >= .5, -1., 1.)
    for first, last, coordinates, distance, indexes in nearest_coordinates(after, bounds):
        # At a concave join the closest sample's normal can point across the
        # opposite edge. The closed fill determines the side unambiguously.
        signed_distance = distance*signs[first:last].ravel()
        mapped = old[indexes]+old_normals[indexes]*signed_distance[:, None]
        falloff = np.exp(-np.maximum(0, distance-widths[indexes]*2)**2 /
                         np.maximum(1, displacement[indexes]*2+widths[indexes]*2)**2)
        mapped = coordinates+(mapped-coordinates)*falloff[:, None]
        sample = [mapped[:, 1]-bounds.y()-.5, mapped[:, 0]-bounds.x()-.5]
        for channel in range(4):
            output[first:last, :, channel] = map_coordinates(source[..., channel], sample, order=1,
                mode="constant", cval=0).reshape(last-first, source.shape[1])
            back[first:last, :, channel] = map_coordinates(fill[..., channel], sample, order=1,
                mode="constant", cval=0).reshape(last-first, source.shape[1])
    return _premultiplied_qimage(output), _premultiplied_qimage(back)


def opacity_noise(image, background, bounds, loops, values):
    if all(np.all(value == 1) for value in values):
        return image
    source, fill = _qimage_premultiplied(image), _qimage_premultiplied(background)
    amounts = np.concatenate(values)
    for first, last, _, _, indexes in nearest_coordinates(loops, bounds):
        amount = amounts[indexes].reshape(last-first, source.shape[1], 1)
        source[first:last] = fill[first:last]+(source[first:last]-fill[first:last])*amount
    return _premultiplied_qimage(source)


def apply_dots(image, background, bounds, loops, modifier, parameters):
    source, fill = _qimage_premultiplied(image), _qimage_premultiplied(background)
    marks = empty_image(bounds)
    painter = QPainter(marks)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.x(), -bounds.y())
    for loop, parameter in zip(loops, parameters):
        painter.fillPath(dot_dash_path(loop, modifier, parameter), Qt.white)
    painter.end()
    coverage = _qimage_premultiplied(marks)[..., 3:4]
    points = np.vstack([loop.points for loop in loops])
    center = [points[:, 1]-bounds.y()-.5, points[:, 0]-bounds.x()-.5]
    material = np.column_stack([map_coordinates(source[..., c], center, order=1,
                                                mode="constant", cval=0) for c in range(4)])
    amounts = np.concatenate([parameter("intensity")/100 for parameter in parameters])
    result = np.empty_like(source)
    for first, last, _, _, indexes in nearest_coordinates(loops, bounds):
        color = material[indexes].reshape(last-first, source.shape[1], 4)
        mask = coverage[first:last]
        dotted = color*mask+fill[first:last]*(1-color[..., 3:4]*mask)
        amount = amounts[indexes].reshape(last-first, source.shape[1], 1)
        result[first:last] = source[first:last]*(1-amount)+dotted*amount
    return _premultiplied_qimage(result)


def transformed_loops(loops, modifier, mapping):
    inverse, valid = mapping.inverted()
    if not valid:
        return loops
    if isinstance(modifier, (MirrorModifier, ArrayModifier)):
        transforms = [reflection_transform(modifier)] if isinstance(modifier, MirrorModifier) else [
            array_transform(modifier, step) for step in array_indices(modifier)]
        return loops+[loop.mapped(mapping*transform*inverse) for transform in transforms for loop in loops]
    if isinstance(modifier, CageTransformModifier):
        from comic_editor.core.cage import map_points
        result = []
        for loop in loops:
            world = loop.mapped(mapping)
            points = map_points(modifier, world.points)
            moved = StrokeLoop(np.asarray(points), world.width).mapped(inverse)
            moved.points = loop.points+(moved.points-loop.points)*modifier.intensity/100
            result.append(moved)
        return result
    return loops


def render_stroke_stack(canvas, target, image, bounds, modifiers, mapping, source_key, request_scope,
                        *, tiled=False, provisional=False):
    kind, identifier = ("layer", target.layer_id) if isinstance(target, LayerNode) else ("object", target.object_id)
    if not canvas.chapter.stroke_modifier_target(kind, identifier):
        return render_stages(canvas, image, bounds, [m for m in modifiers if not isinstance(m, StrokeModifier)],
                             mapping, request_scope=request_scope, provisional=provisional, source_key=source_key)
    loops = target_loops(canvas, target)
    if tiled:
        parent = target.parent_id if isinstance(target, LayerNode) else target.parent_layer_id
        parent_mapping = canvas.layer_world_transform(parent) if parent else QTransform()
        loops = [loop.mapped(parent_mapping) for loop in loops]
    if not loops:
        return render_stages(canvas, image, bounds, [m for m in modifiers if not isinstance(m, StrokeModifier)],
                             mapping, request_scope=request_scope, provisional=provisional, source_key=source_key)
    final_bounds = QRectF(bounds)
    for modifier in modifiers:
        if not modifier.muted:
            final_bounds = aligned(effect_bounds(final_bounds, [modifier], mapping))
    key = ("stroke-stack", source_key, tiled, repr([m.to_dict() for m in modifiers]),
           canvas._modifier_parameter_signature(target.modifier_ids), canvas._rect_signature(final_bounds),
           tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)))
    cached = None if provisional else canvas._modifier_cache_get(key)
    retention_scope = ("stroke-stack", request_scope)
    retain = (request_scope is not None and canvas._interactive_render and not provisional
              and getattr(canvas, "_effect_preview_channel", "canvas") != "navigator")
    if cached is None and retain:
        retained = canvas._effect_jobs.retained_get(retention_scope, key)
        cached = retained[0] if retained is not None else None
    if cached is not None:
        return cached, final_bounds
    background_key = ("stroke-fill-source", source_key, tiled, canvas._rect_signature(bounds))
    background = None if provisional else canvas._modifier_source_cache_get(background_key)
    background_provisional = provisional
    revision = getattr(canvas, "_effect_provisional_revision", 0)
    if background is None and isinstance(target, LayerNode) and tiled:
        previous = getattr(canvas, "_stroke_hide_border_id", None)
        canvas._stroke_hide_border_id = identifier
        try:
            background, back_bounds = canvas._tiling_stage(target)
            background = placed(background, back_bounds, bounds)
        finally:
            canvas._stroke_hide_border_id = previous
    elif background is None and isinstance(target, LayerNode):
        background = empty_image(bounds)
        painter = QPainter(background)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        previous = getattr(canvas, "_stroke_hide_border_id", None)
        canvas._stroke_hide_border_id = identifier
        canvas._render_modifier_sources.add((kind, identifier))
        try:
            canvas._render_layer(painter, target, 1., mapping.mapRect(bounds))
        finally:
            canvas._stroke_hide_border_id = previous
            canvas._render_modifier_sources.discard((kind, identifier))
            painter.end()
    elif background is None:
        background = empty_image(bounds)
    background_provisional |= getattr(canvas, "_effect_provisional_revision", 0) != revision
    if not background_provisional:
        canvas._modifier_source_cache_put(background_key, background)
    provisional |= background_provisional
    for index, modifier in enumerate(modifiers):
        if modifier.muted or modifier.intensity <= 0 and "intensity" not in modifier.parameter_masks:
            continue
        if not isinstance(modifier, StrokeModifier):
            old_bounds = QRectF(bounds)
            prefix_key = (source_key, tiled, repr([m.to_dict() for m in modifiers[:index]]),
                          canvas._modifier_parameter_signature([m.modifier_id for m in modifiers[:index]]))
            revision = getattr(canvas, "_effect_provisional_revision", 0)
            image, bounds = render_stages(canvas, image, old_bounds, [modifier], mapping,
                request_scope=(*request_scope, "stroke-material", modifier.modifier_id) if request_scope is not None else None,
                provisional=provisional, source_key=("stroke-material", prefix_key))
            provisional |= getattr(canvas, "_effect_provisional_revision", 0) != revision
            revision = getattr(canvas, "_effect_provisional_revision", 0)
            background, background_bounds = render_stages(canvas, background, old_bounds, [modifier], mapping,
                request_scope=(*request_scope, "stroke-background", modifier.modifier_id) if request_scope is not None else None,
                provisional=background_provisional, source_key=("stroke-background", prefix_key))
            background_provisional |= getattr(canvas, "_effect_provisional_revision", 0) != revision
            provisional |= background_provisional
            background = placed(background, background_bounds, bounds)
            loops = transformed_loops(loops, modifier, mapping)
            continue
        expanded = aligned(effect_bounds(bounds, [modifier], mapping))
        image, background = placed(image, bounds, expanded), placed(background, bounds, expanded)
        bounds = expanded
        prefix = modifiers[:index + 1]
        stage_key = ("stroke-material-stage", source_key, tiled, repr([m.to_dict() for m in prefix]),
                     canvas._modifier_parameter_signature([m.modifier_id for m in prefix]),
                     canvas._rect_signature(bounds),
                     tuple(getattr(mapping, f"m{i}{j}")() for i in range(1, 4) for j in range(1, 4)))
        material = None if provisional else canvas._modifier_cache_get(stage_key)
        fill = None if provisional else canvas._modifier_cache_get(("stroke-background-stage", stage_key))
        if isinstance(modifier, DotDashModifier) and material is not None and fill is not None:
            image, background = material, fill
            continue
        parameters = mask_parameters(canvas, modifier, loops, bounds, mapping)
        if isinstance(modifier, DotDashModifier):
            image = apply_dots(image, background, bounds, loops, modifier, parameters)
        else:
            result = [deform_loop(loop, modifier, parameter) for loop, parameter in zip(loops, parameters)]
            moved, opacity = [item[0] for item in result], [item[1] for item in result]
            if material is not None and fill is not None:
                image, background, loops = material, fill, moved
                continue
            image, background = warp_material(image, background, bounds, loops, moved)
            loops = moved
            image = opacity_noise(image, background, bounds, loops, opacity)
        if not provisional:
            canvas._modifier_cache_put(stage_key, image)
            canvas._modifier_cache_put(("stroke-background-stage", stage_key), background)
    if not provisional:
        canvas._modifier_cache_put(key, image)
        if retain:
            canvas._effect_jobs.retained_put(retention_scope, key, image)
    else:
        canvas._effect_provisional_revision = getattr(canvas, "_effect_provisional_revision", 0) + 1
    return image, bounds
