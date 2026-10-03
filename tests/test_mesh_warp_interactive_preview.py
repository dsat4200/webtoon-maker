"""Mesh handles use transient bounded pixels and finish at exact quality."""
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.ui import distort_pipeline, distort_rendering
from test_distort_rendering import modifier, source_image


def context():
    results, jobs = {}, []
    canvas = SimpleNamespace(
        _projection_exact=False, _interactive_render=True, _render_base_alpha=False,
        _rendering_mask_contributor=0, _effect_preview_channel="canvas",
        _modifier_cache_get=results.get, _modifier_cache_put=results.__setitem__,
        _effect_jobs=SimpleNamespace(request=lambda *args, **kwargs: jobs.append(args) or True),
    )
    return canvas, results, jobs


def test_pen_contact_bounds_warp_work_and_waits_until_release_to_queue_exact(monkeypatch):
    canvas, results, jobs = context()
    canvas._stroke_projection_active = True
    effect = modifier('mesh_warp')
    effect.points[5] = (.61, .41)
    image = source_image().scaled(640, 480)
    bounds = QRectF(0, 0, 640, 480)
    calls = []
    original = distort_rendering.render_distort

    def track(*args, **kwargs):
        result = original(*args, **kwargs)
        calls.append((kwargs['pixel_scale'], result.size().toTuple()))
        return result

    monkeypatch.setattr(distort_rendering, 'render_distort', track)

    def render():
        return distort_pipeline.render_distort_stage(canvas, image, image, bounds, bounds,
            effect, QTransform(), {}, 'pixels', 'scope', False, False)

    draft, provisional = render()
    assert provisional and not jobs
    assert len(calls) == 1 and max(calls[0][1]) <= 96
    assert calls[0][1][0] * calls[0][1][1] <= 4096 + 192
    assert render() == (draft, True) and len(calls) == 1
    assert set(results) == {('contact-distort-draft', 'pixels')}
    canvas._stroke_projection_active = False
    render()
    assert len(jobs) == 1
    expected = original(image, bounds, effect, QTransform(), bounds)
    assert jobs[0][2]() == expected
    canvas._projection_exact = True
    exact, provisional = render()
    assert exact == expected and not provisional


@pytest.mark.parametrize("masked", [False, True])
def test_mesh_preview_matches_existing_draft_blending_without_native_jobs(monkeypatch, masked):
    canvas, results, jobs = context()
    mesh = modifier("mesh_warp")
    mesh.points[5] = (.61, .41)
    source, base = source_image().scaled(640, 480), source_image().scaled(331, 217)
    bounds, target = QRectF(-17, 21, 640, 480), QRectF(31, 43, 331, 217)
    mapping = QTransform.fromTranslate(8, -4)
    field = (np.linspace(10, 85, 331 * 217, dtype=np.float32).reshape(217, 331)
             if masked else np.float32(63))
    monkeypatch.setattr(distort_pipeline, "_parameter_field", lambda *_: field)

    def render():
        return distort_pipeline.render_distort_stage(canvas, source, base, bounds, target,
            mesh, mapping, {}, "revision", "scope", False, False)

    expected, provisional = render()
    assert provisional and len(jobs) == 1
    jobs.clear()
    results.clear()
    canvas._mesh_warp_preview_id = mesh.modifier_id
    calls = []
    original = distort_rendering.render_distort
    def track(*args, **kwargs):
        calls.append(kwargs["pixel_scale"])
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, "render_distort", track)
    actual, provisional = render()
    assert actual == expected and provisional
    assert not jobs and len(calls) == 1 and calls[0] < 1
    assert render() == (actual, True) and len(calls) == 1
    assert set(results) == {("distort-draft", "revision")}

    # Even with the same key and live marker, exact rendering cannot consume
    # the temporary preview. Its source, bounds, masks and placement are intact.
    canvas._projection_exact = True
    exact, provisional = render()
    assert not provisional and calls[-1] == 1.0 and not jobs
    canvas._mesh_warp_preview_id = None
    assert render() == (exact, False)


@pytest.mark.parametrize("attribute,value", [
    ("_interactive_render", False), ("_render_base_alpha", True),
    ("_rendering_mask_contributor", 1), ("_projection_exact", True),
])
def test_mesh_preview_marker_does_not_change_export_or_mask_capture(monkeypatch, attribute, value):
    canvas, _, jobs = context()
    mesh = modifier("mesh_warp")
    canvas._mesh_warp_preview_id = mesh.modifier_id
    setattr(canvas, attribute, value)
    source = source_image().scaled(192, 144)
    scales = []
    original = distort_rendering.render_distort
    def track(*args, **kwargs):
        scales.append(kwargs["pixel_scale"])
        return original(*args, **kwargs)
    monkeypatch.setattr(distort_rendering, "render_distort", track)
    _, provisional = distort_pipeline.render_distort_stage(canvas, source, source,
        QRectF(0, 0, 192, 144), QRectF(0, 0, 192, 144), mesh, QTransform(), {},
        "key", "scope", False, False)
    assert not provisional and not jobs and scales == [1.0]
