"""Outliner indices remain correct when large sibling lists change."""
from PySide6.QtCore import QPersistentModelIndex, Qt

from comic_editor.core.models import ChapterDocument, RasterObject
from comic_editor.ui.tree_model import HierarchyModel


def _assert_tree_indices(model, parent=None):
    from PySide6.QtCore import QModelIndex

    parent = parent if parent is not None else QModelIndex()
    for row in range(model.rowCount(parent)):
        index = model.index(row, 0, parent)
        item = model.item_for_index(index)
        assert model.index_for_entity(item.kind, item.entity_id) == index
        assert index.parent() == parent
        for column in range(model.columnCount()):
            sibling = model.index(row, column, parent)
            assert sibling.row() == row
            assert sibling.parent() == parent
        _assert_tree_indices(model, index)


def test_indices_follow_insertion_deletion_and_reparenting(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page()
    layers = [chapter.add_layer(page.layer_id, str(i)) for i in range(150)]
    objects = [chapter.add_object(layer.layer_id, RasterObject()) for layer in layers]
    model = HierarchyModel(chapter)
    _assert_tree_indices(model)
    old = QPersistentModelIndex(model.index_for_entity("object", objects[-1].object_id))

    chapter.move_entity("layer", layers[-1].layer_id, page.layer_id, 0)
    chapter.move_entity("object", objects[0].object_id, layers[-1].layer_id, 0)
    chapter.add_layer(page.layer_id, "Inserted", index=5)
    chapter.delete_entity("layer", layers[10].layer_id)
    model.rebuild()

    assert not old.isValid()
    _assert_tree_indices(model)
    moved = model.index_for_entity("layer", layers[-1].layer_id)
    assert moved.row() == 0
    assert model.index_for_entity("object", objects[0].object_id).parent() == moved


def test_asset_container_indices_and_drag_reordering(qapp):
    chapter = ChapterDocument(document_kind="asset")
    page = chapter.add_page()
    outer = chapter.add_layer(page.layer_id, "First")
    inner = chapter.add_layer(outer.layer_id, "Nested")
    last = chapter.add_layer(page.layer_id, "Last")
    chapter.add_object(inner.layer_id, RasterObject())
    model = HierarchyModel(chapter)
    _assert_tree_indices(model)
    assert model.rowCount() == 2
    assert not model.index_for_entity("layer", outer.layer_id).parent().isValid()
    payload = model.mimeData([model.index_for_entity("layer", inner.layer_id)])
    destination = model.index_for_entity("layer", last.layer_id)
    assert model.dropMimeData(payload, Qt.MoveAction, 0, 0, destination)
    _assert_tree_indices(model)
    assert model.index_for_entity("layer", inner.layer_id).parent() == model.index_for_entity("layer", last.layer_id)
