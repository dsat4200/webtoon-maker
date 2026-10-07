"""Integration regressions for the unapplied acquired-source proposal.

Copy to tests/test_acquired_source_preview.py only after the proposal is applied.
These tests use the ordinary PNG decoder and ImageStore; no experiment hooks.
The native GPU twelve-current-frame/three-commit oracle remains a separate gate.
"""
from io import BytesIO
from types import SimpleNamespace

import pytest
from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.images import ImageSource, ImageStore
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope
from comic_editor.render.service import RenderQuality, RenderRequest
from comic_editor.ui import acquired_source_preview as preview
from comic_editor.ui.async_projection import ProjectionFailed, ProjectionPending
from comic_editor.ui.cache_dependencies import exact_cache_allowed
from comic_editor.ui.scene_render_backend import CanvasSceneBackend, source_capture_key
from comic_editor.ui import source_images
from test_deferred_source_images import (canvas as source_canvas, cold_source, blocked_decoder, finish)
from test_navigator_patterns import canvas as render_canvas


def payload(color='red', size=(512, 344)):
    result = BytesIO()
    Image.new('RGBA', size, color).save(result, 'PNG')
    return result.getvalue()


@pytest.fixture
def owner(qapp):
    store = ImageStore()
    store.put('art', 'original.png', payload(), 'image/png')
    # Generic/put-decoded admissions are deliberately not proof of a decode.
    store._forget_decoded('art')
    normal = store.image('art')
    obj = SimpleNamespace(object_type='image', is_blender_linked=False,
        pixel_width=normal.width(), pixel_height=normal.height())
    canvas = SimpleNamespace(images=store, chapter=SimpleNamespace(
        objects={'art': obj}, pixel_contract=LEGACY_PIXELS), selected_object_id='art',
        _history_generation=0, _acquired_preview_presentation_owner=True,
        _bounded_effect_preview=True, _projection_exact=False, _interactive_render=True,
        _effect_preview_channel='canvas', _render_base_alpha=False,
        _rendering_mask_contributor=0, _rendering_halftone_source=False,
        _render_cage_source=False, _tiling_capture_geometry=None)
    return canvas


def test_actual_decode_bound_profile_budget_and_cow(owner):
    store = owner.images
    original = store.image('art')
    original_source = store.source('art')
    image = preview.cached_or_acquired(owner, 'art')
    assert image is not None and max(image.width(), image.height()) <= 256
    assert image.width() * image.height() <= 32768
    assert image.format() == original.format() == QImage.Format_ARGB32_Premultiplied
    assert image.colorSpace() == original.colorSpace()
    key = preview.context(owner)['key']
    assert key in store._decoded and store.decoded_budget == 256 * 1024 * 1024
    assert store.decoded_bytes == sum(int(frame.sizeInBytes()) for frame in store._decoded.values())
    assert store.decoded_bytes <= store.decoded_budget
    pixel, identity = store._decoded[key].pixelColor(0, 0), int(store._decoded[key].cacheKey())
    image.setPixelColor(0, 0, QColor('blue'))
    assert store._decoded[key].pixelColor(0, 0) == pixel
    assert int(store._decoded[key].cacheKey()) == identity != int(image.cacheKey())
    assert store.source('art') is original_source
    assert original.width() == owner.chapter.objects['art'].pixel_width == 512


def test_same_lru_pressure_retains_small_representation_without_decode(owner, monkeypatch):
    store = owner.images
    native = store.cached_image('art')
    first = preview.cached_or_acquired(owner, 'art')
    key = preview.context(owner)['key']
    store.decoded_budget = native.sizeInBytes() + first.sizeInBytes() + 1
    store._cache_decoded('pressure', QImage(native))
    assert 'art' not in store._decoded and key in store._decoded
    monkeypatch.setattr(ImageStore, '_decode', staticmethod(lambda *_: pytest.fail('preview decoded source')))
    second = preview.cached_or_acquired(owner, 'art')
    assert second.cacheKey() == first.cacheKey()
    assert store.decoded_bytes <= store.decoded_budget


def test_missing_metadata_after_64_resident_decodes_uses_ordinary_fallback(owner):
    store = owner.images
    preview.cached_or_acquired(owner, 'art')
    for index in range(66):
        identifier = f'other-{index}'
        store.put(identifier, identifier + '.png', payload(size=(4, 4)))
        store._forget_decoded(identifier)
        store.image(identifier)
    assert 'art' in store._decoded and len(store._display_decode_provenance) == 64
    assert store.display_decode_provenance('art') is None
    assert preview.cached_or_acquired(owner, 'art') is None
    assert store.image('art').pixelColor(0, 0) == QColor('red')
    assert not any(isinstance(key, tuple) and key[:3] == ('acquired-source-preview', id(owner), 'art')
        for key in store._decoded)


@pytest.mark.parametrize('change', ['pixels', 'dimensions', 'profile', 'generic-copy'])
def test_resident_original_mutation_cannot_return_stale_preview(owner, change):
    store = owner.images
    preview.cached_or_acquired(owner, 'art')
    old_key = preview.context(owner)['key']
    resident = store._decoded['art']
    if change == 'pixels':
        resident.setPixelColor(0, 0, QColor('blue'))
    elif change == 'dimensions':
        store._cache_decoded('art', resident.scaled(32, 32))
    elif change == 'profile':
        resident.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    else:
        store._cache_decoded('art', QImage(resident))
    assert preview.cached_or_acquired(owner, 'art') is None
    assert old_key not in store._decoded


@pytest.mark.parametrize('change', ['history', 'generation', 'source', 'chapter', 'selection'])
def test_context_changes_cannot_relabel_previous_representation(owner, change):
    preview.cached_or_acquired(owner, 'art')
    old = preview.context(owner)
    if change == 'history':
        owner._history_generation += 1
    elif change == 'generation':
        owner.images._decode_generation += 1
    elif change == 'source':
        source = owner.images.source('art')
        owner.images._sources['art'] = ImageSource(source.filename, source.mime_type, source._encoded)
    elif change == 'chapter':
        owner.chapter = SimpleNamespace(objects=owner.chapter.objects, pixel_contract=LEGACY_PIXELS)
    else:
        owner.selected_object_id = 'other'
    current = preview.context(owner, 'art')
    assert current is None or current['key'] != old['key']


@pytest.mark.parametrize('name,value', [
    ('_projection_exact', True), ('_render_base_alpha', True), ('_rendering_mask_contributor', 1),
    ('_rendering_halftone_source', True), ('_render_cage_source', True),
    ('_rendering_compound_references', True), ('_rendering_outward_gradient', True),
    ('_posterize_statistics_capture', True), ('_tiling_capture_geometry', (1,)),
    ('_bounded_effect_preview', False), ('_interactive_render', False),
    ('_effect_preview_channel', 'navigator'), ('_acquired_preview_presentation_owner', False),
])
def test_native_detached_and_contributor_routes_opt_out(owner, name, value):
    setattr(owner, name, value)
    assert preview.context(owner) is None
    assert preview.cached_or_acquired(owner, 'art') is None


@pytest.mark.parametrize('document,ambient', [(LEGACY_PIXELS, FLOAT_PIXELS),
    (FLOAT_PIXELS, LEGACY_PIXELS), (FLOAT_PIXELS, FLOAT_PIXELS)])
def test_float_actual_or_scoped_precision_preserves_original_route(owner, document, ambient):
    owner.chapter.pixel_contract = document
    with pixel_scope(ambient):
        assert preview.context(owner) is None
        assert preview.cached_or_acquired(owner, 'art') is None


def test_recursive_preview_key_rejected_even_under_detached_exact_probe(owner):
    key = source_capture_key(owner, ('existing-source', 'art'))
    probe = SimpleNamespace(_projection_exact=True, _projection_has_live_preview=lambda: False,
        _effect_preview_channel='canvas')
    assert not exact_cache_allowed(probe, key)
    assert not exact_cache_allowed(probe, ('ordinary-effect', ('nested', key)))


def test_reentrant_history_during_resampling_has_no_stale_admission(owner, monkeypatch):
    original = preview.context
    calls = 0
    def reentrant(*args):
        nonlocal calls
        calls += 1
        if calls == 2:
            owner._history_generation += 1
        return original(*args)
    monkeypatch.setattr(preview, 'context', reentrant)
    with pytest.raises(ProjectionPending):
        preview.cached_or_acquired(owner, 'art')
    assert not any(isinstance(key, tuple) and key[:1] == ('acquired-source-preview',)
        for key in owner.images._decoded)


def test_source_generation_restore_clears_all_representations(owner):
    preview.cached_or_acquired(owner, 'art')
    before = owner.images._decode_generation
    owner.images.restore(owner.images.snapshot())
    assert owner.images._decode_generation > before
    assert not owner.images._decoded and owner.images.decoded_bytes == 0
    assert not owner.images._display_decode_provenance


def test_changed_pin_stamp_during_ordinary_decode_does_not_create_trust(owner, monkeypatch):
    store = owner.images
    store._forget_decoded('art')
    original = store._source_decode_stamp
    calls = 0
    def changed(encoded):
        nonlocal calls
        calls += 1
        value = original(encoded)
        return (*value[:3], value[3] + (calls > 1))
    monkeypatch.setattr(store, '_source_decode_stamp', changed)
    store.image('art')
    assert store.display_decode_provenance('art') is None
    assert preview.cached_or_acquired(owner, 'art') is None


def test_stamped_worker_adoption_trusts_only_current_source(owner):
    store, source = owner.images, owner.images.source('art')
    frame = store.image('art')
    stamp = store._source_decode_stamp(source._encoded)
    store._forget_decoded('art')
    assert store.adopt_decoded('art', source._encoded, store._decode_generation, frame, source_stamp=stamp)
    assert store.display_decode_provenance('art') is not None
    assert not store.adopt_decoded('art', source._encoded, store._decode_generation, frame,
        source_stamp=(*stamp[:3], stamp[3] + 1))
    store._forget_decoded('art')
    assert store.adopt_decoded('art', source._encoded, store._decode_generation, frame)
    assert store.display_decode_provenance('art') is None


@pytest.mark.parametrize('error', [ProjectionFailed(('deleted-pin',), None, 'missing'), RuntimeError('reentrant')])
def test_backend_capture_restores_every_flag_when_context_throws(owner, monkeypatch, error):
    class WeakOwner(SimpleNamespace):
        pass
    canvas = WeakOwner(**owner.__dict__)
    names = ('_interactive_render', '_effect_viewport_world', '_vector_render_scale_override',
        '_effect_region_requests', '_projection_tile_key', '_effect_preview_channel',
        '_projection_exact', '_projection_defer_effects', '_bounded_effect_preview',
        '_stroke_projection_active', '_live_underlay_object_id', '_live_underlay_amount')
    absent = object()
    previous = {name: getattr(canvas, name, absent) for name in names}
    def fail(*args):
        raise error
    monkeypatch.setattr(preview, 'context', fail)
    document = SimpleNamespace(underlay=('', 0.), live_preview=True)
    request = RenderRequest((0., 0., 16., 16.), 1., (16, 16), ('owned-preview',), 1,
        quality=RenderQuality.INTERACTIVE, defer_effects=False)
    with pytest.raises(type(error)):
        with CanvasSceneBackend(canvas).capture(document, request, QRectF(0, 0, 16, 16)):
            pytest.fail('failed context yielded')
    for name, value in previous.items():
        assert getattr(canvas, name, absent) is value or getattr(canvas, name, absent) == value


def test_explicit_owner_scope_restores_after_exception(owner):
    owner._acquired_preview_presentation_owner = 'previous'
    with pytest.raises(RuntimeError):
        with preview.presentation_scope(owner, False):
            assert preview.context(owner) is None
            raise RuntimeError('cancelled')
    assert owner._acquired_preview_presentation_owner == 'previous'


@pytest.mark.parametrize('interactive', [False, True])
def test_detached_qimage_caller_cannot_impersonate_presentation(owner, interactive):
    from PySide6.QtGui import QPainter
    owner._acquired_preview_presentation_owner = False
    image = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    @preview.widget_presentation
    def paint(canvas, actual, **kwargs):
        assert actual.device() is image
        return preview.context(canvas)
    try:
        assert paint(owner, painter, interactive=interactive) is None
    finally:
        painter.end()
    assert owner._acquired_preview_presentation_owner is False


def test_explicit_owned_raster_scope_enables_only_its_call(owner):
    from PySide6.QtGui import QPainter
    owner._acquired_preview_presentation_owner = False
    image = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    painter = QPainter(image)
    @preview.widget_presentation
    def paint(canvas, actual, **kwargs):
        return preview.context(canvas)
    try:
        with preview.presentation_scope(owner, True):
            assert paint(owner, painter, interactive=True) is not None
        assert paint(owner, painter, interactive=True) is None
    finally:
        painter.end()


def test_missing_source_uses_ordinary_empty_route(owner):
    owner.images.remove('art')
    assert source_images.image_for_render(owner, 'art').isNull()
    assert not owner.images._decoded and owner.images.decoded_bytes == 0


def test_genuinely_cold_owned_source_uses_existing_async_decoder(source_canvas, monkeypatch):
    canvas = source_canvas
    obj = cold_source(canvas)
    canvas.set_selection('object', obj.object_id, activate_default_tool=False)
    canvas._projection_exact = canvas._projection_defer_effects = False
    canvas._bounded_effect_preview = canvas._interactive_render = True
    canvas._effect_preview_channel = 'canvas'
    canvas._acquired_preview_presentation_owner = True
    assert preview.context(canvas) is not None
    entered, release, calls = blocked_decoder(monkeypatch)
    try:
        with pytest.raises(ProjectionPending) as waiting:
            source_images.image_for_render(canvas, obj.object_id)
        assert waiting.value.scope[0] == 'source-image-decode'
        assert entered.wait(2) and canvas.images.cached_image(obj.object_id) is None
        assert not any(isinstance(key, tuple) and key[:1] == ('acquired-source-preview',)
            for key in canvas.images._decoded)
        release.set()
        finish(canvas)
        # First handoff is the ordinary frame; the next owned call can reuse a
        # preview only after its actual guarded original adoption is resident.
        ordinary = source_images.image_for_render(canvas, obj.object_id)
        assert ordinary.width() == 48 and ordinary.height() == 32
        assert canvas.images.display_decode_provenance(obj.object_id) is not None
        current = source_images.image_for_render(canvas, obj.object_id)
        assert not current.isNull() and len(calls) == 1
        from threading import get_ident
        assert calls[0] != get_ident()
        current_context = preview.context(canvas)
        assert current_context['acquired'] is True
        assert current_context['key'] in canvas.images._decoded
        assert current.cacheKey() == canvas.images._decoded[current_context['key']].cacheKey()
    finally:
        release.set()


@pytest.mark.parametrize('change', ['pixels', 'generic-replacement', 'metadata-evicted'])
def test_source_key_changes_before_any_warm_stage_lookup(owner, change):
    store = owner.images
    preview.cached_or_acquired(owner, 'art')
    acquired = source_capture_key(owner, ('existing-source', 'art'))
    if change == 'pixels':
        store._decoded['art'].fill(QColor('blue'))
    elif change == 'generic-replacement':
        frame = QImage(store._decoded['art'])
        frame.fill(QColor('blue'))
        store._cache_decoded('art', frame)
    else:
        store._display_decode_provenance.pop('art')
    fallback = source_capture_key(owner, ('existing-source', 'art'))
    assert fallback != acquired
    assert preview.context(owner)['acquired'] is False
    assert preview.cached_or_acquired(owner, 'art') is None
    probe = SimpleNamespace(_projection_exact=True, _projection_has_live_preview=lambda: False,
        _effect_preview_channel='canvas')
    assert not exact_cache_allowed(probe, fallback)


def test_trusted_representation_key_stable_across_native_eviction_and_completion(owner):
    store = owner.images
    normal = store.image('art')
    preview.cached_or_acquired(owner, 'art')
    acquired = source_capture_key(owner, ('existing-source', 'art'))
    encoded = store.source('art')._encoded
    stamp = store._source_decode_stamp(encoded)
    store._forget_decoded('art')
    assert source_capture_key(owner, ('existing-source', 'art')) == acquired
    assert store.adopt_decoded('art', encoded, store._decode_generation, normal, source_stamp=stamp)
    assert source_capture_key(owner, ('existing-source', 'art')) == acquired


@pytest.mark.parametrize('change', ['pixels', 'generic-replacement'])
def test_actual_warm_mirror_source_and_stage_do_not_reuse_untrusted_pixels(render_canvas, change):
    from comic_editor.core.models import HueSaturationLightnessModifier, ImageObject
    canvas = render_canvas
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=100, y=200, pixel_width=512, pixel_height=344))
    canvas.images.put(obj.object_id, 'original.png', payload(), 'image/png')
    canvas.images._forget_decoded(obj.object_id)
    canvas.images.image(obj.object_id)
    canvas.set_selection('object', obj.object_id, activate_default_tool=False)
    canvas._projection_exact = canvas._projection_defer_effects = False
    canvas._bounded_effect_preview = canvas._interactive_render = True
    canvas._effect_preview_channel = 'canvas'
    modifier = HueSaturationLightnessModifier(hue=30)
    canvas.chapter.add_modifier(modifier, [('object', obj.object_id)])
    def render():
        from comic_editor.render.service import RenderStatus
        document = canvas._render_document_state()
        request = RenderRequest((0., 0., 1200., 900.), .25, (300, 225),
            ('owned-source-regression',), document.revision, quality=RenderQuality.INTERACTIVE,
            defer_effects=False)
        with preview.presentation_scope(canvas, True):
            result = canvas._render_service.render_region(document, request)
        assert result.status is RenderStatus.PROVISIONAL and not result.image.isNull()
        return result.image
    warm = render()
    assert canvas._modifier_source_cache and canvas._modifier_render_cache
    assert warm.pixelColor(50, 80).alpha() > 0, 'Current source must actually cover the observed interior'
    with preview.presentation_scope(canvas, True):
        assert preview.context(canvas)['acquired'] is True
        acquired_key = source_capture_key(canvas, ('warm-source-proof',))
    if change == 'pixels':
        canvas.images._decoded[obj.object_id].fill(QColor('blue'))
    else:
        replacement = QImage(canvas.images._decoded[obj.object_id])
        replacement.fill(QColor('blue'))
        canvas.images._cache_decoded(obj.object_id, replacement)
    with preview.presentation_scope(canvas, True):
        assert preview.context(canvas)['acquired'] is False
        assert source_capture_key(canvas, ('warm-source-proof',)) != acquired_key
    current = render()
    canvas._modifier_source_cache.clear()
    canvas._modifier_source_cache_bytes = 0
    canvas._modifier_render_cache.clear()
    canvas._modifier_render_cache_bytes = 0
    fresh = render()
    assert bytes(current.constBits()) != bytes(warm.constBits()), (
        warm.pixelColor(50, 80).name(), current.pixelColor(50, 80).name(), fresh.pixelColor(50, 80).name(),
        canvas.images._decoded[obj.object_id].pixelColor(0, 0).name(),
        [(str(key)[-500:], frame.pixelColor(frame.width() // 2, frame.height() // 2).name())
            for key, frame in canvas._modifier_source_cache.items()])
    assert bytes(current.constBits()) == bytes(fresh.constBits())


@pytest.mark.parametrize('result_kind', ['success', 'stale', 'throw'])
def test_compact_request_is_bounded_clipped_and_restores_actual_painter(owner, result_kind):
    from PySide6.QtGui import QPainter, QTransform
    from comic_editor.render.service import RenderStatus
    from comic_editor.ui.native_artwork import _paint_compact_live_scene
    requests = []
    def request(document, region):
        requests.append(region)
        if result_kind == 'throw':
            raise RuntimeError('service failed')
        frame = QImage(*region.pixel_size, QImage.Format_ARGB32_Premultiplied)
        frame.fill(QColor('red'))
        return SimpleNamespace(image=frame, status=RenderStatus.EXACT)
    owner._render_service = SimpleNamespace(render_region=request,
        current=lambda document, region: result_kind != 'stale')
    owner._render_document_state = lambda: SimpleNamespace(revision=4)
    owner.scale = 4.
    owner.devicePixelRatioF = lambda: 2.
    owner.camera_transform = lambda: QTransform()
    image = QImage(64, 64, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('transparent'))
    painter = QPainter(image)
    painter.setClipRect(8, 12, 16, 20)
    painter.setTransform(QTransform.fromTranslate(3, 5))
    before_transform, before_hints = painter.worldTransform(), painter.renderHints()
    try:
        if result_kind == 'throw':
            with pytest.raises(RuntimeError):
                _paint_compact_live_scene(owner, painter, QRectF(0, 0, 4000, 3000))
        else:
            _paint_compact_live_scene(owner, painter, QRectF(0, 0, 4000, 3000))
        assert painter.worldTransform() == before_transform and painter.renderHints() == before_hints
        assert len(requests) == 1
        actual = requests[0]
        assert actual.scale <= 1. and (actual.pixel_size[0] - 4) * (actual.pixel_size[1] - 4) <= 1024 * 1024
        assert actual.quality is RenderQuality.INTERACTIVE and actual.defer_effects is False
        assert QRectF(*actual.requested_region).width() < 4000
        assert QRectF(*actual.region).width() > 4000
    finally:
        painter.end()
