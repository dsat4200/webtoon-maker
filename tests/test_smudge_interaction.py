"""Real mouse/pen routing for smudge drawing, editing, previews and cancellation."""
import copy

import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor

from comic_editor.core.models import DistortModifier
from comic_editor.ui.canvas import ToolKind
from test_distort_pointer_events import canvas, pointer, escape


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)


def attach(canvas):
    canvas.scale, canvas.rotation = 1., 0.
    canvas.tiles.paint_dab(canvas.selected_object_id, QPointF(170, 175), 100,
                           QColor("red"), square=True)
    modifier = DistortModifier(modifier_type="distort_smudge", frame=(60, 70, 350, 300))
    canvas.chapter.add_modifier(modifier, [("object", canvas.selected_object_id)])
    canvas.active_modifier_id = modifier.modifier_id
    return modifier


def draw(canvas, stylus=False):
    points = [QPointF(160, 150), QPointF(195, 125), QPointF(250, 140), QPointF(310, 210)]
    pointer(canvas, stylus, "press", points[0])
    for point in points[1:-1]:
        pointer(canvas, stylus, "move", point)
    pointer(canvas, stylus, "release", points[-1])
    return points


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
def test_draw_preview_fits_two_points_keeps_pressure_and_final_release_and_undo(canvas, stylus):
    modifier = attach(canvas)
    before = canvas.chapter.to_dict()
    pointer(canvas, stylus, "press", QPointF(160, 150))
    pointer(canvas, stylus, "move", QPointF(195, 125))
    assert canvas._modifier_handle_drag["mode"] == "draw"
    assert canvas.chapter.to_dict() == before
    assert canvas._mesh_warp_preview_modifier() is None
    first = canvas.grab().toImage()
    pointer(canvas, stylus, "move", QPointF(250, 140))
    assert canvas.grab().toImage() != first  # Dotted open gesture preview.
    pointer(canvas, stylus, "release", QPointF(310, 210))
    strokes = modifier.parameters["strokes"]
    assert len(strokes) == 1 and len(strokes[0]["points"]) == 2
    stroke = strokes[0]
    assert stroke["points"][0]["position"] == [160, 150]
    assert stroke["points"][1]["position"] == [310, 210]
    assert all(p == pytest.approx(.65 if stylus else 1.) for _, p in stroke["pressure"])
    assert stroke["points"][0]["handle"] != stroke["points"][0]["position"]
    assert canvas.smudge_selected_stroke_id == stroke["id"]
    assert canvas.smudge_selected_point_index == 1
    assert len(canvas.command_stack._undo) == 1
    after = canvas.chapter.to_dict()
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after


@pytest.mark.parametrize("stylus", [False, True])
def test_point_handle_drag_uses_transient_preview_and_cancel_preserves_curve(canvas, stylus):
    modifier = attach(canvas)
    draw(canvas)
    stroke = modifier.parameters["strokes"][0]
    before = canvas.chapter.to_dict()
    original = copy.deepcopy(stroke)
    start = QPointF(*original["points"][1]["position"])
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", start + QPointF(15, 20))
    assert canvas._mesh_warp_preview_modifier() is modifier
    canvas.grab()
    assert canvas._mesh_warp_preview_presented
    assert canvas.smudge_selected_point()["position"] == [325, 230]
    escape(canvas)
    assert canvas.chapter.to_dict() == before
    assert canvas._modifier_handle_drag is None
    assert canvas.active_modifier_id == modifier.modifier_id
    canvas.grab()
    assert not canvas._mesh_warp_preview_presented
    handle = QPointF(*original["points"][1]["handle"])
    pointer(canvas, stylus, "press", handle)
    pointer(canvas, stylus, "move", handle + QPointF(8, 10))
    pointer(canvas, stylus, "release", handle + QPointF(12, 14))
    assert canvas.smudge_selected_point()["handle"] == pytest.approx((handle + QPointF(12, 14)).toTuple())
    assert canvas.smudge_selected_point()["position"] == original["points"][1]["position"]
    assert len(canvas.command_stack._undo) == 2


def test_left_gizmos_edit_selected_point_toggle_type_delete_and_undo(canvas):
    modifier = attach(canvas)
    draw(canvas)
    original = copy.deepcopy(modifier.parameters["strokes"][0])
    rects = canvas._smudge_gizmo_rects()
    flow = rects["flow"]
    canvas._dispatch_tool_press(flow.center(), 1., Qt.NoModifier)
    canvas._tool_move(flow.topLeft(), 1.)
    canvas._tool_release()
    stroke = modifier.parameters["strokes"][0]
    assert stroke["points"][1]["flow"] == 100
    assert stroke["points"][0] == original["points"][0]
    assert len(canvas.command_stack._undo) == 2
    canvas._dispatch_tool_press(rects["point_type"].center(), 1., Qt.NoModifier)
    canvas._tool_release()
    assert canvas.smudge_selected_point()["point_type"] == "vector"
    assert len(canvas.command_stack._undo) == 3
    canvas._dispatch_tool_press(rects["delete"].center(), 1., Qt.NoModifier)
    canvas._tool_release()
    assert not modifier.parameters["strokes"]
    assert len(canvas.command_stack._undo) == 4
    canvas.command_stack.undo()
    assert len(canvas.chapter.modifiers[modifier.modifier_id].parameters["strokes"]) == 1


def test_click_does_not_draw_and_escape_or_tool_switch_cancels_freehand(canvas):
    modifier = attach(canvas)
    pointer(canvas, False, "press", QPointF(160, 150))
    pointer(canvas, False, "release", QPointF(160, 150))
    assert not modifier.parameters["strokes"] and not canvas.command_stack.can_undo
    for cancel in (lambda: escape(canvas), lambda: canvas.set_tool(ToolKind.TRANSFORM)):
        pointer(canvas, False, "press", QPointF(160, 150))
        pointer(canvas, False, "move", QPointF(260, 240))
        cancel()
        assert canvas._modifier_handle_drag is None
        assert not modifier.parameters["strokes"] and not canvas.command_stack.can_undo
