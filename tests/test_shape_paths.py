from __future__ import annotations

import json
import math

import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPointingDevice, QTabletEvent,
)
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core import settings as settings_module
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, PathNode, RasterObject, ShapeStyle,
)
from comic_editor.core.settings import EditorSettings, load_settings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _canvas():
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    layer = chapter.add_layer(
        page.layer_id, "Layer 1",
        BoundGeometry.rectangle(40, 40, 700, 700),
    )
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(1000, 800)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id, False)
    return canvas, chapter, page, layer


def _send_tablet(
    canvas, event_type, position, pressure,
    button=Qt.NoButton, buttons=Qt.NoButton,
):
    global_position = QPointF(
        canvas.mapToGlobal(position.toPoint())
    )
    event = QTabletEvent(
        event_type, QPointingDevice.primaryPointingDevice(),
        position, global_position, pressure,
        0.0, 0.0, 0.0, 0.0, 0.0,
        Qt.NoModifier, button, buttons,
    )
    QCoreApplication.sendEvent(canvas, event)


def test_shape_creation_finish_and_global_style_gizmos(qapp):
    canvas, chapter, page, _layer = _canvas()
    canvas.show()
    try:
        canvas.set_tool(ToolKind.SHAPE_CREATE)
        canvas._creation_nodes = [
            PathNode(x=180, y=180), PathNode(x=420, y=260),
        ]
        canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
        canvas._creation_style = ShapeStyle(
            primary_color="#334455", base_thickness=12,
            outline_color="#abcdef", outline_thickness=5,
        )
        canvas._creation_parent_id = page.layer_id
        canvas._creation_insertion_index = 0

        geometry = canvas._shape_overlay_geometry()
        assert geometry is not None
        assert "finish" in geometry["buttons"]
        assert set(geometry["handles"]) == {
            "base_thickness", "outline_thickness",
        }
        finish_rect = geometry["buttons"]["finish"][0]
        assert canvas.rect().contains(finish_rect.toAlignedRect())

        stroke = geometry["handles"]["base_thickness"]
        assert canvas._begin_shape_overlay_interaction(stroke)
        canvas._update_shape_property_drag(stroke + QPointF(40, 0))
        assert canvas._creation_style.base_thickness == 22
        assert canvas._finish_shape_property_drag()

        outline = canvas._shape_overlay_geometry()["handles"]["outline_thickness"]
        assert canvas._begin_shape_overlay_interaction(outline)
        canvas._update_shape_property_drag(outline + QPointF(800, 0))
        assert canvas._creation_style.outline_thickness == 100
        assert canvas._cancel_shape_property_drag()
        assert canvas._creation_style.outline_thickness == 5

        before_commands = len(canvas.command_stack._undo)
        assert canvas._begin_shape_overlay_interaction(finish_rect.center())
        created = chapter.layers[canvas.selected_id]
        assert created.layer_kind == "open_shape"
        assert created.bound.closed is False
        assert created.shape_style.base_thickness == 22
        assert created.shape_style.outline_thickness == 5
        assert created.compound_operation == "add"
        assert len(canvas.command_stack._undo) == before_commands + 1
        canvas.command_stack.undo()
        assert created.layer_id not in canvas.chapter.layers
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_shape_edit_global_style_and_nested_compound_gizmos(qapp):
    canvas, chapter, _page, root = _canvas()
    root.compound_enabled = True
    intermediary = chapter.add_layer(
        root.layer_id, "Nested", BoundGeometry.rectangle(80, 80, 500, 500)
    )
    line = chapter.add_layer(
        intermediary.layer_id, "Line",
        BoundGeometry.path([
            PathNode(x=100, y=140), PathNode(x=360, y=140),
        ], False),
        layer_kind="open_shape",
        style=ShapeStyle(base_thickness=20, outline_thickness=4),
    )
    root_id = root.layer_id
    intermediary_id = intermediary.layer_id
    line_id = line.layer_id
    canvas.set_selection("layer", line.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas.show()
    try:
        geometry = canvas._shape_overlay_geometry()
        assert geometry["context"]["compound_parent"].layer_id == root.layer_id
        assert geometry["buttons"]["compound"][1] == "Add"

        before_commands = len(canvas.command_stack._undo)
        assert canvas._begin_shape_overlay_interaction(
            geometry["buttons"]["compound"][0].center()
        )
        assert line.compound_operation == "subtract"
        assert len(canvas.command_stack._undo) == before_commands + 1
        canvas.command_stack.undo()
        line = canvas.chapter.layers[line_id]
        assert line.compound_operation == "add"

        geometry = canvas._shape_overlay_geometry()
        stroke = geometry["handles"]["base_thickness"]
        before_commands = len(canvas.command_stack._undo)
        assert canvas._begin_shape_overlay_interaction(stroke)
        assert canvas._finish_shape_property_drag()
        assert len(canvas.command_stack._undo) == before_commands

        before_commands = len(canvas.command_stack._undo)
        assert canvas._begin_shape_overlay_interaction(stroke)
        canvas._update_shape_property_drag(stroke - QPointF(800, 0))
        assert canvas.chapter.layers[line_id].shape_style.base_thickness == 0
        assert canvas._finish_shape_property_drag()
        assert len(canvas.command_stack._undo) == before_commands + 1
        canvas.command_stack.undo()
        assert canvas.chapter.layers[line_id].shape_style.base_thickness == 20

        chapter = canvas.chapter
        root = chapter.layers[root_id]
        intermediary = chapter.layers[intermediary_id]
        closed = chapter.add_layer(
            root.layer_id, "Closed", BoundGeometry.rectangle(120, 120, 90, 90)
        )
        canvas.set_selection("layer", closed.layer_id, False)
        assert set(canvas._shape_overlay_geometry()["handles"]) == {
            "outline_thickness"
        }

        intermediary.compound_operation = "ignore"
        canvas.set_selection("layer", line_id, False)
        assert "compound" not in canvas._shape_overlay_geometry()["buttons"]
    finally:
        canvas.hide()
        canvas.deleteLater()


def test_zero_width_open_shape_and_compound_creation_preview(qapp):
    canvas, chapter, _page, root = _canvas()
    root.compound_enabled = True
    root.fill_color = "#ff0000"
    canvas.set_selection("layer", root.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=200, y=250), PathNode(x=820, y=250),
    ]
    canvas._creation_style = ShapeStyle(base_thickness=40, outline_thickness=30)
    canvas._creation_parent_id = root.layer_id
    canvas._creation_insertion_index = 0

    geometry = BoundGeometry.path(canvas._creation_nodes, False)
    assert geometry.closed is False
    original, added = canvas._creation_compound_preview_paths(
        geometry, canvas._creation_style
    )
    assert not original.contains(QPointF(800, 250))
    assert added.contains(QPointF(800, 250))

    canvas._creation_compound_operation = "subtract"
    original, subtracted = canvas._creation_compound_preview_paths(
        geometry, canvas._creation_style
    )
    assert original.contains(QPointF(250, 250))
    assert not subtracted.contains(QPointF(250, 250))

    canvas._creation_compound_operation = "ignore"
    assert canvas._creation_compound_preview_paths(
        geometry, canvas._creation_style
    ) is None

    assert CanvasWidget.open_shape_mesh(geometry, 0, 0).isEmpty()
    assert not CanvasWidget.open_shape_mesh(geometry, 0, 20).isEmpty()
    canvas._creation_compound_operation = "subtract"
    canvas._finish_shape(False)
    created = canvas.chapter.layers[canvas.selected_id]
    assert created.parent_id == root.layer_id
    assert created.compound_operation == "subtract"
    assert created.shape_style.base_thickness == 40
    assert created.shape_style.outline_thickness == 30
    canvas.deleteLater()


def test_path_node_shape_style_round_trip_and_open_leaf_invariants():
    chapter = ChapterDocument()
    page = chapter.add_page()
    path = BoundGeometry.path([
        PathNode(x=10, y=20, width_multiplier=0.3),
        PathNode(
            x=100, y=80, point_type="bezier",
            incoming=(70, 30), width_multiplier=2.4,
        ),
    ])
    line = chapter.add_layer(
        page.layer_id, "Line", path, layer_kind="open_shape",
        style=ShapeStyle(
            primary_color="#123456", base_thickness=14,
            outline_color="#abcdef", outline_thickness=3,
            start_cap="point", end_cap="square",
        ),
    )
    loaded = ChapterDocument.from_dict(chapter.to_dict())
    result = loaded.layers[line.layer_id]
    assert loaded.schema_version == 14
    assert result.layer_kind == "open_shape"
    assert result.bound.closed is False
    assert result.bound.nodes[1].incoming == (70, 30)
    assert result.bound.nodes[1].width_multiplier == 2.4
    assert result.shape_style.end_cap == "square"
    loaded.add_layer(line.layer_id)
    loaded.add_object(line.layer_id, RasterObject())
    loaded.validate()


def test_migrate_v3_rectangle_radius_fill_and_border():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id)
    data = chapter.to_dict()
    data["schema_version"] = 3
    item = next(
        candidate for candidate in data["layers"]
        if candidate["id"] == layer.layer_id
    )
    item["bound"] = {
        "type": "rect", "points": [[10, 20], [310, 220]],
    }
    item.pop("shape_style")
    item.update({
        "fill_color": "#112233", "border_width": 7,
        "border_color": "#445566", "vertex_radius": 18,
    })
    loaded = ChapterDocument.from_dict(data)
    migrated = loaded.layers[layer.layer_id]
    assert migrated.bound.primitive == "rectangle"
    assert len(migrated.bound.nodes) == 4
    assert {node.roundness for node in migrated.bound.nodes} == {18}
    assert migrated.shape_style.primary_color == "#112233"
    assert migrated.shape_style.outline_thickness == 7
    assert migrated.shape_style.outline_color == "#445566"


@pytest.mark.parametrize(
    ("legacy", "primitive", "node_count", "roundness"),
    [
        (
            {"type": "circle", "points": [[200, 200], [300, 200]]},
            "ellipse", 4, 0,
        ),
        (
            {
                "type": "polygon",
                "points": [[20, 20], [220, 40], [120, 240]],
            },
            "custom", 3, 16,
        ),
    ],
)
def test_migrate_v3_circle_and_polygon(
    legacy, primitive, node_count, roundness,
):
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id)
    data = chapter.to_dict()
    data["schema_version"] = 3
    item = next(
        candidate for candidate in data["layers"]
        if candidate["id"] == layer.layer_id
    )
    item["bound"] = legacy
    item.pop("shape_style")
    item["vertex_radius"] = 16
    loaded = ChapterDocument.from_dict(data)
    bound = loaded.layers[layer.layer_id].bound
    assert bound.primitive == primitive
    assert len(bound.nodes) == node_count
    assert max(node.roundness for node in bound.nodes) == roundness


def test_reject_malformed_bezier_handles():
    with pytest.raises(ValueError, match="incoming"):
        BoundGeometry.path([
            PathNode(x=0, y=0),
            PathNode(
                x=100, y=0, point_type="bezier",
                outgoing=(120, 0),
            ),
        ])


def test_variable_width_open_shape_outline_and_caps_render(qapp):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    line = chapter.add_layer(
        page.layer_id, "Line",
        BoundGeometry.path([
            PathNode(x=100, y=500, width_multiplier=0.5),
            PathNode(x=900, y=500, width_multiplier=2.0),
        ]),
        layer_kind="open_shape",
        style=ShapeStyle(
            primary_color="#ff0000", base_thickness=40,
            outline_color="#0000ff", outline_thickness=10,
            start_cap="point", end_cap="round",
        ),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    image = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    assert image.pixelColor(500, 500).red() > 200
    assert image.pixelColor(500, 530).blue() > 200
    start_height = sum(
        image.pixelColor(120, y).alpha() > 0
        and image.pixelColor(120, y).lightness() < 240
        for y in range(440, 561)
    )
    end_height = sum(
        image.pixelColor(850, y).alpha() > 0
        and image.pixelColor(850, y).lightness() < 240
        for y in range(400, 601)
    )
    assert end_height > start_height
    assert line.bound.closed is False


def test_open_bezier_with_unlocked_rounding_renders(qapp):
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page()
    path = BoundGeometry.path([
        PathNode(x=100, y=400),
        PathNode(
            x=500, y=300, point_type="bezier",
            incoming=(350, 550), outgoing=(650, 100),
            handles_locked=False, roundness=30,
        ),
        PathNode(x=900, y=600),
    ])
    chapter.add_layer(
        page.layer_id, "Curve", path, layer_kind="open_shape",
        style=ShapeStyle(primary_color="#111111", base_thickness=12),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    image = QImage(540, 540, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    assert any(
        image.pixelColor(x, y).lightness() < 80
        for y in range(100, 400, 10)
        for x in range(40, 500, 10)
    )


def test_shape_creation_open_close_and_bezier_drag(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    for point in (QPointF(100, 100), QPointF(300, 100)):
        widget = canvas.document_to_widget(point)
        canvas._tool_press(widget, 1)
        canvas._tool_release()
    third = canvas.document_to_widget(QPointF(300, 300))
    dragged = canvas.document_to_widget(QPointF(260, 260))
    canvas._tool_press(third, 1)
    canvas._tool_move(dragged, 1)
    canvas._tool_release()
    canvas._finish_shape(False)
    open_layer = chapter.layers[canvas.selected_id]
    assert open_layer.layer_kind == "open_shape"
    assert canvas.tool == ToolKind.SHAPE_EDIT
    assert open_layer.bound.nodes[-1].point_type == "bezier"
    assert open_layer.bound.nodes[-1].incoming is not None
    assert open_layer.bound.nodes[-1].outgoing is None
    assert open_layer.shape_style.outline_color == canvas.primary_color
    assert open_layer.shape_style.primary_color == canvas.secondary_color
    assert open_layer.shape_style.outline_thickness == 4

    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    for point in (
        QPointF(400, 400), QPointF(600, 400), QPointF(500, 600),
    ):
        widget = canvas.document_to_widget(point)
        canvas._tool_press(widget, 1)
        canvas._tool_release()
    canvas._tool_press(canvas.document_to_widget(QPointF(400, 400)), 1)
    canvas._tool_release()
    closed = chapter.layers[canvas.selected_id]
    assert closed.layer_kind == "bounded"
    assert closed.bound.closed is True
    assert closed.fill_color == canvas.secondary_color
    assert closed.shape_style.outline_color == canvas.primary_color
    assert closed.shape_style.outline_thickness == 4
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_free_shape_close_hit_precedes_handles_and_gradient_lines_stay_open(qapp):
    canvas, _chapter, page, _layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    first = PathNode(
        x=100, y=100, point_type="bezier", outgoing=(100, 100)
    )
    canvas._creation_nodes = [
        first, PathNode(x=300, y=100), PathNode(x=250, y=300),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id

    hit = canvas._creation_hit_test(QPointF(100, 100))
    assert hit is not None
    assert hit["kind"] == "node"
    assert hit["node_id"] == first.node_id
    assert hit["close"] is True
    assert "close" in canvas._shape_hit_tooltip(
        BoundGeometry.path(canvas._creation_nodes, False), hit
    ).lower()

    canvas._gradient_creation_parent_id = page.layer_id
    canvas._gradient_creation_type = "line"
    hit = canvas._creation_hit_test(QPointF(100, 100))
    assert not (hit and hit.get("close"))


def test_free_shape_close_allows_pointer_jitter_but_drag_moves_first_point(qapp):
    canvas, chapter, page, _layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100), PathNode(x=300, y=100),
        PathNode(x=250, y=300),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
    start = canvas.document_to_widget(QPointF(100, 100))
    canvas._tool_press(start, 1.0)
    assert canvas._creation_close_candidate
    jitter = max(1, QApplication.startDragDistance() - 1)
    canvas._tool_move(start + QPointF(jitter, 0), 1.0)
    canvas._tool_release()
    closed = chapter.layers[canvas.selected_id]
    assert closed.bound.closed
    assert not canvas._creation_nodes

    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100), PathNode(x=300, y=100),
        PathNode(x=250, y=300),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
    start = canvas.document_to_widget(QPointF(100, 100))
    canvas._tool_press(start, 1.0)
    drag = QApplication.startDragDistance() + 5
    canvas._tool_move(start + QPointF(drag, 0), 1.0)
    canvas._tool_release()
    assert len(canvas._creation_nodes) == 3
    assert canvas._creation_nodes[0].x > 100
    assert not canvas._creation_close_candidate


def test_free_shape_stylus_jitter_still_closes(qapp):
    canvas, chapter, page, _layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100), PathNode(x=300, y=100),
        PathNode(x=250, y=300),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
    canvas.show()
    qapp.processEvents()
    start = canvas.document_to_widget(QPointF(100, 100))
    jitter = max(1, QApplication.startDragDistance() - 1)
    _send_tablet(
        canvas, QEvent.TabletPress, start, 0.6,
        Qt.LeftButton, Qt.LeftButton,
    )
    _send_tablet(canvas, QEvent.TabletMove, start + QPointF(jitter, 0), 0.6)
    _send_tablet(
        canvas, QEvent.TabletRelease, start + QPointF(jitter, 0), 0.0,
        Qt.LeftButton,
    )
    closed = chapter.layers[canvas.selected_id]
    assert closed.bound.closed
    assert not canvas._creation_nodes


def test_custom_page_shape_closes_from_first_anchor(qapp):
    canvas, _chapter, page, _layer = _canvas()
    finished = []
    canvas.pageCreationFinished.connect(
        lambda bound, before, anchor: finished.append((bound, before, anchor))
    )
    assert canvas.begin_page_creation(page.layer_id, "custom")
    canvas._creation_nodes = [
        PathNode(x=100, y=1200), PathNode(x=300, y=1200),
        PathNode(x=250, y=1400),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
    start = canvas.document_to_widget(QPointF(100, 1200))
    canvas._tool_press(start, 1.0)
    canvas._tool_release()
    assert len(finished) == 1
    assert finished[0][0].closed
    assert finished[0][2] == page.layer_id


def test_shape_draft_cleanup_and_no_document_pointer_events_are_safe(qapp):
    canvas, _chapter, _page, _layer = _canvas()
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100), PathNode(x=300, y=100),
        PathNode(x=250, y=300),
    ]
    canvas._creation_active_control = "draft_node"
    canvas._creation_close_candidate = True
    canvas._pen_contact_active = True
    canvas._tablet_tool_active = True

    state = canvas.capture_session_state()
    assert state is not None
    assert not canvas._creation_nodes
    assert canvas._creation_active_control is None
    canvas.restore_session_state(state)
    assert not canvas._creation_nodes

    canvas.clear_document()
    assert canvas.chapter is None
    canvas._tool_press(QPointF(10, 10), 1.0)
    canvas._tool_move(QPointF(20, 20), 1.0)
    canvas._tool_release()
    canvas._pen_contact_active = True
    canvas._tablet_tool_active = True
    _send_tablet(canvas, QEvent.TabletMove, QPointF(20, 20), 0.5)
    assert canvas.chapter is None
    assert not canvas._pen_contact_active
    assert not canvas._tablet_tool_active


@pytest.mark.parametrize(
    ("tool", "end"),
    [
        (ToolKind.BOX_BOUND, QPointF(260, 220)),
        (ToolKind.CIRCLE_BOUND, QPointF(220, 100)),
    ],
)
def test_primitive_creation_uses_active_primary_and_secondary(qapp, tool, end):
    canvas, chapter, page, layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(tool)
    start = QPointF(100, 100)
    canvas._tool_press(canvas.document_to_widget(start), 1)
    canvas._tool_move(canvas.document_to_widget(end), 1)
    canvas._tool_release()
    created = chapter.layers[canvas.selected_id]
    assert created.shape_style.outline_color == canvas.primary_color
    assert created.fill_color == canvas.secondary_color
    assert created.shape_style.outline_thickness == 4


def test_shape_edit_width_cap_insert_and_primitive_conversion(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=300, y=300),
    ], False)
    layer.layer_kind = "open_shape"
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    selected = layer.bound.nodes[0]
    canvas._selected_shape_node_id = selected.node_id
    positions = canvas._shape_gizmo_positions(layer.bound, selected)
    thickness = positions["thickness"]
    canvas._tool_press(canvas.document_to_widget(thickness), 1)
    direction = thickness - QPointF(selected.x, selected.y)
    length = max(1.0, (direction.x() ** 2 + direction.y() ** 2) ** 0.5)
    target = thickness + direction / length * (10 / canvas.scale)
    canvas._tool_move(canvas.document_to_widget(target), 1)
    canvas._tool_release()
    assert selected.width_multiplier == pytest.approx(2.0)
    cap = canvas._shape_gizmo_positions(layer.bound, selected)["cap"]
    canvas._tool_press(canvas.document_to_widget(cap), 1)
    assert layer.shape_style.start_cap == "point"

    canvas._update_shape_hover(QPointF(200, 100))
    assert canvas._shape_hover_insert is not None
    canvas._tool_press(canvas.document_to_widget(QPointF(200, 100)), 1)
    canvas._tool_release()
    assert len(layer.bound.nodes) == 4

    layer.layer_kind = "bounded"
    layer.bound = BoundGeometry.rectangle(50, 50, 300, 200)
    canvas._selected_shape_node_id = ""
    canvas.primitiveConversionRequested.connect(
        lambda _primitive: canvas.resolve_primitive_conversion(True)
    )
    canvas._update_shape_hover(QPointF(150, 50))
    canvas._tool_press(canvas.document_to_widget(QPointF(150, 50)), 1)
    canvas._tool_release()
    assert layer.bound.primitive == "custom"
    assert len(layer.bound.nodes) == 5


def test_rectangle_mouse_resize_updates_connected_vertices(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.rectangle(50, 60, 300, 200)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas.show()
    qapp.processEvents()
    start = canvas.document_to_widget(QPointF(50, 60)).toPoint()
    target = canvas.document_to_widget(QPointF(90, 100)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, target)
    assert layer.bound.primitive == "rectangle"
    assert layer.bound.points == pytest.approx([
        (90, 100), (350, 100), (350, 260), (90, 260),
    ])


def test_rectangle_free_corner_and_edge_drags_are_undoable(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.settings.rectangle_edit_mode = "free"
    layer.bound = BoundGeometry.rectangle(100, 100, 300, 200)
    for index, node in enumerate(layer.bound.nodes):
        node.roundness = 10 + index
    canvas.set_tool(ToolKind.SHAPE_EDIT)

    corner = canvas.document_to_widget(QPointF(100, 100))
    canvas._tool_press(corner, 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(70, 80)), 1)
    canvas._tool_release()
    assert layer.bound.points == pytest.approx([
        (70, 80), (400, 100), (400, 300), (100, 300),
    ])
    assert [node.roundness for node in layer.bound.nodes] == [10, 11, 12, 13]

    top_midpoint = (
        QPointF(*layer.bound.nodes[0].position)
        + QPointF(*layer.bound.nodes[1].position)
    ) / 2
    canvas._tool_press(canvas.document_to_widget(top_midpoint), 1)
    canvas._tool_move(
        canvas.document_to_widget(top_midpoint + QPointF(40, 25)), 1
    )
    canvas._tool_release()
    for actual, expected in zip(layer.bound.points, [
        (110, 105), (440, 125), (400, 300), (100, 300),
    ]):
        assert actual == pytest.approx(expected)
    assert layer.bound.primitive == "rectangle"
    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id]
    for actual, expected in zip(restored.bound.points, [
        (70, 80), (400, 100), (400, 300), (100, 300),
    ]):
        assert actual == pytest.approx(expected)
    canvas.command_stack.redo()
    restored = canvas.chapter.layers[layer.layer_id]
    for actual, expected in zip(restored.bound.points, [
        (110, 105), (440, 125), (400, 300), (100, 300),
    ]):
        assert actual == pytest.approx(expected)


@pytest.mark.parametrize("edge", range(4))
def test_rectangle_free_midpoint_moves_only_attached_pair(qapp, edge):
    canvas, chapter, page, layer = _canvas()
    canvas.settings.rectangle_edit_mode = "free"
    layer.bound = BoundGeometry.rectangle(100, 100, 300, 200)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    before = list(layer.bound.points)
    first, second = edge, (edge + 1) % 4
    midpoint = (
        QPointF(*before[first]) + QPointF(*before[second])
    ) / 2
    canvas._tool_press(canvas.document_to_widget(midpoint), 1)
    canvas._tool_move(
        canvas.document_to_widget(midpoint + QPointF(30, -20)), 1
    )
    canvas._tool_release()
    for index, point in enumerate(layer.bound.points):
        expected = (
            (before[index][0] + 30, before[index][1] - 20)
            if index in {first, second} else before[index]
        )
        assert point == pytest.approx(expected)


def test_rectangle_normal_mode_scales_a_skewed_free_quad(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.rectangle(100, 100, 300, 200)
    layer.bound.nodes[0].position = (70, 80)
    layer.bound.nodes[1].position = (430, 120)
    layer.bound.nodes[2].position = (400, 320)
    layer.bound.nodes[3].position = (90, 280)
    canvas.settings.rectangle_edit_mode = "normal"
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    left, top, width, height = layer.bound.bbox()
    right_midpoint = QPointF(left + width, top + height / 2)
    canvas._tool_press(canvas.document_to_widget(right_midpoint), 1)
    canvas._tool_move(
        canvas.document_to_widget(right_midpoint + QPointF(100, 0)), 1
    )
    canvas._tool_release()
    assert layer.bound.primitive == "rectangle"
    assert layer.bound.nodes[0].y == pytest.approx(80)
    assert layer.bound.nodes[1].y == pytest.approx(120)
    assert layer.bound.nodes[0].x == pytest.approx(70)
    assert layer.bound.nodes[1].x > 430
    assert layer.bound.nodes[2].x > 400
    assert layer.bound.nodes[3].x > 90


def test_rectangle_free_corner_and_edge_use_grid_snapping(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.settings.rectangle_edit_mode = "free"
    canvas.settings.snap_to_grid = True
    layer.bound = BoundGeometry.rectangle(90, 90, 300, 210)
    canvas.set_tool(ToolKind.SHAPE_EDIT)

    canvas._tool_press(canvas.document_to_widget(QPointF(90, 90)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(143, 167)), 1)
    canvas._tool_release()
    assert layer.bound.nodes[0].position == pytest.approx((150, 180))

    midpoint = (
        QPointF(*layer.bound.nodes[0].position)
        + QPointF(*layer.bound.nodes[1].position)
    ) / 2
    original = list(layer.bound.points)
    canvas._tool_press(canvas.document_to_widget(midpoint), 1)
    canvas._tool_move(
        canvas.document_to_widget(midpoint + QPointF(26, 34)), 1
    )
    canvas._tool_release()
    moved_midpoint = (
        QPointF(*layer.bound.nodes[0].position)
        + QPointF(*layer.bound.nodes[1].position)
    ) / 2
    assert moved_midpoint.toTuple() == pytest.approx(
        chapter.grid.snap(midpoint.x() + 26, midpoint.y() + 34)
    )
    first_delta = (
        layer.bound.nodes[0].x - original[0][0],
        layer.bound.nodes[0].y - original[0][1],
    )
    second_delta = (
        layer.bound.nodes[1].x - original[1][0],
        layer.bound.nodes[1].y - original[1][1],
    )
    assert first_delta == pytest.approx(second_delta)


@pytest.mark.parametrize(
    ("index", "target", "expected_bbox"),
    [
        (0, (-20, -10), (-20, -10, 320, 210)),
        (1, (320, -10), (0, -10, 320, 210)),
        (2, (320, 220), (0, 0, 320, 220)),
        (3, (-20, 220), (-20, 0, 320, 220)),
        (4, (150, -10), (0, -10, 300, 210)),
        (5, (320, 100), (0, 0, 320, 200)),
        (6, (150, 220), (0, 0, 300, 220)),
        (7, (-20, 100), (-20, 0, 320, 200)),
    ],
)
def test_every_rectangle_handle_preserves_primitive_and_ids(
    qapp, index, target, expected_bbox,
):
    bound = BoundGeometry.rectangle(0, 0, 300, 200)
    original = BoundGeometry.from_dict(bound.to_dict())
    ids = [node.node_id for node in bound.nodes]
    CanvasWidget._move_bound_handle(
        bound, index, QPointF(*target), original
    )
    assert bound.primitive == "rectangle"
    assert [node.node_id for node in bound.nodes] == ids
    assert bound.bbox() == pytest.approx(expected_bbox)
    left, top, width, height = bound.bbox()
    assert bound.points == pytest.approx([
        (left, top), (left + width, top),
        (left + width, top + height), (left, top + height),
    ])


def test_rectangle_radius_gizmos_hit_drag_and_preserve_primitive(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.rectangle(50, 50, 300, 200)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    positions = canvas._rectangle_radius_positions(layer.bound)
    assert len(positions) == 4
    for index, position in enumerate(positions):
        hit = canvas._shape_hit_test(layer.bound, position)
        assert hit["kind"] == "radius"
        assert hit["index"] == index
    canvas.show()
    qapp.processEvents()
    start = canvas.document_to_widget(positions[0]).toPoint()
    target = canvas.document_to_widget(QPointF(100, 100)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, target)
    assert layer.bound.primitive == "rectangle"
    assert layer.bound.nodes[0].roundness == pytest.approx(50, abs=2)
    assert [node.roundness for node in layer.bound.nodes[1:]] == [0, 0, 0]


def test_rectangle_radius_gizmo_screen_offset_is_zoom_independent(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.rectangle(50, 50, 300, 200)
    distances = []
    for scale in (0.5, 1.0, 2.5):
        canvas.scale = scale
        position = canvas._rectangle_radius_positions(layer.bound)[0]
        corner = QPointF(*layer.bound.nodes[0].position)
        distances.append(
            (canvas.document_to_widget(position)
             - canvas.document_to_widget(corner)).manhattanLength()
        )
    assert distances[0] == pytest.approx(distances[1], abs=0.01)
    assert distances[1] == pytest.approx(distances[2], abs=0.01)


def test_point_hover_prevents_insertion_preview(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=300, y=300),
    ], True)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._update_shape_hover(QPointF(100, 100))
    assert canvas._shape_hover_target["kind"] == "node"
    assert canvas._shape_hover_insert is None
    canvas._tool_press(canvas.document_to_widget(QPointF(100, 100)), 1)
    canvas._tool_release()
    assert canvas._selected_shape_node_id == layer.bound.nodes[0].node_id
    assert len(layer.bound.nodes) == 3


def test_primitive_insert_waits_for_confirmation_and_undoes(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.circle(200, 150, 100)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    point = canvas.bound_path(layer.bound).pointAtPercent(0.12)
    requested = []
    canvas.primitiveConversionRequested.connect(requested.append)
    canvas._tool_press(canvas.document_to_widget(point), 1)
    assert requested == ["ellipse"]
    assert layer.bound.primitive == "ellipse"
    canvas.resolve_primitive_conversion(False)
    assert layer.bound.primitive == "ellipse"

    canvas._tool_press(canvas.document_to_widget(point), 1)
    canvas.resolve_primitive_conversion(True)
    canvas._tool_release()
    assert layer.bound.primitive == "custom"
    assert len(layer.bound.nodes) == 5
    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id]
    assert restored.bound.primitive == "ellipse"
    assert len(restored.bound.nodes) == 4


def test_tablet_buttonless_move_drags_draft_point(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=300, y=300),
    ]
    canvas.show()
    qapp.processEvents()
    start = canvas.document_to_widget(QPointF(300, 100))
    target = canvas.document_to_widget(QPointF(340, 140))
    _send_tablet(
        canvas, QEvent.TabletPress, start, 0.6,
        Qt.LeftButton, Qt.LeftButton,
    )
    _send_tablet(canvas, QEvent.TabletMove, target, 0.6)
    _send_tablet(
        canvas, QEvent.TabletRelease, target, 0.0, Qt.LeftButton,
    )
    assert canvas._creation_nodes[1].position == pytest.approx((340, 140))
    assert len(canvas._creation_nodes) == 3


def test_mouse_drag_existing_draft_point_does_not_add_point(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=300, y=300),
    ]
    canvas.show()
    qapp.processEvents()
    start = canvas.document_to_widget(QPointF(300, 100)).toPoint()
    target = canvas.document_to_widget(QPointF(360, 140)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, start)
    QTest.mouseMove(canvas, target)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, target)
    assert canvas._creation_nodes[1].position == pytest.approx((360, 140))
    assert len(canvas._creation_nodes) == 3


def test_new_bound_sibling_and_page_child_placement(qapp):
    canvas, chapter, page, first = _canvas()
    second = chapter.add_layer(
        page.layer_id, "Second",
        BoundGeometry.rectangle(100, 100, 100, 100),
    )
    canvas.set_selection("layer", second.layer_id, False)
    canvas._create_layer_from_world_bound(
        BoundGeometry.rectangle(200, 200, 100, 100)
    )
    children = [
        reference.entity_id for reference in page.children
        if reference.kind == "layer"
    ]
    created = canvas.selected_id
    assert children == [first.layer_id, created, second.layer_id]
    canvas.set_selection("layer", page.layer_id, False)
    canvas._create_layer_from_world_bound(
        BoundGeometry.circle(400, 400, 50)
    )
    assert page.children[0].entity_id == canvas.selected_id


def test_point_type_lock_roundness_and_constant_screen_gizmos(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=500, y=250),
    ], False)
    layer.layer_kind = "open_shape"
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    node = layer.bound.nodes[1]
    canvas._selected_shape_node_id = node.node_id
    type_gizmo = canvas._shape_gizmo_positions(
        layer.bound, node
    )["type"]
    canvas._tool_press(canvas.document_to_widget(type_gizmo), 1)
    assert node.point_type == "bezier"
    assert node.handles_locked
    assert node.incoming is not None and node.outgoing is not None
    assert "roundness" not in canvas._shape_gizmo_positions(
        layer.bound, node
    )
    lock_gizmo = canvas._shape_gizmo_positions(
        layer.bound, node
    )["lock"]
    canvas._tool_press(canvas.document_to_widget(lock_gizmo), 1)
    assert node.handles_locked is False
    assert "roundness" in canvas._shape_gizmo_positions(layer.bound, node)

    node.width_multiplier = 1.0
    canvas.scale = 0.5
    first = canvas._shape_gizmo_positions(layer.bound, node)["thickness"]
    first_distance = (
        canvas.document_to_widget(first)
        - canvas.document_to_widget(QPointF(node.x, node.y))
    ).manhattanLength()
    canvas.scale = 2.0
    second = canvas._shape_gizmo_positions(layer.bound, node)["thickness"]
    second_distance = (
        canvas.document_to_widget(second)
        - canvas.document_to_widget(QPointF(node.x, node.y))
    ).manhattanLength()
    assert first_distance == pytest.approx(second_distance, abs=0.01)


def test_closed_seam_bezier_has_undoable_unlock_and_smoothness_gizmo(qapp):
    canvas, chapter, page, layer = _canvas()
    seam = PathNode(
        x=150, y=100, point_type="bezier",
        incoming=(100, 150), outgoing=(200, 50),
        handles_locked=True, roundness=32, roundness_enabled=True,
    )
    layer.bound = BoundGeometry.path([
        seam,
        PathNode(x=500, y=120),
        PathNode(x=300, y=500),
    ], True)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = seam.node_id
    positions = canvas._shape_gizmo_positions(layer.bound, seam)
    assert "lock" in positions
    assert "roundness" not in positions
    assert math.dist(
        positions["lock"].toTuple(), positions["type"].toTuple()
    ) > 45 / canvas.scale
    lock_hit = canvas._shape_hit_test(
        layer.bound, positions["lock"] + QPointF(14 / canvas.scale, 0)
    )
    assert lock_hit["name"] == "lock"
    canvas._update_shape_hover(positions["lock"])
    assert canvas.toolTip() == "Unlock Bézier handles"
    original_handles = (seam.incoming, seam.outgoing)

    canvas._tool_press(canvas.document_to_widget(positions["lock"]), 1)
    assert seam.handles_locked is False
    assert (seam.incoming, seam.outgoing) == original_handles
    assert "roundness" in canvas._shape_gizmo_positions(layer.bound, seam)
    assert seam.roundness == 32
    canvas._update_shape_hover(
        canvas._shape_gizmo_positions(layer.bound, seam)["lock"]
    )
    assert canvas.toolTip() == "Lock Bézier handles"
    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id].bound.nodes[0]
    assert restored.handles_locked is True


def test_bezier_padlock_states_render_distinctly_and_screen_stably(qapp):
    images = []
    for locked in (True, False):
        image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        CanvasWidget._draw_bezier_lock_gizmo(
            painter, QPointF(32, 32), locked, 1.0
        )
        painter.end()
        images.append(image)
        assert any(
            image.pixelColor(x, y).alpha() > 0
            for y in range(image.height())
            for x in range(image.width())
        )
    assert bytes(images[0].constBits()) != bytes(images[1].constBits())

    canvas, chapter, page, layer = _canvas()
    node = PathNode(
        x=200, y=200, point_type="bezier",
        incoming=(150, 200), outgoing=(250, 200),
    )
    layer.bound = BoundGeometry.path([
        PathNode(x=50, y=100), node, PathNode(x=400, y=300),
    ])
    distances = []
    for scale in (0.5, 2.0):
        canvas.scale = scale
        point = canvas._shape_gizmo_positions(layer.bound, node)["lock"]
        distances.append((
            canvas.document_to_widget(point)
            - canvas.document_to_widget(QPointF(node.x, node.y))
        ).manhattanLength())
    assert distances[0] == pytest.approx(distances[1], abs=0.01)


@pytest.mark.parametrize(
    ("incoming_curve", "outgoing_curve"),
    [(True, False), (False, True), (True, True)],
)
def test_vector_smoothness_matches_adjacent_curve_tangents(
    incoming_curve, outgoing_curve,
):
    previous = (
        PathNode(
            x=0, y=150, point_type="bezier",
            outgoing=(90, 20), handles_locked=False,
        )
        if incoming_curve else PathNode(x=0, y=150)
    )
    corner = PathNode(
        x=150, y=150, roundness=60, roundness_enabled=True
    )
    following = (
        PathNode(
            x=300, y=150, point_type="bezier",
            incoming=(210, 280), handles_locked=False,
        )
        if outgoing_curve else PathNode(x=300, y=150)
    )
    path = CanvasWidget.bound_path(
        BoundGeometry.path([previous, corner, following], False)
    )
    elements = [path.elementAt(index) for index in range(path.elementCount())]
    assert len(elements) == 10
    points = [QPointF(element.x, element.y) for element in elements]
    entry, fillet_in, fillet_out, exit_point = (
        points[3], points[4], points[5], points[6]
    )

    def assert_same_tangent(first: QPointF, second: QPointF) -> None:
        first_length = math.hypot(first.x(), first.y())
        second_length = math.hypot(second.x(), second.y())
        assert first_length > 1e-6 and second_length > 1e-6
        cross = first.x() * second.y() - first.y() * second.x()
        assert cross / (first_length * second_length) == pytest.approx(
            0, abs=1e-6
        )
        assert QPointF.dotProduct(first, second) > 0

    assert_same_tangent(entry - points[2], fillet_in - entry)
    assert_same_tangent(
        exit_point - fillet_out, points[7] - exit_point
    )
    assert math.dist(
        (entry.x(), entry.y()), (fillet_in.x(), fillet_in.y())
    ) <= math.dist(corner.position, (entry.x(), entry.y())) * 2 / 3 + 1e-6
    assert math.dist(
        (exit_point.x(), exit_point.y()),
        (fillet_out.x(), fillet_out.y()),
    ) <= math.dist(corner.position, (exit_point.x(), exit_point.y())) * 2 / 3 + 1e-6


def test_vector_to_vector_smoothness_geometry_is_unchanged():
    corner = PathNode(
        x=100, y=0, roundness=20, roundness_enabled=True
    )
    path = CanvasWidget.bound_path(BoundGeometry.path([
        PathNode(x=0, y=0), corner, PathNode(x=100, y=100),
    ], False))
    points = [
        (path.elementAt(index).x, path.elementAt(index).y)
        for index in range(path.elementCount())
    ]
    expected = [
        (0, 0), (80, 0), (93.3333333333, 0),
        (100, 6.6666666667), (100, 20), (100, 100),
    ]
    assert len(points) == len(expected)
    for actual, target in zip(points, expected):
        assert actual == pytest.approx(target)


def test_bezier_controls_use_global_grid_snap_in_create_and_edit(qapp):
    canvas, chapter, page, layer = _canvas()
    canvas.settings.snap_to_grid = True
    node = PathNode(
        x=300, y=100, point_type="bezier",
        incoming=(240, 160), outgoing=(360, 40),
        handles_locked=False,
    )
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100), node, PathNode(x=500, y=200),
    ])
    layer.layer_kind = "open_shape"
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = node.node_id
    canvas._tool_press(canvas.document_to_widget(QPointF(*node.incoming)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(143, 167)), 1)
    canvas._tool_release()
    assert node.incoming == pytest.approx((150, 180))

    canvas.set_selection("layer", page.layer_id, False)
    canvas.set_tool(ToolKind.SHAPE_CREATE)
    draft = PathNode(
        x=300, y=100, point_type="bezier",
        incoming=(240, 160), outgoing=(360, 40),
        handles_locked=False,
    )
    canvas._creation_nodes = [
        PathNode(x=100, y=100), draft, PathNode(x=500, y=200),
    ]
    canvas._creation_selected_node_id = draft.node_id
    canvas._tool_press(canvas.document_to_widget(QPointF(*draft.incoming)), 1)
    canvas._tool_move(canvas.document_to_widget(QPointF(143, 167)), 1)
    canvas._tool_release()
    assert draft.incoming == pytest.approx((150, 180))


def test_delete_point_gizmo_is_undoable_and_respects_minimum(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.rectangle(100, 100, 300, 200)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    selected = layer.bound.nodes[0]
    canvas._selected_shape_node_id = selected.node_id
    position = canvas._shape_gizmo_positions(
        layer.bound, selected
    )["delete"]
    hit = canvas._shape_hit_test(layer.bound, position)
    assert hit["kind"] == "gizmo"
    assert hit["name"] == "delete"

    canvas._tool_press(canvas.document_to_widget(position), 1)
    canvas._tool_release()
    assert layer.bound.primitive == "custom"
    assert len(layer.bound.nodes) == 3
    canvas._selected_shape_node_id = layer.bound.nodes[0].node_id
    assert "delete" not in canvas._shape_gizmo_positions(
        layer.bound, layer.bound.nodes[0]
    )

    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id]
    assert restored.bound.primitive == "rectangle"
    assert len(restored.bound.nodes) == 4


def test_roundness_click_toggles_and_preserves_sharp_zero(qapp):
    canvas, chapter, page, layer = _canvas()
    node = PathNode(
        x=300, y=200, point_type="bezier",
        incoming=(220, 280), outgoing=(390, 120),
        handles_locked=False,
    )
    layer.bound = BoundGeometry.path([
        PathNode(x=80, y=180), node, PathNode(x=560, y=320),
    ])
    layer.layer_kind = "open_shape"
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = node.node_id

    position = canvas._shape_gizmo_positions(
        layer.bound, node
    )["roundness"]
    canvas._tool_press(canvas.document_to_widget(position), 1)
    canvas._tool_release()
    assert node.roundness_enabled
    assert node.roundness == 0

    position = canvas._shape_gizmo_positions(
        layer.bound, node
    )["roundness"]
    canvas._tool_press(canvas.document_to_widget(position), 1)
    canvas._tool_release()
    assert not node.roundness_enabled
    assert node.roundness == 0

    canvas._tool_press(canvas.document_to_widget(position), 1)
    canvas._tool_release()
    assert node.roundness_enabled
    assert node.roundness == 0

    sharp = CanvasWidget.bound_path(layer.bound)
    assert sharp.elementCount() == 7


def test_closed_seam_vector_converts_to_valid_bezier(qapp):
    canvas, chapter, page, layer = _canvas()
    seam = PathNode(x=100, y=100)
    layer.bound = BoundGeometry.path([
        seam,
        PathNode(x=500, y=100),
        PathNode(x=300, y=500),
    ], True)

    canvas._toggle_shape_node_type(layer.bound, seam)

    assert seam.point_type == "bezier"
    assert seam.incoming is not None
    assert seam.outgoing is not None
    assert seam.incoming == pytest.approx((
        seam.x * 2 - seam.outgoing[0],
        seam.y * 2 - seam.outgoing[1],
    ))
    chapter.to_dict()


def test_handle_normalization_repairs_topology_without_changing_valid_handles():
    first = PathNode(
        x=0, y=0, point_type="bezier",
        incoming=None, outgoing=(50, 0), handles_locked=False,
    )
    middle = PathNode(
        x=100, y=0, point_type="bezier",
        incoming=(75, 0), outgoing=(125, 0), handles_locked=False,
    )
    last = PathNode(
        x=200, y=0, point_type="bezier",
        incoming=(175, 0), outgoing=None, handles_locked=False,
    )
    bound = BoundGeometry.path([first, middle, last], False)
    middle_handles = (middle.incoming, middle.outgoing)
    first.incoming = (-50, 0)
    last.outgoing = (250, 0)

    bound.normalize_bezier_handles()

    assert first.incoming is None
    assert first.outgoing == (50, 0)
    assert (middle.incoming, middle.outgoing) == middle_handles
    assert last.incoming == (175, 0)
    assert last.outgoing is None
    bound.validate()


def test_handle_validation_error_identifies_the_point():
    malformed = PathNode(
        node_id="broken-node", x=0, y=0, point_type="bezier",
        incoming=(-20, 0), outgoing=(20, 0), handles_locked=False,
    )
    bound = BoundGeometry.path([
        malformed, PathNode(x=100, y=0), PathNode(x=50, y=100),
    ], True)
    malformed.incoming = None

    with pytest.raises(
        ValueError,
        match=r"incoming B.zier handle at contour 0, point 0.*broken-node"
    ):
        bound.validate()


def test_unlocked_bezier_roundness_is_curve_aware_and_progressive():
    node = PathNode(
        x=500, y=300, point_type="bezier",
        incoming=(350, 550), outgoing=(650, 100),
        handles_locked=False, roundness=30, roundness_enabled=True,
    )
    bound = BoundGeometry.path([
        PathNode(x=100, y=400), node, PathNode(x=900, y=600),
    ])

    entry_distances = []
    for radius in (30, 120):
        node.roundness = radius
        path = CanvasWidget.bound_path(bound)
        assert path.elementCount() == 13
        elements = [path.elementAt(index) for index in range(path.elementCount())]
        anchor_indices = [
            index for index, element in enumerate(elements)
            if (element.x, element.y) == pytest.approx(node.position)
        ]
        assert anchor_indices == [6]
        anchor_index = anchor_indices[0]
        incoming_control = QPointF(
            elements[anchor_index - 1].x, elements[anchor_index - 1].y
        )
        outgoing_control = QPointF(
            elements[anchor_index + 1].x, elements[anchor_index + 1].y
        )
        incoming = QPointF(node.x, node.y) - incoming_control
        outgoing = outgoing_control - QPointF(node.x, node.y)
        assert (
            incoming.x() * outgoing.y() - incoming.y() * outgoing.x()
        ) == pytest.approx(0, abs=1e-6)
        assert QPointF.dotProduct(incoming, outgoing) > 0
        assert math.hypot(incoming.x(), incoming.y()) == pytest.approx(
            math.hypot(outgoing.x(), outgoing.y())
        )
        entry = QPointF(elements[3].x, elements[3].y)
        entry_distances.append(math.dist(node.position, (entry.x(), entry.y())))
    assert entry_distances[1] > entry_distances[0] * 3

    node.roundness_enabled = False
    sharp = CanvasWidget.bound_path(bound)
    assert sharp.elementCount() == 7


def test_closed_shapes_hide_nonfunctional_width_gizmo():
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=200, y=300),
    ], True)
    node = layer.bound.nodes[1]
    assert "thickness" not in canvas._shape_gizmo_positions(
        layer.bound, node
    )
    layer.bound.closed = False
    layer.layer_kind = "open_shape"
    assert "thickness" in canvas._shape_gizmo_positions(
        layer.bound, node
    )


@pytest.mark.parametrize("endpoint_index", [0, -1])
def test_open_bezier_endpoints_never_create_forbidden_handles(
    qapp, endpoint_index,
):
    canvas, chapter, page, layer = _canvas()
    layer.layer_kind = "open_shape"
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=180),
        PathNode(x=500, y=100),
    ], False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    node = layer.bound.nodes[endpoint_index]
    canvas._selected_shape_node_id = node.node_id

    type_gizmo = canvas._shape_gizmo_positions(
        layer.bound, node
    )["type"]
    canvas._tool_press(canvas.document_to_widget(type_gizmo), 1)
    assert node.point_type == "bezier"
    assert node.handles_locked is False
    if endpoint_index == 0:
        assert node.incoming is None and node.outgoing is not None
        handle_name, handle = "outgoing", node.outgoing
    else:
        assert node.incoming is not None and node.outgoing is None
        handle_name, handle = "incoming", node.incoming

    canvas._tool_press(canvas.document_to_widget(QPointF(*handle)), 1)
    target = QPointF(handle[0] + 35, handle[1] + 20)
    canvas._tool_move(canvas.document_to_widget(target), 1)
    canvas._tool_release()
    assert getattr(node, handle_name) == pytest.approx(
        target.toTuple(), abs=0.6
    )
    if endpoint_index == 0:
        assert node.incoming is None
    else:
        assert node.outgoing is None
    layer.bound.validate()
    chapter.to_dict()

    cap = canvas._shape_gizmo_positions(layer.bound, node)["cap"]
    canvas._tool_press(canvas.document_to_widget(cap), 1)
    chapter.to_dict()


def test_shape_gizmo_labels_tooltips_and_enlarged_hit_targets(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.layer_kind = "open_shape"
    node = PathNode(
        x=300, y=200, point_type="bezier",
        incoming=(240, 200), outgoing=(360, 200),
        handles_locked=False,
    )
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100), node, PathNode(x=500, y=300),
    ], False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._selected_shape_node_id = node.node_id

    type_position = canvas._shape_gizmo_positions(
        layer.bound, node
    )["type"]
    canvas._update_shape_hover(type_position)
    assert canvas.toolTip() == "Convert this point to Vector"

    thickness_position = canvas._shape_gizmo_positions(
        layer.bound, node
    )["thickness"]
    canvas._update_shape_hover(thickness_position)
    assert canvas.toolTip() == (
        "Drag to adjust stroke thickness at this point"
    )

    control_probe = QPointF(
        node.outgoing[0] + 20 / canvas.scale,
        node.outgoing[1],
    )
    hit = canvas._shape_hit_test(layer.bound, control_probe)
    assert hit["kind"] == "control"
    assert hit["name"] == "outgoing"
    canvas._update_shape_hover(QPointF(*node.outgoing))
    assert canvas.toolTip() == "Drag the outgoing Bézier handle"


def test_round_cap_is_one_tangent_aligned_ribbon_subpath():
    bound = BoundGeometry.path([
        PathNode(x=100, y=100, width_multiplier=1),
        PathNode(x=300, y=100, width_multiplier=1),
    ], False)
    mesh = CanvasWidget.open_shape_mesh(
        bound, 20, start_cap="round", end_cap="round"
    )
    bounds = mesh.boundingRect()
    assert bounds.left() == pytest.approx(90)
    assert bounds.right() == pytest.approx(310)
    assert bounds.top() == pytest.approx(90)
    assert bounds.bottom() == pytest.approx(110)
    assert sum(
        mesh.elementAt(index).type == QPainterPath.MoveToElement
        for index in range(mesh.elementCount())
    ) == 1
    assert mesh.contains(QPointF(91, 100))
    assert mesh.contains(QPointF(309, 100))


@pytest.mark.parametrize("start_cap", ["point", "square", "round"])
@pytest.mark.parametrize("end_cap", ["point", "square", "round"])
def test_open_shape_mesh_supports_every_endpoint_cap_pair(
    start_cap, end_cap,
):
    bound = BoundGeometry.path([
        PathNode(x=100, y=100, width_multiplier=0.75),
        PathNode(x=300, y=130, width_multiplier=1.25),
    ], False)

    mesh = CanvasWidget.open_shape_mesh(
        bound, 24, start_cap=start_cap, end_cap=end_cap,
    )

    assert not mesh.isEmpty()
    assert mesh.boundingRect().isValid()


def test_roundness_enabled_migrates_from_saved_radius():
    rounded = PathNode.from_dict({
        "position": [10, 20], "roundness": 12,
    })
    sharp = PathNode.from_dict({
        "position": [10, 20], "roundness": 0,
    })
    assert rounded.roundness_enabled
    assert not sharp.roundness_enabled
    assert rounded.to_dict()["roundness_enabled"] is True


def test_node_drag_moves_selected_node_with_control_points(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(
            x=300, y=100, point_type="bezier",
            incoming=(250, 150), outgoing=(350, 50),
        ),
        PathNode(x=300, y=300),
    ], closed=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    layer_x, layer_y = chapter.layer_world_translation(layer.layer_id)
    node = layer.bound.nodes[1]
    press = QPointF(layer_x + node.x, layer_y + node.y)
    assert canvas._begin_shape_edit(press)
    assert canvas._active_shape_control == "node"

    canvas._update_shape_edit(QPointF(
        layer_x + node.x + 40, layer_y + node.y - 25
    ))

    assert layer.bound.nodes[1].position == pytest.approx((340, 75))
    assert layer.bound.nodes[1].incoming == pytest.approx((290, 125))
    assert layer.bound.nodes[1].outgoing == pytest.approx((390, 25))
    assert layer.bound.nodes[0].position == (100, 100)
    assert layer.bound.nodes[2].position == (300, 300)
    canvas._tool_release()
    assert canvas._active_shape_control is None


def test_shift_selected_nodes_drag_together(qapp):
    canvas, chapter, page, layer = _canvas()
    layer.bound = BoundGeometry.path([
        PathNode(x=100, y=100),
        PathNode(x=300, y=100),
        PathNode(x=300, y=300),
    ], closed=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    layer_x, layer_y = chapter.layer_world_translation(layer.layer_id)
    first, second = layer.bound.nodes[0], layer.bound.nodes[1]
    canvas._selected_shape_node_ids = {
        first.node_id, second.node_id,
    }
    assert canvas._begin_shape_edit(QPointF(
        layer_x + first.x, layer_y + first.y
    ))
    assert canvas._active_shape_control == "node"
    assert len(canvas._shape_drag_nodes) == 2

    canvas._update_shape_edit(QPointF(
        layer_x + first.x + 50, layer_y + first.y + 30
    ))

    assert first.position == pytest.approx((150, 130))
    assert second.position == pytest.approx((350, 130))
    assert layer.bound.nodes[2].position == (300, 300)
    canvas._tool_release()
    assert canvas._active_shape_control is None


def test_shapes_category_stays_open_after_tool_choice(qapp):
    window = MainWindow()
    try:
        assert not window.shapes_category.isExpanded()
        assert not window.shapes_category.header.isCheckable()
        assert window.shapes_category.header.objectName() == "shapeCategoryHeader"
        window.shapes_category.header.click()
        assert window.shapes_category.isExpanded()
        assert not window.shapes_category.contents.isHidden()
        window.shape_tool_buttons[ToolKind.SHAPE_CREATE].click()
        assert window.shapes_category.isExpanded()
        assert not window.shapes_category.contents.isHidden()
        window.shapes_category.header.click()
        assert not window.shapes_category.isExpanded()
        assert window.shapes_category.contents.isHidden()
    finally:
        window.deleteLater()


def test_bound_edit_hotkey_migrates_to_shape_edit(monkeypatch, tmp_path):
    path = tmp_path / "settings.json"
    path.write_text(json.dumps({
        "settings_version": 3,
        "hotkeys": {"bound_edit": "Ctrl+B"},
    }), encoding="utf-8")
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)
    loaded = load_settings()
    assert loaded.settings_version == 12
    assert loaded.hotkeys["shape_edit"] == "Ctrl+B"
    assert "bound_edit" not in loaded.hotkeys
