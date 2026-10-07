"""Derived completions advance Navigator bands without hiding actual edits."""
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS
from test_chapter_preview_scheduling import navigator, finish_build
from test_effect_regions import scene


def context(canvas):
    canvas.chapter.pixel_contract = LEGACY_PIXELS
    canvas.chapter.background = '#18181c'
    canvas._document_projection = SimpleNamespace(revision=1)
    canvas.command_stack = SimpleNamespace(revision=1)
    canvas._history_generation = 1
    canvas.tiles, canvas.images = object(), object()
    canvas.configuration = ('view', 1)
    canvas._projection_configuration = lambda: canvas.configuration


def fresh(preview):
    image = QImage(preview.content_rect().size(), QImage.Format_ARGB32_Premultiplied)
    preview._render_live_preview(image)
    return image


def settle(preview):
    for _ in range(100):
        preview._refresh_cache()
        if (preview._pending_image.isNull() and not preview._dirty_full
                and not preview._dirty_bands):
            return
    pytest.fail('Derived Navigator work did not settle')


def test_completion_storm_finishes_same_model_then_repaints_every_improved_band(navigator):
    canvas, preview = navigator
    context(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    pending = preview._pending_image
    row = preview._pending_row
    canvas.color = QColor('blue')
    for _ in range(300):
        canvas.derivedResultReady.emit()
    assert preview._pending_image is pending and preview._pending_row == row
    assert preview._pending_derived_refresh
    finish_build(preview)
    assert not preview._cache.isNull() and preview._dirty_full
    # The first same-model frame can contain earlier provisional bands. The
    # coalesced follow-up must replace every one, including already passed rows.
    assert preview._cache.pixelColor(0, 0) == QColor('red')
    settle(preview)
    assert preview._cache == fresh(preview)
    assert all(preview._cache.pixelColor(0, y) == QColor('blue')
               for y in range(preview._cache.height()))


@pytest.mark.parametrize('pending', [False, True])
def test_ready_storm_does_not_restart_idle_or_band_timer(navigator, pending):
    canvas, preview = navigator
    context(canvas)
    if pending:
        preview.REFRESH_BAND_HEIGHT = 16
        preview._refresh_cache()
    preview._refresh_timer.start(35 if pending else 120)
    timer = preview._refresh_timer.timerId()
    for _ in range(300):
        canvas.derivedResultReady.emit()
        assert preview._refresh_timer.timerId() == timer
    assert preview._refresh_timer.isActive()


def test_completion_during_every_band_never_abandons_or_postpones_progress(navigator):
    canvas, preview = navigator
    context(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    original = canvas.render_preview
    rows = []
    def render(image, clip):
        original(image, clip)
        rows.append(clip.top())
        canvas.derivedResultReady.emit()
    canvas.render_preview = render
    finish_build(preview)
    assert rows == list(range(0, preview._cache.height(), 16))
    assert preview._dirty_full
    canvas.render_preview = original
    settle(preview)
    assert preview._cache == fresh(preview)


@pytest.mark.parametrize('event', ['visual', 'document', 'hierarchy'])
def test_real_edit_during_derived_notification_abandons_old_pixels(navigator, event):
    canvas, preview = navigator
    context(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    original = canvas.render_preview
    def render(image, clip):
        original(image, clip)
        canvas.derivedResultReady.emit()
        canvas.color = QColor('green')
        canvas.render_preview = original
        if event == 'hierarchy':
            canvas.hierarchyChanged.emit()
        else:
            getattr(canvas, event + 'Changed').emit(QRectF(0, 0, 100, 20))
    canvas.render_preview = render
    preview._refresh_cache()
    assert preview._cache.isNull() and preview._pending_image.isNull()
    assert preview._dirty_full
    settle(preview)
    assert preview._cache == fresh(preview)


def change_context(canvas, change):
    if change == 'chapter':
        canvas.chapter = SimpleNamespace(width=100, height=1000, background='#18181c',
                                         pixel_contract=LEGACY_PIXELS)
    elif change in ('images', 'tiles'):
        setattr(canvas, change, object())
    elif change == 'history_generation':
        canvas._history_generation += 1
    elif change == 'history':
        canvas.command_stack.revision += 1
    elif change == 'revision':
        canvas._document_projection.revision += 1
    elif change == 'configuration':
        canvas.configuration = ('view', 2)
    elif change == 'contract':
        canvas.chapter.pixel_contract = FLOAT_PIXELS
    elif change == 'background':
        canvas.chapter.background = '#008800'
    elif change == 'size':
        canvas.chapter.height += 200


@pytest.mark.parametrize('change', ['chapter', 'images', 'tiles', 'history_generation',
    'history', 'revision', 'configuration', 'contract', 'background', 'size'])
@pytest.mark.parametrize('reentrant', [False, True])
def test_context_change_cannot_publish_mixed_old_model_bands(navigator, change, reentrant):
    canvas, preview = navigator
    context(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    original = canvas.render_preview
    if reentrant:
        def render(image, clip):
            original(image, clip)
            change_context(canvas, change)
            canvas.color = QColor('blue')
            canvas.render_preview = original
        canvas.render_preview = render
    else:
        preview._refresh_cache()
        change_context(canvas, change)
        canvas.color = QColor('blue')
        canvas.derivedResultReady.emit()
        assert preview._pending_image.isNull()
    preview._refresh_cache()
    assert preview._cache.isNull()
    settle(preview)
    assert preview._cache == fresh(preview)


def test_hidden_ready_changes_resume_and_repaint_earlier_bands(navigator):
    canvas, preview = navigator
    context(canvas)
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    row = preview._pending_row
    preview.hide()
    canvas.color = QColor('blue')
    for _ in range(100):
        canvas.derivedResultReady.emit()
    assert not preview._refresh_timer.isActive()
    assert preview._pending_row == row and preview._pending_derived_refresh
    preview.show()
    assert preview._refresh_timer.isActive()
    settle(preview)
    assert preview._cache == fresh(preview)


def test_derived_completion_during_hidden_idle_build_is_not_lost(navigator):
    canvas, preview = navigator
    preview._refresh_cache()
    preview.hide()
    canvas.color = QColor('blue')
    canvas.derivedResultReady.emit()
    assert preview._dirty_full and not preview._refresh_timer.isActive()
    preview.show()
    settle(preview)
    assert preview._cache == fresh(preview)


def test_document_closed_without_signal_abandons_pending_pixels_at_ready(navigator):
    canvas, preview = navigator
    preview.REFRESH_BAND_HEIGHT = 16
    preview._refresh_cache()
    canvas.chapter = None
    canvas.derivedResultReady.emit()
    assert preview._pending_image.isNull() and preview._cache.isNull()
    assert preview._dirty_full


@pytest.mark.parametrize('retry', [False, True])
def test_real_worker_ready_and_retry_preserve_native_result_without_visual_edit(scene, retry):
    canvas = scene
    jobs = canvas._effect_jobs
    image = QImage(9, 7, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('#a0704020'))
    ready, visual = [], []
    canvas.derivedResultReady.connect(lambda: ready.append(True))
    canvas.visualChanged.connect(visual.append)
    revision = canvas._document_projection.revision
    key = ('derived-notification-native-proof',)
    assert jobs.request('derived-proof', key, lambda cancelled: image, image.sizeInBytes())
    jobs.running[3].result(timeout=5)
    jobs.timer.stop()
    jobs.retry_on_release = retry
    jobs.poll()
    assert ready == [True] * (2 if retry else 1)
    assert not visual
    assert canvas._document_projection.revision == revision
    assert canvas._modifier_cache_get(key) == image
