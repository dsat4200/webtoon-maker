"""Stroke gestures keep the event loop free and converge to exact pixels."""
import threading
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QTimer
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, OutlineModifier,
    ParameterMaskBinding, ScreamModifier, ShapeStyle, TextObject, ToneMask,
    WobbleModifier,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import interactive_strokes


@pytest.fixture
def scene(qapp, text_outline_font_family):
    chapter = ChapterDocument(height=420)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 420))
    page.fill_color, page.border_width = None, 0
    group = chapter.add_layer(page.layer_id, "Panel", BoundGeometry.rectangle(0, 0, 700, 420))
    group.fill_color, group.border_width = None, 0
    bound = BoundGeometry.rectangle(110, 100, 420, 190)
    bound.primitive = "ellipse"
    layer = chapter.add_layer(group.layer_id, "Bubble", bound, style=ShapeStyle(
        primary_color="#FFFFFFFF", outline_color="#FF151515", outline_thickness=5))
    chapter.add_object(layer.layer_id, TextObject(
        text="What? I haven't even started yet!", font_family=text_outline_font_family))
    scream = ScreamModifier(height=47, width=69, roundness=0)
    chapter.add_modifier(scream, [("layer", layer.layer_id)])
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(chapter, TileStore())
    yield canvas, layer, scream
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


def render(canvas, interactive):
    image = QImage(1080, 420, QImage.Format_ARGB32_Premultiplied)
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
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        qapp.processEvents()
        canvas._effect_jobs.poll()
        result = render(canvas, True)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            return result
        time.sleep(.002)
    pytest.fail("Stroke preview did not converge")


@pytest.mark.parametrize("stack", ["scream", "mixed", "parent", "masked"])
def test_stroke_previews_converge_to_exact_material_and_fill(scene, qapp, stack):
    canvas, layer, scream = scene
    if stack == "mixed":
        for modifier in (WobbleModifier(position=7, strength=35),
                         OutlineModifier(thickness=3), BlurModifier(strength=2)):
            canvas.chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    elif stack == "parent":
        canvas.chapter.add_modifier(BlurModifier(strength=3), [("layer", layer.parent_id)])
    elif stack == "masked":
        mask = ToneMask(name="Spike height")
        canvas.chapter.masks[mask.mask_id] = mask
        canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 200), 300, QColor("white"))
        scream.parameter_masks["height"] = ParameterMaskBinding(mask.mask_id, 8, 47)
    expected = pixels(render(canvas, False))
    clear(canvas)
    draft = render(canvas, True)
    assert canvas._effect_jobs.submitted > 0
    assert not np.array_equal(pixels(draft), expected)
    np.testing.assert_array_equal(pixels(settle(canvas, qapp)), expected)
    # Export never consumes the reduced editing draft.
    clear(canvas)
    np.testing.assert_array_equal(pixels(render(canvas, False)), expected)


def test_latest_stroke_edit_wins_and_worker_does_not_read_live_geometry(scene, qapp, monkeypatch):
    canvas, layer, scream = scene
    started, release = threading.Event(), threading.Event()
    original = interactive_strokes.warp_material
    observations = []

    def gated(image, background, bounds, before, after):
        if image.width() > 192:
            positions = after[0].points.copy()
            observations.append(threading.current_thread())
            started.set()
            assert release.wait(5)
            np.testing.assert_array_equal(after[0].points, positions)
        return original(image, background, bounds, before, after)

    monkeypatch.setattr(interactive_strokes, "warp_material", gated)
    try:
        render(canvas, True)
        assert started.wait(1)
        layer.translate_x = 28
        scream.height = 18
        render(canvas, True)
        tick = []
        QTimer.singleShot(0, lambda: tick.append(True))
        qapp.processEvents()
        assert tick
        assert observations[0] is not threading.current_thread()
    finally:
        release.set()
    result = settle(canvas, qapp)
    assert canvas._effect_jobs.discarded >= 1
    clear(canvas)
    np.testing.assert_array_equal(pixels(result), pixels(render(canvas, False)))


def test_navigator_uses_bounded_stroke_draft_without_full_render_jobs(scene):
    canvas, _, _ = scene
    canvas._effect_preview_channel = "navigator"
    assert not render(canvas, True).isNull()
    assert canvas._effect_jobs.submitted == 0
    drafts = [image for key, image in canvas._modifier_render_cache.items()
              if key[0] == "stroke-warp-draft"]
    assert drafts
    # Material and fill are packed vertically into one cached result.
    assert all(image.width() <= 192 and image.height() <= 384 for image in drafts)


@pytest.mark.parametrize("budget", [None, 1024], ids=["normal-budget", "one-kib-budget"])
def test_sequential_stroke_stages_converge_after_cache_eviction(scene, qapp, budget):
    canvas, layer, scream = scene
    wobble = WobbleModifier(position=7, strength=35)
    canvas.chapter.add_modifier(wobble, [("layer", layer.layer_id)])
    if budget is not None:
        canvas._modifier_render_cache_budget = budget
        canvas._effect_jobs.retained_budget = budget

    for height in (47, 19):
        scream.height = height
        expected = pixels(render(canvas, False))
        clear(canvas)
        submitted = canvas._effect_jobs.submitted
        render(canvas, True)
        assert canvas._effect_jobs.submitted > submitted

        # One exact handoff can outlive either LRU. Later stroke stages must
        # consume that progress instead of endlessly rebuilding earlier ones.
        result = None
        for _ in range(6):
            job = canvas._effect_jobs.running
            if job is not None:
                job[3].result(timeout=5)
            canvas._effect_jobs.poll()
            qapp.processEvents()
            result = render(canvas, True)
            if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
                break
        else:
            pytest.fail("Sequential stroke stages restarted after cache eviction")
        np.testing.assert_array_equal(pixels(result), expected)

        completed = canvas._effect_jobs.completed
        clear(canvas)
        np.testing.assert_array_equal(pixels(render(canvas, True)), expected)
        assert canvas._effect_jobs.completed == completed
        assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending


@pytest.mark.parametrize("middle", ["outline", "mirror", "blur"])
@pytest.mark.parametrize("budget", [None, 1024], ids=["normal-budget", "one-kib-budget"])
def test_resume_stroke_stack_preserves_mixed_stages_masks_and_parent_transform(
    scene, qapp, middle, budget,
):
    from comic_editor.core.models import MirrorModifier

    canvas, layer, scream = scene
    parent = canvas.chapter.layers[layer.parent_id]
    parent.transform_frame = (0, 0, 700, 420)
    parent.transform_quad = [(45, 25), (695, 5), (720, 405), (25, 415)]
    mask = ToneMask(name="World-space spike height")
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(400, 200), 300, QColor("white"))
    scream.parameter_masks["height"] = ParameterMaskBinding(mask.mask_id, 8, 47)
    effect = {
        "outline": OutlineModifier(thickness=3),
        "mirror": MirrorModifier(axis_start=(450, 0), axis_end=(450, 420)),
        "blur": BlurModifier(strength=2),
    }[middle]
    for modifier in (effect, WobbleModifier(position=7, strength=35)):
        canvas.chapter.add_modifier(modifier, [("layer", layer.layer_id)])
    if budget is not None:
        canvas._modifier_render_cache_budget = budget
        canvas._effect_jobs.retained_budget = budget

    expected = pixels(render(canvas, False))
    clear(canvas)
    render(canvas, True)
    assert canvas._effect_jobs.submitted > 0
    for _ in range(12):
        job = canvas._effect_jobs.running
        if job is not None:
            job[3].result(timeout=5)
        canvas._effect_jobs.poll()
        qapp.processEvents()
        result = render(canvas, True)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            break
    else:
        pytest.fail("Mixed stroke stack did not retain exact progress")
    np.testing.assert_array_equal(pixels(result), expected)


def test_queued_stroke_worker_keeps_full_resolution_fill(scene, qapp):
    canvas, _, _ = scene
    expected = pixels(render(canvas, False))
    clear(canvas)
    started, release = threading.Event(), threading.Event()

    def occupy_worker(cancelled):
        started.set()
        assert release.wait(5)
        return QImage(1, 1, QImage.Format_ARGB32_Premultiplied)

    canvas._effect_jobs.request(("test-blocker",), ("test-blocker",), occupy_worker, 4)
    try:
        assert started.wait(1)
        render(canvas, True)
        assert canvas._effect_jobs.pending
    finally:
        release.set()
    canvas._effect_jobs.running[3].result(timeout=5)
    canvas._effect_jobs.poll()
    # The stroke starts after its caller has already returned a reduced draft.
    # Its captured fill must still have the exact material's dimensions.
    assert canvas._effect_jobs.running is not None
    canvas._effect_jobs.running[3].result(timeout=5)
    np.testing.assert_array_equal(pixels(settle(canvas, qapp)), expected)
