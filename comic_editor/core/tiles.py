"""Sparse 256px RGBA raster tile storage and stroke painting."""
from __future__ import annotations

import math
from pathlib import Path
from typing import Callable, Iterator

import numpy as np
from PIL import Image as PILImage
from PySide6.QtCore import QPointF, QRect, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QPolygonF, QTransform
from scipy.ndimage import (
    binary_closing, binary_dilation, binary_erosion, binary_opening,
    gaussian_filter, label as connected_components,
)


TILE_SIZE = 256


class TileStore:
    def __init__(self, tile_size: int = TILE_SIZE) -> None:
        self.tile_size = tile_size
        self._tiles: dict[str, dict[tuple[int, int], QImage]] = {}
        self._alpha_bounds: dict[
            str, dict[tuple[int, int], tuple[int, int, int, int] | None]
        ] = {}
        self._alpha_bounds_dirty: set[tuple[str, int, int]] = set()
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
            self._alpha_bounds.setdefault(object_id, {})[key] = None
        return image

    def set_tile(self, object_id: str, key: tuple[int, int], image: QImage | None) -> None:
        object_tiles = self._tiles.setdefault(object_id, {})
        bounds = (
            None if image is None or image.isNull()
            else self._alpha_bbox(image)
        )
        if bounds is None:
            object_tiles.pop(key, None)
            self._alpha_bounds.setdefault(object_id, {}).pop(key, None)
        else:
            object_tiles[key] = QImage(image)
            self._alpha_bounds.setdefault(object_id, {})[key] = bounds
        self._alpha_bounds_dirty.discard((object_id, key[0], key[1]))
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
        replace_alpha: bool = False,
    ) -> QRectF:
        return self._paint_samples(
            object_id, [(QPointF(point), float(size), float(opacity))],
            color, erase=erase, square=square, antialias=antialias,
            before=before, replace_alpha=replace_alpha,
        )

    def paint_segment(
        self, object_id: str, start: QPointF, end: QPointF,
        size_start: float, size_end: float, color: QColor,
        opacity_start: float = 1.0, opacity_end: float = 1.0,
        erase: bool = False, square: bool = False,
        antialias: bool = True, density: float = 1.0,
        before: dict[tuple[int, int], QImage | None] | None = None,
        replace_alpha: bool = False,
    ) -> QRectF:
        """Paint one pressure-varying segment with one painter per tile.

        The starting endpoint is deliberately omitted because the preceding
        segment (or the initial dab) already painted it.
        """
        distance = math.hypot(end.x() - start.x(), end.y() - start.y())
        reference_size = max(0.5, min(float(size_start), float(size_end)))
        spacing = max(
            0.5,
            max(0.75, reference_size / 4) / max(0.1, float(density)),
        )
        steps = max(1, int(math.ceil(distance / spacing)))
        samples: list[tuple[QPointF, float, float]] = []
        for index in range(1, steps + 1):
            ratio = index / steps
            samples.append((
                QPointF(
                    start.x() + (end.x() - start.x()) * ratio,
                    start.y() + (end.y() - start.y()) * ratio,
                ),
                size_start + (size_end - size_start) * ratio,
                opacity_start + (opacity_end - opacity_start) * ratio,
            ))
        return self._paint_samples(
            object_id, samples, color, erase=erase, square=square,
            antialias=antialias, before=before,
            replace_alpha=replace_alpha,
        )

    def paint_line(
        self, object_id: str, start: QPointF, end: QPointF, size: float,
        color: QColor, opacity_start: float = 1.0, opacity_end: float = 1.0,
        erase: bool = False, square: bool = False, antialias: bool = True,
        density: float = 1.0,
        before: dict[tuple[int, int], QImage | None] | None = None,
        replace_alpha: bool = False,
    ) -> QRectF:
        return self.paint_segment(
            object_id, start, end, size, size, color,
            opacity_start, opacity_end, erase, square, antialias,
            density, before, replace_alpha,
        )

    def flood_fill(
        self, object_id: str, point: QPointF, bounds: QRectF,
        color: QColor, tolerance: int = 16,
        before: dict[tuple[int, int], QImage | None] | None = None,
    ) -> QRectF:
        """Four-connected RGBA fill over a finite sparse-tile frame.

        Connectivity is resolved per tile with SciPy's compiled component
        labeling, then component labels are joined across tile edges.  This
        avoids constructing one potentially enormous frame-sized image while
        keeping transparent sparse areas fast.
        """
        frame = QRectF(bounds).normalized()
        left, top = math.floor(frame.left()), math.floor(frame.top())
        right = math.ceil(frame.right()) - 1
        bottom = math.ceil(frame.bottom()) - 1
        seed_x, seed_y = math.floor(point.x()), math.floor(point.y())
        if (
            right < left or bottom < top
            or not (left <= seed_x <= right and top <= seed_y <= bottom)
        ):
            return QRectF()
        tolerance = max(0, min(255, int(tolerance)))
        tile_size = self.tile_size
        object_tiles = self._tiles.setdefault(object_id, {})
        rgba_cache: dict[tuple[int, int], np.ndarray] = {}
        label_cache: dict[tuple[int, int], np.ndarray] = {}

        def tile_key(x: int, y: int) -> tuple[int, int]:
            return math.floor(x / tile_size), math.floor(y / tile_size)

        def rgba_for(key: tuple[int, int]) -> np.ndarray:
            cached = rgba_cache.get(key)
            if cached is not None:
                return cached
            source = object_tiles.get(key)
            if source is None:
                array = np.zeros((tile_size, tile_size, 4), dtype=np.uint8)
            else:
                converted = source.convertToFormat(QImage.Format_RGBA8888)
                view = np.frombuffer(
                    converted.constBits(), dtype=np.uint8,
                    count=converted.sizeInBytes(),
                ).reshape(converted.height(), converted.bytesPerLine())
                array = view[:, :converted.width() * 4].reshape(
                    converted.height(), converted.width(), 4
                ).copy()
            rgba_cache[key] = array
            return array

        seed_key = tile_key(seed_x, seed_y)
        seed_local = (
            seed_x - seed_key[0] * tile_size,
            seed_y - seed_key[1] * tile_size,
        )
        target = rgba_for(seed_key)[seed_local[1], seed_local[0]].copy()

        def labels_for(key: tuple[int, int]) -> np.ndarray:
            cached = label_cache.get(key)
            if cached is not None:
                return cached
            rgba = rgba_for(key)
            delta = np.abs(
                rgba.astype(np.int16) - target.astype(np.int16)
            )
            matching = (
                delta[..., 3] <= tolerance
                if int(target[3]) == 0
                else np.max(delta, axis=2) <= tolerance
            )
            origin_x, origin_y = key[0] * tile_size, key[1] * tile_size
            valid_left = max(0, left - origin_x)
            valid_right = min(tile_size - 1, right - origin_x)
            valid_top = max(0, top - origin_y)
            valid_bottom = min(tile_size - 1, bottom - origin_y)
            if (
                valid_right < valid_left or valid_bottom < valid_top
            ):
                matching[:] = False
            else:
                if valid_left:
                    matching[:, :valid_left] = False
                if valid_right + 1 < tile_size:
                    matching[:, valid_right + 1:] = False
                if valid_top:
                    matching[:valid_top, :] = False
                if valid_bottom + 1 < tile_size:
                    matching[valid_bottom + 1:, :] = False
            labels, _count = connected_components(
                matching,
                structure=np.array(
                    [[0, 1, 0], [1, 1, 1], [0, 1, 0]], dtype=np.uint8
                ),
            )
            labels = labels.astype(np.int32, copy=False)
            label_cache[key] = labels
            return labels

        seed_label = int(labels_for(seed_key)[seed_local[1], seed_local[0]])
        if seed_label <= 0:
            return QRectF()
        pending = [(seed_key, seed_label)]
        connected: dict[tuple[int, int], set[int]] = {}
        seen: set[tuple[tuple[int, int], int]] = set()
        directions = (
            ((-1, 0), (slice(None), 0), (slice(None), -1)),
            ((1, 0), (slice(None), -1), (slice(None), 0)),
            ((0, -1), (0, slice(None)), (-1, slice(None))),
            ((0, 1), (-1, slice(None)), (0, slice(None))),
        )
        while pending:
            key, component = pending.pop()
            node = key, int(component)
            if node in seen:
                continue
            seen.add(node)
            labels = labels_for(key)
            connected.setdefault(key, set()).add(int(component))
            for (dx, dy), own_edge, other_edge in directions:
                edge = labels[own_edge]
                positions = edge == component
                if not np.any(positions):
                    continue
                neighbor = key[0] + dx, key[1] + dy
                adjacent = labels_for(neighbor)[other_edge]
                for candidate in np.unique(adjacent[positions]):
                    if candidate > 0:
                        pending.append((neighbor, int(candidate)))

        replacement = np.array([
            color.red(), color.green(), color.blue(), color.alpha()
        ], dtype=np.uint8)
        dirty = QRectF()
        for key, components in connected.items():
            labels = labels_for(key)
            selected = np.isin(labels, tuple(components))
            if not np.any(selected):
                continue
            rgba = rgba_for(key)
            if np.all(rgba[selected] == replacement):
                continue
            if before is not None and key not in before:
                original = object_tiles.get(key)
                before[key] = QImage(original) if original is not None else None
            rgba[selected] = replacement
            ys, xs = np.nonzero(selected)
            origin_x, origin_y = key[0] * tile_size, key[1] * tile_size
            changed = QRectF(
                origin_x + int(xs.min()), origin_y + int(ys.min()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1),
            )
            dirty = changed if dirty.isEmpty() else dirty.united(changed)
            contiguous = np.ascontiguousarray(rgba)
            image = QImage(
                contiguous.data, tile_size, tile_size, tile_size * 4,
                QImage.Format_RGBA8888,
            ).copy().convertToFormat(QImage.Format_ARGB32_Premultiplied)
            self.set_tile(object_id, key, image)
        return dirty

    @staticmethod
    def _rgba_array(image: QImage | None, size: int) -> np.ndarray:
        if image is None or image.isNull():
            return np.zeros((size, size, 4), dtype=np.uint8)
        converted = image.convertToFormat(QImage.Format.Format_RGBA8888)
        view = np.frombuffer(
            converted.constBits(), dtype=np.uint8,
            count=converted.sizeInBytes(),
        ).reshape(converted.height(), converted.bytesPerLine())
        result = np.zeros((size, size, 4), dtype=np.uint8)
        height = min(size, converted.height())
        width = min(size, converted.width())
        result[:height, :width] = view[
            :height, :width * 4
        ].reshape(height, width, 4)
        return result

    @staticmethod
    def _fill_region_mask(
        rgba: np.ndarray, target: np.ndarray,
        tolerance: int, policy: str,
    ) -> np.ndarray:
        tolerance = max(0, min(255, int(tolerance)))
        if policy == "area":
            return np.ones(rgba.shape[:2], dtype=bool)
        if policy == "transparent":
            return rgba[..., 3] <= tolerance
        if policy != "seed":
            raise ValueError(f"Unsupported fill region policy: {policy}")
        delta = np.abs(rgba.astype(np.int16) - target.astype(np.int16))
        return (
            delta[..., 3] <= tolerance
            if int(target[3]) == 0
            else np.max(delta, axis=2) <= tolerance
        )

    @staticmethod
    def _composition_mode(name: str):
        modes = {
            "normal": QPainter.CompositionMode.CompositionMode_SourceOver,
            "darken": QPainter.CompositionMode.CompositionMode_Darken,
            "multiply": QPainter.CompositionMode.CompositionMode_Multiply,
            "color_burn": QPainter.CompositionMode.CompositionMode_ColorBurn,
            "linear_burn": QPainter.CompositionMode.CompositionMode_Darken,
            "subtract": QPainter.CompositionMode.CompositionMode_Difference,
            "darker_color": QPainter.CompositionMode.CompositionMode_Darken,
            "lighten": QPainter.CompositionMode.CompositionMode_Lighten,
            "screen": QPainter.CompositionMode.CompositionMode_Screen,
            "color_dodge": QPainter.CompositionMode.CompositionMode_ColorDodge,
            "glow_dodge": QPainter.CompositionMode.CompositionMode_ColorDodge,
            "add": QPainter.CompositionMode.CompositionMode_Plus,
            "add_glow": QPainter.CompositionMode.CompositionMode_Plus,
            "lighter_color": QPainter.CompositionMode.CompositionMode_Lighten,
            "overlay": QPainter.CompositionMode.CompositionMode_Overlay,
            "soft_light": QPainter.CompositionMode.CompositionMode_SoftLight,
            "hard_light": QPainter.CompositionMode.CompositionMode_HardLight,
            "difference": QPainter.CompositionMode.CompositionMode_Difference,
            "vivid_light": QPainter.CompositionMode.CompositionMode_HardLight,
            "linear_light": QPainter.CompositionMode.CompositionMode_Plus,
            "pin_light": QPainter.CompositionMode.CompositionMode_HardLight,
            "hard_mix": QPainter.CompositionMode.CompositionMode_HardLight,
            "exclusion": QPainter.CompositionMode.CompositionMode_Exclusion,
            "hue": QPainter.CompositionMode.CompositionMode_SourceOver,
            "saturation": QPainter.CompositionMode.CompositionMode_SourceOver,
            "color": QPainter.CompositionMode.CompositionMode_SourceOver,
            "luminosity": QPainter.CompositionMode.CompositionMode_SourceOver,
            "divide": QPainter.CompositionMode.CompositionMode_Screen,
            "burn": QPainter.CompositionMode.CompositionMode_ColorBurn,
        }
        return modes.get(
            name, QPainter.CompositionMode.CompositionMode_SourceOver
        )

    @staticmethod
    def _rgb_to_hsl(rgb: np.ndarray) -> tuple[np.ndarray, ...]:
        maximum = rgb.max(axis=2)
        minimum = rgb.min(axis=2)
        delta = maximum - minimum
        lightness = (maximum + minimum) / 2.0
        saturation = np.zeros_like(lightness)
        active = delta > 1.0e-7
        saturation[active] = delta[active] / np.maximum(
            1.0e-7, 1.0 - np.abs(2.0 * lightness[active] - 1.0)
        )
        hue = np.zeros_like(lightness)
        red, green, blue = rgb[..., 0], rgb[..., 1], rgb[..., 2]
        mask = active & (maximum == red)
        hue[mask] = np.mod(
            (green[mask] - blue[mask]) / delta[mask], 6.0
        )
        mask = active & (maximum == green)
        hue[mask] = (blue[mask] - red[mask]) / delta[mask] + 2.0
        mask = active & (maximum == blue)
        hue[mask] = (red[mask] - green[mask]) / delta[mask] + 4.0
        return np.mod(hue / 6.0, 1.0), saturation, lightness

    @staticmethod
    def _hsl_to_rgb(
        hue: np.ndarray, saturation: np.ndarray, lightness: np.ndarray,
    ) -> np.ndarray:
        chroma = (1.0 - np.abs(2.0 * lightness - 1.0)) * saturation
        sector = np.mod(hue, 1.0) * 6.0
        secondary = chroma * (1.0 - np.abs(np.mod(sector, 2.0) - 1.0))
        zero = np.zeros_like(chroma)
        rgb = np.zeros((*hue.shape, 3), dtype=np.float32)
        choices = (
            (chroma, secondary, zero), (secondary, chroma, zero),
            (zero, chroma, secondary), (zero, secondary, chroma),
            (secondary, zero, chroma), (chroma, zero, secondary),
        )
        for index, components in enumerate(choices):
            mask = np.floor(sector).astype(np.int8) == index
            for channel, values in enumerate(components):
                rgb[..., channel][mask] = values[mask]
        offset = lightness - chroma / 2.0
        return np.clip(rgb + offset[..., None], 0.0, 1.0)

    def _special_blend(
        self, image: QImage, coverage: np.ndarray, color: QColor,
        mode: str,
    ) -> QImage | None:
        special = {
            "linear_burn", "subtract", "darker_color", "glow_dodge",
            "add_glow", "lighter_color", "vivid_light", "linear_light",
            "pin_light", "hard_mix", "hue", "saturation", "color",
            "luminosity", "divide", "black_burn", "white_burn",
            "erase_compare", "compare_density", "replace_alpha",
        }
        if mode not in special:
            return None
        destination = self._rgba_array(image, self.tile_size).astype(
            np.float32
        ) / 255.0
        backdrop = destination[..., :3]
        backdrop_alpha = destination[..., 3]
        source = np.array([
            color.redF(), color.greenF(), color.blueF()
        ], dtype=np.float32)
        source_rgb = np.broadcast_to(source, backdrop.shape)
        source_alpha = np.clip(
            coverage * float(color.alphaF()), 0.0, 1.0
        )
        if mode == "replace_alpha":
            weight = np.clip(coverage, 0.0, 1.0)
            output_rgb = (
                backdrop * (1.0 - weight[..., None])
                + source_rgb * weight[..., None]
            )
            output_alpha = backdrop_alpha * (
                1.0 - weight + weight * float(color.alphaF())
            )
        elif mode == "erase_compare":
            erase = source_alpha < (1.0 - backdrop_alpha)
            output_rgb = backdrop
            output_alpha = np.where(
                erase, backdrop_alpha * (1.0 - source_alpha),
                backdrop_alpha,
            )
        else:
            if mode == "compare_density":
                source_alpha = np.where(
                    source_alpha > backdrop_alpha, source_alpha, 0.0
                )
                blend = source_rgb
            elif mode == "linear_burn":
                blend = np.clip(backdrop + source_rgb - 1.0, 0.0, 1.0)
            elif mode == "subtract":
                blend = np.clip(backdrop - source_rgb, 0.0, 1.0)
            elif mode in {"darker_color", "lighter_color"}:
                source_luma = np.sum(source_rgb * (0.299, 0.587, 0.114), axis=2)
                back_luma = np.sum(backdrop * (0.299, 0.587, 0.114), axis=2)
                choose_source = (
                    source_luma < back_luma
                    if mode == "darker_color" else source_luma > back_luma
                )
                blend = np.where(
                    choose_source[..., None], source_rgb, backdrop
                )
            elif mode in {"glow_dodge", "white_burn"}:
                blend = np.clip(
                    backdrop / np.maximum(1.0 - source_rgb, 1.0e-5),
                    0.0, 1.0,
                )
            elif mode == "add_glow":
                blend = np.clip(backdrop + source_rgb, 0.0, 1.0)
            elif mode in {"vivid_light", "hard_mix"}:
                low = 1.0 - np.minimum(
                    1.0, (1.0 - backdrop) / np.maximum(2.0 * source_rgb, 1.0e-5)
                )
                high = np.minimum(
                    1.0, backdrop / np.maximum(2.0 * (1.0 - source_rgb), 1.0e-5)
                )
                blend = np.where(source_rgb <= 0.5, low, high)
                if mode == "hard_mix":
                    blend = (blend >= 0.5).astype(np.float32)
            elif mode == "linear_light":
                blend = np.clip(backdrop + 2.0 * source_rgb - 1.0, 0.0, 1.0)
            elif mode == "pin_light":
                blend = np.where(
                    source_rgb <= 0.5,
                    np.minimum(backdrop, 2.0 * source_rgb),
                    np.maximum(backdrop, 2.0 * source_rgb - 1.0),
                )
            elif mode == "divide":
                blend = np.clip(
                    backdrop / np.maximum(source_rgb, 1.0e-5), 0.0, 1.0
                )
            elif mode == "black_burn":
                blend = 1.0 - np.minimum(
                    1.0, (1.0 - backdrop) / np.maximum(source_rgb, 1.0e-5)
                )
                source_alpha = np.where(
                    backdrop_alpha > 0.0, source_alpha, 0.0
                )
            elif mode in {"hue", "saturation", "color", "luminosity"}:
                bh, bs, bl = self._rgb_to_hsl(backdrop)
                sh, ss, sl = self._rgb_to_hsl(source_rgb)
                blend = self._hsl_to_rgb(
                    sh if mode in {"hue", "color"} else bh,
                    ss if mode in {"saturation", "color"} else bs,
                    sl if mode == "luminosity" else bl,
                )
            else:
                blend = source_rgb
            output_alpha = source_alpha + backdrop_alpha * (
                1.0 - source_alpha
            )
            premultiplied = (
                source_alpha[..., None] * (1.0 - backdrop_alpha[..., None])
                * source_rgb
                + backdrop_alpha[..., None] * (1.0 - source_alpha[..., None])
                * backdrop
                + source_alpha[..., None] * backdrop_alpha[..., None] * blend
            )
            output_rgb = np.divide(
                premultiplied, output_alpha[..., None],
                out=np.zeros_like(premultiplied),
                where=output_alpha[..., None] > 1.0e-7,
            )
        rgba = np.empty_like(destination)
        rgba[..., :3] = np.clip(output_rgb, 0.0, 1.0)
        rgba[..., 3] = np.clip(output_alpha, 0.0, 1.0)
        pixels = np.ascontiguousarray(np.rint(rgba * 255.0).astype(np.uint8))
        return QImage(
            pixels.data, self.tile_size, self.tile_size, self.tile_size * 4,
            QImage.Format.Format_RGBA8888,
        ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)

    def advanced_fill(
        self, object_id: str, point: QPointF | None, bounds: QRectF,
        color: QColor, profile: dict[str, object],
        before: dict[tuple[int, int], QImage | None] | None = None,
        *,
        region_policy: str = "seed",
        reference_tile: Callable[[tuple[int, int]], QImage | None] | None = None,
        selection_tile: Callable[[tuple[int, int]], np.ndarray | None] | None = None,
        cancel_check: Callable[[], bool] | None = None,
    ) -> QRectF:
        """Sparse profile-driven fill over lazily supplied RGBA/mask tiles."""
        if cancel_check is not None and cancel_check():
            return QRectF()
        frame = QRectF(bounds).normalized()
        left, top = math.floor(frame.left()), math.floor(frame.top())
        right, bottom = math.ceil(frame.right()) - 1, math.ceil(frame.bottom()) - 1
        if right < left or bottom < top:
            return QRectF()
        tile_size = self.tile_size
        object_tiles = self._tiles.setdefault(object_id, {})
        rgba_cache: dict[tuple[int, int], np.ndarray] = {}
        reference_cache: dict[tuple[int, int], np.ndarray] = {}
        target_masks: dict[tuple[int, int], np.ndarray] = {}
        raw_masks: dict[tuple[int, int], np.ndarray] = {}
        blocker_masks: dict[tuple[int, int], np.ndarray] = {}
        masks: dict[tuple[int, int], np.ndarray] = {}

        def rgba_for(key: tuple[int, int]) -> np.ndarray:
            if key not in rgba_cache:
                rgba_cache[key] = self._rgba_array(
                    object_tiles.get(key), tile_size
                )
            return rgba_cache[key]

        def reference_for(key: tuple[int, int]) -> np.ndarray:
            if key not in reference_cache:
                image = (
                    reference_tile(key) if reference_tile is not None
                    else object_tiles.get(key)
                )
                reference_cache[key] = self._rgba_array(image, tile_size)
            return reference_cache[key]

        seed_x = seed_y = 0
        seed_key = (0, 0)
        seed_local = (0, 0)
        if point is not None:
            seed_x, seed_y = math.floor(point.x()), math.floor(point.y())
            if not (left <= seed_x <= right and top <= seed_y <= bottom):
                return QRectF()
            seed_key = (
                math.floor(seed_x / tile_size), math.floor(seed_y / tile_size)
            )
            seed_local = (
                seed_x - seed_key[0] * tile_size,
                seed_y - seed_key[1] * tile_size,
            )
            selected = selection_tile(seed_key) if selection_tile else None
            if selected is not None and not bool(
                selected[seed_local[1], seed_local[0]]
            ):
                return QRectF()
        target = reference_for(seed_key)[seed_local[1], seed_local[0]].copy()
        blocked_color = str(profile.get("do_not_start_color", "")).strip()
        if point is not None and QColor(blocked_color).isValid():
            blocked = QColor(blocked_color)
            blocked_rgba = np.array([
                blocked.red(), blocked.green(), blocked.blue(), blocked.alpha()
            ], dtype=np.int16)
            if np.max(np.abs(target.astype(np.int16) - blocked_rgba)) <= int(
                profile.get("tolerance", 16)
            ):
                return QRectF()
        tolerance = int(profile.get("tolerance", 16))
        region_policy = str(region_policy)

        def valid_frame_mask(key: tuple[int, int]) -> np.ndarray:
            result = np.ones((tile_size, tile_size), dtype=bool)
            origin_x, origin_y = key[0] * tile_size, key[1] * tile_size
            valid_left = max(0, left - origin_x)
            valid_right = min(tile_size - 1, right - origin_x)
            valid_top = max(0, top - origin_y)
            valid_bottom = min(tile_size - 1, bottom - origin_y)
            if valid_left:
                result[:, :valid_left] = False
            if valid_right + 1 < tile_size:
                result[:, valid_right + 1:] = False
            if valid_top:
                result[:valid_top, :] = False
            if valid_bottom + 1 < tile_size:
                result[valid_bottom + 1:, :] = False
            return result

        def target_matching_for(key: tuple[int, int]) -> np.ndarray:
            cached = target_masks.get(key)
            if cached is not None:
                return cached
            matching = self._fill_region_mask(
                reference_for(key), target, tolerance, region_policy
            )
            target_masks[key] = matching
            return matching

        def raw_matching_for(key: tuple[int, int]) -> np.ndarray:
            cached = raw_masks.get(key)
            if cached is not None:
                return cached
            matching = target_matching_for(key) & valid_frame_mask(key)
            selected = selection_tile(key) if selection_tile else None
            if selected is not None:
                matching &= selected.astype(bool)
            raw_masks[key] = matching
            return matching

        def blocker_for(key: tuple[int, int]) -> np.ndarray:
            cached = blocker_masks.get(key)
            if cached is None:
                cached = ~target_matching_for(key)
                blocker_masks[key] = cached
            return cached

        def haloed(
            key: tuple[int, int], source, operation,
            iterations: int, structure: np.ndarray | None = None,
        ) -> np.ndarray:
            if iterations <= 0:
                return source(key).copy()
            tile_radius = max(1, math.ceil(iterations / tile_size))
            tile_span = tile_radius * 2 + 1
            canvas = np.zeros(
                (tile_size * tile_span, tile_size * tile_span), dtype=bool
            )
            for oy in range(-tile_radius, tile_radius + 1):
                for ox in range(-tile_radius, tile_radius + 1):
                    canvas[
                        (oy + tile_radius) * tile_size:
                        (oy + tile_radius + 1) * tile_size,
                        (ox + tile_radius) * tile_size:
                        (ox + tile_radius + 1) * tile_size,
                    ] = source((key[0] + ox, key[1] + oy))
            processed = operation(
                canvas, structure=structure, iterations=iterations
            )
            start = tile_radius * tile_size
            return processed[
                start:start + tile_size, start:start + tile_size
            ].copy()

        def matching_for(key: tuple[int, int]) -> np.ndarray:
            cached = masks.get(key)
            if cached is not None:
                return cached
            matching = raw_matching_for(key).copy()
            gap = min(16, max(0, round(float(
                profile.get("gap_threshold", 0.0)
            ))))
            if bool(profile.get("close_gap", False)) and gap:
                # Close breaks in the reference boundary, not in the
                # fillable region.  Closing the latter erases thin outlines
                # and lets a seeded fill escape from an otherwise closed area.
                closed_blockers = haloed(
                    key, blocker_for, binary_closing, gap,
                    np.ones((3, 3), dtype=bool),
                )
                matching = raw_matching_for(key) & ~closed_blockers
            if not bool(profile.get("fill_narrow_areas", True)):
                current = lambda candidate: (
                    matching if candidate == key
                    else raw_matching_for(candidate)
                )
                matching = haloed(
                    key, current, binary_opening, 1,
                    np.ones((3, 3), dtype=bool),
                )
            matching &= valid_frame_mask(key)
            selected = selection_tile(key) if selection_tile else None
            if selected is not None:
                matching &= selected.astype(bool)
            masks[key] = matching
            return matching

        connected: dict[tuple[int, int], np.ndarray] = {}
        keys = self.keys_for_rect(frame)
        if not bool(profile.get("connected_pixels_only", True)) or point is None:
            connected = {key: matching_for(key).copy() for key in keys}
        else:
            label_cache: dict[tuple[int, int], np.ndarray] = {}

            def labels_for(key: tuple[int, int]) -> np.ndarray:
                if key not in label_cache:
                    labels, _ = connected_components(
                        matching_for(key), structure=np.array(
                            [[0, 1, 0], [1, 1, 1], [0, 1, 0]],
                            dtype=np.uint8,
                        )
                    )
                    label_cache[key] = labels.astype(np.int32, copy=False)
                return label_cache[key]

            seed_label = int(labels_for(seed_key)[seed_local[1], seed_local[0]])
            if seed_label <= 0:
                return QRectF()
            pending = [(seed_key, seed_label)]
            seen: set[tuple[tuple[int, int], int]] = set()
            directions = (
                ((-1, 0), (slice(None), 0), (slice(None), -1)),
                ((1, 0), (slice(None), -1), (slice(None), 0)),
                ((0, -1), (0, slice(None)), (-1, slice(None))),
                ((0, 1), (-1, slice(None)), (0, slice(None))),
            )
            components: dict[tuple[int, int], set[int]] = {}
            while pending:
                if cancel_check is not None and cancel_check():
                    return QRectF()
                key, component = pending.pop()
                node = key, int(component)
                if node in seen or key not in keys:
                    continue
                seen.add(node)
                labels = labels_for(key)
                components.setdefault(key, set()).add(int(component))
                for (dx, dy), own_edge, other_edge in directions:
                    positions = labels[own_edge] == component
                    if not np.any(positions):
                        continue
                    neighbor = key[0] + dx, key[1] + dy
                    if neighbor not in keys:
                        continue
                    adjacent = labels_for(neighbor)[other_edge]
                    for candidate in np.unique(adjacent[positions]):
                        if candidate > 0:
                            pending.append((neighbor, int(candidate)))
            connected = {
                key: np.isin(labels_for(key), tuple(values))
                for key, values in components.items()
            }

        scaling = (
            round(abs(float(profile.get("area_amount", 0.0))))
            if bool(profile.get("area_scaling", False)) else 0
        )
        if scaling:
            scaling = min(64, scaling)
            expand = float(profile.get("area_amount", 0.0)) > 0
            operation = binary_dilation if expand else binary_erosion
            original_connected = {
                key: value.copy() for key, value in connected.items()
            }
            scaling_mode = str(profile.get("area_mode", "round"))
            structure = (
                np.ones((3, 3), dtype=bool)
                if scaling_mode == "rectangle" else None
            )
            for key in keys:
                if cancel_check is not None and cancel_check():
                    return QRectF()
                connected[key] = haloed(
                    key,
                    lambda candidate: original_connected.get(
                        candidate,
                        np.zeros((tile_size, tile_size), dtype=bool),
                    ),
                    operation, scaling, structure,
                )
                selected = selection_tile(key) if selection_tile else None
                if selected is not None:
                    connected[key] &= selected.astype(bool)
                if scaling_mode == "darkest_pixel" and expand:
                    opaque_border = reference_for(key)[..., 3] >= 250
                    connected[key] &= (
                        ~opaque_border
                        | original_connected.get(
                            key,
                            np.zeros((tile_size, tile_size), dtype=bool),
                        )
                    )

        if bool(profile.get("include_vector_path", False)):
            original_connected = {
                key: value.copy() for key, value in connected.items()
            }
            for key in keys:
                if cancel_check is not None and cancel_check():
                    return QRectF()
                connected[key] = haloed(
                    key,
                    lambda candidate: original_connected.get(
                        candidate,
                        np.zeros((tile_size, tile_size), dtype=bool),
                    ),
                    binary_dilation, 1,
                    np.ones((3, 3), dtype=bool),
                ) & valid_frame_mask(key)
                selected = selection_tile(key) if selection_tile else None
                if selected is not None:
                    connected[key] &= selected.astype(bool)

        antialias = bool(profile.get("antialiasing", True))
        opacity = max(0.0, min(1.0, float(profile.get("opacity", 100)) / 100))
        blend = str(profile.get("blend_mode", "normal"))
        dirty = QRectF()
        pending_images: dict[tuple[int, int], QImage] = {}
        pending_before: dict[tuple[int, int], QImage | None] = {}
        for key, selected in connected.items():
            if cancel_check is not None and cancel_check():
                return QRectF()
            if not np.any(selected):
                continue
            destination = object_tiles.get(key)
            pending_before[key] = (
                QImage(destination) if destination is not None else None
            )
            image = (
                QImage(destination) if destination is not None
                else self._empty(tile_size)
            )
            coverage = (
                gaussian_filter(selected.astype(np.float32), 0.65)
                if antialias else selected.astype(np.float32)
            )
            coverage = np.clip(coverage * opacity, 0.0, 1.0)
            special = self._special_blend(
                image, coverage, color, blend
            )
            if special is not None:
                image = special
            else:
                erasing = blend == "erase" or (
                    color.alpha() == 0 and blend == "normal"
                )
                alpha = np.rint(
                    coverage * (255 if erasing else color.alpha())
                ).astype(np.uint8)
                source_rgba = np.empty((tile_size, tile_size, 4), dtype=np.uint8)
                source_rgba[..., 0] = color.red()
                source_rgba[..., 1] = color.green()
                source_rgba[..., 2] = color.blue()
                source_rgba[..., 3] = alpha
                source_rgba = np.ascontiguousarray(source_rgba)
                source = QImage(
                    source_rgba.data, tile_size, tile_size, tile_size * 4,
                    QImage.Format.Format_RGBA8888,
                ).copy().convertToFormat(QImage.Format.Format_ARGB32_Premultiplied)
                painter = QPainter(image)
                if erasing:
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_DestinationOut
                    )
                elif blend == "background":
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_DestinationOver
                    )
                elif blend == "compare_density":
                    painter.setCompositionMode(
                        QPainter.CompositionMode.CompositionMode_Lighten
                    )
                else:
                    painter.setCompositionMode(self._composition_mode(blend))
                painter.drawImage(0, 0, source)
                painter.end()
            pending_images[key] = image
            ys, xs = np.nonzero(selected)
            origin_x, origin_y = key[0] * tile_size, key[1] * tile_size
            changed = QRectF(
                origin_x + int(xs.min()), origin_y + int(ys.min()),
                int(xs.max() - xs.min() + 1),
                int(ys.max() - ys.min() + 1),
            )
            dirty = changed if dirty.isEmpty() else dirty.united(changed)
        if cancel_check is not None and cancel_check():
            return QRectF()
        for key, image in pending_images.items():
            if before is not None and key not in before:
                before[key] = pending_before[key]
            self.set_tile(object_id, key, image)
        return dirty

    def _paint_samples(
        self, object_id: str,
        samples: list[tuple[QPointF, float, float]], color: QColor,
        *, erase: bool, square: bool, antialias: bool,
        before: dict[tuple[int, int], QImage | None] | None,
        replace_alpha: bool = False,
    ) -> QRectF:
        if not samples:
            return QRectF()
        object_tiles = self._tiles.setdefault(object_id, {})
        grouped: dict[
            tuple[int, int], list[tuple[QPointF, float, float]]
        ] = {}
        dirty = QRectF()
        for point, size, opacity in samples:
            size = max(1.0, float(size))
            half = max(0.5, size / 2)
            dab_rect = QRectF(
                point.x() - half - 2, point.y() - half - 2,
                size + 4, size + 4,
            )
            dirty = dab_rect if dirty.isEmpty() else dirty.united(dab_rect)
            for key in self.keys_for_rect(dab_rect):
                if (
                    key not in object_tiles
                    and (
                        erase
                        or (replace_alpha and float(opacity) <= 0.0)
                    )
                ):
                    continue
                grouped.setdefault(key, []).append((point, size, opacity))
        if not grouped:
            return QRectF()
        bounds_cache = self._alpha_bounds.setdefault(object_id, {})
        for key, dabs in grouped.items():
            image = object_tiles.get(key)
            if before is not None and key not in before:
                before[key] = QImage(image) if image is not None else None
            if image is None:
                image = self._empty(self.tile_size)
                object_tiles[key] = image
                bounds_cache[key] = None
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, antialias)
            painter.setCompositionMode(
                QPainter.CompositionMode_Clear
                if erase else
                QPainter.CompositionMode_Source
                if replace_alpha else QPainter.CompositionMode_SourceOver
            )
            painter.setPen(Qt.NoPen)
            if erase:
                painter.setBrush(Qt.black)
            approximate = bounds_cache.get(key)
            cache_known = (
                key in bounds_cache
                and (object_id, key[0], key[1])
                not in self._alpha_bounds_dirty
            )
            for point, size, opacity in dabs:
                half = max(0.5, size / 2)
                local = QPointF(
                    point.x() - key[0] * self.tile_size,
                    point.y() - key[1] * self.tile_size,
                )
                if not erase:
                    actual = QColor(color)
                    actual.setAlphaF(max(0.0, min(1.0, opacity)))
                    painter.setBrush(actual)
                if square:
                    painter.drawRect(QRectF(
                        local.x() - half, local.y() - half, size, size
                    ))
                else:
                    painter.drawEllipse(local, half, half)
                if (
                    not erase and not replace_alpha
                    and cache_known and opacity > 0
                ):
                    dab_bounds = (
                        max(0, math.floor(local.x() - half)),
                        max(0, math.floor(local.y() - half)),
                        min(self.tile_size, math.ceil(local.x() + half)),
                        min(self.tile_size, math.ceil(local.y() + half)),
                    )
                    if approximate is None:
                        approximate = dab_bounds
                    else:
                        approximate = (
                            min(approximate[0], dab_bounds[0]),
                            min(approximate[1], dab_bounds[1]),
                            max(approximate[2], dab_bounds[2]),
                            max(approximate[3], dab_bounds[3]),
                        )
            painter.end()
            if erase or replace_alpha or not cache_known:
                self._alpha_bounds_dirty.add((object_id, key[0], key[1]))
            else:
                bounds_cache[key] = approximate
            self.dirty.add((object_id, key[0], key[1]))
        return dirty

    def iter_tiles(
        self, object_id: str, local_rect: QRectF | None = None,
    ) -> Iterator[tuple[tuple[int, int], QImage]]:
        object_tiles = self._tiles.get(object_id, {})
        if local_rect is None:
            yield from object_tiles.items()
            return
        if local_rect.isEmpty() or not object_tiles:
            return
        edges = (
            local_rect.left(), local_rect.right(),
            local_rect.top(), local_rect.bottom(),
        )
        if not all(math.isfinite(value) for value in edges):
            yield from object_tiles.items()
            return
        left = math.floor(local_rect.left() / self.tile_size)
        right = math.floor(local_rect.right() / self.tile_size)
        top = math.floor(local_rect.top() / self.tile_size)
        bottom = math.floor(local_rect.bottom() / self.tile_size)
        if right < left or bottom < top:
            return
        candidate_count = (right - left + 1) * (bottom - top + 1)
        if candidate_count <= max(64, len(object_tiles) * 2):
            for tile_y in range(top, bottom + 1):
                for tile_x in range(left, right + 1):
                    key = (tile_x, tile_y)
                    image = object_tiles.get(key)
                    if image is not None:
                        yield key, image
            return
        for (tile_x, tile_y), image in object_tiles.items():
            if left <= tile_x <= right and top <= tile_y <= bottom:
                yield (tile_x, tile_y), image

    def remove_object(self, object_id: str) -> None:
        self._tiles.pop(object_id, None)
        self._alpha_bounds.pop(object_id, None)
        self._alpha_bounds_dirty = {
            item for item in self._alpha_bounds_dirty if item[0] != object_id
        }
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
        bounds_cache = self._alpha_bounds.setdefault(object_id, {})
        bounds_cache.clear()
        for key, image in replacement.items():
            bounds_cache[key] = self._alpha_bbox(image)
        self._alpha_bounds_dirty = {
            item for item in self._alpha_bounds_dirty if item[0] != object_id
        }
        for x, y in previous | set(replacement):
            self.dirty.add((object_id, x, y))

    def content_bounds(self, object_id: str) -> QRectF | None:
        result = QRectF()
        found = False
        for (tile_x, tile_y), image in self._tiles.get(object_id, {}).items():
            bbox = self._cached_alpha_bbox(object_id, (tile_x, tile_y), image)
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
        bounds_cache = self._alpha_bounds.setdefault(object_id, {})
        for key in keys:
            image = object_tiles.get(key)
            if (
                image is not None
                and self._cached_alpha_bbox(object_id, key, image) is None
            ):
                object_tiles.pop(key, None)
                bounds_cache.pop(key, None)
                self._alpha_bounds_dirty.discard(
                    (object_id, key[0], key[1])
                )
                self.dirty.add((object_id, key[0], key[1]))

    @staticmethod
    def is_empty(image: QImage) -> bool:
        return TileStore._alpha_bbox(image) is None

    def _cached_alpha_bbox(
        self, object_id: str, key: tuple[int, int], image: QImage,
    ) -> tuple[int, int, int, int] | None:
        marker = (object_id, key[0], key[1])
        cache = self._alpha_bounds.setdefault(object_id, {})
        if key not in cache or marker in self._alpha_bounds_dirty:
            cache[key] = self._alpha_bbox(image)
            self._alpha_bounds_dirty.discard(marker)
        return cache[key]

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

    def load_directory(
        self, root: Path, object_ids: set[str], *, clear: bool = True,
    ) -> None:
        if clear:
            self._tiles.clear()
            self._alpha_bounds.clear()
            self._alpha_bounds_dirty.clear()
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
                    self._alpha_bounds_dirty.add((object_id, key[0], key[1]))
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
