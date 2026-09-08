"""Posterization covers a gradient's artwork, not its control-handle bounds."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, LineGradientField, PathNode, PosterizeModifier,
    PosterizeValueModifier, PosterizeRange, RadialGradientField, ShapeGradientField,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


@pytest.fixture
def gradient_canvas(qapp):
    chapter = ChapterDocument(width=180, height=160, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 180, 160))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Shape", BoundGeometry.rectangle(40, 40, 100, 80))
    parent.fill_color, parent.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    yield canvas, chapter, parent
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def add_gradient(chapter, parent, kind):
    gradient = ColorFillGradientObject(
        field_type="line" if kind == "line" else "radial" if "radial" in kind else "parent_shape",
        line_field=LineGradientField(BoundGeometry.path([PathNode(x=40, y=80), PathNode(x=140, y=80)])),
        radial_field=RadialGradientField(origin_x=90, origin_y=80, radius_x=27, radius_y=18,
            ellipse_enabled=True, rotation=23, reverse_direction=kind == "outward_radial",
            uniform=kind == "uniform_radial", distance=20),
        shape_field=ShapeGradientField(reverse_direction=kind == "outward_shape", distance=20),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#80FF0000"),
                                      ColorGradientStop(position=1, color="#FF0000FF")]),
    )
    chapter.add_object(parent.layer_id, gradient)
    return gradient


def render(canvas, bounds=None):
    bounds = bounds or QRectF(0, 0, 180, 160)
    image = QImage(int(bounds.width()), int(bounds.height()), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.left(), -bounds.top())
    for page_id in reversed(canvas.chapter.root_page_ids):
        canvas._render_layer(painter, canvas.chapter.layers[page_id], 1., bounds)
    painter.end()
    return image


@pytest.mark.parametrize("kind", ["line", "radial", "uniform_radial", "shape", "outward_shape", "outward_radial"])
@pytest.mark.parametrize("factory", [PosterizeModifier, PosterizeValueModifier])
def test_posterize_fills_entire_gradient_and_preserves_its_transparency(gradient_canvas, kind, factory):
    canvas, chapter, parent = gradient_canvas
    gradient = add_gradient(chapter, parent, kind)
    original = render(canvas)
    modifier = factory(ranges=[PosterizeRange(0, "#FF00FF00")])
    chapter.add_modifier(modifier, [("object", gradient.object_id)])
    result = render(canvas)
    expected_alpha = _qimage_premultiplied(original)[..., 3]
    pixels = _qimage_premultiplied(result)
    assert expected_alpha.max() > .5
    np.testing.assert_allclose(pixels[..., 3], expected_alpha, atol=1/255)
    np.testing.assert_allclose(pixels[..., 1], pixels[..., 3], atol=1/255)
    assert pixels[..., (0, 2)].max() == 0
    modifier.muted = True
    assert render(canvas) == original


def test_horizontal_line_gradient_survives_parent_transform_and_viewport_crop(gradient_canvas):
    canvas, chapter, parent = gradient_canvas
    parent.transform_frame = (40, 40, 100, 80)
    parent.transform_quad = [(32, 45), (135, 22), (152, 107), (48, 126)]
    gradient = add_gradient(chapter, parent, "line")
    chapter.add_modifier(PosterizeModifier(ranges=[PosterizeRange(0, "#FF00FF00")]),
                         [("object", gradient.object_id)])
    full = render(canvas)
    assert full.pixelColor(90, 60).green() > 250
    assert full.pixelColor(90, 100).green() > 250
    assert render(canvas, QRectF(50, 55, 70, 60)) == full.copy(50, 55, 70, 60)


def test_gradient_parent_shape_edit_invalidates_posterize_source_with_same_bounds(gradient_canvas):
    canvas, chapter, parent = gradient_canvas
    gradient = add_gradient(chapter, parent, "shape")
    chapter.add_modifier(PosterizeValueModifier(ranges=[PosterizeRange(0, "#FFFF0000"),
                                                       PosterizeRange(60, "#FF00FF00")]),
                         [("object", gradient.object_id)])
    old_signature = canvas._modifier_object_signature(gradient)
    before = render(canvas)
    parent.bound = BoundGeometry.path([PathNode(x=40, y=120), PathNode(x=90, y=40),
                                      PathNode(x=140, y=120)], closed=True)
    assert canvas._modifier_object_signature(gradient) != old_signature
    updated = render(canvas)
    assert updated != before
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    assert render(canvas) == updated
