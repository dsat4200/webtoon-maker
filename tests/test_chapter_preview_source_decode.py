"""Navigator cold sources use the existing native handoff without publishing a band."""
from dataclasses import replace
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, import_image, pixel_scope
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.preview import ChapterPreview
from comic_editor.ui.source_images import image_for_render, _working_representation
from test_deferred_source_images import canvas, cold_source, finish, image_bytes, same_image
from test_chapter_preview_derived import fresh, settle


@pytest.fixture
def source_navigator(canvas, qapp):
    canvas._projection_exact = canvas._projection_defer_effects = False
    canvas._interactive_render = False
    canvas.setUpdatesEnabled(False)
    preview = ChapterPreview(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    preview.resize(92, 300)
    preview.show()
    qapp.processEvents()
    yield canvas, preview
    preview.close()
    preview.deleteLater()


def blocked_native_decoder(monkeypatch):
    original, owner = ImageStore._decode_native, get_ident()
    entered, release, calls = Event(), Event(), []
    def decode(data):
        calls.append(get_ident())
        if get_ident() != owner:
            entered.set()
            assert release.wait(5)
        return original(data)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    return entered, release, calls


@pytest.mark.parametrize('precision', ['legacy8', 'float16', 'float32'])
def test_cold_navigator_band_yields_original_decode_and_matches_native_source_and_fresh_frame(
        source_navigator, monkeypatch, precision):
    canvas, preview = source_navigator
    raw = image_bytes(wide=True)
    obj = cold_source(canvas, raw)
    contract = LEGACY_PIXELS if precision == 'legacy8' else replace(FLOAT_PIXELS, precision=precision)
    canvas.chapter.pixel_contract = contract
    native = ImageStore._decode_native(raw)[0]
    expected = (ImageStore._decode(raw)[0] if precision == 'legacy8' else import_image(native, contract))
    model = canvas.chapter.to_dict()
    entered, release, calls = blocked_native_decoder(monkeypatch)
    try:
        with pixel_scope(contract):
            preview._refresh_cache()
        assert entered.wait(2), 'The original cold source must decode on the existing worker'
        assert preview._cache.isNull() and not preview._pending_image.isNull()
        pending, context = preview._pending_image, preview._pending_context
        assert preview._pending_row == 0 and preview._refresh_timer.isActive()
        assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
        assert not getattr(canvas, '_navigator_defer_sources', False)
        assert not canvas._interactive_render and canvas._effect_preview_channel == 'canvas'
        with pixel_scope(contract):
            preview._refresh_cache()
        assert preview._pending_image is pending and preview._pending_context == context
        assert preview._pending_row == 0 and canvas._effect_jobs.submitted == 1
        assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS, 'A pending source cannot cause a1ms render loop'
        release.set()
        finish(canvas)
        assert preview._refresh_timer.isActive() and not preview._pending_derived_refresh
        with pixel_scope(contract):
            settle(preview)
            assert preview._cache == fresh(preview)
        cached = (canvas.images.cached_image(obj.object_id) if precision == 'legacy8' else
            canvas.images.cached_working_image(obj.object_id, _working_representation(contract)))
        same_image(cached, expected)
        assert calls and all(thread != get_ident() for thread in calls) and len(calls) == 1
        assert not preview._dirty_full and not preview._pending_derived_refresh
        assert canvas.images.source(obj.object_id).data == raw and canvas.chapter.to_dict() == model
    finally:
        release.set()


def test_pending_partial_band_keeps_previous_committed_frame_until_all_new_pixels_ready(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    obj = cold_source(canvas, image_bytes('red'))
    canvas.images.image(obj.object_id)
    settle(preview)
    old = preview._cache.copy()
    canvas.images.put(obj.object_id, 'new.png', image_bytes('blue'))
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    canvas.visualChanged.emit(None)
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2) and preview._cache == old and preview._pending_row == 0
        release.set()
        finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview) and preview._cache != old
    finally:
        release.set()


@pytest.mark.parametrize('change', ['source', 'history', 'chapter'])
def test_changed_context_during_pending_source_never_publishes_old_band(
        source_navigator, monkeypatch, change):
    canvas, preview = source_navigator
    obj = cold_source(canvas, image_bytes('red'))
    entered, release, _calls = blocked_native_decoder(monkeypatch)
    try:
        preview._refresh_cache()
        assert entered.wait(2) and not preview._pending_image.isNull()
        if change == 'source':
            canvas.images.put(obj.object_id, 'new.png', image_bytes('blue'))
        elif change == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
        else:
            canvas.chapter = type(canvas.chapter).from_dict(canvas.chapter.to_dict())
        canvas.documentChanged.emit(None)
        assert preview._pending_image.isNull() and preview._cache.isNull()
        release.set()
        finish(canvas)
        for _ in range(5):
            preview._refresh_cache()
            finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview)
        assert canvas.images.source(obj.object_id).data == image_bytes('blue' if change == 'source' else 'red')
    finally:
        release.set()


@pytest.mark.parametrize('mode', ['standalone', 'canvas', 'posterize'])
def test_source_optin_is_only_owned_navigator_capture(source_navigator, mode):
    canvas, preview = source_navigator
    obj = cold_source(canvas)
    canvas._interactive_render = mode != 'standalone'
    canvas._effect_preview_channel = 'posterize-statistics' if mode == 'posterize' else 'canvas'
    image_for_render(canvas, obj.object_id)
    assert canvas._effect_jobs.submitted == 0
    assert not getattr(canvas, '_navigator_defer_sources', False)


def test_capture_flag_restores_on_exception_and_nested_owned_capture(source_navigator, monkeypatch):
    canvas, preview = source_navigator
    image = QImage(preview.content_rect().size(), QImage.Format_ARGB32_Premultiplied)
    def failed(*_):
        assert canvas._navigator_defer_sources and canvas._effect_preview_channel == 'navigator'
        raise ProjectionPending(('source-image-decode',), ('source-image-decode-preview',))
    monkeypatch.setattr(canvas, 'render_preview', failed)
    with pytest.raises(ProjectionPending):
        preview._render_live_preview(image)
    assert not hasattr(canvas, '_navigator_defer_sources')
    assert not canvas._interactive_render and canvas._effect_preview_channel == 'canvas'
    canvas._navigator_defer_sources = True
    with pytest.raises(ProjectionPending):
        preview._render_live_preview(image)
    assert canvas._navigator_defer_sources is True


def test_navigator_source_handoff_and_unfinished_band_never_enter_disk(source_navigator, tmp_path):
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.ui.cache_dependencies import exact_cache_allowed
    canvas, preview = source_navigator
    cold_source(canvas)
    cache = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            preview._refresh_cache()
            assert preview._cache.isNull()
            for job in canvas._effect_jobs.running_jobs:
                assert not exact_cache_allowed(canvas, job[1])
            finish(canvas)
            settle(preview)
        cache.drain()
        assert not cache.entries and preview._cache == fresh(preview)
    finally:
        cache.close()
        canvas._persistent_render_cache = None


def test_pending_source_budget_retry_resumes_without_owner_thread_decode(source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    jobs, gate = canvas._effect_jobs, Event()
    jobs.budget = 100
    jobs.request(('object', 'blocker', 'canvas'), ('native-blocker',),
        lambda _cancelled: (gate.wait(5), QImage(1, 1, QImage.Format_ARGB32))[1], 100)
    original, calls = ImageStore._decode_native, []
    def decode(data):
        calls.append(get_ident())
        return original(data)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    try:
        preview._refresh_cache()
        assert not calls and preview._pending_row == 0 and preview._cache.isNull()
        assert jobs.retry_on_release and jobs._navigator_retry_relevance is True
        assert preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS and not jobs.pending
        gate.set()
        finish(canvas)
        assert preview._refresh_timer.isActive()
        preview._refresh_cache()
        assert jobs.running[4] > jobs.budget and len(jobs.running_jobs) == 1
        finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview)
        assert len(calls) == 1 and calls[0] != get_ident()
    finally:
        gate.set()


@pytest.mark.parametrize('precision', ['float16', 'float32'])
def test_native_low_alpha_source_uses_the_same_working_pixels_and_shared_handoff(
        source_navigator, precision):
    canvas, preview = source_navigator
    native = QImage(3, 2, QImage.Format_RGBA64)
    native.fill(QColor('red'))
    native.setPixelColor(0, 0, QColor.fromRgbF(.75, .2, .1, 1/65535))
    raw, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    assert native.save(buffer, 'PNG')
    buffer.close()
    raw = bytes(raw)
    obj = cold_source(canvas, raw)
    contract = replace(FLOAT_PIXELS, precision=precision)
    canvas.chapter.pixel_contract = contract
    expected = import_image(ImageStore._decode_native(raw)[0], contract)
    with pixel_scope(contract):
        preview._refresh_cache()
        assert preview._cache.isNull() and canvas._effect_jobs.submitted == 1
        finish(canvas)
        # Another deferred native capture shares the same source worker result.
        canvas._interactive_render = canvas._projection_exact = canvas._projection_defer_effects = True
        actual = image_for_render(canvas, obj.object_id)
        same_image(actual, expected)
        canvas._interactive_render = canvas._projection_exact = canvas._projection_defer_effects = False
        settle(preview)
        assert preview._cache == fresh(preview) and canvas._effect_jobs.submitted == 1
    assert canvas.images.source(obj.object_id).data == raw


@pytest.mark.parametrize('failure', ['exception', 'null'])
def test_failed_or_null_source_resumes_to_existing_failure_without_publishing(
        source_navigator, monkeypatch, failure):
    canvas, preview = source_navigator
    cold_source(canvas)
    def decode(data):
        if failure == 'exception':
            raise ValueError('broken original source')
        return QImage(), b'png'
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    preview._refresh_cache()
    assert preview._pending_source_dependency and preview._cache.isNull()
    for job in canvas._effect_jobs.running_jobs:
        if failure == 'exception':
            with pytest.raises(ValueError, match='broken original source'):
                job[3].result(timeout=5)
        else:
            job[3].result(timeout=5)
    canvas._effect_jobs.poll()
    assert preview._refresh_timer.isActive() and preview._refresh_timer.interval() == preview.REFRESH_DELAY_MS
    with pytest.raises(ProjectionFailed):
        preview._refresh_cache()
    assert preview._pending_image.isNull() and preview._cache.isNull() and preview._dirty_full
    assert preview._pending_source_dependency is None


def test_waiting_source_timer_checks_only_job_metadata_and_cancel_releases_it(
        source_navigator, monkeypatch):
    canvas, preview = source_navigator
    cold_source(canvas)
    entered, release, calls = blocked_native_decoder(monkeypatch)
    original_render = canvas.render_preview
    try:
        preview._refresh_cache()
        assert entered.wait(2)
        pending, context, row = preview._pending_image, preview._pending_context, preview._pending_row
        def unexpected(*_):
            raise AssertionError('Pending source retries must not render the scene')
        monkeypatch.setattr(canvas, 'render_preview', unexpected)
        for _ in range(4):
            preview._refresh_cache()
            assert preview._pending_image is pending and preview._pending_context == context
            assert preview._pending_row == row and preview._cache.isNull()
        assert len(calls) == 1 and canvas._effect_jobs.submitted == 1
        canvas._effect_jobs.cancel()
        release.set()
        finish(canvas)
        monkeypatch.setattr(canvas, 'render_preview', original_render)
        preview._refresh_cache()
        assert canvas._effect_jobs.submitted == 2 and preview._cache.isNull()
        finish(canvas)
        settle(preview)
        assert preview._cache == fresh(preview) and preview._pending_source_dependency is None
    finally:
        release.set()
