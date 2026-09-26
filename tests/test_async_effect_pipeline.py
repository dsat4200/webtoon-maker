"""Exact deferred effects keep GUI calls short and publish no reduced drafts."""
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import (
    BlurModifier, CageTransformModifier, HalftoneModifier, OutlineModifier,
    PixelateModifier, RadialBlurModifier,
)
from comic_editor.ui.async_projection import ProjectionPending
from comic_editor.ui.effect_pipeline import render_stages
from test_effect_regions import scene, enable, image, register


@pytest.mark.parametrize("factory,module_name,function", [
    (lambda: PixelateModifier(pixel_size=11), "modifier_rendering", "apply_pattern_modifier"),
    (lambda: HalftoneModifier(base_resolution=24, blur=2), "modifier_rendering", "apply_pattern_modifier"),
    (lambda: CageTransformModifier(frame=(0, 0, 180, 160)), "cage_rendering", "warp_image"),
    (lambda: RadialBlurModifier(angle=7, center=(90, 80)), "radial_blur", "radial_blur"),
    (lambda: BlurModifier(strength=4), "interactive_effects", "apply_modifier_stack"),
    (lambda: OutlineModifier(thickness=3, blur_radius=2), "interactive_effects", "apply_modifier_stack"),
])
def test_exact_stage_defers_cpu_kernel_without_draft_and_matches_sync(
    scene, monkeypatch, factory, module_name, function,
):
    import importlib
    modifier = factory()
    register(scene, [modifier])
    source, bounds = image(180, 160), QRectF(0, 0, 180, 160)
    expected = render_stages(scene, source, bounds, [modifier], QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    scene._effect_jobs.cancel()
    enable(scene)
    scene._projection_exact = scene._projection_defer_effects = True
    arguments = dict(source_key=("source",), request_scope=("object", "async", "canvas"),
                     required=expected[1])
    module = importlib.import_module("comic_editor.ui." + module_name)
    original = getattr(module, function)
    gui = get_ident()
    entered, release = Event(), Event()
    calls = []

    def blocked(*args, **kwargs):
        if kwargs.get("allow_cpu_fallback") is False:
            assert get_ident() == gui
            return original(*args, **kwargs)
        calls.append(get_ident())
        assert get_ident() != gui, "An expensive exact kernel ran on the GUI thread"
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, function, blocked)
    try:
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **arguments)
        assert entered.wait(2)
        job = scene._effect_jobs.running
        with pytest.raises(ProjectionPending):
            render_stages(scene, source, bounds, [modifier], QTransform(), **arguments)
        assert scene._effect_jobs.running == job
        assert scene._effect_jobs.submitted == 1 and len(calls) == 1
        assert not any("draft" in str(key[0]) for key in scene._modifier_render_cache)
    finally:
        release.set()
    job[3].result(timeout=10)
    scene._effect_jobs.poll()
    actual = render_stages(scene, source, bounds, [modifier], QTransform(), **arguments)
    assert actual == expected
    assert len(calls) == 1
    assert not scene._effect_jobs.exact_failures


def test_exact_pipeline_waits_before_downstream_stage_and_reuses_completed_prefix(scene, monkeypatch):
    from comic_editor.ui import interactive_effects, modifier_rendering
    modifiers = [PixelateModifier(pixel_size=11), BlurModifier(strength=4)]
    register(scene, modifiers)
    source, bounds = image(180, 160), QRectF(0, 0, 180, 160)
    expected = render_stages(scene, source, bounds, modifiers, QTransform())
    scene._modifier_render_cache.clear()
    scene._modifier_render_cache_bytes = 0
    enable(scene)
    scene._projection_exact = scene._projection_defer_effects = True
    gui, calls = get_ident(), []
    pattern = modifier_rendering.apply_pattern_modifier
    stack = interactive_effects.apply_modifier_stack

    def pattern_work(*args, **kwargs):
        if kwargs.get("allow_cpu_fallback") is False:
            assert get_ident() == gui
            return pattern(*args, **kwargs)
        assert get_ident() != gui
        calls.append("pattern")
        return pattern(*args, **kwargs)

    def blur_work(*args, **kwargs):
        assert get_ident() != gui
        calls.append("blur")
        return stack(*args, **kwargs)

    monkeypatch.setattr(modifier_rendering, "apply_pattern_modifier", pattern_work)
    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", blur_work)
    arguments = dict(source_key=("source",), request_scope=("object", "two-stages", "canvas"),
                     required=expected[1])
    for _ in range(4):
        try:
            actual = render_stages(scene, source, bounds, modifiers, QTransform(), **arguments)
            break
        except ProjectionPending:
            scene._effect_jobs.running[3].result(timeout=10)
            scene._effect_jobs.poll()
    else:
        pytest.fail("Exact dependent stages did not finish")
    assert actual == expected and calls == ["pattern", "blur"]


@pytest.mark.parametrize("kind", ["pattern", "cage"])
def test_deferred_exact_preserves_available_gpu_backend(scene, monkeypatch, kind):
    from comic_editor.ui import cage_rendering, modifier_rendering
    modifier = (PixelateModifier(pixel_size=11) if kind == "pattern"
                else CageTransformModifier(frame=(0, 0, 180, 160)))
    register(scene, [modifier])
    source, bounds = image(180, 160), QRectF(0, 0, 180, 160)
    expected = source.copy()
    expected.fill(0xff123456)
    gui, calls = get_ident(), []

    class GPU:
        def render(self, *_args, **_kwargs):
            assert get_ident() == gui
            calls.append(kind)
            return expected

        cage = render

    path = ("comic_editor.ui.gpu_pattern_effects.renderer_for" if kind == "pattern"
            else "comic_editor.ui.gpu_textures.renderer_for")
    monkeypatch.setattr(path, lambda _: GPU())
    monkeypatch.setattr(cage_rendering, "warp_image",
                        lambda *_a, **_k: pytest.fail("GPU cage was replaced by CPU"))
    enable(scene)
    scene._projection_exact = scene._projection_defer_effects = True
    actual, actual_bounds = render_stages(scene, source, bounds, [modifier], QTransform(),
        source_key=("source",), request_scope=("object", "gpu", "canvas"), required=bounds)
    assert actual == expected and actual_bounds == bounds
    assert calls == [kind] and scene._effect_jobs.submitted == 0
