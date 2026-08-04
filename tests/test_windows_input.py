from __future__ import annotations

from comic_editor.ui import windows_input


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
