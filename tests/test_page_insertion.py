from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPointingDevice, QTabletEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ImageObject, PathNode,
    RasterObject, TextObject, VectorDrawingObject, VectorStroke,
    VectorStrokePoint,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _tablet_event(event_type, widget, position, pressure, buttons):
    global_position = QPointF(widget.mapToGlobal(position.toPoint()))
    return QTabletEvent(
        event_type,
        QPointingDevice.primaryPointingDevice(),
        position,
        global_position,
        pressure,
        0.0, 0.0, 0.0, 0.0, 0.0,
        Qt.NoModifier,
        Qt.LeftButton,
        buttons,
    )


def test_add_page_supports_root_insertion_index():
    chapter = ChapterDocument()
    first = chapter.add_page("First")
    third = chapter.add_page("Third")
    second = chapter.add_page("Second", index=1)

    assert chapter.root_page_ids == [
        first.layer_id, second.layer_id, third.layer_id,
    ]
    chapter.validate()


def test_new_page_outline_defaults_and_page_limit():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    assert page.border_width == 4
    page.border_width = 200
    chapter.validate()
    assert page.border_width == 40


def test_add_page_starts_shape_creation_without_gap_prompt(qapp, monkeypatch):
    window = MainWindow()
    chapter = ChapterDocument(height=1200)
    active = chapter.add_page(
        "Page 1", BoundGeometry.rectangle(0, 0, 500, 100)
    )
    lower = chapter.add_page(
        "Page 2", BoundGeometry.rectangle(0, 0, 500, 100), y=250
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", active.layer_id)
    monkeypatch.setattr(window, "_choose_page_shape", lambda: "rectangle")
    try:
        window._add_page()
        assert window.canvas._page_creation_anchor_id == active.layer_id
        assert window.canvas.page_gap_transaction() is None
        assert window.page_gap_confirmation.isHidden()
        assert chapter.layers[lower.layer_id].translate_y == 250
        assert window.canvas._finish_pending_page_bound(
            BoundGeometry.rectangle(0, 120, 500, 100)
        )

        new_id = window.canvas.selected_id
        assert chapter.root_page_ids == [
            active.layer_id, new_id, lower.layer_id,
        ]
        assert chapter.layers[lower.layer_id].translate_y == 250
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        window.canvas.command_stack.undo()
        assert window.chapter.root_page_ids == [
            active.layer_id, lower.layer_id,
        ]
        assert window.chapter.layers[lower.layer_id].translate_y == 250
    finally:
        window._dirty = False
        window.close()


def test_page_creation_rejects_open_or_above_anchor(qapp):
    chapter = ChapterDocument(height=1200)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", active.layer_id)
    messages: list[str] = []
    canvas.pageCreationInvalid.connect(messages.append)

    assert canvas.begin_page_creation(active.layer_id, "custom")
    assert not canvas._finish_pending_page_bound(
        BoundGeometry.path([
            PathNode(x=0, y=250),
            PathNode(x=100, y=250),
            PathNode(x=100, y=350),
        ], closed=False)
    )
    assert not canvas._finish_pending_page_bound(
        BoundGeometry.rectangle(0, 100, 100, 100)
    )
    assert messages[-1].startswith("Draw the new page")


def test_rectangle_circle_and_first_point_custom_page_completion(qapp):
    for kind in ("rectangle", "circle"):
        chapter = ChapterDocument(height=1400)
        active = chapter.add_page(
            bound=BoundGeometry.rectangle(0, 0, 400, 200)
        )
        canvas = CanvasWidget(EditorSettings())
        canvas.resize(800, 700)
        canvas.set_document(chapter, TileStore())
        finished = []
        canvas.pageCreationFinished.connect(
            lambda *values: finished.append(values)
        )
        assert canvas.begin_page_creation(active.layer_id, kind)
        start = (
            QPointF(150, 450)
            if kind == "circle" else QPointF(50, 300)
        )
        end = (
            QPointF(250, 450)
            if kind == "circle" else QPointF(250, 500)
        )
        canvas._tool_press(
            canvas.document_to_widget(start), 1
        )
        canvas._tool_move(
            canvas.document_to_widget(end), 1
        )
        canvas._tool_release()
        assert len(finished) == 1
        assert finished[0][0].closed

    chapter = ChapterDocument(height=1400)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 700)
    canvas.set_document(chapter, TileStore())
    finished = []
    canvas.pageCreationFinished.connect(
        lambda *values: finished.append(values)
    )
    assert canvas.begin_page_creation(active.layer_id, "custom")
    points = [
        QPointF(50, 300), QPointF(250, 300), QPointF(150, 500),
    ]
    for point in points:
        canvas._tool_press(canvas.document_to_widget(point), 1)
        canvas._tool_release()
    canvas._tool_press(canvas.document_to_widget(points[0]), 1)
    canvas._tool_release()
    assert len(finished) == 1
    assert finished[0][0].closed


def test_failed_page_ack_keeps_draft_available_for_retry(qapp):
    chapter = ChapterDocument(height=1400)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 700)
    canvas.set_document(chapter, TileStore())
    failures = []

    def reject(*args):
        del args
        failures.append(True)
        canvas.resolve_page_creation(False, "Insertion failed")

    canvas.pageCreationFinished.connect(reject)
    assert canvas.begin_page_creation(active.layer_id, "rectangle")
    canvas._tool_press(
        canvas.document_to_widget(QPointF(50, 300)), 1
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(250, 500)), 1
    )
    canvas._tool_release()

    assert failures == [True]
    assert canvas._page_creation_anchor_id == active.layer_id
    assert canvas._page_creation_draft is not None
    assert canvas._page_creation_committing is False
    assert len(canvas._creation_points) == 2


def test_main_window_mouse_page_primitives_commit_transactionally(
    qapp, monkeypatch,
):
    for kind in ("rectangle", "circle"):
        window = MainWindow()
        chapter = ChapterDocument(height=1400)
        active = chapter.add_page(
            bound=BoundGeometry.rectangle(0, 0, 400, 200)
        )
        window._set_chapter(chapter, TileStore())
        window.canvas.set_selection("layer", active.layer_id)
        monkeypatch.setattr(
            window, "_choose_page_shape", lambda selected=kind: selected
        )
        window.show()
        qapp.processEvents()
        try:
            window._add_page()
            start = (
                QPointF(150, 400)
                if kind == "circle" else QPointF(50, 300)
            )
            end = (
                QPointF(250, 400)
                if kind == "circle" else QPointF(250, 500)
            )
            start_widget = window.canvas.document_to_widget(start).toPoint()
            end_widget = window.canvas.document_to_widget(end).toPoint()
            QTest.mousePress(
                window.canvas, Qt.LeftButton, pos=start_widget
            )
            QTest.mouseMove(window.canvas, end_widget)
            QTest.mouseRelease(
                window.canvas, Qt.LeftButton, pos=end_widget
            )
            qapp.processEvents()
            assert len(chapter.root_page_ids) == 2
            assert window.canvas._page_creation_anchor_id == ""
            assert window.canvas.tool == ToolKind.SHAPE_EDIT
        finally:
            window._dirty = False
            window.close()


def test_main_window_tablet_page_rectangle_commits_transactionally(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter = ChapterDocument(height=1400)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", active.layer_id)
    monkeypatch.setattr(
        window, "_choose_page_shape", lambda: "rectangle"
    )
    window.show()
    qapp.processEvents()
    try:
        window._add_page()
        start = window.canvas.document_to_widget(QPointF(50, 300))
        end = window.canvas.document_to_widget(QPointF(250, 500))
        QApplication.sendEvent(
            window.canvas,
            _tablet_event(
                QEvent.TabletPress, window.canvas, start,
                1.0, Qt.LeftButton,
            ),
        )
        QApplication.sendEvent(
            window.canvas,
            _tablet_event(
                QEvent.TabletMove, window.canvas, end,
                1.0, Qt.LeftButton,
            ),
        )
        QApplication.sendEvent(
            window.canvas,
            _tablet_event(
                QEvent.TabletRelease, window.canvas, end,
                0.0, Qt.NoButton,
            ),
        )
        qapp.processEvents()

        assert len(chapter.root_page_ids) == 2
        assert window.canvas._page_creation_anchor_id == ""
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
    finally:
        window._dirty = False
        window.close()


def test_main_window_custom_page_enter_uses_acknowledged_commit(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter = ChapterDocument(height=1400)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", active.layer_id)
    monkeypatch.setattr(window, "_choose_page_shape", lambda: "custom")
    window.show()
    qapp.processEvents()
    try:
        window._add_page()
        for point in (
            QPointF(50, 300), QPointF(250, 300), QPointF(150, 500),
        ):
            QTest.mouseClick(
                window.canvas, Qt.LeftButton,
                pos=window.canvas.document_to_widget(point).toPoint(),
            )
        QTest.keyClick(window.canvas, Qt.Key_Return)
        qapp.processEvents()
        assert len(chapter.root_page_ids) == 2
        created = chapter.layers[chapter.root_page_ids[-1]]
        assert created.bound.closed
        assert window.canvas._page_creation_anchor_id == ""
    finally:
        window._dirty = False
        window.close()


def test_object_select_page_border_activates_shape_edit(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(50, 50, 300, 300)
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    point = QPointF(50, 180)
    canvas._request_object_selection(
        point, canvas.document_to_widget(point)
    )
    assert canvas.selected_id == page.layer_id
    assert canvas.tool == ToolKind.SHAPE_EDIT


def test_entity_selection_searches_other_pages_and_their_contents(qapp):
    chapter = ChapterDocument(height=1400)
    first = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 300)
    )
    second = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 300), y=500
    )
    layer = chapter.add_layer(
        second.layer_id, "Second-page shape",
        BoundGeometry.rectangle(40, 40, 250, 180),
    )
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(x=80, y=80, interaction_rect=(0, 0, 80, 60)),
    )
    canvas = CanvasWidget(EditorSettings(page_scope_select=True))
    canvas.resize(800, 700)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", first.layer_id)
    canvas.set_tool(ToolKind.OBJECT_SELECT)

    raster_point = QPointF(100, 600)
    canvas._request_object_selection(
        raster_point, canvas.document_to_widget(raster_point)
    )
    assert canvas.selected_id == raster.object_id

    canvas.set_tool(ToolKind.SHAPE_EDIT)
    border = QPointF(40, 590)
    canvas._tool_press(canvas.document_to_widget(border), 1)
    canvas._tool_release()
    assert canvas.selected_id == layer.layer_id
    assert canvas.active_page_id == second.layer_id


def _start_page_gap(
    canvas: CanvasWidget, start_y: float, end_y: float,
) -> dict:
    assert canvas.set_tool(ToolKind.INSERT_PAGE_GAP)
    start = canvas.document_to_widget(QPointF(200, start_y))
    end = canvas.document_to_widget(QPointF(200, end_y))
    canvas._tool_move(start, 1)
    assert canvas._page_gap_hover_y is not None
    canvas._tool_press(start, 1)
    assert canvas.page_gap_transaction()["phase"] == "creating"
    canvas._tool_move(end, 1)
    canvas._tool_release()
    transaction = canvas.page_gap_transaction()
    assert transaction is not None
    assert transaction["phase"] == "active"
    return transaction


def test_page_gap_hover_upward_drag_clamping_and_minimum(qapp):
    chapter = ChapterDocument(height=700)
    chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 400, 100))
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    assert canvas.set_tool(ToolKind.INSERT_PAGE_GAP)

    hover = canvas.document_to_widget(QPointF(100, 275))
    canvas._tool_move(hover, 1)
    assert canvas._page_gap_hover_y == 275

    transaction = _start_page_gap(canvas, 300, -40)
    assert transaction["top_y"] == 0
    assert transaction["bottom_y"] == 300
    assert canvas.chapter.height == 1000
    assert canvas._page_gap_hit(QPointF(5, 0)) == "top"
    assert canvas._page_gap_hit(QPointF(395, 150)) is None
    canvas.cancel_page_gap_transaction()

    assert canvas.set_tool(ToolKind.INSERT_PAGE_GAP)
    point = canvas.document_to_widget(QPointF(200, 250))
    canvas._tool_press(point, 1)
    canvas._tool_release()
    assert canvas.page_gap_transaction() is None
    assert not canvas.command_stack.can_undo


def test_page_gap_moves_highest_qualifying_hierarchy_roots(qapp):
    chapter = ChapterDocument(height=1000)
    crossing_page = chapter.add_page(
        "Crossing", BoundGeometry.rectangle(0, 0, 500, 500)
    )
    crossing = chapter.add_layer(
        crossing_page.layer_id, "Crossing group",
        BoundGeometry.rectangle(0, 150, 500, 200),
    )
    below_branch = chapter.add_layer(
        crossing.layer_id, "Below branch",
        BoundGeometry.rectangle(0, 300, 100, 40),
    )
    branch_child = chapter.add_object(
        below_branch.layer_id,
        RasterObject(x=5, y=305, interaction_rect=(0, 0, 20, 20)),
    )
    crossing_leaf = chapter.add_object(
        crossing.layer_id,
        RasterObject(x=10, y=220, interaction_rect=(0, 0, 40, 60)),
    )
    below_leaf = chapter.add_object(
        crossing.layer_id,
        RasterObject(x=10, y=330, interaction_rect=(0, 0, 30, 30)),
    )
    hidden_leaf = chapter.add_object(
        crossing.layer_id,
        RasterObject(
            x=60, y=370, visible=False,
            interaction_rect=(0, 0, 30, 30),
        ),
    )
    below_page = chapter.add_page(
        "Below page", BoundGeometry.rectangle(0, 0, 500, 100), y=600
    )
    below_page_child = chapter.add_object(
        below_page.layer_id,
        RasterObject(x=0, y=20, interaction_rect=(0, 0, 20, 20)),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())

    transaction = _start_page_gap(canvas, 250, 330)

    assert transaction["gap_size"] == 80
    assert set(transaction["move_roots"]) == {
        ("layer", below_branch.layer_id),
        ("object", below_leaf.object_id),
        ("object", hidden_leaf.object_id),
        ("layer", below_page.layer_id),
    }
    assert crossing.translate_y == 0
    assert below_branch.translate_y == 80
    assert branch_child.y == 305
    assert crossing_leaf.y == 220
    assert below_leaf.y == 410
    assert hidden_leaf.y == 450
    assert below_page.translate_y == 680
    assert below_page_child.y == 20
    assert chapter.height == 1080


def test_page_gap_handle_adjustment_recomputes_from_baseline(qapp):
    chapter = ChapterDocument(height=800)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 350)
    )
    threshold_leaf = chapter.add_object(
        upper.layer_id,
        RasterObject(x=20, y=220, interaction_rect=(0, 0, 20, 20)),
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 100), y=400
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    _start_page_gap(canvas, 250, 310)
    assert lower.translate_y == 460
    assert threshold_leaf.y == 220

    for target_y, expected in ((200, 510), (250, 460), (200, 510)):
        top = canvas.page_gap_transaction()["top_y"]
        canvas._tool_press(
            canvas.document_to_widget(QPointF(390, top)), 1
        )
        canvas._tool_move(
            canvas.document_to_widget(QPointF(390, target_y)), 1
        )
        canvas._tool_release()
        assert lower.translate_y == expected
        assert threshold_leaf.y == (330 if target_y == 200 else 220)
    assert chapter.height == 910


def test_page_gap_preserves_transform_frames_and_moves_quads(qapp):
    chapter = ChapterDocument(height=900)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 500, 700)
    )
    transformed = chapter.add_layer(
        page.layer_id, "Transformed",
        BoundGeometry.rectangle(0, 0, 100, 100),
    )
    transformed.transform_frame = (0, 0, 100, 100)
    transformed.transform_quad = [
        (30, 350), (150, 360), (145, 470), (25, 460),
    ]
    raster = chapter.add_object(
        page.layer_id,
        RasterObject(
            x=12, y=18,
            interaction_rect=(0, 0, 80, 60),
            transform_frame=(0, 0, 80, 60),
            transform_quad=[
                (250, 420), (340, 420), (340, 500), (250, 500),
            ],
        ),
    )
    layer_quad = list(transformed.transform_quad)
    object_quad = list(raster.transform_quad)
    focal = BlurModifier(
        strength=2, mode="focal", focal_center=(295, 455),
        focal_radius=30,
    )
    chapter.add_modifier(focal, [("object", raster.object_id)])
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())

    _start_page_gap(canvas, 300, 375)

    assert transformed.transform_frame == (0, 0, 100, 100)
    assert transformed.transform_quad == [
        (x, y + 75) for x, y in layer_quad
    ]
    assert raster.transform_frame == (0, 0, 80, 60)
    assert raster.transform_quad == [
        (x, y + 75) for x, y in object_quad
    ]
    assert (raster.x, raster.y) == (12, 18)
    assert focal.focal_center == (295, 530)


def test_page_gap_effect_extent_can_keep_leaf_in_place(qapp):
    chapter = ChapterDocument(height=800)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 500)
    )
    raster = chapter.add_object(
        page.layer_id,
        RasterObject(x=20, y=330, interaction_rect=(0, 0, 30, 30)),
    )
    chapter.add_modifier(
        BlurModifier(strength=20), [("object", raster.object_id)]
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())

    transaction = _start_page_gap(canvas, 300, 350)

    assert ("object", raster.object_id) not in transaction["move_roots"]
    assert raster.y == 330


def test_page_gap_moves_raster_vector_text_and_image_leaves(qapp):
    chapter = ChapterDocument(height=800)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 500, 600)
    )
    raster = chapter.add_object(
        page.layer_id,
        RasterObject(x=20, y=350, interaction_rect=(0, 0, 30, 30)),
    )
    vector = chapter.add_object(
        page.layer_id,
        VectorDrawingObject(
            x=80, y=350,
            strokes=[VectorStroke(points=[
                VectorStrokePoint(x=0, y=0, width=4),
                VectorStrokePoint(x=30, y=20, width=4),
            ])],
        ),
    )
    text = chapter.add_object(
        page.layer_id,
        TextObject(
            text="Below", layout_mode="free",
            transform_quad=[
                (150, 350), (250, 350), (250, 390), (150, 390),
            ],
        ),
    )
    image = chapter.add_object(
        page.layer_id,
        ImageObject(x=300, y=350, pixel_width=40, pixel_height=30),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())

    transaction = _start_page_gap(canvas, 300, 350)

    assert set(transaction["move_roots"]) == {
        ("object", raster.object_id),
        ("object", vector.object_id),
        ("object", text.object_id),
        ("object", image.object_id),
    }
    assert raster.y == 400
    assert vector.y == 400
    assert text.transform_quad == [
        (150, 400), (250, 400), (250, 440), (150, 440),
    ]
    assert image.y == 400


def test_page_gap_confirm_is_one_undoable_edit_and_restores_selection(qapp):
    chapter = ChapterDocument(height=800)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 160)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 100), y=400
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", upper.layer_id)
    previous_tool = canvas.tool
    before = chapter.to_dict()

    _start_page_gap(canvas, 250, 325)
    assert not canvas.command_stack.can_undo
    assert canvas.confirm_page_gap_transaction() is not None
    assert canvas.selected_id == upper.layer_id
    assert canvas.tool == previous_tool
    assert canvas.command_stack.can_undo
    assert canvas.command_stack.top_undo_command.label == "Insert page gap"
    assert canvas.chapter.height == 875
    assert canvas.chapter.layers[lower.layer_id].translate_y == 475

    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    canvas.command_stack.redo()
    assert canvas.chapter.height == 875
    assert canvas.chapter.layers[lower.layer_id].translate_y == 475


def test_page_gap_cancel_and_escape_restore_exact_baseline(qapp):
    chapter = ChapterDocument(height=800)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 160)
    )
    chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 100), y=400
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", upper.layer_id)
    previous_tool = canvas.tool
    before = chapter.to_dict()

    _start_page_gap(canvas, 250, 325)
    assert canvas.cancel_page_gap_transaction()
    assert canvas.chapter.to_dict() == before
    assert canvas.selected_id == upper.layer_id
    assert canvas.tool == previous_tool
    assert not canvas.command_stack.can_undo

    _start_page_gap(canvas, 250, 325)
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.chapter.to_dict() == before
    assert canvas.tool == previous_tool
    assert not canvas.command_stack.can_undo


def test_page_gap_without_qualifying_content_still_increases_height(qapp):
    chapter = ChapterDocument(height=700)
    chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 400, 100))
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())

    transaction = _start_page_gap(canvas, 400, 460)

    assert transaction["move_roots"] == []
    assert chapter.height == 760
    canvas.confirm_page_gap_transaction()
    assert canvas.command_stack.can_undo


def test_main_window_page_gap_preview_locks_editing_and_persistence(qapp):
    window = MainWindow()
    chapter = ChapterDocument(height=800)
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 160)
    )
    chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 100), y=400
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    window._dirty = True
    window.autosave_timer.stop()
    window.show()
    qapp.processEvents()
    try:
        assert window.canvas.set_tool(ToolKind.INSERT_PAGE_GAP)
        start = window.canvas.document_to_widget(QPointF(200, 250))
        window.canvas._tool_press(start, 1)
        assert not window._page_gap_mode_locked
        window._autosave()
        assert window.autosave_timer.isActive()
        assert not window.save()
        window.canvas._tool_release()
        assert window.canvas.page_gap_transaction() is None
        assert window.autosave_timer.isActive()
        window.autosave_timer.stop()

        _start_page_gap(window.canvas, 250, 325)
        qapp.processEvents()

        assert window._page_gap_mode_locked
        assert not window.menuBar().isEnabled()
        assert not window.tool_toolbar.isEnabled()
        assert not window.hierarchy_dock.isEnabled()
        assert not window.project_tabs.isEnabled()
        assert not window.undo_action.isEnabled()
        assert not window.save()
        assert window._hotkey_is_suppressed("select_all", frozenset())
        assert not window._activate_tool(ToolKind.RASTER_PENCIL)
        assert not window.page_gap_confirmation.isHidden()
        anchor = window.canvas.page_gap_confirmation_anchor()
        frame = window.page_gap_confirmation.geometry()
        assert frame.left() >= 0
        assert frame.top() >= 0
        assert abs(frame.center().x() - anchor.x()) <= 1

        window._cancel_page_gap()
        qapp.processEvents()
        assert not window._page_gap_mode_locked
        assert window.menuBar().isEnabled()
        assert window.tool_toolbar.isEnabled()
        assert window.page_gap_confirmation.isHidden()
        assert window._dirty
        assert not window.autosave_timer.isActive()
    finally:
        window._dirty = False
        window.close()
