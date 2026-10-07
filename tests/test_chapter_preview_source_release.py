"""A real source-capacity release wakes one private band without polling fast."""
from threading import Event

import pytest
from PySide6.QtTest import QTest
from PySide6.QtGui import QImage

from test_chapter_preview_source_decode import source_navigator, blocked_native_decoder
from test_deferred_source_images import canvas, cold_source, finish
from test_chapter_preview_derived import fresh, settle


def observe_renders(canvas, monkeypatch):
    calls, original = [], canvas.render_preview
    def render(image, clip):
        calls.append(clip)
        return original(image, clip)
    monkeypatch.setattr(canvas, 'render_preview', render)
    return calls


def test_matching_native_source_success_wakes_existing_slow_private_wait(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    renders = observe_renders(canvas, monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2)
        assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        pending, context, row = preview._pending_image, preview._pending_context, preview._pending_row
        release.set()
        finish(canvas)
        assert preview._refresh_timer.isActive() and preview._refresh_timer.interval() == 1
        assert preview._pending_image is pending and preview._pending_context == context
        assert preview._pending_row == row and len(renders) == 1
        assert not preview._pending_derived_refresh and preview._cache.isNull()
        settle(preview)
        assert preview._cache == fresh(preview) and not preview._dirty_full
    finally:
        release.set()


def test_native_capacity_release_wakes_rejected_source_and_reverts_wait_between_releases(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    jobs, gates, workers = canvas._effect_jobs, [Event(), Event()], []
    jobs.budget = 100
    for index, gate in enumerate(gates):
        jobs.request(('object', 'release-blocker', 'canvas', index), ('native', index),
            lambda _cancelled, gate=gate: (gate.wait(5), QImage(1, 1, QImage.Format_ARGB32))[1], 50)
        workers.append(jobs.running_jobs[-1])
    renders = observe_renders(canvas, monkeypatch)
    try:
        preview._refresh_cache()
        pending, row = preview._pending_image, preview._pending_row
        assert len(renders) == 1 and preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        assert jobs.waiting and jobs.submitted == 2
        for index, gate in enumerate(gates):
            gate.set()
            workers[index][3].result(timeout=5)
            jobs.poll()
            assert preview._refresh_timer.interval() == 1
            assert preview._pending_image is pending and preview._pending_row == row
            assert not preview._pending_derived_refresh and preview._cache.isNull()
            preview._refresh_cache()
            assert len(renders) == index + 2
            assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
            if index == 0:
                assert jobs.waiting and jobs.submitted == 2
                preview._refresh_cache()
                assert len(renders) == 2, 'An unreleased capacity wait must not render another band'
        assert jobs.submitted == 3 and len(jobs.running_jobs) == 1
        finish(canvas)
        assert preview._refresh_timer.interval() == 1
        settle(preview)
        band_count = (preview._cache.height() + preview.REFRESH_BAND_HEIGHT - 1) // preview.REFRESH_BAND_HEIGHT
        assert len(renders) == band_count + 3
        assert preview._cache == fresh(preview) and not preview._dirty_full
    finally:
        for gate in gates:
            gate.set()


def test_matching_capacity_notifications_do_not_restart_an_earlier_continuation(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    renders = observe_renders(canvas, monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2)
        canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
        assert preview._refresh_timer.interval() == 1
        timer = preview._refresh_timer.timerId()
        for _ in range(100):
            canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
            assert preview._refresh_timer.timerId() == timer
        assert len(renders) == 1 and not preview._pending_derived_refresh
        # The one early continuation observes only the running metadata and
        # restores the normal 120 ms wait. It never repeats a pending band.
        QTest.qWait(20)
        assert len(renders) == 1 and preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        QTest.qWait(30)
        assert len(renders) == 1 and preview._pending_row == 0
        release.set()
        finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview)
    finally:
        release.set()


@pytest.mark.parametrize('signal', ['unknown', 'stale-source', 'contact'])
def test_only_current_release_can_shorten_wait_and_active_contact_still_pauses(
        source_navigator, monkeypatch, signal):
    canvas, preview = source_navigator
    cold_source(canvas)
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    renders = observe_renders(canvas, monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2)
        if signal == 'unknown':
            timer = preview._refresh_timer.timerId()
            canvas.derivedResultReady.emit()
            assert preview._refresh_timer.timerId() == timer
            assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
            assert preview._pending_derived_refresh
        elif signal == 'stale-source':
            canvas.images._decode_generation += 1
            canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
            assert preview._pending_image.isNull() and preview._dirty_full
            assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        else:
            canvas._drawing = True
            canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
            assert preview._refresh_timer.interval() == 1
            preview._refresh_cache()
            assert len(renders) == 1 and preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
            assert preview._pending_row == 0 and not preview._pending_derived_refresh
            canvas._drawing = False
        release.set()
        finish(canvas)
    finally:
        canvas._drawing = False
        release.set()


def test_running_source_without_release_never_enters_one_ms_polling(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    renders = observe_renders(canvas, monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2)
        for _ in range(20):
            preview._refresh_cache()
            assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        assert len(renders) == 1 and canvas._effect_jobs.submitted == 1
        assert preview._cache.isNull() and preview._pending_row == 0
        release.set()
        finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview)
    finally:
        release.set()
