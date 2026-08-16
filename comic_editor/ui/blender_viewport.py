"""Shape-bound native Blender viewport and diagnostic overlay controller."""
from __future__ import annotations

import ctypes
import math
import sys
from ctypes import wintypes
from dataclasses import dataclass

from PySide6.QtCore import (
    QEvent, QEventLoop, QObject, QPoint, QRect, Qt, QTimer, Signal,
)
from PySide6.QtGui import (
    QColor, QPainter, QPainterPath, QPen, QPolygon, QRegion, QTransform,
)
from PySide6.QtWidgets import QApplication, QWidget

from comic_editor.core.models import BlenderViewObject, BlenderViewportState
from comic_editor.integrations.blender_process import BlenderProcessManager


ROTATION_EPSILON = 0.01


@dataclass
class FrameGeometry:
    logical_global_rect: QRect
    logical_region: QRegion
    native_global_rect: QRect
    native_region: QRegion


class Win32Api:
    """Small injectable adapter for the native behavior of external windows."""

    GWL_STYLE = -16
    GWL_EXSTYLE = -20
    GWLP_HWNDPARENT = -8
    WS_CAPTION = 0x00C00000
    WS_THICKFRAME = 0x00040000
    WS_MINIMIZEBOX = 0x00020000
    WS_MAXIMIZEBOX = 0x00010000
    WS_SYSMENU = 0x00080000
    WS_POPUP = 0x80000000
    WS_VISIBLE = 0x10000000
    WS_EX_TOOLWINDOW = 0x00000080
    WS_EX_APPWINDOW = 0x00040000
    WS_EX_TRANSPARENT = 0x00000020
    WS_EX_NOACTIVATE = 0x08000000
    SW_HIDE = 0
    SW_SHOWNOACTIVATE = 4
    SWP_NOSIZE = 0x0001
    SWP_NOMOVE = 0x0002
    SWP_NOACTIVATE = 0x0010
    SWP_FRAMECHANGED = 0x0020
    SWP_SHOWWINDOW = 0x0040
    RGN_OR = 2
    WM_CLOSE = 0x0010

    def __init__(self):
        self.available = sys.platform == "win32"
        self._saved_styles: dict[int, tuple[int, int, int]] = {}
        if not self.available:
            return
        self.user32 = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._get_window_long = getattr(
            self.user32, "GetWindowLongPtrW", self.user32.GetWindowLongW
        )
        self._set_window_long = getattr(
            self.user32, "SetWindowLongPtrW", self.user32.SetWindowLongW
        )
        self._get_window_long.argtypes = (wintypes.HWND, ctypes.c_int)
        self._get_window_long.restype = ctypes.c_ssize_t
        self._set_window_long.argtypes = (
            wintypes.HWND, ctypes.c_int, ctypes.c_ssize_t,
        )
        self._set_window_long.restype = ctypes.c_ssize_t
        self.gdi32.CreateRectRgn.argtypes = (
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
        )
        self.gdi32.CreateRectRgn.restype = wintypes.HRGN
        self.gdi32.CombineRgn.argtypes = (
            wintypes.HRGN, wintypes.HRGN, wintypes.HRGN, ctypes.c_int,
        )
        self.gdi32.CombineRgn.restype = ctypes.c_int
        self.gdi32.DeleteObject.argtypes = (wintypes.HGDIOBJ,)
        self.gdi32.DeleteObject.restype = wintypes.BOOL
        self.user32.SetWindowPos.argtypes = (
            wintypes.HWND, wintypes.HWND,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.UINT,
        )
        self.user32.SetWindowPos.restype = wintypes.BOOL
        self.user32.SetWindowRgn.argtypes = (
            wintypes.HWND, wintypes.HRGN, wintypes.BOOL,
        )
        self.user32.SetWindowRgn.restype = ctypes.c_int
        self.user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        self.user32.ShowWindow.restype = wintypes.BOOL
        self.user32.PostMessageW.argtypes = (
            wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
        )
        self.user32.PostMessageW.restype = wintypes.BOOL
        self.user32.GetWindowThreadProcessId.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.DWORD),
        )
        self.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user32.GetWindowRect.argtypes = (
            wintypes.HWND, ctypes.POINTER(wintypes.RECT),
        )
        self.user32.GetWindowRect.restype = wintypes.BOOL
        self.user32.GetClassNameW.argtypes = (
            wintypes.HWND, wintypes.LPWSTR, ctypes.c_int,
        )
        self.user32.GetClassNameW.restype = ctypes.c_int

    def find_window_for_pid(self, pid: int) -> int:
        if not self.available or not pid:
            return 0
        candidates: list[tuple[int, int, bool]] = []
        callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

        def visit(hwnd, _lparam):
            process_id = wintypes.DWORD()
            self.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(process_id))
            if process_id.value != int(pid):
                return True
            rect = wintypes.RECT()
            if not self.user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                return True
            area = max(0, rect.right - rect.left) * max(0, rect.bottom - rect.top)
            class_name = ctypes.create_unicode_buffer(256)
            self.user32.GetClassNameW(hwnd, class_name, len(class_name))
            preferred = "GHOST" in class_name.value.upper()
            candidates.append((area, int(hwnd), preferred))
            return True

        self.user32.EnumWindows(callback_type(visit), 0)
        if not candidates:
            return 0
        candidates.sort(key=lambda item: (item[2], item[0]), reverse=True)
        return candidates[0][1]

    def attach_external(self, hwnd: int, owner_hwnd: int) -> bool:
        if not self.available or not hwnd:
            return False
        style = int(self._get_window_long(hwnd, self.GWL_STYLE))
        exstyle = int(self._get_window_long(hwnd, self.GWL_EXSTYLE))
        owner = int(self._get_window_long(hwnd, self.GWLP_HWNDPARENT))
        self._saved_styles.setdefault(hwnd, (style, exstyle, owner))
        style &= ~(
            self.WS_CAPTION | self.WS_THICKFRAME | self.WS_MINIMIZEBOX
            | self.WS_MAXIMIZEBOX | self.WS_SYSMENU
        )
        style |= self.WS_POPUP | self.WS_VISIBLE
        exstyle = (exstyle | self.WS_EX_TOOLWINDOW) & ~self.WS_EX_APPWINDOW
        self._set_window_long(hwnd, self.GWL_STYLE, style)
        self._set_window_long(hwnd, self.GWL_EXSTYLE, exstyle)
        self._set_window_long(hwnd, self.GWLP_HWNDPARENT, owner_hwnd)
        self.user32.SetWindowPos(
            hwnd, owner_hwnd, 0, 0, 0, 0,
            self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE
            | self.SWP_FRAMECHANGED,
        )
        return True

    def configure_overlay(self, hwnd: int, owner_hwnd: int) -> None:
        if not self.available or not hwnd:
            return
        exstyle = int(self._get_window_long(hwnd, self.GWL_EXSTYLE))
        exstyle |= (
            self.WS_EX_TOOLWINDOW | self.WS_EX_TRANSPARENT
            | self.WS_EX_NOACTIVATE
        )
        exstyle &= ~self.WS_EX_APPWINDOW
        self._set_window_long(hwnd, self.GWL_EXSTYLE, exstyle)
        self._set_window_long(hwnd, self.GWLP_HWNDPARENT, owner_hwnd)

    def logical_rect_to_native(
        self, owner_hwnd: int, rect: QRect, fallback_ratio: float,
    ) -> QRect:
        if not self.available:
            return QRect(rect)
        converter = getattr(
            self.user32, "LogicalToPhysicalPointForPerMonitorDPI", None
        )
        if converter is not None and owner_hwnd:
            first = wintypes.POINT(rect.left(), rect.top())
            second = wintypes.POINT(rect.right() + 1, rect.bottom() + 1)
            if converter(owner_hwnd, ctypes.byref(first)) and converter(
                owner_hwnd, ctypes.byref(second)
            ):
                return QRect(
                    first.x, first.y,
                    max(1, second.x - first.x), max(1, second.y - first.y),
                )
        ratio = max(0.1, float(fallback_ratio))
        return QRect(
            round(rect.x() * ratio), round(rect.y() * ratio),
            max(1, round(rect.width() * ratio)),
            max(1, round(rect.height() * ratio)),
        )

    def set_region_and_position(
        self, hwnd: int, rect: QRect, region: QRegion, insert_after: int,
    ) -> bool:
        if not self.available or not hwnd or rect.isEmpty() or region.isEmpty():
            return False
        target = self.gdi32.CreateRectRgn(0, 0, 0, 0)
        if not target:
            return False
        for item in region:
            part = self.gdi32.CreateRectRgn(
                item.left(), item.top(), item.right() + 1, item.bottom() + 1
            )
            if part:
                self.gdi32.CombineRgn(target, target, part, self.RGN_OR)
                self.gdi32.DeleteObject(part)
        if not self.user32.SetWindowRgn(hwnd, target, False):
            self.gdi32.DeleteObject(target)
            return False
        return bool(self.user32.SetWindowPos(
            hwnd, insert_after,
            rect.x(), rect.y(), rect.width(), rect.height(),
            self.SWP_NOACTIVATE | self.SWP_SHOWWINDOW,
        ))

    def stack_above(self, hwnd: int, below: int) -> None:
        if self.available and hwnd:
            self.user32.SetWindowPos(
                hwnd, below, 0, 0, 0, 0,
                self.SWP_NOMOVE | self.SWP_NOSIZE | self.SWP_NOACTIVATE,
            )

    def hide(self, hwnd: int) -> None:
        if self.available and hwnd:
            self.user32.ShowWindow(hwnd, self.SW_HIDE)

    def request_close(self, hwnd: int) -> None:
        if self.available and hwnd:
            self.user32.PostMessageW(hwnd, self.WM_CLOSE, 0, 0)


class ExternalWindowManager(QObject):
    geometryChanged = Signal()

    def __init__(self, api: Win32Api | None = None, parent=None):
        super().__init__(parent)
        self.api = api or Win32Api()
        self.hwnd = 0
        self.pid = 0
        self.owner_hwnd = 0
        self.last_geometry: FrameGeometry | None = None

    def attach_pid(self, pid: int, owner_hwnd: int) -> bool:
        hwnd = self.api.find_window_for_pid(pid)
        if not hwnd or not self.api.attach_external(hwnd, owner_hwnd):
            return False
        self.pid = int(pid)
        self.hwnd = int(hwnd)
        self.owner_hwnd = int(owner_hwnd)
        return True

    def sync(self, geometry: FrameGeometry) -> bool:
        if not self.hwnd:
            return False
        changed = geometry != self.last_geometry
        success = self.api.set_region_and_position(
            self.hwnd, geometry.native_global_rect,
            geometry.native_region, self.owner_hwnd,
        )
        if success:
            self.last_geometry = geometry
            if changed:
                self.geometryChanged.emit()
        return success

    def hide(self) -> None:
        self.api.hide(self.hwnd)

    def detach(self) -> None:
        self.hide()
        self.hwnd = 0
        self.pid = 0
        self.last_geometry = None


class CanvasOverlayWindow(QWidget):
    """Click-through proof overlay; it intentionally does not render the canvas."""

    def __init__(self, owner: QWidget, api: Win32Api):
        super().__init__(
            owner,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowDoesNotAcceptFocus,
        )
        self.api = api
        self.frame_label = ""
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.hide()

    def ensure_native_style(self, owner_hwnd: int) -> None:
        self.api.configure_overlay(int(self.winId()), int(owner_hwnd))

    def sync(self, geometry: FrameGeometry, frame_label: str, below_hwnd: int) -> None:
        self.frame_label = frame_label
        self.setGeometry(geometry.logical_global_rect)
        self.setMask(geometry.logical_region)
        self.show()
        self.raise_()
        self.api.stack_above(int(self.winId()), below_hwnd)
        self.update()

    def paintEvent(self, event) -> None:  # noqa: N802
        del event
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        color = QColor(255, 96, 180, 210)
        painter.setPen(QPen(color, 2))
        bounds = self.rect().adjusted(7, 7, -8, -8)
        span = min(28, max(8, min(bounds.width(), bounds.height()) // 5))
        for x, y, sx, sy in (
            (bounds.left(), bounds.top(), 1, 1),
            (bounds.right(), bounds.top(), -1, 1),
            (bounds.right(), bounds.bottom(), -1, -1),
            (bounds.left(), bounds.bottom(), 1, -1),
        ):
            painter.drawLine(x, y, x + sx * span, y)
            painter.drawLine(x, y, x, y + sy * span)
        center = bounds.center()
        painter.drawLine(center.x() - 12, center.y(), center.x() + 12, center.y())
        painter.drawLine(center.x(), center.y() - 12, center.x(), center.y() + 12)
        painter.setPen(QColor(255, 255, 255, 215))
        painter.drawText(
            self.rect().adjusted(12, 12, -12, -12),
            Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignHCenter,
            self.frame_label,
        )


def _translated_path(path: QPainterPath, x: float, y: float) -> QPainterPath:
    transform = QTransform()
    transform.translate(x, y)
    return transform.map(path)


def _path_region(path: QPainterPath, origin: QPoint, sx=1.0, sy=1.0) -> QRegion:
    polygon = path.toFillPolygon()
    points = QPolygon([
        QPoint(
            round((point.x() - origin.x()) * sx),
            round((point.y() - origin.y()) * sy),
        )
        for point in polygon
    ])
    return QRegion(points, Qt.FillRule.OddEvenFill) if not points.isEmpty() else QRegion()


def frame_geometry(
    canvas, obj: BlenderViewObject, api: Win32Api, owner_hwnd: int,
) -> FrameGeometry | None:
    chapter = canvas.chapter
    parent = chapter.layers.get(obj.parent_layer_id) if chapter is not None else None
    if (
        chapter is None or parent is None or parent.bound is None
        or not obj.visible or abs(float(canvas.rotation)) > ROTATION_EPSILON
    ):
        return None
    combined: QPainterPath | None = None
    for layer in chapter.ancestor_layers(parent.layer_id):
        if not layer.visible or layer.opacity <= 0 or layer.bound is None:
            return None
        wx, wy = chapter.layer_world_translation(layer.layer_id)
        candidate = _translated_path(
            canvas.layer_effective_path(layer.layer_id), wx, wy
        )
        combined = candidate if combined is None else combined.intersected(candidate)
    if combined is None or combined.isEmpty():
        return None
    document_path = QPainterPath()
    document_path.addRect(0, 0, chapter.width, chapter.height)
    combined = combined.intersected(document_path)
    widget_path = canvas.camera_transform().map(combined)
    canvas_path = QPainterPath()
    canvas_path.addRect(canvas.rect())
    widget_path = widget_path.intersected(canvas_path)
    if widget_path.isEmpty():
        return None
    local_bounds = widget_path.boundingRect().toAlignedRect().intersected(canvas.rect())
    if local_bounds.width() < 2 or local_bounds.height() < 2:
        return None
    global_top_left = canvas.mapToGlobal(local_bounds.topLeft())
    logical_rect = QRect(global_top_left, local_bounds.size())
    logical_region = _path_region(widget_path, local_bounds.topLeft())
    if logical_region.isEmpty():
        return None
    ratio = float(canvas.devicePixelRatioF())
    native_rect = api.logical_rect_to_native(owner_hwnd, logical_rect, ratio)
    sx = native_rect.width() / max(1, logical_rect.width())
    sy = native_rect.height() / max(1, logical_rect.height())
    native_region = _path_region(widget_path, local_bounds.topLeft(), sx, sy)
    if native_region.isEmpty():
        return None
    return FrameGeometry(logical_rect, logical_region, native_rect, native_region)


class BlenderViewportController(QObject):
    """Coordinates shape context, one Blender process, HWND, and overlay."""

    activeFrameChanged = Signal(str)
    viewStateUpdated = Signal(str)
    errorOccurred = Signal(str)

    def __init__(
        self, main_window: QWidget, canvas,
        process: BlenderProcessManager | None = None,
        native_api: Win32Api | None = None,
    ):
        super().__init__(main_window)
        self.main_window = main_window
        self.canvas = canvas
        self.process = process or BlenderProcessManager(self)
        self.external = ExternalWindowManager(native_api, self)
        self.overlay = CanvasOverlayWindow(main_window, self.external.api)
        self.active_object_id = ""
        self._switch_generation = 0
        self._attach_attempts = 0
        self._geometry_timer = QTimer(self)
        self._geometry_timer.setSingleShot(True)
        self._geometry_timer.timeout.connect(self._sync_geometry)
        self._attach_timer = QTimer(self)
        self._attach_timer.setInterval(100)
        self._attach_timer.timeout.connect(self._try_attach)
        self.process.ready.connect(self._process_ready)
        self.process.failed.connect(self._process_failed)
        self.process.viewStateChanged.connect(self._view_state_changed)
        self.canvas.selectionChanged.connect(self.refresh_context)
        self.canvas.cameraChanged.connect(self.schedule_geometry)
        self.canvas.hierarchyChanged.connect(self.refresh_context)
        self.canvas.chapterReplaced.connect(lambda _chapter: self.refresh_context())
        self.canvas.documentChanged.connect(lambda _rect: self.schedule_geometry())
        self.canvas.blenderRestartRequested.connect(self.restart)
        self.main_window.installEventFilter(self)
        self.canvas.installEventFilter(self)

    def eventFilter(self, watched, event) -> bool:  # noqa: N802
        if watched in {self.main_window, self.canvas} and event.type() in {
            QEvent.Type.Move, QEvent.Type.Resize, QEvent.Type.Show,
            QEvent.Type.Hide, QEvent.Type.WindowStateChange,
        }:
            self.schedule_geometry()
        return super().eventFilter(watched, event)

    def _context_frame_id(self) -> str:
        chapter = self.canvas.chapter
        if chapter is None or not self.canvas.selected_id:
            return ""
        if self.canvas.selected_kind == "object":
            selected = chapter.objects.get(self.canvas.selected_id)
            layer_id = selected.parent_layer_id if selected is not None else ""
        else:
            layer_id = self.canvas.selected_id
        while layer_id and layer_id in chapter.layers:
            frame = chapter.blender_view_for_layer(layer_id)
            if frame is not None:
                return frame.object_id
            layer_id = chapter.layers[layer_id].parent_id or ""
        return ""

    def refresh_context(self, *_args) -> None:
        target = self._context_frame_id()
        if target == self.active_object_id:
            self.schedule_geometry()
            return
        previous = self.active_object_id
        self._switch_generation += 1
        generation = self._switch_generation
        self.external.hide()
        self.overlay.hide()
        if previous and self.canvas.chapter is not None:
            self.canvas.set_blender_view_status(previous, "inactive")

        def continue_switch(_ok=False, payload=None):
            if generation != self._switch_generation:
                return
            if previous and _ok:
                self._store_view_state(previous, payload)
            self.active_object_id = target
            self.activeFrameChanged.emit(target)
            if not target:
                return
            self.canvas.set_blender_view_status(target, "loading")
            if self.process.state == "ready":
                self._apply_active_state()
            elif self.process.state == "starting":
                return
            elif self.process.state == "failed":
                self.canvas.set_blender_view_status(
                    target, "failed", "Double-click to retry"
                )
            else:
                self.process.ensure_started()

        if previous and self.process.state == "ready":
            completed = {"value": False}

            def received(ok, payload):
                if completed["value"]:
                    return
                completed["value"] = True
                continue_switch(ok, payload)

            self._request_with_timeout(
                "GET_VIEW_STATE", None, received, timeout_ms=500
            )
        else:
            continue_switch()

    def _process_ready(self, _pid: int) -> None:
        self._attach_attempts = 0
        self._attach_timer.start()
        self._try_attach()

    def _try_attach(self) -> None:
        if self.process.state != "ready":
            self._attach_timer.stop()
            return
        owner_hwnd = int(self.main_window.winId())
        if self.external.attach_pid(self.process.pid, owner_hwnd):
            self._attach_timer.stop()
            self.overlay.ensure_native_style(owner_hwnd)
            self._apply_active_state()
            return
        self._attach_attempts += 1
        if self._attach_attempts >= 50:
            self._attach_timer.stop()
            self._process_failed("Unable to find the Blender window")

    def _apply_active_state(self) -> None:
        chapter = self.canvas.chapter
        obj = chapter.objects.get(self.active_object_id) if chapter else None
        if not isinstance(obj, BlenderViewObject) or not self.external.hwnd:
            return

        def applied(ok: bool, payload) -> None:
            if not isinstance(
                self.canvas.chapter.objects.get(self.active_object_id)
                if self.canvas.chapter else None,
                BlenderViewObject,
            ):
                return
            if ok:
                self._store_view_state(self.active_object_id, payload)
                self.canvas.set_blender_view_status(
                    self.active_object_id, "ready"
                )
                self.schedule_geometry()
            else:
                self._process_failed(str(payload))

        if obj.view_state is None:
            self._request_with_timeout(
                "GET_VIEW_STATE", None, applied, timeout_ms=500
            )
        else:
            self._request_with_timeout(
                "SET_VIEW_STATE", obj.view_state.to_dict(), applied,
                timeout_ms=500,
            )

    def _request_with_timeout(
        self, command: str, payload, callback, *, timeout_ms: int,
    ) -> None:
        completed = {"value": False}

        def finish(ok: bool, result) -> None:
            if completed["value"]:
                return
            completed["value"] = True
            callback(ok, result)

        self.process.request(command, payload, callback=finish)
        QTimer.singleShot(
            max(1, int(timeout_ms)),
            lambda: finish(False, "Blender view-state request timed out"),
        )

    def _store_view_state(self, object_id: str, payload) -> None:
        chapter = self.canvas.chapter
        obj = chapter.objects.get(object_id) if chapter else None
        if not isinstance(obj, BlenderViewObject) or not isinstance(payload, dict):
            return
        try:
            state = BlenderViewportState.from_dict(payload)
        except (TypeError, ValueError):
            return
        before = obj.view_state.to_dict() if obj.view_state is not None else None
        after = state.to_dict() if state is not None else None
        if before == after:
            return
        obj.view_state = state
        self.viewStateUpdated.emit(object_id)

    def _view_state_changed(self, payload) -> None:
        if self.active_object_id:
            self._store_view_state(self.active_object_id, payload)

    def schedule_geometry(self, *_args) -> None:
        if not self._geometry_timer.isActive():
            self._geometry_timer.start(0)

    def _sync_geometry(self) -> None:
        chapter = self.canvas.chapter
        obj = chapter.objects.get(self.active_object_id) if chapter else None
        if not isinstance(obj, BlenderViewObject):
            self.external.hide()
            self.overlay.hide()
            return
        if abs(float(self.canvas.rotation)) > ROTATION_EPSILON:
            self.external.hide()
            self.overlay.hide()
            self.canvas.set_blender_view_status(obj.object_id, "rotation")
            return
        if (
            self.process.state != "ready" or not self.external.hwnd
            or self.main_window.isMinimized() or not self.main_window.isVisible()
        ):
            self.external.hide()
            self.overlay.hide()
            return
        geometry = frame_geometry(
            self.canvas, obj, self.external.api, int(self.main_window.winId())
        )
        if geometry is None:
            self.external.hide()
            self.overlay.hide()
            return
        if self.external.sync(geometry):
            self.canvas.set_blender_view_status(obj.object_id, "ready")
            self.overlay.sync(
                geometry, f"{obj.name} · diagnostic overlay", self.external.hwnd
            )

    def _process_failed(self, message: str) -> None:
        self.external.detach()
        self.overlay.hide()
        if self.active_object_id:
            self.canvas.set_blender_view_status(
                self.active_object_id, "failed", str(message)
            )
        self.errorOccurred.emit(str(message))

    def restart(self, object_id: str = "") -> None:
        if object_id:
            self.active_object_id = object_id
        if self.active_object_id:
            self.canvas.set_blender_view_status(
                self.active_object_id, "loading"
            )
        self.external.detach()
        self.overlay.hide()
        self.process.restart()

    def flush_active_view_state(self, timeout_ms: int = 500) -> bool:
        """Synchronously capture the last composition before save/tab detach."""
        if not self.active_object_id or self.process.state != "ready":
            return False
        loop = QEventLoop()
        result = {"done": False, "ok": False, "payload": None}

        def received(ok: bool, payload) -> None:
            if result["done"]:
                return
            result.update(done=True, ok=bool(ok), payload=payload)
            if loop.isRunning():
                loop.quit()

        self.process.request("GET_VIEW_STATE", callback=received)
        QTimer.singleShot(max(1, int(timeout_ms)), loop.quit)
        if not result["done"]:
            loop.exec()
        if result["ok"]:
            self._store_view_state(
                self.active_object_id, result["payload"]
            )
        return bool(result["ok"])

    def shutdown(self) -> None:
        self._geometry_timer.stop()
        self._attach_timer.stop()
        self.overlay.hide()
        if self.external.hwnd:
            self.external.api.request_close(self.external.hwnd)
        self.process.stop()
        self.external.detach()
