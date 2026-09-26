"""The Shape Edit action follows selection and enables the existing Fill tool."""
from PySide6.QtCore import QPoint
from PySide6.QtGui import QFont

from comic_editor.core.models import BoundGeometry, ChapterDocument, PathNode
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tool_ribbon_pages import ToolSettingsControls


def test_shape_settings_button_closes_and_refreshes_with_history(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    monkeypatch.setattr("comic_editor.ui.clipboard_history.create_network_manager", lambda *_: None)
    monkeypatch.setattr(MainWindow, "_clipboard_data_changed", lambda *_: None)
    monkeypatch.setattr(
        "comic_editor.integrations.blender_source.BlenderSourceClient.connect_to_provider",
        lambda *_: None,
    )
    window = MainWindow()
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 400, 400))
    shape = chapter.add_layer(page.layer_id, "Open shape", BoundGeometry.path([
        PathNode(x=50, y=50), PathNode(x=350, y=50), PathNode(x=200, y=300),
    ]), layer_kind="open_shape")
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("layer", shape.layer_id)
        assert window._activate_tool(ToolKind.SHAPE_EDIT)
        controls = window.tool_settings_controls
        button = controls.close_shape_button
        assert controls.context_label.text() == "Shape Edit"
        assert controls.stack.currentWidget() is controls.shape_edit_page
        assert button.isEnabled() and not button.isHidden()
        assert window.fill_tool_button.isHidden()
        button.click()
        assert window.chapter.layers[shape.layer_id].bound.closed
        assert window.canvas.selected_id == shape.layer_id
        assert not button.isEnabled()
        assert not window.fill_tool_button.isHidden()
        assert window.fill_tool_button.isEnabled()
        assert len(window.canvas.command_stack._undo) == 1
        window._undo()
        assert not window.chapter.layers[shape.layer_id].bound.closed
        assert button.isEnabled() and window.fill_tool_button.isHidden()
        window._redo()
        assert window.chapter.layers[shape.layer_id].bound.closed
        assert not button.isEnabled() and not window.fill_tool_button.isHidden()
        window._undo()
        window.canvas.set_selection("layer", page.layer_id)
        assert not button.isEnabled()
        window.canvas.set_selection("layer", shape.layer_id)
        assert button.isEnabled()
    finally:
        window._dirty = False
        window.close()
        window.deleteLater()


def test_close_shape_settings_fit_narrow_sidebar(qapp, text_outline_font_family):
    controls = ToolSettingsControls(EditorSettings())
    controls.setFont(QFont(text_outline_font_family, 9))
    controls.set_context(ToolKind.SHAPE_EDIT, False, can_close_shape=True)
    controls.setFixedWidth(160)
    controls.resize(160, 400)
    controls.show()
    try:
        qapp.processEvents()
        button = controls.close_shape_button
        left = button.mapTo(controls, QPoint()).x()
        assert 0 <= left and left + button.width() <= controls.width()
        assert button.width() >= button.sizeHint().width()
        requests = []
        controls.closeShapeRequested.connect(lambda: requests.append(True))
        button.click()
        assert requests == [True]
        controls.set_context(ToolKind.SHAPE_EDIT, False, can_close_shape=False)
        button.click()
        assert requests == [True]
    finally:
        controls.close()
        controls.deleteLater()
