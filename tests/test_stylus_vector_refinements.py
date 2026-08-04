from __future__ import annotations

from PySide6.QtCore import QCoreApplication, QEvent, QPointF, Qt
from PySide6.QtGui import (
    QGuiApplication, QImage, QPainter, QPointingDevice, QTabletEvent,
)
from PySide6.QtWidgets import QAbstractSpinBox, QApplication, QMenu

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tool_ribbon_pages import VectorToolsControls


def _tablet_event(event_type, local, global_position, pressure, buttons):
    return QTabletEvent(
        event_type, QPointingDevice.primaryPointingDevice(),
        local, global_position, pressure,
        0.0, 0.0, 0.0, 0.0, 0.0,
        Qt.NoModifier, Qt.LeftButton, buttons,
    )


def test_stylus_click_triggers_popup_action_once(qapp):
    window = MainWindow()
    menu = QMenu(window)
    action = menu.addAction("Stylus option")
    triggered: list[bool] = []
    action.triggered.connect(lambda: triggered.append(True))
    try:
        window.show()
        menu.popup(window.mapToGlobal(window.rect().center()))
        qapp.processEvents()
        center = menu.actionGeometry(action).center()
        local = QPointF(center)
        global_position = QPointF(menu.mapToGlobal(center))

        assert window._forward_popup_tablet_event(
            menu, _tablet_event(
                QEvent.TabletPress, local, global_position,
                1.0, Qt.LeftButton,
            ),
        )
        assert window._forward_popup_tablet_event(
            menu, _tablet_event(
                QEvent.TabletRelease, local, global_position,
                0.0, Qt.NoButton,
            ),
        )
        QApplication.processEvents()
        assert triggered == [True]
    finally:
        menu.close()
        window.close()


def test_canvas_tablet_press_is_not_forwarded_as_popup(
    qapp, monkeypatch,
):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 300)
    )
    raster = chapter.add_object(
        page.layer_id,
        RasterObject(interaction_rect=(0, 0, 300, 300)),
    )
    window._set_chapter(chapter, TileStore())
    received: list[float] = []
    monkeypatch.setattr(
        window.canvas, "_tool_press",
        lambda _position, pressure: received.append(pressure),
    )
    monkeypatch.setattr(
        QApplication, "widgetAt", lambda _position: window.canvas,
    )
    try:
        window.show()
        qapp.processEvents()
        local = QPointF(window.canvas.rect().center())
        global_position = QPointF(
            window.canvas.mapToGlobal(local.toPoint())
        )
        event = _tablet_event(
            QEvent.TabletPress, local, global_position,
            0.25, Qt.LeftButton,
        )
        release = _tablet_event(
            QEvent.TabletRelease, local, global_position,
            0.0, Qt.NoButton,
        )

        QCoreApplication.sendEvent(window.canvas, event)
        QCoreApplication.sendEvent(window.canvas, release)

        assert received == [0.25]
    finally:
        window.close()


def test_outliner_tablet_tap_toggles_visibility_both_ways(qapp):
    window = MainWindow()
    chapter = ChapterDocument()
    page = chapter.add_page(
        bound=BoundGeometry.rectangle(0, 0, 300, 300)
    )
    layer = chapter.add_layer(
        page.layer_id, "Shape",
        BoundGeometry.rectangle(20, 20, 100, 100),
    )
    window._set_chapter(chapter, TileStore())
    try:
        window.show()
        window.tree.expandAll()
        qapp.processEvents()
        index = window.hierarchy_model.index_for_entity(
            "layer", layer.layer_id
        )
        rect = window.tree.visualRect(index)
        local_point = rect.topLeft() + QPointF(8, rect.height() / 2).toPoint()
        global_point = window.tree.viewport().mapToGlobal(local_point)

        def tap():
            assert window._forward_outliner_tablet_event(
                window.tree.viewport(),
                _tablet_event(
                    QEvent.TabletPress, QPointF(local_point),
                    QPointF(global_point), 1.0, Qt.LeftButton,
                ),
            )
            assert window._forward_outliner_tablet_event(
                window.tree.viewport(),
                _tablet_event(
                    QEvent.TabletRelease, QPointF(local_point),
                    QPointF(global_point), 0.0, Qt.NoButton,
                ),
            )
            qapp.processEvents()

        tap()
        assert layer.visible is False
        tap()
        assert layer.visible is True
        assert window._tablet_outliner_press is None
    finally:
        window.deleteLater()


def test_redraw_sliders_keep_exact_manual_opacity_and_hide_pressure(qapp):
    settings = EditorSettings(
        vector_redraw_parameter="opacity",
        vector_redraw_amount=35,
        vector_redraw_opacity_max=100,
    )
    controls = VectorToolsControls(settings)
    try:
        controls.refresh()
        controls.redraw_amount.setValue(37)
        assert settings.vector_redraw_amount == 37
        assert controls.redraw_amount_slider.value() == 7

        controls.redraw_amount_slider.setValue(8)
        assert controls.redraw_amount.value() == 40
        assert settings.vector_redraw_amount == 40
        assert controls.redraw_amount.buttonSymbols() == (
            QAbstractSpinBox.ButtonSymbols.NoButtons
        )

        controls.set_selection_summary(1, 1, 1)
        assert settings.vector_redraw_interaction == "point"
        assert controls.redraw_maximum_row.isHidden()
    finally:
        controls.deleteLater()


def test_tablet_hover_uses_active_brush_size_and_simplify_radius(qapp):
    settings = EditorSettings(
        active_pencil_size="small",
        pencil_size_px={"small": 4, "medium": 12, "large": 22},
    )
    canvas = CanvasWidget(settings)
    canvas._tablet_hover_widget = QPointF(40, 40)

    def painted_width(draw):
        image = QImage(100, 100, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        painter = QPainter(image)
        draw(painter)
        painter.end()
        xs = [
            x for y in range(image.height()) for x in range(image.width())
            if image.pixelColor(x, y).alpha()
        ]
        return max(xs) - min(xs) + 1

    canvas.tool = ToolKind.RASTER_PENCIL
    small = painted_width(canvas._draw_tablet_hover)
    settings.active_pencil_size = "large"
    large = painted_width(canvas._draw_tablet_hover)
    assert large > small

    canvas.tool = ToolKind.VECTOR_SIMPLIFY
    simplify = painted_width(canvas._draw_simplify_hover)
    assert 23 <= simplify <= 27


def test_shift_navigation_is_suppressed_only_for_selection_tools(
    qapp, monkeypatch,
):
    canvas = CanvasWidget(EditorSettings())
    monkeypatch.setattr(
        QGuiApplication, "keyboardModifiers",
        lambda: Qt.ShiftModifier,
    )
    canvas.tool = ToolKind.DRAW_SELECT_RECT
    assert canvas._navigation_mode() is None
    canvas.tool = ToolKind.RASTER_PENCIL
    assert canvas._navigation_mode() == "rotate"
