"""Latest-only asynchronous rendering for Blender-backed frame layers."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import threading
import time
from typing import Callable

from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen

from .renderer.offscreen import OffscreenRenderer, RenderOptions, RendererUnavailable
from .renderer.scene import SceneData


class RenderQuality(str, Enum):
    DRAFT = "draft"
    INTERACTIVE = "interactive"
    FINAL = "final"
    FULL = "final"


@dataclass(frozen=True, slots=True)
class RenderRequest:
    chapter_id: str
    frame_id: str
    scene_revision: int
    material_revision: int
    cache_revision: str
    target_size: tuple[int, int]
    quality: RenderQuality = RenderQuality.FINAL
    generation_id: int = 0
    scene: SceneData | None = None
    transparent: bool = True
    antialiasing: bool = False
    selected_node_ids: frozenset[str] = field(default_factory=frozenset)

    def __post_init__(self) -> None:
        object.__setattr__(self, "quality", RenderQuality(self.quality))
        width, height = self.target_size
        if not self.chapter_id or not self.frame_id:
            raise ValueError("render chapter/frame ids must be non-empty")
        if width <= 0 or height <= 0 or width > 16384 or height > 16384 or width * height > 64_000_000:
            raise ValueError("render target is outside the supported bounds")
        if min(self.scene_revision, self.material_revision, self.generation_id) < 0:
            raise ValueError("render revisions and generation id cannot be negative")
        object.__setattr__(
            self, "selected_node_ids",
            frozenset(str(item) for item in self.selected_node_ids),
        )


@dataclass(frozen=True, slots=True)
class RenderResult:
    generation_id: int
    image: QImage
    available: bool
    error: str | None
    render_ms: float
    request: RenderRequest


BackendFactory = Callable[[], OffscreenRenderer]


def unavailable_placeholder(size: tuple[int, int], reason: str = "3D renderer unavailable") -> QImage:
    """Return a transparent, premultiplied placeholder of the requested size."""
    width, height = int(size[0]), int(size[1])
    image = QImage(width, height, QImage.Format.Format_RGBA8888_Premultiplied)
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    try:
        tile = max(8, min(32, min(width, height) // 8 or 8))
        for y in range(0, height, tile):
            for x in range(0, width, tile):
                shade = 38 if ((x // tile) + (y // tile)) % 2 else 58
                painter.fillRect(x, y, min(tile, width - x), min(tile, height - y), QColor(shade, shade, shade, 42))
        painter.setPen(QPen(QColor(190, 80, 80, 180), max(1, min(width, height) // 128)))
        painter.drawRect(0, 0, max(0, width - 1), max(0, height - 1))
        painter.drawLine(0, 0, max(0, width - 1), max(0, height - 1))
        painter.drawLine(max(0, width - 1), 0, 0, max(0, height - 1))
        if width >= 180 and height >= 48:
            painter.setPen(QColor(225, 225, 225, 210))
            painter.drawText(image.rect(), 0x0084, reason[:160])  # AlignHCenter | AlignVCenter
    finally:
        painter.end()
    return image


class RenderService(QObject):
    """Own one worker/context and retain only the newest requested render.

    Signals are safe to connect to Qt GUI objects; Qt queues cross-thread signal
    delivery. ``result_ready`` is emitted for successful renders and no-GL
    placeholders alike. Consumers distinguish them through ``result.available``.
    """

    result_ready = Signal(object)
    render_failed = Signal(int, str)
    availability_changed = Signal(bool, str)

    def __init__(self, parent: QObject | None = None, *, backend_factory: BackendFactory = OffscreenRenderer) -> None:
        super().__init__(parent)
        self._backend_factory = backend_factory
        self._condition = threading.Condition()
        self._pending: RenderRequest | None = None
        self._latest: RenderResult | None = None
        self._desired_generation = 0
        self._next_generation = 1
        self._rendering = False
        self._stopping = False
        self._available = False
        self._reason = "renderer has not initialized"
        self._thread = threading.Thread(target=self._run, name="webtoon-3d-render", daemon=True)
        self._thread.start()

    @property
    def available(self) -> bool:
        with self._condition:
            return self._available

    @property
    def reason(self) -> str:
        with self._condition:
            return self._reason

    def submit(self, request: RenderRequest) -> int:
        """Queue ``request`` and replace any older request that has not started."""
        with self._condition:
            if self._stopping:
                raise RuntimeError("render service is shut down")
            generation = request.generation_id
            if generation < self._next_generation:
                generation = self._next_generation
            self._next_generation = generation + 1
            queued = replace(request, generation_id=generation)
            self._pending = queued
            self._desired_generation = generation
            self._condition.notify()
            return generation

    def cancel(self) -> None:
        """Discard pending work and suppress the result of an in-flight render."""
        with self._condition:
            self._pending = None
            self._desired_generation = self._next_generation
            self._next_generation += 1
            self._condition.notify_all()

    def latest_result(self) -> RenderResult | None:
        with self._condition:
            return self._latest

    def latest_image(self) -> QImage | None:
        result = self.latest_result()
        return result.image.copy() if result is not None else None

    def wait_idle(self, timeout: float = 5.0) -> bool:
        """Test/shutdown helper; GUI code normally waits for ``result_ready``."""
        deadline = time.monotonic() + max(timeout, 0.0)
        with self._condition:
            while (self._pending is not None or self._rendering) and not self._stopping:
                remaining = deadline - time.monotonic()
                if remaining <= 0.0:
                    return False
                self._condition.wait(remaining)
            return self._pending is None and not self._rendering

    def shutdown(self, wait: bool = True) -> None:
        with self._condition:
            if self._stopping:
                return
            self._stopping = True
            self._pending = None
            self._desired_generation = self._next_generation
            self._condition.notify_all()
        if wait and threading.current_thread() is not self._thread:
            self._thread.join(timeout=10.0)

    def _set_availability(self, available: bool, reason: str) -> None:
        changed = False
        with self._condition:
            if self._available != available or self._reason != reason:
                self._available, self._reason = available, reason
                changed = True
        if changed:
            self.availability_changed.emit(available, reason)

    def _run(self) -> None:
        backend: OffscreenRenderer | None = None
        backend_error: str | None = None
        try:
            try:
                backend = self._backend_factory()
                self._set_availability(True, "")
            except Exception as exc:
                backend_error = str(exc) or exc.__class__.__name__
                self._set_availability(False, backend_error)
            while True:
                with self._condition:
                    while self._pending is None and not self._stopping:
                        self._condition.wait()
                    if self._stopping:
                        return
                    request = self._pending
                    self._pending = None
                    self._rendering = True
                assert request is not None
                started = time.perf_counter()
                error: str | None = None
                if backend is None and request.scene is not None:
                    try:
                        backend = self._backend_factory()
                        backend_error = None
                        self._set_availability(True, "")
                    except Exception as exc:
                        backend_error = str(exc) or exc.__class__.__name__
                        self._set_availability(False, backend_error)
                available = backend is not None and request.scene is not None
                try:
                    if backend is None:
                        error = backend_error or "OpenGL renderer is unavailable"
                        image = unavailable_placeholder(request.target_size, error)
                    elif request.scene is None:
                        error = "3D scene data is unavailable"
                        image = unavailable_placeholder(request.target_size, error)
                    else:
                        interactive = request.quality in (RenderQuality.DRAFT, RenderQuality.INTERACTIVE)
                        options = RenderOptions(
                            interactive=interactive,
                            antialiasing=request.antialiasing and request.quality is RenderQuality.FINAL,
                            transparent=request.transparent,
                            selected_node_ids=request.selected_node_ids,
                        )
                        image = backend.render(request.scene, request.target_size, options)
                        if image.format() != QImage.Format.Format_RGBA8888_Premultiplied:
                            image = image.convertToFormat(QImage.Format.Format_RGBA8888_Premultiplied)
                        self._set_availability(True, "")
                except Exception as exc:
                    available = False
                    error = str(exc) or exc.__class__.__name__
                    image = unavailable_placeholder(request.target_size, error)
                    self._set_availability(False, error)
                    # A failed context is never reused. The next submission may
                    # create a fresh context after a driver reset or wake-up.
                    if backend is not None:
                        try:
                            backend.release()
                        except Exception:
                            pass
                        backend = None
                        backend_error = error
                elapsed = (time.perf_counter() - started) * 1000.0
                result = RenderResult(request.generation_id, image, available, error, elapsed, request)
                deliver = False
                with self._condition:
                    self._rendering = False
                    if request.generation_id == self._desired_generation and not self._stopping:
                        self._latest = result
                        deliver = True
                    self._condition.notify_all()
                if deliver:
                    if error:
                        self.render_failed.emit(request.generation_id, error)
                    self.result_ready.emit(result)
        finally:
            if backend is not None:
                try:
                    backend.release()
                except Exception:
                    pass
            with self._condition:
                self._rendering = False
                self._condition.notify_all()


__all__ = [
    "RenderQuality",
    "RenderRequest",
    "RenderResult",
    "RenderService",
    "unavailable_placeholder",
]
