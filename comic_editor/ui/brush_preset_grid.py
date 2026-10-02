"""A large preset overlay whose tiles only paint already prepared images."""
from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt, Signal
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QListView, QListWidget,
    QListWidgetItem, QStyle, QStyledItemDelegate, QToolButton, QVBoxLayout,
)

from comic_editor.core.brush_thumbnails import THUMBNAIL_HEIGHT, THUMBNAIL_WIDTH


TILE_SIZE = QSize(176, 124)
ERROR_ROLE = Qt.UserRole + 1


class BrushTileDelegate(QStyledItemDelegate):
    def sizeHint(self, option, index):
        return self.parent().gridSize()

    def paint(self, painter, option, index):
        painter.save()
        rect = option.rect.adjusted(3, 3, -3, -3)
        selected = bool(option.state & QStyle.State_Selected)
        hovered = bool(option.state & QStyle.State_MouseOver)
        background = option.palette.color(QPalette.Highlight if selected else QPalette.Base)
        if hovered and not selected:
            background = option.palette.color(QPalette.AlternateBase)
        painter.setPen(option.palette.color(QPalette.Highlight if selected else QPalette.Mid))
        painter.setBrush(background)
        painter.drawRoundedRect(rect, 5, 5)
        icon_rect = QRect(rect.center().x()-THUMBNAIL_WIDTH//2, rect.top()+8,
                          THUMBNAIL_WIDTH, THUMBNAIL_HEIGHT)
        icon = index.data(Qt.DecorationRole)
        text_color = option.palette.color(QPalette.HighlightedText if selected else QPalette.Text)
        painter.setPen(text_color)
        if icon is not None and not icon.isNull():
            icon.paint(painter, icon_rect)
        else:
            painter.fillRect(icon_rect, option.palette.color(QPalette.AlternateBase))
            status = 'Preview unavailable' if index.data(ERROR_ROLE) else 'Preparing preview…'
            painter.drawText(icon_rect, Qt.AlignCenter, status)
        text_rect = QRect(rect.left()+7, icon_rect.bottom()+7, rect.width()-14,
                          rect.bottom()-icon_rect.bottom()-9)
        painter.setFont(option.font)
        painter.setClipRect(text_rect)
        painter.drawText(text_rect, Qt.AlignHCenter | Qt.AlignTop | Qt.TextWordWrap,
                         str(index.data(Qt.DisplayRole) or 'Brush'))
        painter.restore()


class BrushPresetGrid(QFrame):
    chosen = Signal(int)
    closed = Signal()

    def __init__(self, combo):
        super().__init__(combo.window(), Qt.Popup)
        self.combo = combo
        self.setObjectName('brushPresetGrid')
        self.setFrameShape(QFrame.StyledPanel)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 12, 12, 12)
        title_row = QHBoxLayout()
        title = QLabel('Brushes', self)
        font = title.font()
        font.setBold(True)
        font.setPointSizeF(font.pointSizeF()+2)
        title.setFont(font)
        self.count = QLabel(self)
        self.search = QLineEdit(self)
        self.search.setPlaceholderText('Find a brush…')
        self.search.setClearButtonEnabled(True)
        self.search.setMaximumWidth(360)
        self.search.installEventFilter(self)
        close = QToolButton(self)
        close.setIcon(self.style().standardIcon(QStyle.SP_TitleBarCloseButton))
        close.setToolTip('Close brush picker (Esc)')
        close.clicked.connect(self.hide)
        title_row.addWidget(title)
        title_row.addWidget(self.count)
        title_row.addStretch(1)
        title_row.addWidget(self.search, 1)
        title_row.addWidget(close)
        layout.addLayout(title_row)
        self.grid = QListWidget(self)
        self.grid.setObjectName('brushPresetTiles')
        self.grid.setViewMode(QListView.IconMode)
        self.grid.setFlow(QListView.LeftToRight)
        self.grid.setWrapping(True)
        self.grid.setResizeMode(QListView.Adjust)
        self.grid.setMovement(QListView.Static)
        self.grid.setGridSize(TILE_SIZE)
        self.grid.setSpacing(4)
        self.grid.setUniformItemSizes(True)
        self.grid.setWordWrap(True)
        self.grid.setMouseTracking(True)
        self.grid.setVerticalScrollMode(QListView.ScrollPerPixel)
        self.grid.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.grid.setItemDelegate(BrushTileDelegate(self.grid))
        self.grid.viewport().installEventFilter(self)
        self.grid.itemClicked.connect(self._activate)
        self.grid.itemActivated.connect(self._activate)
        layout.addWidget(self.grid, 1)
        self.search.textChanged.connect(self._filter)
        self.search.returnPressed.connect(self._activate_current)

    def refresh(self):
        selected = self.combo.currentData()
        self.grid.clear()
        for index in range(self.combo.count()):
            identifier = self.combo.itemData(index)
            item = QListWidgetItem(self.combo.itemIcon(index), self.combo.itemText(index))
            item.setData(Qt.UserRole, index)
            item.setData(ERROR_ROLE, self.combo.thumbnails.error(identifier))
            item.setToolTip(self.combo.itemText(index) +
                            ('\n' + self.combo.thumbnails.error(identifier) if self.combo.thumbnails.error(identifier) else ''))
            self.grid.addItem(item)
            if identifier == selected:
                self.grid.setCurrentItem(item)
        self._filter()

    def thumbnail_changed(self, key):
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            identifier = self.combo.itemData(item.data(Qt.UserRole))
            if self.combo.thumbnails.keys.get(identifier) == key:
                item.setIcon(self.combo.thumbnails.icon(identifier))
                error = self.combo.thumbnails.error(identifier)
                item.setData(ERROR_ROLE, error)
                item.setToolTip(item.text() + ('\n' + error if error else ''))

    def open(self):
        self.search.clear()
        self.refresh()
        available = self.combo.screen().availableGeometry()
        width = min(960, available.width()-32)
        height = min(680, available.height()-48)
        point = self.combo.mapToGlobal(QPoint(0, self.combo.height()+4))
        x = max(available.left()+8, min(point.x(), available.right()-width-7))
        y = max(available.top()+8, min(point.y(), available.bottom()-height-7))
        self.setGeometry(x, y, width, height)
        self.show()
        self._fit_columns()
        if self.grid.currentItem() is not None:
            self.grid.scrollToItem(self.grid.currentItem())
        self.grid.setFocus()

    def _filter(self):
        query = self.search.text().strip().casefold()
        visible = 0
        for i in range(self.grid.count()):
            item = self.grid.item(i)
            show = query in item.text().casefold()
            item.setHidden(not show)
            visible += int(show)
        current = self.grid.currentItem()
        if current is None or current.isHidden():
            self.grid.setCurrentItem(next((self.grid.item(i) for i in range(self.grid.count())
                                          if not self.grid.item(i).isHidden()), None))
        self.count.setText(f'{visible} brushes' if visible == self.grid.count()
                           else f'{visible} / {self.grid.count()}')

    def _activate(self, item):
        if not self.isVisible() or item is None or item.isHidden():
            return
        index = item.data(Qt.UserRole)
        self.hide()
        self.chosen.emit(index)

    def _activate_current(self):
        self._activate(self.grid.currentItem())

    def eventFilter(self, watched, event):
        if watched is self.grid.viewport() and event.type() == QEvent.Resize:
            self._fit_columns()
        if watched is self.search and event.type() == QEvent.KeyPress and event.key() == Qt.Key_Down:
            self.grid.setFocus()
            return True
        return super().eventFilter(watched, event)

    def _fit_columns(self):
        available = self.grid.viewport().width()
        columns = max(1, available // TILE_SIZE.width())
        size = QSize(max(TILE_SIZE.width(), available // columns), TILE_SIZE.height())
        if self.grid.gridSize() != size:
            self.grid.setGridSize(size)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self.hide()
            event.accept()
        else:
            super().keyPressEvent(event)

    def hideEvent(self, event):
        self.closed.emit()
        super().hideEvent(event)
