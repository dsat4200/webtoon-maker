"""One-time materialization of legacy geometric fills into sparse tiles."""
from __future__ import annotations

import math

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPolygonF, QTransform

from .models import BoundGeometry, ChapterDocument, LayerNode, RasterObject
from .tiles import TileStore


def _geometry_path(geometry: BoundGeometry) -> QPainterPath:
    path = QPainterPath()
    path.setFillRule(Qt.FillRule.OddEvenFill)

    def add_contour(nodes, closed: bool, primitive: str = "custom") -> None:
        if not nodes:
            return
        if primitive == "ellipse":
            xs = [node.x for node in nodes]
            ys = [node.y for node in nodes]
            path.addEllipse(QRectF(
                min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
            ))
            return
        start = nodes[0]
        path.moveTo(QPointF(start.x, start.y))
        count = len(nodes) if closed else len(nodes) - 1
        for index in range(max(0, count)):
            first = nodes[index]
            second = nodes[(index + 1) % len(nodes)]
            if first.outgoing is not None or second.incoming is not None:
                path.cubicTo(
                    QPointF(*(first.outgoing or first.position)),
                    QPointF(*(second.incoming or second.position)),
                    QPointF(*second.position),
                )
            else:
                path.lineTo(QPointF(*second.position))
        if closed:
            path.closeSubpath()

    add_contour(geometry.nodes, geometry.closed, geometry.primitive)
    for contour in geometry.additional_contours:
        add_contour(contour.nodes, contour.closed)
    return path


def _quad_transform(
    frame: tuple[float, float, float, float],
    quad: list[tuple[float, float]],
) -> QTransform:
    source = QRectF(*frame)
    source_quad = QPolygonF([
        source.topLeft(), source.topRight(),
        source.bottomRight(), source.bottomLeft(),
    ])
    result = QTransform.quadToQuad(
        source_quad, QPolygonF([QPointF(*point) for point in quad])
    )
    return result if isinstance(result, QTransform) else QTransform()


def _layer_parent_transform(layer: LayerNode) -> QTransform:
    if layer.transform_frame is not None and layer.transform_quad is not None:
        return _quad_transform(layer.transform_frame, layer.transform_quad)
    result = QTransform()
    result.translate(layer.translate_x, layer.translate_y)
    return result


def _layer_world_transform(
    document: ChapterDocument, layer_id: str,
) -> QTransform:
    result = QTransform()
    for layer in document.ancestor_layers(layer_id):
        result = _layer_parent_transform(layer) * result
    return result


def _layer_effective_path(
    document: ChapterDocument, layer_id: str,
    cache: dict[str, QPainterPath],
) -> QPainterPath:
    cached = cache.get(layer_id)
    if cached is not None:
        return QPainterPath(cached)
    layer = document.layers[layer_id]
    if layer.bound is None:
        return QPainterPath()
    base = _geometry_path(layer.bound)
    if not layer.compound_enabled:
        cache[layer_id] = QPainterPath(base)
        return base
    additions = QPainterPath(base)
    additions.setFillRule(Qt.FillRule.OddEvenFill)
    subtractions = QPainterPath()
    subtractions.setFillRule(Qt.FillRule.OddEvenFill)
    root_inverse, valid = _layer_world_transform(
        document, layer_id
    ).inverted()
    if not valid:
        raise ValueError(f"Layer {layer_id} has a non-invertible transform")
    for child in layer.children:
        if child.kind != "layer":
            continue
        candidate = document.layers[child.entity_id]
        if not candidate.visible or candidate.compound_operation == "ignore":
            continue
        operand = _layer_effective_path(document, candidate.layer_id, cache)
        operand = root_inverse.map(
            _layer_world_transform(document, candidate.layer_id).map(operand)
        )
        if candidate.compound_operation == "subtract":
            subtractions = (
                QPainterPath(operand) if subtractions.isEmpty()
                else subtractions.united(operand)
            )
        else:
            additions = additions.united(operand)
    result = additions.subtracted(subtractions)
    cache[layer_id] = QPainterPath(result)
    return result


def _rasterize_path(
    tiles: TileStore, obj: RasterObject,
    path: QPainterPath, color: str,
) -> None:
    bounds = path.boundingRect().adjusted(-1.0, -1.0, 1.0, 1.0)
    if bounds.isEmpty():
        raise ValueError(f"Legacy fill {obj.object_id} has empty geometry")
    obj.interaction_rect = (
        math.floor(bounds.left()), math.floor(bounds.top()),
        max(1.0, math.ceil(bounds.right()) - math.floor(bounds.left())),
        max(1.0, math.ceil(bounds.bottom()) - math.floor(bounds.top())),
    )
    tile_size = obj.tile_size
    for key in tiles.keys_for_rect(bounds):
        tile_x, tile_y = key
        tile_rect = QRectF(
            tile_x * tile_size, tile_y * tile_size, tile_size, tile_size
        )
        if not tile_rect.intersects(bounds):
            continue
        image = QImage(
            tile_size, tile_size, QImage.Format.Format_ARGB32_Premultiplied
        )
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.translate(-tile_rect.left(), -tile_rect.top())
        painter.fillPath(path, QColor(color))
        painter.end()
        if TileStore._alpha_bbox(image) is not None:
            tiles.set_tile(obj.object_id, key, image)


def materialize_legacy_fills(
    document: ChapterDocument, tiles: TileStore,
) -> int:
    """Finish fill migration after persisted raster tiles are available."""
    plans = list(document.legacy_fill_migrations)
    if not plans:
        return 0
    cache: dict[str, QPainterPath] = {}
    try:
        for plan in plans:
            object_id = str(plan["object_id"])
            obj = document.objects.get(object_id)
            if not isinstance(obj, RasterObject):
                raise ValueError(f"Raster target {object_id} is missing")
            if plan["kind"] == "fill_layer":
                path = _layer_effective_path(
                    document, str(plan["source_layer_id"]), cache
                )
            elif plan["kind"] == "vector_fill":
                path = _geometry_path(BoundGeometry.from_dict(plan["geometry"]))
            else:
                raise ValueError(f"Unknown legacy fill kind: {plan['kind']}")
            _rasterize_path(tiles, obj, path, str(plan["color"]))
    except Exception as error:
        raise ValueError(
            f"Legacy fill migration could not be completed: {error}"
        ) from error
    document.legacy_fill_migrations.clear()
    return len(plans)
