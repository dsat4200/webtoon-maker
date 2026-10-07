"""Ready native originals survive scene turnover without crossing revisions."""
from dataclasses import replace
from threading import Event
import time
from types import SimpleNamespace

import numpy as np
import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice
from PySide6.QtGui import QColor, QColorSpace, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject
from comic_editor.core.pixel_contract import FLOAT_PIXELS, LEGACY_PIXELS, PixelContract
from comic_editor.core.tile_backing import EditableTile
from comic_editor.core.tile_history import TileHistoryCache
from comic_editor.core.tiles import TileStore
from comic_editor.render.outputs import capture_document
from comic_editor.render.pixels import pixel_scope
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.source_resources import ReadyOriginals


def native(format=QImage.Format_RGBA64):
    image = QImage(16, 16, format)
    if format in (QImage.Format_RGBA64, QImage.Format_RGBA64_Premultiplied):
        pixels = np.frombuffer(image.bits(), np.uint16).reshape(16, 16, 4)
        pixels[:] = (4, 3, 2, 7) if format == QImage.Format_RGBA64_Premultiplied else (23991, 38991, 53991, 7)
    else:
        dtype = np.float16 if image.depth() == 64 else np.float32
        pixels = np.frombuffer(image.bits(), dtype).reshape(16, 16, 4)
        alpha = .000003
        pixels[:] = (.36, .58, .81, alpha)
        if format in (QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied):
            pixels[..., :3] *= pixels[..., 3:4]
    image.setColorSpace(QColorSpace(QColorSpace.DisplayP3))
    return image


def png(image):
    buffer = QBuffer()
    assert buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, 'PNG')
    return bytes(buffer.data())


def sources(tmp_path):
    image = native()
    images = ImageStore()
    images.put('image', 'source.png', png(image), 'image/png')
    images._decoded.clear()
    images.decoded_bytes = 0
    live = TileStore(16)
    live.set_tile('raster', (0, 0), image)
    live.save_directory(tmp_path/'tiles', {'raster'}, complete=True)
    tiles = TileStore(16)
    tiles.load_directory(tmp_path/'tiles', {'raster'})
    return image, images, tiles


def snapshot(images, tiles, revision=0, *, contract=LEGACY_PIXELS, identity=(101, 102, 103), chapter=None):
    chapter = chapter or ChapterDocument(width=16, height=16, pixel_contract=contract, document_kind='asset')
    result = capture_document(chapter, tiles, images)
    result.images.decoded_budget = images.decoded_budget
    return replace(result, document=replace(result.document, identity=identity, revision=revision))


def same_pixels(actual, expected):
    assert actual.format() == expected.format()
    assert actual.colorSpace() == expected.colorSpace()
    assert bytes(actual.constBits()) == bytes(expected.constBits())


def test_ready_native_images_and_tiles_decode_once_across_unchanged_snapshot(tmp_path, monkeypatch):
    expected, images, tiles = sources(tmp_path)
    original = ImageStore._decode_native
    calls = []
    def decode(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    first = snapshot(images, tiles).finish_sources()
    same_pixels(first.images.native_image('image'), expected)
    same_pixels(first.tiles.tile('raster', (0, 0)), expected)
    assert len(calls) == 1 and first.tiles.residency.decodes == 1
    ready = ReadyOriginals()
    ready.capture(first)
    assert ready.byte_count == expected.sizeInBytes()*2
    second = snapshot(images, tiles, 1).finish_sources()
    ready.adopt(second)
    assert ready.byte_count == 0
    same_pixels(second.images.native_image('image'), expected)
    same_pixels(second.tiles.tile('raster', (0, 0)), expected)
    assert len(calls) == 1 and second.tiles.residency.decodes == 0
    assert second.images._decoded is not first.images._decoded
    assert second.tiles.residency.entries is not first.tiles.residency.entries


def test_replaced_image_source_cannot_adopt_previous_decoded_pixels(tmp_path):
    old, images, tiles = sources(tmp_path)
    first = snapshot(images, tiles)
    first.images.native_image('image')
    ready = ReadyOriginals()
    ready.capture(first)
    replacement = native()
    np.frombuffer(replacement.bits(), np.uint16).reshape(16, 16, 4)[:] = (11997, 21997, 31997, 11)
    images.put('image', 'replacement.png', png(replacement), 'image/png')
    images._decoded.clear()
    images.decoded_bytes = 0
    second = snapshot(images, tiles, 1)
    ready.adopt(second)
    assert ('native', 'image') not in second.images._decoded
    same_pixels(second.images.native_image('image'), replacement)
    assert bytes(replacement.constBits()) != bytes(old.constBits())


def test_edited_tile_revision_keeps_current_captured_pixels(tmp_path):
    old, images, tiles = sources(tmp_path)
    first = snapshot(images, tiles).finish_sources()
    first.tiles.tile('raster', (0, 0))
    ready = ReadyOriginals()
    ready.capture(first)
    edited = tiles.tile('raster', (0, 0))
    edited.setPixelColor(3, 4, QColor.fromRgbF(.91, .19, .39, .41))
    second = snapshot(images, tiles, 1).finish_sources()
    ready.adopt(second)
    same_pixels(second.tiles.tile('raster', (0, 0)), edited)
    assert bytes(edited.constBits()) != bytes(old.constBits())


def test_same_tile_version_with_different_immutable_pin_is_rejected(tmp_path):
    old, images, tiles = sources(tmp_path)
    first = snapshot(images, tiles).finish_sources()
    first.tiles.tile('raster', (0, 0))
    ready = ReadyOriginals()
    ready.capture(first)
    replacement = native()
    np.frombuffer(replacement.bits(), np.uint16).reshape(16, 16, 4)[:] = (4991, 6991, 8991, 3)
    live = TileStore(16)
    live.set_tile('raster', (0, 0), replacement)
    live.save_directory(tmp_path/'replacement', {'raster'}, complete=True)
    newer = TileStore(16)
    newer.load_directory(tmp_path/'replacement', {'raster'})
    second = snapshot(images, newer, 1).finish_sources()
    assert first.tiles._tiles['raster'].version((0, 0)) == second.tiles._tiles['raster'].version((0, 0))
    ready.adopt(second)
    assert not second.tiles.residency.entries
    same_pixels(second.tiles.tile('raster', (0, 0)), replacement)
    assert second.tiles.residency.decodes == 1


@pytest.mark.parametrize('guard', ['document', 'contract', 'environment'])
def test_document_color_contract_and_environment_guard_original_handoff(tmp_path, guard):
    _old, images, tiles = sources(tmp_path)
    first = snapshot(images, tiles).finish_sources()
    first.images.native_image('image')
    first.tiles.tile('raster', (0, 0))
    ready = ReadyOriginals()
    ready.capture(first)
    options = {'identity': (201, 202, 203)} if guard == 'document' else {'contract': FLOAT_PIXELS} if guard == 'contract' else {}
    second = snapshot(images, tiles, 1, **options).finish_sources()
    if guard == 'environment':
        second = replace(second, pixel_environment=SimpleNamespace(signature=('changed',)))
    ready.adopt(second)
    assert ready.byte_count == 0 and not second.images._decoded and not second.tiles.residency.entries


def test_original_handoff_uses_existing_eviction_budgets_and_consumes_handles(tmp_path):
    image, images, tiles = sources(tmp_path)
    images.put('other', 'other.png', png(image), 'image/png')
    images._decoded.clear()
    images.decoded_bytes = 0
    images.decoded_budget = image.sizeInBytes()
    first = snapshot(images, tiles)
    first.images.native_image('image')
    first.images.native_image('other')
    assert list(first.images._decoded) == [('native', 'other')]
    ready = ReadyOriginals()
    ready.capture(first)
    second = snapshot(images, tiles, 1)
    second.images.decoded_budget = 0
    ready.adopt(second)
    assert not second.images._decoded and second.images.decoded_bytes == 0
    assert ready.byte_count == 0
    first.tiles.residency.budget = image.sizeInBytes()
    first.tiles.set_tile('raster', (1, 0), image.copy())
    first.tiles.tile('raster', (0, 0))
    ready.capture(first)
    assert ready.byte_count <= first.images.decoded_budget + first.tiles.residency.budget
    ready.clear()
    assert ready.byte_count == 0 and ready.identity is None


@pytest.mark.parametrize('format', [QImage.Format_RGBA64_Premultiplied,
    QImage.Format_RGBA16FPx4, QImage.Format_RGBA16FPx4_Premultiplied,
    QImage.Format_RGBA32FPx4, QImage.Format_RGBA32FPx4_Premultiplied])
def test_frozen_native_source_handoff_preserves_all_bits_profile_and_low_alpha(format):
    original = native(format)
    history = TileHistoryCache(0)
    frozen = history.capture(original)
    tiles, images = TileStore(16), ImageStore()
    owner = tiles._object_tiles('raster')
    owner.entries[0, 0] = EditableTile(frozen)
    owner.versions[0, 0] = 1
    first = snapshot(images, tiles)
    same_pixels(first.tiles.tile('raster', (0, 0)), original)
    ready = ReadyOriginals()
    ready.capture(first)
    reads = history.reads
    second = snapshot(images, tiles, 1)
    ready.adopt(second)
    same_pixels(second.tiles.tile('raster', (0, 0)), original)
    assert second.tiles.residency.decodes == 0 and history.reads == reads


def wait_scheduler(scheduler):
    deadline = time.monotonic()+10
    results = []
    while scheduler.busy and time.monotonic() < deadline:
        results.extend(scheduler.poll())
        time.sleep(.001)
    results.extend(scheduler.poll())
    assert not scheduler.busy
    assert not any(result.error for result in results)
    return results


@pytest.mark.parametrize('cancelled', [False, True])
def test_native_worker_turnover_and_cancellation_reuse_original_decode(qapp, tmp_path, monkeypatch, cancelled):
    _image, images, tiles = sources(tmp_path)
    chapter = ChapterDocument(width=16, height=16, document_kind='asset', pixel_contract=FLOAT_PIXELS)
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 16, 16))
    obj = chapter.add_object(page.layer_id, ImageObject(object_id='image', pixel_width=16, pixel_height=16))
    original = ImageStore._decode_native
    calls = []
    def decode(*args):
        calls.append(True)
        return original(*args)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    scheduler = SceneScheduler()
    entered, release = Event(), Event()
    prepare = scheduler._prepare_feedback
    def held(demand, backend, token):
        if demand.serial == 1 and cancelled:
            entered.set()
            assert release.wait(10)
        return prepare(demand, backend, token)
    monkeypatch.setattr(scheduler, '_prepare_feedback', held)
    request = ProjectionRequest(ProjectionAddress(0, 0, 0), 16, 2)
    def demand(revision):
        frozen = snapshot(images, tiles, revision, chapter=chapter)
        return SceneDemand(revision+1, frozen, (request,), (None,), (8., 8.))
    try:
        scheduler.submit(demand(0))
        if cancelled:
            assert entered.wait(10)
            scheduler.cancel()
            release.set()
        first_results = wait_scheduler(scheduler)
        assert len(calls) == 1
        scheduler.submit(demand(1))
        second_results = wait_scheduler(scheduler)
        assert len(calls) == 1
        assert any(result.tiles for result in second_results)
        assert ('native', obj.object_id) in scheduler._backend.snapshot.images._decoded
        if not cancelled:
            first_tile = next(iter(next(result.tiles for result in first_results if result.tiles).values()))[0]
            second_tile = next(iter(next(result.tiles for result in second_results if result.tiles).values()))[0]
            assert bytes(first_tile.constBits()) == bytes(second_tile.constBits())
    finally:
        release.set()
        scheduler.close()
        scheduler.executor.shutdown(wait=True)
