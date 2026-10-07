"""Production output shares detached native evaluation and atomic publication."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Event

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.assets import AssetManifest
from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject, ShapeStyle
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.outputs import (
    OutputCancelled, asset_thumbnail, capture_document, render_snapshot, write_export,
)
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, pixel_scope, premultiplied_pixels
from comic_editor.ui.canvas import CanvasWidget


def encoded(image):
    data = QByteArray()
    buffer = QBuffer(data)
    assert buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    return bytes(data)


def scene(contract=LEGACY_PIXELS):
    chapter = ChapterDocument(width=64, height=64, background='#00000000',
                              document_kind='asset', pixel_contract=contract)
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 64, 64))
    shape = chapter.add_layer(page.layer_id, bound=BoundGeometry.rectangle(2, 2, 12, 15),
                              style=ShapeStyle(primary_color='#FF804020', outline_thickness=0))
    raster = chapter.add_object(page.layer_id, RasterObject(interaction_rect=(18, 0, 64, 64)))
    tiles = TileStore()
    tile = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    tile.fill(QColor('transparent'))
    for y in range(18, 29):
        for x in range(18, 28):
            tile.setPixelColor(x, y, QColor(40, 80, 120, 128))
    tiles.set_tile(raster.object_id, (0, 0), tile)
    image = chapter.add_object(page.layer_id, ImageObject(pixel_width=2, pixel_height=2,
        transform_quad=[(32, 32), (34, 32), (34, 34), (32, 34)]))
    native = QImage(2, 2, QImage.Format_RGBA64)
    np.frombuffer(native.bits(), np.uint16).reshape(2, 2, 4)[:] = [32768, 16384, 8192, 1]
    sources = ImageStore()
    sources.put(image.object_id, 'original.png', encoded(native))
    return chapter, tiles, sources, shape, raster, image


def dispose(canvas):
    canvas._scene_controller.reset()
    canvas._scene_controller.scheduler.close()
    canvas._effect_jobs.cancel()
    canvas._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    canvas.deleteLater()


@pytest.mark.parametrize('contract', [LEGACY_PIXELS, FLOAT_PIXELS,
                                     replace(FLOAT_PIXELS, working_space='linear_srgb')])
def test_detached_mixed_scene_matches_same_live_native_kernels(qapp, monkeypatch, contract):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter, tiles, images, _, _, image = scene(contract)
    original = images.source(image.object_id).data
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(chapter, tiles, images)
    reference = QImage(64, 64, contract.image_format)
    try:
        with pixel_scope(contract):
            canvas.render_preview(reference)
        snapshot = capture_document(chapter, tiles, images)
        with ThreadPoolExecutor(max_workers=1) as worker:
            result = worker.submit(render_snapshot, snapshot).result(timeout=15)
        assert result.format() == contract.image_format
        np.testing.assert_array_equal(premultiplied_pixels(result), premultiplied_pixels(reference))
        if contract.floating:
            values = premultiplied_pixels(result)
            assert 0 < values[32, 32, 3] < 1/255.
        assert images.source(image.object_id).data == original
    finally:
        dispose(canvas)


def test_inactive_asset_thumbnail_matches_legacy_entity_crop(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    chapter, tiles, images, shape, raster, _ = scene()
    canvas = CanvasWidget(EditorSettings(canvas_renderer='raster'))
    canvas.set_document(ChapterDocument(width=17, height=11), TileStore())
    try:
        for kind, identifier in [('object', raster.object_id), ('layer', shape.layer_id)]:
            manifest = AssetManifest(document=chapter, root_kind=kind, root_id=identifier)
            reference = canvas.render_asset_thumbnail(manifest, tiles, size=76, padding=5, images=images)
            snapshot = capture_document(chapter, tiles, images)
            with ThreadPoolExecutor(max_workers=1) as worker:
                result = worker.submit(asset_thumbnail, snapshot, manifest, 76, 5).result(timeout=15)
            assert bytes(result.constBits()) == bytes(reference.constBits())
        assert canvas.chapter.width == 17 and canvas.chapter.height == 11
    finally:
        dispose(canvas)


def test_export_keeps_native_sources_and_preserves_float_low_alpha_channels(tmp_path):
    chapter, tiles, images, _, raster, image = scene(FLOAT_PIXELS)
    original = images.source(image.object_id).data
    snapshot = capture_document(chapter, tiles, images)
    tiles.tile(raster.object_id, (0, 0)).fill(QColor('blue'))
    image.opacity = 0.
    destination = tmp_path/'native.png'
    with ThreadPoolExecutor(max_workers=1) as worker:
        worker.submit(write_export, snapshot, destination).result(timeout=15)
    native = QImage(str(destination)).convertToFormat(QImage.Format_RGBA64)
    np.testing.assert_array_equal(np.frombuffer(native.constBits(), np.uint16).reshape(64, 64, 4)[32, 32],
                                  [32768, 16384, 8192, 1])
    assert native.pixelColor(23, 23).blue() < 180
    assert images.source(image.object_id).data == original
    assert not list(tmp_path.glob('*.tmp'))


def test_output_rectangle_keeps_background_outside_document():
    chapter = ChapterDocument(width=4, height=4, background='#FF123456', document_kind='asset')
    snapshot = capture_document(chapter, TileStore())
    result = render_snapshot(snapshot, (-2., -3., 9., 11.))
    assert result.size().toTuple() == (9, 11)
    assert result.pixelColor(0, 0) == QColor('#123456')
    assert result.pixelColor(8, 10) == QColor('#123456')


def test_encoding_failure_leaves_existing_destination_and_removes_temporary(tmp_path, monkeypatch):
    import comic_editor.render.outputs as outputs
    snapshot = capture_document(ChapterDocument(width=4, height=4), TileStore())
    destination = tmp_path/'existing.png'
    destination.write_bytes(b'existing-complete-file')
    monkeypatch.setattr(QImage, 'save', lambda *_a, **_k: False)
    with pytest.raises(OSError, match='encode'):
        outputs.write_export(snapshot, destination)
    assert destination.read_bytes() == b'existing-complete-file'
    assert list(tmp_path.iterdir()) == [destination]


def test_cancelled_output_cannot_publish(tmp_path):
    snapshot = capture_document(ChapterDocument(width=4, height=4), TileStore())
    cancelled = Event()
    cancelled.set()
    with pytest.raises(OutputCancelled):
        write_export(snapshot, tmp_path/'cancelled.png', cancelled=cancelled)
    assert not list(tmp_path.iterdir())
