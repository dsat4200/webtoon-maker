"""Long outlined drawings retain exact visible pixels without full-page captures."""
import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, OutlineModifier, ParameterMaskBinding,
    RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import interactive_effects


@pytest.fixture
def scene(qapp, monkeypatch):
    chapter = ChapterDocument(height=3600)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 3600))
    layer = chapter.add_layer(page.layer_id, "Long drawing", BoundGeometry.rectangle(0, 0, 800, 3600))
    for entity in (page, layer):
        entity.fill_color, entity.border_width = None, 0
    obj = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 800, 3600)))
    tiles = TileStore()
    for y in range(0, 3600, 55):
        tiles.paint_dab(obj.object_id, QPointF(400 + y % 100, y), 80, QColor(60, 140, 225, 180))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(320, 240)
    canvas.set_document(chapter, tiles)
    canvas.center_x, canvas.scale = 400, 1
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *_a, **_k: False)
    yield canvas, chapter, layer, obj
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas):
    canvas._invalidate_scene_cache()
    canvas._ensure_scene_cache()
    return np.frombuffer(canvas._scene_cache.constBits(), np.uint8).copy()


@pytest.mark.parametrize("target", ["object", "layer"])
@pytest.mark.parametrize("scale,rotation", [(1, 0), (.45, 0), (1.7, 21)])
def test_outlines_match_full_capture_across_pan_zoom_and_mask(scene, target, scale, rotation, monkeypatch):
    canvas, chapter, layer, obj = scene
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    for y in range(0, 3600, 200):
        canvas.tiles.paint_dab(mask.mask_id, QPointF(410, y), 240, QColor("white"))
    effect = OutlineModifier(thickness=12, color="#FF402038")
    effect.parameter_masks["thickness"] = ParameterMaskBinding(mask.mask_id, 2, 18)
    ref = (target, obj.object_id if target == "object" else layer.layer_id)
    chapter.add_modifier(effect, [ref])
    chapter.add_modifier(OutlineModifier(thickness=5, color="#FFDDBB11"), [ref])
    canvas.scale, canvas.rotation = scale, rotation
    optimized = interactive_effects.outline_capture_bounds
    for y in (100, 500, 777, 1900, 3460, 500):
        canvas.center_y = y
        monkeypatch.setattr(interactive_effects, "outline_capture_bounds", optimized)
        actual = render(canvas)
        monkeypatch.setattr(interactive_effects, "outline_capture_bounds", lambda _c, _p, bounds, *_a: bounds)
        expected = render(canvas)
        np.testing.assert_array_equal(actual, expected)


def test_small_pan_reuses_source_window_and_export_remains_complete(scene, monkeypatch):
    canvas, chapter, _layer, obj = scene
    chapter.add_modifier(OutlineModifier(thickness=10), [("object", obj.object_id)])
    canvas.center_y = 1700
    render(canvas)
    original_keys = set(canvas._modifier_source_cache)
    sources = [image for key, image in canvas._modifier_source_cache.items()
               if key[:2] == ("object-source", obj.object_id)]
    assert sources and max(image.height() for image in sources) <= 768
    canvas.center_y += 4
    render(canvas)
    assert original_keys == set(canvas._modifier_source_cache)
    actual = QImage(1080, 3600, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(actual)
    monkeypatch.setattr(interactive_effects, "outline_capture_bounds", lambda _c, _p, bounds, *_a: bounds)
    expected = QImage(actual.size(), actual.format())
    canvas.render_preview(expected)
    assert bytes(actual.constBits()) == bytes(expected.constBits())
    assert actual.pixelColor(455, 55) != QColor(chapter.background)
    assert actual.pixelColor(465, 3465) != QColor(chapter.background)


@pytest.mark.parametrize("target", ["object", "layer"])
def test_blurred_outline_viewport_window_preserves_masked_halo(scene, monkeypatch, target):
    canvas, chapter, layer, obj = scene
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    for y in range(0, 3600, 200):
        canvas.tiles.paint_dab(mask.mask_id, QPointF(410, y), 200, QColor("white"))
    effect = OutlineModifier(thickness=8, color="#CCFE1020", blur_radius=4, blur_strength=60)
    effect.parameter_masks["blur_radius"] = ParameterMaskBinding(mask.mask_id, 1, 8)
    effect.parameter_masks["blur_strength"] = ParameterMaskBinding(mask.mask_id, 25, 100)
    ref = (target, obj.object_id if target == "object" else layer.layer_id)
    chapter.add_modifier(effect, [ref])
    chapter.add_modifier(OutlineModifier(thickness=3, color="#FF00CCFF", blur_radius=2,
                                         blur_strength=50), [ref])
    optimized = interactive_effects.outline_capture_bounds
    for y in (777, 1900):
        canvas.center_y = y
        monkeypatch.setattr(interactive_effects, "outline_capture_bounds", optimized)
        actual = render(canvas)
        monkeypatch.setattr(interactive_effects, "outline_capture_bounds", lambda _c, _p, bounds, *_a: bounds)
        np.testing.assert_array_equal(actual, render(canvas))
