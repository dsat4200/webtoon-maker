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
    return {
        "frame_pending": bool(getattr(canvas, "_projection_frame_pending", False) or stale),
        "presented_revision": presented, "requested_revision": revision,
        "jobs_busy": jobs.running is not None or bool(jobs.pending),
        "failed": bool(failures or render_error),
        "render_error": str(render_error) if render_error else None,
        "running": jobs.running is not None,
        "pending_count": len(jobs.pending),
        "failure_count": len(failures),
        "submitted": jobs.submitted, "completed": jobs.completed,
        "discarded": jobs.discarded,
    }


def read_native_frame(canvas):
    """Read the existing native framebuffer without invoking another paint."""
    import ctypes
    import numpy as np
    from PySide6.QtGui import QImage
    canvas.makeCurrent()
    try:
        width = round(canvas.width() * canvas.devicePixelRatioF())
        height = round(canvas.height() * canvas.devicePixelRatioF())
        array = np.zeros((height, width, 4), np.uint8)
        gl = ctypes.WinDLL("opengl32")
        gl.glReadPixels.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, ctypes.c_uint, ctypes.c_uint, ctypes.c_void_p]
        canvas.context().functions().glBindFramebuffer(0x8D40, canvas.defaultFramebufferObject())
        gl.glReadPixels(0, 0, width, height, 0x1908, 0x1401, array.ctypes.data)
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
