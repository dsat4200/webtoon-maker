"""Raster effect windows stay stable during partial main-canvas repaints."""
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import BoundGeometry, ChapterDocument, CurvesModifier, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def scene(qapp, monkeypatch):
    chapter = ChapterDocument(width=240, height=180, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 240, 180))
    parent = chapter.add_layer(page.layer_id, "Sheared parent", BoundGeometry.rectangle(0, 0, 100, 100))
    parent.transform_frame = (0, 0, 100, 100)
    parent.transform_quad = [(0, 0), (120, 20), (145, 110), (25, 90)]
    obj = chapter.add_object(parent.layer_id, RasterObject(x=4, y=7,
        interaction_rect=(0, 0, 64, 64), transform_frame=(0, 0, 64, 64),
        transform_quad=[(10, 5), (80, 5), (92, 70), (22, 70)]))
    chapter.add_modifier(CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]}),
                         [("object", obj.object_id)])
    tiles = TileStore()
    tiles.paint_dab(obj.object_id, QPointF(32, 32), 40, QColor("#dd6622"))
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    canvas.set_document(chapter, tiles)
    canvas._interactive_render = True
    canvas._effect_viewport_world = QRectF(0, 0, 180, 120)
    requirements = []

    def stages(_canvas, image, bounds, _modifiers, _mapping, **options):
        required = options["required"]
        requirements.append(None if required is None else QRectF(required))
        return image, bounds

    monkeypatch.setattr("comic_editor.ui.effect_pipeline.render_stages", stages)
    yield canvas, obj, requirements
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render_raster(canvas, obj, visible):
    result = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
    result.fill(0)
    painter = QPainter(result)
    try:
        canvas._render_radial_raster(painter, obj, 1., visible)
    finally:
        painter.end()


def test_full_and_dirty_repaints_request_one_world_viewport_in_tile_coordinates(scene):
    canvas, obj, requirements = scene
    parent = canvas.layer_world_transform(obj.parent_layer_id)
    parent_inverse = parent.inverted()[0]
    placement = QTransform.fromTranslate(obj.x, obj.y) * canvas._drawing_object_transform(obj)
    expected = (placement * parent).inverted()[0].mapRect(canvas._effect_viewport_world)
    for world in (canvas._effect_viewport_world, QRectF(30, 40, 12, 14), QRectF(80, 20, 20, 17)):
        render_raster(canvas, obj, parent_inverse.mapRect(world))
    assert requirements == [expected, expected, expected]


@pytest.mark.parametrize("context", ["export", "source", "mask", "navigator", "no_viewport"])
def test_special_captures_keep_their_original_required_region(scene, context):
    canvas, obj, requirements = scene
    if context == "export":
        canvas._interactive_render = False
    elif context == "source":
        canvas._render_modifier_sources.add(("layer", obj.parent_layer_id))
    elif context == "mask":
        canvas._rendering_mask_contributor = 1
    elif context == "navigator":
        canvas._effect_preview_channel = "navigator"
    else:
        canvas._effect_viewport_world = None
    visible = QRectF(17, 22, 29, 31)
    placement = QTransform.fromTranslate(obj.x, obj.y) * canvas._drawing_object_transform(obj)
    expected = None if context == "navigator" else placement.inverted()[0].mapRect(visible)
    render_raster(canvas, obj, visible)
    assert requirements == [expected]
