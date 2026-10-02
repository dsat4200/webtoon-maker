"""Radial integration is independent of the final intensity-mask mix."""
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import ParameterMaskBinding, RadialBlurModifier, ToneMask
from comic_editor.ui.async_projection import ProjectionPending
from comic_editor.ui.effect_pipeline import render_stages
from comic_editor.ui.modifier_rendering import _premultiplied_qimage, _qimage_premultiplied
from comic_editor.ui.radial_pipeline import render_radial_stage
from test_effect_regions import scene, image, register, enable


def test_cached_integration_keeps_original_single_rounding_and_mask_extremes(scene, monkeypatch):
    from comic_editor.ui import radial_blur
    modifier = RadialBlurModifier(center=(31, 27), angle=24)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    source = image(67, 55)
    pixels = _qimage_premultiplied(source)
    angular = radial_blur.radial_blur(pixels, modifier.center, modifier.angle, QTransform())
    calls = []
    original = radial_blur.radial_blur
    monkeypatch.setattr(radial_blur, "radial_blur", lambda *a, **k: calls.append(1) or original(*a, **k))
    for end in (.8, .6, .9):
        field = np.broadcast_to(np.linspace(0, 1/end, 67, dtype=np.float32).clip(0, 1), (55, 67))
        fields = {(modifier.modifier_id, "intensity"): field}
        mask.touch()
        actual, provisional = render_radial_stage(scene, source, source, modifier, fields,
            QTransform(), (0., 0.), source_key=("source",), scope=("radial",), asynchronous=False,
            deferred=False, provisional=False, navigator=False, exact=True)
        expected = _premultiplied_qimage(pixels*(1-field[..., None])+angular*field[..., None])
        assert actual == expected and not provisional
    assert len(calls) == 1


def test_pending_integration_survives_intensity_gradient_and_binding_edits(scene, monkeypatch):
    from comic_editor.ui import radial_blur
    modifier = RadialBlurModifier(center=(90, 80), angle=7)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["intensity"] = ParameterMaskBinding(mask.mask_id, 0, 100)
    register(scene, [modifier])
    # Real gradient creation and sampling, rather than a mocked mask field.
    scene.set_tone_mask_mode(mask.mask_id)
    from PySide6.QtCore import QPointF
    scene._mask_gradient_press(QPointF(20, 80))
    scene._mask_gradient_move(QPointF(160, 80))
    scene._finish_mask_gradient()
    enable(scene)
    scene._projection_exact = scene._projection_defer_effects = True
    source, bounds = image(180, 160), QRectF(0, 0, 180, 160)
    args = dict(source_key=("source",), request_scope=("object", "radial", "canvas"))
    entered, release = Event(), Event()
    original = radial_blur.radial_blur
    def blocked(*a, **k):
        entered.set()
        assert release.wait(5)
        return original(*a, **k)
    monkeypatch.setattr(radial_blur, "radial_blur", blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **args)
        assert entered.wait(2)
        job = scene._effect_jobs.running
        mask.gradient.line_field.geometry.nodes[-1].position = (120, 80)
        mask.gradient.touch_revision()
        mask.touch()
        modifier.parameter_masks["intensity"].white_value = 80
        scene.documentChanged.emit(None)
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **args)
        assert scene._effect_jobs.running == job and not job[2].is_set()
        assert scene._effect_jobs.submitted == 1
    finally:
        release.set()
    job[3].result(timeout=10)
    scene._effect_jobs.poll()
    latest = render_stages(scene, source, bounds, [modifier], QTransform(), **args)
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._effect_jobs.cancel()
    scene._projection_defer_effects = False
    monkeypatch.setattr(radial_blur, "radial_blur", original)
    assert render_stages(scene, source, bounds, [modifier], QTransform(), **args) == latest


def test_changed_angle_cancels_old_integration_even_when_output_region_changes(scene, monkeypatch):
    from comic_editor.ui import radial_blur
    modifier = RadialBlurModifier(center=(90, 80), angle=7)
    register(scene, [modifier])
    enable(scene)
    scene._projection_exact = scene._projection_defer_effects = True
    source, bounds = image(180, 160), QRectF(0, 0, 180, 160)
    args = dict(source_key=("source",), request_scope=("object", "radial", "canvas"))
    entered, release = Event(), Event()
    original = radial_blur.radial_blur
    def blocked(*a, **k):
        entered.set()
        assert release.wait(5)
        return original(*a, **k)
    monkeypatch.setattr(radial_blur, "radial_blur", blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **args)
        assert entered.wait(2)
        job = scene._effect_jobs.running
        modifier.angle = 24
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **args)
        assert job[2].is_set(), "Obsolete angular samples kept a newer regional request waiting"
        assert scene._effect_jobs.pending
        pending = next(iter(scene._effect_jobs.pending.values()))
        assert pending[0] != job[0]
    finally:
        release.set()
    with pytest.raises(radial_blur.RadialRenderCancelled):
        job[3].result(timeout=10)
    scene._effect_jobs.poll()
    scene._effect_jobs.running[3].result(timeout=10)
    scene._effect_jobs.poll()
    assert render_stages(scene, source, bounds, [modifier], QTransform(), **args)[0] is not None


@pytest.mark.parametrize("change", ["angle", "angle_mask", "source", "mapping", "center", "origin"])
def test_integration_invalidates_for_every_sampling_dependency(scene, monkeypatch, change):
    from comic_editor.ui import radial_blur
    modifier = RadialBlurModifier(center=(31, 27), angle=7)
    source = image(67, 55)
    mask = ToneMask()
    scene.chapter.masks[mask.mask_id] = mask
    modifier.parameter_masks["angle"] = ParameterMaskBinding(mask.mask_id, 0, 10)
    fields = {(modifier.modifier_id, "angle"): np.full((55, 67), .7, np.float32)}
    mapping, origin, source_key = QTransform(), (0., 0.), ("source",)
    calls = []
    original = radial_blur.radial_blur
    monkeypatch.setattr(radial_blur, "radial_blur", lambda *a, **k: calls.append(1) or original(*a, **k))
    def render():
        return render_radial_stage(scene, source, source, modifier, fields, mapping, origin,
            source_key=source_key, scope=("radial",), asynchronous=False, deferred=False,
            provisional=False, navigator=False, exact=True)
    render()
    render()
    assert len(calls) == 1
    if change == "angle":
        modifier.parameter_masks["angle"].white_value = 20
    elif change == "angle_mask":
        mask.touch()
        fields[(modifier.modifier_id, "angle")] *= .5
    elif change == "source":
        source_key = ("changed-source",)
    elif change == "mapping":
        mapping.translate(7, 4)
    elif change == "center":
        modifier.center = (20, 25)
    else:
        origin = (3., 2.)
    render()
    assert len(calls) == 2
