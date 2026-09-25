"""Ctrl-click chooser previews and pen input through the application event filter."""
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QPointF, QTimer, Qt
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPointingDevice, QTabletEvent
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.selection_candidates import SelectionCandidateMenu


@pytest.fixture
def chooser_window(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    chapter = ChapterDocument(height=200)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 200))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(20, 20, 160, 120))
    group.fill_color, group.border_width = "#00ff00", 0
    left = chapter.add_object(group.layer_id, RasterObject(name="Red", interaction_rect=(30, 30, 60, 60)))
    right = chapter.add_object(group.layer_id, RasterObject(name="Blue", interaction_rect=(55, 30, 60, 60)))
    tiles = TileStore()
    tiles.paint_dab(left.object_id, QPointF(60, 60), 60, QColor("red"), square=True, antialias=False)
    tiles.paint_dab(right.object_id, QPointF(85, 60), 60, QColor("blue"), square=True, antialias=False)
    window._set_chapter(chapter, tiles)
    window.resize(1280, 900)
    window.show()
    window.canvas.set_selection("layer", page.layer_id, activate_default_tool=False)
    window.canvas.set_tool(ToolKind.OBJECT_SELECT)
    window._test_candidates = [("object", left.object_id), ("object", right.object_id), ("layer", group.layer_id)]
    qapp.processEvents()
    yield window
    popup = QApplication.activePopupWidget()
    if popup is not None:
        popup.close()
    window.autosave_timer.stop()
    window.canvas._effect_jobs.cancel()
    window._dirty = False
    window.hide()
    window.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def run_chooser(window, interact, open_menu=None):
    """Exercise the real nested popup loop, surfacing callback assertions safely."""
    errors, visited = [], []

    def drive():
        menu = QApplication.activePopupWidget()
        try:
            assert isinstance(menu, SelectionCandidateMenu)
            visited.append(True)
            interact(menu)
        except BaseException as error:
            errors.append(error)
        finally:
            if menu is not None:
                menu.close()

    QTimer.singleShot(0, drive)
    if open_menu is None:
        window._show_selection_candidates(
            [{"kind": kind, "id": identifier} for kind, identifier in window._test_candidates],
            window.canvas.mapToGlobal(QPoint(40, 40)),
        )
    else:
        open_menu()
    if errors:
        raise errors[0]
    assert visited


def tablet(target, menu, event_type, position, *, modifiers=Qt.NoModifier):
    global_position = QPointF(menu.mapToGlobal(position))
    local = QPointF(target.mapFromGlobal(global_position.toPoint()))
    pressed = event_type == QEvent.TabletPress
    event = QTabletEvent(
        event_type, QPointingDevice.primaryPointingDevice(), local, global_position,
        0.5 if pressed else 0.0, 0, 0, 0, 0, 0, modifiers,
        Qt.NoButton if event_type == QEvent.TabletMove else Qt.LeftButton,
        Qt.LeftButton if pressed else Qt.NoButton,
    )
    QCoreApplication.sendEvent(target, event)
    assert event.isAccepted()


def hover(menu, action):
    # Send a real move even if an earlier test left the system cursor at the
    # same coordinates; QTest.mouseMove otherwise sends no event on Windows.
    position = menu.actionGeometry(action).center()
    QCoreApplication.sendEvent(menu, QMouseEvent(
        QEvent.MouseMove, QPointF(position), QPointF(menu.mapToGlobal(position)),
        Qt.NoButton, Qt.NoButton, Qt.NoModifier,
    ))


@pytest.mark.parametrize("existing_solo", [False, True])
def test_hover_isolates_object_or_layer_and_restores_on_leave_and_escape(chooser_window, existing_solo):
    window, canvas = chooser_window, chooser_window.canvas
    left, right, group = window._test_candidates
    prior = {right} if existing_solo else set()
    canvas.set_solo_entities(prior)
    before, selection = canvas.chapter.to_dict(), (canvas.selected_kind, canvas.selected_id)
    revision, dirty = canvas.command_stack.revision, window._dirty
    original = render(canvas)

    def interact(menu):
        for index, candidate in enumerate((left, right, group)):
            hover(menu, menu.actions()[index])
            assert canvas.solo_entities == {candidate}
            image = render(canvas)
            assert (image.pixelColor(40, 60).alpha() > 0) == (candidate != right)
            assert (image.pixelColor(105, 60).alpha() > 0) == (candidate != left)
            assert (image.pixelColor(25, 110).alpha() > 0) == (candidate == group)
            assert canvas.chapter.to_dict() == before
            assert (canvas.selected_kind, canvas.selected_id) == selection
        QCoreApplication.sendEvent(menu, QEvent(QEvent.Leave))
        assert canvas.solo_entities == prior
        assert render(canvas) == original
        hover(menu, menu.actions()[0])
        assert canvas.solo_entities == {left}
        QTest.keyClick(menu, Qt.Key_Escape)
        assert canvas.solo_entities == prior

    run_chooser(window, interact)
    assert render(canvas) == original
    assert canvas.chapter.to_dict() == before
    assert (canvas.selected_kind, canvas.selected_id) == selection
    assert canvas.command_stack.revision == revision
    assert window._dirty == dirty


@pytest.mark.parametrize("method", ["mouse", "keyboard", "pen_menu", "pen_canvas"])
def test_choosing_candidate_restores_solo_and_selects_once(chooser_window, monkeypatch, method):
    window, canvas = chooser_window, chooser_window.canvas
    left, right, _ = window._test_candidates
    canvas.set_solo_entities({left})
    selected = []
    canvas.selectionChanged.connect(lambda kind, identifier: selected.append((kind, identifier)))
    before = canvas.chapter.to_dict()
    # A committed selection normally remembers its parent's last raster.
    parent_id = canvas.chapter.objects[right[1]].parent_layer_id
    next(layer for layer in before["layers"] if layer["id"] == parent_id)["last_raster_id"] = right[1]
    if method == "pen_canvas":
        # Windows can retain the originating canvas's tablet grab even though
        # the pen is positioned over the popup, and widgetAt can report canvas.
        monkeypatch.setattr(QApplication, "widgetAt", lambda _: canvas)

    def interact(menu):
        action = menu.actions()[1]
        position = menu.actionGeometry(action).center()
        if method.startswith("pen"):
            target = canvas if method == "pen_canvas" else menu
            tablet(target, menu, QEvent.TabletMove, position)
            assert canvas.solo_entities == {right}
            tablet(target, menu, QEvent.TabletPress, position)
            tablet(target, menu, QEvent.TabletRelease, position)
        elif method == "keyboard":
            menu.setActiveAction(menu.actions()[0])
            QTest.keyClick(menu, Qt.Key_Down)
            assert canvas.solo_entities == {right}
            QTest.keyClick(menu, Qt.Key_Return)
        else:
            QTest.mouseMove(menu, position)
            QTest.mouseClick(menu, Qt.LeftButton, pos=position)
        assert not menu.isVisible()
        assert canvas.solo_entities == {left}

    run_chooser(window, interact)
    assert selected == [right]
    assert (canvas.selected_kind, canvas.selected_id) == right
    assert canvas.chapter.to_dict() == before
    assert not canvas._pen_contact_active
    assert not canvas._tablet_tool_active


def test_ctrl_pen_open_release_hover_and_outside_dismissal(chooser_window, monkeypatch):
    window, canvas = chooser_window, chooser_window.canvas
    original_selection = canvas.selected_kind, canvas.selected_id
    # Use event modifiers rather than relying on global keyboard polling.
    monkeypatch.setattr("comic_editor.ui.canvas.QGuiApplication.keyboardModifiers", lambda: Qt.NoModifier)
    monkeypatch.setattr(QApplication, "widgetAt", lambda _: canvas)
    canvas.center_x, canvas.center_y, canvas.scale = 100, 100, 1

    def interact(menu):
        assert not canvas._pen_contact_active
        assert not canvas._tablet_tool_active
        position = menu.actionGeometry(menu.actions()[0]).center()
        tablet(canvas, menu, QEvent.TabletRelease, position)
        assert menu.isVisible()
        assert (canvas.selected_kind, canvas.selected_id) == original_selection
        tablet(canvas, menu, QEvent.TabletMove, position)
        assert canvas.solo_entities == {tuple(menu.actions()[0].data())}
        outside = QPoint(-15, -15)
        tablet(canvas, menu, QEvent.TabletMove, outside)
        assert not canvas.solo_entities
        tablet(canvas, menu, QEvent.TabletPress, outside)
        assert not menu.isVisible()

    def open_menu():
        position = canvas.document_to_widget(QPointF(75, 60)).toPoint()
        tablet(canvas, canvas, QEvent.TabletPress, position, modifiers=Qt.ControlModifier)

    run_chooser(window, interact, open_menu)
    assert not canvas.solo_entities
    assert (canvas.selected_kind, canvas.selected_id) == original_selection
