"""Native coverage survives natural tile admission and real modified captures."""
from dataclasses import replace

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject, HueSaturationLightnessModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, premultiplied_pixels
from comic_editor.render.service import RenderQuality, RenderRequest, RenderStatus
from comic_editor.ui.canvas import CanvasWidget


WIDE_FORMATS = [
    (QImage.Format_RGBA64, np.uint16),
    (QImage.Format_RGBA64_Premultiplied, np.uint16),
    (QImage.Format_RGBA16FPx4, np.float16),
    (QImage.Format_RGBA16FPx4_Premultiplied, np.float16),
    (QImage.Format_RGBA32FPx4, np.float32),
    (QImage.Format_RGBA32FPx4_Premultiplied, np.float32),
]


@pytest.mark.parametrize('format,dtype', WIDE_FORMATS)
@pytest.mark.parametrize('padded', [False, True])
@pytest.mark.parametrize('coverage', ['empty', 'sub-byte', 'smallest-positive'])
def test_native_alpha_bounds_preserve_positive_channels_and_ignore_stride_padding(format, dtype, padded, coverage):
    rows = np.zeros((4, (5 if padded else 3)*4), dtype)
    # Hidden HDR/negative color does not become coverage; padding also is not
    # artwork even when its alpha is nonzero.
    if dtype != np.uint16:
        rows[:, :12].reshape(4, 3, 4)[..., :3] = [2., -.125, 1.25]
    if padded:
        rows[:, 15::4] = 1
    if coverage != 'empty':
        alpha = 1 if dtype == np.uint16 else 1/65535 if coverage == 'sub-byte' else np.nextafter(dtype(0), dtype(1))
        rows[1, 2*4+3] = rows[2, 1*4+3] = alpha
    image = QImage(rows.data, 3, 4, rows.strides[0], format)
    before = bytes(image.constBits())
    expected = None if coverage == 'empty' else (1, 1, 3, 3)
    assert TileStore._alpha_bbox(image) == expected
    assert bytes(image.constBits()) == before
    store = TileStore(tile_size=3)
    store.set_tile('native', (0, 0), image)
    admitted = store.tile('native', (0, 0))
    if expected is None:
        assert admitted is None and store.content_bounds('native') is None
    else:
        assert admitted is not None and admitted.format() == format
        assert store.content_bounds('native').getRect() == (1., 1., 2., 2.)
        # Qt can remove row padding when owning the stored copy, but every
        # actual channel remains unchanged.
        owned = np.frombuffer(admitted.constBits(), dtype).reshape(4, admitted.bytesPerLine()//np.dtype(dtype).itemsize)
        np.testing.assert_array_equal(owned[:, :12], rows[:, :12])


def test_bounds_fallback_uses_floating_alpha_without_byte_quantization():
    class Color:
        def __init__(self, alpha):
            self.coverage = alpha
        def alphaF(self):
            return self.coverage
        def alpha(self):
            raise AssertionError('Fallback must not quantize native coverage')
    class Image:
        def format(self):
            return QImage.Format_RGBA32FPx4_Premultiplied
        def width(self):
            return 3
        def height(self):
            return 2
        def constBits(self):
            raise ValueError('Controlled unavailable raw view')
        def pixelColor(self, x, y):
            return Color(1e-9 if (x, y) == (2, 1) else 0.)
    assert TileStore._alpha_bbox(Image()) == (2, 1, 3, 2)


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    chapter = ChapterDocument(width=8, height=8, document_kind='asset', background='#00000000')
    page = chapter.add_page('Native modified', BoundGeometry.rectangle(0, 0, 8, 8))
    page.fill_color, page.border_width = None, 0
    owner.set_document(chapter, TileStore())
    owner.setUpdatesEnabled(False)
    yield owner
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.close()
    owner.deleteLater()


def exact(canvas, contract):
    canvas.chapter.pixel_contract = contract
    config = canvas._projection_configuration()
    canvas._render_service.configure((*config, None), document=config[:3])
    document = canvas._render_document_state()
    request = RenderRequest((0., 0., 8., 8.), 1., (8, 8), ('native-modified-source',),
        document.revision, quality=RenderQuality.EXACT, defer_effects=False)
    result = canvas._render_service.render_region(document, request)
    assert result.status is RenderStatus.EXACT and result.image.format() == contract.image_format
    return result.image


def add_source(canvas, kind, alpha):
    page_id = canvas.chapter.root_page_ids[0]
    if kind == 'Image':
        original = QImage(1, 1, QImage.Format_RGBA64)
        channels = np.array([32768, 16384, 8192, alpha], np.uint16)
        np.frombuffer(original.bits(), np.uint16)[:4] = channels
        original.setColorSpace(QColorSpace(QColorSpace.SRgb))
        raw, buffer = QByteArray(), QBuffer()
        buffer.setBuffer(raw)
        buffer.open(QIODevice.WriteOnly)
        assert original.save(buffer, 'PNG')
        buffer.close()
        obj = canvas.chapter.add_object(page_id, ImageObject(x=2, y=3, pixel_width=1, pixel_height=1))
        canvas.images.put(obj.object_id, 'native.png', bytes(raw), 'image/png')
        native = channels.astype(np.float32)/np.float32(65535)
        native[:3] *= native[3]
        return obj, native, bytes(raw)
    original = QImage(256, 256, QImage.Format_RGBA32FPx4_Premultiplied)
    original.fill(0)
    coverage = np.float32(alpha/65535)
    # Neutral color avoids an HSL RGB rounding issue while retaining HDR.
    native = np.array([1.25*coverage]*3+[coverage], np.float32)
    np.frombuffer(original.bits(), np.float32)[:4] = native
    obj = canvas.chapter.add_object(page_id, RasterObject(x=2, y=3, interaction_rect=(0, 0, 1, 1)))
    before = bytes(original.constBits())
    canvas.tiles.set_tile(obj.object_id, (0, 0), original)
    assert canvas.tiles.content_bounds(obj.object_id).getRect() == (0., 0., 1., 1.)
    assert bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits()) == before
    return obj, native, before


@pytest.mark.parametrize('precision', ['float16', 'float32'])
@pytest.mark.parametrize('kind', ['Image', 'Raster'])
@pytest.mark.parametrize('target', ['object', 'layer'])
@pytest.mark.parametrize('alpha', [1, 4096])
def test_modified_native_object_and_layer_exact_export_preserve_source_coverage(canvas, precision, kind, target, alpha):
    obj, native, before = add_source(canvas, kind, alpha)
    if target == 'layer':
        layer = canvas.chapter.add_layer(obj.parent_layer_id, 'Effect subtree', BoundGeometry.rectangle(0, 0, 8, 8))
        layer.fill_color, layer.border_width = None, 0
        canvas.chapter.move_entity('object', obj.object_id, layer.layer_id, 0)
        canvas.chapter.add_modifier(HueSaturationLightnessModifier(), [('layer', layer.layer_id)])
    else:
        canvas.chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    policy = replace(FLOAT_PIXELS, precision=precision)
    image = exact(canvas, policy)
    expected = native.astype(np.float16).astype(np.float32) if precision == 'float16' else native
    np.testing.assert_array_equal(premultiplied_pixels(image)[3, 2, 3], expected[3])
    if kind == 'Raster':
        np.testing.assert_array_equal(premultiplied_pixels(image)[3, 2], expected)
    assert all(source.format() == policy.image_format for source in canvas._modifier_source_cache.values())
    for source in canvas._modifier_source_cache.values():
        point = (0, 0) if source.size().width() == 1 else (3, 2)
        np.testing.assert_array_equal(premultiplied_pixels(source)[point], expected)
    repeat = exact(canvas, policy)
    assert bytes(repeat.constBits()) == bytes(image.constBits())
    exported = canvas.render_export_image()
    rows = np.frombuffer(exported.constBits(), np.uint16).reshape(8, exported.bytesPerLine()//2)
    assert rows[3, 2*4+3] == round(float(expected[3])*65535)
    raw, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    assert exported.save(buffer, 'PNG')
    buffer.close()
    decoded, _ = ImageStore._decode_native(bytes(raw))
    assert bytes(decoded.constBits()) == bytes(exported.constBits())
    actual_source = canvas.images.source(obj.object_id).data if kind == 'Image' else bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits())
    assert actual_source == before


def test_legacy_generic_modified_capture_retains_established_argb32_bytes(canvas):
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(0)
    image.setPixel(0, 0, 0xffff0000)
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject(x=2, y=3))
    canvas.tiles.set_tile(obj.object_id, (0, 0), image)
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(), [('object', obj.object_id)])
    result = exact(canvas, LEGACY_PIXELS)
    assert result.pixel(2, 3) == image.pixel(0, 0)
    assert all(source.format() == QImage.Format_ARGB32_Premultiplied for source in canvas._modifier_source_cache.values())


@pytest.mark.parametrize('precision', ['float16', 'float32'])
@pytest.mark.parametrize('old_bbox', [None, [1, 1, 2, 2]])
def test_reopen_16bit_png_rejects_matching_stale_bounds_lazily_and_preserves_exact_export(canvas, tmp_path, precision, old_bbox):
    import json
    root = tmp_path/'raster'
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0], RasterObject(x=2, y=3, tile_size=4))
    directory = root/obj.object_id
    directory.mkdir(parents=True)
    path = directory/'0_0.png'
    image = QImage(4, 4, QImage.Format_RGBA64)
    image.fill(0)
    rows = np.frombuffer(image.bits(), np.uint16).reshape(4, image.bytesPerLine()//2)
    rows[0, :4] = rows[2, 8:12] = [32768, 16384, 8192, 1]
    assert image.save(str(path), 'PNG')
    original = path.read_bytes()
    assert original[:8] == b'\x89PNG\r\n\x1a\n' and original[24] == 16
    stat = path.stat()
    marker = f'{obj.object_id}/0_0.png'
    (root/'.tile-index.json').write_text(json.dumps({marker: [stat.st_size, stat.st_mtime_ns, old_bbox]}))
    loaded = TileStore(tile_size=4, cache_budget=64)
    loaded.load_directory(root, {obj.object_id})
    assert loaded.residency.decodes == loaded.residency.bytes == 0
    assert (obj.object_id, 0, 0) in loaded._alpha_bounds_dirty
    assert loaded.content_bounds(obj.object_id).getRect() == (0., 0., 3., 3.)
    native = loaded.tile(obj.object_id, (0, 0))
    assert native.format() == QImage.Format_RGBA64
    np.testing.assert_array_equal(np.frombuffer(native.constBits(), np.uint16).reshape(4, 16), rows)
    canvas.set_document(canvas.chapter, loaded)
    policy = replace(FLOAT_PIXELS, precision=precision)
    output = exact(canvas, policy)
    alpha = np.float16(1/65535).astype(np.float32) if precision == 'float16' else np.float32(1/65535)
    assert premultiplied_pixels(output)[3, 2, 3] == alpha
    exported = canvas.render_export_image()
    export_rows = np.frombuffer(exported.constBits(), np.uint16).reshape(8, exported.bytesPerLine()//2)
    assert export_rows[3, 11] == 1
    loaded.save_directory(root, {obj.object_id}, complete=True)
    record = json.loads((root/'.tile-index.json').read_text())[marker]
    assert record[2] == [0, 0, 3, 3] and record[3] == 2
    assert path.read_bytes() == original
    reopened = TileStore(tile_size=4)
    reopened.load_directory(root, {obj.object_id})
    assert reopened.residency.decodes == 0
    assert reopened.content_bounds(obj.object_id).getRect() == (0., 0., 3., 3.)
    assert reopened.residency.decodes == 0


def test_old_matching_8bit_png_bounds_stay_lazy_and_byte_compatible(tmp_path):
    import json
    root = tmp_path/'raster'
    directory = root/'legacy'
    directory.mkdir(parents=True)
    image = QImage(4, 4, QImage.Format_ARGB32_Premultiplied)
    image.fill(0xffff0000)
    path = directory/'0_0.png'
    assert image.save(str(path), 'PNG')
    assert path.read_bytes()[24] == 8
    stat = path.stat()
    (root/'.tile-index.json').write_text(json.dumps({'legacy/0_0.png': [stat.st_size, stat.st_mtime_ns, [0, 0, 4, 4]]}))
    store = TileStore(tile_size=4)
    store.load_directory(root, {'legacy'})
    assert store.content_bounds('legacy').getRect() == (0., 0., 4., 4.)
    assert store.residency.decodes == store.residency.bytes == 0
    assert not store._alpha_bounds_dirty
    decoded = store.tile('legacy', (0, 0))
    assert decoded.format() == QImage.Format_ARGB32_Premultiplied
    assert bytes(decoded.constBits()) == bytes(image.constBits())


@pytest.mark.parametrize('format,dtype', WIDE_FORMATS[:2])
def test_wide_disk_decode_residency_budget_evicts_without_losing_source_bytes(tmp_path, format, dtype):
    root = tmp_path/'raster'
    directory = root/'wide'
    directory.mkdir(parents=True)
    original = {}
    for x in range(3):
        image = QImage(4, 4, format)
        image.fill(0)
        np.frombuffer(image.bits(), dtype)[:4] = [1, 1, 1, 1]
        path = directory/f'{x}_0.png'
        assert image.save(str(path), 'PNG')
        original[x] = path.read_bytes()
    store = TileStore(tile_size=4, cache_budget=4*4*8)
    store.load_directory(root, {'wide'})
    assert store.residency.decodes == 0
    for x in range(3):
        image = store.tile('wide', (x, 0))
        assert image.depth() == 64 and image.sizeInBytes() == 4*4*8
        assert store.residency.bytes <= store.residency.budget
    assert store.residency.evictions == 2
    assert store.tile('wide', (0, 0)).depth() == 64
    assert all((directory/f'{x}_0.png').read_bytes() == before for x, before in original.items())
