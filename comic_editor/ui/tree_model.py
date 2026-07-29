"""Drag-reorderable hierarchy model with document invariant enforcement."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtGui import QColor

from comic_editor.core.models import ChapterDocument, LayerNode


@dataclass
class TreeItem:
    kind: str
    entity_id: str
    parent: "TreeItem | None" = None
    children: list["TreeItem"] = field(default_factory=list)


class HierarchyModel(QAbstractItemModel):
    mutationCommitted = Signal(object, object, str)

    MIME = "application/x-vertical-comic-entity"

    def __init__(self, chapter: ChapterDocument | None = None, parent=None):
        super().__init__(parent)
        self.chapter = chapter
        self.root = TreeItem("root", "")
        self._items: dict[tuple[str, str], TreeItem] = {}
        self.rebuild()

    def set_chapter(self, chapter: ChapterDocument | None) -> None:
        self.beginResetModel()
        self.chapter = chapter
        self._build()
        self.endResetModel()

    def rebuild(self) -> None:
        self.beginResetModel()
        self._build()
        self.endResetModel()

    def _build(self) -> None:
        self.root = TreeItem("root", "")
        self._items = {}
        if self.chapter is None:
            return

        def build_layer(layer_id: str, parent: TreeItem) -> TreeItem:
            item = TreeItem("layer", layer_id, parent)
            self._items[("layer", layer_id)] = item
            parent.children.append(item)
            layer = self.chapter.layers[layer_id]
            for child in layer.children:
                if child.kind == "layer":
                    build_layer(child.entity_id, item)
                else:
                    object_item = TreeItem("object", child.entity_id, item)
                    self._items[("object", child.entity_id)] = object_item
                    item.children.append(object_item)
            return item

        for page_id in self.chapter.root_page_ids:
            build_layer(page_id, self.root)

    def item_for_index(self, index: QModelIndex) -> TreeItem:
        return index.internalPointer() if index.isValid() else self.root

    def index_for_entity(self, kind: str, entity_id: str) -> QModelIndex:
        item = self._items.get((kind, entity_id))
        if not item or item.parent is None:
            return QModelIndex()
        row = item.parent.children.index(item)
        return self.createIndex(row, 0, item)

    def index(self, row: int, column: int, parent=QModelIndex()) -> QModelIndex:
        parent_item = self.item_for_index(parent)
        if not 0 <= row < len(parent_item.children):
            return QModelIndex()
        return self.createIndex(row, column, parent_item.children[row])

    def parent(self, index: QModelIndex) -> QModelIndex:
        if not index.isValid():
            return QModelIndex()
        item = self.item_for_index(index)
        parent_item = item.parent
        if parent_item is None or parent_item is self.root:
            return QModelIndex()
        grandparent = parent_item.parent or self.root
        return self.createIndex(grandparent.children.index(parent_item), 0, parent_item)

    def rowCount(self, parent=QModelIndex()) -> int:
        if parent.isValid() and parent.column() > 0:
            return 0
        return len(self.item_for_index(parent).children)

    def columnCount(self, parent=QModelIndex()) -> int:
        return 3

    def headerData(self, section: int, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return ("Name", "Type", "Opacity")[section]
        return None

    def data(self, index: QModelIndex, role=Qt.DisplayRole):
        if not index.isValid() or self.chapter is None:
            return None
        item = self.item_for_index(index)
        entity = (
            self.chapter.layers[item.entity_id]
            if item.kind == "layer" else self.chapter.objects[item.entity_id]
        )
        if role in (Qt.DisplayRole, Qt.EditRole):
            if index.column() == 0:
                return entity.name
            if index.column() == 1:
                if item.kind == "layer":
                    if entity.layer_kind == "fill":
                        return "Fill Layer"
                    if entity.layer_kind == "open_shape":
                        return "Open Shape"
                    primitive = {
                        "rectangle": "Rectangle",
                        "ellipse": "Circle",
                        "custom": "Shape",
                    }[entity.bound.primitive]
                    return "Page" if entity.is_page else f"{primitive} Layer"
                return entity.object_type.title()
            if index.column() == 2:
                return f"{round(entity.opacity * 100)}%"
        if role == Qt.CheckStateRole and index.column() == 0:
            return Qt.Checked if entity.visible else Qt.Unchecked
        if role == Qt.ToolTipRole:
            if item.kind == "layer":
                return "Drag to reorder or nest. Page layers remain at the root."
            return "Drag objects between non-page layers."
        if role == Qt.BackgroundRole:
            return QColor("#303238") if item.kind == "layer" else QColor("#050505")
        if role == Qt.ForegroundRole:
            return QColor("#eeeeee")
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or self.chapter is None:
            return False
        item = self.item_for_index(index)
        entity = (
            self.chapter.layers[item.entity_id]
            if item.kind == "layer" else self.chapter.objects[item.entity_id]
        )
        before = self.chapter.to_dict()
        if role == Qt.CheckStateRole and index.column() == 0:
            entity.visible = value == Qt.Checked
            label = "Toggle visibility"
        elif role == Qt.EditRole and index.column() == 0 and str(value).strip():
            entity.name = str(value).strip()
            label = "Rename entity"
        else:
            return False
        after = self.chapter.to_dict()
        self.dataChanged.emit(index, index, [role, Qt.DisplayRole])
        self.mutationCommitted.emit(before, after, label)
        return True

    def flags(self, index: QModelIndex):
        if not index.isValid():
            return Qt.ItemIsDropEnabled
        item = self.item_for_index(index)
        flags = (
            Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsDragEnabled
            | Qt.ItemIsUserCheckable
        )
        if index.column() == 0:
            flags |= Qt.ItemIsEditable
        if (
            item.kind == "layer"
            and self.chapter.layers[item.entity_id].layer_kind
            not in {"fill", "open_shape"}
        ):
            flags |= Qt.ItemIsDropEnabled
        return flags

    def supportedDropActions(self):
        return Qt.MoveAction

    def mimeTypes(self) -> list[str]:
        return [self.MIME]

    def mimeData(self, indexes) -> QMimeData:
        index = next((item for item in indexes if item.column() == 0), QModelIndex())
        data = QMimeData()
        if index.isValid():
            item = self.item_for_index(index)
            data.setData(self.MIME, json.dumps({
                "kind": item.kind, "id": item.entity_id
            }).encode("utf-8"))
        return data

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(self.MIME) or self.chapter is None:
            return False
        payload = json.loads(bytes(data.data(self.MIME)).decode("utf-8"))
        item = self.item_for_index(parent)
        if item.kind == "object":
            return False
        if (
            item.kind == "layer"
            and self.chapter.layers[item.entity_id].layer_kind
            in {"fill", "open_shape"}
        ):
            return False
        if payload["kind"] == "layer" and self.chapter.layers[payload["id"]].is_page:
            return item is self.root
        if item is self.root:
            return False
        if payload["kind"] == "object" and self.chapter.layers[item.entity_id].is_page:
            return False
        return True

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if action == Qt.IgnoreAction:
            return True
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        payload = json.loads(bytes(data.data(self.MIME)).decode("utf-8"))
        parent_item = self.item_for_index(parent)
        new_parent = None if parent_item is self.root else parent_item.entity_id
        if row < 0:
            row = len(parent_item.children)
        before = self.chapter.to_dict()
        try:
            self.chapter.move_entity(payload["kind"], payload["id"], new_parent, row)
            self.chapter.validate()
        except ValueError:
            restored = ChapterDocument.from_dict(before)
            self.chapter.__dict__.update(restored.__dict__)
            return False
        after = self.chapter.to_dict()
        self.rebuild()
        self.mutationCommitted.emit(before, after, "Reorder hierarchy")
        return True
