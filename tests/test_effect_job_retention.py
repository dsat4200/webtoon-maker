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


def test_cancel_pending_work_can_preserve_completed_exact_results(owner):
    jobs = owner._effect_jobs
    source = image()
    jobs.retained_put(("pipeline", "background"), "unchanged", source)
    started, release = Event(), Event()

    def compute(cancelled):
        started.set()
        assert release.wait(5)
        return image("red")

    jobs.request("active", "old-state", compute, source.sizeInBytes())
    try:
        assert started.wait(1)
        jobs.request("queued", "old-state", compute, source.sizeInBytes())
        jobs.cancel(clear_retained=False)
        assert not jobs.pending
        assert jobs.running[2].is_set()
        assert jobs.retained_get(("pipeline", "background"), "unchanged")[0] == source
    finally:
        release.set()
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.discarded == 1
    assert jobs.retained_get(("pipeline", "background"), "unchanged")[0] == source
    jobs.cancel()
    assert not jobs.retained and jobs.retained_bytes == 0


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


def test_cancel_exact_preserves_nonexact_jobs_and_completed_checkpoints(owner):
    jobs = owner._effect_jobs
    source = image()
    jobs.retained_put(('pipeline', 'stable'), 'prefix', source, shared=True)
    started, release = Event(), Event()
    def gated(cancelled):
        started.set()
        assert release.wait(5)
        return image('red')
    jobs.request('old-exact', 'old-key', gated, 100, require_exact=True)
    try:
        assert started.wait(1)
        jobs.request('live-preview', 'preview-key', lambda _: image('blue'), 100)
        jobs.request('queued-exact', 'queued-key', lambda _: image('red'), 100, require_exact=True)
        jobs.waiting['memory-wait'] = 'old-key'
        jobs.exact_failures['old-failure'] = ('old-key', 'obsolete')
        assert jobs.cancel_exact()
        assert jobs.running[2].is_set()
        assert list(jobs.pending) == ['live-preview']
        assert not jobs.waiting and not jobs.exact_failures
        assert jobs.retained_get(('pipeline', 'stable'), 'prefix')[0] == source
    finally:
        release.set()
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.discarded == 1 and 'old-key' not in owner.results
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.completed == 1
    assert jobs.result('live-preview', 'preview-key') == image('blue')
    assert jobs.retained_get(('pipeline', 'stable'), 'prefix')[0] == source


def test_cancel_exact_marks_finished_futures_before_they_can_publish(owner):
    jobs = owner._effect_jobs
    jobs.request('old-exact', 'old-key', lambda _: image('red'), 100, require_exact=True)
    jobs.running[3].result(timeout=5)
    assert jobs.has_finished
    assert jobs.cancel_exact()
    jobs.poll()
    assert jobs.discarded == 1 and jobs.completed == 0
    assert not owner.results and not jobs.retained and not jobs.exact_failures


def test_cancel_exact_keeps_running_preview_and_its_memory_retry(owner):
    jobs = owner._effect_jobs
    jobs.budget = 100
    started, release = Event(), Event()
    def preview(cancelled):
        started.set()
        assert release.wait(5)
        return image('blue')
    jobs.request('live-preview', 'preview-key', preview, 80)
    try:
        assert started.wait(1)
        jobs.request('waiting-exact', 'old-key', lambda _: image('red'), 80, require_exact=True)
        assert jobs.waiting and jobs.retry_on_release
        assert jobs.cancel_exact()
        assert not jobs.running[2].is_set()
        assert not jobs.waiting and jobs.retry_on_release
    finally:
        release.set()
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.completed == 1 and not jobs.discarded
    assert jobs.result('live-preview', 'preview-key') == image('blue')


def test_live_exact_cancellation_preserves_immutable_source_decode_then_full_cancel_retires_it(owner):
    jobs = owner._effect_jobs
    started, release = Event(), Event()
    scope = ('source-image-decode', 'image', 'legacy')
    queued = ('source-image-decode', 'other-image', 'native')
    failure = ('source-image-decode', 'missing-image', 'legacy')
    def decode(cancelled):
        started.set()
        assert release.wait(5)
        return image('blue')
    jobs.request(scope, 'immutable-bytes', decode, 100, require_exact=True)
    try:
        assert started.wait(1)
        jobs.request(queued, 'other-immutable-bytes', decode, 100, require_exact=True)
        jobs.waiting[('source-image-decode', 'waiting', 'legacy')] = 'waiting-bytes'
        jobs.exact_failures[failure] = ('missing-bytes', 'Source could not be read')
        jobs.request('obsolete-artwork', 'gesture-pose', lambda _: image('red'), 100, require_exact=True)
        jobs.retry_on_release = True
        assert jobs.cancel_exact()
        assert not jobs.running[2].is_set()
        assert list(jobs.pending) == [queued]
        assert jobs.waiting and failure in jobs.exact_failures and jobs.retry_on_release
        jobs.cancel(clear_retained=False)
        assert jobs.running[2].is_set()
        assert not jobs.pending and not jobs.waiting and not jobs.exact_failures
    finally:
        release.set()
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert not jobs.completed and not owner.results


def test_finished_source_decode_publishes_across_live_cancellation(owner):
    jobs = owner._effect_jobs
    scope = ('source-image-decode', 'image', 'legacy')
    jobs.request(scope, 'immutable-bytes', lambda _: image('blue'), 100, require_exact=True)
    jobs.running[3].result(timeout=5)
    assert not jobs.cancel_exact()
    jobs.poll()
    assert jobs.completed == 1 and not jobs.discarded
    assert jobs.result(scope, 'immutable-bytes') == image('blue')


def test_memory_waiting_source_decode_keeps_retry_when_obsolete_artwork_unwinds(owner):
    jobs = owner._effect_jobs
    jobs.budget = 100
    started, release = Event(), Event()
    scope = ('source-image-decode', 'image', 'legacy')
    def old_artwork(cancelled):
        started.set()
        assert release.wait(5)
        return image('red')
    jobs.request('obsolete-artwork', 'old-pose', old_artwork, 80, require_exact=True)
    try:
        assert started.wait(1)
        jobs.request(scope, 'immutable-bytes', lambda _: image('blue'), 80, require_exact=True)
        assert jobs.waiting[scope] == 'immutable-bytes' and jobs.retry_on_release
        assert jobs.cancel_exact()
        assert jobs.running[2].is_set()
        assert jobs.waiting[scope] == 'immutable-bytes' and jobs.retry_on_release
    finally:
        release.set()
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.discarded == 1 and not jobs.waiting
    assert owner.repaints
    jobs.request(scope, 'immutable-bytes', lambda _: image('blue'), 80, require_exact=True)
    jobs.running[3].result(timeout=5)
    jobs.poll()
    assert jobs.result(scope, 'immutable-bytes') == image('blue')


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


def test_shared_prefixes_survive_regional_churn_within_existing_budget(owner):
    jobs = owner._effect_jobs
    prefix = image()
    jobs.retained_budget = prefix.sizeInBytes() * 4
    jobs.retained_put("prefix", "stable", prefix, shared=True)
    jobs.retained_put("checkpoint-alias", "same", prefix)
    for index in range(30):
        jobs.retained_put(("region", index), index, image("red"))
        assert jobs.retained_bytes <= jobs.retained_budget
    assert jobs.retained_get("prefix", "stable")[0] == prefix
    assert jobs.retained_get("checkpoint-alias", "same") is None
    assert jobs.retained_shared_bytes == prefix.sizeInBytes()
    jobs.retained_put("prefix", "changed", image("blue"), shared=True)
    assert jobs.retained_get("prefix", "stable") is None
    assert jobs.retained_get("prefix", "changed")[0] == image("blue")
    assert jobs.retained_shared_bytes == prefix.sizeInBytes()
    jobs.cancel(clear_retained=False)
    assert jobs.retained_shared_bytes == prefix.sizeInBytes()
    jobs.cancel()
    assert not jobs._retained_shared and not jobs._retained_shared_images
    assert jobs.retained_shared_bytes == 0


def test_shared_partition_has_its_own_lru_with_half_byte_and_entry_limits(owner):
    jobs = owner._effect_jobs
    source = image()
    jobs.retained_budget = source.sizeInBytes() * 4
    jobs.retained_limit = 6
    for key, color in (("a", "red"), ("b", "blue")):
        jobs.retained_put(key, key, image(color), shared=True)
    jobs.retained_get("a", "a")
    jobs.retained_put("c", "c", image("green"), shared=True)
    assert set(jobs._retained_shared) == {"a", "c"}
    for index in range(10):
        jobs.retained_put(index, index, image())
    assert jobs.retained_get("a", "a") is not None
    assert jobs.retained_get("c", "c") is not None
    assert jobs.retained_get("b", "b") is None
    assert jobs.retained_shared_bytes == jobs.retained_budget // 2
    jobs.cancel()
    for index in range(20):
        jobs.retained_put(index, index, source, shared=True)
        assert len(jobs._retained_shared) <= jobs.retained_limit // 2
        assert len(jobs.retained) <= jobs.retained_limit
    assert jobs.retained_shared_bytes == source.sizeInBytes()
    for scope in tuple(jobs._retained_shared):
        jobs.retained_remove(scope)
    assert jobs.retained_shared_bytes == 0 and not jobs._retained_shared_images


def test_oversized_capture_cannot_flush_shared_prefix_but_can_stand_alone(owner):
    jobs = owner._effect_jobs
    prefix = image()
    jobs.retained_budget = prefix.sizeInBytes() * 2
    jobs.retained_put("prefix", "exact", prefix, shared=True)
    oversized = QImage(300, 200, QImage.Format_ARGB32_Premultiplied)
    oversized.fill(QColor("green"))
    assert jobs.retained_put("capture", "large", oversized) is False
    assert jobs.retained_get("prefix", "exact")[0] == prefix
    assert jobs.retained_bytes == prefix.sizeInBytes()
    jobs.retained_remove("prefix")
    assert jobs.retained_put("capture", "large", oversized)
    assert jobs.retained_get("capture", "large")[0] == oversized
    assert jobs.retained_bytes == oversized.sizeInBytes()


def test_exact_worker_oversized_result_is_available_for_gui_handoff(owner):
    jobs = owner._effect_jobs
    prefix = image()
    jobs.retained_budget = prefix.sizeInBytes() * 2
    jobs.retained_put("prefix", "exact", prefix, shared=True)
    result = QImage(300, 200, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor("green"))
    jobs.request("large", "key", lambda _: result, result.sizeInBytes(),
                 require_exact=True, allow_oversized=True)
    jobs.running[3].result(timeout=2)
    jobs.poll()
    owner.results.clear()  # The ordinary LRU cannot be the only handoff.
    assert jobs.result("large", "key") == result
    assert jobs.retained_bytes == result.sizeInBytes()


@pytest.mark.parametrize("failure", ["exception", "none", "empty"])
def test_exact_worker_failure_is_terminal_until_inputs_change(owner, failure):
    from comic_editor.ui.async_projection import ProjectionFailed
    jobs = owner._effect_jobs

    def fail(_):
        if failure == "exception":
            raise ValueError("Synthetic bad effect")
        return None if failure == "none" else QImage()

    jobs.request("exact", "old", fail, 100, require_exact=True)
    try:
        jobs.running[3].result(timeout=2)
    except ValueError:
        pass
    jobs.poll()
    assert jobs.exact_failures
    with pytest.raises(ProjectionFailed):
        jobs.result("exact", "old")
    with pytest.raises(ProjectionFailed):
        jobs.request("exact", "old", fail, 100, require_exact=True)
    assert jobs.submitted == 1
    jobs.request("exact", "new", lambda _: image(), 100, require_exact=True)
    jobs.running[3].result(timeout=2)
    assert jobs.result("exact", "new") == image()
    assert not jobs.exact_failures


def test_exact_memory_wait_does_not_retain_another_input_snapshot(owner):
    from comic_editor.ui.async_projection import ProjectionPending, projection_result_or_pending
    jobs = owner._effect_jobs
    jobs.budget = 100
    started, release = Event(), Event()
    try:
        jobs.request("first", "a", lambda _: (started.set(), release.wait(5), image())[2],
                     80, require_exact=True)
        assert started.wait(2)
        jobs.request("next", "b", lambda _: image(), 80, require_exact=True)
        assert not jobs.pending and jobs.waiting == {"next": "b"}
        assert jobs.bytes_in_flight == 80
        with pytest.raises(ProjectionPending):
            projection_result_or_pending(owner, "next", "b")
    finally:
        release.set()
    jobs.running[3].result(timeout=2)
    jobs.poll()
    assert not jobs.waiting and not jobs.retry_on_release


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


@pytest.mark.parametrize("result", ["image", "none"])
def test_canceled_exact_completion_releases_projection_wait_without_failure(owner, result):
    jobs = owner._effect_jobs
    notifications = []
    owner._effect_result_ready = lambda scope, key: notifications.append((scope, key))
    jobs.request("reference", "old", lambda _: image() if result == "image" else None,
                 100, require_exact=True)
    jobs.running[3].result(timeout=2)
    jobs.cancel(clear_retained=False)
    jobs.poll()
    assert notifications == [(None, None)]
    assert owner.repaints == 1
    assert jobs.result("reference", "old") is None
    assert jobs.completed == 0 and jobs.discarded == 1
    assert not jobs.exact_failures and not owner.results
    assert jobs.running is None and not jobs.pending


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
