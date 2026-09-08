"""Halftone captures full gradient fields and keeps the cached GPU path."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF, Qt
from PySide6.QtGui import QImage, QPainter

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, ColorGradientRamp,
    ColorGradientStop, HalftoneModifier, LineGradientField, PathNode,
    PosterizeModifier, PosterizeRange, RadialGradientField, ShapeGradientField,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.modifier_rendering import _qimage_premultiplied


FIELDS = ["line", "radial", "uniform_radial", "shape", "outward_shape", "outward_radial"]


@pytest.fixture
def scene(qapp):
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


def gradient(chapter, parent, kind="line"):
    return chapter.add_object(parent.layer_id, ColorFillGradientObject(
        field_type="line" if kind == "line" else "radial" if "radial" in kind else "parent_shape",
        line_field=LineGradientField(BoundGeometry.path([PathNode(x=40, y=80), PathNode(x=140, y=80)])),
        radial_field=RadialGradientField(origin_x=90, origin_y=80, radius_x=27, radius_y=18,
            ellipse_enabled=True, rotation=23, reverse_direction=kind == "outward_radial",
            uniform=kind == "uniform_radial", distance=20),
        shape_field=ShapeGradientField(reverse_direction=kind == "outward_shape", distance=20),
        ramp=ColorGradientRamp(stops=[ColorGradientStop(position=0, color="#80662299"),
                                      ColorGradientStop(position=1, color="#FF339966")]),
    ))


def render(canvas, bounds=None):
    bounds = bounds or QRectF(0, 0, 180, 160)
    image = QImage(int(bounds.width()), int(bounds.height()), QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    painter.setRenderHint(QPainter.Antialiasing, True)
    painter.translate(-bounds.left(), -bounds.top())
    try:
        for page_id in reversed(canvas.chapter.root_page_ids):
            canvas._render_layer(painter, canvas.chapter.layers[page_id], 1., bounds)
    finally:
        painter.end()
    return image


def attach(chapter, obj, **settings):
    modifier = HalftoneModifier(base_resolution=100, spacing=15, blur=0, **settings)
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    return modifier


@pytest.mark.parametrize("kind", FIELDS)
@pytest.mark.parametrize("grid", ["square", "hexagonal", "radial", "line", "ring", "stippling"])
def test_every_grid_covers_gradient_field_and_preserves_alpha(scene, kind, grid):
    canvas, chapter, parent = scene
    obj = gradient(chapter, parent, kind)
    original = render(canvas)
    modifier = attach(chapter, obj, grid_type=grid)
    result = render(canvas)
    before, after = _qimage_premultiplied(original), _qimage_premultiplied(result)
    assert np.count_nonzero(np.abs(before[..., :3] - after[..., :3]).max(axis=2) > .1) > 100
    np.testing.assert_allclose(after[..., 3], before[..., 3], atol=1/255)
    modifier.muted = True
    assert render(canvas) == original


@pytest.mark.parametrize("color_mode", ["two", "gradient", "source", "target_layer"])
def test_gradient_works_with_all_ink_colors_and_transparent_background(scene, color_mode):
    canvas, chapter, parent = scene
    obj = gradient(chapter, parent)
    original = _qimage_premultiplied(render(canvas))
    source = chapter.add_layer(chapter.root_page_ids[0], "Colors", BoundGeometry.rectangle(0, 0, 180, 160))
    source.fill_color, source.border_width, source.visible = "#ff0000", 0, False
    attach(chapter, obj, color_mode=color_mode, target_layer_id=source.layer_id,
           target_hue=120, transparent_background=True)
    result = _qimage_premultiplied(render(canvas))
    assert result[..., 3].max() > .5
    assert np.count_nonzero(original[..., 3] - result[..., 3] > .1) > 100
    assert np.all(result[..., 3] <= original[..., 3] + 1/255)
    if color_mode == "target_layer":
        np.testing.assert_allclose(result[..., 1], result[..., 3], atol=1/255)
        assert result[..., (0, 2)].max() == 0


def test_parent_transform_and_viewport_crop_keep_gradient_grid_frame(scene, monkeypatch):
    canvas, chapter, parent = scene
    obj = gradient(chapter, parent)
    modifier = attach(chapter, obj)
    calls = []

    class RecordingRenderer:
        def render(self, image, effect, **kwargs):
            calls.append((image.cacheKey(), image.width(), image.height()))
            return None  # Let the real CPU fallback draw the pattern.

    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: RecordingRenderer())
    render(canvas)
    modifier.spacing = 19
    render(canvas)
    assert calls[0] == calls[1] and calls[0][1:] == (100, 80)
    parent.transform_frame = (40, 40, 100, 80)
    parent.transform_quad = [(32, 45), (135, 22), (152, 107), (48, 126)]
    full = render(canvas)
    assert calls[-1][1:] == (100, 80)
    assert render(canvas, QRectF(50, 55, 70, 60)) == full.copy(50, 55, 70, 60)
    obj.ramp.stops[0].color = "#FFFF0000"
    assert render(canvas) != full
    assert calls[-1][0] != calls[-2][0]


def test_halftone_and_posterize_keep_stack_order_on_gradient(scene):
    canvas, chapter, parent = scene
    obj = gradient(chapter, parent)
    halftone = attach(chapter, obj)
    posterize = PosterizeModifier(ranges=[PosterizeRange(0, "#FF00FF00")])
    chapter.add_modifier(posterize, [("object", obj.object_id)])
    after = _qimage_premultiplied(render(canvas))
    np.testing.assert_allclose(after[..., 1], after[..., 3], atol=1/255)
    assert after[..., (0, 2)].max() == 0
    obj.modifier_ids = [posterize.modifier_id, halftone.modifier_id]
    before = _qimage_premultiplied(render(canvas))
    np.testing.assert_allclose(before[..., 0], before[..., 1], atol=1/255)
    np.testing.assert_allclose(before[..., 1], before[..., 2], atol=1/255)
    assert not np.array_equal(before, after)


@pytest.mark.parametrize("kind", FIELDS)
def test_gradient_uses_real_gpu_and_reuses_uploads(scene, kind):
    from comic_editor.ui.gpu_pattern_effects import GpuPatternRenderer
    canvas, chapter, parent = scene
    gpu = GpuPatternRenderer()
    if not gpu.available:
        pytest.skip(gpu.reason)
    try:
        canvas.settings.canvas_renderer = "auto"
        canvas._gpu_pattern_renderer = gpu
        obj = gradient(chapter, parent, kind)
        original = render(canvas)
        modifier = attach(chapter, obj)
        assert render(canvas) != original
        assert gpu.available and gpu.uploads == 1 and gpu.draws == 1
        modifier.spacing = 21
        render(canvas)
        assert gpu.available and gpu.uploads == 1 and gpu.draws == 2
    finally:
        gpu.close()
