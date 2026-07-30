from __future__ import annotations

from PySide6.QtCore import QEvent, QPointF
from PySide6.QtTest import QTest
from PySide6.QtCore import Qt
from PySide6.QtGui import QPointingDevice, QTabletEvent
from PySide6.QtWidgets import QApplication, QMessageBox

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, PathNode, RasterObject,
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


def test_drawn_page_inserts_after_active_and_shifts_lower_pages(
    qapp, monkeypatch,
):
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
    monkeypatch.setattr(
        window, "_choose_add_page_gap_action", lambda: "insert"
    )
    try:
        window._add_page()
        assert window.canvas._page_creation_anchor_id == ""
        assert (
            window.canvas.page_gap_transaction()["origin"] == "add_page"
        )
        assert not window.page_gap_confirmation.isHidden()
        assert chapter.layers[lower.layer_id].translate_y == 370
        assert not window.canvas.command_stack.can_undo
        window._confirm_page_gap()
        assert window.page_gap_confirmation.isHidden()
        assert window.canvas._page_creation_anchor_id == active.layer_id
        assert window.canvas._finish_pending_page_bound(
            BoundGeometry.rectangle(0, 120, 500, 100)
        )

        new_id = window.canvas.selected_id
        assert chapter.root_page_ids == [
            active.layer_id, new_id, lower.layer_id,
        ]
        assert chapter.layers[lower.layer_id].translate_y == 370
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        assert window.canvas._page_gap_state is None
        window.canvas.command_stack.undo()
        assert window.chapter.root_page_ids == [
            active.layer_id, lower.layer_id,
        ]
        assert window.chapter.layers[lower.layer_id].translate_y == 250
    finally:
        window._dirty = False
        window.close()


def test_declining_page_shift_keeps_overlap_and_has_no_gap_editor(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter = ChapterDocument(height=1200)
    active = chapter.add_page(
        "Page 1", BoundGeometry.rectangle(0, 0, 500, 100)
    )
    lower = chapter.add_page(
        "Page 2", BoundGeometry.rectangle(0, 0, 500, 100), y=180
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", active.layer_id)
    monkeypatch.setattr(
        window, "_choose_add_page_gap_action", lambda: "continue"
    )
    monkeypatch.setattr(window, "_choose_page_shape", lambda: "rectangle")
    try:
        window._add_page()
        assert window.canvas._finish_pending_page_bound(
            BoundGeometry.rectangle(0, 120, 500, 100)
        )
        assert chapter.layers[lower.layer_id].translate_y == 180
        assert window.canvas._page_gap_state is None
        assert window.canvas.selected_id not in {
            active.layer_id, lower.layer_id,
        }
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


def test_confirmed_page_gap_rejects_draft_outside_guides(qapp):
    chapter = ChapterDocument(height=1400)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 400, 200), y=500
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    assert canvas.begin_page_gap_transaction(
        "add_page", active.layer_id, [active.layer_id],
        [lower.layer_id], 200,
    )
    transaction = canvas.confirm_page_gap_transaction()
    assert canvas.begin_page_creation(
        active.layer_id, "rectangle",
        before=transaction["before"], gap_bounds=(200, 320),
    )
    messages = []
    canvas.pageCreationInvalid.connect(messages.append)

    assert not canvas._finish_pending_page_bound(
        BoundGeometry.rectangle(0, 220, 300, 140)
    )
    assert messages[-1].startswith("Keep the complete page")
    assert canvas._page_creation_anchor_id == active.layer_id


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


def test_insert_page_gap_and_drag_bottom_group(qapp):
    chapter = ChapterDocument(height=1000)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100), y=300
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", upper.layer_id)
    assert canvas.set_tool(ToolKind.INSERT_PAGE_GAP)

    canvas._tool_move(
        canvas.document_to_widget(QPointF(150, 200)), 1
    )
    assert canvas._page_gap_hover["owner_id"] == lower.layer_id
    canvas._tool_press(
        canvas.document_to_widget(QPointF(150, 200)), 1
    )
    assert chapter.layers[lower.layer_id].translate_y == 420
    assert canvas.page_gap_transaction()["origin"] == "standalone"
    assert not canvas.command_stack.can_undo

    canvas._tool_press(
        canvas.document_to_widget(QPointF(150, 320)), 1
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(150, 370)), 1
    )
    canvas._tool_release()
    assert chapter.layers[lower.layer_id].translate_y == 470
    assert canvas._page_gap_state["bottom_y"] == 370
    assert not canvas.command_stack.can_undo
    canvas.confirm_page_gap_transaction()
    assert canvas.selected_id == lower.layer_id
    assert canvas.tool == ToolKind.SHAPE_EDIT
    assert canvas.command_stack.can_undo


def test_page_gap_cancel_restores_staged_layout_and_tool(qapp):
    chapter = ChapterDocument(height=1000)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100), y=300
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", upper.layer_id)
    assert canvas.set_tool(ToolKind.INSERT_PAGE_GAP)
    before = chapter.to_dict()
    assert canvas.begin_page_gap_transaction(
        "standalone", lower.layer_id, [upper.layer_id],
        [lower.layer_id], 200,
    )
    assert chapter.layers[lower.layer_id].translate_y == 420

    assert canvas.cancel_page_gap_transaction() == "standalone"
    assert canvas.chapter.to_dict() == before
    assert canvas.tool == ToolKind.INSERT_PAGE_GAP
    assert not canvas.command_stack.can_undo


def test_add_page_shape_cancel_restores_pre_gap_layout(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter = ChapterDocument(height=1000)
    active = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100), y=300
    )
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", active.layer_id)
    before = chapter.to_dict()
    monkeypatch.setattr(
        window, "_choose_add_page_gap_action", lambda: "insert"
    )
    monkeypatch.setattr(window, "_choose_page_shape", lambda: None)
    try:
        window._add_page()
        assert chapter.layers[lower.layer_id].translate_y == 420
        window._confirm_page_gap()
        assert window.chapter.to_dict() == before
        assert window.canvas._page_gap_state is None
        assert window.canvas.tool == ToolKind.SHAPE_EDIT
        assert not window.canvas.command_stack.can_undo
    finally:
        window._dirty = False
        window.close()


def test_page_gap_release_rebases_top_with_margin(qapp):
    chapter = ChapterDocument(height=700)
    upper = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100)
    )
    lower = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 100), y=300
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(800, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", lower.layer_id, False)
    canvas.begin_page_gap_editor(
        lower.layer_id, [upper.layer_id], [lower.layer_id], 100, 220
    )

    canvas._tool_press(
        canvas.document_to_widget(QPointF(150, 150)), 1
    )
    canvas._tool_move(
        canvas.document_to_widget(QPointF(150, -150)), 1
    )
    canvas._tool_release()

    top = min(
        canvas.page_world_bounds(page_id).top()
        for page_id in chapter.root_page_ids
    )
    assert top == 120
    assert chapter.height > 700
