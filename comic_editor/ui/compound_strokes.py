"""Stroke effects on individual compound operands, before boolean composition.

Only transient boundary samples and the owner's attributed outline are changed.
Child artwork is rendered normally through the resulting compound clipping path.
"""
from dataclasses import dataclass
import numpy as np
from scipy.spatial import cKDTree
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QPainter, QPainterPath

from comic_editor.core.models import (
    BoundGeometry, PathContour, PathNode, StrokeModifier, DotDashModifier,
)
from comic_editor.core.stroke_geometry import (
    StrokeLoop, sample_path, deform_loop, normals, dot_dash_path, loop_path,
)
from comic_editor.ui.shape_contours import compile_bound, geometry_key
from comic_editor.ui.shape_outline import path_key
from comic_editor.ui.shape_outline_compound import (
    compound_outline, transform_key, _uniform_compound_outline,
)
from comic_editor.ui.compound_outline_painting import prepare_outline_raster
from comic_editor.ui.stroke_rendering import mask_parameters, nearest_coordinates
from comic_editor.ui.effect_pipeline import empty_image, aligned
from comic_editor.ui.modifier_rendering import (
    _qimage_premultiplied, _premultiplied_qimage, modifier_render_settings,
)


def scoped(canvas, layer, document=None):
    document = document or canvas.chapter
    return bool(document and layer.bound is not None and layer.bound.closed
                and layer.layer_kind == "bounded" and not layer.is_page
                and (layer.compound_enabled or document.closest_compound_ancestor(layer.layer_id) is not None))


def modifiers(canvas, layer, document=None):
    document = document or canvas.chapter
    if not scoped(canvas, layer, document):
        return []
    return [modifier for mid in layer.modifier_ids
            if isinstance(modifier := document.modifiers.get(mid), StrokeModifier)
            and not modifier.muted and (modifier.intensity > 0 or "intensity" in modifier.parameter_masks)]


@dataclass
class Appearance:
    bound: BoundGeometry
    path: QPainterPath
    loops: list
    opacity: list
    dots: list
    signature: tuple


def _outline_styles(bound, loops):
    """Carry per-anchor width and enabled flags onto the derived render samples."""
    styles = {
        (node.outline_multiplier, node.outline_enabled)
        for contour in bound.iter_contours() for node in contour.nodes
    }
    if len(styles) == 1:
        # Uniform outlines need no geometric attribution. This is also exact
        # for non-default widths and disabled outlines, including multiple
        # contours: every nearest source sample would return this same pair.
        width, enabled = next(iter(styles))
        return [(np.full(len(loop.points), width),
                 np.full(len(loop.points), enabled, dtype=bool)) for loop in loops]
    samples, widths, enabled = [], [], []
    for contour, source in zip(compile_bound(bound), bound.iter_contours()):
        for edge in contour.edges:
            first, last = source.nodes[edge.index], source.nodes[(edge.index+1) % len(source.nodes)]
            length = edge.path.length()
            count = min(8192, max(2, int(length*2)+1))
            for fraction in np.linspace(0, 1, count, endpoint=False):
                samples.append(edge.path.pointAtPercent(edge.path.percentAtLength(length*fraction)).toTuple())
                widths.append(first.outline_multiplier+(last.outline_multiplier-first.outline_multiplier)*fraction)
                enabled.append(first.outline_enabled)
    if not samples:
        return [(np.ones(len(loop.points)), np.ones(len(loop.points), dtype=bool)) for loop in loops]
    tree = cKDTree(samples)
    widths, enabled = np.asarray(widths), np.asarray(enabled)
    return [(widths[indexes], enabled[indexes]) for loop in loops
            for indexes in [tree.query(loop.points)[1]]]


def _boundary_indexes(loop, style):
    """Omit redundant straight-span vertices from constant-style boundaries.

    All deformation, mask, opacity and dash samples remain in the full loop.
    This only removes collinear polygon anchors at floating-point precision;
    the distance threshold is independent of zoom and preview quality.
    """
    points = loop.points
    if len(points) < 4 or not (np.all(style[0] == style[0][0])
                               and np.all(style[1] == style[1][0])):
        return np.arange(len(points))
    incoming, outgoing = points-np.roll(points, 1, axis=0), np.roll(points, -1, axis=0)-points
    chord = incoming+outgoing
    length = np.linalg.norm(chord, axis=1)
    cross = incoming[:, 0]*outgoing[:, 1]-incoming[:, 1]*outgoing[:, 0]
    roundoff = np.finfo(float).eps * max(1., float(np.max(np.abs(points)))) * 32
    redundant = ((np.abs(cross) <= roundoff*length)
                 & (np.sum(incoming*outgoing, axis=1) >= 0) & (length > roundoff))
    indexes = np.flatnonzero(~redundant)
    return indexes if len(indexes) >= 3 else np.arange(len(points))


def appearance(canvas, layer, document=None):
    document = document or canvas.chapter
    effects = modifiers(canvas, layer, document)
    if not effects:
        return None
    building = getattr(canvas, "_compound_stroke_building", set())
    if layer.layer_id in building:
        return None
    mapping = canvas._document_layer_world_transform(document, layer.layer_id)
    mask_signature = canvas._modifier_parameter_signature([m.modifier_id for m in effects]) if document is canvas.chapter else ()
    # Unmasked stroke effects are defined in the owner's local geometry.
    # Translation/affine placement cannot change these samples; world-space
    # parameter masks still require the complete placement in their key.
    placement = transform_key(mapping) if any(m.parameter_masks for m in effects) else ()
    key = (geometry_key(layer.bound), layer.vertex_radius, layer.border_width,
           tuple(tuple((n.outline_multiplier, n.outline_enabled) for n in contour.nodes)
                 for contour in layer.bound.iter_contours()),
           tuple(repr(modifier_render_settings(m)) for m in effects), mask_signature, placement)
    cache = getattr(canvas, "_compound_stroke_appearances", None)
    if cache is None:
        cache = canvas._compound_stroke_appearances = {}
    existing = cache.get(layer.layer_id)
    if existing is not None and existing.signature == key:
        return existing
    canvas._compound_stroke_building = building
    building.add(layer.layer_id)
    try:
        raw = canvas.bound_path(layer.bound, layer.vertex_radius)
        loops = sample_path(raw, max(.1, layer.border_width))
        if not loops:
            return None
        original = loops
        opacity = [np.ones(len(loop.points)) for loop in loops]
        dots = []
        for modifier in effects:
            bounds = aligned(raw.controlPointRect().united(
                loop_path(np.vstack([loop.points for loop in loops])).boundingRect()).adjusted(-2, -2, 2, 2))
            if document is canvas.chapter:
                parameters = mask_parameters(canvas, modifier, loops, bounds, mapping)
            else:
                parameters = [{name: np.full(len(loop.points), getattr(modifier, name))
                               for name in modifier.parameter_ranges()}.__getitem__ for loop in loops]
            if isinstance(modifier, DotDashModifier):
                # Store the arclength frame at this stage so later deformation
                # moves the marks with the same owner instead of resetting them.
                dots.append((modifier, parameters, loops))
            else:
                result = [deform_loop(loop, modifier, parameter) for loop, parameter in zip(loops, parameters)]
                loops = [item[0] for item in result]
                opacity = [old*item[1] for old, item in zip(opacity, result)]
        if all(np.array_equal(old.points, new.points) for old, new in zip(original, loops)):
            bound, path = layer.bound, raw
        else:
            styles = _outline_styles(layer.bound, original)
            contours = [PathContour(nodes=[PathNode(node_id=f"stroke:{layer.layer_id}:{ci}:{pi}",
                x=float(loop.points[pi, 0]), y=float(loop.points[pi, 1]),
                outline_multiplier=float(style[0][pi]), outline_enabled=bool(style[1][pi]))
                for pi in _boundary_indexes(loop, style)], closed=True)
                for ci, (loop, style) in enumerate(zip(loops, styles))]
            bound = BoundGeometry(nodes=contours[0].nodes, closed=True, primitive="custom", additional_contours=contours[1:])
            path = canvas.bound_path(bound)
        result = Appearance(bound, path, loops, opacity, dots, key)
        # One current version per owner; bound the cache on large documents.
        if len(cache) >= 128:
            cache.pop(next(iter(cache)))
        cache[layer.layer_id] = result
        return result
    finally:
        building.discard(layer.layer_id)


def _shade(canvas, coverage, fill, source, value):
    bounds = aligned(coverage.controlPointRect().adjusted(-1, -1, 1, 1))
    if bounds.isEmpty():
        return empty_image(bounds), bounds
    key = ("compound-stroke-ink", path_key(coverage), path_key(fill),
           transform_key(source.mapping), value.signature)
    cached = canvas._modifier_cache_get(key)
    if cached is not None:
        return cached, bounds
    image = empty_image(bounds)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.x(), -bounds.y())
    painter.fillPath(coverage, Qt.white)
    painter.end()
    pixels = _qimage_premultiplied(image)
    loops = [loop.mapped(source.mapping) for loop in value.loops]
    opacities = np.concatenate(value.opacity)
    nearest = np.empty((image.height(), image.width()), dtype=np.int32)
    for first, last, _, _, indexes in nearest_coordinates(loops, bounds):
        nearest[first:last] = indexes.reshape(last-first, image.width())
    pixels *= opacities[nearest, None]
    inverse, valid = source.mapping.inverted()
    local_fill = inverse.map(fill) if valid else fill
    centers = []
    for loop in value.loops:
        outward = normals(loop.points)
        # A subtracting edge's visible ink is outside its own operand. An
        # adding edge's ink is inside. Ask the composed fill for that side.
        outside = np.array([local_fill.contains(QPointF(*(point+normal*.25)))
                            for point, normal in zip(loop.points, outward)])
        points = loop.points+outward*np.where(outside, 1., -1.)[:, None]*loop.width/2
        centers.append(StrokeLoop(points, loop.width))
    for modifier, parameters, earlier_loops in value.dots:
        marks = empty_image(bounds)
        painter = QPainter(marks)
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(-bounds.x(), -bounds.y())
        for loop, parameter, earlier in zip(centers, parameters, earlier_loops):
            painter.fillPath(source.mapping.map(dot_dash_path(loop, modifier, parameter,
                spacing_points=earlier.points)), Qt.white)
        painter.end()
        marked = _qimage_premultiplied(marks)[..., 3]
        strength = np.concatenate([parameter("intensity")/100 for parameter in parameters])[nearest]
        pixels *= (1-strength+strength*marked)[..., None]
    image = _premultiplied_qimage(pixels)
    canvas._modifier_cache_put(key, image)
    return image, bounds


def paint_outline(canvas, painter, layer, path, sources, tolerance=.125):
    """Shade only surviving spans attributed to a modified owner."""
    modified = {}
    for index, source in enumerate(sources):
        owner = canvas.chapter.layers.get(source.owner_id)
        value = appearance(canvas, owner) if owner is not None else None
        if value is not None and (value.dots or any(np.any(opacity < 1) for opacity in value.opacity)):
            modified[index] = value
    ordinary = tuple(index for index in range(len(sources)) if index not in modified)
    color = QColor(layer.border_color)
    indices = (*ordinary, -1)
    # A short final boundary can still produce hundreds of overlapping pieces
    # from a curved/tapered contributor. Qt's geometric clip can lose the inner
    # contour of that coverage, filling the entire bubble with outline color.
    # Judge complexity from the generated coverage, not the final boundary.
    # Complete uniform outlines retain their native cached geometry.
    raster = (prepare_outline_raster(painter, path)
              if not _uniform_compound_outline(
                  layer.border_width, sources, indices) else None)
    if raster is not None and raster.bounds.isEmpty():
        return
    base = compound_outline(path, layer.border_width, sources, canvas._outline_cache, tolerance,
                            source_indices=indices, clip=raster is None)
    if raster is not None and base.elementCount() <= 128:
        # Simple variable-width shapes must retain the same geometric edge
        # pixels as their standalone form. The raw build above shares cached
        # attribution/edge meshes with this clipped result.
        raster = None
        base = compound_outline(path, layer.border_width, sources, canvas._outline_cache, tolerance,
                                source_indices=indices, clip=True)
    if raster is None:
        painter.fillPath(base, color)
    else:
        raster.paint(painter, base, path, color)
        del raster
    for index, value in modified.items():
        coverage = compound_outline(path, layer.border_width, sources, canvas._outline_cache, tolerance,
                                    source_indices=(index,))
        if coverage.isEmpty():
            continue
        image, bounds = _shade(canvas, coverage, path, sources[index], value)
        colored = image.copy()
        brush = QPainter(colored)
        brush.setCompositionMode(QPainter.CompositionMode_SourceIn)
        brush.fillRect(colored.rect(), color)
        brush.end()
        painter.drawImage(bounds.topLeft(), colored)
