"""Native source, captured color resources, and final encoding boundaries."""
from copy import deepcopy
from dataclasses import replace
from io import BytesIO

import numpy as np
import pytest
from PIL import Image
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PySide6.QtGui import QColor, QColorSpace, QImage, QImageReader, QPainter

from comic_editor.core.images import ImageStore
from comic_editor.core.pixel_arrays import native_rgba_pixels
from comic_editor.render.pixels import (
    FLOAT_PIXELS, LEGACY_PIXELS, capture_color_environment, color_config,
    color_environment, display_image, export_image, import_image,
    pixel_scope, premultiplied_pixels, transform_pixels, working_image, working_rgba,
)


def _png(image):
    data = QByteArray()
    output = QBuffer(data)
    assert output.open(QIODevice.WriteOnly)
    assert image.save(output, 'PNG')
    output.close()
    return bytes(data)


def test_native_tile_save_reload_and_old_alpha_metadata_preserve_tiny_coverage(tmp_path):
    import json
    from comic_editor.core.tiles import TileStore
    original = QImage(4, 3, QImage.Format_RGBA64)
    pixels = np.frombuffer(original.bits(), np.uint16).reshape(3, 4, 4)
    pixels[:] = 0
    pixels[1, 2] = (32768, 16384, 8192, 1)
    original.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    store = TileStore()
    store.set_tile('native', (0, 0), original)
    assert store._alpha_bbox(original) == (2, 1, 3, 2)
    root = tmp_path/'tiles'
    store.save_directory(root, {'native'}, complete=True)
    reloaded = TileStore()
    reloaded.load_directory(root, {'native'})
    assert reloaded.residency.decodes == 0
    assert reloaded.content_bounds('native') == QRectF(2, 1, 1, 1)
    assert reloaded.residency.decodes == 0
    loaded = reloaded.tile('native', (0, 0))
    np.testing.assert_array_equal(native_rgba_pixels(loaded)[0], native_rgba_pixels(original)[0])
    assert loaded.colorSpace() == original.colorSpace()
    manifest = root/'.tile-index.json'
    records = json.loads(manifest.read_text())
    records.pop('.alpha-bounds-version')
    records['native/0_0.png'][2] = None  # Previous byte-only alpha metadata.
    manifest.write_text(json.dumps(records))
    old = TileStore()
    old.load_directory(root, {'native'})
    assert old.residency.decodes == 0
    assert old.content_bounds('native') == QRectF(2, 1, 1, 1)
    np.testing.assert_array_equal(native_rgba_pixels(old.tile('native', (0, 0)))[0], native_rgba_pixels(original)[0])


def test_float_tile_alpha_bounds_retain_sub_byte_coverage():
    from comic_editor.core.tiles import TileStore
    pixels = np.zeros((3, 4, 4), np.float32)
    pixels[2, 1] = (1e-10, 2e-10, 3e-10, 1e-9)
    assert TileStore._alpha_bbox(working_image(pixels, FLOAT_PIXELS)) == (1, 2, 2, 3)


def test_float_bake_keeps_native_low_alpha_png_in_its_source_space():
    from concurrent.futures import ThreadPoolExecutor
    from comic_editor.core.models import ChapterDocument, BoundGeometry, ImageObject, ShapeStyle
    from comic_editor.core.tiles import TileStore
    from comic_editor.render.bake_sources import rasterized_source
    from comic_editor.render.outputs import capture_document, render_snapshot
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb')
    chapter = ChapterDocument(width=8, height=8, document_kind='asset', pixel_contract=contract,
                              background='#00000000')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 8, 8),
                            style=ShapeStyle(primary_color=None, outline_thickness=0))
    obj = chapter.add_object(page.layer_id, ImageObject(x=2, y=2, pixel_width=2, pixel_height=2))
    native = QImage(2, 2, QImage.Format_RGBA64)
    np.frombuffer(native.bits(), np.uint16).reshape(2, 2, 4)[:] = (32768, 16384, 8192, 1)
    store = ImageStore()
    original = _png(native)
    store.put(obj.object_id, 'source.png', original)
    snapshot = capture_document(chapter, TileStore(), store)
    with ThreadPoolExecutor(max_workers=1) as worker:
        prepared = worker.submit(rasterized_source, snapshot, 'object', obj.object_id).result(timeout=15)
        expected = worker.submit(render_snapshot, snapshot, tuple(prepared.bounds.getRect()),
                                 target=('object', obj.object_id)).result(timeout=15)
    with pixel_scope(contract, environment=snapshot.pixel_environment):
        expected = export_image(expected, contract)
    np.testing.assert_array_equal(native_rgba_pixels(prepared.image)[0], native_rgba_pixels(expected)[0])
    assert native_rgba_pixels(prepared.image)[0][0, 0, 3] == np.float32(1/65535)
    reloaded = ImageStore()
    reloaded.put(obj.object_id, 'rasterized.png', prepared.encoded)
    np.testing.assert_array_equal(native_rgba_pixels(reloaded.native_image(obj.object_id))[0],
                                  native_rgba_pixels(prepared.image)[0])
    assert store.source(obj.object_id).data == original


@pytest.mark.parametrize('format,dtype,channels,wide', [
    (QImage.Format_RGBA8888, np.uint8, (128, 64, 32, 1), False),
    (QImage.Format_RGBA64, np.uint16, (32768, 16384, 8192, 1), True),
])
def test_low_alpha_straight_native_png_round_trip_has_no_associated_integer_temporary(
        format, dtype, channels, wide):
    original = QImage(3, 2, format)
    np.frombuffer(original.bits(), dtype).reshape(2, 3, 4)[:] = channels
    encoded = _png(original)
    store = ImageStore()
    store.put('art', 'original.png', encoded)
    working = store.image('art', contract=FLOAT_PIXELS)
    exported = export_image(working, FLOAT_PIXELS, high_precision=wide)
    reloaded = QImage.fromData(_png(exported), 'PNG').convertToFormat(format)
    np.testing.assert_array_equal(np.frombuffer(reloaded.constBits(), dtype).reshape(2, 3, 4),
                                  np.broadcast_to(channels, (2, 3, 4)))
    assert store.source('art').data == encoded
    np.testing.assert_array_equal(np.frombuffer(original.constBits(), dtype).reshape(2, 3, 4),
                                  np.broadcast_to(channels, (2, 3, 4)))


def test_straight_source_stride_and_argb_channels_are_read_before_float_association():
    data = np.full((2, 8), 0xDEADBEEF, np.uint32)
    data[:, :3] = 0x01804020
    source = QImage(data.data, 3, 2, data.strides[0], QImage.Format_ARGB32)
    expected = np.broadcast_to(np.array([128, 64, 32, 1], np.float32) / 255., (2, 3, 4)).copy()
    channels, associated = native_rgba_pixels(source)
    assert not associated
    np.testing.assert_array_equal(channels, expected)
    expected[..., :3] *= expected[..., 3:4]
    np.testing.assert_array_equal(premultiplied_pixels(import_image(source, FLOAT_PIXELS)), expected)
    assert np.all(data[:, 3:] == 0xDEADBEEF)


def test_embedded_icc_conversion_enters_float_before_low_alpha_association():
    source = QImage(1, 1, QImage.Format_RGBA64)
    channels = np.array([32768, 16384, 8192, 1], np.uint16)
    np.frombuffer(source.bits(), np.uint16)[:] = channels
    profile = QColorSpace(QColorSpace.DisplayP3)
    source.setColorSpace(profile)
    reference = working_image((channels.astype(np.float32) / 65535.).reshape(1, 1, 4), FLOAT_PIXELS)
    reference.reinterpretAsFormat(QImage.Format_RGBA32FPx4)
    reference.setColorSpace(profile)
    reference = reference.convertedToColorSpace(QColorSpace(QColorSpace.SRgb))
    expected, associated = native_rgba_pixels(reference)
    assert not associated
    expected[..., :3] *= expected[..., 3:4]
    np.testing.assert_array_equal(premultiplied_pixels(import_image(source, FLOAT_PIXELS)), expected)
    assert source.colorSpace() == profile
    np.testing.assert_array_equal(np.frombuffer(source.constBits(), np.uint16), channels)


def test_working_source_cache_is_policy_specific_bounded_detached_and_invalidated(monkeypatch):
    import comic_editor.render.pixels as pixels
    original = QImage(2, 2, QImage.Format_RGBA64)
    original.fill(QColor('#ff804020'))
    store = ImageStore(decoded_budget=128)
    store.put('art', 'original.png', _png(original))
    calls = []
    convert = pixels.import_image
    monkeypatch.setattr(pixels, 'import_image', lambda *a, **k: (calls.append(True), convert(*a, **k))[1])
    first = store.image('art', contract=FLOAT_PIXELS)
    expected = premultiplied_pixels(first)
    first.fill(0)
    np.testing.assert_array_equal(premultiplied_pixels(store.image('art', contract=FLOAT_PIXELS)), expected)
    assert len(calls) == 1
    linear = replace(FLOAT_PIXELS, working_space='linear_srgb')
    changed = store.image('art', contract=linear)
    assert premultiplied_pixels(changed)[0, 0, 0] < expected[0, 0, 0]
    assert len(calls) == 2 and store.decoded_bytes <= store.decoded_budget
    original.fill(QColor('blue'))
    store.put('art', 'replacement.png', _png(original))
    updated = store.image('art', contract=FLOAT_PIXELS)
    assert premultiplied_pixels(updated)[0, 0, 2] == 1.
    assert len(calls) == 3 and store.decoded_bytes <= store.decoded_budget
    assert store.image('art').format() == LEGACY_PIXELS.image_format


@pytest.mark.parametrize('dtype,format,values', [
    (np.uint16, QImage.Format_Grayscale16, [257, 32769, 65533]),
    (np.float32, QImage.Format_RGBA32FPx4, [-.125, .400049, 1.375]),
])
def test_pillow_fallback_keeps_wide_scalar_samples_and_embedded_profile(monkeypatch, dtype, format, values):
    data = np.array([values], dtype=dtype)
    profile = QColorSpace(QColorSpace.SRgbLinear)
    output = BytesIO()
    Image.fromarray(data).save(output, 'TIFF', icc_profile=bytes(profile.iccProfile()))
    monkeypatch.setattr(QImageReader, 'read', lambda self: QImage())
    native, _ = ImageStore._decode_native(output.getvalue())
    assert native.format() == format and native.colorSpace() == profile
    channels, associated = native_rgba_pixels(native)
    expected = data.astype(np.float32) / 65535. if dtype is np.uint16 else data
    np.testing.assert_array_equal(channels[..., 0], expected)
    np.testing.assert_array_equal(channels[..., 1], expected)
    assert not associated and np.all(channels[..., 3] == 1.)


def _cube(path, scale):
    path.write_text(f'LUT_1D_SIZE 2\n0 0 0\n{scale} {scale} {scale}\n', encoding='ascii')


def _file_config(path, src):
    import PyOpenColorIO as ocio
    config = deepcopy(color_config())
    srgb = config.getColorSpace('srgb')
    srgb.setTransform(ocio.FileTransform(src=src), ocio.COLORSPACE_DIR_TO_REFERENCE)
    config.addColorSpace(srgb)
    path.write_text(config.serialize(), encoding='utf8')


def test_external_lut_edits_invalidate_new_color_resources_but_captured_render_survives(tmp_path):
    cube, config = tmp_path / 'curve.cube', tmp_path / 'config.ocio'
    _cube(cube, .5)
    _file_config(config, cube.name)
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb', ocio_config=str(config))
    captured = capture_color_environment(contract)
    samples = np.array([[[.5, .25, .125, 1.]]], np.float32)
    before = transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract)
    np.testing.assert_array_equal(before, samples * np.array([.5, .5, .5, 1.], np.float32))
    _cube(cube, 1.)
    assert color_environment(contract) != captured.signature
    np.testing.assert_array_equal(transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract), samples)
    cube.unlink()
    config.unlink()
    with pixel_scope(contract, environment=captured):
        assert color_environment(contract) == captured.signature
        np.testing.assert_array_equal(transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract), before)


def test_custom_config_edits_invalidate_processors_and_environment_context_is_captured(tmp_path, monkeypatch):
    import PyOpenColorIO as ocio
    config = tmp_path / 'config.ocio'
    first, second = tmp_path / 'first.cube', tmp_path / 'second.cube'
    _cube(first, .5)
    _cube(second, .75)
    _file_config(config, '$TEST_WEBTOON_LUT')
    editable = ocio.Config.CreateFromFile(str(config))
    editable.addEnvironmentVar('TEST_WEBTOON_LUT', first.name)
    config.write_text(editable.serialize(), encoding='utf8')
    monkeypatch.setenv('TEST_WEBTOON_LUT', first.name)
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb', ocio_config=str(config))
    captured = capture_color_environment(contract)
    monkeypatch.setenv('TEST_WEBTOON_LUT', second.name)
    samples = np.array([[[.5, .5, .5, 1.]]], np.float32)
    np.testing.assert_array_equal(transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract)[..., :3],
                                  np.full((1, 1, 3), np.float32(.375)))
    with pixel_scope(contract, environment=captured):
        np.testing.assert_array_equal(transform_pixels(samples, 'srgb', 'linear_srgb', contract=contract)[..., :3],
                                      np.full((1, 1, 3), np.float32(.25)))
    _file_config(config, first.name)
    assert color_environment(contract) != captured.signature


def test_empty_float_display_export_and_zero_coverage_do_not_access_color_processor():
    assert display_image(QImage(), FLOAT_PIXELS).isNull()
    assert export_image(QImage(), FLOAT_PIXELS).isNull()
    source = working_image(np.array([[[.5, -.2, 1.5, 0.]]], np.float32), FLOAT_PIXELS)
    encoded = export_image(source, FLOAT_PIXELS)
    np.testing.assert_array_equal(native_rgba_pixels(encoded)[0], np.zeros((1, 1, 4), np.float32))


def test_shared_color_gradient_kernel_preserves_float_color_and_fractional_coverage():
    from comic_editor.core.models import ColorGradientRamp, ColorGradientStop
    from comic_editor.render.scene_kernels import SceneKernels
    owner = object.__new__(SceneKernels)
    owner._gradient_render_cache = {}
    ramp = ColorGradientRamp(stops=[ColorGradientStop(position=0., color='#FF808080'),
                                    ColorGradientStop(position=1., color='#FF808080')])
    coverage = np.array([[.00001, .37519]], np.float32)
    scalar = np.array([[.2, .8]], np.float32)
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb')
    with pixel_scope(contract):
        image, bounds = owner._gradient_image_from_scalar(scalar, coverage, ramp, QRectF(0, 0, 2, 1), ('scalar',))
        expected = np.broadcast_to(working_rgba('#FF808080'), (1, 2, 4)).copy()
        expected[..., 3] *= coverage
        expected[..., :3] *= expected[..., 3:4]
        assert image.format() == contract.image_format and bounds == QRectF(0, 0, 2, 1)
        np.testing.assert_array_equal(premultiplied_pixels(image), expected)
    with pixel_scope(LEGACY_PIXELS):
        old, _ = owner._gradient_image_from_scalar(scalar, coverage, ramp, QRectF(0, 0, 2, 1), ('scalar',))
        assert old.format() == QImage.Format_RGBA8888_Premultiplied
        assert old.pixelColor(0, 0).alpha() == 0


def test_float_scene_image_paint_keeps_import_precision(qapp, monkeypatch):
    from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject
    from comic_editor.core.settings import EditorSettings
    from comic_editor.core.tiles import TileStore
    from comic_editor.ui.canvas import CanvasWidget
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter = ChapterDocument(width=8, height=8, document_kind='asset', pixel_contract=FLOAT_PIXELS)
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 8, 8))
    obj = chapter.add_object(page.layer_id, ImageObject(pixel_width=2, pixel_height=2,
        placement_mode='free', transform_quad=[(1, 1), (3, 1), (3, 3), (1, 3)]))
    source = QImage(2, 2, QImage.Format_RGBA64)
    channels = np.array([32768, 16384, 8192, 1], np.uint16)
    np.frombuffer(source.bits(), np.uint16).reshape(2, 2, 4)[:] = channels
    images = ImageStore()
    images.put('unused', 'unused.png', _png(source))
    images.put(obj.object_id, 'art.png', _png(source))
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    try:
        canvas.set_document(chapter, TileStore(), images)
        target = QImage(8, 8, FLOAT_PIXELS.image_format)
        target.fill(0)
        with pixel_scope(FLOAT_PIXELS):
            painter = QPainter(target)
            canvas._render_image_object(painter, obj)
            painter.end()
        expected = channels.astype(np.float32) / 65535.
        expected[:3] *= expected[3]
        np.testing.assert_array_equal(premultiplied_pixels(target)[1:3, 1:3],
                                      np.broadcast_to(expected, (2, 2, 4)))
        assert images.source(obj.object_id).data == _png(source)
    finally:
        canvas._effect_jobs.cancel()
        canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
        canvas.deleteLater()


def test_authored_overlay_color_enters_working_space_once_without_losing_tiny_alpha():
    from comic_editor.core.models import SolidColorOverlayModifier
    from comic_editor.ui.modifier_rendering import apply_modifier_stack, _straight
    contract = replace(FLOAT_PIXELS, working_space='linear_srgb')
    source = np.array([[[.125, .25, .5, 1.]]], np.float32) * np.float32(2. ** -24)
    with pixel_scope(contract):
        np.testing.assert_array_equal(_straight(source)[..., :3], [[[.125, .25, .5]]])
        image = apply_modifier_stack(working_image(source), [SolidColorOverlayModifier(color='#FF808080',
            blend_mode='replace', intensity=100)], (0, 0))
        expected = working_rgba('#FF808080') * source[..., 3:4]
        np.testing.assert_array_equal(premultiplied_pixels(image), expected)
