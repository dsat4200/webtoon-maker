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
    worker = point_lut._worker
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
