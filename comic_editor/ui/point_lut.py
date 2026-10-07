"""Compile compatible legacy point chains without changing stage precision."""
from functools import lru_cache
from contextlib import contextmanager
from contextvars import ContextVar
from concurrent.futures import CancelledError
import atexit
import json

import numpy as np
from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QGuiApplication, QImage

from comic_editor.core.models import BrightnessContrastModifier, CurvesModifier, BlurModifier, modifier_from_dict
from comic_editor.render.pixels import current_contract, LEGACY_PIXELS, pixel_scope


_gpu = None
_worker = None
_graphics_owner = ContextVar('effect_graphics_owner',default=None)


@contextmanager
def graphics_scope(worker):
    """Bind detached effects to their scene's graphics owner for this call."""
    token = _graphics_owner.set(worker)
    try:
        yield
    finally:
        _graphics_owner.reset(token)


def graphics_worker():
    return _graphics_owner.get() or _worker


def prepare_point_worker(owner=None):
    """Start graphics initialization between GUI events, before large jobs."""
    global _worker
    app = QGuiApplication.instance()
    if app is None or QThread.currentThread() != app.thread() or app.platformName() == 'offscreen':
        return _worker
    from comic_editor.render.gpu.worker import GpuWorker
    if owner is not None:
        existing = getattr(owner,'_graphics_worker',None)
        context = owner.context() if hasattr(owner,'context') else None
        if existing is not None and not existing.closed:
            if existing.share_context is context and (not existing.ready.is_set() or existing.available):
                return existing
            existing.close()
        if context is not None:
            existing = GpuWorker(share_context=context)
            owner._graphics_worker = existing
            context.aboutToBeDestroyed.connect(existing.close)
            owner.destroyed.connect(existing.close)
            app.aboutToQuit.connect(existing.close)
            atexit.register(existing.close)
            return existing
    if _worker is not None:
        return _worker
    _worker = GpuWorker()
    app.aboutToQuit.connect(_worker.close)
    # Test/embedded hosts may own QApplication without ever running exec().
    # Finish our GL owner before PySide's process-exit application teardown.
    atexit.register(_worker.close)
    return _worker


def _supported(modifier):
    return (not modifier.parameter_masks and (
        isinstance(modifier, BrightnessContrastModifier)
        or isinstance(modifier, CurvesModifier) and modifier.color_mode == 'rgb'))


@lru_cache(maxsize=32)  # Fixed-size tables: 32 MiB of immutable float data.
def compile_point_table(signature, quantize_stages=False):
    from comic_editor.ui.modifier_rendering import apply_modifier_stack
    effects = [modifier_from_dict(json.loads(signature[-1]))]
    if len(signature) > 1:
        source = (compile_point_table(signature[:-1], True) if quantize_stages
                  else compile_point_table(signature[:-1]))
    else:
        source = np.empty((256, 256, 4), np.float32)
        values = np.arange(256, dtype=np.float32) / 255.
        source[..., :3] = values[None, :, None]
        source[..., 3] = values[:, None]
    placeholder = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
    placeholder.fill(Qt.transparent)
    with pixel_scope(LEGACY_PIXELS):
        result = apply_modifier_stack(placeholder, effects, (0, 0),
            original_pixels=source, return_pixels=True, _point_lut=False)
    result = np.ascontiguousarray(result, np.float32)
    if quantize_stages:
        # Spatial stacks publish RGBA8 between stages. Preserve that distinct
        # rounding contract while still fusing the table lookups themselves.
        result = np.clip(result * 255., 0., 255.).astype(np.uint8).astype(np.float32) / 255.
    result.setflags(write=False)
    return result


def _gpu_renderer():
    global _gpu
    app = QGuiApplication.instance()
    if app is None or app.platformName() == 'offscreen':
        return None
    worker = graphics_worker()
    if worker is not None:
        # GUI captures use the exact CPU table. Detached jobs may wait on the
        # graphics owner; the GUI never waits for an upload or shader compile.
        return (worker if QThread.currentThread() != app.thread()
                and worker.ready.is_set() and worker.available and not worker.closed else None)
    if QThread.currentThread() != app.thread():
        return None
    if _gpu is None:
        from comic_editor.render.gpu.point_chain import GpuPointChain
        _gpu = GpuPointChain()
        app.aboutToQuit.connect(_gpu.close)
    return _gpu if _gpu.available else None


def point_chain(image, pixels, modifiers, *, quantize_stages=False, source_key=None, canonical_input=False):
    """Return exact float intermediates for eligible canonical RGBA8 input."""
    if (current_contract().floating or image.depth() > 32 and not canonical_input
            or len(modifiers) < 2 or pixels.shape[0] * pixels.shape[1] < 65536
            or not all(_supported(modifier) for modifier in modifiers)):
        return None
    from comic_editor.ui.modifier_rendering import modifier_render_settings
    signature = []
    for modifier in modifiers:
        settings = modifier_render_settings(modifier)
        settings['id'] = 'compiled-point-operation'
        signature.append(json.dumps(settings, sort_keys=True, separators=(',', ':')))
    signature = tuple(signature)
    table = compile_point_table(signature, True) if quantize_stages else compile_point_table(signature)
    if pixels.shape[0] * pixels.shape[1] >= 1024 * 1024:
        gpu = _gpu_renderer()
        if gpu is not None:
            try:
                result = gpu.apply_lut(pixels, table,
                    source_key=(int(image.cacheKey()) if source_key is None else source_key, LEGACY_PIXELS.signature),
                    palette_key=(signature, quantize_stages))
            except (RuntimeError, ValueError, CancelledError):
                result = None
            if result is not None:
                return result
    # Small tiles and detached workers avoid a graphics transfer. All channel
    # lookups preserve the same final float bits as the original CPU kernels.
    result = np.empty_like(pixels)
    for top in range(0, pixels.shape[0], 128):
        values = np.rint(pixels[top:top + 128] * 255.).astype(np.uint8)
        alpha = values[..., 3]
        for channel in range(3):
            result[top:top + 128, ..., channel] = table[alpha, values[..., channel], channel]
        result[top:top + 128, ..., 3] = table[alpha, 0, 3]
    return result


def device_modifier_stack(image, modifiers, *, worker=None, source_key=None,
                          quantize_stages=False, final_bytes=True, stage_bounds=None):
    """A compatible exact stack for a device-capable final consumer.

    CPU QPainter composition, export and disk recording instead materialize the
    returned lease explicitly. Masks, focal blur and float policies remain on
    their reference paths. Generic stacks keep float point intermediates;
    spatial stacks request their distinct byte boundaries.
    """
    if current_contract().floating or image.isNull() or image.depth() > 32:
        return None
    from comic_editor.render.device import PointTableStage, ScalarBlurStage, ByteQuantizeStage, NativeCopyStage
    from comic_editor.render.pixels import premultiplied_pixels
    from comic_editor.ui.modifier_rendering import modifier_render_settings
    active = tuple(m for m in modifiers if not m.muted and m.intensity > 0)
    if not active or any(not (_supported(m) or isinstance(m,BlurModifier)
        and not m.parameter_masks and m.mode == 'full' and m.intensity == 100.) for m in active):
        return None
    worker = worker or graphics_worker()
    if worker is None or not worker.ready.is_set() or not worker.available or worker.closed:
        return None
    stages, points = [], []
    frame = None
    if stage_bounds is not None:
        from PySide6.QtCore import QRectF
        frame = QRectF(stage_bounds)
        if (not quantize_stages or frame != QRectF(frame.toAlignedRect())
                or round(frame.width()) != image.width() or round(frame.height()) != image.height()):
            return None
    def flush():
        if points:
            signature = []
            for modifier in points:
                settings = modifier_render_settings(modifier)
                settings['id'] = 'compiled-point-operation'
                signature.append(json.dumps(settings,sort_keys=True,separators=(',',':')))
            signature = tuple(signature)
            stages.append(PointTableStage(compile_point_table(signature,quantize_stages),
                                          (signature,quantize_stages),quantize_stages))
            points.clear()
    for modifier in active:
        if isinstance(modifier,BlurModifier):
            flush()
            if frame is not None:
                from comic_editor.core.effect_geometry import effect_bounds
                target = QRectF(effect_bounds(frame,[modifier]).toAlignedRect())
                stages.append(NativeCopyStage((round(target.width()),round(target.height())),
                    (round(frame.x()-target.x()),round(frame.y()-target.y()))))
                frame = target
            stages.append(ScalarBlurStage(modifier.strength,modifier.algorithm))
            if quantize_stages:
                stages.append(ByteQuantizeStage())
        else:
            points.append(modifier)
    flush()
    if final_bytes:
        stages.append(ByteQuantizeStage())
    pixels = premultiplied_pixels(image)
    identity = (int(image.cacheKey()) if source_key is None else source_key, LEGACY_PIXELS.signature,
                None if stage_bounds is None else tuple(stage_bounds.getRect()))
    future = worker.submit_segment(pixels,stages,source_key=identity,canonical_input=True)
    if future is None:
        return None
    if QThread.currentThread() == QGuiApplication.instance().thread():
        # Submission is safe on GUI; callers must publish its Future later.
        return future
    try:
        return future.result()
    except (RuntimeError,ValueError,CancelledError):
        return None
