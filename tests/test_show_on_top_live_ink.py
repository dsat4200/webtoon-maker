"""Unfinished vector ink respects promotion without entering the scene cache."""
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.mark.parametrize("finish", [False, True], ids=["cancel", "commit"])
@pytest.mark.parametrize("projection", [False, True], ids=["legacy", "projection"])
def test_live_vector_stroke_stays_below_promoted_artwork(qapp, finish, projection):
    chapter = ChapterDocument(height=480)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 480))
    page.fill_color, page.border_width = None, 0
    top = chapter.add_object(page.layer_id, RasterObject(show_on_top=True))
    drawing = chapter.add_object(page.layer_id, VectorDrawingObject(), index=0)
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(480, 480)
    canvas.set_document(chapter, TileStore())
    canvas._document_projection_enabled = projection
    canvas.tiles.paint_dab(top.object_id, QPointF(240, 240), 60, QColor("red"))
    canvas.center_x, canvas.center_y, canvas.scale = 240, 240, 1
    canvas.set_selection("object", drawing.object_id)
    canvas.primary_color = "#ff0000ff"

    def color(x):
        image = canvas.grab().toImage()
        point = canvas.camera_transform().map(QPointF(x, 240))
        return image.pixelColor(round(point.x()), round(point.y())).getRgb()[:3]

    try:
        assert color(240) == (255, 0, 0)
        canvas._begin_vector_pencil(drawing, QPointF(100, 240), 1)
        canvas._append_vector_sample(QPointF(280, 240), 1)
        assert color(150) == (0, 0, 255)
        assert color(240) == (255, 0, 0)
        if finish:
            canvas._finish_vector_pencil(drawing)
            assert color(150) == (0, 0, 255)
        else:
            canvas._cancel_vector_gesture()
            assert color(150) != (0, 0, 255)
        assert color(240) == (255, 0, 0)
    finally:
        canvas._cancel_vector_gesture()
        canvas._effect_jobs.cancel()
        canvas.deleteLater()
