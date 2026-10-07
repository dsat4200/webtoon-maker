"""Cold source decoding must yield without changing original pixels or caches."""
from threading import Event, get_ident

import pytest
from PySide6.QtCore import QByteArray, QBuffer, QIODevice
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.source_images import image_for_render, _decode_memory


def image_bytes(color='red', *, wide=False):
    image = QImage(48, 32, QImage.Format_RGBA64 if wide else QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    image.setPixelColor(13, 9, QColor.fromRgbF(.3123, .4517, .7919, .671))
    image.setColorSpace(QColorSpace(QColorSpace.NamedColorSpace.AdobeRgb))
    array, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(array)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return bytes(array)


def same_image(left, right):
    assert left.size() == right.size() and left.format() == right.format()
    assert bytes(left.constBits()) == bytes(right.constBits())
    assert left.colorSpace() == right.colorSpace()


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    chapter = ChapterDocument(width=128, height=128, document_kind='asset')
    chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 128, 128))
    owner.set_document(chapter, TileStore())
    owner._interactive_render = owner._projection_exact = owner._projection_defer_effects = True
    yield owner
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.deleteLater()


def cold_source(canvas, raw=None):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=10, y=20, pixel_width=48, pixel_height=32))
    canvas.images.put(obj.object_id, 'colors.png', raw or image_bytes())
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    return obj


def finish(canvas):
    for job in canvas._effect_jobs.running_jobs:
        job[3].result(timeout=5)
    canvas._effect_jobs.poll()


def blocked_decoder(monkeypatch):
    original, owner, entered, release, calls = ImageStore._decode, get_ident(), Event(), Event(), []
    def decode(data):
        calls.append(get_ident())
        if get_ident() != owner:
            entered.set()
            assert release.wait(5)
        return original(data)
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(decode))
    return entered, release, calls


@pytest.mark.parametrize('wide', [False, True])
def test_cold_decoder_runs_once_off_owner_thread_and_hands_off_exact_pixels(canvas, monkeypatch, wide):
    raw = image_bytes(wide=wide)
    expected = ImageStore._decode(raw)[0]
    native = ImageStore._decode_native(raw)[0]
    obj = cold_source(canvas, raw)
    entered, release, calls = blocked_decoder(monkeypatch)
    try:
        with pytest.raises(ProjectionPending) as first:
            image_for_render(canvas, obj.object_id)
        assert entered.wait(2)
        assert canvas.images.cached_image(obj.object_id) is None
        with pytest.raises(ProjectionPending) as second:
            image_for_render(canvas, obj.object_id)
        assert first.value.key == second.value.key and canvas._effect_jobs.submitted == 1
        assert canvas._effect_jobs.bytes_in_flight == _decode_memory(canvas.images.source(obj.object_id)._encoded)
        release.set()
        finish(canvas)
        same_image(image_for_render(canvas, obj.object_id), expected)
        assert calls == [calls[0]] and calls[0] != get_ident()
        assert not canvas._effect_jobs.retained
        assert not any(key[:1] == ('source-image-decode-preview',) for key in canvas._modifier_render_cache)
        same_image(canvas.images.native_image(obj.object_id), native)
    finally:
        release.set()


@pytest.mark.parametrize('mode', ['replace', 'remove', 'restore', 'history', 'new_store', 'relabel'])
def test_completed_decoder_cannot_adopt_a_replaced_context(canvas, monkeypatch, mode):
    raw, newer = image_bytes(), image_bytes('blue')
    obj = cold_source(canvas, raw)
    store, snapshot = canvas.images, canvas.images.snapshot()
    entered, release, calls = blocked_decoder(monkeypatch)
    try:
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        assert entered.wait(2)
        if mode == 'replace':
            store.put(obj.object_id, 'new.png', newer)
        elif mode == 'remove':
            store.remove(obj.object_id)
        elif mode == 'restore':
            store.restore(snapshot)
        elif mode == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
        elif mode == 'new_store':
            canvas.images = ImageStore()
            canvas.images.put(obj.object_id, 'new.png', newer)
        else:
            store.relabel(obj.object_id, 'same-pixels.png')
        canvas.images._decoded.clear()
        canvas.images.decoded_bytes = 0
        release.set()
        finish(canvas)
        if mode == 'remove':
            assert image_for_render(canvas, obj.object_id).isNull()
            assert canvas.images.cached_image(obj.object_id) is None
        elif mode == 'relabel':
            same_image(image_for_render(canvas, obj.object_id), ImageStore._decode(raw)[0])
            assert canvas._effect_jobs.submitted == 1
        else:
            with pytest.raises(ProjectionPending):
                image_for_render(canvas, obj.object_id)
            assert canvas.images.cached_image(obj.object_id) is None
            finish(canvas)
            expected = newer if mode in {'replace', 'new_store'} else raw
            same_image(image_for_render(canvas, obj.object_id), ImageStore._decode(expected)[0])
            assert canvas._effect_jobs.submitted == 2
    finally:
        release.set()


def test_reentrant_source_replacement_at_ready_signal_rejects_old_handoff(canvas, monkeypatch):
    obj = cold_source(canvas)
    entered, release, _calls = blocked_decoder(monkeypatch)
    try:
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        assert entered.wait(2)
        newer = image_bytes('blue')
        def changed(*_):
            canvas.images.put(obj.object_id, 'new.png', newer)
        canvas.derivedResultReady.connect(changed)
        release.set()
        # result() polls a finished future, whose ready signal reenters before
        # adoption. It must not overwrite the new source's decoded pixels.
        canvas._effect_jobs.running[3].result(timeout=5)
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        canvas.derivedResultReady.disconnect(changed)
        same_image(canvas.images.cached_image(obj.object_id), ImageStore._decode(newer)[0])
    finally:
        release.set()


def test_changed_pin_stamp_during_ready_signal_rejects_handoff(canvas, monkeypatch):
    import os
    obj = cold_source(canvas)
    path = canvas.images.source(obj.object_id)._encoded.pin.path
    entered, release, _calls = blocked_decoder(monkeypatch)
    try:
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        assert entered.wait(2)
        def changed(*_):
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
        canvas.derivedResultReady.connect(changed)
        release.set()
        canvas._effect_jobs.running[3].result(timeout=5)
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        canvas.derivedResultReady.disconnect(changed)
        assert canvas.images.cached_image(obj.object_id) is None
    finally:
        release.set()


def test_live_and_exact_requests_share_one_native_decode_and_borrowers_detach(canvas, monkeypatch):
    obj = cold_source(canvas)
    entered, release, _calls = blocked_decoder(monkeypatch)
    try:
        canvas._projection_exact = canvas._projection_defer_effects = False
        canvas._bounded_effect_preview = True
        with pytest.raises(ProjectionPending) as live:
            image_for_render(canvas, obj.object_id)
        assert entered.wait(2)
        canvas._projection_exact = canvas._projection_defer_effects = True
        canvas._bounded_effect_preview = False
        canvas._effect_jobs.cancel_exact()
        with pytest.raises(ProjectionPending) as exact:
            image_for_render(canvas, obj.object_id)
        assert live.value.key == exact.value.key and canvas._effect_jobs.submitted == 1
        release.set()
        finish(canvas)
        borrowed = image_for_render(canvas, obj.object_id)
        borrowed.fill(QColor('black'))
        same_image(image_for_render(canvas, obj.object_id), ImageStore._decode(image_bytes())[0])
    finally:
        release.set()


def test_decode_admission_waits_without_falling_back_to_owner_thread(canvas, monkeypatch):
    obj = cold_source(canvas)
    gate = Event()
    jobs = canvas._effect_jobs
    jobs.budget = 100
    jobs.request(('blocker',), ('blocker-preview',), lambda _cancelled: (gate.wait(5), QImage(1, 1, QImage.Format_ARGB32))[1], 100)
    calls = []
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda data: calls.append(get_ident()) or QImage()))
    try:
        with pytest.raises(ProjectionPending) as pending:
            image_for_render(canvas, obj.object_id)
        assert not calls and pending.value.scope in jobs.waiting
        assert jobs.bytes_in_flight == 100 and not jobs.pending
        gate.set()
        finish(canvas)
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        assert jobs.running[4] > jobs.budget and len(jobs.running_jobs) == 1
    finally:
        gate.set()


def test_oversized_decoded_frame_reuses_bounded_handoff(canvas):
    obj = cold_source(canvas)
    canvas.images.decoded_budget = 1
    with pytest.raises(ProjectionPending):
        image_for_render(canvas, obj.object_id)
    finish(canvas)
    expected = ImageStore._decode(image_bytes())[0]
    same_image(image_for_render(canvas, obj.object_id), expected)
    assert canvas.images.cached_image(obj.object_id) is None
    same_image(image_for_render(canvas, obj.object_id), expected)
    assert canvas._effect_jobs.submitted == 1


def test_exact_scene_capture_unwinds_pending_and_resumes_native_pixels(canvas, monkeypatch):
    from comic_editor.render.service import RenderQuality, RenderRequest, RenderStatus
    obj = cold_source(canvas, image_bytes(wide=True))
    document = canvas._render_document_state()
    request = RenderRequest((0., 0., 128., 128.), 1., (128, 128), ('source-oracle',),
        document.revision, quality=RenderQuality.EXACT)
    expected = canvas._render_service.render_region(document, request)
    assert expected.status is RenderStatus.EXACT
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    from dataclasses import replace
    deferred = replace(request, defer_effects=True)
    entered, release, _calls = blocked_decoder(monkeypatch)
    model = canvas.chapter.to_dict()
    ambient = canvas._projection_exact, canvas._projection_defer_effects
    try:
        pending = canvas._render_service.render_region(document, deferred)
        assert pending.status is RenderStatus.PENDING and entered.wait(2)
        assert (canvas._projection_exact, canvas._projection_defer_effects) == ambient
        assert canvas.chapter.to_dict() == model
        release.set()
        finish(canvas)
        actual = canvas._render_service.render_region(document, deferred)
        assert actual.status is RenderStatus.EXACT
        same_image(actual.image, expected.image)
        assert canvas.chapter.to_dict() == model
        assert not canvas.images.cached_image(obj.object_id).isNull()
    finally:
        release.set()


@pytest.mark.parametrize('mode', ['synchronous', 'export', 'mask', 'bounded_live'])
def test_source_edge_only_defers_explicit_safe_captures(canvas, mode):
    obj = cold_source(canvas)
    if mode == 'synchronous':
        canvas._projection_defer_effects = False
    elif mode == 'export':
        canvas._interactive_render = False
    elif mode == 'mask':
        canvas._rendering_mask_contributor = 1
    else:
        canvas._projection_exact = canvas._projection_defer_effects = False
        canvas._bounded_effect_preview = True
    if mode == 'bounded_live':
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        finish(canvas)
    expected = ImageStore._decode(image_bytes())[0]
    same_image(image_for_render(canvas, obj.object_id), expected)
    assert canvas._effect_jobs.submitted == (mode == 'bounded_live')


def test_failure_and_canceled_future_never_enter_decoded_store_or_disk(canvas, monkeypatch, tmp_path):
    from comic_editor.render.cache import PersistentRenderCache
    from comic_editor.ui.cache_dependencies import exact_cache_allowed
    obj = cold_source(canvas)
    cache = canvas._persistent_render_cache = PersistentRenderCache(tmp_path)
    entered, release, _calls = blocked_decoder(monkeypatch)
    try:
        with cache.record():
            with pytest.raises(ProjectionPending) as pending:
                image_for_render(canvas, obj.object_id)
            assert entered.wait(2)
            assert not exact_cache_allowed(canvas, pending.value.key)
            canvas._effect_jobs.cancel()
            release.set()
            finish(canvas)
        cache.drain()
        assert not cache.entries and canvas.images.cached_image(obj.object_id) is None
        monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda _data: (_ for _ in ()).throw(ValueError('invalid original'))))
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        for job in canvas._effect_jobs.running_jobs:
            with pytest.raises(ValueError, match='invalid original'):
                job[3].result(timeout=5)
        canvas._effect_jobs.poll()
        with pytest.raises(ProjectionFailed, match='invalid original'):
            image_for_render(canvas, obj.object_id)
        assert canvas.images.cached_image(obj.object_id) is None
    finally:
        release.set()
        cache.close()
        canvas._persistent_render_cache = None
