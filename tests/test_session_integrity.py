from __future__ import annotations

from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QPainterPath
from PySide6.QtWidgets import QMessageBox

from comic_editor.core import settings
from comic_editor.core.models import (
    BoundGeometry, PathNode, RasterObject, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "settings_path", lambda: tmp_path / "settings.json")
    projects = []
    for name in ("First", "Second"):
        repository = SeriesRepository(tmp_path / name)
        series = repository.create(name)
        chapter, tiles = repository.create_chapter(series, name)
        raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
        for point in (QPointF(20, 20), QPointF(320, 20)):
            tiles.paint_dab(raster.object_id, point, 8, QColor("blue"))
        vector = chapter.add_object(raster.parent_layer_id, VectorDrawingObject(
            strokes=[VectorStroke(points=[
                VectorStrokePoint(x=100, y=100), VectorStrokePoint(x=300, y=180),
                VectorStrokePoint(x=450, y=300), VectorStrokePoint(x=300, y=440),
            ])],
        ))
        shape = chapter.add_layer(raster.parent_layer_id, "Shape", BoundGeometry.path([
            PathNode(x=100, y=100),
            PathNode(x=300, y=180, point_type="bezier", handles_locked=False,
                     incoming=(260, 140), outgoing=(340, 220)),
            PathNode(x=450, y=300), PathNode(x=300, y=440), PathNode(x=100, y=360),
        ], closed=True))
        repository.save_chapter(chapter, tiles)
        alternate, _ = repository.create_chapter(series, name + " alternate")
        projects.append(SimpleNamespace(
            repository=repository, chapter_id=chapter.chapter_id,
            alternate_id=alternate.chapter_id, raster_id=raster.object_id,
            vector_id=vector.object_id, shape_id=shape.layer_id,
        ))
    window = MainWindow()
    window.settings.snap_to_grid = False
    try:
        for project in projects:
            assert window.open_series(project.repository.root)
        window.project_tabs.setCurrentIndex(0)
        yield window, projects
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()
        window.deleteLater()


def _select(window, project, kind, tab=0):
    canvas = window.canvas
    canvas.scale = 1.0
    if kind == "shape":
        obj = window.chapter.layers[project.shape_id]
        canvas.set_selection("layer", obj.layer_id)
    else:
        object_id = project.raster_id if kind == "raster" else project.vector_id
        obj = window.chapter.objects[object_id]
        canvas.set_selection("object", object_id)
    canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    if kind == "raster":
        path = QPainterPath()
        path.addRect(QRectF(tab * 300, 0, 100, 100))
        canvas._drawing_selection_path = path
    elif kind == "vector":
        stroke = obj.strokes[0]
        canvas._set_vector_selection(
            obj, {stroke.stroke_id},
            {point.point_id for point in stroke.points[tab:tab + 2]},
        )
    else:
        canvas._set_shape_point_selection(
            obj, {node.node_id for node in obj.bound.nodes[tab:tab + 2]},
            preserve_primary=False,
        )
    canvas._refresh_drawing_selection_transform()
    canvas._selection_pivot = QPointF(15 + tab, 25 + tab)
    canvas._selection_pivot_custom = True
    return obj


def _selection(canvas):
    return (
        canvas._selection_snapshot(),
        list(canvas._selection_transform_quad or []),
        QPointF(canvas._selection_pivot) if canvas._selection_pivot is not None else None,
        canvas._selection_pivot_custom,
    )


def _tiles(canvas, object_id):
    return canvas.tiles.object_tiles(object_id)


@pytest.mark.parametrize("kind", ["raster", "vector", "shape"])
def test_each_tab_restores_its_selection_without_editing_history(editor, kind):
    window, projects = editor
    saved = []
    for tab, project in enumerate(projects):
        window.project_tabs.setCurrentIndex(tab)
        _select(window, project, kind, tab)
        assert window.save()
        saved.append((_selection(window.canvas), window.canvas.command_stack.revision))
    for tab in (0, 1, 0, 1):
        window.project_tabs.setCurrentIndex(tab)
        assert _selection(window.canvas) == saved[tab][0]
        assert window.canvas.command_stack.revision == saved[tab][1]
        assert not window._dirty
        assert not window.active_session.dirty

    state = window.active_session.canvas_state.drawing_selection
    assert state is not None
    assert window.canvas._selected_vector_point_ids is not state.vector_points
    assert window.canvas._selected_shape_node_ids is not state.shape_points
    assert window.canvas._selection_transform_quad is not state.quad
    window.canvas._selection_pivot.setX(999)
    assert state.pivot.x() == 16


@pytest.mark.parametrize("kind", ["raster", "vector", "shape"])
def test_restored_selection_transforms_and_undo_only_edit_active_tab(editor, kind):
    window, projects = editor
    obj = _select(window, projects[0], kind)
    before_model = window.chapter.to_dict()
    before_tiles = _tiles(window.canvas, projects[0].raster_id)
    window.project_tabs.setCurrentIndex(1)
    _select(window, projects[1], kind, 1)
    other_model = window.chapter.to_dict()
    other_tiles = _tiles(window.canvas, projects[1].raster_id)
    window.project_tabs.setCurrentIndex(0)
    canvas = window.canvas
    quad = canvas._selection_transform_quad
    center = QPointF(sum(x for x, _ in quad) / 4, sum(y for _, y in quad) / 4)
    press = center + QPointF(20, 0)
    assert canvas._begin_drawing_selection_transform(obj, press)
    assert canvas._selection_transform_mode == "translate"
    canvas._update_drawing_selection_transform(obj, press + QPointF(50, 25))
    assert canvas._finish_drawing_selection_transform(obj)
    after_model = canvas.chapter.to_dict()
    after_tiles = _tiles(canvas, projects[0].raster_id)
    assert after_model != before_model or after_tiles != before_tiles
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before_model
    assert _tiles(canvas, projects[0].raster_id) == before_tiles
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after_model
    assert _tiles(canvas, projects[0].raster_id) == after_tiles
    window.project_tabs.setCurrentIndex(1)
    assert window.chapter.to_dict() == other_model
    assert _tiles(canvas, projects[1].raster_id) == other_tiles


@pytest.mark.parametrize("kind", ["raster", "vector"])
def test_clear_restored_selection_preserves_other_content_and_tab(editor, kind):
    window, projects = editor
    _select(window, projects[0], kind)
    before_model = window.chapter.to_dict()
    before_tiles = _tiles(window.canvas, projects[0].raster_id)
    window.project_tabs.setCurrentIndex(1)
    _select(window, projects[1], kind, 1)
    other_model = window.chapter.to_dict()
    other_tiles = _tiles(window.canvas, projects[1].raster_id)
    window.project_tabs.setCurrentIndex(0)
    window._clear_canvas()
    if kind == "raster":
        assert window.canvas.tiles.tile(projects[0].raster_id, (0, 0)) is None
        assert window.canvas.tiles.tile(projects[0].raster_id, (1, 0)) == before_tiles[(1, 0)]
    else:
        obj = window.chapter.objects[projects[0].vector_id]
        assert sum(len(stroke.points) for stroke in obj.strokes) == 2
    after_model = window.chapter.to_dict()
    after_tiles = _tiles(window.canvas, projects[0].raster_id)
    window.canvas.command_stack.undo()
    assert window.chapter.to_dict() == before_model
    assert _tiles(window.canvas, projects[0].raster_id) == before_tiles
    window.canvas.command_stack.redo()
    assert window.chapter.to_dict() == after_model
    assert _tiles(window.canvas, projects[0].raster_id) == after_tiles
    window.project_tabs.setCurrentIndex(1)
    assert window.chapter.to_dict() == other_model
    assert _tiles(window.canvas, projects[1].raster_id) == other_tiles


def test_paste_overlay_survives_tab_switch_without_moving_underlying_pixels(editor):
    window, projects = editor
    project = projects[0]
    target = _select(window, project, "raster")
    payload = window.canvas.drawing_selection_clipboard()
    # The copied shape is blue; change the underlying original to red.
    window.canvas.tiles.paint_dab(target.object_id, QPointF(20, 20), 8, QColor("red"))
    assert window.canvas.paste_drawing_clipboard(payload)
    window.project_tabs.setCurrentIndex(1)
    _select(window, projects[1], "raster", 1)
    window.project_tabs.setCurrentIndex(0)
    canvas = window.canvas
    assert canvas._raster_paste_overlay is not None
    saved_overlay = window.active_session.canvas_state.drawing_selection.paste_overlay
    assert canvas._raster_paste_overlay is not saved_overlay
    assert canvas._begin_drawing_selection_transform(target, QPointF(70, 50))
    canvas._update_drawing_selection_transform(target, QPointF(170, 50))
    assert canvas._finish_drawing_selection_transform(target)
    tile = canvas.tiles.tile(target.object_id, (0, 0))
    assert tile.pixelColor(20, 20) == QColor("red")
    assert tile.pixelColor(120, 20) == QColor("blue")
    canvas.command_stack.undo()
    assert canvas.tiles.tile(target.object_id, (0, 0)).pixelColor(20, 20) == QColor("blue")
    canvas.command_stack.redo()
    assert canvas.tiles.tile(target.object_id, (0, 0)).pixelColor(20, 20) == QColor("red")


def test_cut_saved_selection_stays_deleted_after_save_and_reopen(editor):
    window, projects = editor
    _select(window, projects[0], "raster")
    assert window._cut_drawing_selection()
    assert window.save()
    _, loaded = projects[0].repository.load_chapter(projects[0].chapter_id)
    assert loaded.tile(projects[0].raster_id, (0, 0)) is None
    assert loaded.tile(projects[0].raster_id, (1, 0)) is not None
    window.canvas.command_stack.undo()
    assert window.save()
    _, loaded = projects[0].repository.load_chapter(projects[0].chapter_id)
    assert loaded.tile(projects[0].raster_id, (0, 0)) is not None
    window.canvas.command_stack.redo()
    assert window.save()
    _, loaded = projects[0].repository.load_chapter(projects[0].chapter_id)
    assert loaded.tile(projects[0].raster_id, (0, 0)) is None


def test_new_chapter_and_empty_canvas_have_no_previous_drawing_selection(editor):
    window, projects = editor
    _select(window, projects[0], "raster")
    window.chapter_combo.setCurrentIndex(1)
    canvas = window.canvas
    assert canvas._drawing_selection_path.isEmpty()
    assert canvas._selection_transform_quad is None
    assert canvas._selection_pivot is None
    canvas._drawing_selection_path.addRect(QRectF(10, 10, 20, 20))
    canvas._selected_shape_node_ids.add("old-shape-point")
    canvas._selected_vector_point_ids.add("old-vector-point")
    canvas.clear_document()
    assert canvas._drawing_selection_path.isEmpty()
    assert not canvas._selected_shape_node_ids
    assert not canvas._selected_vector_point_ids


def test_failed_chapter_switch_preserves_current_canvas_and_export_name(editor, monkeypatch):
    window, projects = editor
    _select(window, projects[0], "raster")
    assert window.save()
    before = _selection(window.canvas)
    chapter, session, stack = window.chapter, window.active_session, window.canvas.command_stack
    revision = stack.revision
    errors = []
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[2]))

    def fail_load(*args, **kwargs):
        raise OSError("Chapter unavailable")

    monkeypatch.setattr(window.repository, "load_chapter", fail_load)
    window.chapter_combo.setCurrentIndex(1)
    assert errors == ["Chapter unavailable"]
    assert window.chapter is chapter
    assert window.active_session is session
    assert window.canvas.command_stack is stack and stack.revision == revision
    assert _selection(window.canvas) == before
    assert not window._dirty
    assert window.chapter_combo.currentData() == chapter.chapter_id
    assert window.chapter_combo.currentText() == "First"
    # Keep export cheap while exercising its naming and publication path.
    monkeypatch.setattr(window.canvas, "render_preview", lambda image: image.fill(QColor("white")))
    window._export_png()
    assert len(list((window.repository.root / "exports").glob("First-*.png"))) == 1


def test_failed_manual_save_keeps_session_dirty(editor, monkeypatch):
    window, _ = editor
    window._mark_dirty(None)
    monkeypatch.setattr(QMessageBox, "critical", lambda *args: None)

    def fail_save(*args, **kwargs):
        raise OSError("Disk unavailable")

    monkeypatch.setattr(window.repository, "save_chapter", fail_save)
    assert not window.save()
    assert window._dirty and window.active_session.dirty


def test_switching_tabs_cancels_shape_selection_preview(editor):
    window, projects = editor
    shape = _select(window, projects[0], "shape")
    original = shape.bound.to_dict()
    canvas = window.canvas
    quad = list(canvas._selection_transform_quad)
    center = QPointF(sum(x for x, _ in quad) / 4, sum(y for _, y in quad) / 4)
    press = center + QPointF(20, 0)
    assert canvas._begin_drawing_selection_transform(shape, press)
    canvas._update_drawing_selection_transform(shape, press + QPointF(50, 25))
    assert shape.bound.to_dict() != original
    revision = canvas.command_stack.revision
    window.project_tabs.setCurrentIndex(1)
    window.project_tabs.setCurrentIndex(0)
    assert shape.bound.to_dict() == original
    assert canvas._selection_transform_quad == quad
    assert canvas._selection_transform_mode is None
    assert canvas._selection_before_tiles is None
    assert canvas.command_stack.revision == revision
