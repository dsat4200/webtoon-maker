from __future__ import annotations

from PySide6.QtCore import QModelIndex, Qt

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, VectorDrawingObject, VectorFillObject,
)
from comic_editor.ui.tree_model import HierarchyModel


def _vector_tree():
    chapter = ChapterDocument()
    page = chapter.add_page()
    drawing = chapter.add_object(page.layer_id, VectorDrawingObject())
    first = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(geometry=BoundGeometry.rectangle(0, 0, 20, 20)),
    )
    second = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(geometry=BoundGeometry.rectangle(30, 0, 20, 20)),
    )
    return chapter, drawing, first, second


def test_vector_fills_are_nested_under_their_drawing(qapp):
    chapter, drawing, first, second = _vector_tree()
    model = HierarchyModel(chapter)
    drawing_index = model.index_for_entity("object", drawing.object_id)
    assert drawing_index.isValid()
    assert model.rowCount(drawing_index) == 2
    assert model.parent(
        model.index_for_entity("object", first.object_id)
    ) == drawing_index
    assert model.data(drawing_index.siblingAtColumn(1)) == "Vector Drawing"
    assert model.data(
        model.index_for_entity("object", first.object_id).siblingAtColumn(1)
    ) == "Vector Fill"


def test_vector_fill_drop_is_limited_to_its_owner(qapp):
    chapter, drawing, first, second = _vector_tree()
    model = HierarchyModel(chapter)
    first_index = model.index_for_entity("object", first.object_id)
    drawing_index = model.index_for_entity("object", drawing.object_id)
    mime = model.mimeData([first_index])
    assert model.canDropMimeData(
        mime, Qt.MoveAction, 2, 0, drawing_index
    )
    assert not model.canDropMimeData(
        mime, Qt.MoveAction, 0, 0, QModelIndex()
    )
    assert model.dropMimeData(
        mime, Qt.MoveAction, 2, 0, drawing_index
    )
    assert drawing.fill_child_ids == [second.object_id, first.object_id]
