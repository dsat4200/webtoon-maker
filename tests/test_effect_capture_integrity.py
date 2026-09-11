"""Cached effect captures preserve full content and export visibility."""
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    MirrorModifier, RasterObject, TilingModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def capture_scene(qapp):
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 400))
    layer = chapter.add_layer(page.layer_id, "Parent", BoundGeometry.rectangle(0, 0, 700, 400))
    child = chapter.add_layer(layer.layer_id, "Child", BoundGeometry.rectangle(0, 0, 700, 400))
    for item in (page, layer, child):
        item.fill_color, item.border_width = None, 0
    obj = chapter.add_object(child.layer_id, RasterObject(interaction_rect=(0, 0, 650, 360)))
    tiles = TileStore()
    for x in range(80, 650, 100):
        tiles.paint_dab(obj.object_id, QPointF(x, 180), 140, QColor("#cf407c"))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    yield canvas, page, layer, child, obj
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def clear(canvas):
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0


def render(canvas, interactive=False, visible=None):
    image = QImage(1080, 400, QImage.Format_ARGB32_Premultiplied)
    canvas._interactive_render = interactive
    try:
        if visible is None:
            canvas.render_preview(image)
        else:
            image.fill(QColor(canvas.chapter.background))
            painter = QPainter(image)
            painter.setRenderHint(QPainter.Antialiasing, True)
            for page_id in reversed(canvas.chapter.root_page_ids):
                canvas._render_layer(painter, canvas.chapter.layers[page_id], 1., visible)
            painter.end()
    finally:
        canvas._interactive_render = False
    return np.frombuffer(image.constBits(), np.uint8).copy()


def settle(canvas):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        canvas._effect_jobs.poll()
        result = render(canvas, True)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            return result
        QTest.qWait(5)
    pytest.fail("Effect capture did not converge")


def test_narrow_viewport_capture_does_not_remove_offscreen_content_from_export(capture_scene):
    canvas, _, layer, _, _ = capture_scene
    canvas.chapter.add_modifier(BlurModifier(strength=3), [("layer", layer.layer_id)])
    expected = render(canvas)
    clear(canvas)
    render(canvas, visible=QRectF(0, 0, 128, 400))
    np.testing.assert_array_equal(render(canvas), expected)


@pytest.mark.parametrize("parent_effect", ["blur", "mirror", "tiling"])
@pytest.mark.parametrize("mask_kind", ["object", "layer"])
def test_selected_mask_only_capture_cannot_leak_into_export_or_deselection(
        capture_scene, parent_effect, mask_kind):
    canvas, page, layer, child, obj = capture_scene
    target = obj if mask_kind == "object" else child
    target.mask_only = True
    target_id = obj.object_id if mask_kind == "object" else child.layer_id
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(hue=30), [("object", obj.object_id)])
    effect = (BlurModifier(strength=3) if parent_effect == "blur" else
              MirrorModifier() if parent_effect == "mirror" else
              TilingModifier(center=(350, 180), side=300))
    canvas.chapter.add_modifier(effect, [("layer", layer.layer_id)])
    canvas.set_selection(mask_kind, target_id)
    expected = render(canvas)
    clear(canvas)
    assert np.any(settle(canvas) != expected)
    np.testing.assert_array_equal(render(canvas), expected)
    canvas.set_selection("layer", page.layer_id)
    np.testing.assert_array_equal(settle(canvas), expected)
