"""A native completion improves Navigator only if it can supply its transient pixels."""
from threading import Event

import numpy as np
import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.ui.effect_jobs import EffectJobs, _navigator_job_relevant
from test_chapter_preview_scheduling import navigator, finish_build
from test_chapter_preview_derived import context, settle, fresh


@pytest.fixture
def dependency_navigator(navigator):
    canvas, preview = navigator
    context(canvas)
    jobs = canvas._effect_jobs = EffectJobs(canvas)
    canvas.received = {}
    canvas._modifier_cache_put = lambda key, image: canvas.received.update({key: QImage(image)})
    canvas._effect_result_ready = lambda scope, key: None
    yield canvas, preview, jobs
    jobs.cancel()
    jobs.executor.shutdown(wait=True, cancel_futures=True)


@pytest.mark.parametrize('wrapper', [None, 'tile-graph', 'tile-frame', 'mesh-region', 'pattern-frame'])
@pytest.mark.parametrize('channel,relevant', [('canvas', False), ('overflow', False),
    ('posterize-statistics', False), ('navigator', True), ('future-channel', True)])
def test_known_channel_ownership_and_nested_native_regions(wrapper, channel, relevant):
    scope = ('object', 'owner', channel, 'modifier', ('effect-region', (0, 0, 256, 256)))
    if wrapper:
        scope = (wrapper, scope, (1, 2))
    assert _navigator_job_relevant(scope) is relevant


@pytest.mark.parametrize('scope', [None, 'unknown', (), ('new-shared-source', 1),
    ('source-image-decode', 1, 2, 'owner'), ('cage-tool', 'owner'), ('tile-graph', 'unknown-parent'),
    ([], 'opaque'), ('object', 'owner', []), ('tile-graph', ([], 'opaque'))])
def test_shared_source_and_unknown_jobs_remain_conservative(scope):
    assert _navigator_job_relevant(scope)


def test_unrelated_native_completions_during_every_band_finish_one_frame(dependency_navigator):
    canvas, preview, jobs = dependency_navigator
    preview.REFRESH_BAND_HEIGHT = 16
    original = canvas.render_preview
    emitted = []
    canvas.derivedResultReady.connect(lambda: emitted.append(True))
    def render(image, clip):
        original(image, clip)
        for index in range(8):
            jobs._emit_derived_ready(('tile-graph', ('layer', 'owner', 'canvas'), (1, index)), ('native', index))
    canvas.render_preview = render
    finish_build(preview)
    assert not preview._dirty_full and preview._pending_image.isNull() and not preview._pending_derived_refresh
    assert len(canvas.calls) == (preview._cache.height() + 15) // 16
    assert len(emitted) == len(canvas.calls) * 8
    assert jobs._navigator_derived_relevance is None


def test_completed_navigator_remains_clean_but_global_consumers_still_receive(dependency_navigator):
    canvas, preview, jobs = dependency_navigator
    finish_build(preview)
    preview._refresh_timer.stop()
    calls = len(canvas.calls)
    global_ready = []
    canvas.derivedResultReady.connect(lambda: global_ready.append(True))
    for index in range(300):
        jobs._emit_derived_ready(('tile-frame', ('object', 'owner', 'canvas'), 'modifier'), ('native', index))
    assert len(global_ready) == 300 and len(canvas.calls) == calls
    assert not preview._dirty_full and not preview._refresh_timer.isActive()


@pytest.mark.parametrize('scope', [('source-image-decode', 1, 2, 'owner'),
    ('object', 'owner', 'navigator', 'modifier'),
    ('tile-frame', ('layer', 'owner', 'navigator'), 'modifier')])
def test_awaited_source_or_navigator_stage_keeps_band_and_coalesces_one_followup(dependency_navigator, scope):
    canvas, preview, jobs = dependency_navigator
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    pending, row = preview._pending_image, preview._pending_row
    canvas.color = QColor('blue')
    for _ in range(100):
        jobs._emit_derived_ready(scope, ('ready',))
    assert preview._pending_image is pending and preview._pending_row == row and preview._pending_derived_refresh
    finish_build(preview)
    assert preview._dirty_full
    settle(preview)
    assert preview._cache == fresh(preview)
    assert not preview._dirty_full and not preview._pending_derived_refresh


@pytest.mark.parametrize('event', ['documentChanged', 'visualChanged', 'hierarchyChanged'])
def test_real_edit_inside_ignored_native_notification_still_invalidates(dependency_navigator, event):
    canvas, preview, jobs = dependency_navigator
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    def edit():
        canvas.color = QColor('green')
        if event == 'hierarchyChanged':
            canvas.hierarchyChanged.emit()
        else:
            getattr(canvas, event).emit(None)
    canvas.derivedResultReady.connect(edit)
    jobs._emit_derived_ready(('object', 'owner', 'canvas'), ('native',))
    assert preview._pending_image.isNull() and preview._dirty_full
    settle(preview)
    assert preview._cache == fresh(preview)


@pytest.mark.parametrize('channel,relevant', [('canvas', False), ('navigator', True), ('future-channel', True)])
def test_denied_request_retry_refreshes_only_relevant_navigator(dependency_navigator, channel, relevant):
    canvas, preview, jobs = dependency_navigator
    finish_build(preview)
    preview._refresh_timer.stop()
    jobs.budget = 32
    release = Event()
    image = QImage(2, 2, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('blue'))
    def compute(cancelled):
        assert release.wait(5.)
        return image
    scope = ('object', 'owner', 'canvas')
    try:
        assert jobs.request(scope, ('first',), compute, 32)
        assert jobs.request(('object', 'other', channel), ('second',), lambda cancelled: image, 32)
        assert jobs.retry_on_release and jobs._navigator_retry_relevance is relevant
        release.set()
        jobs.running[3].result(timeout=5.)
        jobs.timer.stop()
        jobs.poll()
        assert preview._dirty_full is relevant
        assert jobs._navigator_retry_relevance is None and not jobs.retry_on_release
    finally:
        release.set()


def test_notification_metadata_is_scoped_and_reentrant(dependency_navigator):
    canvas, preview, jobs = dependency_navigator
    seen = []
    def nested():
        seen.append(jobs._navigator_derived_relevance)
        if len(seen) == 1:
            jobs._emit_derived_ready(('object', 'owner', 'canvas'), ('inner',))
            seen.append(jobs._navigator_derived_relevance)
    canvas.derivedResultReady.connect(nested)
    jobs._emit_derived_ready(('source-image-decode', 1, 2, 'owner'), ('outer',))
    assert seen == [True, False, True] and jobs._navigator_derived_relevance is None
    settle(preview)
    canvas.derivedResultReady.emit()
    assert preview._dirty_full


@pytest.mark.parametrize('precision', ['legacy8', 'float16', 'float32'])
def test_real_worker_global_signal_and_native_result_bytes_survive_filter(dependency_navigator, precision):
    canvas, preview, jobs = dependency_navigator
    finish_build(preview)
    preview._refresh_timer.stop()
    formats = {'legacy8': QImage.Format_ARGB32_Premultiplied,
        'float16': QImage.Format_RGBA16FPx4_Premultiplied, 'float32': QImage.Format_RGBA32FPx4_Premultiplied}
    image = QImage(3, 2, formats[precision])
    if precision == 'legacy8':
        image.fill(QColor('#a0704020'))
    else:
        dtype = np.float16 if precision == 'float16' else np.float32
        pixels = np.ndarray((2, 3, 4), dtype=dtype, buffer=image.bits(),
            strides=(image.bytesPerLine(), 4 * np.dtype(dtype).itemsize, np.dtype(dtype).itemsize))
        alpha = .0001
        pixels[:] = np.array([1.75 * alpha, .2 * alpha, .1 * alpha, alpha], dtype=dtype)
    original = bytes(image.constBits())
    global_ready = []
    canvas.derivedResultReady.connect(lambda: global_ready.append(True))
    scope, key = ('object', 'owner', 'canvas'), ('native-format-proof', precision)
    assert jobs.request(scope, key, lambda cancelled: image, image.sizeInBytes(), require_exact=True)
    jobs.running[3].result(timeout=5.)
    jobs.timer.stop()
    jobs.poll()
    result = canvas.received[key]
    assert result.format() == image.format() and bytes(result.constBits()) == original
    assert global_ready == [True] and not preview._dirty_full
    assert jobs.result(scope, key).format() == image.format()
