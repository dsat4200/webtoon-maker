"""Demand-driven image nodes with explicit spatial dependencies.

Frames describe pixels, not the current camera. Kernels and caches are supplied
by the backend; this evaluator has no dependency on a widget or document model.
"""
from dataclasses import dataclass
import math

from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter
from comic_editor.render.service import RenderPending


TILE_SIZE = 256


def tiles(frame, requested, tile_size=TILE_SIZE):
    for y in range(math.floor(requested.top() / tile_size), math.ceil(requested.bottom() / tile_size)):
        for x in range(math.floor(requested.left() / tile_size), math.ceil(requested.right() / tile_size)):
            bounds = QRectF(x * tile_size, y * tile_size, tile_size, tile_size).intersected(frame)
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
    input_index: int | None = None  # Explicit bypass for a fused point prefix.


class TileCacheMiss(Exception):
    """A cache-only request needs new pixels."""


class TileGraph:
    def __init__(self, nodes, source, get, put, *, cache_only=False, tile_size=TILE_SIZE,
                 continue_pending=None):
        if not isinstance(tile_size, int) or tile_size <= 0:
            raise ValueError('Tile size must be a positive integer')
        self.nodes = tuple(nodes)
        self.continue_pending = continue_pending
        self.source = source
        self.get, self.put = get, put
        self.cache_only = cache_only
        self.tile_size = tile_size
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
                input_index = index - 1 if node.input_index is None else node.input_index
                if not 0 <= input_index < index:
                    raise ValueError('Tile inputs must precede their output node')
                requirement = node.required_input(work)
                if isinstance(requirement, QRectF):
                    needed = QRectF(requirement.toAlignedRect())
                    incoming = self.region(input_index, needed)
                else:
                    # Copies/warps can need distant, disjoint source islands.
                    # Do not allocate the empty space between them.
                    needed = tuple(QRectF(rect.toAlignedRect()) for rect in requirement)
                    incoming = tuple(self.region(input_index, rect) for rect in needed)
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
        coverage = list(tiles(node.frame, requested.intersected(node.frame), self.tile_size))
        if len(coverage) == 1 and coverage[0][1] == requested:
            address, bounds = coverage[0]
            image = self.tile(index, address, bounds)
            if image.format() == node.format and image.size() == requested.size().toSize():
                # A complete tile already has the requested frame and format.
                # Keep a COW image handle instead of repainting every pixel.
                result = QImage(image)
                if requested == node.frame:
                    self._frames[frame_key] = result
                return result
        assembled, pending = [], None
        for address, bounds in coverage:
            try:
                assembled.append((bounds, self.tile(index, address, bounds)))
            except RenderPending as error:
                if self.continue_pending is None or not self.continue_pending():
                    raise
                pending = pending or error
        if pending is not None:
            # Some neighbors may already have entered their tile caches. An
            # incomplete assembled region never becomes a finished image.
            raise pending
        # Callers can request halos outside the semantic frame. They are empty,
        # rather than clamped edge pixels, until a kernel applies its edge rule.
        result = QImage(max(1, int(requested.width())), max(1, int(requested.height())), node.format)
        if result.isNull():
            raise MemoryError("Could not allocate effect region")
        result.fill(Qt.transparent)
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
