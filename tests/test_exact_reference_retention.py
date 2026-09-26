"""Native wand samples reuse exact stages without changing sampling policy."""
import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage, QTransform

from comic_editor.core.models import BlurModifier, DistortModifier, OutlineModifier, PixelateModifier, RasterObject
from comic_editor.ui import distort_pipeline, interactive_effects, modifier_rendering
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.effect_regions import exact_reference_sampling, region_requests_enabled
from test_effect_regions import scene, image, register, crop


@pytest.fixture(autouse=True)
def disconnected_canvas(monkeypatch):
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", lambda *_: None)


def enable_reference(canvas, monkeypatch):
    canvas._interactive_render = False
    canvas._projection_exact = canvas._exact_reference_render = True
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    canvas._modifier_render_cache_budget = 1
    monkeypatch.setattr(canvas._effect_jobs, "request", lambda *_a, **_k: pytest.fail("Exact reference queued work"))
    assert exact_reference_sampling(canvas)
    assert not region_requests_enabled(canvas)


def test_noninteractive_warp_prefix_survives_ordinary_cache_eviction(scene, monkeypatch):
    first = DistortModifier(modifier_type="distort_deform", frame=(0, 0, 320, 240),
        source_points=[(0, 0), (1, 0), (1, 1), (0, 1)],
        points=[(.04, .01), (.95, .03), (.97, .96), (.01, .97)])
    second = DistortModifier(modifier_type="distort_twirl", frame=(0, 0, 320, 240),
        center=(160, 120), radius=110, parameters={"angle": 35})
    register(scene, [first, second])
    source, bounds = image(320, 240), QRectF(0, 0, 320, 240)
    expected, full_bounds = render_stages(scene, source, bounds, [first, second], QTransform())
    enable_reference(scene, monkeypatch)
    calls = []
    original = distort_pipeline.render_distort_stage
    def record(*args, **kwargs):
        calls.append(args[5].modifier_id)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_pipeline, "render_distort_stage", record)
    arguments = dict(source_key=("source", 1), request_scope=("object", "image", "canvas"))
    for region in (QRectF(30, 40, 100, 100), QRectF(130, 70, 100, 100)):
        assert render_stages(scene, source, bounds, [first, second], QTransform(),
                             required=region, **arguments) == crop(expected, full_bounds, region)
    assert calls.count(first.modifier_id) == 1 and calls.count(second.modifier_id) == 2
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget
    assert scene._effect_jobs.retained_shared_bytes > 0
    assert not scene._interactive_render and not region_requests_enabled(scene)
    arguments["source_key"] = ("source", 2)
    render_stages(scene, source, bounds, [first, second], QTransform(),
                  required=QRectF(30, 40, 100, 100), **arguments)
    assert calls.count(first.modifier_id) == 2


def test_noninteractive_outline_reuses_finished_result_after_lru_eviction(scene, monkeypatch):
    source, outline = image(170, 130), OutlineModifier(thickness=5, blur_radius=3)
    original = interactive_effects.apply_modifier_stack
    expected = original(source, [outline], (0, 0), {})
    enable_reference(scene, monkeypatch)
    calls = []
    def record(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", record)
    def render(key):
        return interactive_effects.render_interactive_stack(scene, source, [outline], (0, 0),
            cache_key=key, scope=("object", "outline", "canvas"))
    assert render("stable") == (expected, False)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    assert render("stable") == (expected, False) and len(calls) == 1
    outline.thickness = 8
    assert render("edited")[0] == original(source, [outline], (0, 0), {}) and len(calls) == 2


def test_native_reference_reuses_actual_outlined_object_across_tiles(scene, monkeypatch):
    page_id = scene.chapter.root_page_ids[0]
    obj = scene.chapter.add_object(page_id, RasterObject(interaction_rect=(0, 0, 650, 180)))
    scene.tiles.paint_line(obj.object_id, QPointF(25, 90), QPointF(625, 90),
                           80, QColor("red"), square=True, antialias=False)
    outline = OutlineModifier(thickness=9, blur_radius=4)
    scene.chapter.add_modifier(outline, [("object", obj.object_id)])
    def capture(x):
        result = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
        scene.render_preview(result, source_rect=QRectF(x, 0, 256, 256))
        return result
    expected = [capture(x) for x in (0, 256)]
    enable_reference(scene, monkeypatch)
    calls = []
    original = interactive_effects.apply_modifier_stack
    def record(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", record)
    with scene._mask_wand_reference_render():
        assert capture(0) == expected[0]
        scene._modifier_render_cache.clear()
        scene._modifier_render_cache_bytes = 0
        scene._modifier_source_cache.clear()
        scene._modifier_source_cache_bytes = 0
        assert capture(256) == expected[1]
    assert len(calls) == 1
    assert scene._effect_jobs.retained_bytes <= scene._effect_jobs.retained_budget


def test_reference_pattern_full_frame_reused_across_crops(scene, monkeypatch):
    source, bounds = image(171, 133), QRectF(-17, 9, 171, 133)
    pattern = PixelateModifier(pixel_size=13, blur=3)
    register(scene, [pattern])
    expected, full_bounds = render_stages(scene, source, bounds, [pattern], QTransform())
    enable_reference(scene, monkeypatch)
    calls = []
    original = modifier_rendering.apply_pattern_modifier
    def record(*args, **kwargs):
        if kwargs.get("allow_cpu_fallback", True):
            calls.append(1)
        return original(*args, **kwargs)
    monkeypatch.setattr(modifier_rendering, "apply_pattern_modifier", record)
    for region in (QRectF(0, 20, 61, 53), QRectF(60, 50, 70, 60)):
        assert render_stages(scene, source, bounds, [pattern], QTransform(), required=region,
            source_key="pattern-source", request_scope=("object", "pattern", "canvas")) == crop(expected, full_bounds, region)
    assert len(calls) == 1
    assert not region_requests_enabled(scene)


def test_reference_blur_keeps_prior_noninteractive_crop_semantics(scene, monkeypatch):
    source, bounds = image(173, 135), QRectF(-21, 11, 173, 135)
    blur = BlurModifier(strength=17)
    register(scene, [blur])
    region = QRectF(13, 29, 79, 71)
    expected = render_stages(scene, source, bounds, [blur], QTransform(), required=region)
    enable_reference(scene, monkeypatch)
    assert render_stages(scene, source, bounds, [blur], QTransform(), required=region,
        source_key="blur-source", request_scope=("object", "blur", "canvas")) == expected
    assert not region_requests_enabled(scene)


@pytest.mark.parametrize("attribute,value", [
    ("_exact_reference_render", False), ("_projection_exact", False),
    ("_render_base_alpha", True), ("_rendering_mask_contributor", 1),
    ("_render_modifier_sources", {("object", "nested")}),
    ("_render_cage_source", True), ("_rendering_halftone_source", True),
    ("_rendering_compound_references", True), ("_rendering_outward_gradient", True),
    ("_tiling_capture_geometry", object()), ("_effect_preview_channel", "navigator"),
])
def test_reference_retention_requires_explicit_scope_and_safe_capture(scene, monkeypatch, attribute, value):
    enable_reference(scene, monkeypatch)
    setattr(scene, attribute, value)
    assert not exact_reference_sampling(scene)
