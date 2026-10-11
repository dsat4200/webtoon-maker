"""A measured Windows presentation workaround; artwork still uses its renderer.

RTX 5060 / NVIDIA 591.44 / Qt 6.11.1 showed intermittent WGL presentation
stalls on an AMD 164E-connected display. Only this identified combination in
auto mode uses the existing QWidget presenter. GPU effect workers, native
sampling, exact caches and explicit renderer choices are unchanged.
"""
from dataclasses import dataclass
import sys


@dataclass(frozen=True)
class PresentationIdentity:
    vendor: str
    renderer: str
    version: str
    screen_name: str
    qt_version: str


def current_presentation_identity(context):
    """Read the existing availability probe while its GL context is current."""
    if sys.platform != 'win32':
        return None
    import ctypes as C
    from PySide6.QtCore import qVersion
    from PySide6.QtGui import QOpenGLContext
    if QOpenGLContext.currentContext() is not context:
        return None
    try:
        gl = C.WinDLL('opengl32')
        gl.glGetString.argtypes = [C.c_uint]
        gl.glGetString.restype = C.c_char_p
        values = [gl.glGetString(kind) for kind in (0x1F00, 0x1F01, 0x1F02)]
        screen = context.screen()
        if any(value is None for value in values) or screen is None:
            return None
        return PresentationIdentity(*(value.decode('ascii', 'replace') for value in values),
                                    screen.name(), qVersion())
    except (OSError, RuntimeError, ValueError):
        return None


def _active_display_routes():
    """Return unambiguous active target names/adapters using read-only Win32 APIs.

    Unknown, disconnected, indirect and differing source/target adapters do not
    establish the measured display route. Errors keep the ordinary presenter.
    No Windows display or GPU preference is modified.
    """
    if sys.platform != 'win32':
        return ()
    import ctypes as C
    u32, i32 = C.c_uint32, C.c_int32

    class Luid(C.Structure):
        _fields_ = [('low', u32), ('high', i32)]

    class Rational(C.Structure):
        _fields_ = [('numerator', u32), ('denominator', u32)]

    class Source(C.Structure):
        _fields_ = [('adapter', Luid), ('id', u32), ('mode', u32), ('status', u32)]

    class Target(C.Structure):
        _fields_ = [('adapter', Luid), ('id', u32), ('mode', u32), ('technology', i32),
                    ('rotation', i32), ('scaling', i32), ('refresh', Rational),
                    ('scanline', i32), ('available', i32), ('status', u32)]

    class Path(C.Structure):
        _fields_ = [('source', Source), ('target', Target), ('flags', u32)]

    class ModeData(C.Union):
        # DISPLAYCONFIG_MODE_INFO's largest member is the aligned 48-byte
        # DISPLAYCONFIG_VIDEO_SIGNAL_INFO. Modes themselves are not inspected.
        _fields_ = [('signal_storage', C.c_uint64 * 6)]

    class Mode(C.Structure):
        _fields_ = [('type', i32), ('id', u32), ('adapter', Luid), ('data', ModeData)]

    class Header(C.Structure):
        _fields_ = [('type', i32), ('size', u32), ('adapter', Luid), ('id', u32)]

    class TargetName(C.Structure):
        _fields_ = [('header', Header), ('flags', u32), ('technology', i32),
                    ('manufacturer', C.c_uint16), ('product', C.c_uint16),
                    ('connector', u32), ('name', C.c_wchar * 64), ('path', C.c_wchar * 128)]

    class AdapterName(C.Structure):
        _fields_ = [('header', Header), ('path', C.c_wchar * 128)]

    if [C.sizeof(kind) for kind in (Path, Mode, Header, TargetName, AdapterName)] != [72, 64, 20, 420, 276]:
        return ()
    try:
        user = C.WinDLL('user32')
        user.GetDisplayConfigBufferSizes.argtypes = [u32, C.POINTER(u32), C.POINTER(u32)]
        user.GetDisplayConfigBufferSizes.restype = i32
        user.QueryDisplayConfig.argtypes = [u32, C.POINTER(u32), C.POINTER(Path),
                                           C.POINTER(u32), C.POINTER(Mode), C.c_void_p]
        user.QueryDisplayConfig.restype = i32
        user.DisplayConfigGetDeviceInfo.argtypes = [C.POINTER(Header)]
        user.DisplayConfigGetDeviceInfo.restype = i32
        for _attempt in range(3):
            path_count, mode_count = u32(), u32()
            code = user.GetDisplayConfigBufferSizes(2, C.byref(path_count), C.byref(mode_count))
            if code or not 0 < path_count.value <= 64 or mode_count.value > 256:
                return ()
            paths = (Path * path_count.value)()
            modes = (Mode * max(1, mode_count.value))()
            code = user.QueryDisplayConfig(2, C.byref(path_count), paths,
                                          C.byref(mode_count), modes, None)
            if code != 122:  # ERROR_INSUFFICIENT_BUFFER: topology changed.
                break
        if code:
            return ()
        # This measured workaround intentionally declines multi-display setups.
        if path_count.value != 1:
            return ()
        path = paths[0]
        if (not path.flags & 1 or not path.target.available or path.target.technology != 5
                or (path.source.adapter.low, path.source.adapter.high)
                != (path.target.adapter.low, path.target.adapter.high)):
            return ()
        target, adapter = TargetName(), AdapterName()
        for value, kind, identifier in ((target, 2, path.target.id), (adapter, 4, 0)):
            value.header.type, value.header.size = kind, C.sizeof(type(value))
            value.header.adapter, value.header.id = path.target.adapter, identifier
            if user.DisplayConfigGetDeviceInfo(C.byref(value.header)):
                return ()
        return ((target.name, adapter.path),) if target.name and adapter.path else ()
    except (OSError, AttributeError, ValueError):
        return ()


def prefer_raster_presentation(identity, screen_name):
    """Select only the demonstrated cross-adapter configuration, in auto mode."""
    if (identity is None or sys.platform != 'win32'
            or identity.qt_version != '6.11.1'
            or identity.vendor != 'NVIDIA Corporation'
            or identity.renderer.split('/')[0] != 'NVIDIA GeForce RTX 5060'
            or identity.version.split()[-1:] != ['591.44']
            or not screen_name or screen_name != identity.screen_name):
        return False
    routes = _active_display_routes()
    return (len(routes) == 1 and routes[0][0] == screen_name
            and 'pci#ven_1002&dev_164e&' in routes[0][1].lower())
