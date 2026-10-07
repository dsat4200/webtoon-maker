"""Native source/color precision crosses the actual scene and export edges."""
from dataclasses import replace
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, premultiplied_pixels
from comic_editor.render.service import RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.async_projection import ProjectionPending
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.source_images import image_for_render, _working_representation


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    chapter = ChapterDocument(width=8, height=8, document_kind='asset', background='#00000000')
    page = chapter.add_page('Native source', BoundGeometry.rectangle(0, 0, 8, 8))
    page.fill_color, page.border_width = None, 0
    owner.set_document(chapter, TileStore())
    owner.setUpdatesEnabled(False)
    yield owner
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.close()
    owner.deleteLater()


FORMATS = [
    (QImage.Format_RGBA8888, np.uint8, (128, 64, 32, 1), 255),
    (QImage.Format_RGBA64, np.uint16, (32768, 16384, 8192, 1), 65535),
]


def source(canvas, format=QImage.Format_RGBA64, dtype=np.uint16,
           channels=(32768, 16384, 8192, 1), *, profile=QColorSpace.SRgb):
    image = QImage(1, 1, format)
    np.frombuffer(image.bits(), dtype)[:4] = channels
    image.setColorSpace(QColorSpace(profile))
    raw, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=2, y=3, pixel_width=1, pixel_height=1))
    canvas.images.put(obj.object_id, 'native.png', bytes(raw), 'image/png')
    return obj, bytes(raw), image


def expected(channels, maximum, precision):
    value = np.array(channels, np.float32)/np.float32(maximum)
    value[:3] *= value[3]
    return value.astype(np.float16).astype(np.float32) if precision == 'float16' else value


def export_channels(value):
    straight = value.copy()
    if straight[3] > 0:
        straight[:3] /= straight[3]
    else:
        straight[:3] = 0
    return np.rint(straight*np.float32(65535)).astype(np.uint16)


def exact(canvas, contract, *, deferred=False):
    canvas.chapter.pixel_contract = contract
    config = canvas._projection_configuration()
    canvas._render_service.configure((*config, None), document=config[:3])
    document = canvas._render_document_state()
    request = RenderRequest((0., 0., 8., 8.), 1., (8, 8), ('native-source-proof',),
        document.revision, quality=RenderQuality.EXACT, defer_effects=deferred)
    return canvas._render_service.render_region(document, request)


def finish(canvas):
    for job in canvas._effect_jobs.running_jobs:
        job[3].result(timeout=5)
    canvas._effect_jobs.poll()


@pytest.mark.parametrize('format,dtype,channels,maximum', FORMATS)
@pytest.mark.parametrize('precision', ['float32', 'float16'])
@pytest.mark.parametrize('profile,space', [(QColorSpace.SRgb, 'srgb'), (QColorSpace.SRgbLinear, 'linear_srgb')])
def test_exact_scene_preserves_native_low_alpha_and_known_color_space(
        canvas, monkeypatch, format, dtype, channels, maximum, precision, profile, space):
    obj, raw, original = source(canvas, format, dtype, channels, profile=profile)
    contract = replace(FLOAT_PIXELS, precision=precision, working_space=space)
    native = canvas.images.native_image(obj.object_id)
    original_bytes, native_bytes = bytes(original.constBits()), bytes(native.constBits())
    profile_bytes = bytes(native.colorSpace().iccProfile())
    def forbidden(*_):
        raise AssertionError('Floating scene sources must use original samples, not display pixels')
    monkeypatch.setattr(canvas.images, 'image', forbidden)
    result = exact(canvas, contract)
    assert result.status is RenderStatus.EXACT and result.image.format() == contract.image_format
    np.testing.assert_array_equal(premultiplied_pixels(result.image)[3, 2], expected(channels, maximum, precision))
    working = canvas.images.cached_working_image(obj.object_id, _working_representation(contract))
    assert working is not None and working.format() == contract.image_format
    # Repeating a native capture reuses the converted source. Neither decoding
    # nor conversion may occur again merely because the projection tile moves.
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(forbidden))
    monkeypatch.setattr('comic_editor.render.pixels.import_image', forbidden)
    repeat = exact(canvas, contract)
    assert bytes(repeat.image.constBits()) == bytes(result.image.constBits())
    assert canvas.images.source(obj.object_id).data == raw
    assert bytes(original.constBits()) == original_bytes
    assert bytes(canvas.images.native_image(obj.object_id).constBits()) == native_bytes
    assert bytes(canvas.images.native_image(obj.object_id).colorSpace().iccProfile()) == profile_bytes
    assert canvas.images.decoded_bytes <= canvas.images.decoded_budget


@pytest.mark.parametrize('format,dtype,channels,maximum', FORMATS)
@pytest.mark.parametrize('precision', ['float32', 'float16'])
@pytest.mark.parametrize('crop', [False, True])
def test_application_native_export_quantizes_straight_color_once_and_restores_editing_context(
        canvas, format, dtype, channels, maximum, precision, crop):
    obj, raw, original = source(canvas, format, dtype, channels)
    canvas.chapter.pixel_contract = replace(FLOAT_PIXELS, precision=precision)
    if crop:
        canvas.chapter.export_rect_enabled = True
        canvas.chapter.export_rect = (1., 2., 3., 3.)
    canvas._interactive_render = True
    exported = canvas.render_export_image()
    assert canvas._interactive_render and exported.format() == QImage.Format_RGBA64
    assert exported.colorSpace() == QColorSpace(QColorSpace.SRgb)
    rows = np.frombuffer(exported.constBits(), np.uint16).reshape(exported.height(), exported.bytesPerLine()//2)
    point = (1, 1) if crop else (2, 3)
    x, y = point
    np.testing.assert_array_equal(rows[y, 4*x:4*x+4], export_channels(expected(channels, maximum, precision)))
    # PNG encode/decode must preserve these native straight RGBA16 channels.
    payload, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert exported.save(buffer, 'PNG')
    buffer.close()
    decoded, _ = ImageStore._decode_native(bytes(payload))
    assert decoded.format() == QImage.Format_RGBA64
    assert bytes(decoded.constBits()) == bytes(exported.constBits())
    assert decoded.colorSpace() == exported.colorSpace()
    assert canvas.images.source(obj.object_id).data == raw
    assert canvas.images.native_image(obj.object_id).colorSpace() == original.colorSpace()


@pytest.mark.parametrize('precision', ['float32', 'float16'])
@pytest.mark.parametrize('warm_native', [False, True])
def test_deferred_float_source_decode_and_color_conversion_run_off_owner_thread(canvas, monkeypatch, precision, warm_native):
    obj, raw, _ = source(canvas)
    contract = replace(FLOAT_PIXELS, precision=precision)
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    if warm_native:
        canvas.images.native_image(obj.object_id)
    decode = ImageStore._decode_native
    from comic_editor.render.pixels import import_image
    owner, entered, release, decoding, conversion = get_ident(), Event(), Event(), [], []
    def record_decode(data):
        decoding.append(get_ident())
        return decode(data)
    def blocked_convert(image, policy, **kwargs):
        conversion.append(get_ident())
        assert get_ident() != owner
        entered.set()
        assert release.wait(5)
        return import_image(image, policy, **kwargs)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(record_decode))
    monkeypatch.setattr('comic_editor.render.pixels.import_image', blocked_convert)
    try:
        pending = exact(canvas, contract, deferred=True)
        assert pending.status is RenderStatus.PENDING and pending.image.isNull()
        assert entered.wait(2) and conversion == [conversion[0]]
        assert conversion[0] != owner and all(thread != owner for thread in decoding)
        assert len(decoding) == (0 if warm_native else 1)
        assert canvas._effect_jobs.bytes_in_flight >= 128+len(raw)
        release.set()
        finish(canvas)
        result = exact(canvas, contract, deferred=True)
        assert result.status is RenderStatus.EXACT
        np.testing.assert_array_equal(premultiplied_pixels(result.image)[3, 2],
            expected((32768, 16384, 8192, 1), 65535, precision))
        assert canvas._effect_jobs.submitted == 1
        assert canvas.images.source(obj.object_id).data == raw
    finally:
        release.set()


def test_float_working_handoff_above_store_budget_is_reused_and_borrowers_detach(canvas):
    obj, raw, _ = source(canvas)
    canvas.chapter.pixel_contract = FLOAT_PIXELS
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    canvas.images.decoded_budget = 1
    canvas._interactive_render = canvas._projection_exact = canvas._projection_defer_effects = True
    with pixel_scope(FLOAT_PIXELS):
        with pytest.raises(ProjectionPending):
            image_for_render(canvas, obj.object_id)
        finish(canvas)
        borrowed = image_for_render(canvas, obj.object_id)
        original = bytes(borrowed.constBits())
        assert canvas.images.cached_working_image(obj.object_id, _working_representation(FLOAT_PIXELS)) is None
        borrowed.fill(0)
        assert bytes(image_for_render(canvas, obj.object_id).constBits()) == original
        assert canvas._effect_jobs.submitted == 1
        assert canvas.images.decoded_bytes == 0 and canvas.images.source(obj.object_id).data == raw


@pytest.mark.parametrize('change', ['source', 'history', 'stamp', 'contract'])
def test_float_ready_handoff_rejects_reentrant_source_or_capture_changes(canvas, monkeypatch, change):
    import os
    obj, raw, _ = source(canvas)
    canvas.chapter.pixel_contract = FLOAT_PIXELS
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    canvas._interactive_render = canvas._projection_exact = canvas._projection_defer_effects = True
    from comic_editor.render.pixels import import_image
    entered, release = Event(), Event()
    def blocked(image, contract, **kwargs):
        entered.set()
        assert release.wait(5)
        return import_image(image, contract, **kwargs)
    monkeypatch.setattr('comic_editor.render.pixels.import_image', blocked)
    def changed(*_):
        if change == 'source':
            canvas.images.put(obj.object_id, 'replaced.png', raw)
        elif change == 'history':
            canvas._history_generation = getattr(canvas, '_history_generation', 0)+1
        elif change == 'contract':
            canvas.chapter.pixel_contract = replace(FLOAT_PIXELS, precision='float16')
        else:
            path = canvas.images.source(obj.object_id)._encoded.pin.path
            stat = path.stat()
            os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns+1_000_000))
    try:
        with pixel_scope(FLOAT_PIXELS):
            with pytest.raises(ProjectionPending):
                image_for_render(canvas, obj.object_id)
            assert entered.wait(2)
            canvas.derivedResultReady.connect(changed)
            release.set()
            canvas._effect_jobs.running[3].result(timeout=5)
            with pytest.raises(ProjectionPending):
                image_for_render(canvas, obj.object_id)
            canvas.derivedResultReady.disconnect(changed)
            assert canvas.images.cached_working_image(obj.object_id, _working_representation(FLOAT_PIXELS)) is None
    finally:
        release.set()


def test_scene_rejects_ready_source_when_callback_changes_actual_document_contract(canvas):
    obj, raw, _ = source(canvas)
    canvas.images._decoded.clear()
    canvas.images.decoded_bytes = 0
    pending = exact(canvas, FLOAT_PIXELS, deferred=True)
    assert pending.status is RenderStatus.PENDING
    canvas._effect_jobs.running[3].result(timeout=5)
    original_history = getattr(canvas, '_history_generation', 0)
    new_contract = replace(FLOAT_PIXELS, precision='float16')
    def changed(*_):
        canvas.chapter.pixel_contract = new_contract
    canvas.derivedResultReady.connect(changed)
    try:
        stale = exact(canvas, FLOAT_PIXELS, deferred=True)
    finally:
        canvas.derivedResultReady.disconnect(changed)
    assert stale.status is RenderStatus.STALE and stale.image.isNull()
    assert canvas.images.cached_working_image(obj.object_id, _working_representation(FLOAT_PIXELS)) is None
    assert getattr(canvas, '_history_generation', 0) == original_history
    assert canvas.images.source(obj.object_id).data == raw
    fresh = exact(canvas, new_contract, deferred=True)
    if fresh.status is RenderStatus.PENDING:
        finish(canvas)
        fresh = exact(canvas, new_contract, deferred=True)
    assert fresh.status is RenderStatus.EXACT and fresh.image.format() == new_contract.image_format
    np.testing.assert_array_equal(premultiplied_pixels(fresh.image)[3, 2],
        expected((32768, 16384, 8192, 1), 65535, 'float16'))


def test_working_contract_variants_share_existing_budget_and_source_replacement_clears_them(canvas):
    obj, raw, _ = source(canvas)
    initial_native = bytes(canvas.images.native_image(obj.object_id).constBits())
    variants = [FLOAT_PIXELS, replace(FLOAT_PIXELS, precision='float16'),
                replace(FLOAT_PIXELS, working_space='linear_srgb')]
    canvas._interactive_render = False
    for policy in variants:
        canvas.chapter.pixel_contract = policy
        with pixel_scope(policy):
            result = image_for_render(canvas, obj.object_id)
            assert result.format() == policy.image_format
    assert sum(key[:2] == ('working', obj.object_id) for key in canvas.images._decoded if isinstance(key, tuple)) == 3
    with pixel_scope(LEGACY_PIXELS):
        assert image_for_render(canvas, obj.object_id).format() == QImage.Format_ARGB32_Premultiplied
    assert bytes(canvas.images.native_image(obj.object_id).constBits()) == initial_native
    assert canvas.images.decoded_bytes <= canvas.images.decoded_budget
    snapshot = canvas.images.snapshot()
    canvas.images.put(obj.object_id, 'replacement.png', raw)
    assert not any(key[:2] == ('working', obj.object_id) for key in canvas.images._decoded if isinstance(key, tuple))
    with pixel_scope(FLOAT_PIXELS):
        image_for_render(canvas, obj.object_id)
    canvas.images.restore(snapshot)
    assert not canvas.images._decoded and canvas.images.decoded_bytes == 0


def test_legacy_document_temporary_float_scope_keeps_original_display_source_semantics(canvas, monkeypatch):
    obj, raw, _ = source(canvas)
    assert canvas.chapter.pixel_contract == LEGACY_PIXELS
    baseline = canvas.images.image(obj.object_id)
    def forbidden(*_):
        raise AssertionError('A temporary float effect stage must not migrate a legacy source contract')
    monkeypatch.setattr('comic_editor.ui.source_images._working_image_for_render', forbidden)
    with pixel_scope(FLOAT_PIXELS):
        actual = image_for_render(canvas, obj.object_id)
    assert actual.format() == baseline.format()
    assert bytes(actual.constBits()) == bytes(baseline.constBits())
    assert canvas._effect_jobs.submitted == 0
    assert not any(key[:1] == ('working',) for key in canvas.images._decoded if isinstance(key, tuple))
    assert canvas.images.source(obj.object_id).data == raw


@pytest.mark.parametrize('profile,working', [(QColorSpace.SRgb, 'linear_srgb'), (QColorSpace.SRgbLinear, 'srgb')])
def test_scene_applies_source_profile_before_composition_with_native_low_alpha(canvas, profile, working):
    _obj, _raw, _original = source(canvas, profile=profile)
    values = np.array((32768, 16384, 8192, 1), np.float32)/np.float32(65535)
    rgb = values[:3]
    if working == 'linear_srgb':
        rgb = np.where(rgb <= .04045, rgb/12.92, ((rgb+.055)/1.055)**2.4)
    else:
        rgb = np.where(rgb <= .0031308, rgb*12.92, 1.055*rgb**(1/2.4)-.055)
    required = np.concatenate((rgb*values[3], values[3:])).astype(np.float32)
    result = exact(canvas, replace(FLOAT_PIXELS, working_space=working))
    assert result.status is RenderStatus.EXACT
    actual = premultiplied_pixels(result.image)[3, 2]
    np.testing.assert_allclose(actual, required, atol=1e-9, rtol=0)
    assert actual[3] == values[3]


@pytest.mark.parametrize('precision', ['float32', 'float16'])
def test_scene_keeps_already_native_float_hdr_color_and_coverage(canvas, precision):
    from comic_editor.render.pixels import working_image
    values = np.array([[[1.12345, -.07321, .00071, .31739]]], np.float32)
    original = working_image(values, FLOAT_PIXELS)
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(x=2, y=3, pixel_width=1, pixel_height=1))
    canvas.images.put_decoded(obj.object_id, 'trusted-hdr', b'', original)
    before = bytes(original.constBits())
    result = exact(canvas, replace(FLOAT_PIXELS, precision=precision))
    required = values[0, 0] if precision == 'float32' else values[0, 0].astype(np.float16).astype(np.float32)
    assert result.status is RenderStatus.EXACT
    np.testing.assert_array_equal(premultiplied_pixels(result.image)[3, 2], required)
    assert bytes(canvas.images.native_image(obj.object_id).constBits()) == before
