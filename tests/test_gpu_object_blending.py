"""Compare real OpenGL blend output with exact CPU premultiplied arithmetic."""
import numpy as np
import pytest
from PySide6.QtCore import QRect
from PySide6.QtGui import QImage, QOpenGLContext

from comic_editor.ui.gpu_object_blending import GPU_MODES, GpuObjectBlendRenderer
from comic_editor.ui.object_blending import _custom_composite


@pytest.fixture(scope="module")
def gpu(qapp):
    renderer = GpuObjectBlendRenderer(allow_offscreen=True)
    if not renderer.available:
        pytest.skip(renderer.reason)
    yield renderer
    renderer.close()


def image(values):
    return QImage(values.data, values.shape[1], values.shape[0], values.strides[0],
                  QImage.Format_RGBA8888_Premultiplied).copy()


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.width(), 4).copy()


@pytest.mark.parametrize("mode", GPU_MODES)
def test_gpu_matches_cpu_with_transparency_height_halos_and_context_restore(gpu, mode):
    rng = np.random.default_rng(18)
    back = rng.integers(0, 256, (37, 53, 4), dtype=np.uint8)
    front = rng.integers(0, 256, (39, 55, 4), dtype=np.uint8)
    back[..., :3] = np.minimum(back[..., :3], back[..., 3:4])
    front[..., :3] = np.minimum(front[..., :3], front[..., 3:4])
    destination, source = image(back), image(front)
    core = QRect(1, 1, 53, 37)
    previous = QOpenGLContext.currentContext()
    actual = gpu.composite(destination, source, mode, core, 1.25)
    assert actual is not None, gpu.reason
    assert QOpenGLContext.currentContext() is previous
    expected = _custom_composite(destination, source, mode, core, 1.25)
    np.testing.assert_allclose(pixels(actual), pixels(expected), atol=1)


def test_gpu_rejects_oversized_input_before_allocation(gpu):
    oversized = QImage(2048, 1, QImage.Format_ARGB32_Premultiplied)
    assert gpu.composite(oversized, oversized, "color", QRect(0, 0, 2048, 1), 1) is None
