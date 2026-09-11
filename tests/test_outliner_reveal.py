"""Canvas selection and asset insertion reveal the matching outliner row."""
import json

import pytest
from PySide6.QtCore import QCoreApplication, QMimeData, QPointF, QRectF, Qt
from PySide6.QtGui import QDragEnterEvent, QDragMoveEvent, QDropEvent
from PySide6.QtTest import QTest

from comic_editor.core.assets import AssetRepository, extract_asset
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject, TextObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ASSET_MIME, ToolKind
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    result = MainWindow()
    result.resize(1280, 900)
    chapter = ChapterDocument(height=800)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 800))
    for index in range(35):
        chapter.add_layer(page.layer_id, f"Before {index}", BoundGeometry.rectangle(800, 20, 20, 20))
    outer = chapter.add_layer(page.layer_id, "Combined", BoundGeometry.rectangle(50, 50, 600, 600))
    outer.compound_enabled = True
    inner = chapter.add_layer(outer.layer_id, "Nested", BoundGeometry.rectangle(80, 80, 500, 500))
    for index in range(35):
        chapter.add_layer(page.layer_id, f"After {index}", BoundGeometry.rectangle(850, 20, 20, 20))
    result._set_chapter(chapter, TileStore())
    result.show()
    qapp.processEvents()
    result.canvas.center_x, result.canvas.center_y, result.canvas.scale = 350, 350, 1
    result.canvas._invalidate_scene_cache()
    result._test_outer, result._test_inner = outer, inner
    yield result
    result.autosave_timer.stop()
    result.canvas._effect_jobs.cancel()
    for session in result.sessions.values():
        session.dirty = False
    result._dirty = False
    result.hide()
    result.deleteLater()


def assert_revealed(window, kind, identifier, *, centered=False):
    index = window.hierarchy_model.index_for_entity(kind, identifier)
    assert index.isValid()
    assert window.tree.currentIndex().siblingAtColumn(0) == index
    assert window.tree.selectionModel().isRowSelected(index.row(), index.parent())
    parent = index.parent()
    while parent.isValid():
        assert window.tree.isExpanded(parent)
        parent = parent.parent()
    rect = window.tree.visualRect(index)
    assert not rect.isEmpty()
    assert window.tree.viewport().rect().contains(rect.center())
    if centered:
        assert abs(rect.center().y()-window.tree.viewport().rect().center().y()) <= rect.height()*1.5


@pytest.mark.parametrize("starting_tool", [ToolKind.OBJECT_SELECT, ToolKind.TEXT_EDIT, ToolKind.SHAPE_EDIT])
def test_click_nested_compound_text_reveals_collapsed_ancestors(window, qapp, starting_tool):
    canvas = window.canvas
    rect = QRectF(170, 170, 250, 120)
    text = window.chapter.add_object(window._test_inner.layer_id, TextObject(
        text="Select nested text", layout_mode="free", margin=0,
        width=rect.width(), height=rect.height(), transform_quad=canvas._rect_quad(rect),
    ))
    window._hierarchy_changed()
    canvas.set_selection("layer", window._test_outer.layer_id)
    canvas.set_tool(starting_tool)
    window.tree.collapseAll()
    canvas.setFocus()
    QTest.mouseClick(canvas, Qt.LeftButton, pos=canvas.document_to_widget(rect.center()).toPoint())
    qapp.processEvents()
    assert canvas.selected_kind == "object"
    assert canvas.selected_id == text.object_id
    assert_revealed(window, "object", text.object_id, centered=True)


@pytest.mark.parametrize("asset_kind", ["layer", "object"])
def test_library_asset_drop_reveals_inserted_row_after_model_reset(window, qapp, tmp_path, asset_kind):
    source = ChapterDocument(height=200)
    page = source.add_page("Source", BoundGeometry.rectangle(0, 0, 1080, 200))
    root = source.add_layer(page.layer_id, "Asset group", BoundGeometry.rectangle(0, 0, 90, 90))
    text = source.add_object(root.layer_id, TextObject(text="Asset text", layout_mode="free", width=80, height=80,
        transform_quad=[(0, 0), (80, 0), (80, 80), (0, 80)]))
    manifest, tiles = extract_asset(source, TileStore(), asset_kind,
        root.layer_id if asset_kind == "layer" else text.object_id, "Asset group")
    repository = AssetRepository(tmp_path)
    repository.create(manifest, tiles, window.canvas.render_asset_thumbnail(manifest, tiles))
    window.asset_library.set_repository(repository)
    window.canvas.asset_repository = repository
    canvas = window.canvas
    canvas.set_selection("layer", window._test_inner.layer_id)
    window.tree.collapseAll()
    mime = QMimeData()
    mime.setData(ASSET_MIME, json.dumps({"asset_id": manifest.asset_id}).encode())
    point = canvas.document_to_widget(QPointF(300, 300))
    enter = QDragEnterEvent(point.toPoint(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, enter)
    assert enter.isAccepted()
    move = QDragMoveEvent(point.toPoint(), Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, move)
    drop = QDropEvent(point, Qt.CopyAction, mime, Qt.LeftButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, drop)
    assert drop.isAccepted()
    qapp.processEvents()
    inserted = canvas.selected_id
    assert inserted != window._test_inner.layer_id
    target = window.chapter.modifier_target(asset_kind, inserted)
    assert (target.parent_id if asset_kind == "layer" else target.parent_layer_id) == window._test_inner.layer_id
    assert_revealed(window, asset_kind, inserted, centered=True)


def test_reveal_keeps_multi_selection_and_primary_through_reset(window, qapp):
    first = window.chapter.add_object(window._test_inner.layer_id, RasterObject(name="First"))
    second = window.chapter.add_object(window._test_inner.layer_id, RasterObject(name="Second"))
    window._hierarchy_changed()
    refs = [("object", first.object_id), ("object", second.object_id)]
    assert window.canvas.set_selection_set(refs, primary=refs[0])
    window.hierarchy_model.rebuild()
    qapp.processEvents()
    assert_revealed(window, *refs[0])
    rows = window.tree.selectionModel().selectedRows(0)
    assert {window.hierarchy_model.item_for_index(index).entity_id for index in rows} == {first.object_id, second.object_id}


def test_clicking_visible_tree_row_does_not_move_it_under_pointer(window, qapp):
    page = window.hierarchy_model.index_for_entity("layer", window.chapter.root_page_ids[0])
    window.tree.setExpanded(page, True)
    window.tree.scrollToTop()
    qapp.processEvents()
    index = window.hierarchy_model.index(3, 0, page)
    before = window.tree.visualRect(index)
    assert window.tree.viewport().rect().contains(before.center())
    QTest.mouseClick(window.tree.viewport(), Qt.LeftButton, pos=before.center())
    qapp.processEvents()
    assert window.tree.visualRect(index) == before
    assert window.tree.currentIndex().siblingAtColumn(0) == index
