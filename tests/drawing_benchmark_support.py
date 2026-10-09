"""Readiness accounting for the manual sustained-input benchmark."""
from dataclasses import dataclass, field


@dataclass
class ExactProgress:
    """A quick paint is not evidence that the requested exact scene is ready."""
    first_paint_at: float | None = None
    first_exact_at: float | None = None
    ready_since: float | None = None
    last_frame_at: float | None = None
    last_signature: tuple | None = None
    last_ready: bool = False
    transitions: list = field(default_factory=list)

    def observe(self, now, *, frame_pending, jobs_busy, failed, signature, activity):
        ready = not (frame_pending or jobs_busy or failed)
        changed = ready != self.last_ready or signature != self.last_signature
        if self.first_paint_at is None:
            self.first_paint_at = now
        if ready and self.first_exact_at is None:
            self.first_exact_at = now
        if not ready:
            self.ready_since = None
        elif changed or self.ready_since is None:
            self.ready_since = now
        if changed or not self.transitions:
            self.transitions.append({"at": now, "ready": ready,
                                     "activity": activity, "signature": signature,
                                     "frame_pending": frame_pending,
                                     "jobs_busy": jobs_busy, "failed": failed})
        self.last_frame_at, self.last_signature, self.last_ready = now, signature, ready
        return ready

    def terminal(self, now, *, last_activity_at, signature, jobs_busy, failed,
                 quiet_seconds=.1):
        return bool(self.last_ready and not jobs_busy and not failed
                    and self.last_signature == signature
                    and self.last_frame_at >= last_activity_at
                    and self.ready_since is not None
                    and now - self.ready_since >= quiet_seconds)


def canvas_signature(canvas):
    return (canvas.center_x, canvas.center_y, canvas.scale, canvas.rotation,
            canvas.command_stack.revision, canvas._document_projection.revision)


def canvas_pending(canvas):
    jobs = canvas._effect_jobs
    failures = getattr(jobs, "exact_failures", {})
    revision = canvas._document_projection.revision
    presented = getattr(canvas, "_projection_presented_revision", None)
    stale = bool(getattr(canvas, "_projection_async_enabled", False) and presented != revision)
    render_error = getattr(canvas, "_projection_render_error", None)
    controller = getattr(canvas, "_scene_controller", None)
    scheduler = getattr(controller, "scheduler", None)
    scene_busy = bool(controller is not None and (controller.capture is not None
        or (scheduler is not None and scheduler.busy)))
    return {
        "frame_pending": bool(getattr(canvas, "_projection_frame_pending", False) or stale),
        "presented_revision": presented, "requested_revision": revision,
        "jobs_busy": jobs.running is not None or bool(jobs.pending) or scene_busy,
        "scene_capture": bool(controller is not None and controller.capture is not None),
        "scene_busy": scene_busy,
        "scene_submitted": getattr(scheduler, "submitted", 0),
        "scene_completed": getattr(scheduler, "completed", 0),
        "scene_discarded": getattr(scheduler, "discarded", 0),
        "failed": bool(failures or render_error),
        "render_error": str(render_error) if render_error else None,
        "running": jobs.running is not None,
        "pending_count": len(jobs.pending),
        "failure_count": len(failures),
        "submitted": jobs.submitted, "completed": jobs.completed,
        "discarded": jobs.discarded,
    }


def _native_framebuffer_extent(context):
    """Query the existing read attachment, independently of Qt's viewport."""
    import ctypes
    convention = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)
    def procedure(name, *arguments):
        address = context.getProcAddress(name.encode())
        if not address:
            raise RuntimeError(f'Native framebuffer query unavailable: {name}')
        return convention(None, *arguments)(address)
    get_integer = procedure('glGetIntegerv', ctypes.c_uint, ctypes.c_void_p)
    get_attachment = procedure('glGetFramebufferAttachmentParameteriv',
        ctypes.c_uint, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)
    def integer(parameter):
        value = ctypes.c_int()
        get_integer(parameter, ctypes.byref(value))
        return value.value
    def attachment(parameter):
        value = ctypes.c_int()
        get_attachment(0x8CA8, 0x8CE0, parameter, ctypes.byref(value))
        return value.value
    kind, name = attachment(0x8CD0), attachment(0x8CD1)
    dimensions = []
    if kind == 0x1702:  # GL_TEXTURE: Qt's single-sample widget framebuffer.
        level = attachment(0x8CD2)
        previous = integer(0x8069)  # GL_TEXTURE_BINDING_2D
        bind = procedure('glBindTexture', ctypes.c_uint, ctypes.c_uint)
        query = procedure('glGetTexLevelParameteriv', ctypes.c_uint, ctypes.c_int,
                          ctypes.c_uint, ctypes.c_void_p)
        bind(0x0DE1, name)
        try:
            for parameter in (0x1000, 0x1001):  # GL_TEXTURE_WIDTH/HEIGHT
                value = ctypes.c_int()
                query(0x0DE1, level, parameter, ctypes.byref(value))
                dimensions.append(value.value)
        finally:
            bind(0x0DE1, previous)
    elif kind == 0x8D41:  # GL_RENDERBUFFER
        previous = integer(0x8CA7)
        bind = procedure('glBindRenderbuffer', ctypes.c_uint, ctypes.c_uint)
        query = procedure('glGetRenderbufferParameteriv', ctypes.c_uint,
                          ctypes.c_uint, ctypes.c_void_p)
        bind(0x8D41, name)
        try:
            for parameter in (0x8D42, 0x8D43):  # GL_RENDERBUFFER_WIDTH/HEIGHT
                value = ctypes.c_int()
                query(0x8D41, parameter, ctypes.byref(value))
                dimensions.append(value.value)
        finally:
            bind(0x8D41, previous)
    if len(dimensions) != 2 or min(dimensions) <= 0:
        raise RuntimeError(f'Native framebuffer has no readable color extent: {kind:#x}')
    return tuple(dimensions)


def read_native_frame(canvas):
    """Read every existing native framebuffer pixel without another paint.

    Odd widget extents at fractional DPR use Qt's attachment rounding. Both
    Python round and GL_VIEWPORT can omit the final row/column, so neither
    supplies the readback dimensions.
    """
    import ctypes
    import numpy as np
    from PySide6.QtGui import QImage
    canvas.makeCurrent()
    try:
        context = canvas.context()
        convention = getattr(ctypes, 'WINFUNCTYPE', ctypes.CFUNCTYPE)
        get_integer = convention(None, ctypes.c_uint, ctypes.c_void_p)(
            context.getProcAddress(b'glGetIntegerv'))
        previous = ctypes.c_int()
        get_integer(0x8CAA, ctypes.byref(previous))  # GL_READ_FRAMEBUFFER_BINDING
        functions = context.functions()
        functions.glBindFramebuffer(0x8CA8, canvas.defaultFramebufferObject())
        try:
            width, height = _native_framebuffer_extent(context)
            array = np.zeros((height, width, 4), np.uint8)
            read = convention(None, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                             ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p)(
                context.getProcAddress(b'glReadPixels'))
            read(0, 0, width, height, 0x1908, 0x1401, array.ctypes.data)
        finally:
            functions.glBindFramebuffer(0x8CA8, previous.value)
        array = np.ascontiguousarray(array[::-1])
        image = QImage(array.data, width, height, width * 4,
                       QImage.Format_RGBA8888_Premultiplied).copy()
        image.setDevicePixelRatio(canvas.devicePixelRatioF())
        return image
    finally:
        canvas.doneCurrent()


def pixel_difference(first, second):
    import numpy as np
    if first.size() != second.size() or first.format() != second.format():
        return {"identical": False, "dimensions_or_format_differ": True}
    a = np.frombuffer(first.constBits(), np.uint8).astype(np.int16)
    b = np.frombuffer(second.constBits(), np.uint8).astype(np.int16)
    delta = np.abs(a - b)
    return {"identical": not bool(np.any(delta)),
            "changed_bytes": int(np.count_nonzero(delta)),
            "maximum_byte_error": int(delta.max(initial=0)),
            "mean_byte_error": float(delta.mean())}
