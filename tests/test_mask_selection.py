from __future__ import annotations

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QFont, QGuiApplication, QPointingDevice, QTabletEvent, QTransform
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, SeriesDocument, ShapeStyle, ToneMask,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def canvas(qapp):
    chapter = ChapterDocument(height=600)
    chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 500, 600))
    mask = ToneMask(saved=True, name="Mask")
    chapter.masks[mask.mask_id] = mask
    widget = CanvasWidget(EditorSettings(snap_to_grid=False))
    widget.resize(500, 600)
    widget.set_document(chapter, TileStore())
    widget.scale = 1
    widget.center_x, widget.center_y = 250, 300
    widget.set_tone_mask_mode(mask.mask_id)
    assert widget.set_tool(ToolKind.MASK_SELECT)
    widget.show()
    qapp.processEvents()
    yield widget
    widget.close()
    widget.deleteLater()


def _lasso(canvas, points, modifiers=Qt.NoModifier):
    positions = [canvas.document_to_widget(QPointF(*point)).toPoint() for point in points]
    QTest.mousePress(canvas, Qt.LeftButton, modifiers, positions[0])
    for point in positions[1:]:
        QTest.mouseMove(canvas, point)
    QTest.mouseRelease(canvas, Qt.LeftButton, modifiers, positions[-1])


def _field(canvas):
    return canvas.render_tone_mask_field(
        canvas.active_tone_mask_id, 500, 600, QTransform(), QRectF(0, 0, 500, 600),
    )


def test_lasso_adds_and_control_removes_only_enclosed_pixels(canvas):
    _lasso(canvas, [(50, 50), (150, 50), (150, 150), (50, 150)])
    _lasso(canvas, [(200, 50), (300, 50), (300, 150), (200, 150)], Qt.ShiftModifier)
    assert _field(canvas)[100, [100, 175, 250]] == pytest.approx([1, 0, 1])
    _lasso(canvas, [(75, 75), (125, 75), (125, 125), (75, 125)], Qt.ControlModifier)
    assert _field(canvas)[100, [60, 100, 140, 250]] == pytest.approx([1, 0, 1, 1])
    assert len(canvas.command_stack._undo) == 3
    canvas.command_stack.undo()
    assert _field(canvas)[100, 100] == 1
    assert not canvas.chapter.masks[canvas.active_tone_mask_id].paint_has_subtractions
    canvas.command_stack.redo()
    assert _field(canvas)[100, 100] == 0


def test_shift_remains_freehand_and_control_takes_priority(canvas):
    triangle = [(50, 50), (250, 50), (50, 250)]
    _lasso(canvas, triangle, Qt.ShiftModifier)
    assert _field(canvas)[100, 100] == 1
    assert _field(canvas)[240, 240] == 0
    _lasso(canvas, triangle, Qt.ShiftModifier | Qt.ControlModifier)
    assert _field(canvas)[100, 100] == 0


def test_lasso_cutouts_work_over_linked_gradient_and_can_be_added_back(canvas):
    canvas.set_tool(ToolKind.GRADIENT)
    canvas._mask_gradient_press(QPointF(50, 300))
    canvas._mask_gradient_move(QPointF(450, 300))
    canvas._finish_mask_gradient()
    gradient_before = canvas.active_mask_gradient().to_dict()
    canvas.set_tool(ToolKind.MASK_SELECT)
    box = [(200, 200), (300, 200), (300, 400), (200, 400)]
    _lasso(canvas, box, Qt.ControlModifier)
    assert _field(canvas)[300, 250] == 0
    assert _field(canvas)[300, 400] > .8
    assert canvas.active_mask_gradient().to_dict() == gradient_before
    _lasso(canvas, box, Qt.ShiftModifier)
    assert _field(canvas)[300, 250] == 1
    assert len(canvas.chapter.objects) == 0


def test_cancel_or_exit_discards_unfinished_lasso(canvas):
    before = canvas.chapter.to_dict()
    canvas._dispatch_tool_press(canvas.document_to_widget(QPointF(50, 50)), 1, Qt.ShiftModifier)
    for point in [(250, 50), (250, 250)]:
        canvas._tool_move(canvas.document_to_widget(QPointF(*point)), 1)
    QTest.keyClick(canvas, Qt.Key_Escape)
    assert canvas.active_tone_mask_id
    assert canvas._mask_selection_gesture is None
    assert canvas.chapter.to_dict() == before
    assert not canvas.command_stack.can_undo
    canvas._dispatch_tool_press(canvas.document_to_widget(QPointF(50, 50)), 1, Qt.ControlModifier)
    canvas.set_tone_mask_mode("")
    canvas._tool_release()
    assert canvas._mask_selection_gesture is None
    assert not canvas.command_stack.can_undo
    assert not canvas.set_tool(ToolKind.MASK_SELECT)


def test_tiny_lasso_is_noop_and_mask_switch_does_not_leak(canvas):
    _lasso(canvas, [(50, 50), (51, 50)])
    assert not canvas.command_stack.can_undo
    original_id = canvas.active_tone_mask_id
    other = ToneMask(saved=True)
    canvas.chapter.masks[other.mask_id] = other
    canvas._begin_mask_selection(QPointF(50, 50), Qt.ShiftModifier)
    canvas._move_mask_selection(QPointF(150, 50))
    canvas._move_mask_selection(QPointF(150, 150))
    canvas.set_tone_mask_mode(other.mask_id)
    canvas._tool_release()
    assert not canvas.tiles.object_tiles(original_id)
    assert not canvas.tiles.object_tiles(other.mask_id)


def test_mask_cutouts_round_trip_with_saved_tiles(canvas, tmp_path):
    _lasso(canvas, [(50, 50), (350, 50), (350, 350), (50, 350)], Qt.ShiftModifier)
    _lasso(canvas, [(200, 200), (300, 200), (300, 300), (200, 300)], Qt.ControlModifier)
    expected = _field(canvas)
    mask_id = canvas.active_tone_mask_id
    repository = SeriesRepository(tmp_path / "selection")
    repository.create("Selection")
    repository.save_chapter(canvas.chapter, canvas.tiles)
    chapter, tiles = repository.load_chapter(canvas.chapter.chapter_id)
    assert chapter.masks[mask_id].paint_has_subtractions
    canvas.set_document(chapter, tiles)
    canvas.set_tone_mask_mode(mask_id)
    np.testing.assert_allclose(_field(canvas), expected)


def test_select_button_and_context_are_only_available_in_mask_mode(qapp):
    window = MainWindow()
    chapter = ChapterDocument(height=600)
    page = chapter.add_page()
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    window.series = SeriesDocument()
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    button = window.tool_buttons[ToolKind.MASK_SELECT]
    try:
        assert button.isHidden()
        window._enter_mask_mode(mask.mask_id)
        assert not button.isHidden()
        button.click()
        assert window.canvas.tool == ToolKind.MASK_SELECT
        assert window.tool_settings_controls.context_label.text() == "Mask Select"
        assert window.tool_settings_controls.stack.currentWidget() is window.tool_settings_controls.mask_select_page
        wand = window.tool_buttons[ToolKind.MASK_WAND]
        assert not wand.isHidden()
        wand.click()
        assert window.canvas.tool == ToolKind.MASK_WAND
        controls = window.tool_settings_controls
        assert controls.context_label.text() == "Magic Wand"
        assert controls.stack.currentWidget() is controls.mask_wand_page
        controls.mask_wand_tolerance.setValue(42)
        assert controls.mask_wand_tolerance_slider.value() == 42
        assert window.canvas.settings.mask_wand_tolerance == 42
        window._finish_mask_mode(True)
        assert button.isHidden()
        assert wand.isHidden()
        assert not window.canvas.set_tool(ToolKind.MASK_WAND)
        assert window.tool_buttons[ToolKind.OBJECT_SELECT].isEnabled()
    finally:
        window._dirty = False
        window.close()
        window.deleteLater()


def test_wand_settings_fit_narrow_sidebar(qapp, text_outline_font_family):
    window = MainWindow()
    window.setFont(QFont(text_outline_font_family, 9))
    window.resize(1400, 960)
    chapter = ChapterDocument(height=600)
    page = chapter.add_page()
    mask = ToneMask(saved=True)
    chapter.masks[mask.mask_id] = mask
    window.series = SeriesDocument()
    window._set_chapter(chapter, TileStore())
    window.canvas.set_selection("layer", page.layer_id)
    window.show()
    window.workspace_splitter.setSizes([230, 1170])
    window._enter_mask_mode(mask.mask_id)
    window.tool_buttons[ToolKind.MASK_WAND].click()
    try:
        # Nested scroll/ribbon layouts settle through posted layout requests.
        for _ in range(6):
            qapp.processEvents()
        viewport = window.tool_settings_page.viewport()
        controls = window.tool_settings_controls
        assert viewport.width() <= 200
        widgets = [
            controls.mask_wand_tolerance, controls.mask_wand_tolerance_slider,
            *controls.mask_wand_page.findChildren(QLabel),
        ]
        for widget in widgets:
            left = widget.mapTo(viewport, QPoint()).x()
            assert 0 <= left
            assert left + widget.width() <= viewport.width()
            if isinstance(widget, QLabel) and widget.wordWrap():
                assert widget.height() >= widget.heightForWidth(widget.width())
        assert controls.mask_wand_tolerance_slider.y() > controls.mask_wand_tolerance.y()
        assert window.tool_settings_page.horizontalScrollBar().maximum() == 0
    finally:
        window._dirty = False
        window.close()
        window.deleteLater()


@pytest.mark.parametrize("modifiers,expected", [(Qt.ShiftModifier, 1), (Qt.ControlModifier, 0)])
def test_pen_lasso_honors_modifiers_and_release_position(canvas, monkeypatch, modifiers, expected):
    _lasso(canvas, [(40, 40), (200, 40), (200, 200), (40, 200)])
    monkeypatch.setattr(QGuiApplication, "keyboardModifiers", lambda: modifiers)
    for kind, position in [
        (QEvent.TabletPress, (50, 50)),
        (QEvent.TabletMove, (150, 50)),
        (QEvent.TabletMove, (150, 150)),
        (QEvent.TabletRelease, (50, 150)),
    ]:
        local = canvas.document_to_widget(QPointF(*position))
        release = kind == QEvent.TabletRelease
        event = QTabletEvent(
            kind, QPointingDevice.primaryPointingDevice(), local,
            QPointF(canvas.mapToGlobal(local.toPoint())), 0 if release else .7,
            0, 0, 0, 0, 0, modifiers, Qt.LeftButton,
            Qt.NoButton if release else Qt.LeftButton,
        )
        QCoreApplication.sendEvent(canvas, event)
    assert _field(canvas)[125, 75] == expected
    assert canvas._nav_mode is None
    assert canvas._mask_selection_gesture is None


def test_lasso_preview_and_cutout_overlay_are_mask_scoped(canvas):
    _lasso(canvas, [(50, 50), (400, 50), (400, 400), (50, 400)])
    before_cutout = canvas.grab().toImage().pixelColor(100, 100)
    _lasso(canvas, [(75, 75), (125, 75), (125, 125), (75, 125)], Qt.ControlModifier)
    assert canvas.grab().toImage().pixelColor(100, 100) != before_cutout
    canvas._begin_mask_selection(QPointF(250, 250), Qt.ShiftModifier)
    canvas._move_mask_selection(QPointF(350, 250))
    canvas._move_mask_selection(QPointF(350, 350))
    preview = canvas.grab().toImage()
    canvas._cancel_mask_selection()
    assert canvas.grab().toImage() != preview


def test_mask_pencil_can_add_back_after_control_lasso(canvas):
    area = [(50, 50), (200, 50), (200, 200), (50, 200)]
    _lasso(canvas, area)
    _lasso(canvas, area, Qt.ControlModifier)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._tool_press(canvas.document_to_widget(QPointF(100, 100)), 1)
    canvas._tool_release()
    assert _field(canvas)[100, 100] > .9
    assert _field(canvas)[175, 175] == 0


def _wand_artwork(canvas):
    page = canvas.chapter.root_page_ids[0]
    layers = []
    for name, rect, color in (
        ("Dark", (200, 50, 100, 100), "#202020"),
        ("Similar", (300, 50, 100, 100), "#303030"),
        ("Disconnected", (200, 250, 100, 100), "#202020"),
    ):
        layers.append(canvas.chapter.add_layer(
            page, name, BoundGeometry.rectangle(*rect),
            style=ShapeStyle(primary_color=color, outline_thickness=0),
        ))
    canvas._invalidate_scene_cache()
    return layers


def _wand_click(canvas, point, modifiers=Qt.NoModifier):
    assert canvas.set_tool(ToolKind.MASK_WAND)
    QTest.mouseClick(
        canvas, Qt.LeftButton, modifiers,
        canvas.document_to_widget(QPointF(*point)).toPoint(),
    )


def test_wand_tolerance_connectivity_and_undo_across_tiles(canvas):
    _wand_artwork(canvas)
    canvas.settings.mask_wand_tolerance = 0
    _wand_click(canvas, (220, 100))
    assert _field(canvas)[100, [220, 280, 350, 450]] == pytest.approx([1, 1, 0, 0])
    assert _field(canvas)[300, 250] == 0
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert not _field(canvas).any()
    canvas.settings.mask_wand_tolerance = 16
    _wand_click(canvas, (220, 100))
    assert _field(canvas)[100, [220, 280, 350, 450]] == pytest.approx([1, 1, 1, 0])
    assert _field(canvas)[300, 250] == 0
    expected = _field(canvas)
    _wand_click(canvas, (220, 100))
    assert len(canvas.command_stack._undo) == 1  # Re-selecting is a no-op.
    canvas.command_stack.undo()
    assert not _field(canvas).any()
    canvas.command_stack.redo()
    np.testing.assert_array_equal(_field(canvas), expected)


def test_wand_control_removes_and_shift_control_takes_priority(canvas, monkeypatch):
    _wand_artwork(canvas)
    _lasso(canvas, [(190, 40), (410, 40), (410, 160), (190, 160)])
    canvas.rotation, canvas.scale = 25, .8
    canvas.settings.mask_wand_tolerance = 0
    _wand_click(canvas, (220, 100), Qt.ControlModifier | Qt.ShiftModifier)
    assert _field(canvas)[100, [220, 280, 350, 405]] == pytest.approx([0, 0, 1, 1])
    assert canvas.chapter.masks[canvas.active_tone_mask_id].paint_has_subtractions
    assert canvas._nav_mode is None
    assert len(canvas.command_stack._undo) == 2
    canvas.command_stack.undo()
    assert _field(canvas)[100, 220] == 1
    assert not canvas.chapter.masks[canvas.active_tone_mask_id].paint_has_subtractions
    canvas.command_stack.redo()
    monkeypatch.setattr(QGuiApplication, "keyboardModifiers", lambda: Qt.ShiftModifier)
    _wand_click(canvas, (220, 100), Qt.ShiftModifier)
    assert _field(canvas)[100, 220] == 1
    assert canvas._nav_mode is None


def test_wand_control_cutout_preserves_gradient_and_source_artwork(canvas):
    _wand_artwork(canvas)
    canvas.set_tool(ToolKind.GRADIENT)
    canvas._mask_gradient_press(QPointF(50, 300))
    canvas._mask_gradient_move(QPointF(450, 300))
    canvas._finish_mask_gradient()
    gradient = canvas.active_mask_gradient().to_dict()
    source_layers = {key: layer.to_dict() for key, layer in canvas.chapter.layers.items()}
    _wand_click(canvas, (220, 100), Qt.ControlModifier)
    assert _field(canvas)[100, 250] == 0
    assert _field(canvas)[300, 250] > .4
    assert canvas.active_mask_gradient().to_dict() == gradient
    assert {key: layer.to_dict() for key, layer in canvas.chapter.layers.items()} == source_layers
    assert not canvas.chapter.objects


def test_wand_stops_at_document_bounds_and_ignores_hidden_art(canvas):
    layers = _wand_artwork(canvas)
    for layer in layers:
        layer.visible = False
    canvas.chapter.width, canvas.chapter.height = 500, 600
    canvas.settings.mask_wand_tolerance = 255
    _wand_click(canvas, (220, 100))
    assert _field(canvas).all()
    outside = canvas.render_tone_mask_field(
        canvas.active_tone_mask_id, 550, 650, QTransform(), QRectF(0, 0, 550, 650),
    )
    assert not outside[600:, :].any()
    assert not outside[:, 500:].any()
    count = len(canvas.command_stack._undo)
    canvas._mask_wand_press(QPointF(-1, 100), Qt.NoModifier)
    canvas._mask_wand_press(QPointF(500, 600), Qt.ControlModifier)
    assert len(canvas.command_stack._undo) == count


@pytest.mark.parametrize("modifiers,expected", [(Qt.ShiftModifier, 1), (Qt.ControlModifier, 0)])
def test_pen_wand_honors_modifiers(canvas, monkeypatch, modifiers, expected):
    _wand_artwork(canvas)
    _lasso(canvas, [(190, 40), (410, 40), (410, 160), (190, 160)])
    assert canvas.set_tool(ToolKind.MASK_WAND)
    monkeypatch.setattr(QGuiApplication, "keyboardModifiers", lambda: modifiers)
    local = canvas.document_to_widget(QPointF(220, 100))
    for kind in (QEvent.TabletPress, QEvent.TabletRelease):
        release = kind == QEvent.TabletRelease
        event = QTabletEvent(
            kind, QPointingDevice.primaryPointingDevice(), local,
            QPointF(canvas.mapToGlobal(local.toPoint())), 0 if release else .7,
            0, 0, 0, 0, 0, modifiers, Qt.LeftButton,
            Qt.NoButton if release else Qt.LeftButton,
        )
        QCoreApplication.sendEvent(canvas, event)
    assert _field(canvas)[100, 250] == expected
    assert canvas._nav_mode is None
    assert not canvas._tablet_tool_active
