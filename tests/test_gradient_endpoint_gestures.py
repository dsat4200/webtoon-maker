"""Color and mask gradients share endpoint and midpoint pointer behavior."""
import math

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QMouseEvent, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, LineGradientField,
    PathNode, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def canvas(qapp):
    widget = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    chapter = ChapterDocument(height=500)
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 600, 500))
    widget.resize(600, 500)
    widget.set_document(chapter, TileStore())
    widget.scale = 1
    widget.center_x, widget.center_y = 300, 250
    yield widget
    widget._effect_jobs.cancel()
    widget.close()
    widget.deleteLater()


def setup_gradient(canvas, shape, mask):
    obj = ColorFillGradientObject(gradient_shape=shape, line_field=LineGradientField(
        BoundGeometry.path([PathNode(x=150.25, y=200.5), PathNode(x=350.25, y=200.5)], False)))
    if mask:
        owner = ToneMask(saved=True, gradient=obj)
        canvas.chapter.masks[owner.mask_id] = owner
        canvas.set_tone_mask_mode(owner.mask_id)
    else:
        canvas.chapter.add_object(canvas.chapter.root_page_ids[0], obj)
        canvas.set_selection("object", obj.object_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.GRADIENT)
    return obj


def current_gradient(canvas, mask, object_id):
    return canvas.active_mask_gradient() if mask else canvas.chapter.objects[object_id]


def pointer(canvas, stylus, kind, world):
    position = canvas.document_to_widget(world)
    if stylus:
        types = {"press": QEvent.TabletPress, "move": QEvent.TabletMove, "release": QEvent.TabletRelease}
        event = QTabletEvent(types[kind], QPointingDevice.primaryPointingDevice(),
            position, position, 0 if kind == "release" else .7, 0., 0., 0., 0., 0.,
            Qt.NoModifier, Qt.NoButton if kind == "move" else Qt.LeftButton,
            Qt.NoButton if kind == "release" else Qt.LeftButton)
    else:
        types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}
        event = QMouseEvent(types[kind], position, position,
            Qt.NoButton if kind == "move" else Qt.LeftButton,
            Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


@pytest.mark.parametrize("shape", ["linear", "circular"])
@pytest.mark.parametrize("mask", [False, True], ids=["color", "mask"])
@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
@pytest.mark.parametrize("control", ["first", "last", "translate"])
def test_handles_final_release_and_one_undo(canvas, shape, mask, stylus, control):
    obj = setup_gradient(canvas, shape, mask)
    nodes = obj.line_field.geometry.nodes
    original = [QPointF(*node.position) for node in nodes]
    controls = canvas._gradient_control_points(obj)
    assert len(controls) == 3
    key = "translate:" if control == "translate" else "node:" + nodes[0 if control == "first" else -1].node_id
    start = controls[key]
    assert canvas._gradient_control_hit(obj, start)[0] == ("translate" if control == "translate" else "node")
    before = canvas.chapter.to_dict()
    undo_count = len(canvas.command_stack._undo)
    delta = QPointF(27, -33)
    pointer(canvas, stylus, "press", start)
    for fraction in (.25, .5, .75):
        pointer(canvas, stylus, "move", start + delta*fraction)
    pointer(canvas, stylus, "release", start + delta)
    obj = current_gradient(canvas, mask, obj.object_id)
    for i, node in enumerate(obj.line_field.geometry.nodes):
        expected = original[i] + delta if control == "translate" or i == (0 if control == "first" else 1) else original[i]
        assert node.position == pytest.approx(expected.toTuple())
    if control == "translate":
        assert math.dist(*[node.position for node in obj.line_field.geometry.nodes]) == pytest.approx(200)
    assert obj.gradient_shape == shape
    assert len(canvas.command_stack._undo) == undo_count + 1
    assert canvas._mask_gradient_drag is None and canvas._active_gradient_control is None
    assert not canvas._gradient_drag_nodes
    after = canvas.chapter.to_dict()
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after


@pytest.mark.parametrize("mask", [False, True], ids=["color", "mask"])
@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
def test_circular_click_noop_and_escape_restores_midpoint_drag(canvas, mask, stylus):
    obj = setup_gradient(canvas, "circular", mask)
    before = canvas.chapter.to_dict()
    start = canvas._gradient_control_points(obj)["translate:"]
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "release", start)
    assert canvas.chapter.to_dict() == before
    assert not canvas.command_stack.can_undo
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", start + QPointF(45, 20))
    assert canvas.chapter.to_dict() != before
    QTest.keyClick(canvas, Qt.Key_Escape)
    pointer(canvas, stylus, "release", start + QPointF(45, 20))
    assert canvas.chapter.to_dict() == before
    assert not canvas.command_stack.can_undo
    assert not canvas._gradient_drag_nodes


@pytest.mark.parametrize("mask", [False, True], ids=["color", "mask"])
@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
def test_new_circular_gradient_uses_creation_default_and_release(canvas, mask, stylus):
    if mask:
        owner = ToneMask(saved=True)
        canvas.chapter.masks[owner.mask_id] = owner
        canvas.set_tone_mask_mode(owner.mask_id)
    else:
        canvas.set_selection("layer", canvas.chapter.root_page_ids[0], activate_default_tool=False)
    canvas.set_tool(ToolKind.GRADIENT)
    canvas.set_gradient_shape("circular")
    start, end = QPointF(100, 150), QPointF(330, 240)
    before = canvas.chapter.to_dict()
    pointer(canvas, stylus, "press", start)
    pointer(canvas, stylus, "move", QPointF(200, 180))
    pointer(canvas, stylus, "release", end)
    obj = canvas.active_mask_gradient() if mask else next(iter(canvas.chapter.objects.values()))
    assert obj.gradient_shape == "circular"
    assert [n.position for n in obj.line_field.geometry.nodes] == [start.toTuple(), end.toTuple()]
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before


def test_circular_midpoint_preserves_dormant_curve_under_parent_transform(canvas):
    obj = setup_gradient(canvas, "circular", False)
    page = canvas.chapter.layers[obj.parent_layer_id]
    page.transform_frame = (0, 0, 600, 500)
    page.transform_quad = [(25, 40), (565, 80), (545, 480), (5, 440)]
    obj.line_field.geometry = BoundGeometry.path([
        PathNode(x=150, y=200, point_type="bezier", outgoing=(170, 180)),
        PathNode(x=250, y=270),
        PathNode(x=350, y=200, point_type="bezier", incoming=(330, 180)),
    ], False)
    original = [(n.position, n.incoming, n.outgoing) for n in obj.line_field.geometry.nodes]
    start = canvas._gradient_control_points(obj)["translate:"]
    end = start + QPointF(27, -33)
    local_delta = canvas._gradient_world_to_local(obj, end)-canvas._gradient_world_to_local(obj, start)
    pointer(canvas, False, "press", start)
    pointer(canvas, False, "move", start + QPointF(15, -20))
    pointer(canvas, False, "release", end)
    for node, initial in zip(obj.line_field.geometry.nodes, original):
        for value, old in zip((node.position, node.incoming, node.outgoing), initial):
            assert value == (None if old is None else pytest.approx((QPointF(*old)+local_delta).toTuple()))
