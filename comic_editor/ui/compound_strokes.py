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
from comic_editor.ui.shape_outline_compound import compound_outline, transform_key
from comic_editor.ui.stroke_rendering import mask_parameters, nearest_coordinates
from comic_editor.ui.effect_pipeline import empty_image, aligned
from comic_editor.ui.modifier_rendering import _qimage_premultiplied, _premultiplied_qimage


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
    samples, widths, enabled = [], [], []
    for contour, source in zip(compile_bound(bound), bound.iter_contours()):
        for edge in contour.edges:
            first, last = source.nodes[edge.index], source.nodes[(edge.index+1) % len(source.nodes)]
            count = min(8192, max(2, int(edge.path.length()*2)+1))
            for fraction in np.linspace(0, 1, count, endpoint=False):
                samples.append(edge.path.pointAtPercent(edge.path.percentAtLength(edge.path.length()*fraction)).toTuple())
                widths.append(first.outline_multiplier+(last.outline_multiplier-first.outline_multiplier)*fraction)
                enabled.append(first.outline_enabled)
    if not samples:
        return [(np.ones(len(loop.points)), np.ones(len(loop.points), dtype=bool)) for loop in loops]
    tree = cKDTree(samples)
    widths, enabled = np.asarray(widths), np.asarray(enabled)
    return [(widths[indexes], enabled[indexes]) for loop in loops
            for indexes in [tree.query(loop.points)[1]]]


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
    key = (geometry_key(layer.bound), layer.border_width,
           tuple(tuple((n.outline_multiplier, n.outline_enabled) for n in contour.nodes)
                 for contour in layer.bound.iter_contours()),
           tuple(repr(m.to_dict()) for m in effects), mask_signature, transform_key(mapping))
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
                x=float(point[0]), y=float(point[1]), outline_multiplier=float(width), outline_enabled=bool(enabled))
                for pi, (point, width, enabled) in enumerate(zip(loop.points, style[0], style[1]))], closed=True)
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
    base = compound_outline(path, layer.border_width, sources, canvas._outline_cache, tolerance,
                            source_indices=(*ordinary, -1))
    painter.fillPath(base, color)
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
