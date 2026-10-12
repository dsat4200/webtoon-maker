"""Native closing-edge updates equal full lasso redraws at every input point."""
from collections import Counter

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.tiles import TileStore
from comic_editor.ui import lasso_brush_features
from comic_editor.ui.canvas import ToolKind
from test_lasso_brush import make_canvas


def _source(canvas, raster):
    return {key: (image.format(), image.width(), image.height(), bytes(image.constBits()))
            for key, image in canvas.tiles.object_tiles(raster.object_id).items()}


def _scene(image_format, color):
    canvas, chapter, raster = make_canvas()
    canvas.set_document(chapter, TileStore(tile_size=64))
    canvas.set_selection('object', raster.object_id)
    canvas.set_tool(ToolKind.LASSO_BRUSH)
    canvas.rotation = -.663008407605119  # Captured camera angle is degrees.
    canvas.scale = 1.1877456524942058
    chapter.layers[raster.parent_layer_id].translate_x = 73.125
    chapter.layers[raster.parent_layer_id].translate_y = 81.375
    raster.x, raster.y = 4.25, 6.125
    raster.transform_frame = (4.25, 6.125, 400., 400.)
    raster.transform_quad = [(5.125, 8.25), (420.125, 15.25),
                             (410.125, 423.25), (-4.875, 416.25)]
    for y in range(-1, 8):
        for x in range(-1, 8):
            image = QImage(64, 64, image_format)
            image.fill(QColor.fromRgbF(.125 + (x % 3)*.125,
                                       .25 + (y % 3)*.125, .8125, .875))
            canvas.tiles.set_tile(raster.object_id, (x, y), image)
    canvas.primary_color = color
    return canvas, chapter, raster


@pytest.mark.parametrize('image_format', [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA64_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
@pytest.mark.parametrize('color', ['#80CC3311', '#00000000'])
def test_changed_native_tiles_equal_former_full_redraw_and_preserve_history(
        qapp, monkeypatch, image_format, color):
    actual, chapter, raster = _scene(image_format, color)
    reference, old_chapter, old_raster = _scene(image_format, color)
    # This is the former whole-polygon algorithm. Only its affected-tile
    # optimization is disabled; source acquisition and QPainter stay real.
    reference._lasso_brush_changed_coverage = lambda *_args: None
    original = _source(actual, raster)
    frame, height = raster.interaction_rect, chapter.height
    counts, active = Counter(), [None]
    painter = lasso_brush_features.QPainter
    def counted(*args):
        counts[active[0]] += 1
        return painter(*args)
    counted.Antialiasing = painter.Antialiasing
    counted.CompositionMode_DestinationOut = painter.CompositionMode_DestinationOut
    monkeypatch.setattr(lasso_brush_features, 'QPainter', counted)
    points = [(4.125, 4.25), (390.875, 4.25), (390.875, 390.75)]
    points += [(390.875-index*12.5, 390.75) for index in range(1, 31)]
    # Reversals, tile boundaries, and self crossings can remove earlier fill.
    points += [(256.125, 2.875), (5.875, 256.125), (256.125, 256.125),
               (2.875, 2.875), (-.125, 128.125), (128.125, -.125)]
    try:
        for canvas, obj, label in ((reference, old_raster, 'full'), (actual, raster, 'changed')):
            active[0] = label
            mapping = canvas._drawing_local_to_world_transform(obj)
            canvas._begin_lasso_brush(mapping.map(QPointF(*points[0])))
        for point in points[1:]:
            for canvas, obj, label in ((reference, old_raster, 'full'), (actual, raster, 'changed')):
                active[0] = label
                canvas._continue_lasso_brush(canvas._drawing_local_to_world_transform(obj)
                                             .map(QPointF(*point)))
                assert canvas._native_input_error is None
            assert _source(actual, raster) == _source(reference, old_raster), point
            assert actual._lasso_brush['preview_keys'] == reference._lasso_brush['preview_keys']
            assert raster.interaction_rect == old_raster.interaction_rect
            assert chapter.height == old_chapter.height
        assert counts['changed'] < counts['full']*.6, counts
        for canvas in (actual, reference):
            canvas._finish_lasso_brush()
        result = _source(actual, raster)
        assert result == _source(reference, old_raster)
        assert len(actual.command_stack._undo) == 1
        actual.command_stack.undo()
        assert _source(actual, raster) == original
        assert raster.interaction_rect == frame and chapter.height == height
        actual.command_stack.redo()
        assert _source(actual, raster) == result
    finally:
        actual.close()
        reference.close()


def test_new_native_lasso_tile_is_alpha_scanned_once(qapp, monkeypatch):
    canvas, _chapter, raster = make_canvas()
    calls = []
    alpha_bbox = canvas.tiles._alpha_bbox
    def measured(image):
        calls.append(image.cacheKey())
        return alpha_bbox(image)
    monkeypatch.setattr(canvas.tiles, '_alpha_bbox', measured)
    try:
        canvas._begin_lasso_brush(QPointF(12, 12))
        canvas._continue_lasso_brush(QPointF(50, 12))
        calls.clear()
        canvas._continue_lasso_brush(QPointF(50, 50))
        assert canvas._native_input_error is None
        assert len(calls) == 1
        assert set(canvas.tiles._tiles[raster.object_id]) == {(0, 0)}
        canvas._finish_lasso_brush()
    finally:
        canvas.close()
