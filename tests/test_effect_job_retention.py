"""Exact images survive unrelated LRU churn and worker-to-pipeline handoffs."""
from threading import Event

import pytest
from PySide6.QtCore import QObject, Signal
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import HueSaturationLightnessModifier
from comic_editor.ui.effect_jobs import EffectJobs
from comic_editor.ui import interactive_effects
from comic_editor.ui.modifier_rendering import BlurPyramidCache, OutlineDistanceCache


class Owner(QObject):
    visualChanged = Signal(object)

    def __init__(self):
        super().__init__()
        self.results = {}
        self.repaints = 0
        self._interactive_render = True
        self._render_base_alpha = False
        self._rendering_mask_contributor = 0
        self._effect_preview_channel = "canvas"
        self._outline_distance_cache = OutlineDistanceCache()
        self._blur_pyramid_cache = BlurPyramidCache()
        self._effect_jobs = EffectJobs(self)

    def _modifier_cache_get(self, key):
        return self.results.get(key)

    def _modifier_cache_put(self, key, result):
        self.results[key] = QImage(result)

    def _invalidate_scene_cache(self):
        pass

    def update(self):
        self.repaints += 1


@pytest.fixture
def owner(qapp):
    canvas = Owner()
    yield canvas
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def image(color="#917cc0"):
    result = QImage(80, 60, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor(color))
    return result


def test_worker_checkpoint_handoff_preserves_other_completed_target(owner):
    jobs = owner._effect_jobs
    first, second = image(), image("#72aa38")
    jobs.retained_budget = first.sizeInBytes() + second.sizeInBytes()
    jobs.retained_put(("pipeline", "a"), "final-a", first)
    jobs.retained_put(("result", "b"), "stage-b", second)
    completed = jobs.result("b", "stage-b")
    # The pipeline takes ownership before releasing its worker-result alias.
    jobs.retained_put(("pipeline", "b"), "final-b", completed)
    jobs.retained_remove(("result", "b"), "stage-b")
    assert jobs.retained_get(("pipeline", "a"), "final-a")[0] == first
    assert jobs.retained_get(("pipeline", "b"), "final-b")[0] == second
    assert jobs.retained_bytes == first.sizeInBytes() + second.sizeInBytes()


def test_shared_storage_is_released_only_after_last_scope_and_read_is_detached(owner):
    jobs = owner._effect_jobs
    source = image()
    original = QImage(source)
    jobs.retained_put("a", "key", source)
    jobs.retained_put("b", "key", QImage(source))
    assert jobs.retained_bytes == source.sizeInBytes()
    detached = jobs.retained_get("a", "key")[0]
    source.fill(QColor("red"))
    detached.fill(QColor("blue"))
    assert jobs.retained_get("b", "key")[0] == original
    jobs.retained_remove("a")
    assert jobs.retained_bytes == original.sizeInBytes()
    jobs.retained_remove("b")
    assert not jobs.retained_bytes
    assert not jobs._retained_images


def test_many_alias_scopes_remain_entry_bounded(owner):
    jobs = owner._effect_jobs
    jobs.retained_limit = 3
    source = image()
    for index in range(20):
        jobs.retained_put(index, "same-storage", source)
    assert len(jobs.retained) == 3
    assert jobs.retained_get(16, "same-storage") is None
    assert jobs.retained_get(19, "same-storage")[0] == source
    assert jobs.retained_bytes == source.sizeInBytes()
    jobs.cancel()
    assert not jobs.retained_bytes and not jobs._retained_images


def test_completed_worker_is_exact_before_poll_timer_fires(owner):
    jobs = owner._effect_jobs
    exact = image()
    jobs.request("reference", "version", lambda cancelled: exact, exact.sizeInBytes())
    jobs.running[3].result(timeout=2)
    assert jobs.completed == 0
    assert jobs.result("reference", "version") == exact
    assert jobs.completed == 1 and jobs.submitted == 1
    assert owner.repaints == 1
    assert jobs.result("reference", "version") == exact
    assert owner.repaints == 1


def test_canceled_completed_worker_is_never_adopted_by_early_lookup(owner):
    jobs = owner._effect_jobs
    exact = image()
    jobs.request("reference", "old", lambda cancelled: exact, exact.sizeInBytes())
    jobs.running[3].result(timeout=2)
    jobs.cancel()
    assert jobs.result("reference", "old") is None
    assert jobs.completed == 0 and jobs.discarded == 1
    assert not owner.results


@pytest.mark.parametrize("new_size", [80, 300], ids=["capacity-pressure", "oversized"])
def test_deferred_request_preserves_unrelated_pending_work(owner, new_size):
    jobs = owner._effect_jobs
    jobs.budget = 200
    gate = Event()
    try:
        jobs.request("running", "a", lambda cancelled: (gate.wait(2), image())[1], 160)
        jobs.request("pending", "b", lambda cancelled: image("green"), 20)
        assert jobs.request("large", "c", lambda cancelled: image("blue"), new_size, allow_oversized=True)
        assert "pending" in jobs.pending
        assert jobs.bytes_in_flight == 180
        assert jobs.retry_on_release
        gate.set()
        jobs.running[3].result(timeout=2)
        jobs.poll()
        jobs.running[3].result(timeout=2)
        jobs.poll()
        assert jobs.result("pending", "b") == image("green")
        assert jobs.submitted == 2
    finally:
        gate.set()


@pytest.mark.parametrize("cached", [False, True], ids=["synchronous", "cache-hit"])
def test_interactive_exact_stack_is_retained_after_normal_cache_churn(owner, monkeypatch, cached):
    source = image()
    modifiers = [HueSaturationLightnessModifier(hue=35)]
    expected = interactive_effects.apply_modifier_stack(source, modifiers, (0, 0))
    if cached:
        owner._modifier_cache_put("stack-key", expected)
    result, provisional = interactive_effects.render_interactive_stack(
        owner, source, modifiers, (0, 0), cache_key="stack-key", scope="reference")
    assert result == expected and not provisional
    owner.results.clear()

    def unexpected_render(*args, **kwargs):
        pytest.fail("An unchanged exact stack was recomputed")

    monkeypatch.setattr(interactive_effects, "apply_modifier_stack", unexpected_render)
    result, provisional = interactive_effects.render_interactive_stack(
        owner, source, modifiers, (0, 0), cache_key="stack-key", scope="reference")
    assert result == expected and not provisional
    assert not owner._effect_jobs.submitted


@pytest.mark.parametrize("context", ["export", "navigator", "base-alpha", "mask", "provisional"])
def test_capture_and_provisional_results_do_not_replace_retained_canvas_result(owner, context):
    source = image()
    owner._effect_jobs.retained_put(("result", "reference"), "visible", source)
    if context == "export":
        owner._interactive_render = False
    elif context == "navigator":
        owner._effect_preview_channel = "navigator"
    elif context == "base-alpha":
        owner._render_base_alpha = True
    elif context == "mask":
        owner._rendering_mask_contributor = 1
    interactive_effects.render_interactive_stack(owner, source,
        [HueSaturationLightnessModifier(hue=35)], (0, 0), cache_key="capture",
        scope="reference", upstream_provisional=context == "provisional")
    assert owner._effect_jobs.result("reference", "visible") == source
    assert owner._effect_jobs.result("reference", "capture") is None
