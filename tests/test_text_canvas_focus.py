"""Canvas text pointer input must give the real keyboard focus to its editor."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui import main_window as main_window_module
from comic_editor.ui.canvas import CanvasWidget, GpuCanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow
from test_text_updates import _chapter_with_texts


@pytest.mark.parametrize('host', ['canvas', 'main_window'])
@pytest.mark.parametrize('backend', ['raster', 'gpu_widget_no_render'])
def test_actual_text_click_focus_after_selecting_and_leaving_font_size(qapp, monkeypatch, host, backend):
    monkeypatch.setattr(main_window_module, 'save_settings', lambda _value: None)
    monkeypatch.setattr(main_window_module, 'load_settings', lambda: EditorSettings(snap_to_grid=False))
    canvas_type = CanvasWidget
    if backend == 'gpu_widget_no_render':
        # Check QOpenGLWidget's focus ownership without starting a GPU renderer.
        canvas_type = GpuCanvasWidget
        monkeypatch.setattr(GpuCanvasWidget, 'paintEvent', lambda self, _event: self._update_text_gizmo_overlay())
        monkeypatch.setattr(main_window_module, 'create_canvas', lambda settings, parent=None: canvas_type(settings, parent))
    chapter, page, first, _second = _chapter_with_texts()
    first.text = 'alpha beta'
    if host == 'main_window':
        window = MainWindow()
        window.resize(1280, 900)
        window._set_chapter(chapter, TileStore())
        canvas = window.canvas
    else:
        window = canvas = canvas_type(EditorSettings(snap_to_grid=False))
        canvas.resize(900, 650)
        canvas.set_document(chapter, TileStore())
    canvas.center_x, canvas.center_y, canvas.scale = 360, 250, 1.0
    canvas.set_selection('layer', page.layer_id)
    canvas.set_tool(ToolKind.OBJECT_SELECT)
    window.show()
    window.activateWindow()
    qapp.processEvents()
    try:
        document, origin, transform = canvas._text_edit_layout(first)
        position = canvas.document_to_widget(
            origin + transform.map(canvas._text_caret_rect(document, 7).center())
        ).toPoint()
        assert canvas.rect().contains(position)

        QTest.mouseClick(canvas, Qt.MouseButton.LeftButton, pos=position)
        qapp.processEvents()
        assert canvas.selected_id == first.object_id
        assert canvas.tool == ToolKind.TEXT_EDIT
        assert qapp.focusWidget() is canvas

        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=position)
        qapp.processEvents()
        assert canvas._text_selection_range() == [6, 10]
        assert qapp.focusWidget() is canvas

        font_size = canvas._text_gizmo_overlay.size
        font_size.setFocus()
        qapp.processEvents()
        assert qapp.focusWidget() is font_size
        QTest.mouseDClick(canvas, Qt.MouseButton.LeftButton, pos=position)
        qapp.processEvents()
        assert canvas._text_selection_range() == [6, 10]
        assert qapp.focusWidget() is canvas
        QTest.keyClicks(qapp.focusWidget(), 'gamma')
        canvas.commit_active_text_edit()
        assert first.text == 'alpha gamma'
        assert first.font_size == 32
    finally:
        window.hide()
        window.deleteLater()
