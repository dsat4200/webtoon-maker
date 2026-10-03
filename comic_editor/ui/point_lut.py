"""Compile compatible legacy point chains without changing stage precision."""
from functools import lru_cache
from concurrent.futures import CancelledError
import atexit
import json

import numpy as np
from PySide6.QtCore import QThread, Qt
from PySide6.QtGui import QGuiApplication, QImage

from comic_editor.core.models import BrightnessContrastModifier, CurvesModifier, modifier_from_dict
from comic_editor.render.pixels import current_contract, LEGACY_PIXELS, pixel_scope


_gpu = None
_worker = None


def prepare_point_worker():
    """Start graphics initialization between GUI events, before large jobs."""
    global _worker
    app = QGuiApplication.instance()
    if (app is None or QThread.currentThread() != app.thread()
            or app.platformName() == 'offscreen' or _worker is not None):
        return
    from comic_editor.render.gpu.worker import GpuWorker
    _worker = GpuWorker()
    app.aboutToQuit.connect(_worker.close)
    # Test/embedded hosts may own QApplication without ever running exec().
    # Finish our GL owner before PySide's process-exit application teardown.
    atexit.register(_worker.close)


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
    if _worker is not None:
        # GUI captures use the exact CPU table. Detached jobs may wait on the
        # graphics owner; the GUI never waits for an upload or shader compile.
        return (_worker if QThread.currentThread() != app.thread()
                and _worker.ready.is_set() and _worker.available and not _worker.closed else None)
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
