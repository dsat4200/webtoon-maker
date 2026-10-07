"""Conservative exact-kernel admission; widget captures never wait on the GPU."""
from concurrent.futures import CancelledError

from PySide6.QtCore import QThread
from PySide6.QtGui import QGuiApplication

from comic_editor.render.pixels import current_contract


def scalar_blur(pixels, strength, algorithm):
    # Preserve the float policy's reference path until its spatial kernels have
    # their own contract. Legacy blur intentionally quantizes its pyramid input.
    if current_contract().floating or pixels.shape[0] * pixels.shape[1] < 1024 * 1024:
        return None
    from comic_editor.ui import point_lut
    worker = point_lut.graphics_worker()
    app = QGuiApplication.instance()
    if (app is None or QThread.currentThread() == app.thread() or worker is None
            or not worker.ready.is_set() or not worker.available or worker.closed):
        return None
    from comic_editor.ui.modifier_rendering import BlurPyramidCache
    values = BlurPyramidCache._pixels(pixels)
    try:
        return worker.apply_blur(values, strength, algorithm=algorithm,
                                 source_key=BlurPyramidCache._key(values))
    except (RuntimeError, ValueError, CancelledError):
        return None


def resident_stack(image, modifiers, *, worker=None, source_key=None,
                   quantize_stages=False):
    """Final-consumer entry point; intermediate QPainter edges stay explicit."""
    from comic_editor.ui.point_lut import device_modifier_stack
    return device_modifier_stack(image,modifiers,worker=worker,source_key=source_key,
                                 quantize_stages=quantize_stages,final_bytes=True)


def resident_spatial_stack(canvas, image, bounds, modifiers, mapping, *, required=None,
                           source_key=None):
    """One exact resident point/blur segment at the shared stage renderer edge.

    Full native frames preserve every blur pyramid's origin. Admission may
    decline large frames, leaving the regional CPU graph in charge. The final
    QImage returned here is consumed by QPainter, so it is materialized once.
    """
    from PySide6.QtCore import QRect,QRectF
    from comic_editor.core.effect_geometry import effect_bounds
    from comic_editor.ui.point_lut import device_modifier_stack,graphics_worker
    from comic_editor.render.device import DeviceImage
    app = QGuiApplication.instance()
    worker = getattr(canvas,'_graphics_worker',None) or graphics_worker()
    if (app is None or QThread.currentThread() == app.thread() or current_contract().floating
            or worker is None or image.width()*image.height() < 1024*1024
            or not mapping.isAffine() or image.isNull()):
        return None
    active = tuple(m for m in modifiers if not m.muted and m.intensity > 0)
    if len(active) < 2:
        return None
    result = device_modifier_stack(image,active,worker=worker,source_key=source_key,
        quantize_stages=True,stage_bounds=bounds)
    if not isinstance(result,DeviceImage):
        return None
    output = QRectF(bounds)
    for modifier in active:
        output = QRectF(effect_bounds(output,[modifier],mapping).toAlignedRect())
    region = output if required is None else QRectF(output.intersected(required).toAlignedRect())
    if region.isEmpty():
        result.release()
        return None
    cropped = result.copy(QRect(round(region.x()-output.x()),round(region.y()-output.y()),
                                round(region.width()),round(region.height())))
    try:
        return cropped.materialize(),region
    finally:
        cropped.release()
        result.release()
