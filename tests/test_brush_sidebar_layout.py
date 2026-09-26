"""Verify the brush page inside the actual narrow ribbon, after layout settles."""
from PySide6.QtCore import QPoint, Qt
from PySide6.QtTest import QTest

from comic_editor.core import settings as settings_module
from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.models import ChapterDocument, RasterObject
from comic_editor.core.tiles import TileStore


def test_real_sidebar_keeps_numeric_fields_and_preview_inside_viewport(qapp, tmp_path, monkeypatch):
    from comic_editor.ui.main_window import MainWindow
    from comic_editor.ui.canvas import ToolKind
    from PySide6.QtNetwork import QNetworkAccessManager

    # This local layout test never downloads images. Avoid unrelated Windows
    # TLS plugin discovery while keeping real Qt network-manager ownership.
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', QNetworkAccessManager)
    monkeypatch.setattr('comic_editor.ui.clipboard_history.create_network_manager', QNetworkAccessManager)

    preferences = tmp_path / 'sidebar-preferences.json'
    monkeypatch.setattr(settings_module, 'settings_path', lambda: preferences)
    window = MainWindow()
    try:
        # Imported names and notices must not enlarge the page. Keep the tips
        # simple: this tests the real container without rendering a whole pack.
        brushes = [BrushDefinition(id=f'layout-{i}',
                    name=f'{i:02d} A long downloaded brush name for a narrow sidebar',
                    warnings=('Compatibility notice',) * 12) for i in range(34)]
        window.settings.brush_presets = [b.to_dict() for b in brushes]
        window.settings.active_brush_id = brushes[0].id
        chapter = ChapterDocument()
        page = chapter.add_page()
        raster = chapter.add_object(page.layer_id, RasterObject())
        window._set_chapter(chapter, TileStore())
        window.resize(1320, 900)
        window.show()
        qapp.processEvents()
        window.canvas.set_selection('object', raster.object_id)
        window._activate_tool(ToolKind.BRUSH)
        window.tool_settings_controls.refresh()
        controls = window.tool_settings_controls.brush_page
        viewport = window.tool_settings_page.viewport()

        for width in (230, 220):
            window.workspace_splitter.setSizes([width, 1090])
            # Qt posts several nested layout requests; a single processEvents
            # call captures the old wide page before the scroll area catches up.
            QTest.qWait(350)
            for _ in range(6):
                qapp.processEvents()
            assert viewport.width() < 210
            for widget in (controls.presets, controls.size, controls.size_slider,
                           controls.opacity, controls.opacity_slider, controls.preview):
                left = widget.mapTo(viewport, QPoint()).x()
                assert 0 <= left
                assert left + widget.width() <= viewport.width()
            for field in (controls.size, controls.opacity):
                assert not field.lineEdit().isReadOnly()
                assert field.visibleRegion().boundingRect().width() == field.width()
            pixmap = controls.preview.pixmap()
            assert pixmap is not None and not pixmap.isNull()
            assert pixmap.width() <= controls.preview.contentsRect().width()

        controls.size.setFocus()
        controls.size.selectAll()
        QTest.keyClicks(controls.size, '31')
        QTest.keyClick(controls.size, Qt.Key_Tab)
        assert window.settings.brush_size_px == 31
    finally:
        window.autosave_timer.stop()
        window._autosave_jobs.shutdown()
        window.canvas._effect_jobs.cancel()
        window.deleteLater()
