"""Screen-relative brush sizing resolves once without mutating preset units."""
from dataclasses import replace
import math

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QTransform

from comic_editor.core.brushes import BrushDefinition
from comic_editor.core.brush_preview import render_brush_preview
from comic_editor.core.brush_view import resolve_brush_view_size
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.brush_controls import BrushSettingsDialog
from comic_editor.ui.canvas import CanvasWidget, ToolKind


def make_canvas(brush):
    chapter = ChapterDocument()
    page = chapter.add_page("Screen size", BoundGeometry.rectangle(0, 0, 1080, 1080))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 100, 100)))
    settings = EditorSettings(brush_presets=[brush.to_dict()], active_brush_id=brush.id,
                              brush_size_px=brush.size, snap_to_grid=False)
    canvas = CanvasWidget(settings)
    canvas.resize(640, 480)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.BRUSH)
    return canvas, raster


def raster_pixels(canvas, raster):
    return {key: bytes(image.constBits()) for key, image in canvas.tiles.object_tiles(raster.object_id).items()}


def test_screen_size_roundtrip_and_independent_second_brush(qapp):
    brush = BrushDefinition(size=20, size_by_view=True, dual=BrushDefinition(size=8))
    assert BrushDefinition.from_dict(brush.to_dict()) == brush
    assert not BrushDefinition.from_dict({"size": 20}).size_by_view
    resolved = resolve_brush_view_size(brush, 2)
    assert resolved.size == 10 and not resolved.size_by_view
    assert resolved.dual.size == 8
    linked = resolve_brush_view_size(replace(brush, dual_link_size=True), 2)
    assert linked.size == 10 and linked.dual.size == 4
    assert brush.size == 20 and brush.dual.size == 8
    dialog = BrushSettingsDialog(brush)
    assert dialog.controls["size_by_view"].isChecked()
    assert dialog.result_definition().to_dict() == brush.to_dict()
    dialog.controls["size_by_view"].setChecked(False)
    assert not dialog.result_definition().size_by_view
    dialog.close()


def test_screen_size_preview_uses_fixed_100_percent_view(qapp):
    fixed = BrushDefinition(size=10)
    by_view = replace(fixed, size_by_view=True)
    a = render_brush_preview(fixed, 120, 50)
    b = render_brush_preview(by_view, 120, 50)
    assert bytes(a.constBits()) == bytes(b.constBits())


@pytest.mark.parametrize("zoom,rotation", [(.5, 0), (1, 47), (3, -73)])
@pytest.mark.parametrize("object_scale", [1, 2])
def test_screen_size_matches_cursor_and_painted_local_diameter(qapp, zoom, rotation, object_scale):
    brush = BrushDefinition(size=24, size_by_view=True, antialiasing=0)
    canvas, raster = make_canvas(brush)
    canvas.scale, canvas.rotation = zoom, rotation
    raster.transform_frame = (0, 0, 100, 100)
    transform = QTransform().translate(180, 140).rotate(29).scale(object_scale, object_scale)
    raster.transform_quad = [transform.map(QPointF(x, y)).toTuple()
                             for x, y in ((0, 0), (100, 0), (100, 100), (0, 100))]
    local = QPointF(50, 50)
    world = canvas._raster_world_point(raster, local)
    center = canvas.camera_transform().map(world)
    cursor = canvas._paint_brush_cursor_path(center)
    assert cursor.boundingRect().width() == pytest.approx(24, abs=.03)
    assert cursor.boundingRect().height() == pytest.approx(24, abs=.03)
    saved = list(canvas.settings.brush_presets)
    canvas._begin_paint_brush(world, 1)
    assert canvas._paint_brush_stroke.definition.size == pytest.approx(24 / (zoom * object_scale))
    canvas._finish_paint_brush()
    bounds = canvas.tiles.content_bounds(raster.object_id)
    assert abs(bounds.width() * zoom * object_scale - 24) <= 2 * zoom * object_scale
    assert canvas.settings.brush_presets == saved and canvas.settings.brush_size_px == 24
    after = raster_pixels(canvas, raster)
    assert len(canvas.command_stack._undo) == 1
    canvas.command_stack.undo()
    assert canvas.tiles.content_bounds(raster.object_id) is None
    canvas.command_stack.redo()
    assert raster_pixels(canvas, raster) == after
    canvas.close()


def test_screen_size_and_linked_secondary_are_frozen_until_next_stroke(qapp):
    brush = BrushDefinition(size=24, size_by_view=True, dual=BrushDefinition(size=8), dual_link_size=True)
    canvas, raster = make_canvas(brush)
    canvas.scale = .5
    canvas._begin_paint_brush(QPointF(30, 30), 1)
    stroke = canvas._paint_brush_stroke
    assert (stroke.definition.size, stroke.definition.dual.size) == (48, 16)
    canvas.scale = 2
    canvas._continue_paint_brush(QPointF(60, 30), 1)
    assert (stroke.definition.size, stroke.definition.dual.size) == (48, 16)
    canvas._finish_paint_brush()
    canvas._begin_paint_brush(QPointF(30, 70), 1)
    assert (canvas._paint_brush_stroke.definition.size, canvas._paint_brush_stroke.definition.dual.size) == (12, 4)
    canvas._finish_paint_brush()
    assert canvas.settings.brush_presets[0]["size"] == 24
    canvas.close()


def test_nonuniform_projective_raster_uses_pen_down_equal_area_scale(qapp):
    canvas, raster = make_canvas(BrushDefinition(size=24, size_by_view=True))
    canvas.scale, canvas.rotation = 1.7, 35
    raster.transform_frame = (0, 0, 100, 100)
    raster.transform_quad = [(180, 280), (380, 240), (400, 460), (160, 480)]
    local = QPointF(30, 40)
    transform = canvas._drawing_local_to_world_transform(raster) * canvas.camera_transform()
    # Independent finite differences check the projective Jacobian's area scale.
    delta = .0001
    dx = (transform.map(local + QPointF(delta, 0)) - transform.map(local - QPointF(delta, 0))) / (2 * delta)
    dy = (transform.map(local + QPointF(0, delta)) - transform.map(local - QPointF(0, delta))) / (2 * delta)
    scale = math.sqrt(abs(dx.x() * dy.y() - dx.y() * dy.x()))
    assert canvas._paint_brush_view_scale(raster, local) == pytest.approx(scale, rel=1e-6)
    world = canvas._raster_world_point(raster, local)
    canvas._begin_paint_brush(world, 1)
    assert canvas._paint_brush_stroke.definition.size == pytest.approx(24 / scale)
    canvas._finish_paint_brush()
    canvas.close()


def test_fixed_size_brush_keeps_existing_local_size_under_zoom_and_transform(qapp):
    canvas, raster = make_canvas(BrushDefinition(size=24))
    canvas.scale = 3
    raster.transform_frame = (0, 0, 100, 100)
    raster.transform_quad = [(0, 0), (200, 0), (200, 200), (0, 200)]
    world = canvas._raster_world_point(raster, QPointF(30, 30))
    center = canvas.camera_transform().map(world)
    assert canvas._paint_brush_cursor_path(center).boundingRect().width() == pytest.approx(144)
    canvas._begin_paint_brush(world, 1)
    assert canvas._paint_brush_stroke.definition.size == 24
    canvas._finish_paint_brush()
    canvas.close()
