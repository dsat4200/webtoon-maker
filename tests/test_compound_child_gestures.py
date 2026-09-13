"""Pointer editing preserves an open compound contributor and its sibling text."""
import math

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import QImage, QMouseEvent, QPointingDevice, QTabletEvent

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode, ShapeStyle, TextObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def compound_child(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=620)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 620))
    page.fill_color, page.border_width = None, 0
    # Keep the saved scene's large local coordinates, rounded bubble, and
    # tapered three-point tail; place the parent in a small test viewport.
    bound = BoundGeometry.rectangle(150, 22707.050112752455, 420, 212.94988724754512)
    for node in bound.nodes:
        node.roundness, node.roundness_enabled = 106.47494362377256, True
    bubble = chapter.add_layer(page.layer_id, "Bubble", bound, style=ShapeStyle(
        primary_color="#FFFFFFFF", outline_color="#FF000000", outline_thickness=4))
    bubble.compound_enabled = True
    bubble.ignore_parent_mask = True
    bubble.transform_frame = (150, 22707.050112752455, 420, 212.94988724754512)
    bubble.transform_quad = [(80, 100), (500, 100), (500, 312.94988724754512),
                             (80, 312.94988724754512)]
    tail = chapter.add_layer(bubble.layer_id, "Tail", BoundGeometry.path([
        PathNode(x=360, y=22800, width_multiplier=10),
        PathNode(x=437.95839514474926, y=23085.813649243533,
                 width_multiplier=3.2, outline_multiplier=1.3566307516629306),
        PathNode(x=646.9583951447493, y=23022.813649243533,
                 width_multiplier=.1, outline_multiplier=1.5223502053141933),
    ], closed=False), layer_kind="open_shape", style=ShapeStyle(
        primary_color="#FFFFFFFF", base_thickness=12,
        outline_color="#FF000000", outline_thickness=4))
    text = TextObject(text="The tail should move.", font_family=text_outline_font_family,
                      font_size=24, margin=36)
    chapter.add_object(bubble.layer_id, text)
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(900, 650)
    canvas.set_document(chapter, TileStore(), reset_view=False)
    canvas.scale = 1
    canvas.set_selection("layer", tail.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    yield canvas, bubble.layer_id, tail.layer_id, text.object_id
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def send_pointer(canvas, stylus, kind, position):
    if stylus:
        types = {"press": QEvent.TabletPress, "move": QEvent.TabletMove,
                 "release": QEvent.TabletRelease}
        event = QTabletEvent(types[kind], QPointingDevice.primaryPointingDevice(),
            position, position, 0 if kind == "release" else .7, 0., 0., 0., 0., 0.,
            Qt.NoModifier, Qt.NoButton if kind == "move" else Qt.LeftButton,
            Qt.NoButton if kind == "release" else Qt.LeftButton)
    else:
        types = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove,
                 "release": QEvent.MouseButtonRelease}
        event = QMouseEvent(types[kind], position, position,
            Qt.NoButton if kind == "move" else Qt.LeftButton,
            Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier)
    QCoreApplication.sendEvent(canvas, event)


def rendered_pixels(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height,
                   QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
@pytest.mark.parametrize("control", ["node", "translate", "thickness", "outline_width"])
def test_compound_child_release_and_single_undo_preserve_parent_and_text(
    compound_child, stylus, control,
):
    canvas, bubble_id, tail_id, text_id = compound_child
    tail = canvas.chapter.layers[tail_id]
    node_index = 2 if control == "outline_width" else 1
    node = tail.bound.nodes[node_index]
    canvas._selected_shape_node_id = node.node_id
    transform = canvas.layer_world_transform(tail_id)
    inverse, valid = transform.inverted()
    assert valid
    if control == "translate":
        canvas.set_tool(ToolKind.TRANSFORM)
        left, top, width, height = tail.bound.bbox()
        start_world = transform.map(QPointF(left + width * .3, top + height * .4))
        start = canvas.document_to_widget(start_world)
        delta = QPointF(23, -17)
    elif control == "node":
        start = canvas.document_to_widget(transform.map(QPointF(*node.position)))
        delta = QPointF(23, -17)
    else:
        display = canvas._shape_handle_display_transform(node)
        handle = canvas._shape_gizmo_positions(tail.bound, node)[control]
        start = display.map(handle)
        direction = start - display.map(QPointF(*node.position))
        delta = direction / math.hypot(direction.x(), direction.y()) * 20
    final = start + delta
    before_image = rendered_pixels(canvas)
    before = canvas.chapter.to_dict()
    parent_before = canvas.chapter.layers[bubble_id].to_dict()
    text_before = canvas.chapter.objects[text_id].to_dict()
    original_nodes = [item.to_dict() for item in tail.bound.nodes]
    original_world = [transform.map(QPointF(*item.position)) for item in tail.bound.nodes]
    undo_count = len(canvas.command_stack._undo)

    send_pointer(canvas, stylus, "press", start)
    if control == "translate":
        assert canvas._geometry_transform_target is not None
        assert canvas._transform_drag_mode == "translate"
    else:
        assert canvas._active_shape_control == control
    for fraction in (.2, .4, .6):
        send_pointer(canvas, stylus, "move", start + delta * fraction)
    # The release endpoint is deliberately absent from the motion packets.
    send_pointer(canvas, stylus, "release", final)
    tail = canvas.chapter.layers[tail_id]
    if control == "node":
        world = canvas.widget_to_document(final)
        expected = inverse.map(QPointF(round(world.x()), round(world.y()))).toTuple()
        assert tail.bound.nodes[1].position == pytest.approx(expected)
        assert tail.bound.nodes[0].to_dict() == original_nodes[0]
        assert tail.bound.nodes[2].to_dict() == original_nodes[2]
    elif control == "translate":
        world_delta = canvas.widget_to_document(final) - canvas.widget_to_document(start)
        actual_transform = canvas.layer_world_transform(tail_id)
        for current, original in zip(tail.bound.nodes, original_world):
            assert actual_transform.map(QPointF(*current.position)).toTuple() == pytest.approx(
                (original + world_delta).toTuple())
    else:
        attribute = "width_multiplier" if control == "thickness" else "outline_multiplier"
        assert getattr(tail.bound.nodes[node_index], attribute) == pytest.approx(
            getattr(PathNode.from_dict(original_nodes[node_index]), attribute) + 2)
        assert [item.position for item in tail.bound.nodes] == [
            tuple(item["position"]) for item in original_nodes]
    assert canvas.chapter.layers[bubble_id].to_dict() == parent_before
    assert canvas.chapter.objects[text_id].to_dict() == text_before
    assert tail.parent_id == bubble_id and tail.compound_operation == "add"
    assert canvas.selected_kind == "layer" and canvas.selected_id == tail_id
    assert canvas._active_shape_control is None and canvas._geometry_transform_target is None
    assert canvas._outline_pending_point is None
    assert len(canvas.command_stack._undo) == undo_count + 1
    after = canvas.chapter.to_dict()
    after_image = rendered_pixels(canvas)
    assert not np.array_equal(before_image, after_image)
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    np.testing.assert_array_equal(rendered_pixels(canvas), before_image)
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after
    np.testing.assert_array_equal(rendered_pixels(canvas), after_image)


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
def test_clicking_fractional_compound_child_node_does_not_move_or_add_undo(
    compound_child, stylus,
):
    canvas, _, tail_id, _ = compound_child
    tail = canvas.chapter.layers[tail_id]
    node = tail.bound.nodes[1]
    point = canvas.document_to_widget(
        canvas.layer_world_transform(tail_id).map(QPointF(*node.position)))
    before = canvas.chapter.to_dict()
    undo_count = len(canvas.command_stack._undo)
    send_pointer(canvas, stylus, "press", point)
    assert canvas._active_shape_control == "node"
    send_pointer(canvas, stylus, "release", point)
    assert canvas.chapter.to_dict() == before
    assert len(canvas.command_stack._undo) == undo_count


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "stylus"])
@pytest.mark.parametrize("mode", ["handle", "rotate"], ids=["uniform-resize", "rotate"])
def test_compound_child_transform_uses_final_release_and_preserves_undo(
    compound_child, stylus, mode,
):
    canvas, bubble_id, tail_id, text_id = compound_child
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.settings.transform_mode = "uniform"
    tail = canvas.chapter.layers[tail_id]
    transform = canvas.layer_world_transform(tail_id)
    left, top, width, height = tail.bound.bbox()
    corners = [(left, top), (left + width, top),
               (left + width, top + height), (left, top + height)]
    quad = [transform.map(QPointF(*point)).toTuple() for point in corners]
    handles, rotation_handle, pivot = canvas._transform_control_points(quad)
    if mode == "handle":
        start_world = QPointF(*handles[2])
        anchor = QPointF(*quad[0])
        final_world = anchor + (start_world - anchor) * 1.18

        def expected_point(point):
            return anchor + (point - anchor) * 1.18
    else:
        start_world = rotation_handle
        angle = math.radians(17)

        def expected_point(point):
            offset = point - pivot
            return pivot + QPointF(
                offset.x() * math.cos(angle) - offset.y() * math.sin(angle),
                offset.x() * math.sin(angle) + offset.y() * math.cos(angle))

        final_world = expected_point(start_world)
    start, final = map(canvas.document_to_widget, (start_world, final_world))
    before_image = rendered_pixels(canvas)
    before = canvas.chapter.to_dict()
    parent_before = canvas.chapter.layers[bubble_id].to_dict()
    text_before = canvas.chapter.objects[text_id].to_dict()
    original_nodes = [node.to_dict() for node in tail.bound.nodes]
    expected_world = [expected_point(transform.map(QPointF(*node.position)))
                      for node in tail.bound.nodes]
    undo_count = len(canvas.command_stack._undo)
    send_pointer(canvas, stylus, "press", start)
    assert canvas._transform_drag_mode == mode
    assert canvas._geometry_transform_target == ("layer_group", tail_id)
    send_pointer(canvas, stylus, "move", start + (final - start) * .6)
    send_pointer(canvas, stylus, "release", final)
    tail = canvas.chapter.layers[tail_id]
    actual_transform = canvas.layer_world_transform(tail_id)
    for node, expected in zip(tail.bound.nodes, expected_world):
        assert actual_transform.map(QPointF(*node.position)).toTuple() == pytest.approx(
            expected.toTuple(), abs=1e-6)
    assert [node.to_dict() for node in tail.bound.nodes] == original_nodes
    assert canvas.chapter.layers[bubble_id].to_dict() == parent_before
    assert canvas.chapter.objects[text_id].to_dict() == text_before
    assert canvas._geometry_transform_target is None
    assert len(canvas.command_stack._undo) == undo_count + 1
    after = canvas.chapter.to_dict()
    after_image = rendered_pixels(canvas)
    assert not np.array_equal(before_image, after_image)
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    np.testing.assert_array_equal(rendered_pixels(canvas), before_image)
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after
    np.testing.assert_array_equal(rendered_pixels(canvas), after_image)
