"""Proposed new-owner regression for native modified source and export edges."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import time

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject, HueSaturationLightnessModifier
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.render.pixels import FLOAT_PIXELS, export_image, premultiplied_pixels
from comic_editor.render.scene import DetachedSceneBackend, SceneSnapshotCompiler
from comic_editor.render.service import DocumentRenderService, RenderRequest, RenderQuality
from comic_editor.ui.canvas import CanvasWidget


@pytest.fixture
def canvas(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda *_: None)
    owner = CanvasWidget(EditorSettings(canvas_renderer='raster', grid_overlay_visible=False))
    chapter = ChapterDocument(width=8, height=8, document_kind='asset', background='#00000000')
    page = chapter.add_page('Native source', BoundGeometry.rectangle(0, 0, 8, 8))
    page.fill_color, page.border_width = None, 0
    owner.set_document(chapter, TileStore(tile_size=4))
    owner.setUpdatesEnabled(False)
    yield owner
    owner._scene_controller.reset()
    owner._scene_controller.scheduler.close()
    owner._effect_jobs.cancel()
    owner._effect_jobs.executor.shutdown(wait=True, cancel_futures=True)
    owner.close()
    owner.deleteLater()


def encoded(image):
    raw, buffer = QByteArray(), QBuffer()
    buffer.setBuffer(raw)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    buffer.close()
    return bytes(raw)


def capture(canvas, qapp):
    pending = SceneSnapshotCompiler().capture(canvas, canvas._render_document_state())
    deadline = time.monotonic()+10
    while not pending.advance(.001):
        assert time.monotonic() < deadline
        qapp.processEvents()
    assert not pending.stale and pending.result is not None
    return pending.result


def render(snapshot):
    backend = DetachedSceneBackend(snapshot)
    try:
        service = DocumentRenderService(backend)
        service.projection.revision = snapshot.document.revision
        request = RenderRequest((0., 0., 8., 8.), 1., (8, 8),
            ('native-source-precision',), snapshot.document.revision, quality=RenderQuality.EXACT)
        result = service.render_region(snapshot.document, request)
        assert result.exact
        assert backend.scene._modifier_source_cache, 'Fixture must render through the modified-source allocation'
        assert all(image.format() == snapshot.document.pixel_contract.image_format
            for image in backend.scene._modifier_source_cache.values())
        return QImage(result.image), export_image(result.image, snapshot.document.pixel_contract)
    finally:
        backend.close()


@pytest.mark.parametrize('precision', ['float16', 'float32'])
@pytest.mark.parametrize('kind', ['Image', 'Raster'])
@pytest.mark.parametrize('target', ['object', 'layer'])
@pytest.mark.parametrize('alpha', [1, 4096])
def test_detached_modified_native_sources_keep_low_alpha_originals_profile_and_png_output(canvas, qapp, precision, kind, target, alpha):
    policy = replace(FLOAT_PIXELS, precision=precision)
    canvas.chapter.pixel_contract = policy
    original = QImage(4, 4, QImage.Format_RGBA64)
    original.fill(0)
    original.setColorSpace(QColorSpace(QColorSpace.SRgb))
    channels = np.array([32768, 32768, 32768, alpha], np.uint16)
    np.frombuffer(original.bits(), np.uint16)[:4] = channels
    original_icc = bytes(original.colorSpace().iccProfile())
    assert original_icc
    page = canvas.chapter.root_page_ids[0]
    if kind == 'Image':
        obj = canvas.chapter.add_object(page, ImageObject(x=2, y=3, pixel_width=4, pixel_height=4))
        before = encoded(original)
        canvas.images.put(obj.object_id, 'native16.png', before, 'image/png')
    else:
        obj = canvas.chapter.add_object(page, RasterObject(x=2, y=3, tile_size=4))
        canvas.tiles.set_tile(obj.object_id, (0, 0), original)
        before = bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits())
        assert canvas.tiles.content_bounds(obj.object_id).getRect() == (0., 0., 1., 1.)
    if target == 'layer':
        layer = canvas.chapter.add_layer(page, 'Modified subtree', BoundGeometry.rectangle(0, 0, 8, 8))
        layer.fill_color, layer.border_width = None, 0
        canvas.chapter.move_entity('object', obj.object_id, layer.layer_id, 0)
        reference = ('layer', layer.layer_id)
    else:
        reference = ('object', obj.object_id)
    canvas.chapter.add_modifier(HueSaturationLightnessModifier(), [reference])
    snapshot = capture(canvas, qapp)
    with ThreadPoolExecutor(max_workers=1) as worker:
        output, exported = worker.submit(render, snapshot).result(timeout=20)
        repeat, repeat_export = worker.submit(render, snapshot).result(timeout=20)
    expected = channels.astype(np.float32)/np.float32(65535)
    expected[:3] *= expected[3]
    if precision == 'float16':
        expected = expected.astype(np.float16).astype(np.float32)
    assert output.format() == policy.image_format
    # Independent complete authored frame, including every transparent pixel.
    # The single HSL stage is neutral; the native source has no resampling.
    expected_frame = np.zeros((8, 8, 4), np.float32)
    expected_frame[3, 2] = expected
    np.testing.assert_array_equal(premultiplied_pixels(output), expected_frame)
    dtype = np.float16 if precision == 'float16' else np.float32
    native_rows = np.frombuffer(output.constBits(), dtype).reshape(8, output.bytesPerLine()//np.dtype(dtype).itemsize)
    native_visible = native_rows[:, :8*4].reshape(8, 8, 4)
    assert native_visible.tobytes() == expected_frame.astype(dtype).tobytes()
    # Export's documented edge is straight RGB with one half-up quantization.
    # This is arithmetic on the independently authored frame, not export_image.
    expected_straight = expected_frame.copy()
    np.divide(expected_straight[..., :3], expected_straight[..., 3:4],
              out=expected_straight[..., :3], where=expected_straight[..., 3:4] > 0.)
    expected_export = np.floor(expected_straight*np.float32(65535)+np.float32(.5)).astype(np.uint16)
    assert bytes(output.constBits()) == bytes(repeat.constBits())
    assert exported.format() == QImage.Format_RGBA64 and exported.colorSpace() == original.colorSpace()
    rows = np.frombuffer(exported.constBits(), np.uint16).reshape(8, exported.bytesPerLine()//2)
    assert rows[:, :8*4].reshape(8, 8, 4).tobytes() == expected_export.tobytes()
    assert rows[3, 2*4+3] == alpha
    assert bytes(exported.colorSpace().iccProfile()) == original_icc
    assert bytes(exported.constBits()) == bytes(repeat_export.constBits())
    reloaded, _ = ImageStore._decode_native(encoded(exported))
    assert reloaded.format() == QImage.Format_RGBA64
    decoded_rows = np.frombuffer(reloaded.constBits(), np.uint16).reshape(8, reloaded.bytesPerLine()//2)
    assert decoded_rows[:, :8*4].reshape(8, 8, 4).tobytes() == expected_export.tobytes()
    assert bytes(reloaded.colorSpace().iccProfile()) == original_icc
    assert bytes(reloaded.constBits()) == bytes(exported.constBits())
    actual_source = (canvas.images.source(obj.object_id).data if kind == 'Image'
        else bytes(canvas.tiles.tile(obj.object_id, (0, 0)).constBits()))
    assert actual_source == before
