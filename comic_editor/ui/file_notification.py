"""A single reusable, non-modal file confirmation above the drawing canvas."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QEvent, QTimer, Qt, QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QFrame, QHBoxLayout, QLabel, QPushButton, QSizePolicy


class CanvasFileNotification(QFrame):
    def __init__(self, canvas):
        super().__init__(canvas)
        self.setObjectName("canvasFileNotification")
        self.setStyleSheet(
            "#canvasFileNotification { background: #263b31; color: #edf8f0; "
            "border: 1px solid #658872; border-radius: 6px; }"
            "#canvasFileNotification QLabel { color: #edf8f0; }"
        )
        layout = QHBoxLayout(self)
        layout.setContentsMargins(12, 7, 9, 7)
        self.message = QLabel(self)
        self.message.setTextFormat(Qt.TextFormat.PlainText)
        self.message.setWordWrap(True)
        self.message.setMinimumWidth(0)
        self.message.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        layout.addWidget(self.message, 1)
        self.open_button = QPushButton(self)
        self.open_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.open_button.clicked.connect(self._open_location)
        layout.addWidget(self.open_button, 0, Qt.AlignmentFlag.AlignRight)
        self.dismiss_button = QPushButton("×", self)
        self.dismiss_button.setToolTip("Dismiss notification")
        self.dismiss_button.setFixedWidth(26)
        self.dismiss_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.dismiss_button.clicked.connect(self.hide)
        layout.addWidget(self.dismiss_button)
        self.directory: Path | None = None
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.timeout.connect(self.hide)
        canvas.installEventFilter(self)
        self.hide()

    def show_result(self, operation: str, destination: Path) -> None:
        destination = Path(destination).expanduser().resolve()
        self.directory = destination.parent
        self.message.setText(
            f"Successfully {operation} {destination.name} to {self.directory}"
        )
        self.message.setToolTip(self.message.text())
        self.open_button.setText(f"Open {operation} location")
        self.open_button.setToolTip(str(self.directory))
        self._place()
        self.show()
        self.raise_()
        self.timer.start(12000)

    def _place(self) -> None:
        width = max(1, self.parentWidget().width() - 20)
        self.setFixedWidth(width)
        self.layout().activate()
        self.setFixedHeight(max(44, self.layout().totalHeightForWidth(width)))
        self.move(10, 10)

    def _open_location(self) -> None:
        if self.directory is not None:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.directory)))

    def eventFilter(self, watched, event):  # noqa: N802
        if event.type() == QEvent.Type.Resize and not self.isHidden():
            self._place()
        return super().eventFilter(watched, event)

    def enterEvent(self, event):  # noqa: N802
        self.timer.stop()
        super().enterEvent(event)

    def leaveEvent(self, event):  # noqa: N802
        self.timer.start(12000)
        super().leaveEvent(event)
