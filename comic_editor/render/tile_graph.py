"""Demand-driven image nodes with explicit spatial dependencies.

Frames describe pixels, not the current camera. Kernels and caches are supplied
by the backend; this evaluator has no dependency on a widget or document model.
"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter


TILE_SIZE = 256


def tiles(frame, requested):
    for y in range(math.floor(requested.top() / TILE_SIZE), math.ceil(requested.bottom() / TILE_SIZE)):
        for x in range(math.floor(requested.left() / TILE_SIZE), math.ceil(requested.right() / TILE_SIZE)):
            bounds = QRectF(x * TILE_SIZE, y * TILE_SIZE, TILE_SIZE, TILE_SIZE).intersected(frame)
            if not bounds.isEmpty():
                yield (x, y), bounds


@dataclass(frozen=True)
class TileNode:
    frame: QRectF
    identity: tuple
    required_input: object  # (output region) -> input region
    evaluate: object       # (output region, input image, input region) -> image
    shared_frame: bool = False
    format: QImage.Format = QImage.Format_ARGB32_Premultiplied


class TileCacheMiss(Exception):
    """A cache-only request needs new pixels."""


class TileGraph:
    def __init__(self, nodes, source, get, put, *, cache_only=False):
        self.nodes = tuple(nodes)
        self.source = source
        self.get, self.put = get, put
        self.cache_only = cache_only
        self._request_cache = {}
        self._frames = {}

    def tile(self, index, address, bounds):
        node = self.nodes[index]
        work = node.frame if node.shared_frame else bounds
        key = ("effect-tile", node.identity, tuple(work.getRect()))
        owner = (index, "frame" if node.shared_frame else address)
        cached = self._request_cache.get(key)
        if cached is None:
            cached = self.get(owner, key)
        if cached is None:
            if self.cache_only:
                raise TileCacheMiss()
            if index == 0:
                cached = self.source(work)
            else:
                requirement = node.required_input(work)
                if isinstance(requirement, QRectF):
                    needed = QRectF(requirement.toAlignedRect())
                    incoming = self.region(index - 1, needed)
                else:
                    # Copies/warps can need distant, disjoint source islands.
                    # Do not allocate the empty space between them.
                    needed = tuple(QRectF(rect.toAlignedRect()) for rect in requirement)
                    incoming = tuple(self.region(index - 1, rect) for rect in needed)
                cached = node.evaluate(work, incoming, needed)
            if cached is None or cached.isNull():
                raise TileCacheMiss()
            self.put(owner, key, cached)
        self._request_cache[key] = cached
        if work == bounds:
            return cached
        rect = QRectF(bounds)
        rect.translate(-work.topLeft())
        return cached.copy(rect.toAlignedRect())

    def region(self, index, requested):
        node = self.nodes[index]
        requested = QRectF(requested.toAlignedRect())
        frame_key = (index, tuple(requested.getRect()))
        if frame_key in self._frames:
            return self._frames[frame_key]
        # Callers can request halos outside the semantic frame. They are empty,
        # rather than clamped edge pixels, until a kernel applies its edge rule.
        result = QImage(max(1, int(requested.width())), max(1, int(requested.height())), node.format)
        if result.isNull():
            raise MemoryError("Could not allocate effect region")
        result.fill(Qt.transparent)
        assembled = [(bounds, self.tile(index, address, bounds))
                     for address, bounds in tiles(node.frame, requested.intersected(node.frame))]
        painter = QPainter(result)
        painter.setCompositionMode(QPainter.CompositionMode_Source)
        try:
            for bounds, image in assembled:
                painter.drawImage(bounds.topLeft() - requested.topLeft(), image)
        finally:
            painter.end()
        if requested == node.frame:
            self._frames[frame_key] = result
        return result

    def output(self, requested):
        bounds = QRectF(self.nodes[-1].frame.intersected(requested).toAlignedRect())
        return self.region(len(self.nodes) - 1, bounds), bounds
