"""A native painted opacity output can yield without changing any pixels."""
from threading import Event, get_ident
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter, QTransform

from comic_editor.core.models import (BoundGeometry, ChapterDocument, DistortModifier,
    ImageObject, ParameterMaskBinding, ToneMask)
from comic_editor.core.pixel_contract import LEGACY_PIXELS, PixelContract
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import pixel_scope, working_image
from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
from comic_editor.ui.cache_dependencies import exact_cache_allowed
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.viewport_masking import mask_output
from comic_editor.ui.posterize_controls import PosterizeSampler, PosterizeSampleRequest
from test_posterize import editor


@pytest.fixture
def scene(qapp):
    document = ChapterDocument(height=800)
    page = document.add_page('Page', BoundGeometry.rectangle(0, 0, 1080, 800))
    page.fill_color, page.border_width = None, 0
    mask = ToneMask(saved=True, name='Paint mask')
    document.masks[mask.mask_id] = mask
    tiles = TileStore()
    tiles.paint_dab(mask.mask_id, QPointF(140, 155), 160, QColor(255, 255, 255, 187))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    canvas.set_document(document, tiles)
    yield canvas, mask
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


def draw(canvas, image, binding, mapping=None):
    target = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(target)
    try:
        return mask_output(canvas, image, QRectF(0, 0, image.width(), image.height()),
            mapping or QTransform(), binding, QRectF(0, 0, image.width(), image.height()), painter)[0]
    finally:
        painter.end()


def deferred(canvas):
    canvas._interactive_render = canvas._projection_exact = canvas._projection_defer_effects = True
    canvas._posterize_statistics_capture = True
    canvas._effect_preview_channel = 'posterize-statistics'
    canvas._render_modifier_sources.add(('object', 'captured'))


def spin(qapp, predicate):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(.002)
    assert predicate()


def bytes_of(image):
    return np.frombuffer(image.constBits(), np.uint8).copy()


@pytest.mark.parametrize('contract,signed,projective', [
    (LEGACY_PIXELS, False, False), (LEGACY_PIXELS, True, True),
    (PixelContract(version=2, precision='float16'), False, True),
    (PixelContract(version=2, precision='float32', working_space='linear_srgb'), True, False),
])
def test_blocked_exact_paint_output_matches_same_native_oracle(scene, qapp, monkeypatch, contract, signed, projective):
    canvas, mask = scene
    mask.paint_offset = (-.375, .8125)
    mask.paint_has_subtractions = signed
    if signed:
        canvas.tiles.paint_dab(mask.mask_id, QPointF(130, 145), 43, QColor('black'))
    binding = ParameterMaskBinding(mask.mask_id, .87, -.1)
    values = np.empty((320, 288, 4), np.float32)
    values[:] = (.2, .1, .05, .61)
    values[..., 0] = np.linspace(0, .6, 288)[None, :]
    mapping = QTransform(1, .02, .0003, -.04, 1, -.0001, .23, -.37, 1) if projective else QTransform()
    entered, release = Event(), Event()
    original = canvas._effect_jobs.request
    owner = get_ident()
    def request(scope, key, compute, memory, **kwargs):
        assert kwargs == {'allow_oversized': True, 'require_exact': True}
        assert not exact_cache_allowed(canvas, key)
        def blocked(cancelled):
            assert get_ident() != owner
            entered.set()
            assert release.wait(5)
            return compute(cancelled)
        return original(scope, key, blocked, memory, **kwargs)
    with pixel_scope(contract):
        image = working_image(values)
        expected = draw(canvas, image, binding, mapping)
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        deferred(canvas)
        monkeypatch.setattr(canvas._effect_jobs, 'request', request)
        monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *a, **k:
            pytest.fail('Deferred capture ran the full field on the GUI'))
        try:
            with pytest.raises(ProjectionPending):
                draw(canvas, image, binding, mapping)
            assert entered.wait(1)
            release.set()
            spin(qapp, lambda: not canvas._effect_jobs.has_running())
            actual = draw(canvas, image, binding, mapping)
            assert actual.format() == expected.format()
            np.testing.assert_array_equal(bytes_of(actual), bytes_of(expected))
            assert actual.devicePixelRatio() == expected.devicePixelRatio()
        finally:
            release.set()


@pytest.mark.parametrize('change', ['paint', 'binding', 'history', 'document'])
def test_obsolete_output_is_not_used_after_current_source_changes(scene, qapp, monkeypatch, change):
    canvas, mask = scene
    image = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(57, 82, 161, 201))
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    entered, release = Event(), Event()
    original = canvas._effect_jobs.request
    calls = []
    def request(scope, key, compute, memory, **kwargs):
        calls.append(key)
        def blocked(cancelled):
            entered.set()
            assert release.wait(5)
            return compute(cancelled)
        return original(scope, key, blocked, memory, **kwargs)
    deferred(canvas)
    monkeypatch.setattr(canvas._effect_jobs, 'request', request)
    try:
        with pytest.raises(ProjectionPending):
            draw(canvas, image, binding)
        assert entered.wait(1)
        if change == 'paint':
            canvas.tiles.paint_dab(mask.mask_id, QPointF(230, 270), 60, QColor('white'))
            mask.touch()
        elif change == 'binding':
            binding.white_value = .31
        elif change == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0) + 1
        else:
            replacement = ChapterDocument.from_dict(canvas.chapter.to_dict())
            replacement.masks[mask.mask_id].paint_offset = (17.25, -.125)
            canvas.set_document(replacement, canvas.tiles, canvas.images)
            deferred(canvas)
        with pytest.raises(ProjectionPending):
            draw(canvas, image, binding)
        assert len(calls) == 2 and calls[0] != calls[1]
        release.set()
        spin(qapp, lambda: not canvas._effect_jobs.has_running())
        actual = draw(canvas, image, binding)
        canvas._projection_defer_effects = False
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        expected = draw(canvas, image, binding)
        np.testing.assert_array_equal(bytes_of(actual), bytes_of(expected))
    finally:
        release.set()


def test_failed_worker_reports_failure_without_gui_field_fallback(scene, qapp, monkeypatch):
    canvas, mask = scene
    image = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.white)
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    deferred(canvas)
    original = canvas._effect_jobs.request
    def request(scope, key, compute, memory, **kwargs):
        def failed(cancelled):
            raise MemoryError('test opacity allocation failed')
        return original(scope, key, failed, memory, **kwargs)
    monkeypatch.setattr(canvas._effect_jobs, 'request', request)
    monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *a, **k: pytest.fail('GUI fallback'))
    with pytest.raises(ProjectionPending):
        draw(canvas, image, binding)
    spin(qapp, lambda: not canvas._effect_jobs.has_running())
    with pytest.raises(ProjectionFailed, match='test opacity allocation failed'):
        draw(canvas, image, binding)


def test_snapshot_pins_selected_cold_revision_without_waiting_chapter_prefetch(tmp_path):
    store = TileStore()
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.white)
    path = tmp_path / 'first.png'
    assert image.save(str(path))
    owner = store._object_tiles('mask')
    owner.register((0, 0), path)
    owner.register((4, 4), path)
    class Unfinished:
        def done(self):
            return False
        def result(self):
            pytest.fail('Visible mask waited for unrelated chapter prefetch')
    store._snapshot_future = Unfinished()
    snapshot = store.detached_snapshot({'mask'}, wait_for_prefetch=False, selected_keys={'mask': {(0, 0)}})
    assert store.residency.decodes == 0
    assert tuple(snapshot._tiles['mask']) == ((0, 0),)
    replacement = tmp_path / 'replacement.png'
    image.fill(Qt.black)
    assert image.save(str(replacement))
    replacement.replace(path)
    np.testing.assert_array_equal(bytes_of(snapshot.tile('mask', (0, 0))),
        np.full(256 * 256 * 4, 255, np.uint8))


def test_actual_statistics_yields_paint_opacity_and_matches_full_sample_oracle(editor, qapp, monkeypatch):
    canvas, controls, raster = editor
    source = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(180, 52, 25, 213))
    image = canvas.chapter.add_object(raster.parent_layer_id, ImageObject(pixel_width=256, pixel_height=256,
        source_filename='paint.png', transform_frame=(0, 0, 256, 256),
        transform_quad=[(0, 0), (256, 0), (256, 256), (0, 256)]))
    canvas.images.put_decoded(image.object_id, 'paint.png', b'test', source, 'image/png')
    canvas.chapter.add_modifier(DistortModifier(parameters={'angle': 40}, frame=(0, 0, 256, 256),
        center=(128, 128), radius=110), [('object', image.object_id)])
    canvas.set_selection('object', image.object_id)
    mask = ToneMask()
    canvas.chapter.masks[mask.mask_id] = mask
    canvas.tiles.paint_dab(mask.mask_id, QPointF(135, 122), 177, QColor(255, 255, 255, 196))
    image.opacity_mask = ParameterMaskBinding(mask.mask_id, .13, .97)
    reference = PosterizeSampler()
    expected = reference.sample(canvas, controls.targets())
    expected_pixels = [part[0].copy() for part in reference._samples]
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    entered, release = Event(), Event()
    keys = []
    original = canvas._effect_jobs.request
    def request(scope, key, compute, memory, **kwargs):
        keys.append(key)
        def blocked(cancelled):
            entered.set()
            assert release.wait(5)
            return compute(cancelled)
        return original(scope, key, blocked, memory, **kwargs)
    monkeypatch.setattr(canvas._effect_jobs, 'request', request)
    monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *a, **k: pytest.fail('GUI field'))
    sampler = PosterizeSampler()
    pending = PosterizeSampleRequest(canvas, controls.targets(), sampler=sampler, parent=controls)
    finished = []
    pending.finished.connect(finished.append)
    try:
        pending.start()
        assert entered.wait(1) and pending.active and not sampler._samples
        release.set()
        spin(qapp, lambda: not pending.active)
        assert finished
        assert any(key[0] == 'opacity-output-preview-handoff' for key in keys)
        np.testing.assert_array_equal(finished[0].counts, expected.counts)
        for part, pixels in zip(sampler._samples, expected_pixels):
            np.testing.assert_array_equal(part[0], pixels)
    finally:
        release.set()


def test_exact_live_cancellation_discards_old_mask_output(scene, qapp, monkeypatch):
    canvas, mask = scene
    source = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.white)
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    deferred(canvas)
    entered, release = Event(), Event()
    original = canvas._effect_jobs.request
    def request(scope, key, compute, memory, **kwargs):
        def blocked(cancelled):
            entered.set()
            assert release.wait(5)
            return compute(cancelled)
        return original(scope, key, blocked, memory, **kwargs)
    monkeypatch.setattr(canvas._effect_jobs, 'request', request)
    try:
        with pytest.raises(ProjectionPending):
            draw(canvas, source, binding)
        assert entered.wait(1)
        assert canvas._effect_jobs.cancel_exact()
        release.set()
        spin(qapp, lambda: not canvas._effect_jobs.has_running())
        assert canvas._effect_jobs.completed == 0
        assert not canvas._effect_jobs.retained
        with pytest.raises(ProjectionPending):
            draw(canvas, source, binding)
        spin(qapp, lambda: not canvas._effect_jobs.has_running())
        assert draw(canvas, source, binding) is not None
    finally:
        release.set()


def test_reentrant_mask_change_cannot_adopt_completed_old_output(scene, qapp, monkeypatch):
    canvas, mask = scene
    source = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.white)
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    deferred(canvas)
    canvas._effect_jobs.timer.stop()
    with pytest.raises(ProjectionPending):
        draw(canvas, source, binding)
    deadline = time.monotonic() + 5
    while not canvas._effect_jobs.has_finished and time.monotonic() < deadline:
        time.sleep(.002)
    assert canvas._effect_jobs.has_finished
    canvas._effect_jobs.timer.stop()
    canvas.derivedResultReady.connect(lambda *_: setattr(binding, 'white_value', .37))
    with pytest.raises(ProjectionPending):
        draw(canvas, source, binding)


def test_oversized_mask_output_waits_for_other_native_job_without_inline_work(scene, qapp, monkeypatch):
    canvas, mask = scene
    source = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    source.fill(Qt.white)
    binding = ParameterMaskBinding(mask.mask_id, 0, 1)
    deferred(canvas)
    canvas._effect_jobs.budget = 1024
    entered, release = Event(), Event()
    def occupying(cancelled):
        entered.set()
        assert release.wait(5)
        result = QImage(1, 1, QImage.Format_ARGB32_Premultiplied)
        result.fill(Qt.black)
        return result
    canvas._effect_jobs.request('unrelated', ('unrelated',), occupying, 64)
    assert entered.wait(1)
    monkeypatch.setattr(canvas, 'render_tone_mask_field', lambda *a, **k: pytest.fail('inline field'))
    try:
        with pytest.raises(ProjectionPending) as pending:
            draw(canvas, source, binding)
        assert canvas._effect_jobs.waiting[pending.value.scope] == pending.value.key
        assert len(canvas._effect_jobs.running_jobs) == 1
        release.set()
        spin(qapp, lambda: not canvas._effect_jobs.has_running())
        with pytest.raises(ProjectionPending):
            draw(canvas, source, binding)
        spin(qapp, lambda: not canvas._effect_jobs.has_running())
        assert draw(canvas, source, binding) is not None
    finally:
        release.set()


def test_identical_source_mask_cache_respects_working_pixel_contract(scene):
    canvas, mask = scene
    source = QImage(288, 320, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor(41, 75, 139, 219))
    binding = ParameterMaskBinding(mask.mask_id, .17, .89)
    with pixel_scope(LEGACY_PIXELS):
        legacy = draw(canvas, source, binding)
    contract = PixelContract(version=2, precision='float32', working_space='linear_srgb')
    canvas.chapter.pixel_contract = contract
    with pixel_scope(contract):
        actual = draw(canvas, source, binding)
        assert actual.format() == contract.image_format
        canvas._modifier_render_cache.clear()
        canvas._modifier_render_cache_bytes = 0
        expected = draw(canvas, source, binding)
        np.testing.assert_array_equal(bytes_of(actual), bytes_of(expected))
    assert legacy.format() == QImage.Format_ARGB32_Premultiplied
