"""Grid presentation stays identical during detached and live raster paints."""
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import GpuCanvasWidget, RasterCanvasWidget


@pytest.mark.parametrize("canvas_type", [RasterCanvasWidget, GpuCanvasWidget])
@pytest.mark.parametrize("rotation", [0., 17.])
def test_qimage_grid_uses_direct_raster_coverage(qapp, canvas_type, rotation):
    # A GPU canvas can also supply a detached QImage capture. Only its actual
    # GL paint device needs the cached transparent overlay workaround.
    canvas = canvas_type(EditorSettings(grid_overlay_visible=True,
        grid_size_px=64, grid_divisions=4, grid_opacity=.4))
    chapter = ChapterDocument(width=640, height=480)
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 640, 480))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 320, 240, 1
    canvas.rotation = rotation
    image = QImage(640, 480, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#2d313b"))
    expected = image.copy()
    actual = QPainter(image)
    reference = QPainter(expected)
    try:
        canvas._paint_projection_grid(actual, canvas)
        reference.setRenderHint(QPainter.Antialiasing, True)
        reference.setTransform(canvas.camera_transform())
        reference.setClipRect(QRectF(0, 0, 640, 480), Qt.IntersectClip)
        canvas._draw_grid(reference, canvas.visible_document_rect())
    finally:
        actual.end()
        reference.end()
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
    assert image == expected
    assert getattr(canvas, "_projection_grid_cache", None) is None
