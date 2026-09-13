from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainterPath

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def _scene():
    chapter = ChapterDocument(height=1600)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 1600))
    a = chapter.add_layer(page.layer_id, "Ink")
    b = chapter.add_layer(page.layer_id, "Color")
    b.translate_x, b.translate_y = 50, 25
    drawings = [chapter.add_object(layer.layer_id, RasterObject()) for layer in (a, b)]
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 400, 300, 1
    for drawing, color in zip(drawings, ("red", "blue")):
        canvas.tiles.paint_dab(drawing.object_id, QPointF(80, 80), 30,
                               QColor(color), antialias=False, square=True)
    refs = [("object", drawing.object_id) for drawing in drawings]
    canvas.set_selection_set(refs, primary=refs[0])
    canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    canvas.command_stack.clear()
    return canvas, chapter, drawings


def _lasso(canvas, points=None):
    points = points or [(20, 20), (240, 20), (240, 200), (20, 200)]
    canvas._tool_press(canvas.document_to_widget(QPointF(*points[0])), 1)
    for point in points[1:]:
        canvas._tool_move(canvas.document_to_widget(QPointF(*point)), 1)
    canvas._tool_release()
    assert canvas._selection_transform_quad


def _start_move(canvas, delta=QPointF(280, 60)):
    press = QPointF(90, 115)  # Away from the pivot and resize handles.
    canvas._tool_press(canvas.document_to_widget(press), 1)
    assert canvas._selection_transform_mode == "translate"
    canvas._tool_move(canvas.document_to_widget(press + delta), 1)


def _pixel(canvas, drawing, local):
    x, y = round(local.x()), round(local.y())
    key = x // canvas.tiles.tile_size, y // canvas.tiles.tile_size
    image = canvas.tiles.tile(drawing.object_id, key)
    return (QColor(Qt.transparent) if image is None else
            image.pixelColor(x % canvas.tiles.tile_size, y % canvas.tiles.tile_size))


@pytest.mark.parametrize("projective", [False, True])
def test_multi_lasso_maps_world_movement_into_each_raster(qapp, projective):
    canvas, chapter, drawings = _scene()
    b = drawings[1]
    if projective:
        b.transform_frame = (0, 0, 300, 300)
        b.transform_quad = [(20, 30), (330, 0), (300, 330), (0, 300)]
    before = {d.object_id: canvas.tiles.object_tiles(d.object_id) for d in drawings}
    mappings = [canvas._drawing_local_to_world_transform(d) for d in drawings]
    _lasso(canvas)
    _start_move(canvas)
    assert {d.object_id for d in drawings} == set(canvas._selection_raster_states)
    assert all(canvas.tiles.object_tiles(d.object_id) == before[d.object_id] for d in drawings)
    canvas._tool_release()
    assert len(canvas.command_stack._undo) == 1
    for drawing, mapping, color in zip(drawings, mappings, ("red", "blue")):
        destination = mapping.inverted()[0].map(mapping.map(QPointF(80, 80)) + QPointF(280, 60))
        assert _pixel(canvas, drawing, QPointF(80, 80)).alpha() == 0
        assert _pixel(canvas, drawing, destination) == QColor(color)
    after = {d.object_id: canvas.tiles.object_tiles(d.object_id) for d in drawings}
    canvas.command_stack.undo()
    assert all(canvas.tiles.object_tiles(d.object_id) == before[d.object_id] for d in drawings)
    canvas.command_stack.redo()
    assert all(canvas.tiles.object_tiles(d.object_id) == after[d.object_id] for d in drawings)
    assert len(chapter.objects) == 2
    canvas.deleteLater()


def test_cancel_multi_lasso_move_preserves_pixels_and_history(qapp):
    canvas, _chapter, drawings = _scene()
    before = {d.object_id: canvas.tiles.object_tiles(d.object_id) for d in drawings}
    _lasso(canvas)
    original_quad = list(canvas._selection_transform_quad)
    _start_move(canvas)
    assert canvas.set_tool(ToolKind.OBJECT_SELECT)
    canvas._tool_release()
    assert not canvas._selection_raster_states
    assert not canvas.command_stack.can_undo
    assert canvas._selection_transform_quad == original_quad
    assert all(canvas.tiles.object_tiles(d.object_id) == before[d.object_id] for d in drawings)
    canvas.deleteLater()


def test_session_capture_cancels_multi_raster_preview(qapp):
    canvas, _chapter, drawings = _scene()
    before = {d.object_id: canvas.tiles.object_tiles(d.object_id) for d in drawings}
    _lasso(canvas)
    original_quad = list(canvas._selection_transform_quad)
    _start_move(canvas)
    state = canvas._capture_drawing_selection_state()
    assert state.quad == original_quad
    assert not canvas._selection_raster_states
    assert not canvas.command_stack.can_undo
    assert all(canvas.tiles.object_tiles(d.object_id) == before[d.object_id] for d in drawings)
    canvas.deleteLater()


def test_multi_lasso_undo_only_rewrites_changed_tiles(qapp, monkeypatch):
    canvas, _chapter, drawings = _scene()
    untouched = {}
    for drawing in drawings:
        for index in range(40):
            canvas.tiles.paint_dab(drawing.object_id, QPointF(800, 600 + 256 * index),
                                   5, QColor("green"), antialias=False)
        untouched[drawing.object_id] = {
            key: image.cacheKey() for key, image in canvas.tiles.object_tiles(drawing.object_id).items()
            if key[1] >= 2
        }
    _lasso(canvas)
    _start_move(canvas)
    canvas._tool_release()
    writes = []
    original = canvas.tiles.set_tile
    def write(identifier, key, image):
        writes.append((identifier, key))
        original(identifier, key, image)
    monkeypatch.setattr(canvas.tiles, "set_tile", write)
    canvas.command_stack.undo()
    assert 2 <= len(writes) <= 8
    for drawing in drawings:
        assert untouched[drawing.object_id] == {
            key: image.cacheKey() for key, image in canvas.tiles.object_tiles(drawing.object_id).items()
            if key[1] >= 2
        }
    canvas.deleteLater()


def test_ctrl_subtraction_applies_to_all_selected_rasters(qapp, monkeypatch):
    canvas, _chapter, drawings = _scene()
    _lasso(canvas)
    monkeypatch.setattr(QGuiApplication, "keyboardModifiers", lambda: Qt.ControlModifier)
    _lasso(canvas, [(55, 55), (105, 55), (105, 110), (55, 110)])
    monkeypatch.setattr(QGuiApplication, "keyboardModifiers", lambda: Qt.NoModifier)
    _start_move(canvas)
    canvas._tool_release()
    assert _pixel(canvas, drawings[0], QPointF(80, 80)) == QColor("red")
    assert _pixel(canvas, drawings[1], QPointF(80, 80)).alpha() == 0
    assert _pixel(canvas, drawings[1], QPointF(360, 140)) == QColor("blue")
    canvas.deleteLater()
