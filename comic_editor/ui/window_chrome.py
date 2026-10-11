"""Window controls hosted in the existing Project toolbar."""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import sys

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, QSize, Qt
from PySide6.QtGui import QColor, QGuiApplication, QIcon, QKeySequence, QPainter, QPen, QShortcut
from PySide6.QtWidgets import (
    QApplication, QHBoxLayout, QLabel, QStyle, QStyleOptionToolButton,
    QToolBar, QToolButton, QWidget,
)

from comic_editor.ui.icons import iconoir


_ASSET = Path(__file__).with_name("assets") / "webtoon-maker.png"
_CONTROL_WIDTH = 108
_RESIZE_MARGIN = 6


class _TrackMouseEvent(ctypes.Structure):
    _fields_ = (
        ("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
        ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD),
    )


def _text_color() -> QColor:
    from comic_editor.ui.themes import theme_color
    return QColor(theme_color("text", "#E6E6E8"))


class WindowControlButton(QToolButton):
    """Crisp titlebar glyphs with ordinary Qt button interaction."""

    def __init__(self, kind: str, parent: QWidget):
        super().__init__(parent)
        self.kind = kind
        self.maximized = False
        self.setObjectName(f"window{kind.title()}")
        self.setProperty("windowControl", True)
        self.setFixedSize(36, 30)
        self.setAutoRaise(True)
        self.setFocusPolicy(Qt.FocusPolicy.TabFocus)
        self._sync_label()

    def _sync_label(self) -> None:
        label = "Restore window" if self.kind == "maximize" and self.maximized else {
            "minimize": "Minimize window", "maximize": "Maximize window",
            "close": "Close window",
        }[self.kind]
        self.setToolTip(label)
        self.setAccessibleName(label)

    def set_maximized(self, maximized: bool) -> None:
        self.maximized = maximized
        self._sync_label()
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        option = QStyleOptionToolButton()
        self.initStyleOption(option)
        painter = QPainter(self)
        self.style().drawComplexControl(QStyle.ComplexControl.CC_ToolButton, option, painter, self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setPen(QPen(_text_color(), 1.4))
        x = self.width() / 2
        y = self.height() / 2
        if self.kind == "minimize":
            painter.drawLine(QPoint(round(x - 5), round(y + 3)), QPoint(round(x + 5), round(y + 3)))
        elif self.kind == "close":
            painter.drawLine(QPoint(round(x - 4), round(y - 4)), QPoint(round(x + 4), round(y + 4)))
            painter.drawLine(QPoint(round(x + 4), round(y - 4)), QPoint(round(x - 4), round(y + 4)))
        elif self.maximized:
            painter.drawPolyline([
                QPoint(round(x - 2), round(y - 5)), QPoint(round(x + 5), round(y - 5)),
                QPoint(round(x + 5), round(y + 2)),
            ])
            painter.drawRect(QRect(round(x - 5), round(y - 2), 7, 7))
        else:
            painter.drawRect(QRect(round(x - 5), round(y - 5), 10, 10))
        painter.end()


class WindowChromeController(QObject):
    """Keep chrome visible when toolbar commands overflow, with native edges."""

    def __init__(self, window: QWidget, toolbar: QToolBar):
        super().__init__(window)
        self.window = window
        self.toolbar = toolbar
        self._original_margins = toolbar.contentsMargins()
        self._native_handle = None
        self._native_maximize_pressed = False
        window.setWindowFlag(Qt.WindowType.FramelessWindowHint, True)
        toolbar.setMovable(False)
        toolbar.setFloatable(False)

        self.brand = QWidget(toolbar)
        self.brand.setObjectName("windowBrand")
        brand_layout = QHBoxLayout(self.brand)
        brand_layout.setContentsMargins(2, 0, 6, 0)
        brand_layout.setSpacing(6)
        self.logo = QToolButton(self.brand)
        self.logo.setObjectName("windowLogo")
        self.logo.setAutoRaise(True)
        self.logo.setFixedSize(28, 28)
        self.logo.setIconSize(QSize(24, 24))
        self.logo.setIcon(QIcon(str(_ASSET)) if _ASSET.is_file() else iconoir("text-square"))
        self.logo.setAccessibleName("Webtoon Maker window")
        self.logo.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        brand_layout.addWidget(self.logo)
        self.title = QLabel(self.brand)
        self.title.setObjectName("windowTitle")
        self.title.setMaximumWidth(180)
        self.title.hide()
        brand_layout.addWidget(self.title)
        first_action = toolbar.actions()[0] if toolbar.actions() else None
        self.brand_action = toolbar.insertWidget(first_action, self.brand)

        # A child overlay and reserved contents margin keep these controls out
        # of QToolBar's overflow menu without introducing another toolbar row.
        self.controls = QWidget(toolbar)
        self.controls.setObjectName("windowControls")
        self.controls.setFixedSize(_CONTROL_WIDTH, 30)
        layout = QHBoxLayout(self.controls)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        self.minimize = WindowControlButton("minimize", self.controls)
        self.maximize = WindowControlButton("maximize", self.controls)
        self.close = WindowControlButton("close", self.controls)
        for button in (self.minimize, self.maximize, self.close):
            layout.addWidget(button)
        self.minimize.clicked.connect(window.showMinimized)
        self.maximize.clicked.connect(self.toggle_maximized)
        self.close.clicked.connect(window.close)
        self.close_shortcut = QShortcut(QKeySequence("Alt+F4"), window)
        self.close_shortcut.activated.connect(window.close)
        self._sync_title()
        self._sync_state()
        toolbar.installEventFilter(self)
        window.installEventFilter(self)
        QApplication.instance().installEventFilter(self)

    def toggle_maximized(self) -> None:
        if self.window.isMaximized():
            self.window.showNormal()
        else:
            self.window.showMaximized()

    def _sync_title(self) -> None:
        title = self.window.windowTitle() or "Webtoon Maker"
        self.title.setText(title)
        self.title.setToolTip(title)
        self.logo.setToolTip(title + " — drag to move; double-click to maximize")

    def _sync_state(self) -> None:
        fullscreen = self.window.isFullScreen()
        self.controls.setVisible(not fullscreen)
        self.maximize.set_maximized(self.window.isMaximized())
        margin = self._original_margins
        self.toolbar.setContentsMargins(
            margin.left(), margin.top(),
            margin.right() + (0 if fullscreen else _CONTROL_WIDTH), margin.bottom(),
        )
        self._position_controls()

    def _position_controls(self) -> None:
        margin = self._original_margins
        self.controls.move(
            max(0, self.toolbar.width() - margin.right() - _CONTROL_WIDTH),
            max(0, (self.toolbar.height() - self.controls.height()) // 2),
        )
        self.controls.raise_()
        # Preserve command space. Show the title only when all command widths
        # and the window controls fit with a comfortable extra title slot.
        used_width = sum(
            self.toolbar.widgetForAction(action).sizeHint().width()
            if self.toolbar.widgetForAction(action) is not None else 8
            for action in self.toolbar.actions() if action is not self.brand_action
        )
        show_title = self.toolbar.width() >= used_width + _CONTROL_WIDTH + 250
        if self.title.isHidden() == show_title:
            self.title.setVisible(show_title)

    def _drag_surface(self, widget: QObject) -> bool:
        return widget in (self.toolbar, self.brand, self.logo, self.title)

    def _resize_edges(self, position: QPoint) -> Qt.Edge:
        if self.window.isMaximized() or self.window.isFullScreen():
            return Qt.Edge(0)
        edges = Qt.Edge(0)
        if position.x() < _RESIZE_MARGIN:
            edges |= Qt.Edge.LeftEdge
        elif position.x() >= self.window.width() - _RESIZE_MARGIN:
            edges |= Qt.Edge.RightEdge
        if position.y() < _RESIZE_MARGIN:
            edges |= Qt.Edge.TopEdge
        elif position.y() >= self.window.height() - _RESIZE_MARGIN:
            edges |= Qt.Edge.BottomEdge
        return edges

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        kind = event.type()
        if watched is self.window:
            if kind == QEvent.Type.WindowStateChange:
                self._sync_state()
            elif kind == QEvent.Type.WindowTitleChange:
                self._sync_title()
            elif kind in (QEvent.Type.Show, QEvent.Type.WinIdChange):
                self._configure_native_window()
            elif kind == QEvent.Type.WindowDeactivate and self._native_maximize_pressed:
                self._cancel_native_maximize()
        if watched is self.toolbar and kind in (QEvent.Type.Resize, QEvent.Type.LayoutRequest):
            self._position_controls()
        if kind not in (QEvent.Type.MouseButtonPress, QEvent.Type.MouseButtonDblClick):
            return False
        if not isinstance(watched, QWidget) or watched.window() is not self.window:
            return False
        if event.button() != Qt.MouseButton.LeftButton:
            return False
        if kind == QEvent.Type.MouseButtonDblClick and self._drag_surface(watched):
            self.toggle_maximized()
            return True
        if kind == QEvent.Type.MouseButtonPress:
            handle = self.window.windowHandle()
            if handle is None:
                return False
            edges = self._resize_edges(self.window.mapFromGlobal(event.globalPosition().toPoint()))
            if edges and handle.startSystemResize(edges):
                return True
            if self._drag_surface(watched) and handle.startSystemMove():
                return True
        return False

    def _configure_native_window(self) -> None:
        if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return
        hwnd = int(self.window.winId())
        if hwnd == self._native_handle:
            return
        self._native_handle = hwnd
        user32 = ctypes.windll.user32
        get_style = user32.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.GetWindowLongW
        set_style = user32.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8 else user32.SetWindowLongW
        get_style.argtypes = (wintypes.HWND, ctypes.c_int)
        get_style.restype = ctypes.c_ssize_t
        set_style.argtypes = (wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t)
        set_style.restype = ctypes.c_ssize_t
        # Native sizing and maximize capabilities retain Win+Arrow snapping
        # and the standard system window menu on a frameless Qt window.
        style = get_style(hwnd, -16)
        set_style(hwnd, -16, style | 0x00040000 | 0x00020000 | 0x00010000)

    def _native_position(self, msg, *, client: bool = False) -> QPoint | None:
        user32 = ctypes.windll.user32
        rectangle = wintypes.RECT()
        user32.GetWindowRect.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.RECT))
        user32.GetWindowRect.restype = wintypes.BOOL
        if not user32.GetWindowRect(msg.hWnd, ctypes.byref(rectangle)):
            return None
        screen_x = ctypes.c_short(msg.lParam & 0xffff).value
        screen_y = ctypes.c_short((msg.lParam >> 16) & 0xffff).value
        if client:
            point = wintypes.POINT(screen_x, screen_y)
            user32.ClientToScreen.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.POINT))
            user32.ClientToScreen.restype = wintypes.BOOL
            if not user32.ClientToScreen(msg.hWnd, ctypes.byref(point)):
                return None
            screen_x, screen_y = point.x, point.y
        scale = self.window.devicePixelRatioF()
        return QPoint(
            round((screen_x - rectangle.left) / scale),
            round((screen_y - rectangle.top) / scale),
        )

    def _on_maximize(self, position: QPoint) -> bool:
        return bool(
            self.controls.isVisibleTo(self.window) and self.maximize.isEnabled()
            and self.maximize.rect().contains(self.maximize.mapFrom(self.window, position))
        )

    def _maximize_hover(self, hovered: bool) -> None:
        self.maximize.setAttribute(Qt.WidgetAttribute.WA_UnderMouse, hovered)
        self.maximize.update()

    def _cancel_native_maximize(self, *, release_capture: bool = True) -> None:
        pressed = self._native_maximize_pressed
        self._native_maximize_pressed = False
        self.maximize.setDown(False)
        if pressed and release_capture:
            user32 = ctypes.windll.user32
            user32.ReleaseCapture.argtypes = ()
            user32.ReleaseCapture.restype = wintypes.BOOL
            user32.ReleaseCapture()

    def native_event(self, event_type, message) -> tuple[bool, int]:
        """Called by the owning QMainWindow before its other native handlers."""
        if sys.platform != "win32" or QGuiApplication.platformName() != "windows":
            return False, 0
        msg = wintypes.MSG.from_address(int(message))
        if msg.message in (0x0215, 0x001F):  # WM_CAPTURECHANGED, WM_CANCELMODE
            self._cancel_native_maximize(release_capture=msg.message != 0x0215)
            self._maximize_hover(False)
            return False, 0
        if msg.message == 0x02A2:  # WM_NCMOUSELEAVE
            self._maximize_hover(False)
            return False, 0
        native_mouse = (0x00A0, 0x00A1, 0x00A2, 0x00A3)
        captured_mouse = (0x0200, 0x0202)
        if msg.message != 0x0084 and msg.message not in native_mouse and not (
            self._native_maximize_pressed and msg.message in captured_mouse
        ):
            return False, 0
        position = self._native_position(msg, client=msg.message in captured_mouse)
        if position is None:
            return False, 0
        on_maximize = self._on_maximize(position)
        if msg.message in (0x00A0, 0x0200):  # WM_NCMOUSEMOVE, captured WM_MOUSEMOVE
            self._maximize_hover(on_maximize)
            if self._native_maximize_pressed:
                self.maximize.setDown(on_maximize)
                return True, 0
            if on_maximize:
                # Returning HTMAXBUTTON enables Windows 11 Snap Layouts;
                # native hover messages still need to paint our Qt button.
                tracking = _TrackMouseEvent(ctypes.sizeof(_TrackMouseEvent), 0x12, msg.hWnd, 0)
                track = ctypes.windll.user32.TrackMouseEvent
                track.argtypes = (ctypes.POINTER(_TrackMouseEvent),)
                track.restype = wintypes.BOOL
                track(ctypes.byref(tracking))  # TME_LEAVE | TME_NONCLIENT
            return False, 0
        if msg.message in (0x00A1, 0x00A3) and msg.wParam == 9 and on_maximize:
            # Own the click once HTMAXBUTTON routes it to the nonclient lane.
            # Capture makes release outside the button cancel it reliably.
            self._native_maximize_pressed = True
            self.maximize.setDown(True)
            capture = ctypes.windll.user32.SetCapture
            capture.argtypes = (wintypes.HWND,)
            capture.restype = wintypes.HWND
            capture(msg.hWnd)
            return True, 0
        if msg.message in (0x00A2, 0x0202) and self._native_maximize_pressed:
            self._cancel_native_maximize()
            if on_maximize:
                self.maximize.click()
            return True, 0
        if msg.message != 0x0084:  # WM_NCHITTEST
            return False, 0
        edges = self._resize_edges(position)
        hit = {
            Qt.Edge.LeftEdge: 10, Qt.Edge.RightEdge: 11,
            Qt.Edge.TopEdge: 12, Qt.Edge.BottomEdge: 15,
            Qt.Edge.TopEdge | Qt.Edge.LeftEdge: 13,
            Qt.Edge.TopEdge | Qt.Edge.RightEdge: 14,
            Qt.Edge.BottomEdge | Qt.Edge.LeftEdge: 16,
            Qt.Edge.BottomEdge | Qt.Edge.RightEdge: 17,
        }.get(edges)
        if hit is not None:
            return True, hit
        if on_maximize:
            return True, 9  # HTMAXBUTTON: native Windows 11 Snap Layout hover
        # Native caption dragging retains the operating system's snap behavior.
        hovered = self.window.childAt(position)
        if self._drag_surface(hovered):
            return True, 2  # HTCAPTION
        return False, 0


def install_window_chrome(window: QWidget, toolbar: QToolBar) -> WindowChromeController:
    """Add titlebar functions to a stable toolbar without moving its commands."""
    return WindowChromeController(window, toolbar)
