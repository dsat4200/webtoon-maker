"""Explicit region rendering and bounded tile scheduling.

The synchronous backend owns scene traversal; the service owns image composition,
requests, revision checks, and retained document tiles. No editor/UI imports are
allowed here. A legacy backend must only be invoked on its owning thread.
"""
from __future__ import annotations

from collections import defaultdict
from contextlib import AbstractContextManager
from dataclasses import dataclass
from enum import Enum
import math
import time
from typing import Callable, Protocol

from PySide6.QtCore import QRect, QRectF, QSize, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from .projection import DocumentProjection, ProjectionAddress, ProjectionRequest
from .pixels import LEGACY_PIXELS, PixelContract, pixel_scope


class RenderQuality(Enum):
    INTERACTIVE = "interactive"
    EXACT = "exact"


class RenderStatus(Enum):
    EXACT = "exact"
    PROVISIONAL = "provisional"
    PENDING = "pending"
    FAILED = "failed"
    STALE = "stale"


class RenderPending(Exception):
    """A required exact dependency has been queued but is not ready."""


class RenderFailed(RuntimeError):
    """A required exact dependency failed."""


def _immutable_sequence(value):
    return tuple(_immutable_sequence(item) if isinstance(item, (list, tuple)) else item
                 for item in value)


@dataclass(frozen=True)
class RenderDocument:
    """Immutable identity and display metadata, not a copy of source pixels."""

    identity: tuple
    configuration: tuple
    revision: int
    width: int
    height: int
    background: str
    overflow: float = 0.
    underlay: tuple[str, float] = ("", 0.)
    live_preview: bool = False
    pixel_contract: PixelContract = LEGACY_PIXELS

    def __post_init__(self):
        if not isinstance(self.pixel_contract, PixelContract):
            raise ValueError("Render documents require a validated pixel contract")
        for name in ("identity", "configuration", "underlay"):
            object.__setattr__(self, name, _immutable_sequence(getattr(self, name)))

    @property
    def bounds(self):
        return QRectF(0, 0, self.width, self.height)


@dataclass(frozen=True)
class RenderRequest:
    """All capture coordinates are document coordinates, independent of camera."""

    region: tuple[float, float, float, float]
    scale: float
    pixel_size: tuple[int, int]
    key: tuple
    revision: int
    requested_region: tuple[float, float, float, float] | None = None
    phase: str | None = None
    quality: RenderQuality = RenderQuality.EXACT
    defer_effects: bool = False

    def __post_init__(self):
        # Detach sequence inputs so callers cannot mutate a queued request.
        for name in ("region", "pixel_size", "key"):
            object.__setattr__(self, name, _immutable_sequence(getattr(self, name)))
        if self.requested_region is not None:
            object.__setattr__(self, "requested_region", tuple(self.requested_region))
        if (len(self.region) != 4 or not all(math.isfinite(v) for v in self.region)
                or self.region[2] <= 0 or self.region[3] <= 0):
            raise ValueError("Render region must have finite positive dimensions")
        if not math.isfinite(self.scale) or self.scale <= 0:
            raise ValueError("Render scale must be finite and positive")
        if (len(self.pixel_size) != 2
                or any(not isinstance(v, int) or v <= 0 for v in self.pixel_size)):
            raise ValueError("Render pixel dimensions must be positive integers")
        if self.requested_region is not None and (
                len(self.requested_region) != 4
                or not all(math.isfinite(v) for v in self.requested_region)
                or self.requested_region[2] < 0 or self.requested_region[3] < 0):
            raise ValueError("Requested region must have finite nonnegative dimensions")
        if self.phase not in (None, "base", "top"):
            raise ValueError("Unknown document output phase")
        if not isinstance(self.quality, RenderQuality):
            raise ValueError("Unknown render quality")

    @property
    def bounds(self):
        return QRectF(*self.region)

    @property
    def requested(self):
        return QRectF(*(self.requested_region or self.region))

    @property
    def transform(self):
        x, y, _, _ = self.region
        return QTransform(self.scale, 0, 0, self.scale, -x * self.scale, -y * self.scale)


@dataclass(frozen=True)
class RenderResult:
    request: RenderRequest
    document: RenderDocument
    image: QImage
    status: RenderStatus
    error: str = ""

    @property
    def exact(self):
        return self.status is RenderStatus.EXACT and not self.image.isNull()


@dataclass
class CaptureState:
    provisional: bool = False


class SceneBackend(Protocol):
    def matches(self, document: RenderDocument) -> bool: ...
    def capture(self, document: RenderDocument, request: RenderRequest,
                effect_region: QRectF) -> AbstractContextManager[CaptureState]: ...
    def paint(self, painter: QPainter, visible: QRectF, *, phase=None,
              page_contents_only=False) -> None: ...
    def page_area(self) -> QPainterPath: ...
    def overflow_channel(self) -> AbstractContextManager: ...


@dataclass(frozen=True)
class TileBatchPolicy:
    center: tuple[float, float]
    deadline: float | None = None
    blocks_started: int = 0


@dataclass
class TileBatchResult:
    tiles: dict
    blocks_started: int
    yielded: bool = False
    pending: bool = False
    error: str = ""


class DocumentRenderService:
    def __init__(self, backend: SceneBackend, *, projection=None):
        self.backend = backend
        self.projection = projection if projection is not None else DocumentProjection()

    def configure(self, configuration, *, document):
        self.projection.configure(configuration, document=document)

    def invalidate(self, region: QRectF | None = None):
        self.projection.invalidate(region)

    def current_document(self, document: RenderDocument):
        return (document.revision == self.projection.revision
                and self.backend.matches(document))

    def current(self, document: RenderDocument, request: RenderRequest):
        return request.revision == document.revision and self.current_document(document)

    def render_region(self, document: RenderDocument, request: RenderRequest) -> RenderResult:
        with pixel_scope(document.pixel_contract):
            return self._render_region(document, request)

    def _render_region(self, document: RenderDocument, request: RenderRequest) -> RenderResult:
        if not self.current(document, request):
            return RenderResult(request, document, QImage(), RenderStatus.STALE)
        image = QImage(QSize(*request.pixel_size), document.pixel_contract.image_format)
        if image.isNull():
            raise MemoryError("Could not allocate document render image")
        image.fill(Qt.transparent)
        bounds, visible = request.bounds, request.requested
        effect_region = QRectF(bounds)
        if document.overflow <= 0:
            visible = visible.intersected(document.bounds)
            effect_region = effect_region.intersected(document.bounds)
        status, error = RenderStatus.EXACT, ""
        try:
            with self.backend.capture(document, request, effect_region) as capture:
                painter = QPainter(image)
                try:
                    painter.setRenderHint(QPainter.Antialiasing, True)
                    painter.setTransform(request.transform)
                    if request.phase != "top":
                        painter.fillRect(document.bounds, QColor(document.background))
                        if document.overflow > 0:
                            self._paint_overflow(painter, document, request)
                    painter.setClipRect(document.bounds)
                    if not visible.isEmpty():
                        self.backend.paint(painter, visible, phase=request.phase)
                finally:
                    painter.end()
            if capture.provisional:
                status = RenderStatus.PROVISIONAL
        except RenderPending:
            status = RenderStatus.PENDING
        except RenderFailed as failure:
            status, error = RenderStatus.FAILED, str(failure)
        # Reentrant callbacks can switch the document or invalidate a region.
        # Such pixels must not acquire the latest revision merely by finishing.
        if not self.current(document, request):
            status, error = RenderStatus.STALE, ""
        presentable = (status is RenderStatus.EXACT or status is RenderStatus.PROVISIONAL
                       and request.quality is RenderQuality.INTERACTIVE)
        return RenderResult(request, document, image if presentable else QImage(), status, error)

    def _paint_overflow(self, painter, document, request):
        chapter_area = QPainterPath()
        chapter_area.addRect(document.bounds)
        area = QPainterPath()
        area.addRect(request.bounds)
        outside = area.subtracted(self.backend.page_area().intersected(chapter_area))
        if outside.isEmpty():
            return
        image = QImage(QSize(*request.pixel_size), document.pixel_contract.image_format)
        image.fill(Qt.transparent)
        overflow = QPainter(image)
        try:
            overflow.setRenderHint(QPainter.Antialiasing, True)
            overflow.setTransform(request.transform)
            overflow.setClipPath(outside)
            with self.backend.overflow_channel():
                self.backend.paint(overflow, request.bounds, page_contents_only=True)
        finally:
            overflow.end()
        painter.save()
        try:
            painter.setTransform(QTransform())
            painter.setOpacity(document.overflow)
            painter.drawImage(0, 0, image)
        finally:
            painter.restore()

    def render_tiles(self, document: RenderDocument, requests: list[ProjectionRequest],
                     policy: TileBatchPolicy, *, phase=None, defer_effects=False,
                     capture: Callable[[RenderRequest], RenderResult] | None = None):
        """Render fixed 4×4 capture blocks, preserving Qt clip antialiasing."""
        groups = defaultdict(list)
        for request in requests:
            address = request.address
            groups[(address.level, address.x // 4, address.y // 4)].append(request)
        batch = TileBatchResult({request.address: (QImage(), False) for request in requests},
                                policy.blocks_started)
        def distance(key):
            _, x, y = key
            request = groups[key][0]
            side = 4 * request.tile_size / request.scale
            return ((x + .5) * side - policy.center[0]) ** 2 + ((y + .5) * side - policy.center[1]) ** 2
        for key in sorted(groups, key=distance):
            if (policy.deadline is not None and time.perf_counter() >= policy.deadline
                    and batch.blocks_started > 0):
                batch.yielded = True
                break
            group = groups[key]
            batch.blocks_started += 1
            level, block_x, block_y = key
            block = [ProjectionRequest(ProjectionAddress(level, x, y),
                                       group[0].tile_size, group[0].gutter)
                     for y in range(block_y * 4, (block_y + 1) * 4)
                     for x in range(block_x * 4, (block_x + 1) * 4)]
            bounds, requested = QRectF(), QRectF()
            for tile in block:
                bounds = bounds.united(tile.capture_rect)
            for tile in group:
                requested = requested.united(tile.capture_rect)
            scale = group[0].scale
            request = RenderRequest(tuple(bounds.getRect()), scale,
                (round(bounds.width() * scale), round(bounds.height() * scale)), key,
                document.revision, tuple(requested.getRect()), phase,
                RenderQuality.EXACT, defer_effects)
            result = (capture(request) if capture is not None else self.render_region(document, request))
            # The callback contract is checked before pixels enter a tile cache.
            if (result.request != request or result.document != document
                    or not self.current(document, request)):
                batch.tiles = {tile.address: (QImage(), False) for tile in requests}
                break
            if result.exact and result.image.size() != QSize(*request.pixel_size):
                raise ValueError("Scene backend returned the wrong capture dimensions")
            for tile in group:
                offset = tile.capture_rect.topLeft() - bounds.topLeft()
                crop = QRect(round(offset.x() * scale), round(offset.y() * scale),
                             tile.pixel_size, tile.pixel_size)
                batch.tiles[tile.address] = (result.image.copy(crop) if result.exact else QImage(),
                                            result.exact)
            batch.pending = result.status is RenderStatus.PENDING
            batch.error = result.error
            if result.status in (RenderStatus.PENDING, RenderStatus.FAILED, RenderStatus.STALE):
                break
        return batch

    def collect(self, requests, render, *, render_many=None):
        return self.projection.collect(requests, render, render_many=render_many)

    def collect_tiles(self, document, requests, policy, *, phase=None, defer_effects=False):
        """Evaluate/cache document tiles directly, without canvas callbacks."""
        if not self.current_document(document):
            return []
        self.configure((*document.configuration, phase), document=document.identity)
        if not self.current_document(document):
            return []
        def render_tile(tile):
            request = RenderRequest(tuple(tile.capture_rect.getRect()), tile.scale,
                (tile.pixel_size, tile.pixel_size),
                (tile.address.level, tile.address.x, tile.address.y), document.revision,
                phase=phase, defer_effects=defer_effects)
            result = self.render_region(document, request)
            return result.image, result.exact
        return self.collect(requests, render_tile, render_many=lambda missing:
            self.render_tiles(document, missing, policy, phase=phase,
                              defer_effects=defer_effects).tiles)
