"""Focused history restores must not rebuild unrelated document/UI records."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, TextObject,
    VectorDrawingObject, VectorStroke, VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow


def _chapter(count=1):
    chapter = ChapterDocument(height=4000)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 4000))
    for index in range(count):
        layer = chapter.add_layer(page.layer_id, str(index),
            BoundGeometry.rectangle(0, index * 80, 600, 70))
        chapter.add_object(layer.layer_id, RasterObject(name=f"Ink {index}"))
    return chapter, page


def test_object_undo_keeps_unrelated_models_tree_rows_and_selection(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    chapter, _page = _chapter(150)
    window._set_chapter(chapter, TileStore())
    canvas = window.canvas
    target, other = list(chapter.objects.values())[:2]
    canvas.set_selection("object", target.object_id)
    before = chapter.to_dict()
    target.x, target.name = 12, "Moved ink"
    after = chapter.to_dict()
    canvas.push_model_change(before, after, "Move and rename object")
    resets, updates = [], []
    window.hierarchy_model.modelReset.connect(lambda: resets.append(True))
    window.hierarchy_model.dataChanged.connect(lambda *_: updates.append(True))
    old_index = window.hierarchy_model.index_for_entity("object", target.object_id)
    old_item = old_index.internalPointer()
    monkeypatch.setattr(window.tree, "scrollTo", lambda *_:
        pytest.fail("Object undo should retain the outliner viewport"))
    try:
        window._undo()
        assert canvas.chapter is window.chapter is chapter
        assert chapter.objects[other.object_id] is other
        assert chapter.to_dict() == before
        assert canvas.selected_id == target.object_id
        assert window.hierarchy_model.index_for_entity(
            "object", target.object_id).internalPointer() is old_item
        assert old_index.data() == "Ink 0"
        assert not resets and updates
        window._redo()
        assert chapter.to_dict() == after
        assert old_index.data() == "Moved ink"
        assert not resets
    finally:
        window.autosave_timer.stop()
        window.canvas._effect_jobs.cancel()
        window._dirty = False
        window.deleteLater()


def test_object_patch_history_retains_current_chapter_after_structural_restore(qapp):
    chapter, page = _chapter()
    raster = next(iter(chapter.objects.values()))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", raster.object_id)
    original = chapter.to_dict()
    raster.x = 20
    moved = chapter.to_dict()
    canvas.push_model_change(original, moved, "Move raster")
    layer = chapter.add_layer(page.layer_id, "New layer",
        BoundGeometry.rectangle(0, 100, 100, 100))
    expanded = chapter.to_dict()
    canvas.push_model_change(moved, expanded, "Add layer")

    canvas.command_stack.undo()
    assert canvas.chapter is chapter
    assert canvas.chapter.to_dict() == moved
    restored_chapter = canvas.chapter
    canvas.command_stack.undo()
    assert canvas.chapter is restored_chapter
    assert canvas.chapter.to_dict() == original
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == moved
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == expanded
    assert layer.layer_id in canvas.chapter.layers


def test_text_object_patch_round_trips_style_and_color_runs(qapp):
    chapter, page = _chapter()
    text = chapter.add_object(page.layer_id, TextObject(
        text="Before", layout_mode="free", width=200, height=80))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    before = chapter.to_dict()
    text.text = "Colored text"
    text.color_runs = [{"start": 0, "end": 7, "color": "#FFFF0000"}]
    text.bold = True
    text.font_size = 30
    after = chapter.to_dict()
    canvas.push_model_change(before, after, "Edit text")
    for _ in range(2):
        canvas.command_stack.undo()
        assert canvas.chapter is chapter
        assert chapter.to_dict() == before
        canvas.command_stack.redo()
        assert chapter.to_dict() == after


def test_raster_undo_redo_repaints_only_stroke_region_with_matching_pixels(qapp):
    chapter, page = _chapter()
    target = chapter.add_object(page.layer_id, RasterObject())
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(600, 480)
    canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 300, 240, 1.0
    canvas.set_selection("object", target.object_id)
    canvas._ensure_scene_cache()
    empty = QImage(canvas._scene_cache)
    canvas._begin_stroke(QPointF(100, 100), 1.0)
    canvas._continue_stroke(QPointF(200, 110), 1.0)
    canvas._end_stroke()
    canvas._ensure_scene_cache()
    painted = QImage(canvas._scene_cache)
    assert painted != empty
    dirty = []
    canvas.documentChanged.connect(dirty.append)

    canvas.command_stack.undo()
    assert dirty[-1].contains(QPointF(150, 105))
    assert not canvas._scene_dirty_full
    assert canvas._scene_dirty_widget.width() < canvas.width() // 2
    canvas._ensure_scene_cache()
    assert canvas._scene_cache == empty
    canvas.command_stack.redo()
    assert not canvas._scene_dirty_full
    canvas._ensure_scene_cache()
    assert canvas._scene_cache == painted


def test_vector_history_parses_only_changed_strokes(qapp, monkeypatch):
    chapter, page = _chapter()
    drawing = chapter.add_object(page.layer_id, VectorDrawingObject(strokes=[
        VectorStroke(points=[VectorStrokePoint(x=x, y=y, width=4)
                             for x in range(10)]) for y in range(250)
    ]))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", drawing.object_id)
    unchanged_stroke, changed_stroke = drawing.strokes[:2]
    before = drawing.to_dict()
    changed_stroke.points[4].y = 80
    changed_stroke.touch_render_revision()
    drawing.touch_revision()
    after = drawing.to_dict()
    canvas._push_vector_change({drawing.object_id: before}, "Move point")
    parsed = []
    original = VectorStroke.from_dict

    def parse(cls, payload):
        parsed.append(payload["id"])
        return original(payload)

    monkeypatch.setattr(VectorStroke, "from_dict", classmethod(parse))
    canvas.command_stack.undo()
    assert parsed == [changed_stroke.stroke_id]
    assert drawing.to_dict() == before
    assert drawing.strokes[0] is unchanged_stroke
    assert drawing.strokes[1] is changed_stroke
    parsed.clear()
    canvas.command_stack.redo()
    assert parsed == [changed_stroke.stroke_id]
    assert drawing.to_dict() == after
