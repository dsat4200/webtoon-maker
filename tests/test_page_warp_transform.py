"""An ancestor move must carry a child's world-space warp through the preview."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QEvent, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QKeyEvent, QPainter

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, DistortModifier, ImageObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind


@pytest.fixture
def page_scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(width=1080, height=512)
    page = chapter.add_page("Moving page", BoundGeometry.rectangle(20, 20, 260, 260))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                         grid_overlay_visible=False))
    canvas.resize(512, 512)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.center_x = canvas.center_y = 256
    canvas.scale = 1
    obj = chapter.add_object(page.layer_id, ImageObject(x=70, y=70, pixel_width=128, pixel_height=128))
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#c0d0e0"))
    painter = QPainter(image)
    for y in range(0, 128, 16):
        for x in range(0, 128, 16):
            if (x // 16 + y // 16) % 2:
                painter.fillRect(x, y, 16, 16, QColor("#ff6c20"))
    painter.end()
    canvas.images.put_decoded(obj.object_id, "checker.png", b"", image)
    warp = DistortModifier(modifier_type="distort_mesh_warp", frame=(70, 70, 128, 128),
                           center=(134, 134), parameters={"rows": 3, "columns": 3,
                                                       "smoothness": 0, "interpolation": "nearest"})
    warp.validate()
    warp.points[4] = (.68, .35)
    chapter.add_modifier(warp, [("object", obj.object_id)])
    canvas.set_selection("layer", page.layer_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    yield canvas, page, warp
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def pixels(canvas, delta=(0, 0)):
    image = QImage(320, 320, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, source_rect=QRectF(delta[0], delta[1], 320, 320))
    return np.frombuffer(image.constBits(), np.uint8).copy()


def start_move(canvas, delta):
    start = QPointF(140, 140)
    assert canvas._begin_geometry_transform(start)
    assert canvas._transform_drag_mode == "translate"
    canvas._update_geometry_transform_preview(start + QPointF(*delta))


def test_page_translation_keeps_warp_aligned_during_preview_commit_and_history(page_scene):
    canvas, _page, warp = page_scene
    before = pixels(canvas)
    model = copy.deepcopy(canvas.chapter.to_dict())
    rig = warp.to_dict()
    delta = (35, 23)
    start_move(canvas, delta)
    np.testing.assert_array_equal(pixels(canvas, delta), before)
    assert canvas.chapter.to_dict() == model  # Preview must not alter saved rig data.
    canvas._commit_geometry_transform()
    np.testing.assert_array_equal(pixels(canvas, delta), before)
    assert canvas.chapter.modifiers[warp.modifier_id].frame[:2] == (105, 93)
    committed = canvas.chapter.to_dict()
    canvas.command_stack.undo()
    assert canvas.chapter.modifiers[warp.modifier_id].to_dict() == rig
    np.testing.assert_array_equal(pixels(canvas), before)
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == committed
    np.testing.assert_array_equal(pixels(canvas, delta), before)


def test_cancelling_page_move_restores_warp_pixels_without_mutating_model(page_scene):
    canvas, _page, _warp = page_scene
    before = pixels(canvas)
    model = copy.deepcopy(canvas.chapter.to_dict())
    start_move(canvas, (35, 23))
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), before)
    QCoreApplication.sendEvent(canvas, QKeyEvent(QEvent.KeyPress, Qt.Key_Escape, Qt.NoModifier))
    assert canvas._geometry_transform_target is None
    assert canvas.chapter.to_dict() == model
    np.testing.assert_array_equal(pixels(canvas), before)


def test_committed_page_translation_is_rigid_without_preview_render(page_scene):
    canvas, _page, _warp = page_scene
    before = pixels(canvas)
    start_move(canvas, (35, 23))
    canvas._commit_geometry_transform()
    committed = canvas.chapter.to_dict()
    canvas.chapter.validate()
    assert canvas.chapter.to_dict() == committed
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), before)


def test_successive_preview_positions_do_not_accumulate_warp_translation(page_scene):
    canvas, _page, warp = page_scene
    before = pixels(canvas)
    model = copy.deepcopy(canvas.chapter.to_dict())
    rig = warp.to_dict()
    start = QPointF(140, 140)
    assert canvas._begin_geometry_transform(start)
    for delta in ((35, 23), (42, -10), (-9, 30), (0, 0)):
        canvas._update_geometry_transform_preview(start + QPointF(*delta))
        np.testing.assert_array_equal(pixels(canvas, delta), before)
        assert canvas.chapter.to_dict() == model
    canvas._commit_geometry_transform()
    current = canvas.chapter.modifiers[warp.modifier_id].to_dict()
    np.testing.assert_allclose(current.pop("points"), rig.pop("points"), rtol=0, atol=1e-12)
    assert current == rig
    np.testing.assert_array_equal(pixels(canvas), before)


@pytest.mark.parametrize("stylus", [False, True], ids=["mouse", "pen"])
def test_pointer_drag_and_final_release_keep_warp_aligned(page_scene, stylus):
    from test_distort_pointer_events import pointer

    canvas, _page, _warp = page_scene
    before = pixels(canvas)
    initial = copy.deepcopy(canvas.chapter.to_dict())
    revision = canvas.command_stack.revision
    start = QPointF(140, 140)
    pointer(canvas, stylus, "press", start)
    assert canvas._transform_drag_mode == "translate"
    pointer(canvas, stylus, "move", start + QPointF(20, 10))
    np.testing.assert_array_equal(pixels(canvas, (20, 10)), before)
    assert canvas.chapter.to_dict() == initial
    pointer(canvas, stylus, "release", start + QPointF(35, 23))
    assert canvas._geometry_transform_target is None
    assert canvas.command_stack.revision == revision + 1
    np.testing.assert_array_equal(pixels(canvas, (35, 23)), before)
