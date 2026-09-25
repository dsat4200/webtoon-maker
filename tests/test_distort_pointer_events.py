"""Mouse and pen events commit/cancel distortion gizmos through the canvas."""
from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QKeyEvent, QMouseEvent, QPointingDevice, QTabletEvent

from comic_editor.core.models import BoundGeometry, ChapterDocument, DistortModifier, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def canvas(qapp):
    widget = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                          grid_overlay_visible=False))
    chapter = ChapterDocument(width=600, height=480, document_kind="image")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 600, 480))
    page.fill_color, page.border_width = None, 0
    owner = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(60, 70, 200, 160)))
    widget.resize(600, 480)
    widget.set_document(chapter, TileStore())
    widget.set_selection("object", owner.object_id, activate_default_tool=False)
    widget.set_tool(ToolKind.OBJECT_SELECT)
    widget.scale, widget.rotation = 1.25, 14.
    widget.center_x, widget.center_y = 300, 240
    widget.modifier_mode = True
    yield widget
    widget._effect_jobs.cancel()
    widget.close()
    widget.deleteLater()


def attach(canvas, control):
    modifier = DistortModifier(
        modifier_type="distort_deform" if control == "pin" else "distort_twirl",
        frame=(60, 70, 200, 160), center=(160, 150), radius=45,
        points=[(.25, .4)] if control == "pin" else [],
        source_points=[(.25, .4)] if control == "pin" else [],
    )
    canvas.chapter.add_modifier(modifier, [("object", canvas.selected_object_id)])
    canvas.active_modifier_id = modifier.modifier_id
    return modifier


def pointer(canvas, stylus, phase, world):
    position = canvas.document_to_widget(world)
    global_position = QPointF(canvas.mapToGlobal(position.toPoint()))
    button = Qt.NoButton if phase == "move" else Qt.LeftButton
    buttons = Qt.NoButton if phase == "release" else Qt.LeftButton
    if stylus:
        event_type = {"press": QEvent.TabletPress, "move": QEvent.TabletMove,
                      "release": QEvent.TabletRelease}[phase]
        event = QTabletEvent(event_type, QPointingDevice.primaryPointingDevice(),
            position, global_position, 0. if phase == "release" else .65,
            0., 0., 0., 0., 0., Qt.NoModifier, button, buttons)
    else:
        event_type = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                      "release": QEvent.MouseButtonRelease}[phase]
        event = QMouseEvent(event_type, position, global_position, button, buttons, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


def escape(canvas):
    QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))


def gesture_points(control):
    if control == "center":
        return QPointF(160, 150), QPointF(187, 131)
    if control == "radius":
        return QPointF(205, 150), QPointF(225, 174)
    return QPointF(110, 134), QPointF(137, 168)


def assert_final_position(modifier, control, final):
    if control == "center":
        assert modifier.center == pytest.approx(final.toTuple())
        assert modifier.radius == 45
    elif control == "radius":
        assert modifier.radius == pytest.approx(math.dist((160, 150), final.toTuple()))
        assert modifier.center == (160, 150)
    else:
        assert modifier.points[0] == pytest.approx(((final.x() - 60) / 200,
                                                  (final.y() - 70) / 160))
        assert modifier.source_points[0] == (.25, .4)


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
@pytest.mark.parametrize("control", ["center", "radius", "pin"])
@pytest.mark.parametrize("move_samples", [False, True], ids=["release-only", "drag-then-release"])
def test_pointer_route_uses_final_release_position_and_one_undo(canvas, stylus, control, move_samples):
    modifier = attach(canvas, control)
    original = canvas.chapter.to_dict()
    undo_count = len(canvas.command_stack._undo)
    start, final = gesture_points(control)
    pointer(canvas, stylus, "press", start)
    assert canvas._modifier_handle_drag["distort"] == modifier.modifier_id
    if move_samples:
        for amount in (.2, .5, .8):
            pointer(canvas, stylus, "move", start + (final - start) * amount)
        # Last move deliberately differs from release; coalesced input must not
        # leave a handle at the last move instead of the pen-up location.
        assert canvas.chapter.to_dict() != original
    pointer(canvas, stylus, "release", final)
    assert_final_position(canvas.chapter.modifiers[modifier.modifier_id], control, final)
    assert canvas._modifier_handle_drag is None
    assert not canvas._pen_contact_active and not canvas._tablet_tool_active
    assert len(canvas.command_stack._undo) == undo_count + 1
    finished = canvas.chapter.to_dict()
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == original
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == finished


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
@pytest.mark.parametrize("control", ["center", "radius", "pin"])
def test_clicking_existing_handle_without_drag_does_not_make_an_undo(canvas, stylus, control):
    attach(canvas, control)
    original = canvas.chapter.to_dict()
    start, _ = gesture_points(control)
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "release", start)
    assert canvas.chapter.to_dict() == original
    assert not canvas.command_stack.can_undo
    assert canvas._modifier_handle_drag is None


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
@pytest.mark.parametrize("control", ["input_angle", "output_angle"])
def test_mirror_angle_handles_follow_rotated_camera_without_click_jump(canvas, stylus, control):
    modifier = DistortModifier(modifier_type="distort_mirror", frame=(60,70,200,160), center=(160,150),
                               parameters={"input_angle": 25., "output_angle": -35.})
    canvas.chapter.add_modifier(modifier, [("object", canvas.selected_object_id)])
    canvas.active_modifier_id = modifier.modifier_id
    initial = canvas.chapter.to_dict()
    widget_point = dict(canvas._distort_handle_points(modifier))[control]
    start = canvas.widget_to_document(widget_point)
    delta = start - QPointF(*modifier.center)
    assert math.degrees(math.atan2(delta.y(),delta.x())) == pytest.approx(modifier.parameters[control])
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "release", start)
    assert canvas.chapter.to_dict() == initial
    assert not canvas.command_stack.can_undo
    angle = math.radians(70)
    final = QPointF(*modifier.center) + QPointF(math.cos(angle),math.sin(angle))*70
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "release", final)
    assert modifier.parameters[control] == pytest.approx(70.)
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == initial


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
@pytest.mark.parametrize("control", ["center", "radius", "pin"])
def test_escape_cancels_pointer_drag_and_late_release_stays_cancelled(canvas, stylus, control):
    modifier = attach(canvas, control)
    original = canvas.chapter.to_dict()
    undo_count = len(canvas.command_stack._undo)
    start, final = gesture_points(control)
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", final)
    assert canvas.chapter.to_dict() != original
    escape(canvas)
    assert canvas._modifier_handle_drag is None
    assert canvas.chapter.to_dict() == original
    pointer(canvas, stylus, "release", final + QPointF(8, 9))
    assert canvas.chapter.to_dict() == original
    assert len(canvas.command_stack._undo) == undo_count
    assert not canvas._pen_contact_active and not canvas._tablet_tool_active
    assert modifier.modifier_id in canvas.chapter.modifiers


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
def test_pointer_adds_pin_at_press_and_moves_it_at_release(canvas, stylus):
    modifier = attach(canvas, "pin")
    canvas.distort_pin_mode = "add"
    original = canvas.chapter.to_dict()
    start, final = QPointF(210, 120), QPointF(235, 150)
    pointer(canvas, stylus, "press", start)
    assert len(modifier.points) == 2
    pointer(canvas, stylus, "release", final)
    assert modifier.source_points[1] == pytest.approx((.75, .3125))
    assert modifier.points[1] == pytest.approx((.875, .5))
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == original


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
def test_pointer_source_pin_edit_preserves_destination(canvas, stylus):
    modifier = attach(canvas, "pin")
    canvas.distort_edit_source = True
    pointer(canvas, stylus, "press", QPointF(110, 134))
    pointer(canvas, stylus, "release", QPointF(130, 150))
    assert modifier.source_points[0] == pytest.approx((.35, .5))
    assert modifier.points[0] == (.25, .4)
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[modifier.modifier_id].source_points[0] == (.25, .4)
