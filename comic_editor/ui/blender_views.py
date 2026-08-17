"""Ribbon browser for Blender-provided Comic Views."""
from __future__ import annotations

from PySide6.QtCore import QSize, Qt, Signal
from PySide6.QtGui import QIcon, QPixmap
from PySide6.QtWidgets import (
    QFormLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget, QListWidgetItem,
    QPushButton, QSpinBox, QVBoxLayout, QWidget,
)

from comic_editor.integrations.blender_source import ComicViewInfo


class BlenderViewsWidget(QWidget):
    connectRequested = Signal(str, int, str)
    disconnectRequested = Signal()
    refreshRequested = Signal()
    addRequested = Signal(object)

    def __init__(self, parent: QWidget | None = None):
        super().__init__(parent)
        self._views: dict[str, ComicViewInfo] = {}
        self._relinking = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.setSpacing(5)

        connection = QWidget(self)
        form = QFormLayout(connection)
        form.setContentsMargins(0, 0, 0, 0)
        self.host = QLineEdit("127.0.0.1", connection)
        self.port = QSpinBox(connection)
        self.port.setRange(1024, 65535)
        self.port.setValue(47837)
        self.token = QLineEdit(connection)
        self.token.setPlaceholderText("Token shown in Blender")
        form.addRow("Host", self.host)
        form.addRow("Port", self.port)
        form.addRow("Token", self.token)
        layout.addWidget(connection)

        buttons = QHBoxLayout()
        self.connect_button = QPushButton("Connect", self)
        self.disconnect_button = QPushButton("Disconnect", self)
        self.refresh_button = QPushButton("Refresh", self)
        buttons.addWidget(self.connect_button)
        buttons.addWidget(self.disconnect_button)
        buttons.addWidget(self.refresh_button)
        layout.addLayout(buttons)
        self.status = QLabel("Disconnected", self)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)

        self.list = QListWidget(self)
        self.list.setViewMode(QListWidget.IconMode)
        self.list.setResizeMode(QListWidget.Adjust)
        self.list.setMovement(QListWidget.Static)
        self.list.setIconSize(QSize(144, 96))
        self.list.setGridSize(QSize(164, 132))
        self.list.setSpacing(4)
        self.list.setSelectionMode(QListWidget.SingleSelection)
        layout.addWidget(self.list, 1)
        self.add_button = QPushButton("Add Selected View to Canvas", self)
        self.add_button.setEnabled(False)
        layout.addWidget(self.add_button)

        self.connect_button.clicked.connect(lambda: self.connectRequested.emit(
            self.host.text().strip(), self.port.value(), self.token.text().strip()
        ))
        self.disconnect_button.clicked.connect(self.disconnectRequested)
        self.refresh_button.clicked.connect(self.refreshRequested)
        self.add_button.clicked.connect(self._add_selected)
        self.list.itemSelectionChanged.connect(self._selection_changed)
        self.list.itemDoubleClicked.connect(lambda _item: self._add_selected())
        self.set_connection_state("disconnected")

    def set_endpoint(self, host: str, port: int, token: str) -> None:
        self.host.setText(str(host or "127.0.0.1"))
        self.port.setValue(max(1024, min(65535, int(port))))
        self.token.setText(str(token or ""))

    def set_connection_state(self, state: str) -> None:
        labels = {
            "disconnected": "Disconnected — cached Blender images remain available.",
            "connecting": "Connecting to Blender…",
            "connected": "Connected to Blender Comic Views.",
            "error": "Unable to connect. Check Blender's host, port, and token.",
        }
        self.status.setText(labels.get(state, state.title()))
        connected = state == "connected"
        self.connect_button.setEnabled(not connected and state != "connecting")
        self.disconnect_button.setEnabled(connected or state == "connecting")
        self.refresh_button.setEnabled(connected)

    def set_status_message(self, message: str) -> None:
        self.status.setText(str(message))

    def set_relink_mode(self, enabled: bool, name: str = "") -> None:
        self._relinking = bool(enabled)
        self.add_button.setText(
            f"Relink {name or 'Selected Image'} to This View"
            if self._relinking else "Add Selected View to Canvas"
        )

    def set_views(self, views: list[ComicViewInfo]) -> None:
        selected = self.selected_view_uuid()
        self._views = {view.view_uuid: view for view in views}
        self.list.clear()
        for view in views:
            suffix = " • modified" if view.dirty else ""
            item = QListWidgetItem(
                f"{view.name}\n{view.width}×{view.height} • r{view.revision}{suffix}"
            )
            if not view.thumbnail.isNull():
                item.setIcon(QIcon(QPixmap.fromImage(view.thumbnail)))
            item.setData(Qt.UserRole, view.view_uuid)
            item.setToolTip(
                f"{view.name}\nRevision {view.revision}\n"
                f"{view.width} × {view.height} pixels"
            )
            self.list.addItem(item)
            if view.view_uuid == selected:
                item.setSelected(True)
        self._selection_changed()

    def selected_view_uuid(self) -> str:
        item = self.list.currentItem()
        return str(item.data(Qt.UserRole)) if item is not None else ""

    def selected_view(self) -> ComicViewInfo | None:
        return self._views.get(self.selected_view_uuid())

    def _selection_changed(self) -> None:
        self.add_button.setEnabled(self.selected_view() is not None)

    def _add_selected(self) -> None:
        view = self.selected_view()
        if view is not None:
            self.addRequested.emit(view)
