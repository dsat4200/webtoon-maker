"""Bounded, exact document tiles shared by all camera presentations.

The cache knows nothing about the viewport transform or mutable editor state.
Callers invalidate document regions and provide a renderer for missing tiles.
An unfinished render never replaces an existing exact tile with a draft.
"""
from __future__ import annotations

from collections import OrderedDict
from bisect import bisect_left
from dataclasses import dataclass
import math
from typing import Callable

from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage


# Include physical-pixel density above the editor's 8x logical zoom for HiDPI
# displays. Tile dimensions and memory accounting remain independent of level.
RESOLUTION_SCALES = (1., 1.25, 1.5, 2., 3., 4., 6., 8., 12., 16., 24., 32., 48., 64.)


@dataclass(frozen=True)
class ProjectionAddress:
    level: int
    x: int
    y: int


@dataclass(frozen=True)
class ProjectionRequest:
    address: ProjectionAddress
    tile_size: int = 256
    gutter: int = 2

    @property
    def scale(self) -> float:
        return RESOLUTION_SCALES[self.address.level]

    @property
    def world_rect(self) -> QRectF:
        side = self.tile_size / self.scale
        return QRectF(self.address.x * side, self.address.y * side, side, side)

    @property
    def capture_rect(self) -> QRectF:
        margin = self.gutter / self.scale
        return self.world_rect.adjusted(-margin, -margin, margin, margin)

    @property
    def source_rect(self) -> QRectF:
        return QRectF(self.gutter, self.gutter, self.tile_size, self.tile_size)

    @property
    def pixel_size(self) -> int:
        return self.tile_size + 2 * self.gutter


@dataclass
class ProjectionTile:
    request: ProjectionRequest
    image: QImage
    revision: int
    valid: bool = True


class DocumentProjection:
    """Retain finished tiles until their document pixels actually change."""

    def __init__(self, *, tile_size=256, gutter=2, budget=256 * 1024 * 1024,
                 configuration_limit=8):
        self.tile_size = max(1, int(tile_size))
        self.gutter = max(1, int(gutter))
        self.budget = max(0, int(budget))
        self.tiles: OrderedDict[ProjectionAddress, ProjectionTile] = OrderedDict()
        self._configurations = OrderedDict([(None, self.tiles)])
        self._document = None
        self.configuration_limit = max(1, int(configuration_limit))
        self.bytes = 0
        self.revision = 0
        self.configuration = None
        self.hits = self.renders = self.incomplete = self.evictions = 0

    @staticmethod
    def resolution_level(pixels_per_unit: float) -> int:
        # Never enlarge a low-resolution preview to manufacture vector detail.
        # Level zero also preserves native raster/effect pixels when zoomed out.
        value = max(1., min(RESOLUTION_SCALES[-1], float(pixels_per_unit)))
        return bisect_left(RESOLUTION_SCALES, value)

    @classmethod
    def resolution_scale(cls, pixels_per_unit: float) -> float:
        return RESOLUTION_SCALES[cls.resolution_level(pixels_per_unit)]

    def configure(self, signature, *, document=None) -> None:
        # A temporary solo/underlay view is another rendering of the same
        # document. Keep its finished pixels without giving it a new budget.
        # Standalone callers retain the traditional document-switch behavior.
        document = signature if document is None else document
        if document != self._document:
            self.clear()
            self._configurations.clear()
            self._document = document
        self.configuration = signature
        self.tiles = self._configurations.setdefault(signature, OrderedDict())
        self._configurations.move_to_end(signature)
        while len(self._configurations) > self.configuration_limit:
            _, removed = self._configurations.popitem(last=False)
            self.bytes -= sum(tile.image.sizeInBytes() for tile in removed.values())
            self.evictions += len(removed)

    def clear(self) -> None:
        self.tiles = OrderedDict()
        self._configurations = OrderedDict([(self.configuration, self.tiles)])
        self.bytes = 0
        self.revision += 1

    def invalidate(self, world: QRectF | None = None) -> None:
        """Keep old exact pixels available while their replacement is evaluated."""
        self.revision += 1
        for tiles in self._configurations.values():
            for tile in tiles.values():
                if world is None or tile.request.capture_rect.intersects(world):
                    tile.valid = False

    def requests(self, world: QRectF, pixels_per_unit: float) -> list[ProjectionRequest]:
        if world.isEmpty() or not all(math.isfinite(v) for v in (
            world.x(), world.y(), world.width(), world.height(), pixels_per_unit,
        )):
            return []
        level = self.resolution_level(pixels_per_unit)
        side = self.tile_size / RESOLUTION_SCALES[level]
        left, top = math.floor(world.left() / side), math.floor(world.top() / side)
        right, bottom = math.ceil(world.right() / side), math.ceil(world.bottom() / side)
        return [ProjectionRequest(ProjectionAddress(level, x, y), self.tile_size, self.gutter)
                for y in range(top, bottom) for x in range(left, right)]

    def collect(self, requests: list[ProjectionRequest],
                render: Callable[[ProjectionRequest], tuple[QImage, bool]], *,
                render_many=None) -> list[ProjectionTile]:
        protected = {request.address for request in requests}
        missing = [request for request in requests
                   if request.address not in self.tiles or not self.tiles[request.address].valid]
        rendered = render_many(missing) if missing and render_many is not None else {}
        # Batch captures may finish neighboring tiles for free. Keep those
        # pixels too, so moving into the rest of that block needs no recapture.
        for address, (image, exact) in rendered.items():
            if address in protected or not exact or image.isNull():
                continue
            request = ProjectionRequest(address, self.tile_size, self.gutter)
            existing = self.tiles.get(address)
            if existing is not None:
                if existing.valid:
                    continue
                self.bytes -= existing.image.sizeInBytes()
            self.tiles[address] = ProjectionTile(request, QImage(image), self.revision)
            self.bytes += image.sizeInBytes()
            self.renders += 1
        result = []
        for request in requests:
            tile = self.tiles.get(request.address)
            if tile is not None and tile.valid:
                self.hits += 1
                self.tiles.move_to_end(request.address)
            else:
                self.renders += 1
                image, exact = (rendered[request.address] if request.address in rendered
                                else render(request))
                if exact and not image.isNull():
                    if image.width() != request.pixel_size or image.height() != request.pixel_size:
                        raise ValueError("Projection renderer returned the wrong tile dimensions")
                    if tile is not None:
                        self.bytes -= tile.image.sizeInBytes()
                    tile = ProjectionTile(request, QImage(image), self.revision)
                    self.tiles[request.address] = tile
                    self.tiles.move_to_end(request.address)
                    self.bytes += image.sizeInBytes()
                else:
                    self.incomplete += 1
            if tile is not None:
                result.append(tile)
        self._trim(protected)
        return result

    def _trim(self, protected) -> None:
        # Visible tiles remain usable through this frame even when a tiny test
        # budget cannot hold it. Evict them from the cache after collecting:
        # the returned tile objects still own their implicitly shared pixels.
        for signature, tiles in self._configurations.items():
            for address in tuple(tiles):
                if self.bytes <= self.budget:
                    return
                if signature != self.configuration or address not in protected:
                    self.bytes -= tiles.pop(address).image.sizeInBytes()
                    self.evictions += 1
        while self.tiles and self.bytes > self.budget:
            _, tile = self.tiles.popitem(last=False)
            self.bytes -= tile.image.sizeInBytes()
            self.evictions += 1

    def snapshot(self) -> dict:
        return dict(tiles=sum(map(len, self._configurations.values())),
                    active_tiles=len(self.tiles), configurations=len(self._configurations),
                    bytes=self.bytes, hits=self.hits,
                    renders=self.renders, incomplete=self.incomplete, evictions=self.evictions)
