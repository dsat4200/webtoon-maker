"""Optional publication preserves saved-source aliases without rewriting values."""
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from threading import Event
from types import SimpleNamespace

import pytest
from PySide6.QtGui import QColor, QImage

from comic_editor.core.models import ImageObject, RasterObject
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.resident_cache import ResidentRenderCache
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.disk_cache import DiskCacheController
from test_detached_scene import canvas


@pytest.fixture
def saved_alias_scene(canvas, tmp_path):
    raster = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        RasterObject(interaction_rect=(0, 0, 64, 64)))
    image = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    canvas.tiles.set_tile(raster.object_id, (0, 0), image)
    root = tmp_path / 'cache'
    view = ResidentRenderCache(budget=2 * 1024 * 1024).view(str(root))
    backing = PersistentRenderCache(root, resident=view)
    try:
        pixels = RenderDependencies(canvas, backing).tiles(canvas.tiles, raster.object_id)
        original = tmp_path / 'original.png'
        assert image.save(str(original), 'PNG')
        owner = canvas.tiles._tiles[raster.object_id]
        version = owner.version((0, 0))
        owner.commit((0, 0), original)
        assert RenderDependencies(canvas, backing).tiles(canvas.tiles, raster.object_id) == pixels
        key = ('native-source-probe', pixels)
        with backing.record():
            assert backing.retain('source', key, image, state={'native': True})
        backing.drain()
        descriptor = backing.descriptor('source', key)
        # Re-encoding changes bytes without changing the known native pixels or
        # source version. This private test file remains a valid PNG image.
        adopted = tmp_path / 'adopted.png'
        adopted.write_bytes(original.read_bytes() + b'new-encoding')
        assert QImage(str(adopted)).pixelColor(32, 32) == QColor('red')
        owner.commit((0, 0), adopted)
        assert owner.version((0, 0)) == version
        context = dict(root=root, tiles=canvas.tiles, images=canvas.images,
            view=view, contract=(), environment=())
        yield canvas, context, raster, pixels, descriptor, adopted
    finally:
        backing.close()


def capture_sources(canvas, context):
    capture = DiskCacheController._capture_export_sources(SimpleNamespace(canvas=canvas), [context])
    while True:
        try:
            next(capture)
        except StopIteration as result:
            return result.value


def publish(sources, cancelled=None):
    with ThreadPoolExecutor(max_workers=1) as worker:
        return worker.submit(DiskCacheController._publish_memory, sources,
            cancelled or Event()).result(timeout=20)


def test_existing_values_publish_updated_source_seals_and_keep_sibling_entries(saved_alias_scene):
    canvas, context, raster, pixels, descriptor, adopted = saved_alias_scene
    index = context['root'] / 'index.json'
    before = json.loads(index.read_text())
    old_entry = before['entries'][descriptor.identity]
    sources = capture_sources(canvas, context)
    # Another ordinary writer can publish after the source capture. Metadata
    # publication must merge its manifest rather than flush a stale index.
    sibling = PersistentRenderCache(context['root'])
    try:
        image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
        image.fill(QColor('blue'))
        with sibling.record():
            assert sibling.retain('source', ('sibling',), image)
        sibling.drain()
        sibling_identity = sibling.descriptor('source', ('sibling',)).identity
    finally:
        sibling.close()
    assert publish(sources) == (True, '')
    after = json.loads(index.read_text())
    assert after['entries'][descriptor.identity] == old_entry
    assert sibling_identity in after['entries']
    logical = f'tile/{raster.object_id}/0_0'
    assert after['sources'][logical]['digest'] == hashlib.sha256(adopted.read_bytes()).hexdigest()
    assert tuple(after['sources'][logical]['pixels']) == pixels[0][1]
    reopened = PersistentRenderCache(context['root'])
    try:
        assert reopened.resident is None
        assert RenderDependencies(canvas, reopened).tiles(canvas.tiles, raster.object_id) == pixels
        assert reopened.lookup('source', descriptor.key, wait=True).pixelColor(32, 32) == QColor('red')
    finally:
        reopened.close()


def test_cancelled_export_does_not_publish_source_only_metadata(saved_alias_scene, monkeypatch):
    canvas, context, *_ = saved_alias_scene
    index = context['root'] / 'index.json'
    before = index.read_bytes()
    cancelled = Event()
    original = PersistentRenderCache.lookup
    def lookup(backing, *args, **kwargs):
        result = original(backing, *args, **kwargs)
        cancelled.set()
        return result
    monkeypatch.setattr(PersistentRenderCache, 'lookup', lookup)
    assert publish(capture_sources(canvas, context), cancelled) == (False, '')
    assert index.read_bytes() == before


def test_source_only_publication_cannot_cross_a_saved_cache_clear(saved_alias_scene, monkeypatch):
    canvas, context, *_ = saved_alias_scene
    original = PersistentRenderCache._publish_index
    def clear_before_commit(backing, snapshot, sources, epoch):
        sibling = PersistentRenderCache(backing.root)
        try:
            sibling.clear()
        finally:
            sibling.close()
        return original(backing, snapshot, sources, epoch)
    monkeypatch.setattr(PersistentRenderCache, '_publish_index', clear_before_commit)
    with pytest.raises(RuntimeError, match='Saved cache changed'):
        publish(capture_sources(canvas, context))
    assert not json.loads((context['root'] / 'index.json').read_text())['entries']


def test_export_capture_owns_decoded_native_source_without_encoded_payload(canvas, tmp_path):
    obj = canvas.chapter.add_object(canvas.chapter.root_page_ids[0],
        ImageObject(pixel_width=8, pixel_height=8))
    image = QImage(8, 8, QImage.Format_RGBA64_Premultiplied)
    image.fill(QColor.fromRgbF(.7, .3, .1, .75))
    canvas.images.put_decoded(obj.object_id, 'native.png', b'', image)
    expected = bytes(image.constBits())
    root = tmp_path / 'cache'
    view = ResidentRenderCache(budget=2 * 1024 * 1024).view(str(root))
    backing = PersistentRenderCache(root, resident=view)
    reopened = None
    try:
        fingerprint = RenderDependencies(canvas, backing).image(canvas.images, obj.object_id)
        key = ('decoded-native-source', fingerprint)
        backing.retain('source', key, image)
        context = dict(root=root, tiles=canvas.tiles, images=canvas.images,
            view=view, contract=(), environment=())
        sources = capture_sources(canvas, context)
        captured = sources[0][2]
        assert captured.native_image(obj.object_id).format() == image.format()
        assert bytes(captured.native_image(obj.object_id).constBits()) == expected
        canvas.images._decoded[('native', obj.object_id)].fill(QColor('blue'))
        assert bytes(captured.native_image(obj.object_id).constBits()) == expected
        assert publish(sources) == (True, '')
        reopened = PersistentRenderCache(root)
        cached = reopened.lookup('source', key, wait=True)
        assert cached.format() == image.format()
        assert bytes(cached.constBits()) == expected
    finally:
        if reopened is not None:
            reopened.close()
        backing.close()
