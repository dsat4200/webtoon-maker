from __future__ import annotations

from PySide6.QtCore import QObject, QRect, Qt, Signal
from PySide6.QtWidgets import QWidget

from comic_editor.core.models import (
    BlenderViewObject,
    BoundGeometry,
    ChapterDocument,
    RasterObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.ui.blender_viewport import (
    BlenderViewportController,
    CanvasOverlayWindow,
    FrameGeometry,
    frame_geometry,
)
from comic_editor.ui.canvas import CanvasWidget


class FakeNativeApi:
    available = True

    def __init__(self):
        self.hidden = []
        self.attached = []
        self.positioned = []
        self.stacked = []
        self.closed = []

    def find_window_for_pid(self, pid):
        return 9001 if pid else 0

    def attach_external(self, hwnd, owner_hwnd):
        self.attached.append((hwnd, owner_hwnd))
        return True

    def configure_overlay(self, hwnd, owner_hwnd):
        self.overlay = hwnd, owner_hwnd

    def logical_rect_to_native(self, owner_hwnd, rect, fallback_ratio):
        del owner_hwnd, fallback_ratio
        return QRect(rect)

    def set_region_and_position(self, hwnd, rect, region, insert_after):
        self.positioned.append((hwnd, QRect(rect), region, insert_after))
        return True

    def stack_above(self, hwnd, below):
        self.stacked.append((hwnd, below))

    def hide(self, hwnd):
        self.hidden.append(hwnd)

    def request_close(self, hwnd):
        self.closed.append(hwnd)


class FakeProcess(QObject):
    ready = Signal(int)
    failed = Signal(str)
    viewStateChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.state = "stopped"
        self.pid = 123
        self.started = 0
        self.requests = []
        self.stopped = 0

    def ensure_started(self):
        self.started += 1
        self.state = "starting"

    def request(self, command, payload=None, callback=None):
        self.requests.append((command, payload))
        if callback is not None:
            callback(True, payload or {
                "rotation": [1, 0, 0, 0],
                "location": [0, 0, 0],
                "distance": 10,
                "perspective": "PERSP",
                "lens": 50,
                "camera_zoom": 0,
                "camera_offset": [0, 0],
            })
        return len(self.requests)

    def restart(self):
        self.state = "starting"
        self.started += 1

    def stop(self, force=False):
        del force
        self.stopped += 1
        self.state = "stopped"


def _canvas_with_nested_frames(qapp):
    del qapp
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    outer = chapter.add_layer(
        page.layer_id, "Outer", BoundGeometry.rectangle(0, 0, 500, 400)
    )
    inner = chapter.add_layer(
        outer.layer_id, "Inner", BoundGeometry.rectangle(100, 100, 200, 150)
    )
    outer_frame = chapter.add_object(outer.layer_id, BlenderViewObject(name="Outer 3D"))
    inner_frame = chapter.add_object(inner.layer_id, BlenderViewObject(name="Inner 3D"))
    raster = chapter.add_object(inner.layer_id, RasterObject(name="Ink"))
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, canvas.tiles, reset_view=False)
    canvas.center_x = 250
    canvas.center_y = 200
    canvas.scale = 1
    return canvas, outer, inner, outer_frame, inner_frame, raster


def test_controller_uses_nearest_frame_bearing_shape_context(qapp):
    canvas, outer, inner, outer_frame, inner_frame, raster = (
        _canvas_with_nested_frames(qapp)
    )
    host = QWidget()
    process = FakeProcess()
    controller = BlenderViewportController(
        host, canvas, process=process, native_api=FakeNativeApi()
    )

    canvas.set_selection("object", raster.object_id)
    assert controller.active_object_id == inner_frame.object_id
    assert process.started == 1

    canvas.set_selection("layer", outer.layer_id)
    assert controller.active_object_id == outer_frame.object_id

    controller.shutdown()
    host.close()


def test_frame_geometry_uses_compound_holes_and_rotation_policy(qapp):
    del qapp
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    shape = chapter.add_layer(
        page.layer_id, "Compound", BoundGeometry.rectangle(0, 0, 400, 400)
    )
    shape.compound_enabled = True
    hole = chapter.add_layer(
        shape.layer_id, "Hole", BoundGeometry.circle(200, 200, 100)
    )
    hole.compound_operation = "subtract"
    frame = chapter.add_object(shape.layer_id, BlenderViewObject())
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(600, 600)
    canvas.set_document(chapter, canvas.tiles, reset_view=False)
    canvas.center_x = 200
    canvas.center_y = 200
    canvas.scale = 1
    api = FakeNativeApi()

    geometry = frame_geometry(canvas, frame, api, 1)

    assert geometry is not None
    assert geometry.logical_region.contains(geometry.logical_region.boundingRect().topLeft())
    assert not geometry.logical_region.contains(
        geometry.logical_region.boundingRect().center()
    )
    canvas.rotation = 1
    assert frame_geometry(canvas, frame, api, 1) is None


def test_diagnostic_overlay_is_transparent_for_pointer_input(qapp):
    owner = QWidget()
    overlay = CanvasOverlayWindow(owner, FakeNativeApi())
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
    assert overlay.testAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
    assert overlay.focusPolicy() == Qt.FocusPolicy.NoFocus
    overlay.close()
    owner.close()


def test_view_state_events_persist_without_undo_commands(qapp):
    canvas, _outer, _inner, _outer_frame, inner_frame, raster = (
        _canvas_with_nested_frames(qapp)
    )
    host = QWidget()
    process = FakeProcess()
    controller = BlenderViewportController(
        host, canvas, process=process, native_api=FakeNativeApi()
    )
    canvas.set_selection("object", raster.object_id)
    before_undo = canvas.command_stack.can_undo

    controller._view_state_changed({
        "rotation": [0.70710678, 0, 0.70710678, 0],
        "location": [1, 2, 3],
        "distance": 7,
        "perspective": "ORTHO",
        "lens": 70,
        "camera_zoom": 4,
        "camera_offset": [0.2, -0.1],
    })

    assert inner_frame.view_state is not None
    assert inner_frame.view_state.location == (1.0, 2.0, 3.0)
    assert canvas.command_stack.can_undo == before_undo
    controller.shutdown()
    host.close()


def test_compound_flatten_rejects_frame_collisions_and_preserves_one(qapp):
    del qapp
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    root = chapter.add_layer(
        page.layer_id, "Root", BoundGeometry.rectangle(0, 0, 400, 400)
    )
    root.compound_enabled = True
    child = chapter.add_layer(
        root.layer_id, "Part", BoundGeometry.rectangle(20, 20, 200, 200)
    )
    child.compound_enabled = True
    root_frame = chapter.add_object(root.layer_id, BlenderViewObject(name="Root 3D"))
    child_frame = chapter.add_object(child.layer_id, BlenderViewObject(name="Child 3D"))
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, canvas.tiles, reset_view=False)

    assert not canvas.flatten_compound_layer(root.layer_id)
    chapter.delete_entity("object", root_frame.object_id)
    assert canvas.flatten_compound_layer(root.layer_id)
    assert chapter.blender_view_for_layer(root.layer_id).object_id == child_frame.object_id
    assert chapter.layers[root.layer_id].children[-1].entity_id == child_frame.object_id
    chapter.validate()
