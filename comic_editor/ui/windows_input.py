"""Small Windows input interop helpers used by the canvas."""
from __future__ import annotations

import ctypes
import sys
from ctypes import wintypes


TWF_WANTPALM = 0x00000002
TABLET_ENABLE_MULTITOUCHDATA = 0x01000000
MICROSOFT_TABLETPENSERVICE_PROPERTY = "MicrosoftTabletPenServiceProperty"
WM_TABLET_QUERYSYSTEMGESTURESTATUS = 0x02CC
WINDOWS_GENERIC_MSG = b"windows_generic_MSG"


def _load_user32():
    return ctypes.WinDLL("user32", use_last_error=True)


def tablet_multitouch_native_result(
    event_type, message, enabled: bool,
) -> int | None:
    """Return the Windows tablet-service opt-in for a native query.

    Qt passes a pointer to a ``MSG`` structure to ``QWidget.nativeEvent``.
    Only the top-level Windows message and the exact tablet gesture-status
    query are handled; every other native message must continue through Qt.
    """
    if sys.platform != "win32" or not enabled:
        return None
    try:
        if bytes(event_type) != WINDOWS_GENERIC_MSG:
            return None
        address = int(message)
        if address <= 0:
            return None
        native_message = wintypes.MSG.from_address(address)
    except (TypeError, ValueError, OverflowError, OSError):
        return None
    if native_message.message != WM_TABLET_QUERYSYSTEMGESTURESTATUS:
        return None
    return TABLET_ENABLE_MULTITOUCHDATA


def configure_simultaneous_pen_touch(hwnd: int, enabled: bool) -> bool:
    """Let touch packets reach a canvas while a pen is in proximity.

    Qt opts widgets into touch delivery, but Windows can still classify nearby
    finger contacts as palms and suppress them while a pen is hovering.  The
    tablet-service property requests concurrent multi-touch data; WANTPALM
    makes raw touch delivery immediate instead of filtering those contacts.
    """
    if sys.platform != "win32" or not hwnd:
        return False
    try:
        user32 = _load_user32()
        register_touch = user32.RegisterTouchWindow
        register_touch.argtypes = (wintypes.HWND, wintypes.ULONG)
        register_touch.restype = wintypes.BOOL
        registered = bool(register_touch(
            wintypes.HWND(hwnd), TWF_WANTPALM if enabled else 0,
        ))

        if enabled:
            set_property = user32.SetPropW
            set_property.argtypes = (
                wintypes.HWND, wintypes.LPCWSTR, wintypes.HANDLE,
            )
            set_property.restype = wintypes.BOOL
            property_configured = bool(set_property(
                wintypes.HWND(hwnd),
                MICROSOFT_TABLETPENSERVICE_PROPERTY,
                wintypes.HANDLE(TABLET_ENABLE_MULTITOUCHDATA),
            ))
        else:
            remove_property = user32.RemovePropW
            remove_property.argtypes = (wintypes.HWND, wintypes.LPCWSTR)
            remove_property.restype = wintypes.HANDLE
            remove_property(
                wintypes.HWND(hwnd), MICROSOFT_TABLETPENSERVICE_PROPERTY,
            )
            property_configured = True
        return registered and property_configured
    except (AttributeError, OSError, TypeError, ValueError):
        return False
