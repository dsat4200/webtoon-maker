from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtGui import QImage, QColorSpace

from comic_editor.render.pixels import (
    FLOAT_PIXELS, LEGACY_PIXELS, PixelContract, current_contract, pixel_scope,
    working_image, premultiplied_pixels, import_image, display_image,
    export_image, transform_pixels, color_processor, gpu_color_processor,
)


def test_legacy_quantization_and_scope_restoration():
    pixels = np.array([[[.127, .231, .333, .51], [0, 0, 0, 0]]], np.float32)
    expected = np.clip(pixels * 255, 0, 255).astype(np.uint8)
    image = working_image(pixels)
    rgba = image.convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    assert bytes(rgba.constBits()) == expected.tobytes()
    with pytest.raises(RuntimeError), pixel_scope(FLOAT_PIXELS):
        assert current_contract() == FLOAT_PIXELS
        raise RuntimeError('failed capture')
    assert current_contract() == LEGACY_PIXELS
    for settings in ({'precision': 'float32'}, {'working_space': 'linear_srgb'},
                     {'export_space': 'linear_srgb'}, {'ocio_config': 'custom.ocio'}):
        with pytest.raises(ValueError):
            PixelContract(**settings)


@pytest.mark.parametrize('precision,tolerance', [('float32', 0), ('float16', .0005)])
def test_float_storage_preserves_hdr_coverage_and_subbyte_color(precision, tolerance):
    contract = replace(FLOAT_PIXELS, precision=precision)
    pixels = np.array([[[1.12345, -.05, .0007, .25], [.31, .29, .23, .65]]], np.float32)
    image = working_image(pixels, contract)
    assert image.format() == contract.image_format
    assert np.allclose(premultiplied_pixels(image), pixels, atol=tolerance, rtol=0)
    detached = premultiplied_pixels(image)
    detached[:] = 0
    assert premultiplied_pixels(image)[0, 0, 0] > 1
    expected = pixels.copy()
    pixels[:] = 0
    import gc
    gc.collect()
    assert np.allclose(premultiplied_pixels(image), expected, atol=tolerance, rtol=0)


def test_16_bit_source_does_not_round_through_bytes():
    pixels = np.array([[[.400049, .500058, .600069, 1]]], np.float32)
    source = working_image(pixels, FLOAT_PIXELS).convertToFormat(QImage.Format_RGBA64)
    imported = import_image(source, FLOAT_PIXELS)
    actual = premultiplied_pixels(imported)
    assert np.allclose(actual, pixels, atol=1/65535, rtol=0)
    assert abs(actual[0,0,0] * 255 - round(actual[0,0,0] * 255)) > .0001


@pytest.mark.parametrize('format,dtype,channels,maximum', [
    (QImage.Format_RGBA8888, np.uint8, (128, 64, 32, 1), 255),
    (QImage.Format_RGBA64, np.uint16, (32768, 16384, 8192, 1), 65535),
])
def test_straight_source_alpha_is_multiplied_after_entering_float_storage(format, dtype, channels, maximum):
    source = QImage(1, 1, format)
    np.frombuffer(source.bits(), dtype=dtype)[:4] = channels
    expected = np.array(channels, np.float32)/np.float32(maximum)
    expected[:3] *= expected[3]
    result = import_image(source, FLOAT_PIXELS)
    np.testing.assert_allclose(premultiplied_pixels(result)[0, 0], expected, atol=1e-9, rtol=0)
    np.testing.assert_array_equal(np.frombuffer(source.constBits(), dtype=dtype)[:4], channels)


def test_known_linear_profile_keeps_hdr_samples_without_a_gamma_round_trip():
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb')
    values = np.array([[[1.12345, -.07321, .00071, .31739]]], np.float32)
    source = working_image(values, contract)
    source.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    np.testing.assert_array_equal(premultiplied_pixels(import_image(source, contract)), values)


def test_display_and_integer_export_clip_hdr_color_without_changing_working_pixels():
    values = np.array([[[1.12345, -.07321, .00071, .31739]]], np.float32)
    image = working_image(values, FLOAT_PIXELS)
    displayed = display_image(image, FLOAT_PIXELS).convertToFormat(QImage.Format_RGBA8888_Premultiplied)
    channels = np.frombuffer(displayed.constBits(), np.uint8)[:4]
    assert np.all(channels[:3] <= channels[3])
    assert channels[0] == channels[3] and channels[1] == 0
    exported = export_image(image, FLOAT_PIXELS)
    actual = premultiplied_pixels(exported)[0, 0]
    assert np.all(actual[:3] <= actual[3])
    assert actual[0] == actual[3] and actual[1] == 0
    np.testing.assert_array_equal(premultiplied_pixels(image), values)


@pytest.mark.parametrize('settings', [{'version': True}, {'version': 2.},
    {'version': '2'}, {'working_space': 1}, {'display': False}, {'ocio_config': []}])
def test_pixel_contract_rejects_ambiguous_field_types(settings):
    with pytest.raises(ValueError):
        PixelContract.from_dict(settings)


def test_pixel_contract_signature_is_cached_immutable_and_not_serialized():
    assert FLOAT_PIXELS.signature is FLOAT_PIXELS.signature
    assert isinstance(FLOAT_PIXELS.signature, tuple)
    assert '_signature' not in FLOAT_PIXELS.to_dict()
    other = replace(FLOAT_PIXELS, working_space='linear_srgb')
    assert other.signature != FLOAT_PIXELS.signature
    assert PixelContract.from_dict(other.to_dict()).signature == other.signature


@pytest.mark.parametrize('format', [QImage.Format_Grayscale16, QImage.Format_RGB30,
                                   QImage.Format_A2RGB30_Premultiplied])
def test_wide_channels_in_low_depth_storage_keep_their_native_samples(format):
    pixels = np.array([[[.400049, .500058, .600069, 1.]]], np.float32)
    source = working_image(pixels, FLOAT_PIXELS).convertToFormat(format)
    expected = source.convertToFormat(QImage.Format_RGBA32FPx4_Premultiplied)
    np.testing.assert_array_equal(premultiplied_pixels(source), premultiplied_pixels(expected))
    actual = premultiplied_pixels(import_image(source, FLOAT_PIXELS))
    np.testing.assert_array_equal(actual, premultiplied_pixels(expected))
    assert np.max(np.abs(actual[..., :3]*255-np.rint(actual[..., :3]*255))) > .001


@pytest.mark.parametrize('format', [QImage.Format_ARGB32_Premultiplied, QImage.Format_Grayscale16,
                                   QImage.Format_RGBA64_Premultiplied])
def test_trusted_import_and_copy_prioritize_one_frame_for_its_source_precision(format):
    from comic_editor.core.images import ImageStore
    values = np.array([[[.400049, .500058, .600069, 1.]]], np.float32)
    source = working_image(values, FLOAT_PIXELS).convertToFormat(format)
    wide = format != QImage.Format_ARGB32_Premultiplied
    store = ImageStore(decoded_budget=source.sizeInBytes())
    store.put_decoded('art', 'validated.png', b'already-validated', source)
    actual = store.native_image('art') if wide else store.image('art')
    np.testing.assert_array_equal(premultiplied_pixels(actual), premultiplied_pixels(source))
    generous = ImageStore()
    generous.put_decoded('art', 'validated.png', b'already-validated', source)
    copied = ImageStore(decoded_budget=source.sizeInBytes())
    generous.copy_source_to('art', copied, 'copy')
    result = copied.native_image('copy') if wide else copied.image('copy')
    np.testing.assert_array_equal(premultiplied_pixels(result), premultiplied_pixels(source))
    assert copied.decoded_bytes <= copied.decoded_budget


def test_ocio_conversion_keeps_alpha_and_handles_transparent_pixels():
    pixels = np.array([[[.25, .1, .025, .5], [0, 0, 0, 0], [.18, .36, .72, 1]]], np.float32)
    linear = transform_pixels(pixels, 'srgb', 'linear_srgb')
    assert np.array_equal(linear[...,3], pixels[...,3])
    assert np.array_equal(linear[0,1], pixels[0,1])
    assert linear[0,0,0] == pytest.approx(.10702, abs=2e-5)
    restored = transform_pixels(linear, 'linear_srgb', 'srgb')
    assert np.allclose(restored, pixels, atol=2e-5, rtol=0)
    assert color_processor('', 'srgb', 'linear_srgb') is color_processor('', 'srgb', 'linear_srgb')
    shader = gpu_color_processor('', 'srgb', 'linear_srgb')
    assert 'vec4 ocio_process' in shader.getShaderText()
    assert shader is gpu_color_processor('', 'srgb', 'linear_srgb')


def test_display_and_export_are_distinct_transforms():
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb', export_space='linear_srgb')
    pixels = np.array([[[.18, .18, .18, 1]]], np.float32)
    image = working_image(pixels, contract)
    display = premultiplied_pixels(display_image(image, contract))
    exported = export_image(image, contract)
    assert display[0,0,0] == pytest.approx(.46, abs=.006)
    assert exported.format() == QImage.Format_RGBA64
    assert premultiplied_pixels(exported)[0,0,0] == pytest.approx(.18, abs=1/65535)
    low_precision = export_image(image, contract, high_precision=False)
    assert premultiplied_pixels(low_precision)[0,0,0] == pytest.approx(.18, abs=1/255)
    assert exported.colorSpace() == QColorSpace(QColorSpace.SRgbLinear)


def test_float_modifier_helpers_keep_precision_without_changing_legacy():
    from comic_editor.ui.modifier_rendering import _premultiplied_qimage, _qimage_premultiplied
    from comic_editor.ui.effect_pipeline import empty_image
    from PySide6.QtCore import QRectF
    pixels = np.array([[[.10001, .20002, .30003, .4]]], np.float32)
    with pixel_scope(FLOAT_PIXELS):
        assert np.array_equal(_qimage_premultiplied(_premultiplied_qimage(pixels)), pixels)
        assert empty_image(QRectF(0, 0, 3, 4)).format() == FLOAT_PIXELS.image_format
    assert _premultiplied_qimage(pixels).format() == LEGACY_PIXELS.image_format


def test_render_service_carries_contract_and_restores_scope():
    from test_render_service import setup_service, request
    service, backend, document = setup_service(pixel_contract=FLOAT_PIXELS)
    observed = []
    backend.paint_hook = lambda: observed.append(current_contract())
    result = service.render_region(document, request(document))
    assert result.exact and result.image.format() == FLOAT_PIXELS.image_format
    assert observed == [FLOAT_PIXELS] and current_contract() == LEGACY_PIXELS


def test_legacy_document_policy_is_explicit_and_float_policy_survives_json():
    import json
    from comic_editor.core.models import ChapterDocument, SCHEMA_VERSION
    old = ChapterDocument().to_dict()
    old['schema_version'] = 25
    old.pop('pixel_contract')
    migrated = ChapterDocument.from_dict(old)
    assert migrated.pixel_contract is LEGACY_PIXELS
    assert migrated.schema_version == SCHEMA_VERSION == 26
    assert migrated.to_dict()['pixel_contract'] == LEGACY_PIXELS.to_dict()
    migrated.pixel_contract = replace(FLOAT_PIXELS, precision='float16',
        working_space='linear_srgb', export_space='linear_srgb')
    reopened = ChapterDocument.from_dict(json.loads(json.dumps(migrated.to_dict())))
    assert reopened.pixel_contract == migrated.pixel_contract


@pytest.mark.parametrize('payload', [
    {'version': 3}, {'version': 2, 'precision': 'uint8'},
    {'version': 1, 'working_space': 'linear_srgb'}, {'alpha': 'straight'},
    {'new_unknown_setting': True}, 'float32', [],
])
def test_invalid_persisted_color_policy_is_rejected(payload):
    from comic_editor.core.models import ChapterDocument
    data = ChapterDocument().to_dict()
    data['pixel_contract'] = payload
    with pytest.raises(ValueError):
        ChapterDocument.from_dict(data)


def test_color_policy_is_preserved_by_focused_metadata_undo_and_project_save(tmp_path):
    from comic_editor.core.models import ChapterDocument
    from comic_editor.core.document_patch import DocumentPatch
    from comic_editor.core.persistence import SeriesRepository
    from comic_editor.core.tiles import TileStore
    chapter = ChapterDocument()
    chapter.add_page('Page')
    chapter.pixel_contract = replace(FLOAT_PIXELS, precision='float16')
    before = chapter.to_dict()
    chapter.name = 'Renamed'
    after = chapter.to_dict()
    patch, _ = DocumentPatch.pair(before, after)
    patch.apply(chapter)
    assert chapter.name == before['name'] and chapter.pixel_contract == replace(FLOAT_PIXELS, precision='float16')
    repository = SeriesRepository(tmp_path/'saved')
    series = repository.create('Color')
    repository.save_chapter(chapter, TileStore())
    reopened, _ = repository.load_chapter(chapter.chapter_id)
    assert reopened.pixel_contract == chapter.pixel_contract


def test_color_policy_change_replays_validated_values_in_both_history_paths():
    from comic_editor.core.models import ChapterDocument
    from comic_editor.core.document_patch import DocumentPatch, RecordSnapshot
    for focused in (False, True):
        chapter = ChapterDocument()
        before = (RecordSnapshot.capture(chapter, scalars=['pixel_contract']) if focused
                  else chapter.to_dict())
        chapter.pixel_contract = FLOAT_PIXELS
        after = before.after(chapter) if focused else chapter.to_dict()
        old, new = DocumentPatch.pair(before, after)
        old.apply(chapter)
        assert chapter.pixel_contract == LEGACY_PIXELS
        new.apply(chapter)
        assert chapter.pixel_contract == FLOAT_PIXELS


def test_native_import_store_keeps_16_bit_samples_profile_and_bounded_residency():
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from comic_editor.core.images import ImageStore
    pixels = np.array([[[.400049, .500058, .600069, 1]]], np.float32)
    original = working_image(pixels, FLOAT_PIXELS).convertToFormat(QImage.Format_RGBA64)
    original.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    payload = QByteArray()
    output = QBuffer(payload)
    output.open(QIODevice.WriteOnly)
    assert original.save(output, 'PNG')
    output.close()
    raw = bytes(payload)
    store = ImageStore(decoded_budget=16)
    store.put('art', 'original.png', raw, 'image/png')
    native = store.native_image('art')
    assert native.depth() == 64 and native.colorSpace() == original.colorSpace()
    assert np.array_equal(premultiplied_pixels(native), premultiplied_pixels(original))
    assert store.source('art').data == raw and store.decoded_bytes <= store.decoded_budget
    native.fill(0)
    assert store.native_image('art').pixelColor(0, 0).alphaF() == 1
    assert store.image('art').depth() == 32
    assert store.decoded_bytes <= store.decoded_budget


def test_already_decoded_import_keeps_hdr_native_pixels_and_replaces_old_cache():
    from comic_editor.core.images import ImageStore
    first = np.array([[[1.125, -.02, .0007, .25]]], np.float32)
    store, copied = ImageStore(), ImageStore()
    store.put_decoded('art', 'hdr.png', b'', working_image(first, FLOAT_PIXELS))
    assert np.array_equal(premultiplied_pixels(store.native_image('art')), first)
    store.copy_source_to('art', copied, 'copy')
    assert np.array_equal(premultiplied_pixels(copied.native_image('copy')), first)
    second = np.array([[[.33, .44, .55, .75]]], np.float32)
    store.put_decoded('art', 'new.png', b'', working_image(second, FLOAT_PIXELS))
    assert np.array_equal(premultiplied_pixels(store.native_image('art')), second)
    assert np.array_equal(premultiplied_pixels(copied.native_image('copy')), first)


@pytest.mark.parametrize('masked', [False, True])
@pytest.mark.parametrize('algorithm', ['normal', 'legacy'])
def test_float_blur_preserves_constant_hdr_and_is_linear(masked, algorithm):
    from comic_editor.ui.modifier_rendering import _variable_blur, BlurPyramidCache
    constant = np.broadcast_to(np.array([1.12345, -.07321, .0007, .25], np.float32), (19, 31, 4)).copy()
    strength = np.linspace(1.75, 60.25, 19*31, dtype=np.float32).reshape(19, 31) if masked else 17.375
    cache = BlurPyramidCache(64*1024)
    with pixel_scope(FLOAT_PIXELS):
        filtered = _variable_blur(constant, strength, cache, algorithm)
        np.testing.assert_allclose(filtered, constant, rtol=0, atol=2e-7)
        original = np.random.default_rng(41).uniform(-.25, 1.5, (19, 31, 4)).astype(np.float32)
        original[..., 3] = .35
        first = _variable_blur(original, strength, cache, algorithm)
        combined = _variable_blur(original*.37+constant*.63, strength, cache, algorithm)
        np.testing.assert_allclose(combined, first*.37+filtered*.63, rtol=0, atol=3e-7)
        assert cache.bytes <= cache.budget
        assert all(level.dtype == np.float32 and not level.flags.writeable
                   for pyramid in cache._values.values() for level in pyramid)
    np.testing.assert_array_equal(constant[0, 0], [np.float32(1.12345), np.float32(-.07321), np.float32(.0007), np.float32(.25)])


def test_float_blur_outline_stack_never_quantizes_or_clips_unchanged_hdr_colors():
    from comic_editor.core.models import BlurModifier, OutlineModifier
    from comic_editor.ui.modifier_rendering import apply_modifier_stack
    values = np.broadcast_to(np.array([1.12345, -.07321, .0007, 1.], np.float32), (11, 19, 4)).copy()
    with pixel_scope(FLOAT_PIXELS):
        result = apply_modifier_stack(working_image(values), [BlurModifier(strength=13.75),
            OutlineModifier(thickness=2, blur_radius=3, blur_strength=61)], (0, 0))
        assert result.format() == FLOAT_PIXELS.image_format
        np.testing.assert_allclose(premultiplied_pixels(result), values, rtol=0, atol=2e-7)
