"""Drag-reorderable hierarchy model with document invariant enforcement."""
from __future__ import annotations

import json
from dataclasses import dataclass, field

from PySide6.QtCore import QAbstractItemModel, QMimeData, QModelIndex, Qt, Signal
from PySide6.QtCore import QRect
from PySide6.QtGui import QColor, QIcon, QPainter, QPen, QPixmap
from PySide6.QtWidgets import QApplication, QStyledItemDelegate, QStyle, QStyleOptionViewItem

from comic_editor.core.models import (
    ChapterDocument, GradientObject, ImageObject, LayerNode, SpeedLinesGradientObject,
    RasterObject, TextObject, VectorDrawingObject,
)


@dataclass
class TreeItem:
    kind: str
    entity_id: str
    parent: "TreeItem | None" = None
    children: list["TreeItem"] = field(default_factory=list)


class HierarchyModel(QAbstractItemModel):
    mutationCommitted = Signal(object, object, str)

    MIME = "application/x-vertical-comic-entity"
    _reference_icon_cache: QIcon | None = None

    @classmethod
    def _reference_icon(cls) -> QIcon:
        if cls._reference_icon_cache is None:
            pixmap = QPixmap(16, 16)
            pixmap.fill(Qt.GlobalColor.transparent)
            painter = QPainter(pixmap)
            painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
            painter.setPen(QPen(QColor("#f4d35e"), 1.5))
            painter.setBrush(QColor("#d59b32"))
            painter.drawRect(6, 6, 4, 7)
            painter.drawLine(4, 14, 12, 14)
            painter.drawLine(4, 14, 6, 12)
            painter.drawLine(12, 14, 10, 12)
            painter.drawLine(8, 6, 3, 3)
            painter.drawLine(8, 6, 13, 3)
            painter.end()
            cls._reference_icon_cache = QIcon(pixmap)
        return cls._reference_icon_cache

    def __init__(self, chapter: ChapterDocument | None = None, parent=None):
        super().__init__(parent)
        self.chapter = chapter
        self.root = TreeItem("root", "")
        self._items: dict[tuple[str, str], TreeItem] = {}
        self.link_highlights: set[tuple[str, str]] = set()
        self.mask_highlights: set[tuple[str, str]] = set()
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

        def build_object(object_id: str, parent: TreeItem) -> TreeItem:
            object_item = TreeItem("object", object_id, parent)
            self._items[("object", object_id)] = object_item
            parent.children.append(object_item)
            return object_item

        def build_layer(layer_id: str, parent: TreeItem) -> TreeItem:
            item = TreeItem("layer", layer_id, parent)
            self._items[("layer", layer_id)] = item
            parent.children.append(item)
            layer = self.chapter.layers[layer_id]
            for child in layer.children:
                if child.kind == "layer":
                    build_layer(child.entity_id, item)
                else:
                    build_object(child.entity_id, item)
            return item

        if self.chapter.document_kind == "asset" and self.chapter.root_page_ids:
            container = self.chapter.layers[self.chapter.root_page_ids[0]]
            for child in container.children:
                if child.kind == "layer":
                    build_layer(child.entity_id, self.root)
                else:
                    build_object(child.entity_id, self.root)
        else:
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
        if (
            item.kind == "layer"
            and item.entity_id not in self.chapter.layers
        ) or (
            item.kind == "object"
            and item.entity_id not in self.chapter.objects
        ):
            return None
        entity = (
            self.chapter.layers[item.entity_id]
            if item.kind == "layer" else self.chapter.objects[item.entity_id]
        )
        if role in (Qt.DisplayRole, Qt.EditRole):
            if index.column() == 0:
                if isinstance(entity, TextObject):
                    if role == Qt.EditRole:
                        return entity.name
                    return entity.name if entity.custom_name else entity.display_name
                return entity.name
            if index.column() == 1:
                if item.kind == "layer":
                    if entity.layer_kind == "open_shape":
                        return "Open Shape"
                    primitive = {
                        "rectangle": "Rectangle",
                        "ellipse": "Circle",
                        "custom": "Shape",
                    }[entity.bound.primitive]
                    return "Page" if entity.is_page else f"{primitive} Layer"
                if isinstance(entity, VectorDrawingObject):
                    return "Vector Drawing"
                if isinstance(entity, GradientObject):
                    if isinstance(entity, SpeedLinesGradientObject):
                        return "Speed Lines"
                    return "Gradient"
                if isinstance(entity, ImageObject) and entity.is_blender_linked:
                    return "Blender Comic View"
                return entity.object_type.title()
            if index.column() == 2:
                return f"{round(entity.opacity * 100)}%"
        if (
            role == Qt.DecorationRole and index.column() == 0
            and entity.fill_reference
        ):
            return self._reference_icon()
        if role == Qt.CheckStateRole and index.column() == 0:
            return Qt.Checked if entity.visible else Qt.Unchecked
        if role == Qt.ToolTipRole:
            if entity.fill_reference:
                return "Reference layer source for Fill tools."
            if item.kind == "layer":
                return "Drag to reorder or nest. Page layers remain at the root."
            if isinstance(entity, VectorDrawingObject):
                return "Editable vector strokes."
            return "Drag objects between page or container layers."
        if role == Qt.BackgroundRole:
            if (item.kind, item.entity_id) in self.mask_highlights:
                return QColor("#5f9f72")
            if (item.kind, item.entity_id) in self.link_highlights:
                return QColor("#b85b12")
            return QColor("#303238") if item.kind == "layer" else QColor("#050505")
        if role == Qt.ForegroundRole:
            return QColor("#eeeeee")
        return None

    def setData(self, index: QModelIndex, value, role=Qt.EditRole) -> bool:
        if not index.isValid() or self.chapter is None:
            return False
        item = self.item_for_index(index)
        if (
            item.kind == "layer"
            and item.entity_id not in self.chapter.layers
        ) or (
            item.kind == "object"
            and item.entity_id not in self.chapter.objects
        ):
            return False
        entity = (
            self.chapter.layers[item.entity_id]
            if item.kind == "layer" else self.chapter.objects[item.entity_id]
        )
        before = self.chapter.to_dict()
        if role == Qt.CheckStateRole and index.column() == 0:
            try:
                check_state = Qt.CheckState(value)
            except (TypeError, ValueError):
                return False
            entity.visible = check_state == Qt.Checked
            label = "Toggle visibility"
        elif (
            role == Qt.EditRole and index.column() == 0
            and str(value).strip()
        ):
            entity.name = str(value).strip()
            if isinstance(entity, TextObject):
                entity.custom_name = True
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
        if self.chapter is None or (
            item.kind == "layer"
            and item.entity_id not in self.chapter.layers
        ) or (
            item.kind == "object"
            and item.entity_id not in self.chapter.objects
        ):
            return Qt.NoItemFlags
        flags = Qt.ItemIsEnabled | Qt.ItemIsSelectable | Qt.ItemIsUserCheckable
        if not (
            self.chapter.document_kind == "asset" and item.parent is self.root
        ):
            flags |= Qt.ItemIsDragEnabled
        entity = (
            self.chapter.objects.get(item.entity_id)
            if item.kind == "object" else None
        )
        if index.column() == 0:
            flags |= Qt.ItemIsEditable
        if (
            item.kind == "layer"
            and self.chapter.layers.get(item.entity_id) is not None
            and self.chapter.layers[item.entity_id].layer_kind
            != "fill"
        ):
            flags |= Qt.ItemIsDropEnabled
        if isinstance(entity, VectorDrawingObject):
            flags |= Qt.ItemIsDropEnabled
        return flags

    def supportedDropActions(self):
        return Qt.MoveAction

    def mimeTypes(self) -> list[str]:
        return [self.MIME]

    def mimeData(self, indexes) -> QMimeData:
        data = QMimeData()
        candidates = {
            (self.item_for_index(index).kind,
             self.item_for_index(index).entity_id)
            for index in indexes if index.isValid() and index.column() == 0
        }
        ordered: list[dict[str, str]] = []
        def visit(item: TreeItem) -> None:
            key = (item.kind, item.entity_id)
            if key in candidates:
                ordered.append({"kind": item.kind, "id": item.entity_id})
            for child in item.children:
                visit(child)
        visit(self.root)
        if ordered:
            payload = dict(ordered[0])
            payload["entities"] = ordered
            data.setData(
                self.MIME, json.dumps(payload).encode("utf-8")
            )
        return data

    @staticmethod
    def _payload_entities(payload: dict) -> list[tuple[str, str]]:
        raw = payload.get("entities")
        if not isinstance(raw, list):
            raw = [payload]
        result: list[tuple[str, str]] = []
        for item in raw:
            if isinstance(item, dict) and item.get("kind") and item.get("id"):
                key = (str(item["kind"]), str(item["id"]))
                if key not in result:
                    result.append(key)
        return result

    def canDropMimeData(self, data, action, row, column, parent) -> bool:
        if not data.hasFormat(self.MIME) or self.chapter is None:
            return False
        payload = json.loads(bytes(data.data(self.MIME)).decode("utf-8"))
        entities = self._payload_entities(payload)
        if not entities:
            return False
        item = self.item_for_index(parent)
        if len(entities) > 1:
            if item.kind != "layer":
                return False
            parent_layer = self.chapter.layers.get(item.entity_id)
            if parent_layer is None:
                return False
            return all(
                kind == "object" and isinstance(
                    self.chapter.objects.get(entity_id),
                    (RasterObject, VectorDrawingObject),
                )
                for kind, entity_id in entities
            )
        payload = {"kind": entities[0][0], "id": entities[0][1]}
        if item.kind == "object":
            return False
        moving_object = (
            self.chapter.objects.get(payload["id"])
            if payload["kind"] == "object" else None
        )
        if (
            item.kind == "layer"
            and self.chapter.layers[item.entity_id].layer_kind
            == "fill"
        ):
            return False
        if payload["kind"] == "layer" and self.chapter.layers[payload["id"]].is_page:
            return item is self.root
        if item is self.root:
            return False
        return True

    def dropMimeData(self, data, action, row, column, parent) -> bool:
        if action == Qt.IgnoreAction:
            return True
        if not self.canDropMimeData(data, action, row, column, parent):
            return False
        payload = json.loads(bytes(data.data(self.MIME)).decode("utf-8"))
        entities = self._payload_entities(payload)
        parent_item = self.item_for_index(parent)
        new_parent = None if parent_item is self.root else parent_item.entity_id
        if row < 0:
            row = len(parent_item.children)
        before = self.chapter.to_dict()
        try:
            if len(entities) > 1:
                self.chapter.move_entities(entities, new_parent, row)
            else:
                kind, entity_id = entities[0]
                self.chapter.move_entity(kind, entity_id, new_parent, row)
            self.chapter.validate()
        except ValueError:
            restored = ChapterDocument.from_dict(before)
            self.chapter.__dict__.update(restored.__dict__)
            return False
        after = self.chapter.to_dict()
        self.rebuild()
        self.mutationCommitted.emit(before, after, "Reorder hierarchy")
        return True

    def set_link_highlights(
        self, targets: set[tuple[str, str]] | None,
    ) -> None:
        previous = self.link_highlights
        self.link_highlights = set(targets or ())
        changed = previous | self.link_highlights
        for kind, entity_id in changed:
            index = self.index_for_entity(kind, entity_id)
            if index.isValid():
                self.dataChanged.emit(
                    index, index.siblingAtColumn(2), [Qt.BackgroundRole]
                )

    def set_mask_highlights(
        self, targets: set[tuple[str, str]] | None,
    ) -> None:
        previous = self.mask_highlights
        self.mask_highlights = set(targets or ())
        changed = previous | self.mask_highlights
        for kind, entity_id in changed:
            index = self.index_for_entity(kind, entity_id)
            if index.isValid():
                self.dataChanged.emit(
                    index, index.siblingAtColumn(2), [Qt.BackgroundRole]
                )


class EyeVisibilityDelegate(QStyledItemDelegate):
    _eye = None
    _eye_closed = None

    def _icons(self):
        if EyeVisibilityDelegate._eye is None:
            from comic_editor.ui.icons import iconoir
            EyeVisibilityDelegate._eye = iconoir("eye", 16)
            EyeVisibilityDelegate._eye_closed = iconoir("eye-closed", 16)
        return EyeVisibilityDelegate._eye, EyeVisibilityDelegate._eye_closed

    def paint(self, painter, option, index):
        if index.column() != 0:
            super().paint(painter, option, index)
            return
        eye, eye_closed = self._icons()
        visible = index.data(Qt.CheckStateRole) == Qt.Checked
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        opt.features &= ~QStyleOptionViewItem.ViewItemFeature.HasCheckIndicator
        opt.text = ""
        opt.icon = QIcon()
        widget = option.widget
        style = widget.style() if widget is not None else QApplication.style()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, opt, painter, widget)
        icon = eye if visible else eye_closed
        pix = icon.pixmap(16, 16)
        r = option.rect
        ix = r.left() + 4
        iy = r.top() + (r.height() - 16) // 2
        painter.drawPixmap(ix, iy, pix)
        display = index.data(Qt.DisplayRole)
        if display:
            ref = index.data(Qt.DecorationRole)
            has_ref = ref is not None
            text_x = ix + 20 + (18 if has_ref else 0)
            if has_ref and hasattr(ref, "pixmap"):
                rp = ref.pixmap(16, 16)
                painter.drawPixmap(ix + 20, iy, rp)
            painter.setPen(opt.palette.color(opt.palette.ColorRole.Text) if not (opt.state & QStyle.StateFlag.State_Selected) else opt.palette.color(opt.palette.ColorRole.HighlightedText))
            text_rect = r.adjusted(text_x - r.left(), 0, 0, 0)
            painter.drawText(text_rect, Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft, str(display))

    def editorEvent(self, event, model, option, index):
        if index.column() != 0 or not index.isValid():
            return super().editorEvent(event, model, option, index)
        if event.type() in (event.Type.MouseButtonRelease, event.Type.MouseButtonDblClick):
            r = option.rect
            ix = r.left() + 4
            iy = r.top() + (r.height() - 16) // 2
            icon_rect = QRect(ix, iy, 16, 16)
            pos = event.position().toPoint() if hasattr(event, "position") else event.pos()
            if icon_rect.contains(pos):
                current = index.data(Qt.CheckStateRole)
                new_state = Qt.Unchecked if current == Qt.Checked else Qt.Checked
                return model.setData(index, new_state, Qt.CheckStateRole)
            return False
        return super().editorEvent(event, model, option, index)
