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
    cached_output: object = None  # (output region) -> completed image or None.


class TileCacheMiss(Exception):
    """A cache-only request needs new pixels."""


class TileGraph:
    def __init__(self, nodes, source, get, put, *, cache_only=False, tile_size=TILE_SIZE,
                 continue_pending=None, region_store=None):
        if not isinstance(tile_size, int) or tile_size <= 0:
            raise ValueError('Tile size must be a positive integer')
        self.nodes = tuple(nodes)
        self.continue_pending = continue_pending
        # An exact asynchronous backend can retain unfinished frame assembly.
        # Its store owns admission/eviction; partial pixels never enter get/put.
        # The ordinary pure/synchronous evaluator keeps its original behavior.
        self.region_store = None if cache_only else region_store
        self.source = source
        self.get, self.put = get, put
        self.cache_only = cache_only
        self.tile_size = tile_size
        self._request_cache = {}
        self._frames = {}
        self._pending_frames = {}

    def tile(self, index, address, bounds):
        if self.region_store is not None:
            self.region_store.validate()
        node = self.nodes[index]
        work = node.frame if node.shared_frame else bounds
        key = ("effect-tile", node.identity, tuple(work.getRect()))
        owner = (index, "frame" if node.shared_frame else address)
        cached = self._request_cache.get(key)
        if cached is None:
            cached = self.get(owner, key)
        if cached is None and node.cached_output is not None:
            # A backend stage can already own the exact output, or be waiting
            # for its detached worker, before this graph has adopted its tile.
            # Check that semantic result before rebuilding distant inputs.
            cached = node.cached_output(work)
            if self.region_store is not None:
                # Worker adoption can emit a reentrant document/history change.
                # Validate before handing its pixels to this graph's backend.
                self.region_store.validate()
            if cached is not None and cached.isNull():
                cached = None
            if cached is not None and not self.cache_only:
                # A completed preflight needs the same retained handoff as a
                # newly evaluated tile. Otherwise its raw worker/pipeline alias
                # can leave the LRU while fresh graphs still need this frame.
                self.put(owner, key, cached)
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
        if self.region_store is not None:
            self.region_store.validate()
        node = self.nodes[index]
        requested = QRectF(requested.toAlignedRect())
        frame_key = (index, tuple(requested.getRect()))
        if frame_key in self._frames:
            return self._frames[frame_key]
        if frame_key in self._pending_frames:
            # Spatial siblings frequently need the same complete predecessor.
            # Retrying its unfinished traversal in this capture multiplies
            # work through each warp. A fresh graph retries after publication.
            raise self._pending_frames[frame_key]
        if node.shared_frame:
            # A whole-frame stage has one exact result. Repeatedly cropping its
            # native tiles and painting them into a second full image wastes
            # memory and can displace the very result being consumed.
            try:
                result = self.tile(index, 'frame', node.frame)
            except RenderPending as error:
                self._pending_frames[frame_key] = error
                raise
            if (result.format() == node.format
                    and node.frame == QRectF(node.frame.toAlignedRect())
                    and result.size() == node.frame.size().toSize()
                    and result.devicePixelRatio() == 1
                    and not result.colorSpace().isValid()):
                if requested != node.frame:
                    crop = QRectF(requested)
                    crop.translate(-node.frame.topLeft())
                    result = result.copy(crop.toAlignedRect())
                self._frames[frame_key] = result
                return result
            # Unusual kernel format/DPR/color metadata keeps the established
            # per-tile QPainter conversion and sampling path below.
        coverage = list(tiles(node.frame, requested.intersected(node.frame), self.tile_size))
        if self.region_store is not None and len(coverage) > 1:
            return self._retained_region(index, requested, frame_key, coverage)
        if len(coverage) == 1 and coverage[0][1] == requested:
            address, bounds = coverage[0]
            try:
                image = self.tile(index, address, bounds)
            except RenderPending as error:
                self._pending_frames[frame_key] = error
                raise
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
                    self._pending_frames[frame_key] = error
                    raise
                pending = pending or error
        if pending is not None:
            # Some neighbors may already have entered their tile caches. An
            # incomplete assembled region never becomes a finished image.
            self._pending_frames[frame_key] = pending
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

    def _retained_region(self, index, requested, frame_key, coverage):
        node = self.nodes[index]
        store = self.region_store
        key = ('tile-region-assembly', node.identity, tuple(requested.getRect()),
               node.format.value, self.tile_size)
        entry = store.get(key)
        if entry is not None and entry.complete:
            result = QImage(entry.image)
            self._frames[frame_key] = result
            return result
        pending = None
        for address, bounds in coverage:
            # Worker adoption may evict an older record, or replace a document
            # reentrantly. Do not write through a stale private buffer handle.
            entry = store.get(key)
            if entry is not None and address in entry.covered:
                continue
            try:
                image = self.tile(index, address, bounds)
            except RenderPending as error:
                if self.continue_pending is None or not self.continue_pending():
                    self._pending_frames[frame_key] = error
                    raise
                pending = pending or error
                continue
            if entry is None or not store.current(key, entry):
                entry = store.begin(key, requested, node.format, len(coverage))
            painter = QPainter(entry.image)
            painter.setCompositionMode(QPainter.CompositionMode_Source)
            try:
                painter.drawImage(bounds.topLeft() - requested.topLeft(), image)
            finally:
                painter.end()
            entry.covered.add(address)
        entry = store.get(key)
        if entry is not None and len(entry.covered) == len(coverage):
            result = store.finish(key, entry)
            self._frames[frame_key] = result
            return result
        if pending is None:
            # Admission can evict a predecessor while this capture owns its
            # COW image. Retry missing addresses instead of publishing holes.
            pending = RenderPending('Exact frame assembly was displaced')
        self._pending_frames[frame_key] = pending
        raise pending

    def output(self, requested):
        bounds = QRectF(self.nodes[-1].frame.intersected(requested).toAlignedRect())
        return self.region(len(self.nodes) - 1, bounds), bounds
