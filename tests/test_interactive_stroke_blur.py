"""Paired stroke blur preserves the established blur stage and worker isolation."""
import threading

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.effect_geometry import effect_bounds
from comic_editor.core.models import BlurModifier, ChapterDocument, ParameterMaskBinding, ToneMask
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import aligned, empty_image, render_stages
from comic_editor.ui import interactive_stroke_blur
from comic_editor.ui.stroke_rendering import placed


@pytest.fixture
def scene(qapp):
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.set_document(ChapterDocument(), TileStore())
    bounds = QRectF(31, -18, 230, 130)
    images = []
    for color in ("#B02070E0", "#FFC8A030"):
        image = empty_image(bounds)
        painter = QPainter(image)
        painter.fillRect(QRectF(18, 13, 167, 78), QColor(color))
        painter.fillRect(QRectF(93, 50, 100, 73), QColor("#708040C0"))
        painter.end()
        images.append(image)
    mapping = QTransform(1.04, .04, .0001, -.06, .92, .00008, 30, 20, 1)
    yield canvas, images, bounds, mapping
    canvas._effect_jobs.cancel()
    canvas.close()
    canvas.deleteLater()


def pixels(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize("algorithm", ["normal", "legacy"])
@pytest.mark.parametrize("mode", ["full", "focal"])
@pytest.mark.parametrize("masked", [False, True])
def test_paired_blur_matches_existing_stage_and_runs_exact_work_off_thread(
    scene, monkeypatch, algorithm, mode, masked,
):
    canvas, sources, bounds, mapping = scene
    modifier = BlurModifier(strength=3, algorithm=algorithm, mode=mode,
        intensity=63, focal_center=(145, 70), focal_radius=95, focal_ramp=.3)
    if masked:
        mask = ToneMask(name="Blur amount")
        canvas.chapter.masks[mask.mask_id] = mask
        canvas.tiles.paint_dab(mask.mask_id, QPointF(130, 65), 150, QColor("white"))
        modifier.parameter_masks = {
            "strength": ParameterMaskBinding(mask.mask_id, .5, 5),
            "intensity": ParameterMaskBinding(mask.mask_id, 10, 85),
        }
    canvas._interactive_render = False
    expected = [render_stages(canvas, source, bounds, [modifier], mapping)
                for source in sources]
    target = aligned(effect_bounds(bounds, [modifier], mapping))
    assert all(output_bounds == target for _, output_bounds in expected)
    inputs = [placed(source, bounds, target) for source in sources]
    original = interactive_stroke_blur.apply_modifier_stack
    worker_calls = []

    def observe(image, *args, **kwargs):
        assert threading.current_thread() is not threading.main_thread()
        worker_calls.append(image.size())
        return original(image, *args, **kwargs)

    monkeypatch.setattr(interactive_stroke_blur, "apply_modifier_stack", observe)
    canvas._interactive_render = True
    scope, key = ("paired-test",), ("blur-exact",)
    *_, provisional = interactive_stroke_blur.render_stroke_blur(
        canvas, *inputs, target, modifier, mapping, cache_key=key, scope=scope)
    assert provisional
    canvas._effect_jobs.running[3].result(timeout=5)
    canvas._effect_jobs.poll()
    material, fill, provisional = interactive_stroke_blur.render_stroke_blur(
        canvas, *inputs, target, modifier, mapping, cache_key=key, scope=scope)
    assert not provisional and len(worker_calls) == 2
    for actual, (reference, _) in zip((material, fill), expected):
        np.testing.assert_array_equal(pixels(actual), pixels(reference))


def test_queued_paired_blur_snapshots_models_masks_and_both_images(scene, monkeypatch):
    canvas, sources, bounds, mapping = scene
    modifier = BlurModifier(strength=3)
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("mask", 0, 100)
    target = aligned(effect_bounds(bounds, [modifier], mapping))
    inputs = [placed(source, bounds, target) for source in sources]
    field = np.linspace(0, 1, inputs[0].width() * inputs[0].height(), dtype=np.float32)
    field = field.reshape(inputs[0].height(), inputs[0].width())
    fields = {(modifier.modifier_id, "intensity"): field}
    monkeypatch.setattr(canvas, "_modifier_mask_fields", lambda *args, **kwargs: fields)
    canvas._interactive_render = False
    expected = interactive_stroke_blur.render_stroke_blur(
        canvas, *inputs, target, modifier, mapping, cache_key=("baseline",), scope=None)[:2]
    started, release = threading.Event(), threading.Event()

    def occupy(cancelled):
        started.set()
        assert release.wait(5)
        return QImage(1, 1, QImage.Format_ARGB32_Premultiplied)

    canvas._effect_jobs.request(("blocker",), ("blocker",), occupy, 4)
    try:
        assert started.wait(1)
        canvas._interactive_render = True
        scope, key = ("snapshot",), ("snapshot",)
        *_, provisional = interactive_stroke_blur.render_stroke_blur(
            canvas, *inputs, target, modifier, mapping, cache_key=key, scope=scope)
        assert provisional and canvas._effect_jobs.pending
        modifier.strength = 25
        modifier.parameter_masks["intensity"].white_value = 20
        field.fill(0)
        for image in inputs:
            image.fill(Qt.transparent)
        mapping.translate(200, 100)
    finally:
        release.set()
    canvas._effect_jobs.running[3].result(timeout=5)
    canvas._effect_jobs.poll()
    canvas._effect_jobs.running[3].result(timeout=5)
    canvas._effect_jobs.poll()
    actual = interactive_stroke_blur.cached_blur(canvas, scope, key)
    for output, reference in zip(actual, expected):
        np.testing.assert_array_equal(pixels(output), pixels(reference))


@pytest.mark.parametrize("algorithm", ["normal", "legacy"])
def test_focal_blur_intensity_mask_preserves_zero_and_full_regions(scene, algorithm):
    from comic_editor.ui.modifier_rendering import apply_modifier_stack

    _, (source, _), _, _ = scene
    modifier = BlurModifier(strength=4, algorithm=algorithm, mode="focal",
        focal_center=(110, 60), focal_radius=65, focal_ramp=.25)
    unmasked = apply_modifier_stack(source, [modifier], (0, 0))
    modifier.parameter_masks["intensity"] = ParameterMaskBinding("intensity", 0, 100)
    amount = np.zeros((source.height(), source.width()), dtype=np.float32)
    amount[:, source.width() // 2:] = 1
    masked = apply_modifier_stack(source, [modifier], (0, 0),
        {(modifier.modifier_id, "intensity"): amount})
    shape = (source.height(), source.width(), 4)
    expected = pixels(source).reshape(shape)
    expected[amount == 1] = pixels(unmasked).reshape(shape)[amount == 1]
    np.testing.assert_array_equal(pixels(masked).reshape(shape), expected)
