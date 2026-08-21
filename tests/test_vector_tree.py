from __future__ import annotations

from PySide6.QtCore import Qt

from comic_editor.core.models import (
    ChapterDocument, RasterObject, VectorDrawingObject,
)
from comic_editor.ui.tree_model import HierarchyModel


def _vector_tree():
    chapter = ChapterDocument()
    page = chapter.add_page()
    drawing = chapter.add_object(page.layer_id, VectorDrawingObject())
    color = chapter.add_object(page.layer_id, RasterObject(name="Color"))
    return chapter, drawing, color


def test_vector_drawings_have_no_nested_fill_rows(qapp):
    chapter, drawing, color = _vector_tree()
    model = HierarchyModel(chapter)
    drawing_index = model.index_for_entity("object", drawing.object_id)
    assert drawing_index.isValid()
    assert model.rowCount(drawing_index) == 0
    assert model.data(drawing_index.siblingAtColumn(1)) == "Vector Drawing"
    assert model.data(
        model.index_for_entity("object", color.object_id).siblingAtColumn(1)
    ) == "Raster"


def test_reference_vector_uses_lighthouse_decoration(qapp):
    chapter, drawing, _color = _vector_tree()
    drawing.fill_reference = True
    model = HierarchyModel(chapter)
    drawing_index = model.index_for_entity("object", drawing.object_id)
    icon = model.data(
        drawing_index, Qt.ItemDataRole.DecorationRole
    )
    assert icon is not None and not icon.isNull()
    assert "Reference" in model.data(
        drawing_index, Qt.ItemDataRole.ToolTipRole
    )
