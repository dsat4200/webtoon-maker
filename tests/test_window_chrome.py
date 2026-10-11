from __future__ import annotations

import ctypes
from ctypes import wintypes
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMainWindow, QToolBar, QWidget

from comic_editor.ui.window_chrome import install_window_chrome
from comic_editor.ui import window_chrome


def _window(qapp):
    window = QMainWindow()
    window.setWindowTitle("Webtoon Maker — Test series")
    toolbar = QToolBar("Project", window)
    window.addToolBar(toolbar)
    actions = [toolbar.addAction(f"Command {index}") for index in range(12)]
    window.setCentralWidget(QWidget(window))
    controller = install_window_chrome(window, toolbar)
    window.resize(900, 400)
    window.show()
    qapp.processEvents()
    return window, toolbar, actions, controller


def test_controls_remain_in_toolbar_when_commands_overflow(qapp):
    window, toolbar, actions, chrome = _window(qapp)
    try:
        window.resize(480, 400)
        qapp.processEvents()
        assert window.windowFlags() & Qt.WindowType.FramelessWindowHint
        assert toolbar.actions()[1:] == actions
        assert chrome.controls.isVisibleTo(window)
        assert chrome.controls.geometry().right() < toolbar.width()
        assert chrome.controls.geometry().left() >= toolbar.contentsRect().right()
        assert not any(button.defaultAction() in toolbar.actions() for button in (
            chrome.minimize, chrome.maximize, chrome.close,
        ))
        assert chrome.title.isHidden()
        assert "Test series" in chrome.logo.toolTip()
    finally:
        window.deleteLater()


def test_maximize_restore_and_fullscreen_chrome(qapp):
    window, toolbar, _actions, chrome = _window(qapp)
    try:
        QTest.mouseClick(chrome.maximize, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert window.isMaximized()
        assert chrome.maximize.toolTip() == "Restore window"
        QTest.mouseClick(chrome.maximize, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert not window.isMaximized()
        assert chrome.maximize.toolTip() == "Maximize window"

        window.showFullScreen()
        qapp.processEvents()
        assert chrome.controls.isHidden()
        assert toolbar.contentsMargins().right() == chrome._original_margins.right()
        window.showNormal()
        qapp.processEvents()
        assert chrome.controls.isVisibleTo(window)
    finally:
        window.deleteLater()


def test_brand_double_click_and_resize_hit_regions(qapp):
    window, _toolbar, _actions, chrome = _window(qapp)
    try:
        assert chrome._resize_edges(QPoint(1, 1)) == (Qt.Edge.LeftEdge | Qt.Edge.TopEdge)
        assert chrome._resize_edges(QPoint(window.width() - 1, 80)) == Qt.Edge.RightEdge
        assert not chrome._resize_edges(QPoint(100, 100))
        QTest.mouseDClick(chrome.logo, Qt.MouseButton.LeftButton)
        qapp.processEvents()
        assert window.isMaximized()
        assert not chrome._resize_edges(QPoint(1, 1))
    finally:
        window.deleteLater()


def test_close_controls_and_live_window_title(qapp):
    window, _toolbar, _actions, chrome = _window(qapp)
    try:
        window.setWindowTitle("Renamed series")
        qapp.processEvents()
        assert chrome.title.text() == "Renamed series"
        assert "Renamed series" in chrome.logo.toolTip()
        assert chrome.close.accessibleName() == "Close window"
        assert chrome.minimize.accessibleName() == "Minimize window"
        QTest.mouseClick(chrome.close, Qt.MouseButton.LeftButton)
        assert not window.isVisible()
    finally:
        window.deleteLater()


def _native_api(monkeypatch, chrome):
    """Exercise native MSG routing without moving the real desktop pointer."""
    calls = []

    def get_rect(hwnd, pointer):
        rect = ctypes.cast(pointer, ctypes.POINTER(wintypes.RECT)).contents
        rect.left, rect.top = -400, -200
        rect.right, rect.bottom = -400 + chrome.window.width(), -200 + chrome.window.height()
        return True

    def client_to_screen(hwnd, pointer):
        point = ctypes.cast(pointer, ctypes.POINTER(wintypes.POINT)).contents
        point.x -= 400
        point.y -= 200
        return True

    def capture(hwnd):
        calls.append("capture")
        return 0

    def release():
        calls.append("release")
        return True

    def track(pointer):
        calls.append("track")
        return True

    api = SimpleNamespace(
        GetWindowRect=get_rect, ClientToScreen=client_to_screen,
        SetCapture=capture, ReleaseCapture=release, TrackMouseEvent=track,
    )
    monkeypatch.setattr(window_chrome, "sys", SimpleNamespace(platform="win32"))
    monkeypatch.setattr(window_chrome, "QGuiApplication", SimpleNamespace(platformName=lambda: "windows"))
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(user32=api), raising=False)
    monkeypatch.setattr(chrome, "_configure_native_window", lambda: None)
    return calls


def _native_message(chrome, message, position=None, *, client=False, hit=9):
    if position is None:
        position = chrome.maximize.mapTo(chrome.window, chrome.maximize.rect().center())
    x, y = position.x(), position.y()
    if not client:
        x -= 400
        y -= 200
    msg = wintypes.MSG()
    msg.hWnd = int(chrome.window.winId())
    msg.message, msg.wParam = message, hit
    msg.lParam = (x & 0xffff) | ((y & 0xffff) << 16)
    return chrome.native_event(b"windows_generic_MSG", ctypes.addressof(msg))


def test_native_maximize_hit_enables_snap_layouts_and_preserves_edges(qapp, monkeypatch):
    window, _toolbar, _actions, chrome = _window(qapp)
    try:
        calls = _native_api(monkeypatch, chrome)
        assert _native_message(chrome, 0x0084) == (True, 9)
        assert _native_message(chrome, 0x0084, QPoint(1, 1)) == (True, 13)
        assert _native_message(chrome, 0x00A0) == (False, 0)
        assert chrome.maximize.underMouse()
        assert calls == ["track"]
        _native_message(chrome, 0x02A2)
        assert not chrome.maximize.underMouse()
    finally:
        window.deleteLater()


@pytest.mark.parametrize("release_message,client", [(0x00A2, False), (0x0202, True)])
def test_native_maximize_click_runs_once_and_restores(qapp, monkeypatch, release_message, client):
    window, _toolbar, _actions, chrome = _window(qapp)
    try:
        calls = _native_api(monkeypatch, chrome)
        clicked = []
        chrome.maximize.clicked.connect(lambda: clicked.append(True))
        for maximized in (True, False):
            assert _native_message(chrome, 0x00A1) == (True, 0)
            assert chrome.maximize.isDown()
            assert _native_message(chrome, release_message, client=client) == (True, 0)
            assert window.isMaximized() is maximized
            assert not chrome.maximize.isDown()
            assert not chrome._native_maximize_pressed
        assert clicked == [True, True]
        assert calls == ["capture", "release", "capture", "release"]
    finally:
        window.deleteLater()


def test_native_maximize_release_outside_and_capture_loss_cancel(qapp, monkeypatch):
    window, _toolbar, _actions, chrome = _window(qapp)
    try:
        calls = _native_api(monkeypatch, chrome)
        assert _native_message(chrome, 0x00A1) == (True, 0)
        assert _native_message(chrome, 0x0200, QPoint(100, 100), client=True) == (True, 0)
        assert not chrome.maximize.isDown()
        assert _native_message(chrome, 0x0202, QPoint(100, 100), client=True) == (True, 0)
        assert not window.isMaximized()
        assert calls == ["capture", "release"]
        _native_message(chrome, 0x00A1)
        _native_message(chrome, 0x0215)
        assert not chrome._native_maximize_pressed
        assert not chrome.maximize.isDown()
        assert calls == ["capture", "release", "capture"]
        assert _native_message(chrome, 0x00A2) == (False, 0)
        assert not window.isMaximized()
    finally:
        window.deleteLater()
