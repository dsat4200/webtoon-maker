"""Reusable controls for chapter-local tone masks."""
from __future__ import annotations

import json

from PySide6.QtCore import QPointF, QRectF, QSize, Qt, Signal
from PySide6.QtGui import QColor, QMouseEvent, QPainter, QPen
from PySide6.QtWidgets import (
    QFrame, QGridLayout, QHBoxLayout, QLabel, QPushButton, QScrollArea,
    QSizePolicy, QToolButton, QVBoxLayout, QWidget,
)

from comic_editor.ui.icons import iconoir, iconoir_tinted
from comic_editor.ui.tree_model import HierarchyModel


class MaskButton(QToolButton):
    """Contrast button that also accepts hierarchy drag payloads."""

    hoverChanged = Signal(bool)
    entitiesDropped = Signal(object)
    detachRequested = Signal()

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self.setIcon(iconoir("contrast-circle"))
        self.setCheckable(True)
        self.setAutoRaise(True)
        self.setFixedSize(24, 24)
        self.setAcceptDrops(True)
        self.setToolTip(
            "Attach or edit a tone mask. Right-click an assigned mask to detach."
        )
        self._update_style()
        self.toggled.connect(lambda _checked: self._update_style())

    def _update_style(self) -> None:
        self.setIcon(iconoir_tinted(
            "contrast-circle", "#ff8a24" if self.isChecked() else "#a6a6aa"
        ))
        self.setStyleSheet(
            "QToolButton { color: #ff8a24; border: 1px solid #ff8a24; "
            "border-radius: 3px; background: #4a2b14; }"
            if self.isChecked() else
            "QToolButton { color: #a6a6aa; border: 1px solid #55585f; "
            "border-radius: 3px; background: #292b30; }"
        )

    def enterEvent(self, event) -> None:  # noqa: N802
        self.hoverChanged.emit(True)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: N802
        self.hoverChanged.emit(False)
        super().leaveEvent(event)

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() == Qt.RightButton and self.isChecked():
            self.detachRequested.emit()
            event.accept()
            return
        super().mousePressEvent(event)

    @staticmethod
    def _entities(mime) -> list[tuple[str, str]]:
        if not mime.hasFormat(HierarchyModel.MIME):
            return []
        try:
            raw = json.loads(bytes(mime.data(HierarchyModel.MIME)).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return []
        values = raw.get("entities") if isinstance(raw, dict) else None
        if not isinstance(values, list):
            values = [raw]
        result: list[tuple[str, str]] = []
        for item in values:
            if isinstance(item, dict) and item.get("kind") and item.get("id"):
                target = str(item["kind"]), str(item["id"])
                if target not in result:
                    result.append(target)
        return result

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if self._entities(event.mimeData()):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event) -> None:  # noqa: N802
        entities = self._entities(event.mimeData())
        if entities:
            self.entitiesDropped.emit(entities)
            event.acceptProposedAction()
        else:
            event.ignore()


class DualEndpointSlider(QWidget):
    """One slider track with independently draggable Black and White handles."""

    valuesChanging = Signal(float, float)
    valuesCommitted = Signal(float, float)

    def __init__(
        self, minimum: float, maximum: float, black: float, white: float,
        parent: QWidget | None = None,
    ):
        super().__init__(parent)
        self.minimum = float(minimum)
        self.maximum = float(maximum)
        self.black = float(black)
        self.white = float(white)
        self._active = ""
        self.setMinimumHeight(34)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self.setToolTip("Black and White mask endpoint values; handles may cross")

    def _track(self) -> QRectF:
        return QRectF(9, 8, max(1.0, self.width() - 18.0), 7)

    def _position(self, value: float) -> float:
        ratio = (value - self.minimum) / max(1e-9, self.maximum - self.minimum)
        return self._track().left() + max(0.0, min(1.0, ratio)) * self._track().width()

    def _value(self, x: float) -> float:
        ratio = (x - self._track().left()) / max(1.0, self._track().width())
        return self.minimum + max(0.0, min(1.0, ratio)) * (
            self.maximum - self.minimum
        )

    def setValues(self, black: float, white: float) -> None:  # noqa: N802
        self.black = max(self.minimum, min(self.maximum, float(black)))
        self.white = max(self.minimum, min(self.maximum, float(white)))
        self.update()

    def paintEvent(self, _event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        track = self._track()
        painter.fillRect(track, QColor("#555860"))
        left, right = sorted((self._position(self.black), self._position(self.white)))
        painter.fillRect(QRectF(left, track.top(), right - left, track.height()), QColor("#dd741d"))
        for name, value, fill, text in (
            ("black", self.black, QColor("#111111"), "B"),
            ("white", self.white, QColor("#f4f4f4"), "W"),
        ):
            x = self._position(value)
            center = QPointF(x, 11.5)
            painter.setPen(QPen(QColor("#ff8a24") if self._active == name else QColor("#111111"), 1.5))
            painter.setBrush(fill)
            painter.drawEllipse(center, 6, 6)
            painter.setPen(QColor("#cccccc"))
            painter.drawText(QRectF(x - 18, 20, 36, 13), Qt.AlignCenter, f"{text} {value:g}")

    def mousePressEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if event.button() != Qt.LeftButton:
            return super().mousePressEvent(event)
        x = event.position().x()
        self._active = (
            "black" if abs(x - self._position(self.black))
            <= abs(x - self._position(self.white)) else "white"
        )
        self._move(x)
        event.accept()

    def mouseMoveEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._active and event.buttons() & Qt.LeftButton:
            self._move(event.position().x())
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:  # noqa: N802
        if self._active:
            self._move(event.position().x())
            self._active = ""
            self.update()
            self.valuesCommitted.emit(self.black, self.white)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def _move(self, x: float) -> None:
        value = self._value(x)
        if self._active == "black":
            self.black = value
        else:
            self.white = value
        self.valuesChanging.emit(self.black, self.white)
        self.update()


class MaskTile(QToolButton):
    def __init__(self, mask_id: str, name: str, parent=None):
        super().__init__(parent)
        self.mask_id = mask_id
        self.setText(name)
        self.setCheckable(True)
        self.setToolButtonStyle(Qt.ToolButtonTextUnderIcon)
        self.setFixedSize(92, 92)
        self.setStyleSheet(
            "QToolButton { border: 1px solid #51545b; }"
            "QToolButton:checked { border: 2px solid #ff8a24; }"
        )


class MasksPanel(QWidget):
    """Saved-mask browser. Mutations are coordinated by MainWindow."""

    newRequested = Signal()
    saveCurrentRequested = Signal()
    renameRequested = Signal()
    deleteRequested = Signal()
    maskSelected = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(3, 3, 3, 3)
        row = QHBoxLayout()
        self.new_button = QPushButton("New", self)
        self.save_button = QPushButton("Save Current", self)
        self.rename_button = QPushButton("Rename", self)
        self.delete_button = QPushButton("Delete", self)
        for control in (
            self.new_button, self.save_button,
            self.rename_button, self.delete_button,
        ):
            row.addWidget(control)
        outer.addLayout(row)
        self.status = QLabel("No masks in this chapter.", self)
        self.status.setWordWrap(True)
        outer.addWidget(self.status)
        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.host = QWidget(self.scroll)
        self.grid = QGridLayout(self.host)
        self.grid.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self.scroll.setWidget(self.host)
        outer.addWidget(self.scroll, 1)
        self.new_button.clicked.connect(self.newRequested)
        self.save_button.clicked.connect(self.saveCurrentRequested)
        self.rename_button.clicked.connect(self.renameRequested)
        self.delete_button.clicked.connect(self.deleteRequested)
        self._buttons: dict[str, MaskTile] = {}
        self.active_mask_id = ""

    def refresh(self, chapter, thumbnails: dict[str, object] | None = None) -> None:
        while self.grid.count():
            item = self.grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        self._buttons.clear()
        masks = [mask for mask in chapter.masks.values() if mask.saved] if chapter else []
        masks.sort(key=lambda item: (item.name.casefold(), item.mask_id))
        for index, mask in enumerate(masks):
            button = MaskTile(mask.mask_id, mask.name, self.host)
            if thumbnails and mask.mask_id in thumbnails:
                from PySide6.QtGui import QIcon, QPixmap
                button.setIcon(QIcon(QPixmap.fromImage(thumbnails[mask.mask_id])))
                button.setIconSize(QSize(62, 62))
            button.setChecked(mask.mask_id == self.active_mask_id)
            button.clicked.connect(
                lambda _checked=False, mask_id=mask.mask_id:
                self.maskSelected.emit(mask_id)
            )
            self.grid.addWidget(button, index // 3, index % 3)
            self._buttons[mask.mask_id] = button
        self.status.setText(
            "Select a mask to edit its contributors and paint."
            if masks else "No saved masks in this chapter."
        )
        active = chapter.masks.get(self.active_mask_id) if chapter else None
        self.save_button.setEnabled(bool(active and not active.saved))
        self.rename_button.setEnabled(bool(active and active.saved))
        self.delete_button.setEnabled(bool(active and active.saved))

    def set_active(self, mask_id: str) -> None:
        self.active_mask_id = mask_id
        for current_id, button in self._buttons.items():
            button.setChecked(current_id == mask_id)
