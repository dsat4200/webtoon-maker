from __future__ import annotations

import ctypes
from ctypes import wintypes

from comic_editor.ui import windows_input
from comic_editor.ui.main_window import MainWindow


class _FakeFunction:
    def __init__(self, result=1):
        self.result = result
        self.calls = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.result


class _FakeUser32:
    def __init__(self):
        self.RegisterTouchWindow = _FakeFunction()
        self.SetPropW = _FakeFunction()
        self.RemovePropW = _FakeFunction()


def _value(argument):
    return getattr(argument, "value", argument)


def test_windows_pen_hover_enables_unfiltered_multitouch(monkeypatch):
    user32 = _FakeUser32()
    monkeypatch.setattr(windows_input.sys, "platform", "win32")
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)

    assert windows_input.configure_simultaneous_pen_touch(1234, True)
    assert _value(user32.RegisterTouchWindow.calls[0][0]) == 1234
    assert user32.RegisterTouchWindow.calls[0][1] == windows_input.TWF_WANTPALM
    assert user32.SetPropW.calls[0][1] == (
        windows_input.MICROSOFT_TABLETPENSERVICE_PROPERTY
    )
    assert _value(user32.SetPropW.calls[0][2]) == (
        windows_input.TABLET_ENABLE_MULTITOUCHDATA
    )


def test_disabling_tablet_navigation_restores_normal_touch_filtering(monkeypatch):
    user32 = _FakeUser32()
    monkeypatch.setattr(windows_input.sys, "platform", "win32")
    monkeypatch.setattr(windows_input, "_load_user32", lambda: user32)

    assert windows_input.configure_simultaneous_pen_touch(5678, False)
    assert user32.RegisterTouchWindow.calls[0][1] == 0
    assert user32.RemovePropW.calls[0][1] == (
        windows_input.MICROSOFT_TABLETPENSERVICE_PROPERTY
    )
    assert not user32.SetPropW.calls


def _native_message(message_type: int):
    message = wintypes.MSG()
    message.message = message_type
    return message, ctypes.addressof(message)


def test_native_tablet_query_enables_multitouch(monkeypatch):
    monkeypatch.setattr(windows_input.sys, "platform", "win32")
    message, address = _native_message(
        windows_input.WM_TABLET_QUERYSYSTEMGESTURESTATUS
    )

    assert windows_input.tablet_multitouch_native_result(
        windows_input.WINDOWS_GENERIC_MSG, address, True,
    ) == windows_input.TABLET_ENABLE_MULTITOUCHDATA
    assert message.message == windows_input.WM_TABLET_QUERYSYSTEMGESTURESTATUS


def test_native_tablet_query_ignores_disabled_malformed_and_unrelated(
    monkeypatch,
):
    monkeypatch.setattr(windows_input.sys, "platform", "win32")
    _query, query_address = _native_message(
        windows_input.WM_TABLET_QUERYSYSTEMGESTURESTATUS
    )
    _other, other_address = _native_message(0x8001)

    assert windows_input.tablet_multitouch_native_result(
        windows_input.WINDOWS_GENERIC_MSG, query_address, False,
    ) is None
    assert windows_input.tablet_multitouch_native_result(
        b"windows_dispatcher_MSG", query_address, True,
    ) is None
    assert windows_input.tablet_multitouch_native_result(
        windows_input.WINDOWS_GENERIC_MSG, other_address, True,
    ) is None
    assert windows_input.tablet_multitouch_native_result(
        windows_input.WINDOWS_GENERIC_MSG, None, True,
    ) is None


def test_main_window_handles_native_multitouch_query(qapp, monkeypatch):
    window = MainWindow()
    window.settings.tablet_mode = True
    calls = []
    monkeypatch.setattr(
        "comic_editor.ui.main_window.tablet_multitouch_native_result",
        lambda event_type, message, enabled: (
            calls.append((event_type, message, enabled))
            or windows_input.TABLET_ENABLE_MULTITOUCHDATA
        ),
    )
    try:
        assert window.nativeEvent(b"windows_generic_MSG", 1234) == (
            True, windows_input.TABLET_ENABLE_MULTITOUCHDATA,
        )
        assert calls == [(b"windows_generic_MSG", 1234, True)]
    finally:
        window.close()
