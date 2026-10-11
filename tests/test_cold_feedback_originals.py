"""Cold feedback borrows validated originals without waiting for scene work."""
from concurrent.futures import Future
from threading import Event, get_ident

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication
from PySide6.QtGui import QColor

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject
from comic_editor.render.projection import ProjectionAddress, ProjectionRequest
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.source_resources import ReadyOriginals
from test_ready_original_sources import sources, snapshot, png, native, same_pixels, wait_scheduler


def scene_inputs(tmp_path):
    expected, images, tiles = sources(tmp_path)
    chapter = ChapterDocument(width=16, height=16, document_kind='asset')
    page = chapter.add_page('Page', BoundGeometry.rectangle(0, 0, 16, 16))
    page.fill_color, page.border_width = None, 0
    image = chapter.add_object(page.layer_id, ImageObject(object_id='image', pixel_width=16, pixel_height=16))
    selected = chapter.add_object(page.layer_id, RasterObject(object_id='raster', tile_size=16))
    page.children.reverse()
    request = ProjectionRequest(ProjectionAddress(0, 0, 0), 16, 2)
    return expected, images, tiles, chapter, selected, request


def ordinary(snapshot, request):
    return SceneDemand(1, snapshot, (request,), (None,), (8., 8.))


def feedback(snapshot, selected, request, originals):
    return SceneDemand(2, snapshot, (request,), (None,), (8., 8.), (0., 0., 16., 16.),
        consumer='raster-contact', feedback_target=selected.object_id,
        feedback_only=True, feedback_originals=originals)


@pytest.mark.parametrize('mutation', ['unchanged', 'image-pin', 'native-pixels', 'native-pin'])
def test_feedback_only_worker_reuses_originals_with_generation_and_pin_guards(
        qapp, tmp_path, monkeypatch, mutation):
    _expected, images, tiles, chapter, selected, request = scene_inputs(tmp_path)
    original = ImageStore._decode_native
    decodes = []
    def decode(data):
        decodes.append(True)
        return original(data)
    monkeypatch.setattr(ImageStore, '_decode_native', staticmethod(decode))
    gui_thread = get_ident()
    captured_threads = []
    capture = ReadyOriginals.capture
    def worker_capture(self, source):
        captured_threads.append(get_ident())
        assert get_ident() != gui_thread
        return capture(self, source)
    monkeypatch.setattr(ReadyOriginals, 'capture', worker_capture)
    ordinary_scheduler, contact_scheduler = SceneScheduler(), SceneScheduler()
    try:
        first = snapshot(images, tiles, chapter=chapter)
        ordinary_scheduler.submit(ordinary(first, request))
        wait_scheduler(ordinary_scheduler)
        assert len(decodes) == 1
        originals = ordinary_scheduler.ready_feedback_originals(first.document.identity)
        originals.result(timeout=5.)
        assert captured_threads
        replacement = None
        if mutation == 'image-pin':
            replacement = native()
            np.frombuffer(replacement.bits(), np.uint16).reshape(16, 16, 4)[:] = (12991, 21991, 31991, 11)
            images.put('image', 'replacement.png', png(replacement), 'image/png')
            images._decoded.clear()
            images.decoded_bytes = 0
            # Resource import verification is outside the detached decode count.
            decodes.clear()
        elif mutation == 'native-pixels':
            replacement = tiles.tile('raster', (0, 0))
            replacement.setPixelColor(3, 4, QColor.fromRgbF(.91, .19, .39, .41))
        elif mutation == 'native-pin':
            replacement = native()
            np.frombuffer(replacement.bits(), np.uint16).reshape(16, 16, 4)[:] = (4991, 6991, 8991, 3)
            from comic_editor.core.tiles import TileStore
            newer = TileStore(16)
            newer.set_tile('raster', (0, 0), replacement)
            newer.save_directory(tmp_path/'replacement', {'raster'}, complete=True)
            tiles = TileStore(16)
            tiles.load_directory(tmp_path/'replacement', {'raster'})
        second = snapshot(images, tiles, 1, chapter=chapter)
        contact_scheduler.submit(feedback(second, selected, request, originals))
        results = wait_scheduler(contact_scheduler)
        assert any(item.feedback is not None for item in results)
        assert all(item.tiles is None and item.preview is None for item in results)
        assert originals.result().byte_count == 0
        assert second.cache_spec is None
        if mutation == 'image-pin':
            assert len(decodes) == 1
            same_pixels(second.images.native_image('image'), replacement)
        else:
            assert len(decodes) == 1, 'Unchanged encoded originals must not decode again'
        if mutation in {'native-pixels', 'native-pin'}:
            same_pixels(second.tiles.tile('raster', (0, 0)), replacement)
        if mutation == 'unchanged':
            assert second.tiles.residency.decodes == 0
    finally:
        for scheduler in (ordinary_scheduler, contact_scheduler):
            scheduler.close()
            scheduler.executor.shutdown(wait=True, cancel_futures=False)


@pytest.mark.parametrize('future_state', ['pending-on-ordinary-worker', 'cancelled'])
def test_cold_contact_never_waits_for_an_unready_or_cancelled_originals_handoff(
        qapp, tmp_path, monkeypatch, future_state):
    _expected, images, tiles, chapter, selected, request = scene_inputs(tmp_path)
    ordinary_scheduler, contact_scheduler = SceneScheduler(), SceneScheduler()
    release, entered = Event(), Event()
    try:
        first = snapshot(images, tiles, chapter=chapter)
        ordinary_scheduler.submit(ordinary(first, request))
        wait_scheduler(ordinary_scheduler)
        if future_state == 'pending-on-ordinary-worker':
            def blocked():
                entered.set()
                assert release.wait(10.)
            blocked_job = ordinary_scheduler.executor.submit(blocked)
            assert entered.wait(5.)
            originals = ordinary_scheduler.ready_feedback_originals(first.document.identity)
            assert not originals.done()
        else:
            originals = Future()
            originals.cancel()
        second = snapshot(images, tiles, 1, chapter=chapter)
        contact_scheduler.submit(feedback(second, selected, request, originals))
        results = wait_scheduler(contact_scheduler)
        assert any(item.feedback is not None for item in results)
        if future_state == 'pending-on-ordinary-worker':
            assert not originals.done() and not blocked_job.done()
            assert not release.is_set()
        else:
            assert originals.cancelled()
    finally:
        release.set()
        for scheduler in (ordinary_scheduler, contact_scheduler):
            scheduler.close()
            scheduler.executor.shutdown(wait=True, cancel_futures=False)
