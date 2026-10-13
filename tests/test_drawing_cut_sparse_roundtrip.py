"""A cleared cut tile stays absent through redo and durable saves."""
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath

from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.canvas import ToolKind
from comic_editor.ui.main_window import MainWindow


@pytest.mark.parametrize('high_precision', [False, True])
@pytest.mark.parametrize('whole_tile', [False, True])
def test_cut_undo_redo_save_reopen_keeps_sparse_absence_and_native_originals(
        qapp, tmp_path, monkeypatch, high_precision, whole_tile):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    repository = SeriesRepository(tmp_path / 'Series')
    series = repository.create('Cut roundtrip')
    chapter, tiles = repository.create_chapter(series, 'Native source')
    obj = next(item for item in chapter.objects.values() if isinstance(item, RasterObject))
    image_format = QImage.Format_RGBA64 if high_precision else QImage.Format_ARGB32_Premultiplied
    for key in ((0, 0), (1, 0)):
        image = QImage(256, 256, image_format)
        image.fill(QColor(0, 0, 0, 0))
        painter = QPainter(image)
        painter.fillRect(QRectF(20, 20, 40, 40),
                         QColor.fromRgbF(.3123, .5345, .7967, .4132))
        painter.end()
        tiles.set_tile(obj.object_id, key, image)
    repository.save_chapter(chapter, tiles)
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        canvas = window.canvas
        canvas.set_selection('object', obj.object_id)
        canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
        originals = canvas.tiles.object_tiles(obj.object_id)
        path = QPainterPath()
        path.addRect(QRectF(0, 0, 100 if whole_tile else 40, 100))
        canvas._drawing_selection_path = path
        canvas._refresh_drawing_selection_transform()
        expected = QImage(originals[(0, 0)])
        painter = QPainter(expected)
        painter.setCompositionMode(QPainter.CompositionMode_Clear)
        painter.fillPath(path, QColor('black'))
        painter.end()
        assert window._cut_drawing_selection()
        assert window.save(wait=True)
        initial_cut = canvas.tiles.object_tiles(obj.object_id)
        assert ((0, 0) not in initial_cut) == whole_tile
        assert initial_cut[(1, 0)] == originals[(1, 0)]
        if not whole_tile:
            assert initial_cut[(0, 0)] == expected
        command = canvas.command_stack.top_undo_command
        assert (command.after[(0, 0)] is None) == whole_tile
        tile_file = repository.chapter_root(chapter.chapter_id) / 'raster' / obj.object_id / '0_0.png'
        assert tile_file.exists() != whole_tile
        for redo in (False, True, False, True):
            if redo:
                canvas.command_stack.redo()
            else:
                canvas.command_stack.undo()
            expected_tiles = initial_cut if redo else originals
            assert canvas.tiles.object_tiles(obj.object_id) == expected_tiles
            assert window.save(wait=True)
            assert tile_file.exists() == ((0, 0) in expected_tiles)
            _chapter, reopened = repository.load_chapter(chapter.chapter_id)
            actual = reopened.object_tiles(obj.object_id)
            assert actual == expected_tiles
            assert actual[(1, 0)].format() == originals[(1, 0)].format()
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()
        window.deleteLater()
