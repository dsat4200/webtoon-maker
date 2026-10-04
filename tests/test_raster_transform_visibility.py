"""Raster transform frames never restrict the original sparse artwork."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(height=1200, background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 1200))
    page.fill_color, page.border_width = None, 0
    raster = chapter.add_object(page.layer_id, RasterObject(
        x=600, y=600, interaction_rect=(0, 0, 320, 320)))
    tiles = TileStore()
    for point, color in (((52, 52), "red"), ((280, 52), "blue"),
                         ((52, 280), "orange"), ((280, 280), "purple")):
        tiles.paint_dab(raster.object_id, QPointF(*point), 32, QColor(color),
                        square=True, antialias=False)
    canvas = CanvasWidget(EditorSettings(
        grid_overlay_visible=False, snap_to_grid=False, predictive_ink=False))
    canvas.setFixedSize(480, 480)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y, canvas.scale = 800, 800, 1
    canvas.set_selection("object", raster.object_id)
    yield canvas, raster
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def pixels(image):
    image = image.convertToFormat(QImage.Format_RGBA8888)
    return np.frombuffer(image.constBits(), np.uint8).reshape(
        image.height(), image.width(), 4).copy()


def artwork(canvas):
    canvas._ensure_scene_cache()
    return pixels(canvas._scene_cache)


def reference(canvas, raster):
    """Draw original tiles through the stored mapping without frame filtering."""
    image = QImage(canvas.size(), QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        painter.setRenderHint(QPainter.Antialiasing, False)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, False)
        painter.setTransform(canvas.camera_transform())
        painter.setTransform(canvas.layer_world_transform(raster.parent_layer_id), True)
        painter.setTransform(canvas._drawing_object_transform(raster), True)
        painter.translate(raster.x, raster.y)
        for (x, y), tile in canvas.tiles.iter_tiles(raster.object_id):
            painter.drawImage(x * 256, y * 256, tile)
    finally:
        painter.end()
    return pixels(image)


def transform(canvas, raster, mode):
    canvas.settings.transform_mode = mode
    canvas.set_tool(ToolKind.TRANSFORM)
    corner = QPointF(*canvas.object_world_quad(raster.object_id)[2])
    canvas._tool_press(canvas.document_to_widget(corner), 1)
    assert canvas._transform_handle_index == 2
    canvas._tool_move(canvas.document_to_widget(corner + QPointF(64, 80)), 1)
    preview = list(canvas._transform_preview_quad)
    canvas._tool_release()
    assert raster.transform_quad == preview


@pytest.mark.parametrize("mode", ["free", "uniform"])
def test_raster_transform_commit_preserves_all_sparse_pixels(scene, mode):
    canvas, raster = scene
    original = artwork(canvas)
    tile_ids = {key: tile.cacheKey() for key, tile in canvas.tiles.iter_tiles(raster.object_id)}
    transform(canvas, raster, mode)
    expected = reference(canvas, raster)
    assert not np.array_equal(original, expected)
    np.testing.assert_array_equal(artwork(canvas), expected)
    assert tile_ids == {key: tile.cacheKey() for key, tile in canvas.tiles.iter_tiles(raster.object_id)}
    canvas.command_stack.undo()
    np.testing.assert_array_equal(artwork(canvas), original)
    canvas.command_stack.redo()
    np.testing.assert_array_equal(artwork(canvas), expected)


@pytest.mark.parametrize("mode", ["free", "uniform"])
def test_strokes_after_transform_never_reveal_or_hide_old_tile_chunks(scene, qapp, mode):
    canvas, raster = scene
    transform(canvas, raster, mode)
    mapping = raster.transform_frame, list(raster.transform_quad)
    artwork(canvas)  # Warm exact tiles before the editing frame expands.
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.set_active_colors("#FF00AA00", "#FFFFFFFF")
    canvas._begin_stroke(canvas._raster_world_point(raster, QPointF(150, 150)), 1)
    for point in ((155, 155), (160, 180), (180, 270), (270, 270)):
        canvas._continue_stroke(canvas._raster_world_point(raster, QPointF(*point)), 1)
        qapp.processEvents()
        np.testing.assert_array_equal(artwork(canvas), reference(canvas, raster))
    canvas._end_stroke()
    qapp.processEvents()
    np.testing.assert_array_equal(artwork(canvas), reference(canvas, raster))
    assert (raster.transform_frame, raster.transform_quad) == mapping
    for _ in range(2):
        canvas.command_stack.undo()
        np.testing.assert_array_equal(artwork(canvas), reference(canvas, raster))
        canvas.command_stack.redo()
        np.testing.assert_array_equal(artwork(canvas), reference(canvas, raster))


@pytest.mark.parametrize("mode", ["free", "uniform"])
def test_saved_transformed_raster_reopens_with_all_original_pixels(scene, tmp_path, mode):
    canvas, raster = scene
    transform(canvas, raster, mode)
    expected = reference(canvas, raster)
    chapter = ChapterDocument.from_dict(canvas.chapter.to_dict())
    canvas.tiles.save_directory(tmp_path, {raster.object_id}, complete=True)
    tiles = TileStore()
    tiles.load_directory(tmp_path, {raster.object_id})
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.center_y, canvas.scale = 800, 800, 1
    np.testing.assert_array_equal(artwork(canvas), expected)


def test_transformed_raster_source_queries_still_cull_offscreen_tiles(scene, monkeypatch):
    canvas, raster = scene
    transform(canvas, raster, "uniform")
    point = canvas._raster_world_point(raster, QPointF(52, 52))
    visible = QRectF(point.x() - 10, point.y() - 10, 20, 20)
    queried = []
    iterate = canvas.tiles.iter_tiles
    def watched(identifier, region=None):
        for key, tile in iterate(identifier, region):
            queried.append(key)
            yield key, tile
    monkeypatch.setattr(canvas.tiles, "iter_tiles", watched)
    image = QImage(1080, 1200, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    painter = QPainter(image)
    try:
        canvas._render_raster_content(painter, raster, visible, use_transform_preview=False)
    finally:
        painter.end()
    assert queried == [(0, 0)]
    assert image.pixelColor(round(point.x()), round(point.y())) == QColor("red")


def test_transformed_raster_preserves_pixels_outside_its_source_frame(scene):
    canvas, raster = scene
    canvas.tiles.paint_dab(raster.object_id, QPointF(-40, 280), 20,
                           QColor("cyan"), square=True, antialias=False)
    raster.transform_frame = (0, 0, 320, 320)
    raster.transform_quad = [(600, 600), (920, 600), (920, 920), (600, 920)]
    raster.x = raster.y = 0
    # Source and editing frames are identical here; neither bounds all pixels.
    canvas.center_x = 760
    canvas.documentChanged.emit(None)
    np.testing.assert_array_equal(artwork(canvas), reference(canvas, raster))
    point = canvas.document_to_widget(QPointF(560, 880)).toPoint()
    assert canvas._scene_cache.pixelColor(point) == QColor("cyan")
