"""Pattern integration: stable coordinates, GPU reuse, masks and raster baking."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (BoundGeometry, ChapterDocument,
    HalftoneModifier, PixelateModifier, ParameterMaskBinding, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.baking import apply_raster_modifiers
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import apply_pattern_modifier, _qimage_premultiplied


@pytest.fixture
def pattern_scene(qapp):
    chapter = ChapterDocument(width=240, height=180, document_kind="asset")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 240, 180))
    page.fill_color, page.border_width = None, 0
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", snap_to_grid=False))
    canvas.set_document(chapter, TileStore())
    obj = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 240, 180)))
    for x in range(35, 200, 12):
        canvas.tiles.paint_dab(obj.object_id, QPointF(x, 80), 32,
            QColor(20 + x, 70, 190, 210), square=True, antialias=False)
    canvas.set_selection("object", obj.object_id)
    yield canvas, chapter, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def sample_image():
    image = QImage(120, 90, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ffaaaaaa"))
    painter = QPainter(image)
    painter.fillRect(4, 13, 65, 29, QColor("#ff274876"))
    painter.fillRect(77, 52, 30, 33, QColor("#80d06413"))
    painter.end()
    return image


@pytest.mark.parametrize("modifier", [HalftoneModifier(blur=2, spacing=35),
    HalftoneModifier(grid_type="ring", blur=0), PixelateModifier(pixel_size=11, blur=3)])
def test_viewport_crop_does_not_move_grid_or_change_blur(pattern_scene, modifier):
    canvas, _, _ = pattern_scene
    image = sample_image()
    frame = QRectF(-20, 30, 120, 90)
    full, _ = render_stages(canvas, image, frame, [modifier], QTransform())
    clipped, frame2 = render_stages(canvas, image, frame, [modifier], QTransform(),
                                  required=QRectF(7, 46, 39, 43))
    expected = full.copy(27, 16, 39, 43)
    assert frame2 == QRectF(7, 46, 39, 43)
    assert clipped == expected


def test_slider_edit_reuses_original_gpu_source(pattern_scene, monkeypatch):
    canvas, _, _ = pattern_scene
    class RecordingRenderer:
        def __init__(self):
            self.keys = []
        def render(self, image, modifier, **kwargs):
            self.keys.append(image.cacheKey())
            return image.copy()
    renderer = RecordingRenderer()
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: renderer)
    image = sample_image()
    modifier = PixelateModifier()
    for size in (7, 9):
        modifier.pixel_size = size
        render_stages(canvas, image, QRectF(0, 0, 120, 90), [modifier], QTransform())
    assert renderer.keys == [image.cacheKey(), image.cacheKey()]


def test_intensity_mask_overrides_scalar_and_preserves_unaffected_pixels(qapp):
    image = sample_image()
    modifier = PixelateModifier(brightness=100, intensity=0)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    field = np.zeros((90, 120), np.float32)
    field[:, 60:] = 1
    output = apply_pattern_modifier(image, modifier, {(modifier.modifier_id, "intensity"): field})
    source, result = _qimage_premultiplied(image), _qimage_premultiplied(output)
    np.testing.assert_allclose(source[:, :60], result[:, :60], atol=1/255)
    assert np.abs(source[:, 60:] - result[:, 60:]).max() > .1


@pytest.mark.parametrize("modifier", [HalftoneModifier(blur=0, transparent_background=True),
    PixelateModifier(pixel_size=9, blur=1, saturation=-60)])
def test_transformed_raster_bake_and_undo_preserve_preview(pattern_scene, modifier):
    canvas, chapter, obj = pattern_scene
    obj.transform_frame = (0, 0, 240, 180)
    obj.transform_quad = [(10, 6), (210, 18), (222, 164), (18, 156)]
    chapter.add_modifier(modifier, [("object", obj.object_id)])
    def preview():
        image = QImage(240, 180, QImage.Format_ARGB32_Premultiplied)
        canvas.render_preview(image)
        return image
    before = preview()
    apply_raster_modifiers(canvas, modifier.modifier_id)
    after = preview()
    assert after == before
    assert canvas.chapter.objects[obj.object_id].modifier_source_frame is not None
    canvas.command_stack.undo()
    assert preview() == before
    canvas.command_stack.redo()
    assert preview() == after
