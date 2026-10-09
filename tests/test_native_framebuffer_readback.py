"""Manual benchmark evidence includes the native attachment's true corners."""
import numpy as np
import pytest
from PySide6.QtCore import QSizeF, Qt
from PySide6.QtGui import QImage, QSurfaceFormat
from PySide6.QtOpenGLWidgets import QOpenGLWidget

from drawing_benchmark_support import pixel_difference, read_native_frame


@pytest.mark.parametrize('logical_size', [(95, 97), (955, 927)])
def test_existing_frame_readback_keeps_full_attachment_and_corner_pixels(qapp, logical_size):
    if qapp.platformName() == 'offscreen':
        pytest.skip('Native QOpenGLWidget framebuffer unavailable offscreen')

    class CornerCanvas(QOpenGLWidget):
        def __init__(self):
            super().__init__()
            self.setAttribute(Qt.WA_DontShowOnScreen)
            self.paints = 0
            self.resize(*logical_size)
            fmt = QSurfaceFormat()
            fmt.setVersion(3, 3)
            fmt.setProfile(QSurfaceFormat.CoreProfile)
            fmt.setSamples(0)
            self.setFormat(fmt)

        def paintGL(self):
            self.paints += 1
            physical = (QSizeF(self.size()) * self.devicePixelRatioF()).toSize()
            width, height = physical.width(), physical.height()
            functions = self.context().functions()
            functions.glDisable(0x0C11)  # GL_SCISSOR_TEST
            functions.glClearColor(.2, .3, .4, 1.)
            functions.glClear(0x4000)
            functions.glEnable(0x0C11)
            for x, y, color in ((0, 0, (255, 0, 0)), (width-1, 0, (0, 255, 0)),
                                (0, height-1, (0, 0, 255)), (width-1, height-1, (255, 255, 0))):
                functions.glScissor(x, y, 1, 1)
                functions.glClearColor(*(value / 255 for value in color), 1.)
                functions.glClear(0x4000)
            functions.glDisable(0x0C11)
            # A viewport may intentionally differ from the actual attachment;
            # at DPR 1.5 Qt itself leaves the odd widget viewport one pixel short.
            functions.glViewport(0, 0, width-1, height-1)

    widget = CornerCanvas()
    try:
        widget.show()
        qapp.processEvents()
        if not widget.isValid():
            pytest.skip('A native OpenGL widget could not be initialized')
        paints = widget.paints
        image = read_native_frame(widget)
        assert widget.paints == paints, 'Evidence readback forced another paint'
        assert image.devicePixelRatioF() == widget.devicePixelRatioF()
        data = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4)
        np.testing.assert_array_equal(data[0, 0], [0, 0, 255, 255])
        np.testing.assert_array_equal(data[0, -1], [255, 255, 0, 255])
        np.testing.assert_array_equal(data[-1, 0], [255, 0, 0, 255])
        np.testing.assert_array_equal(data[-1, -1], [0, 255, 0, 255])
        # Qt's independent capture supplies its actual attachment extent. It is
        # allowed to repaint only after the observer's paint-count assertion.
        oracle = widget.grabFramebuffer().convertToFormat(QImage.Format_RGBA8888_Premultiplied)
        assert image.size() == oracle.size()
        assert pixel_difference(image, oracle)['identical']
        if widget.devicePixelRatioF() == 1.5 and logical_size == (955, 927):
            assert (image.width(), image.height()) == (1433, 1391)
    finally:
        widget.close()
        widget.deleteLater()
        qapp.processEvents()
