"""One graphics owner thread with bounded, detached requests.

The GUI owns the native surface's creation/destruction. Contexts, programs,
textures and readbacks live entirely on the worker, with no shared GL objects.
"""
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass
import threading

import numpy as np
from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat

from .point_chain import GpuPointChain


@dataclass(frozen=True)
class _Request:
    future: Future
    operation: str
    pixels: np.ndarray
    parameters: object
    source_key: object
    parameter_key: object
    size: int


class _GraphicsThread(QThread):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.setObjectName('effect-graphics')

    def run(self):
        service = self.service
        renderer = None
        try:
            renderer = GpuPointChain(budget=service.gpu_budget, surface=service.surface)
            service.available, service.reason = renderer.available, renderer.reason
            service.ready.set()
            while True:
                with service.condition:
                    service.condition.wait_for(lambda: service.queue or service.closed)
                    if not service.queue:
                        break
                    request = service.queue.popleft()
                    future = request.future
                try:
                    if future.set_running_or_notify_cancel():
                        result = None
                        if renderer.available:
                            if request.operation == 'lut':
                                result = renderer.apply_lut(request.pixels, request.parameters,
                                    source_key=request.source_key, palette_key=request.parameter_key)
                            else:
                                result = renderer.apply_blur(request.pixels, request.parameters,
                                    source_key=request.source_key, algorithm=request.parameter_key)
                        if result is not None:
                            result.setflags(write=False)
                        future.set_result(result)
                except Exception as error:
                    future.set_exception(error)
                finally:
                    with service.condition:
                        service.queued_bytes -= request.size
                    service.stats = {name: getattr(renderer, name) for name in
                        ('uploads', 'readbacks', 'draws', 'hits', 'compiles', 'bytes')}
        except Exception as error:
            service.reason = str(error)
            service.ready.set()
        finally:
            try:
                if renderer is not None:
                    renderer.close()
                    del renderer  # Context and GL owners are destroyed on this thread.
            except Exception as error:
                service.reason = str(error)
            finally:
                service.available = False
                with service.condition:
                    while service.queue:
                        service.queue.popleft().future.cancel()
                    service.queued_bytes = 0


class GpuWorker(QObject):
    """Submit immutable pixel copies; a full queue asks callers to use the CPU."""
    def __init__(self, *, gpu_budget=128 * 1024 * 1024, queue_budget=128 * 1024 * 1024):
        super().__init__()
        app = QGuiApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError('Graphics surfaces must be created on the application thread')
        self.gpu_budget, self.queue_budget = max(0, int(gpu_budget)), max(0, int(queue_budget))
        self.queue, self.condition = deque(), threading.Condition()
        self.ready = threading.Event()
        self.queued_bytes = 0
        self.available = self.closed = False
        self.reason = ''
        self.stats = {}
        self.surface = self.worker_thread = None
        if app.platformName() == 'offscreen' or not QOpenGLContext.supportsThreadedOpenGL():
            self.reason = 'This platform does not support background OpenGL'
            self.ready.set()
            return
        fmt = QSurfaceFormat()
        fmt.setVersion(4, 3)
        fmt.setProfile(QSurfaceFormat.CoreProfile)
        self.surface = QOffscreenSurface()
        self.surface.setFormat(fmt)
        self.surface.create()
        if not self.surface.isValid():
            self.reason = 'Could not create the background graphics surface'
            self.ready.set()
            self.surface.destroy()
            self.surface = None
            return
        self.worker_thread = _GraphicsThread(self)
        self.worker_thread.finished.connect(self._cleanup_surface)
        self.worker_thread.start()

    def submit_lut(self, pixels, palette, *, source_key, palette_key):
        values = np.asarray(pixels)
        if values.ndim != 3 or values.shape[2] != 4:
            raise ValueError('Graphics input requires an H × W × RGBA array')
        if palette.shape != (256, 256, 4) or palette.dtype != np.float32:
            raise ValueError('Point tables require 256 × 256 × RGBA float32 values')
        size = values.size * 4 * 2 + palette.nbytes
        with self.condition:
            if (self.closed or self.worker_thread is None
                    or self.ready.is_set() and not self.available
                    or size > self.queue_budget - self.queued_bytes):
                return None
            # No mutable input crosses the thread boundary. The copy also keeps
            # a queued request's rows alive after the submitting owner returns.
            values = np.array(values, dtype=np.float32, copy=True, order='C')
            values.setflags(write=False)
            table = np.array(palette, dtype=np.float32, copy=True, order='C')
            table.setflags(write=False)
            future = Future()
            self.queue.append(_Request(future, 'lut', values, table, source_key, palette_key, size))
            self.queued_bytes += size
            self.condition.notify()
            return future

    def submit_blur(self, pixels, strength, *, source_key, algorithm='normal'):
        values = np.asarray(pixels)
        if values.dtype != np.uint8 or values.ndim != 3 or values.shape[2] != 4:
            raise ValueError('Legacy graphics blur requires H × W × RGBA byte pixels')
        if np.ndim(strength) != 0 or algorithm not in ('normal', 'legacy'):
            return None
        # Detached byte input plus the final float result; textures have their
        # independent shared GPU admission and residency budget.
        size = values.nbytes * 5
        with self.condition:
            if (self.closed or self.worker_thread is None
                    or self.ready.is_set() and not self.available
                    or size > self.queue_budget - self.queued_bytes):
                return None
            values = np.array(values, copy=True, order='C')
            values.setflags(write=False)
            future = Future()
            self.queue.append(_Request(future, 'blur', values, float(strength), source_key, algorithm, size))
            self.queued_bytes += size
            self.condition.notify()
            return future

    def apply_blur(self, pixels, strength, *, source_key, algorithm='normal'):
        if QThread.currentThread() == QObject.thread(self):
            raise RuntimeError('A graphics result must not block the application thread')
        future = self.submit_blur(pixels, strength, source_key=source_key, algorithm=algorithm)
        return None if future is None else future.result()

    def apply_lut(self, pixels, palette, *, source_key, palette_key):
        if QThread.currentThread() == QObject.thread(self):
            raise RuntimeError('A graphics result must not block the application thread')
        future = self.submit_lut(pixels, palette, source_key=source_key, palette_key=palette_key)
        return None if future is None else future.result()

    @Slot()
    def _cleanup_surface(self):
        if self.surface is not None:
            self.surface.destroy()
            self.surface = None

    def close(self):
        if QThread.currentThread() != QObject.thread(self):
            raise RuntimeError('Graphics shutdown belongs to the application thread')
        with self.condition:
            self.closed = True
            while self.queue:
                request = self.queue.popleft()
                request.future.cancel()
                self.queued_bytes -= request.size
            self.condition.notify_all()
        if self.worker_thread is None or self.worker_thread.wait(5000):
            self._cleanup_surface()

