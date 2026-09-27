"""Smudge carries paint in stroke order with exact, alpha-safe regional output."""
import copy

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import DistortModifier
from comic_editor.core.smudge import default_tool_settings, validate_strokes
from comic_editor.ui import smudge_rendering
from comic_editor.ui.distort_rendering import PreparedDistortCache, distort_bounds, render_distort
from comic_editor.ui.effect_pipeline import _stage_plan
from test_distort_projection_async import canvas as async_canvas
from test_scene_culling import scene


def source(width=256, height=128):
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("blue"))
    painter = QPainter(image)
    painter.fillRect(0, 0, width//3, height, QColor("red"))
    painter.end()
    return image


def stroke(start=(55, 64), end=(205, 64), *, identifier="stroke", radius=20.,
           flow=100., strength=100., pressure_enabled=False):
    settings = default_tool_settings()
    settings["pressure_enabled"] = pressure_enabled
    return validate_strokes([{"id": identifier, "points": [
        {"position": start, "handle": start, "point_type": "vector", "radius": radius,
         "flow": flow, "strength": strength},
        {"position": end, "handle": end, "point_type": "vector", "radius": radius,
         "flow": flow, "strength": strength}],
        "pressure_settings": settings}])[0]


def modifier(*strokes, opacity=100.):
    value = DistortModifier(modifier_type="distort_smudge",
        parameters={"strokes": list(strokes), "opacity": opacity})
    value.validate()
    return value


def pixels(image):
    value = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    return np.frombuffer(value.constBits(), np.uint8).reshape(value.height(), value.width(), 4).copy()


def render(image, effect, **kwargs):
    return render_distort(image, QRectF(0, 0, image.width(), image.height()), effect,
                          output_bounds=QRectF(0, 0, image.width(), image.height()), **kwargs)


def test_smudge_carries_red_paint_onto_blue_in_direction_without_changing_unaffected_pixels():
    image = source()
    result = render(image, modifier(stroke()))
    assert result.pixelColor(170, 64).red() > 80
    assert result.pixelColor(170, 64).blue() < 175
    assert result.pixelColor(170, 10) == image.pixelColor(170, 10)
    assert result.pixelColor(20, 64) == image.pixelColor(20, 64)
    reverse = render(image, modifier(stroke(start=(205, 64), end=(55, 64))))
    assert reverse.pixelColor(60, 64).blue() > 80
    assert reverse != result


@pytest.mark.parametrize("parameter", ["flow", "strength", "opacity", "empty", "stationary"])
def test_neutral_smudge_is_byte_exact_original(parameter):
    image = source()
    points = stroke(**{parameter: 0}) if parameter in {"flow", "strength"} else stroke()
    if parameter == "stationary":
        points = stroke(end=(55, 64))
    effect = modifier(*([] if parameter == "empty" else [points]), opacity=0 if parameter == "opacity" else 100)
    assert render(image, effect) == image


def test_opacity_is_overlay_mix_and_uses_identical_stroke_checkpoints(monkeypatch):
    image, cache = source(), PreparedDistortCache()
    effect = modifier(stroke())
    full = render(image, effect, preparation_cache=cache)
    calls = []
    original = smudge_rendering._stroke
    monkeypatch.setattr(smudge_rendering, "_stroke", lambda *a, **kw: (calls.append(1), original(*a, **kw))[1])
    effect.parameters["opacity"] = 25.
    quarter = render(image, effect, preparation_cache=cache)
    assert calls == []
    expected = pixels(image).astype(float)*.75 + pixels(full).astype(float)*.25
    assert np.max(np.abs(pixels(quarter).astype(float)-expected)) <= 1


def test_multiple_strokes_use_prior_paint_and_order_matters():
    image = source()
    first = stroke(end=(160, 64))
    second = stroke(start=(150, 64), end=(150, 105), identifier="second", radius=12)
    sequential = render(image, modifier(first, second))
    reversed_order = render(image, modifier(second, first))
    assert sequential.pixelColor(150, 100).red() > reversed_order.pixelColor(150, 100).red()+30


def test_transparent_exterior_carries_alpha_without_dark_color_fringes():
    image = source()
    image.fill(QColor(0, 0, 0, 0))
    painter = QPainter(image)
    painter.fillRect(20, 40, 70, 48, QColor(255, 0, 0, 128))
    painter.end()
    result = render(image, modifier(stroke()))
    data = pixels(result)
    assert np.all(data[..., :3] <= data[..., 3:4])
    assert np.all(data[..., 1:3] == 0)
    assert result.pixelColor(165, 64).alpha() > 0
    assert result.pixelColor(165, 64).red() >= 250


def test_output_regions_match_whole_result_and_world_translation():
    image, effect, cache = source(), modifier(stroke()), PreparedDistortCache()
    expected = render(image, effect, preparation_cache=cache)
    bounds = QRectF(0, 0, 256, 128)
    for left in (0, 64, 128, 192):
        region = render_distort(image, bounds, effect, output_bounds=QRectF(left, 0, 64, 128),
                                preparation_cache=cache)
        assert region == expected.copy(left, 0, 64, 128)
    moved = copy.deepcopy(effect)
    for point in moved.parameters["strokes"][0]["points"]:
        for key in ("position", "handle"):
            point[key] = [point[key][0]+35, point[key][1]-20]
    assert render_distort(image, bounds, moved, QTransform.fromTranslate(35, -20), bounds) == expected


def test_smudge_stage_extent_and_cache_key_survive_camera_requests(scene):
    canvas = scene[0]
    effect = modifier(stroke())
    bounds = QRectF(0, 0, 256, 128)
    left = _stage_plan(canvas, bounds, [effect], QTransform(), "source", False,
                       QRectF(0, 0, 80, 80))
    right = _stage_plan(canvas, bounds, [effect], QTransform(), "source", False,
                        QRectF(176, 40, 80, 80))
    assert left.key == right.key
    assert left.targets == right.targets
    assert left.targets[0].contains(bounds)


@pytest.mark.parametrize("new_end", [-30, 310])
def test_unchanged_stroke_prefix_survives_expanded_work_bounds(monkeypatch, new_end):
    image = source()
    first = stroke(identifier="first")
    second = stroke(start=(180, 64), end=(225, 64), identifier="last")
    effect = modifier(first, second)
    cache = PreparedDistortCache()
    target = QRectF(-80, 0, 440, 128)
    render_distort(image, QRectF(0, 0, 256, 128), effect,
                   output_bounds=target, preparation_cache=cache)
    calls = []
    original = smudge_rendering._stroke
    monkeypatch.setattr(smudge_rendering, "_stroke", lambda pixels, item, *args:
                        (calls.append(item["id"]), original(pixels, item, *args))[1])
    effect.parameters["strokes"][1]["points"][1]["position"] = [new_end, 64]
    effect.parameters["strokes"][1]["points"][1]["handle"] = [new_end, 64]
    cached = render_distort(image, QRectF(0, 0, 256, 128), effect,
                            output_bounds=target, preparation_cache=cache)
    assert calls == ["last"]
    fresh = render_distort(image, QRectF(0, 0, 256, 128), effect,
                           output_bounds=target)
    assert cached == fresh


def test_stroke_prefix_reuse_source_mutation_and_byte_budget(monkeypatch):
    image = source()
    effect = modifier(stroke(), stroke(start=(150, 64), end=(150, 105), identifier="second", radius=12))
    cache = PreparedDistortCache(budget=3*256*128*16)
    calls = []
    original = smudge_rendering._stroke
    def counted(pixels, item, *args):
        calls.append(item["id"])
        return original(pixels, item, *args)
    monkeypatch.setattr(smudge_rendering, "_stroke", counted)
    render(image, effect, preparation_cache=cache)
    assert calls == ["stroke", "second"]
    calls.clear()
    effect.parameters["strokes"][1]["points"][1]["position"][0] += 8
    cached = render(image, effect, preparation_cache=cache)
    assert calls == ["second"]
    assert cache.bytes <= cache.budget
    assert cached == render(image, effect)
    calls.clear()
    image.setPixelColor(55, 64, QColor("white"))
    changed = render(image, effect, preparation_cache=cache)
    assert calls == ["stroke", "second"]
    assert changed == render(image, effect)


def test_longest_checkpoint_survives_when_history_and_source_exceed_budget(monkeypatch):
    image = source()
    effect = modifier(stroke(), stroke(start=(150, 64), end=(150, 105), identifier="second", radius=12),
                      stroke(start=(140, 100), end=(200, 100), identifier="third", radius=8))
    cache = PreparedDistortCache(budget=2*256*128*16)
    render(image, effect, preparation_cache=cache)
    calls = []
    original = smudge_rendering._stroke
    def counted(pixels, item, *args):
        calls.append(item["id"])
        return original(pixels, item, *args)
    monkeypatch.setattr(smudge_rendering, "_stroke", counted)
    effect.parameters["strokes"][2]["points"][1]["position"][0] += 5
    actual = render(image, effect, preparation_cache=cache)
    assert calls == ["third"]
    assert cache.bytes <= cache.budget
    assert actual == render(image, effect)


def test_pressure_response_and_endpoint_radius_affect_smear():
    image = source()
    item = stroke(pressure_enabled=True)
    item["pressure"] = [[0., .1], [.5, .1], [1., .1]]
    low = render(image, modifier(item))
    item["pressure"] = [[0., 1.], [1., 1.]]
    high = render(image, modifier(item))
    assert high.pixelColor(170, 64).red() > low.pixelColor(170, 64).red()
    item["points"][1]["radius"] = 40
    wider = render(image, modifier(item))
    assert wider != high


def test_cubic_bend_and_vector_endpoints_change_smear_path():
    image = source()
    item = stroke(start=(55, 85), end=(205, 85), radius=12)
    item["points"][0].update(point_type="bezier", handle=[55, 5])
    item["points"][1].update(point_type="bezier", handle=[205, 5])
    bent = render(image, modifier(item))
    for point in item["points"]:
        point["point_type"] = "vector"
    straight = render(image, modifier(item))
    assert bent.pixelColor(140, 25).red() > straight.pixelColor(140, 25).red()


def test_cancel_does_not_publish_partial_stroke_checkpoint():
    cache, checks = PreparedDistortCache(), []
    def cancel():
        checks.append(1)
        return len(checks) > 8
    assert render(source(), modifier(stroke()), cancelled=cancel, preparation_cache=cache) is None
    assert len(cache._entries) == 1  # Immutable source only.


def test_active_preview_uses_bounded_draft_without_queuing_native_jobs(monkeypatch):
    from comic_editor.ui.distort_pipeline import render_distort_stage
    from test_mesh_warp_interactive_preview import context
    canvas, _, jobs = context()
    image, effect = source(1024, 1024), modifier(stroke(start=(200, 500), end=(800, 500), radius=32))
    canvas._smudge_preview_id = effect.modifier_id
    bounds = QRectF(0, 0, 1024, 1024)
    output, provisional = render_distort_stage(canvas, image, image, bounds, bounds, effect,
        QTransform(), {}, "revision", "scope", False, False)
    assert provisional and output.size() == image.size() and not jobs
    canvas._projection_exact = True
    exact, provisional = render_distort_stage(canvas, image, image, bounds, bounds, effect,
        QTransform(), {}, "revision", "scope", False, False)
    assert not provisional and not jobs
    assert exact == render(image, effect)


def test_smudge_bounds_include_paint_carried_outside_source():
    image = source()
    item = stroke(start=(230, 64), end=(290, 64), radius=12)
    effect = modifier(item)
    bounds = QRectF(0, 0, 256, 128)
    target = distort_bounds(bounds, effect)
    assert target.right() >= 302
    result = render_distort(image, bounds, effect)
    assert result.pixelColor(275, 64).alpha() > 0
    assert result.pixelColor(275, 5).alpha() == 0


def test_smudge_intensity_mask_leaves_zero_mask_pixels_exact(async_canvas, monkeypatch):
    from comic_editor.ui import distort_pipeline
    canvas = async_canvas
    canvas._projection_defer_effects = False
    image, effect = source(), modifier(stroke())
    bounds = QRectF(0, 0, 256, 128)
    field = np.zeros((128, 256), np.float32)
    field[:, 128:] = 100.
    monkeypatch.setattr(distort_pipeline, "_parameter_field", lambda *_: field)
    actual, provisional = distort_pipeline.render_distort_stage(canvas, image, image,
        bounds, bounds, effect, QTransform(), {}, "mask", "smudge", False, False)
    assert not provisional
    np.testing.assert_array_equal(pixels(actual)[:, :128], pixels(image)[:, :128])
    np.testing.assert_array_equal(pixels(actual)[:, 128:], pixels(render(image, effect))[:, 128:])


def test_exact_smudge_worker_uses_detached_strokes_and_its_own_preparation_cache(async_canvas, monkeypatch):
    from threading import Event, get_ident
    from comic_editor.ui import distort_pipeline, distort_rendering
    from comic_editor.ui.async_projection import ProjectionPending
    canvas = async_canvas
    image, effect = source(), modifier(stroke())
    expected = render(image, effect)
    bounds = QRectF(0, 0, 256, 128)
    canvas._distort_preparation_cache = PreparedDistortCache()
    started, release = Event(), Event()
    calls, main_thread = [], get_ident()
    original = distort_rendering.render_distort
    def compute(*args, **kwargs):
        calls.append((get_ident(), kwargs["preparation_cache"]))
        started.set()
        assert release.wait(5)
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    def stage():
        return distort_pipeline.render_distort_stage(canvas, image, image, bounds, bounds,
            effect, QTransform(), {}, "worker", "smudge", False, False)
    try:
        with pytest.raises(ProjectionPending):
            stage()
        assert started.wait(2)
        effect.parameters["strokes"][0]["points"][1]["position"] = [20, 20]
        image.fill(QColor("black"))
    finally:
        release.set()
    canvas._effect_jobs.running[3].result(timeout=5)
    actual, provisional = stage()
    assert not provisional and actual == expected
    assert calls[0][0] != main_thread
    assert calls[0][1] is not canvas._distort_preparation_cache
