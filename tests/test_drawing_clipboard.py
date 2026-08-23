from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QPainterPath

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import (
    RasterSelectionClipboard, ToolKind, VectorSelectionClipboard,
)
from comic_editor.ui.main_window import MainWindow


def _window_document():
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(
        page.layer_id, "Layer", BoundGeometry.rectangle(0, 0, 500, 300)
    )
    tiles = TileStore()
    window._set_chapter(chapter, tiles)
    return window, chapter, layer, tiles


def _select_rect(window: MainWindow, object_id: str, rect: QRectF) -> None:
    window.canvas.set_selection("object", object_id)
    window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    path = QPainterPath()
    path.addRect(rect)
    window.canvas._drawing_selection_path = path
    window.canvas._refresh_drawing_selection_transform()


def _pixel(tiles: TileStore, object_id: str, x: int, y: int) -> QColor:
    key = x // tiles.tile_size, y // tiles.tile_size
    image = tiles.tile(object_id, key)
    if image is None:
        return QColor(Qt.transparent)
    return image.pixelColor(
        x - key[0] * tiles.tile_size,
        y - key[1] * tiles.tile_size,
    )


def test_raster_copy_cut_and_paste_are_undoable(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    target = chapter.add_object(layer.layer_id, RasterObject(name="Target"))
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 20,
        QColor("#ffff3300"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(10, 10, 80, 80))
        assert window._copy_drawing_selection()
        assert isinstance(window._drawing_clipboard, RasterSelectionClipboard)

        window.canvas.set_selection("object", target.object_id)
        assert window._paste()
        assert _pixel(tiles, target.object_id, 50, 50).alpha() > 0
        assert not window.canvas._drawing_selection_path.isEmpty()

        window.canvas.command_stack.undo()
        assert _pixel(tiles, target.object_id, 50, 50).alpha() == 0
        window.canvas.command_stack.redo()
        assert _pixel(tiles, target.object_id, 50, 50).alpha() > 0

        _select_rect(window, source.object_id, QRectF(35, 35, 30, 30))
        assert window._cut_drawing_selection()
        assert _pixel(tiles, source.object_id, 50, 50).alpha() == 0
        window.canvas.command_stack.undo()
        assert _pixel(tiles, source.object_id, 50, 50).alpha() > 0
    finally:
        window.deleteLater()


def test_raster_paste_overlay_moves_without_underlying_pixels(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    target = chapter.add_object(layer.layer_id, RasterObject(name="Target"))
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 20,
        QColor("#ffff3300"), square=True, antialias=False,
    )
    tiles.paint_dab(
        target.object_id, QPointF(50, 50), 20,
        QColor("#ff2255cc"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(10, 10, 80, 80))
        assert window._copy_drawing_selection()
        window.canvas.set_selection("object", target.object_id)
        assert window._paste()
        window.canvas.scale = 1.0

        quad = list(window.canvas._selection_transform_quad)
        center = QPointF(
            sum(x for x, _ in quad) / 4,
            sum(y for _, y in quad) / 4,
        )
        press = center + QPointF(20, 0)
        assert window.canvas._begin_drawing_selection_transform(target, press)
        assert window.canvas._selection_transform_mode == "translate"
        window.canvas._update_drawing_selection_transform(
            target, press + QPointF(100, 0)
        )
        assert window.canvas._finish_drawing_selection_transform(target)

        assert _pixel(tiles, target.object_id, 50, 50) == QColor("#ff2255cc")
        assert _pixel(tiles, target.object_id, 150, 50) == QColor("#ffff3300")

        window.canvas.command_stack.undo()
        assert _pixel(tiles, target.object_id, 50, 50) == QColor("#ffff3300")
        window.canvas.command_stack.redo()
        assert _pixel(tiles, target.object_id, 50, 50) == QColor("#ff2255cc")
    finally:
        window.deleteLater()


def test_later_raster_edit_finalizes_pasted_overlay_through_redo(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    target = chapter.add_object(layer.layer_id, RasterObject(name="Target"))
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 20,
        QColor("#ffff3300"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(10, 10, 80, 80))
        assert window._copy_drawing_selection()
        window.canvas.set_selection("object", target.object_id)
        assert window._paste()
        assert window.canvas._raster_paste_overlay is not None

        window.canvas.tool = ToolKind.RASTER_PENCIL
        window.canvas._begin_stroke(QPointF(250, 50), 1.0)
        window.canvas._end_stroke()
        assert window.canvas._raster_paste_overlay is None

        window.canvas.command_stack.undo()
        window.canvas.command_stack.undo()
        window.canvas.command_stack.redo()
        assert window.canvas._raster_paste_overlay is not None
        window.canvas.command_stack.redo()
        assert window.canvas._raster_paste_overlay is None
    finally:
        window.deleteLater()


def test_cut_of_pasted_raster_removes_only_the_overlay(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    target = chapter.add_object(layer.layer_id, RasterObject(name="Target"))
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 20,
        QColor("#ffff3300"), square=True, antialias=False,
    )
    tiles.paint_dab(
        target.object_id, QPointF(50, 50), 20,
        QColor("#ff2255cc"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(10, 10, 80, 80))
        assert window._copy_drawing_selection()
        window.canvas.set_selection("object", target.object_id)
        assert window._paste()
        assert window._cut_drawing_selection()
        assert _pixel(tiles, target.object_id, 50, 50) == QColor("#ff2255cc")

        window.canvas.command_stack.undo()
        assert _pixel(tiles, target.object_id, 50, 50) == QColor("#ffff3300")
    finally:
        window.deleteLater()


def test_vector_copy_splits_runs_and_paste_uses_fresh_ids(qapp):
    window, chapter, layer, _tiles = _window_document()
    source_stroke = VectorStroke(
        color="#ff123456", start_cap="square", end_cap="round",
        points=[
            VectorStrokePoint(x=20, y=40, width=5),
            VectorStrokePoint(x=60, y=40, width=6),
            VectorStrokePoint(x=100, y=40, width=7),
            VectorStrokePoint(x=140, y=40, width=8),
        ],
    )
    source = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=[source_stroke])
    )
    target = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Target")
    )
    selected = {
        source_stroke.points[0].point_id,
        source_stroke.points[1].point_id,
        source_stroke.points[3].point_id,
    }
    try:
        window.canvas.set_selection("object", source.object_id)
        window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        window.canvas._set_vector_selection(
            source, {source_stroke.stroke_id}, selected
        )
        assert window._copy_drawing_selection()
        payload = window._drawing_clipboard
        assert isinstance(payload, VectorSelectionClipboard)
        assert [len(stroke.points) for stroke in payload.strokes] == [2, 1]

        window.canvas.set_selection("object", target.object_id)
        assert window._paste()
        assert [len(stroke.points) for stroke in target.strokes] == [2, 1]
        assert all(stroke.color == source_stroke.color for stroke in target.strokes)
        assert {
            point.point_id for stroke in target.strokes for point in stroke.points
        }.isdisjoint(selected)
        assert window.canvas.selected_vector_point_ids == {
            point.point_id for stroke in target.strokes for point in stroke.points
        }

        window.canvas.command_stack.undo()
        assert window.canvas.chapter.objects[target.object_id].strokes == []
        window.canvas.command_stack.redo()
        assert len(window.canvas.chapter.objects[target.object_id].strokes) == 2
    finally:
        window.deleteLater()


def test_vector_cut_undo_restores_content_and_point_selection(qapp):
    window, chapter, layer, _tiles = _window_document()
    stroke = VectorStroke(points=[
        VectorStrokePoint(x=30, y=40),
        VectorStrokePoint(x=90, y=40),
        VectorStrokePoint(x=150, y=40),
    ])
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(strokes=[stroke])
    )
    selected = {stroke.points[1].point_id}
    try:
        window.canvas.set_selection("object", drawing.object_id)
        window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        window.canvas._set_vector_selection(
            drawing, {stroke.stroke_id}, selected
        )
        assert window._cut_drawing_selection()
        assert selected.isdisjoint({
            point.point_id
            for item in drawing.strokes for point in item.points
        })

        window.canvas.command_stack.undo()
        restored = window.canvas.chapter.objects[drawing.object_id]
        assert selected <= {
            point.point_id
            for item in restored.strokes for point in item.points
        }
        assert window.canvas.selected_vector_point_ids == selected
    finally:
        window.deleteLater()


def test_raster_paste_preserves_world_position_across_object_offsets(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(
        layer.layer_id, RasterObject(name="Source", x=100, y=0)
    )
    target = chapter.add_object(
        layer.layer_id, RasterObject(name="Target", x=20, y=0)
    )
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 18,
        QColor("#ffcc7722"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(35, 35, 30, 30))
        assert window._copy_drawing_selection()
        window.canvas.set_selection("object", target.object_id)
        assert window._paste()

        assert _pixel(tiles, target.object_id, 130, 50).alpha() > 0
        assert _pixel(tiles, target.object_id, 50, 50).alpha() == 0
    finally:
        window.deleteLater()


def test_paste_as_new_inserts_above_active_and_selects_content(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    anchor = chapter.add_object(layer.layer_id, RasterObject(name="Anchor"))
    tiles.paint_dab(
        source.object_id, QPointF(70, 60), 18,
        QColor("#ff44aa22"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(55, 45, 30, 30))
        assert window._copy_drawing_selection()
        window.canvas.set_selection("object", anchor.object_id)
        anchor_index = next(
            index for index, ref in enumerate(layer.children)
            if ref.entity_id == anchor.object_id
        )

        assert window._paste_drawing_as_new()
        created_id = window.canvas.selected_id
        assert isinstance(chapter.objects[created_id], RasterObject)
        assert layer.children[anchor_index].entity_id == created_id
        assert layer.children[anchor_index + 1].entity_id == anchor.object_id
        assert not window.canvas._drawing_selection_path.isEmpty()

        window.canvas.command_stack.undo()
        assert created_id not in window.canvas.chapter.objects
        window.canvas.command_stack.redo()
        assert created_id in window.canvas.chapter.objects
        assert window.canvas.selected_id == created_id
    finally:
        window.deleteLater()


def test_internal_buffer_pastes_as_new_after_document_switch(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    tiles.paint_dab(
        source.object_id, QPointF(70, 60), 18,
        QColor("#ff44aa22"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(55, 45, 30, 30))
        assert window._copy_drawing_selection()

        other = ChapterDocument()
        page = other.add_page()
        other_layer = other.add_layer(
            page.layer_id, "Other", BoundGeometry.rectangle(0, 0, 500, 300)
        )
        anchor = other.add_object(
            other_layer.layer_id, RasterObject(name="Anchor")
        )
        other_tiles = TileStore()
        window._set_chapter(other, other_tiles)
        window.canvas.set_selection("object", anchor.object_id)

        assert window._paste_drawing_as_new()
        created_id = window.canvas.selected_id
        assert created_id != anchor.object_id
        assert isinstance(other.objects[created_id], RasterObject)
        assert _pixel(other_tiles, created_id, 70, 60).alpha() > 0
    finally:
        window.deleteLater()


def test_vector_paste_as_new_preserves_closed_style_and_selects_fresh_ids(
    qapp,
):
    window, chapter, layer, _tiles = _window_document()
    stroke = VectorStroke(
        color="#cc123456", closed=True,
        start_cap="square", end_cap="point",
        points=[
            VectorStrokePoint(
                x=20, y=20, outgoing=(30, 10), width=5, opacity=0.4,
            ),
            VectorStrokePoint(
                x=80, y=20, incoming=(70, 10), width=7, opacity=0.6,
            ),
            VectorStrokePoint(x=50, y=80, width=9, opacity=0.8),
        ],
    )
    source = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Lines", strokes=[stroke])
    )
    anchor = chapter.add_object(layer.layer_id, RasterObject(name="Anchor"))
    source_ids = {
        stroke.stroke_id,
        *(point.point_id for point in stroke.points),
    }
    try:
        window.canvas.set_selection("object", source.object_id)
        window.canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        window.canvas._set_vector_selection(
            source,
            {stroke.stroke_id},
            {point.point_id for point in stroke.points},
        )
        assert window._copy_drawing_selection()
        payload = window._drawing_clipboard
        assert isinstance(payload, VectorSelectionClipboard)
        assert payload.strokes[0].closed

        window.canvas.set_selection("object", anchor.object_id)
        assert window._paste_drawing_as_new()
        created = chapter.objects[window.canvas.selected_id]
        assert isinstance(created, VectorDrawingObject)
        pasted = created.strokes[0]
        assert pasted.closed
        assert pasted.color == stroke.color
        assert pasted.start_cap == stroke.start_cap
        assert pasted.end_cap == stroke.end_cap
        assert [point.width for point in pasted.points] == [5, 7, 9]
        assert [point.opacity for point in pasted.points] == [0.4, 0.6, 0.8]
        assert pasted.points[0].outgoing == (30, 10)
        assert pasted.points[1].incoming == (70, 10)
        pasted_ids = {
            pasted.stroke_id,
            *(point.point_id for point in pasted.points),
        }
        assert pasted_ids.isdisjoint(source_ids)
        assert window.canvas.selected_vector_point_ids == {
            point.point_id for point in pasted.points
        }
    finally:
        window.deleteLater()


def test_unified_paste_uses_most_recent_available_source(qapp, monkeypatch):
    window, _chapter, _layer, _tiles = _window_document()
    drawing_calls = []
    image_calls = []
    try:
        window._drawing_clipboard = object()
        window._drawing_clipboard_serial = 5
        monkeypatch.setattr(
            window.canvas, "paste_drawing_clipboard",
            lambda payload: drawing_calls.append(payload) or True,
        )
        monkeypatch.setattr(
            window, "_clipboard_image_sources",
            lambda: [("image.png", "image/png", b"png")],
        )
        monkeypatch.setattr(
            window, "_paste_image",
            lambda sources=None: image_calls.append(sources) or True,
        )

        window._image_clipboard_serial = 4
        assert window._paste()
        assert len(drawing_calls) == 1
        assert image_calls == []

        window._image_clipboard_serial = 6
        assert window._paste()
        assert len(image_calls) == 1

        window._drawing_clipboard_serial = 7
        monkeypatch.setattr(
            window.canvas, "paste_drawing_clipboard", lambda payload: False,
        )
        assert not window._paste()
        assert len(image_calls) == 1
    finally:
        window.deleteLater()


def test_empty_selection_does_not_replace_internal_clipboard(qapp):
    window, chapter, layer, tiles = _window_document()
    source = chapter.add_object(layer.layer_id, RasterObject(name="Source"))
    empty = chapter.add_object(layer.layer_id, RasterObject(name="Empty"))
    tiles.paint_dab(
        source.object_id, QPointF(50, 50), 20,
        QColor("#ffff3300"), square=True, antialias=False,
    )
    try:
        _select_rect(window, source.object_id, QRectF(10, 10, 80, 80))
        assert window._copy_drawing_selection()
        payload = window._drawing_clipboard
        serial = window._drawing_clipboard_serial

        _select_rect(window, empty.object_id, QRectF(10, 10, 80, 80))
        assert not window._copy_drawing_selection()
        assert window._drawing_clipboard is payload
        assert window._drawing_clipboard_serial == serial
    finally:
        window.deleteLater()


def test_drawing_clipboard_hotkeys_yield_to_text_input(qapp):
    window, _chapter, _layer, _tiles = _window_document()
    try:
        window._set_text_shortcut_suppression(True)
        for action_id in ("cut", "copy", "paste", "paste_as_new"):
            assert window._hotkey_is_suppressed(action_id, frozenset())
    finally:
        window.deleteLater()
