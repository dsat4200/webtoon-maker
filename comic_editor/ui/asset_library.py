"""Vertical, folder-aware asset browser used by the permanent ribbon page."""
from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView, QHBoxLayout, QLineEdit, QListWidget,
    QListWidgetItem, QPushButton, QToolButton, QVBoxLayout, QWidget, QMenu,
)

from comic_editor.core.assets import AssetFolder, AssetRepository
from comic_editor.ui.canvas import ASSET_MIME
from comic_editor.ui.icons import iconoir


ITEM_KIND_ROLE = Qt.ItemDataRole.UserRole + 1
FOLDER_KIND = "folder"
ASSET_KIND = "asset"


class _AssetGrid(QListWidget):
    def __init__(self, owner: "AssetLibraryWidget"):
        super().__init__(owner)
        self.owner = owner

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        self.owner._start_asset_drag()

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.mimeData().hasFormat(ASSET_MIME):
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(ASSET_MIME):
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        if item is not None and item.data(ITEM_KIND_ROLE) == FOLDER_KIND:
            event.setDropAction(Qt.DropAction.CopyAction)
            event.accept()
            return
        event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        if not event.mimeData().hasFormat(ASSET_MIME):
            event.ignore()
            return
        item = self.itemAt(event.position().toPoint())
        if item is None or item.data(ITEM_KIND_ROLE) != FOLDER_KIND:
            event.ignore()
            return
        try:
            payload = json.loads(bytes(event.mimeData().data(ASSET_MIME)))
            asset_id = str(payload.get("asset_id") or "")
            folder_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
            if not asset_id or not folder_id:
                raise ValueError
            repository = self.owner.repository
            if repository is None:
                raise RuntimeError("Asset library is not open")
            if repository.folder_for_asset(asset_id) != folder_id:
                repository.move_asset(asset_id, folder_id)
        except (
            AttributeError, KeyError, OSError, RuntimeError, TypeError,
            ValueError, json.JSONDecodeError,
        ) as error:
            self.owner.statusMessage.emit(str(error))
            event.ignore()
            return
        event.setDropAction(Qt.DropAction.CopyAction)
        event.accept()
        self.owner.refresh()
        self.owner.libraryChanged.emit()


class AssetLibraryWidget(QWidget):
    """Single-select grid of assets and nested folders."""

    assetActivated = Signal(str)
    renameRequested = Signal(str)
    deleteRequested = Signal(str)
    folderRenameRequested = Signal(str)
    folderDeleteRequested = Signal(str)
    libraryChanged = Signal()
    statusMessage = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.repository: AssetRepository | None = None
        self.current_folder_id: str | None = None
        self._selected_folder_id: str | None = None
        self.setObjectName("assetLibraryGallery")

        directory_row = QWidget(self)
        directory_layout = QHBoxLayout(directory_row)
        directory_layout.setContentsMargins(0, 0, 0, 0)
        directory_layout.setSpacing(2)
        self.up_folder_button = QToolButton(directory_row)
        self.up_folder_button.setIcon(iconoir("nav-arrow-up"))
        self.up_folder_button.setFixedSize(30, 30)
        self.up_folder_button.setAutoRaise(True)
        self.up_folder_button.setToolTip("Go up one asset folder")
        self.up_folder_button.clicked.connect(self._go_to_parent)
        directory_layout.addWidget(self.up_folder_button)
        self.breadcrumb = QPushButton("All Assets", directory_row)
        self.breadcrumb.setObjectName("assetLibraryBreadcrumb")
        self.breadcrumb.setFlat(True)
        self.breadcrumb.setEnabled(False)
        self.breadcrumb.clicked.connect(self._go_to_root)
        directory_layout.addWidget(self.breadcrumb, 1)
        self.grid = _AssetGrid(self)
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setFlow(QListWidget.Flow.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setIconSize(QSize(82, 82))
        self.grid.setGridSize(QSize(112, 108))
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setDragEnabled(True)
        self.grid.setAcceptDrops(True)
        self.grid.setDragDropMode(QAbstractItemView.DragDropMode.DragDrop)
        self.grid.setDefaultDropAction(Qt.DropAction.CopyAction)
        self.grid.setDropIndicatorShown(True)
        self.grid.setHorizontalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.grid.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.grid.itemDoubleClicked.connect(self._activate_item)
        self.grid.itemSelectionChanged.connect(self._selection_changed)
        self.grid.customContextMenuRequested.connect(self._show_context_menu)

        self.add_folder_button = QPushButton(self)
        self.add_folder_button.setIcon(iconoir("folder-plus"))
        self.add_folder_button.setToolTip("Add folder")
        self.add_folder_button.setFixedSize(32, 32)
        self.delete_folder_button = QPushButton(self)
        self.delete_folder_button.setIcon(iconoir("trash"))
        self.delete_folder_button.setToolTip("Delete selected folder")
        self.delete_folder_button.setFixedSize(32, 32)
        self.folder_name = QLineEdit(self)
        self.folder_name.setPlaceholderText("Selected folder")
        self.folder_name.setEnabled(False)
        self.folder_name.editingFinished.connect(self._rename_selected_folder)
        self.add_folder_button.clicked.connect(self._add_folder)
        self.delete_folder_button.clicked.connect(
            lambda: self._emit_selected_folder(self.folderDeleteRequested)
        )

        footer = QWidget(self)
        footer_layout = QHBoxLayout(footer)
        footer_layout.setContentsMargins(0, 2, 0, 0)
        footer_layout.setSpacing(4)
        footer_layout.addWidget(self.add_folder_button)
        footer_layout.addWidget(self.folder_name, 1)
        footer_layout.addWidget(self.delete_folder_button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(2)
        layout.addWidget(directory_row)
        layout.addWidget(self.grid, 1)
        layout.addWidget(footer)
        self.refresh()

    def set_repository(self, repository: AssetRepository | None) -> None:
        self.repository = repository
        self.current_folder_id = None
        self._selected_folder_id = None
        self.refresh()

    def selected_folder_id(self) -> str | None:
        return self._selected_folder_id

    def _folder_icon(self, folder: AssetFolder) -> QIcon:
        pixmap = QPixmap(self.grid.iconSize())
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform)
        painter.fillRect(pixmap.rect(), Qt.GlobalColor.transparent)
        folder_glyph = iconoir("folder", 28).pixmap(28, 28)
        painter.drawPixmap(
            pixmap.width() - folder_glyph.width() - 2,
            pixmap.height() - folder_glyph.height() - 2,
            folder_glyph,
        )
        children: list[tuple[str, object]] = []
        if self.repository is not None:
            children.extend(
                (FOLDER_KIND, child)
                for child in self.repository.list_folders(folder.folder_id)
            )
            children.extend(
                (ASSET_KIND, asset)
                for asset in self.repository.assets_in_folder(folder.folder_id)
            )
        children = children[:4]
        cell = 29
        margin = 8
        for index, (kind, value) in enumerate(children):
            if kind == FOLDER_KIND:
                child = self._folder_icon(value).pixmap(cell, cell)
            else:
                asset = value
                thumbnail = self.repository.thumbnail_path(asset.asset_id)
                child = QPixmap(str(thumbnail)) if thumbnail.is_file() else QPixmap()
                if child.isNull():
                    child = iconoir("media-image-plus", cell).pixmap(cell, cell)
            child = child.scaled(
                cell, cell, Qt.AspectRatioMode.KeepAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            x = margin + (index % 2) * (cell + margin)
            y = margin + (index // 2) * (cell + margin)
            painter.drawPixmap(x, y, child)
        painter.end()
        return QIcon(pixmap)

    def refresh(self) -> None:
        selected_kind = None
        selected_id = None
        current = self.grid.currentItem()
        if current is not None:
            selected_kind = current.data(ITEM_KIND_ROLE)
            selected_id = current.data(Qt.ItemDataRole.UserRole)
        self.grid.clear()
        if self.repository is None:
            self.breadcrumb.setText("All Assets")
            self.up_folder_button.setEnabled(False)
            self.folder_name.clear()
            self.folder_name.setEnabled(False)
            return
        folder = self.repository.get_folder(self.current_folder_id) if self.current_folder_id else None
        if self.current_folder_id is not None and folder is None:
            self.current_folder_id = None
            self._selected_folder_id = None
            folder = None
        self.breadcrumb.setText(
            "All Assets" if folder is None else f"All Assets / {folder.name}"
        )
        self.breadcrumb.setEnabled(folder is not None)
        self.up_folder_button.setEnabled(folder is not None)
        for child in self.repository.list_folders(self.current_folder_id):
            item = QListWidgetItem(self._folder_icon(child), child.name)
            item.setData(Qt.ItemDataRole.UserRole, child.folder_id)
            item.setData(ITEM_KIND_ROLE, FOLDER_KIND)
            item.setToolTip(f"Open folder {child.name}")
            self.grid.addItem(item)
        for asset in self.repository.assets_in_folder(self.current_folder_id):
            thumbnail = self.repository.thumbnail_path(asset.asset_id)
            pixmap = QPixmap(str(thumbnail)) if thumbnail.is_file() else QPixmap()
            item = QListWidgetItem(QIcon(pixmap), asset.name)
            item.setData(Qt.ItemDataRole.UserRole, asset.asset_id)
            item.setData(ITEM_KIND_ROLE, ASSET_KIND)
            item.setToolTip(f"Drag to place {asset.name}; double-click to edit")
            self.grid.addItem(item)
        if selected_id:
            for index in range(self.grid.count()):
                item = self.grid.item(index)
                if item.data(ITEM_KIND_ROLE) == selected_kind and item.data(Qt.ItemDataRole.UserRole) == selected_id:
                    self.grid.setCurrentItem(item)
                    break
        self._selection_changed()

    def _selection_changed(self) -> None:
        item = self.grid.currentItem()
        folder_id = (
            str(item.data(Qt.ItemDataRole.UserRole))
            if item is not None and item.data(ITEM_KIND_ROLE) == FOLDER_KIND
            else None
        )
        self._selected_folder_id = folder_id
        self.folder_name.setEnabled(folder_id is not None)
        self.folder_name.setText(
            self.repository.get_folder(folder_id).name
            if folder_id and self.repository and self.repository.get_folder(folder_id)
            else ""
        )
        self.delete_folder_button.setEnabled(folder_id is not None)

    def _activate_item(self, item: QListWidgetItem) -> None:
        kind = item.data(ITEM_KIND_ROLE)
        item_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if kind == FOLDER_KIND and item_id:
            self.current_folder_id = item_id
            self._selected_folder_id = None
            self.refresh()
        elif kind == ASSET_KIND and item_id:
            self.assetActivated.emit(item_id)

    def _add_folder(self) -> None:
        if self.repository is None:
            return
        parent = self.current_folder_id
        name = "New Folder"
        suffix = 2
        while True:
            try:
                folder = self.repository.create_folder(name, parent)
                break
            except ValueError:
                name = f"New Folder ({suffix})"
                suffix += 1
        self.refresh()
        for index in range(self.grid.count()):
            item = self.grid.item(index)
            if item.data(ITEM_KIND_ROLE) == FOLDER_KIND and item.data(Qt.ItemDataRole.UserRole) == folder.folder_id:
                self.grid.setCurrentItem(item)
                break
        self.libraryChanged.emit()

    def _go_to_root(self) -> None:
        if self.current_folder_id is None:
            return
        self.current_folder_id = None
        self._selected_folder_id = None
        self.refresh()

    def _go_to_parent(self) -> None:
        if self.repository is None or self.current_folder_id is None:
            return
        folder = self.repository.get_folder(self.current_folder_id)
        if folder is None:
            self.current_folder_id = None
        else:
            self.current_folder_id = folder.parent_id
        self._selected_folder_id = None
        self.refresh()

    def _rename_selected_folder(self) -> None:
        folder_id = self._selected_folder_id
        if not folder_id or self.repository is None:
            return
        try:
            self.repository.rename_folder(folder_id, self.folder_name.text())
        except (OSError, ValueError, FileNotFoundError) as error:
            self.statusMessage.emit(str(error))
        self.refresh()
        self.libraryChanged.emit()

    def _emit_selected_folder(self, signal) -> None:
        if self._selected_folder_id:
            signal.emit(self._selected_folder_id)

    def _show_context_menu(self, point) -> None:
        item = self.grid.itemAt(point)
        if item is None:
            return
        self.grid.setCurrentItem(item)
        kind = item.data(ITEM_KIND_ROLE)
        item_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not item_id:
            return
        menu = QMenu(self)
        rename = menu.addAction("Rename")
        delete = menu.addAction("Delete")
        selected = menu.exec(self.grid.viewport().mapToGlobal(point))
        if selected is rename:
            (self.folderRenameRequested if kind == FOLDER_KIND else self.renameRequested).emit(item_id)
        elif selected is delete:
            (self.folderDeleteRequested if kind == FOLDER_KIND else self.deleteRequested).emit(item_id)

    def _start_asset_drag(self) -> None:
        item = self.grid.currentItem()
        if item is None or item.data(ITEM_KIND_ROLE) != ASSET_KIND:
            return
        asset_id = str(item.data(Qt.ItemDataRole.UserRole) or "")
        if not asset_id:
            return
        mime = QMimeData()
        mime.setData(ASSET_MIME, json.dumps({"asset_id": asset_id}).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = item.icon().pixmap(self.grid.iconSize())
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())
        drag.exec(Qt.DropAction.CopyAction)
