"""Vector edits should keep unrelated drawings ready for the next frame."""
from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, VectorDrawingObject, VectorStroke,
    VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def _scene():
    chapter = ChapterDocument()
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 1200))
    layer = chapter.add_layer(
        page.layer_id, "Ink", BoundGeometry.rectangle(0, 0, 1080, 1200)
    )
    drawings = [chapter.add_object(layer.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[
            VectorStrokePoint(x=20, y=20 + index * 15, width=4),
            VectorStrokePoint(x=60, y=20 + index * 15, width=4),
        ]),
    ])) for index in range(20)]
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.scale = 1.0
    canvas.set_selection("object", drawings[0].object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    return canvas, drawings


def _render(canvas, drawings):
    image = QImage(800, 600, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    for drawing in drawings:
        canvas._render_vector_drawing(painter, drawing, QRectF(0, 0, 800, 600))
    painter.end()
    return image


def test_vector_pencil_keeps_existing_stroke_images_and_other_spatial_indexes(qapp):
    canvas, drawings = _scene()
    _render(canvas, drawings)
    cached_images = {
        key: value[0].cacheKey()
        for key, value in canvas._vector_render_cache.items()
    }
    indexes = dict(canvas._vector_spatial_indexes)
    before = drawings[0].to_dict()

    canvas._begin_vector_pencil(drawings[0], QPointF(100, 100), 1.0)
    canvas._continue_vector_gesture(QPointF(160, 100), 1.0)
    canvas._finish_vector_pencil(drawings[0])
    after = drawings[0].to_dict()

    assert len(drawings[0].strokes) == 2
    assert cached_images == {
        key: canvas._vector_render_cache[key][0].cacheKey()
        for key in cached_images
    }
    for drawing in drawings[1:]:
        assert canvas._vector_spatial_indexes[drawing.object_id] is indexes[drawing.object_id]

    edited = _render(canvas, drawings)
    assert edited.pixelColor(130, 100).alpha() > 0
    # History can run after the user selects another object. It must invalidate
    # the drawing that actually changed and retain the new selection's cache.
    canvas.set_selection("object", drawings[-1].object_id)
    canvas.command_stack.undo()
    assert drawings[0].to_dict() == before
    assert drawings[0].object_id not in canvas._vector_spatial_indexes
    assert canvas._vector_spatial_indexes[drawings[-1].object_id] is indexes[drawings[-1].object_id]
    assert _render(canvas, drawings).pixelColor(130, 100).alpha() == 0
    canvas.command_stack.redo()
    assert drawings[0].to_dict() == after
    assert _render(canvas, drawings) == edited
    assert cached_images == {
        key: canvas._vector_render_cache[key][0].cacheKey()
        for key in cached_images
    }


def test_vector_history_invalidates_changed_geometry_without_revision_bump(qapp):
    canvas, drawings = _scene()
    _render(canvas, drawings)
    stroke = drawings[0].strokes[0]
    before = {drawings[0].object_id: drawings[0].to_dict()}
    unrelated = {
        key: value[0].cacheKey()
        for key, value in canvas._vector_render_cache.items()
        if key[0] != drawings[0].object_id
    }
    for point in stroke.points:
        point.x += 200
    assert canvas._push_vector_change(before, "Move vector points")

    def check_frame(moved):
        image = _render(canvas, drawings)
        assert (image.pixelColor(40, 20).alpha() == 0) == moved
        assert (image.pixelColor(240, 20).alpha() > 0) == moved
        assert unrelated == {
            key: canvas._vector_render_cache[key][0].cacheKey()
            for key in unrelated
        }

    check_frame(True)
    canvas.command_stack.undo()
    check_frame(False)
    canvas.command_stack.redo()
    check_frame(True)
