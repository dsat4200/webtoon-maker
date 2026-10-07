"""Deferred exact captures wait for complete input instead of filtering drafts."""
import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QImage, QTransform

from comic_editor.core.models import ParameterMaskBinding, ToneMask
from comic_editor.ui import distort_pipeline, distort_rendering, effect_pipeline
from comic_editor.ui.async_projection import ProjectionPending
from test_effect_regions import scene, image, register
from test_distort_rendering import modifier


def enable_deferred(scene):
    scene._interactive_render = True
    scene._projection_exact = True
    scene._projection_defer_effects = True


def test_provisional_exact_input_yields_before_planning_or_cache_admission(scene, monkeypatch):
    enable_deferred(scene)
    source = image(200, 160)
    effect = modifier("twirl", angle=85)
    monkeypatch.setattr(effect_pipeline, "_stage_plan",
                        lambda *_: pytest.fail("Provisional exact input entered planning"))
    with pytest.raises(ProjectionPending):
        effect_pipeline.render_stages(scene, source, QRectF(0, 0, 200, 160), [effect],
            QTransform(), provisional=True, source_key=("provisional-test",),
            request_scope=("object", "target", "canvas"))
    assert scene._effect_jobs.submitted == 0
    assert not scene._effect_jobs.retained and not scene._modifier_render_cache


def test_direct_distort_provisional_exact_input_never_runs_native_kernel(scene, monkeypatch):
    enable_deferred(scene)
    source, bounds = image(200, 160), QRectF(0, 0, 200, 160)
    effect = modifier("twirl", angle=85)
    monkeypatch.setattr(distort_rendering, "render_distort",
                        lambda *_a, **_k: pytest.fail("Native draft filtering ran on the caller"))
    monkeypatch.setattr(distort_pipeline, "_parameter_field",
                        lambda *_: pytest.fail("Provisional fields were recopied"))
    with pytest.raises(ProjectionPending) as pending:
        distort_pipeline.render_distort_stage(scene, source, source, bounds, bounds, effect,
            QTransform(), {}, ("exact-key",), ("object", "target"), True, False)
    assert pending.value.scope == ("object", "target")
    assert pending.value.key == ("exact-key",)
    assert scene._effect_jobs.submitted == 0 and not scene._modifier_render_cache


@pytest.mark.parametrize("provisional_field", [False, True])
def test_pending_mask_contributor_retries_to_identical_native_pixels(scene, monkeypatch, provisional_field):
    source, bounds = image(200, 160), QRectF(0, 0, 200, 160)
    effect = modifier("twirl", angle=85)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    effect.parameter_masks["intensity"] = ParameterMaskBinding(
        mask_id=mask.mask_id, black_value=15, white_value=85)
    register(scene, [effect])
    field = np.linspace(0., 1., 200 * 160, dtype=np.float32).reshape(160, 200)
    monkeypatch.setattr(scene, "render_tone_mask_field", lambda *_a, **_k: field)
    args = dict(source_key=("masked-source",), request_scope=("object", "target", "canvas"),
                tile_evaluation=False)
    # The unchanged synchronous native path is the reference.
    expected, expected_bounds = effect_pipeline.render_stages(
        scene, source, bounds, [effect], QTransform(), **args)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._effect_jobs.cancel()
    enable_deferred(scene)
    ready, mask_calls = False, []
    def contributor(*_a, **_k):
        mask_calls.append(ready)
        if not ready:
            if not provisional_field:
                raise ProjectionPending(("mask-contributor",), ("current-mask",))
            scene._effect_provisional_revision = getattr(scene, '_effect_provisional_revision', 0) + 1
        return field
    monkeypatch.setattr(scene, "render_tone_mask_field", contributor)
    with pytest.raises(ProjectionPending):
        effect_pipeline.render_stages(scene, source, bounds, [effect], QTransform(), **args)
    assert scene._effect_jobs.submitted == 0
    assert not any(key[0] == "stage" for key in scene._modifier_render_cache)
    assert not any(scope[0] == "result" for scope in scene._effect_jobs.retained)
    ready = True
    with pytest.raises(ProjectionPending):
        effect_pipeline.render_stages(scene, source, bounds, [effect], QTransform(), **args)
    assert scene._effect_jobs.submitted == 1
    scene._effect_jobs.running[3].result(timeout=5)
    actual, actual_bounds = effect_pipeline.render_stages(
        scene, source, bounds, [effect], QTransform(), **args)
    assert actual_bounds == expected_bounds
    assert bytes(actual.constBits()) == bytes(expected.constBits())
    assert scene._effect_jobs.completed == 1
    assert mask_calls == [False, True]
    assert scene._projection_exact and scene._projection_defer_effects
    assert not scene._render_modifier_sources and not scene._rendering_mask_contributor


@pytest.mark.parametrize("field,value", [("_projection_defer_effects", False),
    ("_interactive_render", False)])
def test_explicit_synchronous_paths_keep_native_compute(scene, monkeypatch, field, value):
    enable_deferred(scene)
    setattr(scene, field, value)
    source, bounds = image(200, 160), QRectF(0, 0, 200, 160)
    scales = []
    def compute(*_a, **kwargs):
        scales.append(kwargs["pixel_scale"])
        return QImage(source)
    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    result, provisional = distort_pipeline.render_distort_stage(scene, source, source,
        bounds, bounds, modifier("twirl", angle=85), QTransform(), {},
        ("source",), ("object", "target"), True, False)
    assert result == source and provisional
    assert scales == [1.] and scene._effect_jobs.submitted == 0


def test_interactive_provisional_preview_still_runs_bounded_actual_effect(scene, monkeypatch):
    enable_deferred(scene)
    scene._projection_exact = False
    source, bounds = image(400, 320), QRectF(0, 0, 400, 320)
    scales = []
    original = distort_rendering.render_distort
    def compute(*a, **kw):
        scales.append(kw["pixel_scale"])
        return original(*a, **kw)
    monkeypatch.setattr(distort_rendering, "render_distort", compute)
    result, provisional = distort_pipeline.render_distort_stage(scene, source, source,
        bounds, bounds, modifier("twirl", angle=85), QTransform(), {},
        ("live-source",), ("object", "target"), True, False)
    assert not result.isNull() and provisional
    assert len(scales) == 1 and 0 < scales[0] < 1
    assert scene._effect_jobs.submitted == 0
    assert all(key[0] == "distort-draft" for key in scene._modifier_render_cache)
