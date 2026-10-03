from __future__ import annotations

import math

import pytest
from PySide6.QtCore import QEvent, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QMouseEvent, QPainterPath, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest

from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject, TextObject, TilingModifier, VectorDrawingObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def make_canvas():
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page("Lasso", BoundGeometry.rectangle(0, 0, 1080, 1080))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 100)))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", raster.object_id)
    assert canvas.set_tool(ToolKind.LASSO_BRUSH)
    return canvas, chapter, raster


def pixels(canvas, raster):
    return {key: bytes(image.constBits()) for key, image in canvas.tiles.object_tiles(raster.object_id).items()}


def color_at(canvas, raster, x, y):
    size = canvas.tiles.tile_size
    key = math.floor(x / size), math.floor(y / size)
    image = canvas.tiles.tile(raster.object_id, key)
    return image.pixelColor(x - key[0] * size, y - key[1] * size) if image else QColor(Qt.transparent)


def drag(canvas, points):
    canvas._tool_press(canvas.camera_transform().map(QPointF(*points[0])), 1)
    for point in points[1:]:
        canvas._tool_move(canvas.camera_transform().map(QPointF(*point)), 1)


def test_live_closed_fill_commits_once_and_undo_redo_restore_tiles_and_frame(qapp):
    canvas, chapter, raster = make_canvas()
    frame = raster.interaction_rect
    height = chapter.height
    canvas.primary_color = "#FFCC3311"
    commits = []
    canvas.documentChanged.connect(commits.append)
    drag(canvas, [(-40, -40), (310, -40), (310, 310)])
    assert color_at(canvas, raster, 200, 40) == QColor(canvas.primary_color)
    assert color_at(canvas, raster, 0, 200).alpha() == 0
    assert not commits and not canvas.command_stack.can_undo
    canvas._tool_move(canvas.camera_transform().map(QPointF(-40, 310)), 1)
    assert color_at(canvas, raster, 0, 200) == QColor(canvas.primary_color)
    canvas._tool_release()
    after = pixels(canvas, raster)
    after_frame = raster.interaction_rect
    assert len(after) == 9
    assert len(commits) == 1 and len(canvas.command_stack._undo) == 1
    assert canvas.command_stack.top_undo_command.label == "Lasso brush"
    canvas.command_stack.undo()
    assert pixels(canvas, raster) == {} and raster.interaction_rect == frame
    assert chapter.height == height
    canvas.command_stack.redo()
    assert pixels(canvas, raster) == after and raster.interaction_rect == after_frame
    canvas.close()


def test_translucent_preview_replaces_closing_edge_without_accumulation(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.primary_color = "#80CC3311"
    drag(canvas, [(30, 30), (200, 30), (200, 200)])
    assert color_at(canvas, raster, 160, 80).alpha() == 128
    canvas._tool_move(canvas.camera_transform().map(QPointF(200, 20)), 1)
    assert color_at(canvas, raster, 160, 80).alpha() == 0
    canvas._tool_move(canvas.camera_transform().map(QPointF(200, 200)), 1)
    assert color_at(canvas, raster, 160, 80).alpha() == 128
    canvas._tool_release()
    assert color_at(canvas, raster, 160, 80).alpha() == 128
    canvas.close()


@pytest.mark.parametrize("cancel", ["escape", "tool", "selection", "document", "session", "focus"])
def test_cancel_restores_existing_pixels_frame_and_height(qapp, cancel):
    canvas, chapter, raster = make_canvas()
    canvas.tiles.paint_dab(raster.object_id, QPointF(60, 60), 50, QColor("blue"))
    before = pixels(canvas, raster)
    frame, height = raster.interaction_rect, chapter.height
    drag(canvas, [(30, 30), (300, 30), (300, height + 100)])
    assert pixels(canvas, raster) != before
    if cancel == "escape":
        canvas.keyPressEvent(QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    elif cancel == "tool":
        canvas.set_tool(ToolKind.OBJECT_SELECT)
    elif cancel == "selection":
        canvas.set_selection("layer", raster.parent_layer_id)
    elif cancel == "document":
        store = canvas.tiles
        canvas.set_document(ChapterDocument(), TileStore())
        assert {key: bytes(image.constBits()) for key, image in store.object_tiles(raster.object_id).items()} == before
        canvas.close()
        return
    elif cancel == "session":
        canvas.capture_session_state()
    else:
        canvas.event(QEvent(QEvent.WindowDeactivate))
    assert pixels(canvas, raster) == before
    assert raster.interaction_rect == frame and chapter.height == height
    assert canvas._lasso_brush is None and not canvas._drawing
    assert not canvas.command_stack.can_undo
    canvas.close()


def test_selection_and_active_secondary_color_are_frozen_at_pen_down(qapp):
    canvas, _chapter, raster = make_canvas()
    selection = QPainterPath()
    selection.addRect(50, 50, 50, 50)
    canvas._drawing_selection_path = selection
    canvas.secondary_color = "#FF2288CC"
    canvas.set_active_color_slot("secondary")
    drag(canvas, [(20, 20), (140, 20)])
    canvas.secondary_color = "red"
    canvas._drawing_selection_path = QPainterPath()
    canvas._tool_move(canvas.camera_transform().map(QPointF(140, 140)), 1)
    canvas._tool_move(canvas.camera_transform().map(QPointF(20, 140)), 1)
    canvas._tool_release()
    assert color_at(canvas, raster, 75, 75) == QColor("#FF2288CC")
    assert color_at(canvas, raster, 40, 75).alpha() == 0
    assert color_at(canvas, raster, 110, 75).alpha() == 0
    canvas.close()


def test_transparent_color_erases_and_undo_restores_original(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.tiles.paint_dab(raster.object_id, QPointF(100, 100), 150, QColor("blue"), square=True)
    before = pixels(canvas, raster)
    canvas.primary_color = "#00000000"
    drag(canvas, [(50, 50), (150, 50), (150, 150), (50, 150)])
    assert color_at(canvas, raster, 100, 100).alpha() == 0
    assert color_at(canvas, raster, 35, 100) == QColor("blue")
    canvas._tool_release()
    canvas.command_stack.undo()
    assert pixels(canvas, raster) == before
    canvas.close()


@pytest.mark.parametrize("tablet", [False, True])
def test_mouse_and_tablet_release_include_final_position(qapp, tablet):
    canvas, _chapter, raster = make_canvas()
    for kind, point in [("press", (30, 30)), ("move", (200, 30)), ("release", (200, 200))]:
        position = canvas.camera_transform().map(QPointF(*point))
        if tablet:
            event_type = {"press": QEvent.TabletPress, "move": QEvent.TabletMove, "release": QEvent.TabletRelease}[kind]
            event = QTabletEvent(event_type, QPointingDevice.primaryPointingDevice(), position, position,
                                 .5 if kind != "release" else 0, 0, 0, 0, 0, 0, Qt.NoModifier,
                                 Qt.NoButton if kind == "move" else Qt.LeftButton,
                                 Qt.NoButton if kind == "release" else Qt.LeftButton)
            canvas.tabletEvent(event)
        else:
            event_type = {"press": QEvent.MouseButtonPress, "move": QEvent.MouseMove, "release": QEvent.MouseButtonRelease}[kind]
            event = QMouseEvent(event_type, position, position,
                                Qt.NoButton if kind == "move" else Qt.LeftButton,
                                Qt.NoButton if kind == "release" else Qt.LeftButton, Qt.NoModifier)
            {"press": canvas.mousePressEvent, "move": canvas.mouseMoveEvent, "release": canvas.mouseReleaseEvent}[kind](event)
    assert color_at(canvas, raster, 160, 80).alpha() == 255
    assert len(canvas.command_stack._undo) == 1
    canvas.close()


def test_widget_mouse_capture_commits_on_release(qapp):
    canvas, _chapter, raster = make_canvas()
    canvas.center_x = canvas.center_y = 200
    canvas.scale = 1
    canvas.show()
    qapp.processEvents()
    first = canvas.camera_transform().map(QPointF(30, 30)).toPoint()
    second = canvas.camera_transform().map(QPointF(200, 30)).toPoint()
    final = canvas.camera_transform().map(QPointF(200, 200)).toPoint()
    QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, first)
    QTest.mouseMove(canvas, second)
    QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, final)
    assert color_at(canvas, raster, 160, 80).alpha() == 255
    assert len(canvas.command_stack._undo) == 1
    canvas.close()


@pytest.mark.parametrize("points", [[(40, 40)], [(40, 40), (200, 200)]])
def test_empty_gestures_do_not_create_tiles_or_undo(qapp, points):
    canvas, _chapter, raster = make_canvas()
    drag(canvas, points)
    canvas._tool_release()
    assert pixels(canvas, raster) == {} and not canvas.command_stack.can_undo
    canvas.close()


def test_transformed_raster_uses_local_pixels_under_rotated_camera(qapp):
    canvas, chapter, raster = make_canvas()
    parent = chapter.layers[raster.parent_layer_id]
    parent.translate_x, parent.translate_y = 70, 80
    raster.x, raster.y = 10, 20
    raster.transform_frame = (10, 20, 100, 100)
    raster.transform_quad = [(100, 100), (300, 120), (270, 280), (90, 260)]
    canvas.rotation = 32
    canvas.scale = 1.7
    transform = canvas._drawing_local_to_world_transform(raster)
    points = [transform.map(QPointF(*point)).toTuple() for point in [(20, 20), (150, 20), (150, 150), (20, 150)]]
    drag(canvas, points)
    assert color_at(canvas, raster, 60, 60).alpha() == 255
    assert color_at(canvas, raster, 130, 130).alpha() == 255
    assert color_at(canvas, raster, 10, 60).alpha() == 0
    canvas._tool_release()
    assert raster.transform_quad == [(100, 100), (300, 120), (270, 280), (90, 260)]
    canvas.close()


def test_live_fill_is_clipped_by_parent_mask_and_matches_commit(qapp):
    canvas, chapter, raster = make_canvas()
    layer = chapter.add_layer(raster.parent_layer_id, "Mask", BoundGeometry.rectangle(80, 80, 80, 80))
    chapter.move_entity("object", raster.object_id, layer.layer_id, 0)
    canvas.set_selection("object", raster.object_id)
    canvas.primary_color = "#FFCC3311"
    drag(canvas, [(40, 40), (200, 40), (200, 200), (40, 200)])
    preview = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.pixelColor(120, 120) == QColor(canvas.primary_color)
    assert preview.pixelColor(60, 120) != QColor(canvas.primary_color)
    assert color_at(canvas, raster, 60, 120) == QColor(canvas.primary_color)
    canvas._tool_release()
    committed = QImage(1080, 1080, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(committed)
    assert preview == committed
    canvas.close()


def test_large_tiled_lasso_fills_one_cell_at_constant_opacity(qapp):
    canvas, chapter, raster = make_canvas()
    chapter.add_modifier(TilingModifier(center=(16, 16), side=32), [("object", raster.object_id)])
    canvas.primary_color = "#80CC3311"
    drag(canvas, [(-100, -100), (500, -100), (500, 500), (-100, 500)])
    canvas._tool_release()
    assert color_at(canvas, raster, 16, 16).alpha() == 128
    assert color_at(canvas, raster, 100, 100).alpha() == 0
    assert len(pixels(canvas, raster)) == 1
    canvas.command_stack.undo()
    assert pixels(canvas, raster) == {}
    canvas.close()


def test_toolbar_is_raster_only_and_context_stays_simple(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page()
    raster = chapter.add_object(page.layer_id, RasterObject())
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    button = window.tool_buttons[ToolKind.LASSO_BRUSH]
    try:
        assert button.isHidden()
        window.canvas.set_selection("object", raster.object_id)
        assert not button.isHidden() and button.isEnabled()
        button.click()
        assert window.canvas.tool == ToolKind.LASSO_BRUSH
        controls = window.tool_settings_controls
        assert controls.stack.currentWidget() is controls.lasso_brush_page
        for obj in (VectorDrawingObject(), ImageObject(), TextObject()):
            other = chapter.add_object(page.layer_id, obj)
            window.canvas.set_selection("object", other.object_id)
            assert button.isHidden()
            assert not window.canvas.set_tool(ToolKind.LASSO_BRUSH)
        window.canvas.set_selection("layer", page.layer_id)
        assert button.isHidden()
        window.canvas.set_selection("object", raster.object_id)
        second = chapter.add_object(page.layer_id, RasterObject())
        window.canvas.set_selection_set([("object", raster.object_id), ("object", second.object_id)])
        assert button.isHidden() and not window.canvas.set_tool(ToolKind.LASSO_BRUSH)
        window.canvas.set_selection("object", raster.object_id)
        window.canvas.active_tone_mask_id = "mask"
        window._sync_tool_buttons()
        assert button.isHidden() and not window.canvas.set_tool(ToolKind.LASSO_BRUSH)
    finally:
        window.deleteLater()
