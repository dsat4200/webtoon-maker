"""An awaited native source resumes its private band without a full-frame loop."""
from dataclasses import replace

import pytest

from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope
from comic_editor.ui.source_images import navigator_source_handoff_current
from test_chapter_preview_source_decode import source_navigator
from test_deferred_source_images import canvas, cold_source, finish, image_bytes
from test_chapter_preview_derived import fresh, settle


@pytest.mark.parametrize('precision', ['legacy8', 'float16', 'float32'])
def test_native_source_success_finishes_one_original_build_without_followup(
        source_navigator, monkeypatch, precision):
    canvas, preview = source_navigator
    cold_source(canvas, image_bytes(wide=True))
    contract = LEGACY_PIXELS if precision == 'legacy8' else replace(FLOAT_PIXELS, precision=precision)
    canvas.chapter.pixel_contract = contract
    calls, original = [], canvas.render_preview
    def render(image, clip):
        calls.append(clip)
        return original(image, clip)
    monkeypatch.setattr(canvas, 'render_preview', render)
    with pixel_scope(contract):
        preview._refresh_cache()
    pending, context = preview._pending_image, preview._pending_context
    row = preview._pending_row
    emitted = []
    canvas.derivedResultReady.connect(lambda: emitted.append(True))
    finish(canvas)
    assert emitted == [True]
    assert preview._pending_image is pending and preview._pending_context == context
    assert preview._pending_row == row and not preview._pending_derived_refresh
    assert preview._refresh_timer.isActive()
    assert canvas._effect_jobs._navigator_derived_dependency is None
    with pixel_scope(contract):
        settle(preview)
        band_count = (preview._cache.height() + preview.REFRESH_BAND_HEIGHT - 1) // preview.REFRESH_BAND_HEIGHT
        assert len(calls) == band_count + 1  # One unfinished source capture, then one complete build.
        assert preview._cache == fresh(preview)
    assert not preview._dirty_full and preview._pending_source_dependency is None


@pytest.mark.parametrize('signal', ['other-source', 'unknown', 'retry-without-source'])
def test_unrelated_or_unknown_completion_still_requests_conservative_followup(source_navigator, signal):
    canvas, preview = source_navigator
    cold_source(canvas)
    preview._refresh_cache()
    scope, key = preview._pending_source_dependency
    if signal == 'other-source':
        canvas._effect_jobs._emit_derived_ready((*scope[:3], 'other'), key)
    elif signal == 'retry-without-source':
        preview._pending_source_dependency = None
        canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
    else:
        canvas.derivedResultReady.emit()
    assert preview._pending_derived_refresh
    finish(canvas)
    settle(preview)
    assert preview._cache == fresh(preview)


@pytest.mark.parametrize('change', ['generation', 'source', 'history', 'contract', 'working-color', 'stamp'])
def test_stale_native_source_cannot_suppress_required_refresh(source_navigator, monkeypatch, change):
    canvas, preview = source_navigator
    obj = cold_source(canvas, image_bytes(wide=True))
    if change == 'working-color':
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    with pixel_scope(canvas.chapter.pixel_contract):
        preview._refresh_cache()
    scope, key = preview._pending_source_dependency
    if change == 'generation':
        canvas.images._decode_generation += 1
    elif change == 'source':
        canvas.images.put(obj.object_id, 'changed.png', image_bytes('blue'))
    elif change == 'history':
        canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
    elif change == 'contract':
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    elif change == 'working-color':
        monkeypatch.setattr('comic_editor.ui.source_images._working_representation', lambda _contract: ('different-loaded-color',))
    else:
        monkeypatch.setattr('comic_editor.ui.source_images._stamp', lambda _path: ('changed-file-stamp',))
    if change != 'contract':
        assert not navigator_source_handoff_current(canvas, scope, key)
    canvas._effect_jobs._emit_derived_ready(scope, key)
    assert preview._pending_image.isNull() or preview._pending_derived_refresh
    assert preview._dirty_full or preview._pending_derived_refresh
    assert preview._cache.isNull()
    finish(canvas)


def test_source_dependency_metadata_is_synchronous_and_reentrant(source_navigator):
    canvas, _preview = source_navigator
    jobs, seen = canvas._effect_jobs, []
    outer = (('source-image-decode', 1, 2, 'owner'), ('source-image-decode-preview', 3, 4, (), ()))
    inner = (('object', 'owner', 'canvas'), ('native',))
    def ready():
        seen.append(jobs._navigator_derived_dependency)
        if len(seen) == 1:
            jobs._emit_derived_ready(*inner)
            seen.append(jobs._navigator_derived_dependency)
    canvas.derivedResultReady.connect(ready)
    jobs._emit_derived_ready(*outer)
    assert seen == [outer, inner, outer]
    assert jobs._navigator_derived_dependency is None and jobs._navigator_derived_relevance is None


def test_source_capacity_wait_across_two_native_completions_finishes_one_build(
        source_navigator, monkeypatch):
    from threading import Event
    from PySide6.QtGui import QImage
    canvas, preview = source_navigator
    cold_source(canvas)
    jobs = canvas._effect_jobs
    jobs.budget = 100
    gates = [Event(), Event()]
    workers = []
    for index, gate in enumerate(gates):
        jobs.request(('object', 'blocker', 'canvas', index), ('native', index),
            lambda _cancelled, gate=gate: (gate.wait(5), QImage(1, 1, QImage.Format_ARGB32))[1], 50)
        workers.append(jobs.running_jobs[-1])
    emitted = []
    canvas.derivedResultReady.connect(lambda: emitted.append(True))
    calls, original = [], canvas.render_preview
    def render(image, clip):
        calls.append(clip)
        return original(image, clip)
    monkeypatch.setattr(canvas, 'render_preview', render)
    try:
        preview._refresh_cache()
        assert jobs.retry_on_release and preview._pending_source_dependency
        pending = preview._pending_image
        for index, gate in enumerate(gates):
            gate.set()
            workers[index][3].result(timeout=5)
            jobs.poll()
            assert preview._pending_image is pending and not preview._pending_derived_refresh
            assert preview._refresh_timer.isActive()
            preview._refresh_cache()
            assert preview._pending_image is pending and preview._cache.isNull()
        assert jobs.submitted == 3 and len(jobs.running_jobs) == 1
        finish(canvas)
        assert not preview._pending_derived_refresh
        settle(preview)
        band_count = (preview._cache.height() + preview.REFRESH_BAND_HEIGHT - 1) // preview.REFRESH_BAND_HEIGHT
        assert len(calls) == band_count + 3
        assert len(emitted) == 5  # Two native successes + two capacity releases + the source success.
        assert preview._cache == fresh(preview)
        assert not preview._dirty_full and jobs._navigator_derived_retry is False
    finally:
        for gate in gates:
            gate.set()


def test_capacity_retry_with_stale_private_source_stays_conservative(source_navigator):
    canvas, preview = source_navigator
    cold_source(canvas)
    preview._refresh_cache()
    canvas.images._decode_generation += 1
    canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
    assert preview._pending_image.isNull() and preview._dirty_full
    assert canvas._effect_jobs._navigator_derived_retry is False
    finish(canvas)



@pytest.mark.parametrize('precision', ['legacy8', 'float16', 'float32'])
def test_three_native_sources_over_two_frame_residency_budget_finish_once(
        source_navigator, monkeypatch, precision):
    from threading import get_ident
    from comic_editor.core.images import ImageStore
    canvas, preview = source_navigator
    contract = LEGACY_PIXELS if precision == 'legacy8' else replace(FLOAT_PIXELS, precision=precision)
    canvas.chapter.pixel_contract = contract
    sources = []
    for index, color in enumerate(('red', 'green', 'blue')):
        obj = cold_source(canvas, image_bytes(color, wide=True))
        obj.x, obj.y = 10, index * 48
        sources.append(obj)
    # All three original images exceed residency. At most two consecutive
    # sources intersect one band, so the current private band can still finish.
    frame_bytes = 48 * 32 * (4 if precision == 'legacy8' else 8 if precision == 'float16' else 16)
    canvas.images.decoded_budget = 2 * frame_bytes
    decoded, owner, original_decode = [], get_ident(), ImageStore._decode_native
    def decode(data):
        decoded.append(get_ident())
        return original_decode(data)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    renders, original_render = [], canvas.render_preview
    def render(image, clip):
        renders.append(clip)
        return original_render(image, clip)
    monkeypatch.setattr(canvas, 'render_preview', render)
    with pixel_scope(contract):
        for _ in range(40):
            preview._refresh_cache()
            finish(canvas)
            assert canvas.images.decoded_bytes <= canvas.images.decoded_budget
            assert not preview._pending_derived_refresh
            if preview._pending_image.isNull() and not preview._dirty_full and not preview._dirty_bands:
                break
        else:
            pytest.fail('Native source availability must not force another residency-evicting full build')
        assert not preview._cache.isNull()
        band_count = (preview._cache.height() + preview.REFRESH_BAND_HEIGHT - 1) // preview.REFRESH_BAND_HEIGHT
        assert len(renders) == band_count + len(sources)
        assert len(decoded) == len(sources) and all(thread != owner for thread in decoded)
        assert len(canvas.images._decoded) == 2
        from PySide6.QtGui import QImage
        from comic_editor.render.pixels import import_image
        from comic_editor.ui.source_images import _working_representation
        from test_deferred_source_images import same_image
        for obj in sources[-2:]:
            raw = canvas.images.source(obj.object_id).data
            expected = (ImageStore._decode(raw)[0] if precision == 'legacy8' else
                import_image(ImageStore._decode_native(raw)[0], contract))
            resident = (canvas.images.cached_image(obj.object_id) if precision == 'legacy8' else
                canvas.images.cached_working_image(obj.object_id, _working_representation(contract)))
            same_image(resident, expected)
        final = preview._cache.copy()
        oracle = QImage(preview.content_rect().size(), QImage.Format_ARGB32_Premultiplied)
        previous = canvas._interactive_render, canvas._effect_preview_channel
        try:
            canvas._interactive_render, canvas._effect_preview_channel = True, 'navigator'
            assert not getattr(canvas, '_navigator_defer_sources', False)
            canvas.render_preview(oracle, None)
        finally:
            canvas._interactive_render, canvas._effect_preview_channel = previous
        # A standalone ordinary capture performs the same native decoding
        # synchronously. It can render all three sources despite two-frame LRU.
        assert final == oracle
    assert not preview._dirty_full and preview._pending_source_dependency is None


def test_capacity_wakeup_preserves_already_required_unrelated_followup(source_navigator):
    canvas, preview = source_navigator
    cold_source(canvas)
    preview._refresh_cache()
    canvas.derivedResultReady.emit()
    assert preview._pending_derived_refresh
    canvas._effect_jobs._emit_derived_ready(retry=True, retry_relevance=True)
    assert preview._pending_derived_refresh
    finish(canvas)
    settle(preview)
    assert preview._cache == fresh(preview)
