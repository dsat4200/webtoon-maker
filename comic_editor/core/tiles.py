"""Sparse 256px RGBA raster tile storage and stroke painting."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Iterator

from PIL import Image as PILImage
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPolygonF, QTransform


TILE_SIZE = 256


class TileStore:
    def __init__(self, tile_size: int = TILE_SIZE) -> None:
        self.tile_size = tile_size
        self._tiles: dict[str, dict[tuple[int, int], QImage]] = {}
        self.dirty: set[tuple[str, int, int]] = set()

    @staticmethod
    def _empty(size: int) -> QImage:
        image = QImage(size, size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        return image

    def tile(self, object_id: str, key: tuple[int, int], create: bool = False) -> QImage | None:
        object_tiles = self._tiles.setdefault(object_id, {})
        image = object_tiles.get(key)
        if image is None and create:
            image = self._empty(self.tile_size)
            object_tiles[key] = image
        return image

    def set_tile(self, object_id: str, key: tuple[int, int], image: QImage | None) -> None:
        object_tiles = self._tiles.setdefault(object_id, {})
        if image is None or image.isNull() or self.is_empty(image):
            object_tiles.pop(key, None)
        else:
            object_tiles[key] = QImage(image)
        self.dirty.add((object_id, key[0], key[1]))

    def snapshot(self, object_id: str, keys: set[tuple[int, int]]) -> dict[tuple[int, int], QImage | None]:
        result: dict[tuple[int, int], QImage | None] = {}
        for key in keys:
            image = self.tile(object_id, key)
            result[key] = QImage(image) if image is not None else None
        return result

    def keys_for_rect(self, rect: QRectF) -> set[tuple[int, int]]:
        if rect.isEmpty():
            return set()
        left = math.floor(rect.left() / self.tile_size)
        right = math.floor(rect.right() / self.tile_size)
        top = math.floor(rect.top() / self.tile_size)
        bottom = math.floor(rect.bottom() / self.tile_size)
        return {(x, y) for y in range(top, bottom + 1) for x in range(left, right + 1)}

    def paint_dab(
        self, object_id: str, point: QPointF, size: float, color: QColor,
        opacity: float = 1.0, erase: bool = False, square: bool = False,
        antialias: bool = True,
        before: dict[tuple[int, int], QImage | None] | None = None,
    ) -> QRectF:
        half = max(0.5, size / 2)
        rect = QRectF(point.x() - half - 2, point.y() - half - 2, size + 4, size + 4)
        for key in self.keys_for_rect(rect):
            image = self.tile(object_id, key)
            if before is not None and key not in before:
                before[key] = QImage(image) if image is not None else None
            image = self.tile(object_id, key, create=True)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, antialias)
            painter.setCompositionMode(
                QPainter.CompositionMode_Clear if erase else QPainter.CompositionMode_SourceOver
            )
            local = QPointF(
                point.x() - key[0] * self.tile_size,
                point.y() - key[1] * self.tile_size,
            )
            if erase:
                painter.setPen(Qt.NoPen)
                painter.setBrush(Qt.black)
            else:
                actual = QColor(color)
                actual.setAlphaF(max(0.0, min(1.0, opacity)))
                painter.setPen(Qt.NoPen)
                painter.setBrush(actual)
            if square:
                painter.drawRect(QRectF(local.x() - half, local.y() - half, size, size))
            else:
                painter.drawEllipse(local, half, half)
            painter.end()
            self.dirty.add((object_id, key[0], key[1]))
        return rect

    def paint_line(
        self, object_id: str, start: QPointF, end: QPointF, size: float,
        color: QColor, opacity_start: float = 1.0, opacity_end: float = 1.0,
        erase: bool = False, square: bool = False, antialias: bool = True,
        density: float = 1.0,
        before: dict[tuple[int, int], QImage | None] | None = None,
    ) -> QRectF:
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        spacing = max(0.5, max(0.75, size / 4) / max(0.1, density))
        steps = max(1, int(math.ceil(distance / spacing)))
        dirty = QRectF()
        for index in range(1, steps + 1):
            ratio = index / steps
            point = QPointF(
                start.x() + (end.x() - start.x()) * ratio,
                start.y() + (end.y() - start.y()) * ratio,
            )
            opacity = opacity_start + (opacity_end - opacity_start) * ratio
            dab = self.paint_dab(
                object_id, point, size, color, opacity, erase, square,
                antialias, before,
            )
            dirty = dab if dirty.isEmpty() else dirty.united(dab)
        return dirty

    def iter_tiles(
        self, object_id: str, local_rect: QRectF | None = None,
    ) -> Iterator[tuple[tuple[int, int], QImage]]:
        for key, image in self._tiles.get(object_id, {}).items():
            tile_rect = QRectF(
                key[0] * self.tile_size, key[1] * self.tile_size,
                self.tile_size, self.tile_size,
            )
            if local_rect is None or tile_rect.intersects(local_rect):
                yield key, image

    def remove_object(self, object_id: str) -> None:
        self._tiles.pop(object_id, None)
        self.dirty = {item for item in self.dirty if item[0] != object_id}

    def object_tiles(self, object_id: str) -> dict[tuple[int, int], QImage]:
        return {
            key: QImage(image)
            for key, image in self._tiles.get(object_id, {}).items()
        }

    def replace_object_tiles(
        self, object_id: str, values: dict[tuple[int, int], QImage | None],
    ) -> None:
        previous = set(self._tiles.get(object_id, {}))
        replacement = {
            key: QImage(image)
            for key, image in values.items()
            if image is not None and not image.isNull() and not self.is_empty(image)
        }
        self._tiles[object_id] = replacement
        for x, y in previous | set(replacement):
            self.dirty.add((object_id, x, y))

    def content_bounds(self, object_id: str) -> QRectF | None:
        result = QRectF()
        found = False
        for (tile_x, tile_y), image in self._tiles.get(object_id, {}).items():
            bbox = self._alpha_bbox(image)
            if bbox is None:
                continue
            left, top, right, bottom = bbox
            rect = QRectF(
                tile_x * self.tile_size + left,
                tile_y * self.tile_size + top,
                right - left,
                bottom - top,
            )
            result = rect if not found else result.united(rect)
            found = True
        return result if found else None

    def projective_transform(
        self,
        object_id: str,
        object_x: float,
        object_y: float,
        destination_quad: list[tuple[float, float]],
        source_rect_local: QRectF | None = None,
    ) -> dict[tuple[int, int], QImage]:
        """Render an object's complete sparse content into parent-local tiles."""
        content = source_rect_local or self.content_bounds(object_id)
        if content is None or content.isEmpty():
            return {}
        source = QRectF(
            object_x + content.x(), object_y + content.y(),
            content.width(), content.height(),
        )
        source_quad = QPolygonF([
            source.topLeft(), source.topRight(), source.bottomRight(), source.bottomLeft()
        ])
        target_quad = QPolygonF([QPointF(*point) for point in destination_quad])
        transform = QTransform.quadToQuad(source_quad, target_quad)
        if not isinstance(transform, QTransform) or not transform.isInvertible():
            raise ValueError("Transform quad is degenerate")
        inverse, valid_inverse = transform.inverted()
        if not valid_inverse:
            raise ValueError("Transform quad is degenerate")
        bounds = target_quad.boundingRect().adjusted(-2, -2, 2, 2)
        target_keys = self.keys_for_rect(bounds)
        target_path = QPainterPath()
        target_path.addPolygon(target_quad)
        result: dict[tuple[int, int], QImage] = {}
        for key in target_keys:
            tile_origin = QPointF(key[0] * self.tile_size, key[1] * self.tile_size)
            tile_rect = QRectF(tile_origin.x(), tile_origin.y(), self.tile_size, self.tile_size)
            if not target_path.intersects(tile_rect):
                continue
            image = self._empty(self.tile_size)
            painter = QPainter(image)
            painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
            painter.translate(-tile_origin.x(), -tile_origin.y())
            painter.setClipPath(target_path, Qt.IntersectClip)
            painter.setTransform(transform, True)
            source_query_parent = inverse.mapRect(tile_rect).adjusted(-2, -2, 2, 2)
            source_query_local = source_query_parent.translated(-object_x, -object_y)
            for (source_x, source_y), source_image in self.iter_tiles(
                object_id, source_query_local
            ):
                painter.drawImage(
                    object_x + source_x * self.tile_size,
                    object_y + source_y * self.tile_size,
                    source_image,
                )
            painter.end()
            if not self.is_empty(image):
                result[key] = image
        return result

    def prune_empty(self, object_id: str, keys: set[tuple[int, int]]) -> None:
        object_tiles = self._tiles.get(object_id, {})
        for key in keys:
            image = object_tiles.get(key)
            if image is not None and self.is_empty(image):
                object_tiles.pop(key, None)
                self.dirty.add((object_id, key[0], key[1]))

    @staticmethod
    def is_empty(image: QImage) -> bool:
        # QImage has no cheap alpha bounding box API; a small downscale rejects
        # most empty tiles before the exact scan used only after erasing.
        for y in range(image.height()):
            for x in range(image.width()):
                if image.pixelColor(x, y).alpha() != 0:
                    return False
        return True

    @staticmethod
    def _alpha_bbox(image: QImage) -> tuple[int, int, int, int] | None:
        try:
            rgba = image.convertToFormat(QImage.Format_RGBA8888)
            pil = PILImage.frombuffer(
                "RGBA", (rgba.width(), rgba.height()), bytes(rgba.constBits()),
                "raw", "RGBA", 0, 1,
            )
            return pil.getchannel("A").getbbox()
        except Exception:
            minimum_x, minimum_y = image.width(), image.height()
            maximum_x = maximum_y = -1
            for y in range(image.height()):
                for x in range(image.width()):
                    if image.pixelColor(x, y).alpha() <= 0:
                        continue
                    minimum_x, minimum_y = min(minimum_x, x), min(minimum_y, y)
                    maximum_x, maximum_y = max(maximum_x, x), max(maximum_y, y)
            if maximum_x < minimum_x:
                return None
            return minimum_x, minimum_y, maximum_x + 1, maximum_y + 1

    def load_directory(self, root: Path, object_ids: set[str]) -> None:
        self._tiles.clear()
        for object_id in object_ids:
            directory = root / object_id
            if not directory.is_dir():
                continue
            for path in directory.glob("*.png"):
                try:
                    x_text, y_text = path.stem.split("_", 1)
                    key = int(x_text), int(y_text)
                except ValueError:
                    continue
                image = QImage(str(path))
                if not image.isNull():
                    self._tiles.setdefault(object_id, {})[key] = image.convertToFormat(
                        QImage.Format_ARGB32_Premultiplied
                    )
        self.dirty.clear()

    def save_directory(self, root: Path, object_ids: set[str], complete: bool = False) -> None:
        root.mkdir(parents=True, exist_ok=True)
        for directory in root.iterdir():
            if directory.is_dir() and directory.name not in object_ids:
                import shutil
                shutil.rmtree(directory)
        targets = set(self.dirty)
        if complete:
            targets = {
                (object_id, key[0], key[1])
                for object_id, tiles in self._tiles.items()
                for key in tiles
                if object_id in object_ids
            }
        for object_id, x, y in targets:
            if object_id not in object_ids:
                continue
            directory = root / object_id
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{x}_{y}.png"
            image = self._tiles.get(object_id, {}).get((x, y))
            if image is None:
                if target.exists():
                    target.unlink()
                continue
            temporary = target.with_suffix(".png.tmp")
            if not image.save(str(temporary), "PNG"):
                raise OSError(f"Unable to save raster tile {target}")
            temporary.replace(target)
        if not complete:
            self.dirty.difference_update(targets)
