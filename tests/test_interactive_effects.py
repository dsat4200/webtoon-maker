"""Full-quality convergence and responsiveness of modified canvas content."""
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QTimer
from PySide6.QtGui import QColor, QImage, QPainter

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    MirrorModifier, OutlineModifier, ParameterMaskBinding, RasterObject, ToneMask, TilingModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import interactive_effects


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(height=400)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 400))
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Ink", BoundGeometry.rectangle(0, 0, 700, 400))
    layer.fill_color, layer.border_width = None, 0
    obj = chapter.add_object(layer.layer_id, RasterObject(interaction_rect=(0, 0, 650, 360)))
    tiles = TileStore()
    for x in range(80, 650, 100):
        tiles.paint_dab(obj.object_id, QPointF(x, 180), 140, QColor("#cf407c"))
    hsl = HueSaturationLightnessModifier(hue=20)
    for effect in (hsl, BlurModifier(strength=4), OutlineModifier(thickness=3)):
        chapter.add_modifier(effect, [("object", obj.object_id)])
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    yield canvas, layer, obj, hsl
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


def render(canvas, interactive):
    image = QImage(1080, 400, QImage.Format_ARGB32_Premultiplied)
    previous = canvas._interactive_render
    canvas._interactive_render = interactive
    try:
        canvas.render_preview(image)
    finally:
        canvas._interactive_render = previous
    return image


def clear(canvas):
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0


def settle(canvas, qapp):
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        qapp.processEvents()
        canvas._effect_jobs.poll()
        result = render(canvas, True)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            return result
        time.sleep(.002)
    pytest.fail("Modified canvas did not converge to its exact render")


@pytest.mark.parametrize("parent_effect", [None, "blur", "mirror", "tiling"])
@pytest.mark.parametrize("masked", [False, True])
def test_nested_previews_converge_without_caching_drafts(scene, qapp, parent_effect, masked):
    canvas, layer, obj, hsl = scene
    if parent_effect:
        effect = (BlurModifier(strength=3) if parent_effect == "blur" else
                  TilingModifier(center=(350, 180), side=300) if parent_effect == "tiling" else
                  MirrorModifier())
        canvas.chapter.add_modifier(effect, [("layer", layer.layer_id)])
    if masked:
        mask = ToneMask(name="Opacity")
        canvas.chapter.masks[mask.mask_id] = mask
        canvas.tiles.paint_dab(mask.mask_id, QPointF(350, 180), 400, QColor("#b3b3b3"))
        obj.opacity_mask = ParameterMaskBinding(mask_id=mask.mask_id, black_value=.2, white_value=.8)
        hsl.parameter_masks["hue"] = ParameterMaskBinding(mask_id=mask.mask_id, black_value=-30, white_value=60)
    expected = pixels(render(canvas, False))
    clear(canvas)
    draft = render(canvas, True)
    assert canvas._effect_jobs.submitted > 0
    assert not np.array_equal(pixels(draft), expected)
    result = settle(canvas, qapp)
    np.testing.assert_array_equal(pixels(result), expected)
    # A later export cannot accidentally retrieve an upscaled draft.
    np.testing.assert_array_equal(pixels(render(canvas, False)), expected)


def test_worker_is_detached_main_loop_runs_and_latest_edit_wins(scene, qapp, monkeypatch):
    canvas, _, _, modifier = scene
    started, release = threading.Event(), threading.Event()
    original = interactive_effects.apply_modifier_stack
    calls = []

    def gated(image, effects, *args, **kwargs):
        if image.width() > 256:
            calls.append((threading.current_thread(), effects[0].hue))
            started.set()
            assert release.wait(5)
        return original(image, effects, *args, **kwargs)

    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", gated)
    try:
        render(canvas, True)
        assert started.wait(1)
        modifier.hue = 90
        tick = []
        QTimer.singleShot(0, lambda: tick.append(True))
        qapp.processEvents()
        assert tick
        render(canvas, True)
        assert calls[0][1] == 20
        assert calls[0][0] != threading.current_thread()
    finally:
        release.set()
    result = settle(canvas, qapp)
    assert canvas._effect_jobs.discarded >= 1
    clear(canvas)
    np.testing.assert_array_equal(pixels(result), pixels(render(canvas, False)))


def test_document_switch_discards_inflight_results(scene, qapp, monkeypatch):
    canvas, _, _, _ = scene
    started, release = threading.Event(), threading.Event()
    original = interactive_effects.apply_modifier_stack

    def gated(image, effects, *args, **kwargs):
        if image.width() > 256:
            started.set()
            assert release.wait(5)
        return original(image, effects, *args, **kwargs)

    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", gated)
    try:
        render(canvas, True)
        assert started.wait(1)
        canvas.set_document(ChapterDocument(height=400), TileStore())
    finally:
        release.set()
    settle(canvas, qapp)
    assert not canvas._modifier_render_cache
    assert canvas._effect_jobs.completed == 0


def test_navigator_uses_compact_preview_without_scheduling_full_page_work(scene):
    canvas, _, _, _ = scene
    canvas._effect_preview_channel = "navigator"
    result = render(canvas, True)
    assert not result.isNull()
    assert canvas._effect_jobs.submitted == 0
    drafts = [image for key, image in canvas._modifier_render_cache.items()
              if key[0] == "interactive-draft"]
    assert drafts
    assert all(image.width() <= 256 and image.height() <= 256 for image in drafts)


def test_offscreen_modified_content_does_not_schedule_effect_work(scene):
    canvas, layer, obj, _ = scene
    canvas.chapter.add_modifier(BlurModifier(strength=3), [("layer", layer.layer_id)])
    canvas._interactive_render = True
    image = QImage(200, 100, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    painter = QPainter(image)
    try:
        # Both the layer and object renderer must avoid allocating full source
        # images when a camera repaint cannot see any of their expanded bounds.
        offscreen = QRectF(2000, 1000, 200, 100)
        canvas._render_modified_layer(painter, layer, 1., offscreen)
        canvas._render_modified_object(painter, obj, 1., offscreen)
    finally:
        painter.end()
        canvas._interactive_render = False
    assert canvas._effect_jobs.submitted == 0
    assert not canvas._modifier_source_cache
