"""One graphics owner thread with bounded, detached requests.

The GUI owns the native surface's creation/destruction. Contexts, programs,
textures and readbacks live entirely on the worker. Presentation borrows shared
textures through immutable leases and completion fences.
"""
from collections import deque
from concurrent.futures import Future
from dataclasses import dataclass,field
from contextvars import copy_context
import threading
import copy
from itertools import count

import numpy as np
from PySide6.QtCore import QObject, QThread, Slot
from PySide6.QtGui import QGuiApplication, QOffscreenSurface, QOpenGLContext, QSurfaceFormat
from shiboken6 import isValid

from .point_chain import GpuPointChain
from .residency import GRAPHICS_RESIDENCY
from comic_editor.render.device import DeviceImage, PointTableStage, ScalarBlurStage, ByteQuantizeStage, NativeCopyStage


_TOKENS = count(1)


def _copy_array(lease,values,**options):
    try:
        return np.array(values,copy=True,order='C',**options)
    except BaseException:
        lease.release()
        raise


@dataclass(frozen=True)
class _Request:
    future: Future
    operation: str
    pixels: np.ndarray
    parameters: object
    source_key: object
    parameter_key: object
    size: int
    context: object = field(default_factory=copy_context)
    copy_lease: object = None


def _compute_request(service, renderer, helpers, helper_sizes, leases, request):
    result = None
    if request.operation == 'helper':
        kind, method, args, kwargs = request.parameters
        for previous in tuple(helpers):
            if previous != kind:
                helpers.pop(previous).close()
                helper_sizes.pop(previous,None)
        # Preserve the established shader for large original frames. The shared
        # CPU ticket makes this operation exclusive; over-budget GPU storage is
        # transient and retired on this owner immediately after the CPU edge.
        previous_size = sum(helper_sizes.values())
        exclusive = (request.size > service.legacy_gpu_budget or
            GRAPHICS_RESIDENCY.bytes-previous_size+request.size > GRAPHICS_RESIDENCY.budget)
        if not GRAPHICS_RESIDENCY.change(service.legacy_residency_token,request.size,
                                         allow_exclusive=True):
            raise RuntimeError('Another oversized graphics operation is active')
        helper = helpers.get(kind)
        if helper is None or not helper.available:
            if kind == 'pattern':
                from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
                helper = GpuPatternRenderer(surface=service.surface)
            elif kind == 'cage':
                from comic_editor.ui.gpu_textures import GpuTextureRenderer
                helper = GpuTextureRenderer(surface=service.surface)
            else:
                from comic_editor.ui.gpu_object_blending import GpuObjectBlendRenderer
                helper = GpuObjectBlendRenderer(surface=service.surface)
            helpers[kind] = helper
        helper_sizes[kind] = request.size
        try:
            if helper.available:
                result = getattr(helper, method)(*args, **kwargs)
        finally:
            if exclusive:
                helpers.pop(kind).close()
                helper_sizes.pop(kind,None)
                GRAPHICS_RESIDENCY.change(service.legacy_residency_token,0)
    elif renderer.available:
        if request.operation == 'segment':
            pixels = request.pixels
            canonical = request.parameter_key
            source_key = request.source_key
            if isinstance(pixels,DeviceImage):
                entry = leases.get(pixels.token)
                if entry is None or pixels.owner is not service:
                    raise RuntimeError('A device input belongs to another or closed owner')
                source_key = entry[0]
                canonical = pixels.canonical
                image = pixels
                pixels = renderer._get(source_key)
                if (image.source_origin != (0,0) or (image.width(),image.height()) !=
                        (pixels.width,pixels.height)):
                    copied = renderer.native_copy(pixels,source_key,
                        (image.width(),image.height()),tuple(-v for v in image.source_origin))
                    if copied is None:
                        raise MemoryError('The device view exceeds graphics admission')
                    pixels,source_key = copied
            value = renderer.segment(pixels,request.parameters,
                                     source_key=source_key,canonical_input=canonical)
            if value is not None:
                resource,key,canonical = value
                from .sync import GlSync
                with renderer._current():
                    fence = GlSync(renderer.context).insert()
                    renderer.functions.glFlush()
                token = next(_TOKENS)
                leases[token] = (key,fence)
                renderer.leases[token] = key
                result = DeviceImage(service,token,('device-image',key),
                    resource.width,resource.height,resource.texture,fence,canonical=canonical)
        elif request.operation == 'materialize':
            entry = leases.get(request.pixels.token)
            if entry is None:
                raise RuntimeError('The device image lease expired')
            source_key = entry[0]
            resource = renderer._get(source_key)
            image = request.pixels
            if image.source_origin != (0,0) or (image.width(),image.height()) != (resource.width,resource.height):
                copied = renderer.native_copy(resource,source_key,
                    (image.width(),image.height()),tuple(-v for v in image.source_origin))
                if copied is None:
                    raise MemoryError('CPU view materialization exceeds graphics admission')
                resource,_key = copied
            result = renderer.read_resource(resource)
        elif request.operation == 'lut':
            result = renderer.apply_lut(request.pixels, request.parameters,
                source_key=request.source_key, palette_key=request.parameter_key)
        else:
            result = renderer.apply_blur(request.pixels, request.parameters,
                source_key=request.source_key, algorithm=request.parameter_key)
    return result


def _admitted_request(service, renderer, helpers, helper_sizes, leases, request):
    from comic_editor.render.admission import RENDER_ADMISSION
    with RENDER_ADMISSION.reserve("graphics",request.size,priority=0,
            copies=request.copy_lease,cancelled=lambda:service.closed):
        return _compute_request(service,renderer,helpers,helper_sizes,leases,request)


class _GraphicsThread(QThread):
    def __init__(self, service):
        super().__init__()
        self.service = service
        self.setObjectName('effect-graphics')

    def run(self):
        service = self.service
        renderer = None
        helpers = {}
        helper_sizes = {}
        leases = {}
        try:
            renderer = GpuPointChain(budget=service.gpu_budget, surface=service.surface,
                                     share_context=service.share_context,context=service.prepared_context)
            service.render_context = renderer.context
            service.available, service.reason = renderer.available, renderer.reason
            service.ready.set()
            while True:
                with service.condition:
                    service.condition.wait_for(lambda: service.queue or service.releases or service.closed)
                    released, service.releases = service.releases, set()
                    if released and renderer.available:
                        from .sync import GlSync
                        with renderer._current():
                            sync = GlSync(renderer.context)
                            for token in released:
                                entry = leases.pop(token,None)
                                renderer.leases.pop(token,None)
                                if entry is not None:
                                    sync.delete(entry[1])
                    if not service.queue:
                        if service.closed:
                            break
                        continue
                    request = service.queue.popleft()
                    future = request.future
                try:
                    if future.set_running_or_notify_cancel():
                        result = request.context.run(_admitted_request, service, renderer, helpers, helper_sizes, leases, request)
                        if isinstance(result, np.ndarray):
                            result.setflags(write=False)
                        future.set_result(result)
                except Exception as error:
                    # A Future may outlive this owner. Its traceback must not
                    # retain contexts/resources for later GUI-thread disposal.
                    future.set_exception(error.with_traceback(None))
                    if not renderer.available:
                        service.available = False
                        service.reason = str(error)
                        break
                finally:
                    if isinstance(request.pixels,DeviceImage):
                        request.pixels.release()
                    if request.copy_lease is not None:
                        request.copy_lease.release()
                    with service.condition:
                        service.queued_bytes -= request.size
                        service.condition.notify_all()
                    service.stats = {name: getattr(renderer, name) for name in
                        ('uploads', 'readbacks', 'draws', 'hits', 'compiles', 'bytes')}
                    service.stats['legacy_bytes'] = sum(helper_sizes.values())
                    GRAPHICS_RESIDENCY.change(renderer.residency_token,renderer.bytes)
        except Exception as error:
            service.reason = str(error)
            service.ready.set()
        finally:
            try:
                for helper in helpers.values():
                    helper.close()
                helpers.clear()
                GRAPHICS_RESIDENCY.change(service.legacy_residency_token,0)
                helper = None
                if renderer is not None:
                    if leases and renderer.available:
                        from .sync import GlSync
                        with renderer._current():
                            sync = GlSync(renderer.context)
                            for key,fence in leases.values():
                                sync.delete(fence)
                        leases.clear()
                        renderer.leases.clear()
                    renderer.close()
                    service.render_context = service.prepared_context = None
                    renderer = None  # Context and GL owners are destroyed on this thread.
            except Exception as error:
                service.reason = str(error)
            finally:
                service.available = False
                service.closed = True
                GRAPHICS_RESIDENCY.change(service.legacy_residency_token,0)
                if renderer is not None:
                    GRAPHICS_RESIDENCY.change(renderer.residency_token,0)
                service.render_context = service.prepared_context = None
                renderer = None
                with service.condition:
                    while service.queue:
                        remaining = service.queue.popleft()
                        remaining.future.cancel()
                        if remaining.copy_lease is not None:
                            remaining.copy_lease.release()
                    service.queued_bytes = 0
                    service.condition.notify_all()


class GpuWorker(QObject):
    """Submit immutable pixel copies; a full queue asks callers to use the CPU."""
    def __init__(self, *, gpu_budget=128 * 1024 * 1024, queue_budget=128 * 1024 * 1024,
                 share_context=None, legacy_gpu_budget=512*1024*1024):
        super().__init__()
        app = QGuiApplication.instance()
        if app is None or QThread.currentThread() != app.thread():
            raise RuntimeError('Graphics surfaces must be created on the application thread')
        self.gpu_budget, self.queue_budget = max(0, int(gpu_budget)), max(0, int(queue_budget))
        # Whole-source legacy shader footprints have a separate explicit cap.
        # Only one helper family stays resident; central admission can account
        # this alongside the ordinary exact device graph budget.
        self.legacy_gpu_budget = max(0,int(legacy_gpu_budget))
        self.legacy_residency_token = GRAPHICS_RESIDENCY.token()
        self.queue, self.condition = deque(), threading.Condition()
        self.releases = set()
        self.share_context, self.render_context = share_context, None
        self.prepared_context = None
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
        if share_context is not None:
            # Establish sharing on the GUI before moving the context. Some
            # native Qt backends cannot establish it against a current context
            # owned by another thread even though their share-group metadata
            # is populated. All resource creation/use/destruction remains on
            # the graphics owner after this affinity transfer.
            context = QOpenGLContext()
            context.setFormat(fmt)
            context.setShareContext(share_context)
            if not context.create():
                self.reason = 'Could not create the shared graphics context'
                self.ready.set()
                self.surface.destroy()
                self.surface = self.worker_thread = None
                return
            context.moveToThread(self.worker_thread)
            self.prepared_context = context
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
            from comic_editor.render.admission import RENDER_ADMISSION
            copy_lease = RENDER_ADMISSION.reserve_copies(size)
            if copy_lease is None:
                return None
            # No mutable input crosses the thread boundary. The copy also keeps
            # a queued request's rows alive after the submitting owner returns.
            values = _copy_array(copy_lease,values,dtype=np.float32)
            values.setflags(write=False)
            table = _copy_array(copy_lease,palette,dtype=np.float32)
            table.setflags(write=False)
            future = Future()
            self.queue.append(_Request(future, 'lut', values, table, source_key, palette_key, size,copy_lease=copy_lease))
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
            from comic_editor.render.admission import RENDER_ADMISSION
            copy_lease = RENDER_ADMISSION.reserve_copies(size)
            if copy_lease is None:
                return None
            values = _copy_array(copy_lease,values)
            values.setflags(write=False)
            future = Future()
            self.queue.append(_Request(future, 'blur', values, float(strength), source_key, algorithm, size,copy_lease=copy_lease))
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

    def submit_segment(self, pixels, stages, *, source_key, canonical_input=False):
        """Submit a native segment and retain its completed device pixels."""
        stages = tuple(stages)
        if not stages or any(not isinstance(s,(PointTableStage,ScalarBlurStage,ByteQuantizeStage,NativeCopyStage)) for s in stages):
            raise ValueError('A device segment needs supported stages')
        for stage in stages:
            if isinstance(stage,PointTableStage) and (stage.table.shape != (256,256,4)
                    or stage.table.dtype != np.float32):
                raise ValueError('Point tables require 256 × 256 × RGBA float32 values')
            if isinstance(stage,NativeCopyStage) and (len(stage.size) != 2 or len(stage.origin) != 2
                    or any(type(v) is not int for v in (*stage.size,*stage.origin)) or min(stage.size) <= 0):
                raise ValueError('Native copies require positive integer dimensions and integer placement')
        if isinstance(pixels,DeviceImage):
            if pixels.owner is not self or pixels.isNull():
                return None
            size = 0
        else:
            pixels = np.asarray(pixels)
            if pixels.ndim != 3 or pixels.shape[2] != 4 or pixels.dtype not in (np.uint8,np.float32):
                raise ValueError('Device inputs require byte or float32 H × W × RGBA pixels')
            if pixels.dtype == np.float32 and not np.all(np.isfinite(pixels)):
                return None
            size = int(pixels.nbytes)
        size += sum(s.table.nbytes for s in stages if isinstance(s,PointTableStage))
        with self.condition:
            if (self.closed or self.worker_thread is None or self.ready.is_set() and not self.available
                    or size > self.queue_budget-self.queued_bytes):
                return None
            from comic_editor.render.admission import RENDER_ADMISSION
            copy_lease = RENDER_ADMISSION.reserve_copies(size)
            if copy_lease is None:
                return None
            if not isinstance(pixels,DeviceImage):
                pixels = _copy_array(copy_lease,pixels)
                pixels.setflags(write=False)
            else:
                pixels = pixels.copy(pixels.rect())
            owned = []
            for stage in stages:
                if isinstance(stage,PointTableStage):
                    table = _copy_array(copy_lease,stage.table)
                    table.setflags(write=False)
                    stage = PointTableStage(table,stage.identity,stage.quantized_output)
                owned.append(stage)
            future = Future()
            self.queue.append(_Request(future,'segment',pixels,tuple(owned),source_key,
                                       bool(canonical_input),size,copy_lease=copy_lease))
            self.queued_bytes += size
            self.condition.notify()
            return future

    def materialize(self, image):
        if QThread.currentThread() == QObject.thread(self):
            raise RuntimeError('Device materialization must not block the application thread')
        size = image.sizeInBytes()
        from comic_editor.render.admission import RENDER_ADMISSION,current_ticket
        if current_ticket() is None:
            with RENDER_ADMISSION.reserve('graphics-consumer',size,
                    cancelled=lambda:self.closed):
                return self.materialize(image)
        with self.condition:
            if self.closed or image.owner is not self or image.isNull():
                raise RuntimeError('Device image is unavailable')
            self.condition.wait_for(lambda: self.closed or
                (size <= self.queue_budget-self.queued_bytes if size <= self.queue_budget else self.queued_bytes == 0))
            if self.closed:
                raise RuntimeError('Device image owner closed')
            copy_lease = self._consumer_copies(size)
            future = Future()
            self.queue.append(_Request(future,'materialize',image.copy(image.rect()),None,None,None,size,copy_lease=copy_lease))
            self.queued_bytes += size
            self.condition.notify()
        return future.result()

    def _consumer_copies(self,size):
        from comic_editor.render.admission import RENDER_ADMISSION
        while not self.closed:
            lease = RENDER_ADMISSION.reserve_copies(size)
            if lease is not None:
                return lease
            self.condition.wait(.05)
        raise RuntimeError('Graphics owner closed before CPU consumer admission')

    def release(self, token):
        with self.condition:
            if not self.closed:
                self.releases.add(token)
                self.condition.notify()

    def helper(self, kind, method, args, kwargs):
        """Run an existing exact shader on its owner, not a CPU scene thread."""
        if QThread.currentThread() == QObject.thread(self):
            raise RuntimeError('A graphics result must not block the application thread')
        if self.closed or self.worker_thread is None or not self.worker_thread.isRunning():
            return None
        def detached(value):
            from PySide6.QtGui import QImage
            if isinstance(value, QImage):
                return QImage(value)
            if isinstance(value, np.ndarray):
                result = _copy_array(copy_lease,value)
                result.setflags(write=False)
                return result
            return copy.deepcopy(value)
        def size(value):
            from PySide6.QtGui import QImage
            if isinstance(value, QImage): return int(value.sizeInBytes())
            if isinstance(value, np.ndarray): return int(value.nbytes)
            return 0
        # Existing helpers retain source, output and auxiliary shader textures.
        # Account their per-request working set conservatively before copying.
        estimate = 8 * sum(size(value) for value in (*args, *kwargs.values()))
        if kind == 'cage':
            from PySide6.QtCore import QRectF
            estimate += max((8*max(0,round(v.width()))*max(0,round(v.height()))
                for v in (*args,*kwargs.values()) if isinstance(v,QRectF)),default=0)
        from comic_editor.render.admission import RENDER_ADMISSION,current_ticket
        if current_ticket() is None:
            with RENDER_ADMISSION.reserve('graphics-helper',estimate,cancelled=lambda:self.closed):
                return self.helper(kind,method,args,kwargs)
        with self.condition:
            if self.closed or self.worker_thread is None:
                return None
            self.condition.wait_for(lambda: self.closed or
                (estimate <= self.queue_budget-self.queued_bytes if estimate <= self.queue_budget
                 else self.queued_bytes == 0))
            if self.closed:
                return None
            copy_lease = self._consumer_copies(estimate)
            parameters = (kind, method, tuple(detached(value) for value in args),
                          {name: detached(value) for name, value in kwargs.items()})
            future = Future()
            self.queue.append(_Request(future, 'helper', None, parameters, None, None, estimate,copy_lease=copy_lease))
            self.queued_bytes += estimate
            self.condition.notify()
        return future.result()

    @Slot()
    def _cleanup_surface(self):
        if QThread.currentThread() != QObject.thread(self):
            raise RuntimeError('Graphics surfaces must be destroyed on the application thread')
        surface = self.surface
        if surface is not None:
            if isValid(surface):
                surface.destroy()
            self.surface = None
        thread = self.worker_thread
        if thread is not None:
            valid = isValid(thread)
            if not valid or not thread.isRunning():
                # Finished owners can be closed again after Qt deletes their
                # native wrappers. Keep valid finished threads inspectable.
                thread.service = None
                self.share_context = None
                if not valid:
                    self.worker_thread = None

    def close(self):
        if QThread.currentThread() != QObject.thread(self):
            raise RuntimeError('Graphics shutdown belongs to the application thread')
        with self.condition:
            self.closed = True
            while self.queue:
                request = self.queue.popleft()
                request.future.cancel()
                if request.copy_lease is not None:
                    request.copy_lease.release()
                self.queued_bytes -= request.size
            self.condition.notify_all()
        thread = self.worker_thread
        if thread is None or not isValid(thread) or thread.wait(5000):
            self._cleanup_surface()


class _HelperProxy:
    def __init__(self, worker, kind):
        self.worker, self.kind = worker, kind
        self.available = True

    def render(self, *args, **kwargs):
        return self.worker.helper(self.kind, 'render', args, kwargs)

    def cage(self, *args, **kwargs):
        return self.worker.helper(self.kind, 'cage', args, kwargs)

    def composite(self, *args, **kwargs):
        return self.worker.helper(self.kind, 'composite', args, kwargs)


def helper_for(canvas, kind):
    from comic_editor.ui import point_lut
    worker = getattr(canvas, '_graphics_worker', None) or point_lut._worker
    if worker is None or worker.closed or worker.worker_thread is None or not worker.worker_thread.isRunning():
        return None
    return _HelperProxy(worker, kind)

