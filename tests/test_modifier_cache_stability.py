"""Unchanged reference artwork must stay exact while tracing or changing tools."""
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest

from comic_editor.core.images import ImageStore
from comic_editor.core.models import (ArrayModifier, BlurModifier, BoundGeometry,
    ChapterDocument, CurvesModifier, HueSaturationLightnessModifier, ImageObject,
    OutlineModifier, RasterObject)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui import effect_pipeline, interactive_effects


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=384, height=256, document_kind="asset", background="#00000000")
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 384, 256))
    page.fill_color, page.border_width = None, 0
    panel = chapter.add_layer(page.layer_id, "Reference panel", BoundGeometry.rectangle(0, 0, 240, 240))
    panel.fill_color, panel.border_width = None, 0
    reference = chapter.add_object(panel.layer_id, ImageObject(pixel_width=240, pixel_height=240))
    image = QImage(240, 240, QImage.Format_ARGB32_Premultiplied)
    for y in range(240):
        for x in range(240):
            image.setPixelColor(x, y, QColor(48 if (x+y)%2 else 191, 75+x//2, 80+y//2))
    trace = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 384, 256)))
    other_trace = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(0, 0, 384, 256)))
    images = ImageStore()
    images.put_decoded(reference.object_id, "reference.png", b"", image)
    modifier = HueSaturationLightnessModifier(hue=19, saturation=-12)
    chapter.add_modifier(modifier, [("object", reference.object_id)])
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster", grid_overlay_visible=False,
        predictive_ink=False, snap_to_grid=False))
    canvas.resize(384, 256)
    canvas.set_document(chapter, TileStore(), images)
    canvas.scale, canvas.center_x, canvas.center_y = 1., 192., 128.
    canvas.set_selection("object", trace.object_id)
    yield canvas, reference, trace, other_trace, panel
    canvas._effect_jobs.cancel()
    canvas.deleteLater()


def render(canvas, interactive=True):
    previous = canvas._interactive_render
    canvas._interactive_render = interactive
    image = QImage(384, 256, QImage.Format_ARGB32_Premultiplied)
    try:
        canvas.render_preview(image)
    finally:
        canvas._interactive_render = previous
    return image


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


def settle(canvas, qapp):
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        qapp.processEvents()
        canvas._effect_jobs.poll()
        image = render(canvas)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            return image
        QTest.qWait(2)
    pytest.fail("Reference effect did not reach exact pixels")


@pytest.mark.parametrize("operation", ["tools", "selection", "stroke", "undo_stroke"])
def test_unselected_reference_remains_exact_during_unrelated_ui_and_stroke_changes(scene, qapp, monkeypatch, operation):
    canvas, reference, trace, other_trace, panel = scene
    expected = settle(canvas, qapp).copy(0, 0, 240, 240)
    calls = []
    original = interactive_effects.apply_modifier_stack
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", lambda *args, **kwargs:
        (calls.append((args[0].width(), args[0].height())), original(*args, **kwargs))[1])
    submitted = canvas._effect_jobs.submitted
    if operation == "tools":
        for tool in (ToolKind.OBJECT_SELECT, ToolKind.EYEDROPPER, ToolKind.RASTER_PENCIL):
            canvas.set_tool(tool)
            np.testing.assert_array_equal(pixels(render(canvas).copy(0, 0, 240, 240)), pixels(expected))
    elif operation == "selection":
        for kind, identifier in (("layer", panel.layer_id), ("object", other_trace.object_id), ("object", trace.object_id)):
            canvas.set_selection(kind, identifier)
            np.testing.assert_array_equal(pixels(render(canvas).copy(0, 0, 240, 240)), pixels(expected))
    else:
        canvas._begin_stroke(QPointF(300, 100), 1.)
        canvas._continue_stroke(QPointF(335, 125), 1.)
        canvas._end_stroke()
        if operation == "undo_stroke":
            canvas.command_stack.undo()
        np.testing.assert_array_equal(pixels(render(canvas).copy(0, 0, 240, 240)), pixels(expected))
    assert canvas.selected_object_id != reference.object_id
    assert not calls, "Unchanged reference effect was recomputed"
    assert canvas._effect_jobs.submitted == submitted


def reference_modifiers(kind):
    if kind == "hsl":
        return [HueSaturationLightnessModifier(hue=19, saturation=-12)]
    if kind == "blur":
        return [BlurModifier(strength=2)]
    if kind == "outline":
        return [OutlineModifier(thickness=3)]
    curves = CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]})
    if kind == "array_curves":
        return [ArrayModifier(count=2, axis_end=(15, 0)), curves]
    return [curves]


@pytest.mark.parametrize("kind,initial", [
    ("hsl", "interactive"), ("curves", "interactive"),
    ("blur", "interactive"), ("outline", "interactive"),
    ("array_curves", "interactive"), ("hsl", "exact_then_viewed"),
])
def test_exact_unselected_reference_survives_ordinary_lru_pressure(scene, qapp, monkeypatch, kind, initial):
    canvas, reference, trace, _other_trace, _panel = scene
    reference.modifier_ids.clear()
    for modifier in reference_modifiers(kind):
        canvas.chapter.add_modifier(modifier, [("object", reference.object_id)])
    if initial == "exact_then_viewed":
        expected = render(canvas, False)
        # Exact captures become viewport-retained only once displayed normally.
        np.testing.assert_array_equal(pixels(render(canvas)), pixels(expected))
    else:
        expected = settle(canvas, qapp)
    if kind == "outline":
        assert canvas._effect_jobs.submitted == 0, "This case must exercise synchronous interactive work"
    elif initial == "interactive":
        assert canvas._effect_jobs.submitted > 0, "This case must exercise completed worker output"
    calls = []
    def instrument(module):
        original = module.apply_modifier_stack
        def tracked(*args, **kwargs):
            calls.append((args[0].width(), args[0].height()))
            return original(*args, **kwargs)
        monkeypatch.setattr(module, "apply_modifier_stack", tracked)
    instrument(interactive_effects)
    instrument(effect_pipeline)
    captures = []
    original_source_put = canvas._modifier_source_cache_put
    def source_put(key, image):
        if reference.object_id in str(key):
            captures.append((image.width(), image.height()))
        return original_source_put(key, image)
    monkeypatch.setattr(canvas, "_modifier_source_cache_put", source_put)
    # Ordinary source/draft churn is intentionally smaller than the protected
    # exact-result budget; the retained output easily fits in that budget.
    canvas._modifier_render_cache_budget = 512 * 1024
    canvas._modifier_source_cache_budget = 512 * 1024
    submitted = canvas._effect_jobs.submitted
    pressure = QImage(400, 400, QImage.Format_ARGB32_Premultiplied)
    pressure.fill(QColor("#ff238a"))
    for index in range(3):
        canvas._modifier_cache_put(("unrelated-effect", index), pressure)
        canvas._modifier_source_cache_put(("unrelated-source", index), pressure)
        assert not any(reference.object_id in str(key) for key in canvas._modifier_render_cache)
        np.testing.assert_array_equal(pixels(render(canvas)), pixels(expected))
    assert canvas.selected_object_id == trace.object_id
    assert not calls, "An unchanged exact image fell back to a recomputed draft"
    assert not captures, "An unchanged exact image was needlessly recaptured"
    assert canvas._effect_jobs.submitted == submitted


def test_modified_reference_layer_ignores_selection_only_last_raster_metadata(scene, qapp, monkeypatch):
    canvas, reference, trace, _other_trace, panel = scene
    first = canvas.chapter.add_object(panel.layer_id, RasterObject())
    second = canvas.chapter.add_object(panel.layer_id, RasterObject())
    canvas.chapter.add_modifier(CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]}), [("layer", panel.layer_id)])
    expected = settle(canvas, qapp)
    calls = []
    original = interactive_effects.apply_modifier_stack
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", lambda *args, **kwargs:
        (calls.append((args[0].width(), args[0].height())), original(*args, **kwargs))[1])
    submitted = canvas._effect_jobs.submitted
    for obj in (first, second, trace):
        canvas.set_selection("object", obj.object_id)
        np.testing.assert_array_equal(pixels(render(canvas)), pixels(expected))
    assert canvas.selected_object_id != reference.object_id
    assert not calls
    assert canvas._effect_jobs.submitted == submitted
