"""Closing an open shape keeps its curve and adds one straight filled seam."""
import copy

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, PathContour, PathNode, RasterObject, ShapeStyle,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.shape_contours import raw_edge_cubics


@pytest.fixture
def scene(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)
    chapter = ChapterDocument(width=128, height=128, document_kind="image", background="#00000000")
    page = chapter.add_page("Test page", BoundGeometry.rectangle(0, 0, 128, 128))
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Open triangle", BoundGeometry.path([
        PathNode(x=24, y=24), PathNode(x=104, y=24), PathNode(x=104, y=104),
    ], closed=False), layer_kind="open_shape", style=ShapeStyle(
        primary_color="#FF375B8A", base_thickness=4, outline_color="#FF101010",
        outline_thickness=2, start_cap="square", end_cap="point"))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                                         grid_overlay_visible=False))
    canvas.resize(128, 128)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", layer.layer_id)
    assert canvas.set_tool(ToolKind.SHAPE_EDIT)
    yield canvas, page.layer_id, layer.layer_id
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.close()
    canvas.deleteLater()


def render(canvas):
    image = QImage(128, 128, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image, source_rect=QRectF(0, 0, 128, 128))
    return image


def test_close_fills_shape_with_one_undo_step_and_exact_redo_and_serialization(scene):
    canvas, _page_id, layer_id = scene
    before = copy.deepcopy(canvas.chapter.to_dict())
    before_image = render(canvas)
    revision = canvas.command_stack.revision
    assert before_image.pixelColor(88, 48).alpha() == 0
    assert canvas.can_close_selected_shape()
    assert canvas.close_selected_shape()
    layer = canvas.chapter.layers[layer_id]
    assert layer.layer_kind == "bounded" and layer.bound.closed
    assert len(layer.bound.nodes) == 3
    assert canvas.command_stack.revision == revision + 1
    assert layer.shape_style.to_dict() == before["layers"][1]["shape_style"]
    after_image = render(canvas)
    assert after_image.pixelColor(88, 48) == QColor("#FF375B8A")
    assert after_image.pixelColor(24, 104).alpha() == 0
    after = copy.deepcopy(canvas.chapter.to_dict())
    assert not canvas.can_close_selected_shape()
    assert not canvas.close_selected_shape()
    assert canvas.command_stack.revision == revision + 1
    canvas.command_stack.undo()
    assert canvas.chapter.to_dict() == before
    assert render(canvas) == before_image
    canvas.command_stack.redo()
    assert canvas.chapter.to_dict() == after
    assert render(canvas) == after_image
    restored = ChapterDocument.from_dict(copy.deepcopy(after))
    restored.validate()
    assert restored.to_dict() == after


def test_close_preserves_existing_beziers_transform_style_and_other_contours(scene):
    canvas, _page_id, layer_id = scene
    layer = canvas.chapter.layers[layer_id]
    layer.bound = BoundGeometry.path([
        PathNode(x=20, y=20, point_type="bezier", outgoing=(30, 0),
                 roundness=17, roundness_enabled=True),
        PathNode(x=100, y=20, point_type="bezier", incoming=(90, 0),
                 outgoing=(110, 35), handles_locked=False, roundness=6),
        PathNode(x=100, y=100, point_type="bezier", incoming=(115, 80),
                 roundness=12, roundness_enabled=True),
    ], closed=False)
    layer.bound.additional_contours = [PathContour(nodes=[
        PathNode(x=30, y=25), PathNode(x=70, y=25),
    ], closed=False)]
    layer.transform_frame = (20., 20., 80., 80.)
    layer.transform_quad = [(24., 22.), (108., 25.), (104., 106.), (25., 100.)]
    layer.opacity = .7
    before = copy.deepcopy(layer.to_dict())
    original_curves = raw_edge_cubics(layer.bound)
    assert canvas.close_selected_shape()
    after = layer.to_dict()
    for name in ("id", "name", "parent_id", "translation", "transform_frame",
                 "transform_quad", "opacity", "shape_style", "modifier_ids"):
        assert after[name] == before[name]
    assert layer.bound.nodes[1].to_dict() == before["bound"]["nodes"][1]
    assert after["bound"]["additional_contours"] == before["bound"]["additional_contours"]
    assert raw_edge_cubics(layer.bound)[:-1] == original_curves
    first, last = layer.bound.nodes[0], layer.bound.nodes[-1]
    assert first.incoming == first.position
    assert last.outgoing == last.position
    assert not first.handles_locked and not last.handles_locked
    assert not first.roundness_enabled and not last.roundness_enabled
    # Cubic controls placed at the endpoints keep the new edge exactly on
    # the straight segment instead of extending the old endpoint tangents.
    assert raw_edge_cubics(layer.bound)[-1] == (
        last.position, last.position, first.position, first.position)


def test_two_node_curve_gets_vector_seam_midpoint_and_visible_fill(scene):
    canvas, _page_id, layer_id = scene
    layer = canvas.chapter.layers[layer_id]
    layer.bound = BoundGeometry.path([
        PathNode(x=24, y=96, point_type="bezier", outgoing=(24, 8)),
        PathNode(x=104, y=96, point_type="bezier", incoming=(104, 8)),
    ], closed=False)
    node_ids = [node.node_id for node in layer.bound.nodes]
    curve = raw_edge_cubics(layer.bound)[0]
    assert canvas.can_close_selected_shape()
    assert canvas.close_selected_shape()
    layer.bound.validate()
    assert [node.node_id for node in layer.bound.nodes[:2]] == node_ids
    assert len(layer.bound.nodes) == 3
    midpoint = layer.bound.nodes[-1]
    assert midpoint.point_type == "vector"
    assert midpoint.position == (64., 96.)
    assert raw_edge_cubics(layer.bound)[0] == curve
    assert all(point[1] == 96 for edge in raw_edge_cubics(layer.bound)[1:] for point in edge)
    assert render(canvas).pixelColor(64, 64) == QColor("#FF375B8A")


def test_missing_primary_color_uses_secondary_for_new_fill(scene):
    canvas, _page_id, layer_id = scene
    layer = canvas.chapter.layers[layer_id]
    layer.fill_color = None
    canvas.secondary_color = "#FFCC7722"
    assert canvas.close_selected_shape()
    assert layer.fill_color == canvas.secondary_color
    assert render(canvas).pixelColor(88, 48) == QColor(canvas.secondary_color)


@pytest.mark.parametrize("ineligible", ["no_chapter", "no_selection", "object", "page",
                                         "closed", "wrong_tool", "missing_bound"])
def test_ineligible_close_does_not_change_model_or_history(scene, ineligible):
    canvas, page_id, layer_id = scene
    if ineligible == "no_chapter":
        canvas.chapter = None
    elif ineligible == "no_selection":
        canvas.clear_selection()
    elif ineligible == "object":
        obj = canvas.chapter.add_object(layer_id, RasterObject())
        canvas.set_selection("object", obj.object_id)
    elif ineligible == "page":
        canvas.set_selection("layer", page_id)
    elif ineligible == "closed":
        canvas.chapter.layers[layer_id].bound.closed = True
        canvas.chapter.layers[layer_id].layer_kind = "bounded"
    elif ineligible == "wrong_tool":
        assert canvas.set_tool(ToolKind.TRANSFORM)
    else:
        canvas.chapter.layers[layer_id].bound = None
    before = copy.deepcopy(canvas.chapter.to_dict()) if canvas.chapter else None
    revision = canvas.command_stack.revision
    assert not canvas.can_close_selected_shape()
    assert not canvas.close_selected_shape()
    assert canvas.command_stack.revision == revision
    assert (canvas.chapter.to_dict() if canvas.chapter else None) == before
