"""Document tiles retain exact effect semantics and independent worker scopes."""
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, BrightnessContrastModifier, CageTransformModifier,
    ChapterDocument, CurvesModifier, DistortModifier, HalftoneModifier,
    HueSaturationLightnessModifier, OutlineModifier, ParameterMaskBinding,
    PixelateModifier, PosterizeModifier, RadialBlurModifier, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.effect_pipeline import cached_stage_output, render_stages
from comic_editor.ui.effect_regions import pointwise_stack, region_scope
from comic_editor.ui.modifier_rendering import _premultiplied_qimage


@pytest.fixture
def scene(qapp, monkeypatch):
    canvas = CanvasWidget(EditorSettings(canvas_renderer="raster"))
    chapter = ChapterDocument(width=800, height=600, document_kind="asset")
    chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 800, 600))
    canvas.set_document(chapter, TileStore())
    monkeypatch.setattr("comic_editor.ui.gpu_pattern_effects.renderer_for", lambda _: None)
    monkeypatch.setattr("comic_editor.ui.gpu_textures.renderer_for", lambda _: None)
    yield canvas
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def image(width=530, height=403):
    yy, xx = np.mgrid[:height, :width]
    alpha = ((xx + yy) % 193) / 192.
    pixels = np.stack((xx / width, yy / height, (xx % 7) / 7, np.ones_like(xx)), axis=-1)
    pixels *= alpha[..., None]
    return _premultiplied_qimage(pixels)


def crop(image, bounds, required):
    region = QRectF(bounds.intersected(required).toAlignedRect()).intersected(bounds)
    rect = QRectF(region)
    rect.translate(-bounds.topLeft())
    return image.copy(rect.toAlignedRect()), region


def register(canvas, modifiers):
    for modifier in modifiers:
        canvas.chapter.modifiers[modifier.modifier_id] = modifier


def enable(canvas):
    canvas._interactive_render = True
    canvas._effect_region_requests = True


@pytest.mark.parametrize("modifiers", [
    [BrightnessContrastModifier(brightness=17, contrast=-13)],
    [CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]})],
    [HueSaturationLightnessModifier(hue=57, saturation=28, lightness=-11)],
    [PosterizeModifier()],
    [BrightnessContrastModifier(brightness=17),
     HueSaturationLightnessModifier(hue=57),
     CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .7), (1, 1)]})],
])
def test_pointwise_tiles_match_full_frame_at_negative_odd_bounds(scene, monkeypatch, modifiers):
    register(scene, modifiers)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    full, full_bounds = render_stages(scene, source, bounds, modifiers, QTransform())
    enable(scene)
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: pytest.fail("Pointwise tile queued a draft"))
    for requested in (QRectF(-25, -45, 280, 310), QRectF(128, 12, 300, 230),
                      QRectF(-100, -100, 800, 600)):
        result = render_stages(scene, source, bounds, modifiers, QTransform(),
            required=requested, source_key=("source",), request_scope=("object", "a", "canvas"))
        expected = crop(full, full_bounds, requested)
        assert result[1] == expected[1]
        assert result[0] == expected[0]


def test_overlapping_requests_reuse_tiles_before_source_capture(scene, monkeypatch):
    modifier = BrightnessContrastModifier(brightness=15)
    register(scene, [modifier])
    source, bounds = image(600, 400), QRectF(0, 0, 600, 400)
    fields = []
    original = scene._modifier_mask_fields
    monkeypatch.setattr(scene, "_modifier_mask_fields", lambda *a, **k: fields.append(a[1:3]) or original(*a, **k))
    enable(scene)
    args = dict(source_key=("unchanged",), request_scope=("object", "a", "canvas"))
    render_stages(scene, source, bounds, [modifier], QTransform(), required=QRectF(20, 20, 280, 150), **args)
    assert len(fields) == 2
    expected = render_stages(scene, source, bounds, [modifier], QTransform(), required=QRectF(80, 40, 290, 130), **args)
    assert len(fields) == 2
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    cached = cached_stage_output(scene, bounds, [modifier], QTransform(), required=QRectF(80, 40, 290, 130), **args)
    assert cached == expected
    assert len(fields) == 2
    modifier.brightness = 30
    assert cached_stage_output(scene, bounds, [modifier], QTransform(), required=QRectF(80, 40, 290, 130), **args) is None
    assert render_stages(scene, source, bounds, [modifier], QTransform(), required=QRectF(80, 40, 290, 130), **args)[0] != expected[0]
    assert len(fields) == 4


def test_pointwise_masks_use_full_document_mapping_and_revision(scene, monkeypatch):
    modifier = BrightnessContrastModifier(brightness=65)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    register(scene, [modifier])
    def fields(modifiers, width, height, mapping, _bounds):
        inverse = mapping.inverted()[0]
        yy, xx = np.mgrid[:height, :width]
        world_x = inverse.m11() * (xx + .5) + inverse.m21() * (yy + .5) + inverse.dx()
        field = np.clip((world_x + 100) / 700 + mask.revision * .1, 0, 1).astype(np.float32)
        return {(modifier.modifier_id, "intensity"): field}
    monkeypatch.setattr(scene, "_modifier_mask_fields", fields)
    source, bounds = image(), QRectF(-37, -61, 530, 403)
    mapping = QTransform().translate(13, 22).scale(1.5, .5)
    full, full_bounds = render_stages(scene, source, bounds, [modifier], mapping)
    enable(scene)
    required = QRectF(-20, 21, 340, 210)
    args = dict(required=required, source_key=("source",), request_scope=("object", "masked", "canvas"))
    result = render_stages(scene, source, bounds, [modifier], mapping, **args)
    assert result == crop(full, full_bounds, required)
    mask.revision += 1
    assert cached_stage_output(scene, bounds, [modifier], mapping, **args) is None
    assert render_stages(scene, source, bounds, [modifier], mapping, **args)[0] != result[0]


@pytest.mark.parametrize("modifier", [BlurModifier(strength=7), HalftoneModifier(),
                                       PixelateModifier(), PosterizeModifier(simplify_enabled=True)])
def test_frame_dependent_effects_do_not_enter_pointwise_adapter(modifier):
    assert not pointwise_stack([modifier])


def test_blur_region_requests_preserve_full_frame_phase(scene, monkeypatch):
    modifiers = [BrightnessContrastModifier(brightness=12), BlurModifier(strength=7),
                 HueSaturationLightnessModifier(hue=30)]
    register(scene, modifiers)
    source, bounds = image(331, 273), QRectF(-31, 17, 331, 273)
    full, full_bounds = render_stages(scene, source, bounds, modifiers, QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: False)
    for region in (QRectF(11, 25, 110, 80), QRectF(91, 73, 170, 140)):
        result = render_stages(scene, source, bounds, modifiers, QTransform(), required=region,
            source_key=("source",), request_scope=("object", "blur", "canvas"))
        assert result == crop(full, full_bounds, region)


@pytest.mark.parametrize("factory", [lambda: PixelateModifier(pixel_size=13),
                                    lambda: HalftoneModifier(base_resolution=30, blur=0)])
def test_pattern_regions_share_one_exact_semantic_frame(scene, monkeypatch, factory):
    from comic_editor.ui import modifier_rendering
    modifier = factory()
    register(scene, [modifier])
    source, bounds = image(320, 271), QRectF(-11, 7, 320, 271)
    full, full_bounds = render_stages(scene, source, bounds, [modifier], QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: False)
    original = modifier_rendering.apply_pattern_modifier
    computes = []
    def compute(*args, **kwargs):
        if kwargs.get("allow_cpu_fallback", True):
            computes.append(args[0].size().toTuple())
        return original(*args, **kwargs)
    monkeypatch.setattr(modifier_rendering, "apply_pattern_modifier", compute)
    for region in (QRectF(13, 27, 120, 100), QRectF(87, 91, 180, 130)):
        result = render_stages(scene, source, bounds, [modifier], QTransform(), required=region,
            source_key=("source",), request_scope=("object", "pattern", "canvas"))
        assert result == crop(full, full_bounds, region)
    assert computes == [(320, 271)]


@pytest.mark.parametrize("modifier", [PixelateModifier(pixel_size=13),
                                    HalftoneModifier(base_resolution=30, blur=0)])
def test_complete_pattern_frame_survives_stage_retention_and_lru_pressure(scene, monkeypatch, modifier):
    from comic_editor.ui import modifier_rendering
    register(scene, [modifier])
    source, bounds = image(320, 271), QRectF(-11, 7, 320, 271)
    enable(scene)
    scene._projection_exact = True
    scene._effect_jobs.retained_budget = source.sizeInBytes() * 4
    arguments = dict(source_key=("source",), request_scope=("object", "pattern", "canvas"))
    full, full_bounds = render_stages(scene, source, bounds, [modifier], QTransform(),
                                    required=bounds, **arguments)
    # Both complete-frame and stage outputs have just been retained. Discard
    # only the ordinary LRU, as unrelated large effects do in a real scene.
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    for index in range(20):
        scene._effect_jobs.retained_put(("unrelated", index), index, source.copy())
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
    monkeypatch.setattr(modifier_rendering, "apply_pattern_modifier",
                        lambda *args, **kwargs: pytest.fail("Exact full pattern frame was recomputed"))
    for region in (QRectF(13, 27, 120, 100), QRectF(87, 91, 180, 130)):
        result = render_stages(scene, source, bounds, [modifier], QTransform(), required=region, **arguments)
        assert result == crop(full, full_bounds, region)


def test_warp_regions_share_exact_upstream_prefix_after_lru_eviction(scene, monkeypatch):
    from comic_editor.ui import distort_rendering
    source, bounds = image(224, 192), QRectF(0, 0, 224, 192)
    prefix = DistortModifier(modifier_type="distort_deform", frame=(0, 0, 224, 192))
    last = DistortModifier(modifier_type="distort_twirl", parameters={"angle": 35},
                          frame=(0, 0, 224, 192), center=(112, 96), radius=70)
    register(scene, [prefix, last])
    full, full_bounds = render_stages(scene, source, bounds, [prefix, last], QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    scene._projection_exact = True
    scene._effect_jobs.retained_budget = source.sizeInBytes() * 4
    calls = []
    original = distort_rendering.render_distort
    monkeypatch.setattr(distort_rendering, "render_distort", lambda *args, **kwargs:
                        (calls.append(args[2].modifier_id), original(*args, **kwargs))[1])
    arguments = dict(source_key=("source",), request_scope=("object", "warps", "canvas"))
    for region in (QRectF(8, 16, 80, 90), QRectF(90, 50, 80, 90)):
        result = render_stages(scene, source, bounds, [prefix, last], QTransform(),
                               required=region, **arguments)
        assert result == crop(full, full_bounds, region)
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        for index in range(20):
            scene._effect_jobs.retained_put(("unrelated", index), index, source.copy())
        assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
    assert calls.count(prefix.modifier_id) == 1
    assert calls.count(last.modifier_id) == 2
    # Independent retention still checks source identity before reuse.
    arguments["source_key"] = ("changed-source",)
    render_stages(scene, source, bounds, [prefix, last], QTransform(),
                  required=QRectF(90, 50, 80, 90), **arguments)
    assert calls.count(prefix.modifier_id) == 2


def test_region_workers_do_not_cancel_siblings_and_completion_uses_projection_hook(scene, monkeypatch):
    enable(scene)
    first_scope = region_scope(scene, ("effect",), QRectF(0, 0, 256, 256))
    second_scope = region_scope(scene, ("effect",), QRectF(256, 0, 256, 256))
    assert first_scope != second_scope
    jobs, gate, entered = scene._effect_jobs, Event(), Event()
    source = image(16, 16)
    completed = []
    monkeypatch.setattr(scene, "_effect_result_ready", lambda scope, key: completed.append((scope, key)), raising=False)
    monkeypatch.setattr(scene, "_invalidate_scene_cache", lambda: pytest.fail("Completion evicted exact projection tiles"))
    def first(cancelled):
        entered.set()
        assert gate.wait(3)
        return None if cancelled() else source
    try:
        assert jobs.request(first_scope, ("first",), first, 4096)
        assert entered.wait(3)
        assert jobs.request(second_scope, ("second",), lambda _: source, 4096)
        assert not jobs.running[2].is_set()
        gate.set()
        jobs.running[3].result(timeout=3)
        jobs.poll()
        jobs.running[3].result(timeout=3)
        jobs.poll()
        assert jobs.result(first_scope, ("first",)) == source
        assert jobs.result(second_scope, ("second",)) == source
        assert completed == [(first_scope, ("first",)), (second_scope, ("second",))]
    finally:
        gate.set()


@pytest.mark.parametrize("modifier", [
    BlurModifier(strength=3), HueSaturationLightnessModifier(hue=45),
    OutlineModifier(thickness=3, blur_radius=2, blur_strength=65),
])
def test_exact_projection_generic_stack_never_queues_or_drafts(scene, monkeypatch, modifier):
    from comic_editor.ui import interactive_effects
    source = image(230, 197)
    arguments = dict(cache_key=("generic-exact",), scope=("object", "generic"))
    expected, provisional = interactive_effects.render_interactive_stack(
        scene, source, [modifier], (13, -9), **arguments)
    assert not provisional
    enable(scene)
    scene._projection_exact = True
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: pytest.fail("Exact projection queued work"))
    monkeypatch.setattr(interactive_effects, "_draft", lambda *_a, **_k: pytest.fail("Exact projection reduced pixels"))
    actual, provisional = interactive_effects.render_interactive_stack(
        scene, source, [modifier], (13, -9), **arguments)
    assert not provisional and actual == expected
    assert scene._interactive_render


@pytest.mark.parametrize("modifier", [
    HalftoneModifier(base_resolution=20, blur=0), PixelateModifier(pixel_size=13),
    CageTransformModifier(frame=(0, 0, 224, 192)),
    RadialBlurModifier(angle=10, center=(112, 96)),
    DistortModifier(modifier_type="distort_twirl", parameters={"angle": 30},
                    frame=(0, 0, 224, 192), center=(112, 96), radius=75),
])
def test_exact_projection_spatial_stages_complete_first_capture(scene, monkeypatch, modifier):
    register(scene, [modifier])
    source, bounds = image(224, 192), QRectF(0, 0, 224, 192)
    expected = render_stages(scene, source, bounds, [modifier], QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    scene._projection_exact = True
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: pytest.fail("Exact projection queued work"))
    before = getattr(scene, "_effect_provisional_revision", 0)
    actual = render_stages(scene, source, bounds, [modifier], QTransform(),
        source_key=("cold-projection",), request_scope=("object", "cold", "canvas"))
    assert actual == expected
    assert getattr(scene, "_effect_provisional_revision", 0) == before
    assert scene._interactive_render
    assert not any("draft" in str(key[0]) for key in scene._modifier_render_cache)


def test_exact_projection_stroke_material_and_blur_complete_and_retain(scene, monkeypatch):
    from comic_editor.core.stroke_geometry import StrokeLoop
    from comic_editor.ui import interactive_strokes, interactive_stroke_blur
    source, background = image(230, 197), image(230, 197)
    background.fill(0)
    bounds, mapping = QRectF(0, 0, 230, 197), QTransform()
    before = [StrokeLoop(np.array([[30., 30.], [190., 30.], [190., 160.], [30., 160.]]), 8)]
    after = [StrokeLoop(before[0].points + np.array([3., 2.]), 8)]
    opacity = [np.array([1., .8, 1., .9])]
    stroke_args = dict(cache_key=("cold-stroke",), scope=("object", "stroke"))
    expected_stroke = interactive_strokes.render_interactive_stroke(
        scene, source, background, bounds, before, after, opacity, **stroke_args)
    blur_args = dict(cache_key=("cold-blur",), scope=("object", "stroke-blur"))
    blur = BlurModifier(strength=3)
    expected_blur = interactive_stroke_blur.render_stroke_blur(
        scene, source, background, bounds, blur, mapping, **blur_args)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    scene._projection_exact = True
    monkeypatch.setattr(scene._effect_jobs, "request", lambda *_a, **_k: pytest.fail("Exact projection queued work"))
    monkeypatch.setattr(interactive_strokes, "_draft", lambda *_a, **_k: pytest.fail("Stroke draft"))
    monkeypatch.setattr(interactive_stroke_blur, "_draft", lambda *_a, **_k: pytest.fail("Stroke blur draft"))
    actual_stroke = interactive_strokes.render_interactive_stroke(
        scene, source, background, bounds, before, after, opacity, **stroke_args)
    actual_blur = interactive_stroke_blur.render_stroke_blur(
        scene, source, background, bounds, blur, mapping, **blur_args)
    assert actual_stroke == expected_stroke and not actual_stroke[-1]
    assert actual_blur == expected_blur and not actual_blur[-1]
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    monkeypatch.setattr(interactive_strokes, "_render", lambda *_a, **_k: pytest.fail("Exact stroke cache lost"))
    monkeypatch.setattr(interactive_stroke_blur, "apply_modifier_stack", lambda *_a, **_k: pytest.fail("Exact blur cache lost"))
    assert interactive_strokes.render_interactive_stroke(
        scene, source, background, bounds, before, after, opacity, **stroke_args) == actual_stroke
    assert interactive_stroke_blur.render_stroke_blur(
        scene, source, background, bounds, blur, mapping, **blur_args) == actual_blur
