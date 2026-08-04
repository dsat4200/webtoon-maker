"""Horizontal Asset Library gallery used by the permanent ribbon page."""
from __future__ import annotations

import json

from PySide6.QtCore import QMimeData, QSize, Qt, Signal
from PySide6.QtGui import QDrag, QIcon, QPixmap
from PySide6.QtWidgets import QAbstractItemView, QListWidget, QListWidgetItem, QMenu

from comic_editor.core.assets import AssetRepository
from comic_editor.ui.canvas import ASSET_MIME


class AssetLibraryWidget(QListWidget):
    assetActivated = Signal(str)
    renameRequested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.repository: AssetRepository | None = None
        self.setObjectName("assetLibraryGallery")
        self.setViewMode(QListWidget.IconMode)
        self.setFlow(QListWidget.LeftToRight)
        self.setWrapping(False)
        self.setResizeMode(QListWidget.Adjust)
        self.setMovement(QListWidget.Static)
        self.setIconSize(QSize(82, 82))
        self.setGridSize(QSize(112, 108))
        self.setSelectionMode(QAbstractItemView.SingleSelection)
        self.setDragEnabled(True)
        self.setHorizontalScrollMode(QAbstractItemView.ScrollPerPixel)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setContextMenuPolicy(Qt.CustomContextMenu)
        self.itemDoubleClicked.connect(self._activate_item)
        self.customContextMenuRequested.connect(self._show_context_menu)
        self.refresh()

    def set_repository(self, repository: AssetRepository | None) -> None:
        self.repository = repository
        self.refresh()

    def refresh(self) -> None:
        selected = self.currentItem().data(Qt.UserRole) if self.currentItem() else ""
        self.clear()
        assets = self.repository.list_assets() if self.repository is not None else []
        if not assets:
            item = QListWidgetItem("No assets in this series")
            item.setFlags(Qt.NoItemFlags)
            item.setTextAlignment(Qt.AlignCenter)
            self.addItem(item)
            return
        for asset in assets:
            thumbnail = self.repository.thumbnail_path(asset.asset_id)
            pixmap = QPixmap(str(thumbnail)) if thumbnail.is_file() else QPixmap()
            item = QListWidgetItem(QIcon(pixmap), asset.name)
            item.setData(Qt.UserRole, asset.asset_id)
            item.setToolTip(f"Drag to place {asset.name}; double-click to edit")
            self.addItem(item)
            if asset.asset_id == selected:
                self.setCurrentItem(item)

    def _activate_item(self, item: QListWidgetItem) -> None:
        asset_id = str(item.data(Qt.UserRole) or "")
        if asset_id:
            self.assetActivated.emit(asset_id)

    def _show_context_menu(self, point) -> None:
        item = self.itemAt(point)
        asset_id = str(item.data(Qt.UserRole) or "") if item else ""
        if not asset_id:
            return
        self.setCurrentItem(item)
        menu = QMenu(self)
        rename = menu.addAction("Rename")
        selected = menu.exec(self.viewport().mapToGlobal(point))
        if selected is rename:
            self.renameRequested.emit(asset_id)

    def startDrag(self, supported_actions) -> None:  # noqa: N802
        item = self.currentItem()
        asset_id = str(item.data(Qt.UserRole) or "") if item else ""
        if not asset_id:
            return
        mime = QMimeData()
        mime.setData(ASSET_MIME, json.dumps({"asset_id": asset_id}).encode("utf-8"))
        drag = QDrag(self)
        drag.setMimeData(mime)
        pixmap = item.icon().pixmap(self.iconSize())
        if not pixmap.isNull():
            drag.setPixmap(pixmap)
            drag.setHotSpot(pixmap.rect().center())
        drag.exec(Qt.CopyAction)
