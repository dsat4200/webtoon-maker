"""Small canvas repaint regions must not resize an unchanged reference effect."""
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRect, QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    ArrayModifier, BoundGeometry, ChapterDocument, CurvesModifier, DistortModifier,
    ImageObject, OutlineModifier, PixelateModifier, RasterObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui import (
    distort_rendering, effect_pipeline, interactive_effects, modifier_rendering,
)


@pytest.fixture(params=["array_curves", "pixelate", "distort", "outline_blur"])
def scene(request, qapp, monkeypatch):
    chapter = ChapterDocument(width=384, height=256, document_kind="asset",
                              background="#FFFFFFFF")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 384, 256))
    page.fill_color, page.border_width = None, 0
    reference = chapter.add_object(page.layer_id, ImageObject(pixel_width=240, pixel_height=240))
    image = QImage(240, 240, QImage.Format_ARGB32_Premultiplied)
    for y in range(240):
        for x in range(240):
            image.setPixelColor(x, y, QColor(48 if (x+y)%2 else 191, 75+x//2, 80+y//2))
    images = ImageStore()
    images.put_decoded(reference.object_id, "reference.png", b"", image)
    trace = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 384, 256)), index=0)
    kind = request.param
    if kind == "array_curves":
        modifiers = [ArrayModifier(count=2, axis_end=(25, 0)),
                     CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .75), (1, 1)]})]
    elif kind == "pixelate":
        modifiers = [PixelateModifier(pixel_size=9)]
    elif kind == "distort":
        modifiers = [DistortModifier(modifier_type="distort_twirl", parameters={"angle": 65},
                                    frame=(0, 0, 240, 240), center=(120, 120), radius=110)]
    else:
        modifiers = [OutlineModifier(thickness=5, blur_radius=4, blur_strength=70)]
    for modifier in modifiers:
        chapter.add_modifier(modifier, [("object", reference.object_id)])
    settings = EditorSettings(canvas_renderer="raster", snap_to_grid=False,
                              predictive_ink=False, grid_overlay_visible=False)
    settings.pencil_size_px[settings.active_pencil_size] = 8
    canvas = CanvasWidget(settings)
    canvas.setMinimumSize(1, 1)
    canvas.resize(384, 256)
    canvas.set_document(chapter, TileStore(), images)
    canvas.scale, canvas.center_x, canvas.center_y = 1., 192., 128.
    canvas.set_selection("object", trace.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas.primary_color = "#FFFF238A"
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _canvas: None)
    yield canvas, reference, trace
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def pixels(image):
    raw = np.frombuffer(image.constBits(), np.uint8).reshape(image.height(), image.bytesPerLine())
    return raw[:, :image.width()*4].reshape(image.height(), image.width(), 4).copy()


def exact_scene(canvas, qapp):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        qapp.processEvents()
        canvas._effect_jobs.poll()
        canvas._ensure_scene_cache()
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            assert not canvas._scene_dirty_full
            return pixels(canvas._scene_cache)
        QTest.qWait(2)
    pytest.fail("Reference effect did not reach exact canvas pixels")


def watch_effect_work(canvas, monkeypatch):
    calls, requests, redraws = [], [], []
    for module, name in (
        (interactive_effects, "apply_modifier_stack"),
        (effect_pipeline, "apply_modifier_stack"),
        (modifier_rendering, "apply_pattern_modifier"),
        (distort_rendering, "render_distort"),
    ):
        original = getattr(module, name)
        def tracked(*args, _original=original, _name=name, **kwargs):
            calls.append(_name)
            return _original(*args, **kwargs)
        monkeypatch.setattr(module, name, tracked)
    original_request = canvas._effect_jobs.request
    def request(*args, **kwargs):
        requests.append(args[0])
        return original_request(*args, **kwargs)
    monkeypatch.setattr(canvas._effect_jobs, "request", request)
    original_redraw = canvas._render_scene_cache_rect
    def redraw(dirty, **kwargs):
        redraws.append(QRect(dirty))
        return original_redraw(dirty, **kwargs)
    monkeypatch.setattr(canvas, "_render_scene_cache_rect", redraw)
    return calls, requests, redraws


def test_overlapping_dirty_rectangles_reuse_full_viewport_reference(scene, qapp, monkeypatch):
    canvas, reference, trace = scene
    expected = exact_scene(canvas, qapp)
    calls, requests, redraws = watch_effect_work(canvas, monkeypatch)
    submitted = canvas._effect_jobs.submitted
    for dirty in (QRectF(70, 70, 28, 22), QRectF(135, 112, 37, 31), QRectF(30, 35, 55, 44)):
        canvas._mark_scene_dirty_world(dirty)
        assert not canvas._scene_dirty_full
        assert canvas._scene_dirty_widget.width() < canvas.width()
        canvas._ensure_scene_cache()
        # Compare both the repainted area and the untouched surrounding frame.
        np.testing.assert_array_equal(pixels(canvas._scene_cache), expected)
    assert canvas.selected_object_id == trace.object_id != reference.object_id
    assert len(redraws) == 3
    assert not calls, "A dirty repaint recomputed unchanged reference effects"
    assert not requests, "A dirty repaint requested a new reference worker"
    assert canvas._effect_jobs.submitted == submitted


def test_real_stroke_over_reference_keeps_exact_reference_between_segments(scene, qapp, monkeypatch):
    canvas, reference, trace = scene
    expected = exact_scene(canvas, qapp)
    calls, requests, redraws = watch_effect_work(canvas, monkeypatch)
    submitted = canvas._effect_jobs.submitted
    assert not list(canvas.tiles.iter_tiles(trace.object_id))
    unaffected = np.ones(expected.shape[:2], dtype=bool)
    # Brush coverage stays in this deliberately generous document/widget band.
    # Pixels immediately above and below it are still inside redraw rectangles.
    unaffected[80:125, 75:170] = False
    canvas._begin_stroke(QPointF(90, 100), 1.)
    for point in (QPointF(110, 102), QPointF(130, 104), QPointF(150, 106)):
        canvas._continue_stroke(point, 1.)
        canvas._ensure_scene_cache()
        np.testing.assert_array_equal(pixels(canvas._scene_cache)[unaffected], expected[unaffected])
    canvas._end_stroke()
    canvas._ensure_scene_cache()
    after = pixels(canvas._scene_cache)
    np.testing.assert_array_equal(after[unaffected], expected[unaffected])
    assert np.any(after[~unaffected] != expected[~unaffected]), "The real tracing stroke must be visible"
    assert list(canvas.tiles.iter_tiles(trace.object_id))
    assert any(rect.width() < canvas.width() and rect.height() < canvas.height() for rect in redraws)
    canvas.command_stack.undo()
    canvas._ensure_scene_cache()
    np.testing.assert_array_equal(pixels(canvas._scene_cache), expected)
    assert canvas.selected_object_id == trace.object_id != reference.object_id
    assert not calls, "Tracing over the reference recomputed its unchanged effects"
    assert not requests, "Tracing over the reference requested a new worker"
    assert canvas._effect_jobs.submitted == submitted


def test_partial_overflow_redraw_reuses_completed_reference(scene, qapp, monkeypatch):
    canvas, reference, _trace = scene
    reference.x = -60
    canvas.center_x = 130
    canvas.chapter.view_overflow = 1.
    canvas._invalidate_scene_cache()
    expected = exact_scene(canvas, qapp)
    outside = canvas.document_to_widget(QPointF(-30, 100)).toPoint()
    assert not np.array_equal(expected[outside.y(), outside.x()], [40, 36, 36, 255])
    calls, requests, redraws = watch_effect_work(canvas, monkeypatch)
    for dirty in (QRectF(-45, 80, 30, 30), QRectF(-5, 110, 40, 45)):
        canvas._mark_scene_dirty_world(dirty)
        canvas._ensure_scene_cache()
        np.testing.assert_array_equal(pixels(canvas._scene_cache), expected)
    assert len(redraws) == 2
    assert not calls, "Overflow dirty repaint recomputed the unchanged effect"
    assert not requests, "Overflow dirty repaint requested a new worker"
    assert getattr(canvas, "_effect_viewport_world", None) is None
    assert canvas._effect_preview_channel == "canvas"
