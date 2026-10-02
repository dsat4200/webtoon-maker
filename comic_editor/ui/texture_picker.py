"""Category hover menu with lazy thumbnails and edge-hover scrolling."""
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from PySide6.QtCore import QPoint, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QCursor, QIcon, QPixmap
from PySide6.QtWidgets import (
    QComboBox, QFrame, QHBoxLayout, QLabel, QListWidget, QListWidgetItem,
    QVBoxLayout, QAbstractItemView,
)

from comic_editor.core.texture_library import texture_categories, texture_thumbnail

_THUMBNAILS = OrderedDict()


class TextureMenu(QFrame):
    selected = Signal(object, str)

    def __init__(self, combo):
        super().__init__(combo.window(), Qt.WindowType.Popup)
        self.combo = combo
        self.setObjectName("textureMenu")
        self.setStyleSheet("""
            QFrame#textureMenu { background: #202026; border: 1px solid #454550; }
            QFrame#textureMenu QLabel { background: transparent; color: #d8d9df; }
            QListWidget { background: #24242b; color: #d8d9df; border: none; outline: none; }
            QListWidget::item { border: 1px solid transparent; border-radius: 4px; padding: 3px; }
            QListWidget::item:hover { background: #303037; }
            QListWidget::item:selected { background: #34495d; border-color: #80c8ff; }
        """)
        self.setFrameShape(QFrame.Shape.StyledPanel)
        layout = QVBoxLayout(self)
        header = QHBoxLayout()
        header.addWidget(QLabel("Textures", self), 1)
        self.status = QLabel(self)
        header.addWidget(self.status)
        layout.addLayout(header)
        row = QHBoxLayout()
        self.categories = QListWidget(self)
        self.categories.setObjectName("textureCategories")
        self.categories.setMouseTracking(True)
        self.categories.setFixedWidth(150)
        self.categories.itemEntered.connect(lambda item: self.categories.setCurrentItem(item))
        self.categories.currentTextChanged.connect(self._category)
        row.addWidget(self.categories)
        self.grid = QListWidget(self)
        self.grid.setObjectName("textureGrid")
        self.grid.setViewMode(QListWidget.ViewMode.IconMode)
        self.grid.setResizeMode(QListWidget.ResizeMode.Adjust)
        self.grid.setMovement(QListWidget.Movement.Static)
        self.grid.setWrapping(True)
        self.grid.setFlow(QListWidget.Flow.LeftToRight)
        self.grid.setIconSize(QSize(144, 96))
        self.grid.setGridSize(QSize(160, 140))
        self.grid.setWordWrap(True)
        self.grid.setUniformItemSizes(True)
        self.grid.setMouseTracking(True)
        self.grid.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.grid.setVerticalScrollMode(QAbstractItemView.ScrollMode.ScrollPerPixel)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.grid.itemClicked.connect(self._select)
        self.grid.itemActivated.connect(self._select)
        row.addWidget(self.grid, 1)
        layout.addLayout(row, 1)
        self.help = QLabel("Hover a category to browse. Hover near the grid’s top or bottom to scroll.", self)
        self.help.setWordWrap(True)
        layout.addWidget(self.help)
        self._pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="texture-thumbnails")
        self._scan = None
        self._jobs = {}
        self._files = {}
        self._failed = set()
        self._items = {}
        self._generation = 0
        self._timer = QTimer(self)
        self._timer.setInterval(30)
        self._timer.timeout.connect(self._tick)
        pool = self._pool
        self.destroyed.connect(lambda: pool.shutdown(wait=False, cancel_futures=True))
        combo.destroyed.connect(self.deleteLater)

    def open(self):
        self._generation += 1
        self._failed.clear()
        self.categories.clear()
        self.grid.clear()
        self._files = {}
        self._items = {}
        self.help.setText("Hover a category to browse. Hover near the grid’s top or bottom to scroll.")
        self.status.setText("Reading library…")
        self._scan = self._pool.submit(texture_categories, self.combo.directory())
        screen = self.combo.screen().availableGeometry()
        self.resize(min(850, screen.width() - 24), min(600, screen.height() - 24))
        point = self.combo.mapToGlobal(QPoint(0, self.combo.height()))
        self.move(max(screen.left(), min(point.x(), screen.right() - self.width() + 1)),
                  max(screen.top(), min(point.y(), screen.bottom() - self.height() + 1)))
        self.show()
        self.categories.setFocus()
        self._timer.start()

    def hideEvent(self, event):
        self._timer.stop()
        for future in self._jobs:
            future.cancel()
        super().hideEvent(event)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def _category(self, category):
        self.grid.clear()
        self._items = {}
        files = self._files.get(category, [])
        self.status.setText(f"{len(files)} textures")
        for path in files:
            item = QListWidgetItem(path.stem, self.grid)
            item.setSizeHint(QSize(160, 140))
            item.setData(Qt.ItemDataRole.UserRole, path)
            item.setToolTip(path.name)
            self._items[path] = item
            if category == self.combo.category and path.name == self.combo.texture_name:
                self.grid.setCurrentItem(item)
        self.grid.scrollToTop()

    def _select(self, item):
        self.selected.emit(item.data(Qt.ItemDataRole.UserRole), self.categories.currentItem().text())
        self.hide()

    @staticmethod
    def _key(path):
        stat = path.stat()
        return str(path), stat.st_mtime_ns, stat.st_size

    def _tick(self):
        if self._scan is not None and self._scan.done():
            try:
                self._files = self._scan.result()
                self.status.setText("No textures found" if not self._files else "")
            except OSError:
                self._files = {}
                self.status.setText("Cannot read folder")
            self._scan = None
            self.categories.addItems(list(self._files))
            if self._files:
                names = list(self._files)
                self.categories.setCurrentRow(names.index(self.combo.category) if self.combo.category in names else 0)
            else:
                self.help.setText("Choose a texture library folder in Settings → Paths.")
        for future, (path, key, generation) in list(self._jobs.items()):
            if not future.done():
                continue
            self._jobs.pop(future)
            try:
                data = future.result()
                pixmap = QPixmap()
                pixmap.loadFromData(data)
                if pixmap.isNull():
                    raise ValueError("Invalid thumbnail")
                _THUMBNAILS[key] = QIcon(pixmap)
                while len(_THUMBNAILS) > 512:
                    _THUMBNAILS.popitem(last=False)
                item = self._items.get(path)
                if item is not None and generation == self._generation:
                    item.setIcon(_THUMBNAILS[key])
            except Exception:
                self._failed.add(path)
                if path in self._items:
                    self._items[path].setToolTip(f"{path.name}\nPreview unavailable")
        self._hover_scroll()
        pending = {path for path, _, _ in self._jobs.values()}
        viewport = self.grid.viewport().rect().adjusted(0, -140, 0, 140)
        for path, item in self._items.items():
            if path in self._failed or not self.grid.visualItemRect(item).intersects(viewport):
                continue
            try:
                key = self._key(path)
            except OSError:
                self._failed.add(path)
                continue
            if key in _THUMBNAILS:
                item.setIcon(_THUMBNAILS[key])
                _THUMBNAILS.move_to_end(key)
            elif path not in pending and len(self._jobs) < 4:
                self._jobs[self._pool.submit(texture_thumbnail, path)] = path, key, self._generation

    def _hover_scroll(self):
        viewport = self.grid.viewport()
        point = viewport.mapFromGlobal(QCursor.pos())
        if not viewport.rect().contains(point):
            return
        edge = 48
        delta = (-max(1, round((edge - point.y()) / 3)) if point.y() < edge else
                 max(1, round((point.y() - viewport.height() + edge) / 3))
                 if point.y() > viewport.height() - edge else 0)
        bar = self.grid.verticalScrollBar()
        bar.setValue(bar.value() + delta)


class TextureCombo(QComboBox):
    textureSelected = Signal(object, str)

    def __init__(self, directory, texture_name="", category="", parent=None):
        super().__init__(parent)
        self.directory = directory
        self.texture_name, self.category = texture_name, category
        self.setObjectName("textureSelector")
        self.addItem(texture_name or "Choose texture…")
        self.setToolTip("Browse textures by category")
        self._menu = None

    def showPopup(self):
        if self._menu is None:
            self._menu = TextureMenu(self)
            self._menu.selected.connect(self.textureSelected)
        self._menu.open()

    def hidePopup(self):
        if self._menu is not None:
            self._menu.hide()
        super().hidePopup()

    def hideEvent(self, event):
        self.hidePopup()
        super().hideEvent(event)
