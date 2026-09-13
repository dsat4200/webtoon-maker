"""Area gestures move each selected raster's pixels in document coordinates."""
import pytest

from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, RasterObject, TextObject, VectorDrawingObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


RED, BLUE, GREEN = QColor("#e83745"), QColor("#2549e2"), QColor("#1b9a57")


def _paint(tiles, obj, x, y, color):
    tiles.paint_dab(obj.object_id, QPointF(x, y), 12, color,
                    square=True, antialias=False)


def _pixel(tiles, obj, x, y):
    key = x // tiles.tile_size, y // tiles.tile_size
    image = tiles.tile(obj.object_id, key)
    return (image.pixelColor(x % tiles.tile_size, y % tiles.tile_size)
            if image is not None else QColor(Qt.transparent))


def _mouse(canvas, operation, x, y):
    point = canvas.document_to_widget(QPointF(x, y)).toPoint()
    if operation == "move":
        QTest.mouseMove(canvas, point)
    elif operation == "press":
        QTest.mousePress(canvas, Qt.LeftButton, Qt.NoModifier, point)
    else:
        QTest.mouseRelease(canvas, Qt.LeftButton, Qt.NoModifier, point)


def _lasso_and_translate(canvas):
    assert canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    _mouse(canvas, "press", 110, 80)
    for point in [(250, 80), (250, 170), (110, 170), (110, 80)]:
        _mouse(canvas, "move", *point)
    _mouse(canvas, "release", 110, 80)
    assert not canvas._drawing_selection_path.isEmpty()
    assert canvas._selection_transform_quad

    # Offset from the selection pivot so this drags its contents.
    _mouse(canvas, "press", 180, 145)
    assert canvas._selection_transform_mode == "translate"
    _mouse(canvas, "move", 270, 185)
    _mouse(canvas, "release", 270, 185)


@pytest.fixture
def layered_rasters(qapp):
    chapter = ChapterDocument(height=700)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 700))
    first = chapter.add_layer(page.layer_id, "First", BoundGeometry.rectangle(0, 0, 500, 400))
    first.translate_x, first.translate_y = 80, 20
    second = chapter.add_layer(page.layer_id, "Second", BoundGeometry.rectangle(0, 0, 500, 400))
    second.translate_x, second.translate_y = 180, 30
    a = chapter.add_object(first.layer_id, RasterObject(name="First ink", x=15, y=25, interaction_rect=(0, 0, 360, 300)))
    b = chapter.add_object(second.layer_id, RasterObject(name="Second ink", x=-25, y=30, interaction_rect=(0, 0, 360, 300)))
    untouched = chapter.add_object(page.layer_id, RasterObject(name="Unselected ink", interaction_rect=(0, 0, 500, 400)))
    tiles = TileStore()
    _paint(tiles, a, 50, 70, RED)   # World (145, 115).
    _paint(tiles, b, 40, 55, BLUE)  # World (195, 115).
    _paint(tiles, a, 260, 220, GREEN)
    _paint(tiles, b, 210, 225, GREEN)
    _paint(tiles, untouched, 140, 110, GREEN)
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False, grid_overlay_visible=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, tiles)
    canvas.show()
    qapp.processEvents()
    canvas.center_x, canvas.center_y, canvas.scale = 450, 350, 1
    yield canvas, chapter, first, second, a, b, untouched
    canvas._effect_jobs.cancel()
    canvas.hide()
    canvas.deleteLater()


def test_mouse_lasso_moves_local_pixels_across_parents_in_one_history_step(layered_rasters):
    canvas, chapter, first, second, a, b, untouched = layered_rasters
    refs = [("object", a.object_id), ("object", b.object_id)]
    assert canvas.set_selection_set(refs, primary=refs[0])
    tiles = canvas.tiles
    before = {obj.object_id: tiles.object_tiles(obj.object_id) for obj in (a, b, untouched)}
    initial_history = len(canvas.command_stack._undo)
    _lasso_and_translate(canvas)

    assert _pixel(tiles, a, 50, 70).alpha() == 0
    assert _pixel(tiles, b, 40, 55).alpha() == 0
    assert _pixel(tiles, a, 140, 110) == RED
    assert _pixel(tiles, b, 130, 95) == BLUE
    assert _pixel(tiles, a, 260, 220) == GREEN
    assert _pixel(tiles, b, 210, 225) == GREEN
    assert tiles.object_tiles(untouched.object_id) == before[untouched.object_id]
    assert len(canvas.command_stack._undo) == initial_history + 1
    assert a.parent_layer_id == first.layer_id and b.parent_layer_id == second.layer_id
    assert (a.x, a.y, b.x, b.y) == (15, 25, -25, 30)
    assert canvas.selected_entities == refs
    after = {obj.object_id: tiles.object_tiles(obj.object_id) for obj in (a, b, untouched)}

    canvas.command_stack.undo()
    assert {obj.object_id: tiles.object_tiles(obj.object_id) for obj in (a, b, untouched)} == before
    assert canvas.selected_entities == refs
    canvas.command_stack.redo()
    assert {obj.object_id: tiles.object_tiles(obj.object_id) for obj in (a, b, untouched)} == after
    assert a.parent_layer_id == first.layer_id and b.parent_layer_id == second.layer_id
    assert set(chapter.objects) == {a.object_id, b.object_id, untouched.object_id}


def test_selected_containers_deduplicate_rasters_and_skip_hidden_descendants(layered_rasters):
    canvas, chapter, first, second, a, b, _untouched = layered_rasters
    nested = chapter.add_layer(first.layer_id, "Nested", BoundGeometry.rectangle(0, 0, 500, 400))
    included = chapter.add_object(nested.layer_id, RasterObject(x=15, y=25))
    hidden = chapter.add_object(nested.layer_id, RasterObject(visible=False, x=15, y=25))
    mask_only = chapter.add_object(nested.layer_id, RasterObject(mask_only=True, x=15, y=25))
    hidden_parent = chapter.add_layer(first.layer_id, "Hidden branch")
    hidden_parent.visible = False
    hidden_child = chapter.add_object(hidden_parent.layer_id, RasterObject(x=15, y=25))
    chapter.add_object(nested.layer_id, VectorDrawingObject())
    for obj in (included, hidden, mask_only, hidden_child):
        _paint(canvas.tiles, obj, 50, 70, RED)
    excluded_before = {obj.object_id: canvas.tiles.object_tiles(obj.object_id)
                       for obj in (hidden, mask_only, hidden_child)}
    refs = [("layer", first.layer_id), ("layer", nested.layer_id),
            ("object", a.object_id), ("layer", second.layer_id)]
    assert canvas.set_selection_set(refs, primary=refs[0])
    targets = canvas._drawing_selection_raster_targets()
    assert {obj.object_id for obj in targets} == {a.object_id, b.object_id, included.object_id}
    assert len(targets) == 3

    _lasso_and_translate(canvas)
    for obj in (a, included):
        assert _pixel(canvas.tiles, obj, 50, 70).alpha() == 0
        assert _pixel(canvas.tiles, obj, 140, 110) == RED
        assert _pixel(canvas.tiles, obj, 230, 150).alpha() == 0
    assert _pixel(canvas.tiles, b, 130, 95) == BLUE
    assert {obj.object_id: canvas.tiles.object_tiles(obj.object_id)
            for obj in (hidden, mask_only, hidden_child)} == excluded_before


@pytest.mark.parametrize("object_type", [TextObject, VectorDrawingObject])
def test_explicit_nonraster_primary_rejects_area_selection(layered_rasters, object_type):
    canvas, chapter, first, _second, a, _b, _untouched = layered_rasters
    other = chapter.add_object(first.layer_id, object_type())
    refs = [("object", a.object_id), ("object", other.object_id)]
    assert canvas.set_selection_set(refs, primary=refs[1])
    before = chapter.to_dict()
    assert canvas._drawing_selection_raster_targets() == []
    assert not canvas.set_tool(ToolKind.DRAW_SELECT_LASSO)
    assert canvas.tool == ToolKind.TRANSFORM
    assert chapter.to_dict() == before
