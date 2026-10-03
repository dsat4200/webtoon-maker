"""Both hierarchy passes finish their own detached effect work exactly once."""
import threading

import numpy as np
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, HueSaturationLightnessModifier,
    OutlineModifier, RasterObject, ShapeStyle,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui import interactive_effects


@pytest.fixture
def scene(qapp):
    chapter = ChapterDocument(width=720, height=420)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 720, 420))
    page.fill_color, page.border_width = None, 0
    parent = chapter.add_layer(page.layer_id, "Mixed parent",
        BoundGeometry.rectangle(40, 40, 600, 320),
        style=ShapeStyle(primary_color=None, outline_thickness=0))
    ordinary = chapter.add_object(parent.layer_id, RasterObject())
    promoted = chapter.add_object(parent.layer_id, RasterObject(show_on_top=True))
    tiles = TileStore()
    tiles.paint_dab(ordinary.object_id, QPointF(470, 210), 170, QColor("blue"))
    tiles.paint_dab(promoted.object_id, QPointF(230, 210), 170, QColor("#df407c"))
    chapter.add_layer(page.layer_id, "Ordinary foreground",
        BoundGeometry.rectangle(20, 20, 660, 370), index=0,
        style=ShapeStyle(primary_color="#00ff00", outline_thickness=0))
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(chapter, tiles)
    yield canvas, parent, promoted
    canvas._effect_jobs.cancel()
    if canvas._effect_jobs.running is not None:
        canvas._effect_jobs.running[3].result(timeout=5)
        canvas._effect_jobs.poll()
    canvas.close()
    canvas.deleteLater()


def render(canvas, interactive):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    previous = canvas._interactive_render
    canvas._interactive_render = interactive
    try:
        canvas.render_preview(image)
    finally:
        canvas._interactive_render = previous
    return image


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


def clear_scene_caches(canvas):
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0


def settle(canvas, qapp):
    for _ in range(8):
        running = canvas._effect_jobs.running
        if running is not None:
            running[3].result(timeout=5)
        canvas._effect_jobs.poll()
        qapp.processEvents()
        result = render(canvas, True)
        if not canvas._effect_jobs.running and not canvas._effect_jobs.pending:
            return result
    pytest.fail(
        "Ordinary/promoted effect passes kept restarting: "
        f"submitted={canvas._effect_jobs.submitted}, completed={canvas._effect_jobs.completed}, "
        f"discarded={canvas._effect_jobs.discarded}"
    )


@pytest.mark.parametrize("stack", ["blur", "hsl-blur-outline"])
@pytest.mark.parametrize("tiny_caches", [False, True], ids=["normal-caches", "one-kib-scene-caches"])
def test_promoted_parent_passes_converge_without_canceling_each_other(
    scene, qapp, monkeypatch, stack, tiny_caches,
):
    canvas, parent, _promoted = scene
    modifiers = [BlurModifier(strength=4)]
    if stack == "hsl-blur-outline":
        modifiers = [HueSaturationLightnessModifier(hue=25), *modifiers,
                     OutlineModifier(thickness=3)]
    for modifier in modifiers:
        canvas.chapter.add_modifier(modifier, [("layer", parent.layer_id)])
    expected = render(canvas, False)
    assert expected.pixelColor(470, 210) == QColor("lime")
    assert expected.pixelColor(230, 210) != QColor("lime")
    clear_scene_caches(canvas)
    if tiny_caches:
        canvas._modifier_render_cache_budget = 1024
        canvas._modifier_source_cache_budget = 1024

    # Hold the first full-resolution worker while both passes repaint. A
    # shared latest-request-wins scope would cancel it on the very first frame.
    entered, release = threading.Event(), threading.Event()
    original = interactive_effects.apply_modifier_stack
    def gated(image, effects, *args, **kwargs):
        if image.width() > 256 and threading.current_thread() is not threading.main_thread():
            entered.set()
            assert release.wait(5), "Effect worker gate was not released"
        return original(image, effects, *args, **kwargs)
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", gated)
    try:
        draft = render(canvas, True)
        assert entered.wait(1)
        jobs = canvas._effect_jobs
        admitted = (*jobs.running_jobs, *jobs.pending.values())
        assert len(admitted) == 2
        assert all(not job[2].is_set() for job in admitted), "Top pass canceled the ordinary pass"
        assert len({job[0] for job in admitted}) == 2
        for _ in range(2):
            render(canvas, True)
            admitted = (*jobs.running_jobs, *jobs.pending.values())
            assert all(not job[2].is_set() for job in admitted), "Repaint canceled an unchanged effect pass"
            assert len(admitted) == 2
        assert not np.array_equal(pixels(draft), pixels(expected))
    finally:
        release.set()

    actual = settle(canvas, qapp)
    np.testing.assert_array_equal(pixels(actual), pixels(expected))
    assert canvas._effect_jobs.completed == 2
    assert canvas._effect_jobs.discarded == 0
    assert canvas._effect_jobs.submitted == 2
    # Rebuilding scene/source LRUs must still consume both exact handoffs.
    clear_scene_caches(canvas)
    np.testing.assert_array_equal(pixels(render(canvas, True)), pixels(expected))
    assert canvas._effect_jobs.submitted == 2
    assert not canvas._effect_jobs.running and not canvas._effect_jobs.pending
    np.testing.assert_array_equal(pixels(render(canvas, False)), pixels(expected))
