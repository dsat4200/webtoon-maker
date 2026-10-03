from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.core.images import ImageStore
from comic_editor.core.models import RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.ui.autosave import RecoverySnapshot


def project(tmp_path):
    repository = SeriesRepository(tmp_path / 'project')
    series = repository.create('Storage')
    chapter, tiles = repository.create_chapter(series, 'Chapter')
    obj = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    for x in range(4):
        tiles.paint_dab(obj.object_id, QPointF(x*256+40, 50), 20, QColor('red'))
    repository.save_chapter(chapter, tiles)
    return repository, chapter, obj.object_id


def test_unsaved_tiles_share_bounded_residency_and_spill_exact_pixels(tmp_path):
    import numpy as np
    store = TileStore(tile_size=16, cache_budget=16*16*4*2)
    expected, versions = {}, {}
    for index in range(20):
        data = np.random.default_rng(index).integers(0, 256, (16, 16, 4), np.uint8)
        image = QImage(data.data, 16, 16, data.strides[0], QImage.Format_RGBA8888_Premultiplied).copy()
        identifier, key = f'art-{index%3}', (index, -1)
        store.set_tile(identifier, key, image)
        expected[identifier, key] = QImage(image)
        versions[identifier, key] = store._object_tiles(identifier).version(key)
        assert store.residency.bytes <= store.residency.budget
    assert store.residency._spill_cache.spills >= 18
    snapshot = store.detached_snapshot({'art-0', 'art-1', 'art-2'})
    for (identifier, key), image in expected.items():
        assert store.tile(identifier, key) == image
        assert store._object_tiles(identifier).version(key) == versions[identifier, key]
        assert snapshot.tile(identifier, key) == image
        assert snapshot.residency.bytes <= snapshot.residency.budget
        # Capture an edit through a borrowed resident image before eviction.
        borrowed = store.tile(identifier, key)
        borrowed.setPixelColor(3, 4, QColor('red'))
        store.tile('art-0', (0, -1))
        store.tile('art-1', (1, -1))
        assert store.tile(identifier, key).pixelColor(3, 4) == QColor('red')
        assert snapshot.tile(identifier, key) == image
        assert store.residency.bytes <= store.residency.budget
    # Private spills do not count as committed project files.
    assert len(store.dirty) == len(expected)
    store.save_directory(tmp_path/'saved', {'art-0', 'art-1', 'art-2'})
    assert not store.dirty
    reloaded = TileStore(tile_size=16, cache_budget=16*16*4*2)
    reloaded.load_directory(tmp_path/'saved', {'art-0', 'art-1', 'art-2'})
    for identifier, key in expected:
        assert reloaded.tile(identifier, key).pixelColor(3, 4) == QColor('red')


def test_open_and_bounds_use_index_without_decoding_pixels(tmp_path):
    repository, chapter, oid = project(tmp_path)
    loaded, tiles = repository.load_chapter(chapter.chapter_id)
    assert tiles.residency.decodes == 0
    assert tiles.content_bounds(oid) == QRectF(30, 40, 788, 20)
    # Bounds queries should use saved metadata, not cause a residency churn.
    assert tiles.residency.decodes == 0
    assert tiles.tile(oid, (2, 0)).pixelColor(40, 50) == QColor('red')
    assert tiles.residency.decodes == 1


def test_effect_signature_survives_eviction_and_save_without_decoding_cold_tiles(tmp_path):
    repository, chapter, oid = project(tmp_path)
    _, tiles = repository.load_chapter(chapter.chapter_id)
    signature = tiles.object_signature(oid)
    assert len(signature) == 4 and tiles.residency.decodes == 0
    original = tiles.tile(oid, (0, 0))
    assert tiles.object_signature(oid) == signature
    original.setPixelColor(40, 50, QColor('blue'))
    edited = tiles.object_signature(oid)
    assert edited != signature
    assert tiles.residency.decodes == 1
    tiles.residency.clear()
    assert tiles.object_signature(oid) == edited and not tiles.residency.bytes
    assert tiles.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('blue')
    assert tiles.object_signature(oid) == edited
    tiles.save_directory(tmp_path/'published', {oid})
    assert tiles.object_signature(oid) == edited
    tiles.tile(oid, (0, 0))
    assert tiles.object_signature(oid) == edited


def test_recovery_snapshot_pins_lazy_pixels_across_replacement_and_removal(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    snapshot = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    assert tiles.residency.decodes == snapshot.tile_store.residency.decodes == 0
    original = snapshot.tiles[oid].backing((0, 0))
    tiles.paint_dab(oid, QPointF(40, 50), 20, QColor('blue'))
    repository.save_chapter(chapter, tiles)
    obj = chapter.objects.pop(oid)
    parent = chapter.layers[obj.parent_layer_id]
    parent.children = [child for child in parent.children if child.entity_id != oid]
    repository.save_chapter(chapter, tiles)
    assert original.is_file()
    snapshot.write()
    recovered, pixels = repository.load_chapter(chapter.chapter_id, recover=True)
    assert oid in recovered.objects
    assert pixels.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('red')


def test_recovery_snapshot_detaches_borrowed_edit_and_reuses_committed_versions(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    borrowed = tiles.tile(oid, (0, 0))
    borrowed.setPixelColor(2, 2, QColor('blue'))
    first = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    borrowed.setPixelColor(2, 2, QColor('green'))
    first.write()
    _, recovered = repository.load_chapter(chapter.chapter_id, recover=True)
    assert recovered.tile(oid, (0, 0)).pixelColor(2, 2) == QColor('blue')
    second = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    second.saved_tiles, second.saved_images = first.saved_tiles, first.saved_images
    second.write()
    tile_root = repository.chapter_root(chapter.chapter_id) / 'autosave' / 'raster' / oid
    times = {path.name: path.stat().st_mtime_ns for path in tile_root.glob('*.png')}
    third = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    third.saved_tiles, third.saved_images = second.saved_tiles, second.saved_images
    third.write()
    assert times == {path.name: path.stat().st_mtime_ns for path in tile_root.glob('*.png')}
    _, recovered = repository.load_chapter(chapter.chapter_id, recover=True)
    assert recovered.tile(oid, (0, 0)).pixelColor(2, 2) == QColor('green')
    # Captured inputs stay immutable after their own publication, too.
    assert first.tiles[oid][(0, 0)].pixelColor(2, 2) == QColor('blue')


def test_snapshot_pins_reuse_and_worker_bounds_require_matching_generation(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    tiles._alpha_bounds.clear()
    first = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    second = RecoverySnapshot.capture(repository.root, chapter, tiles, ImageStore())
    assert first.tiles[oid].backing((0, 0)) == second.tiles[oid].backing((0, 0))
    tiles.paint_dab(oid, QPointF(230, 230), 20, QColor('blue'))
    first.write()
    tiles.adopt_snapshot_bounds(first.derived_bounds)
    # Old bounds for edited tile must not overwrite its new occupied area.
    assert tiles.content_bounds(oid).contains(QPointF(235, 235))
    assert (1, 0) in tiles._alpha_bounds[oid]


def test_loading_revision_pins_before_concurrent_publish_and_visible_tiles_take_priority(tmp_path, monkeypatch):
    from comic_editor.core import tile_backing
    repository, chapter, oid = project(tmp_path)
    chapter, writer = repository.load_chapter(chapter.chapter_id)
    writer.finish_snapshot_prefetch()
    entered, release, publishing = Event(), Event(), Event()
    original = tile_backing.SnapshotBacking.pin
    def blocked(backing, path):
        if current_thread().name.startswith('tile-pins') and path.name == '3_0.png':
            entered.set()
            assert release.wait(5)
        return original(backing, path)
    monkeypatch.setattr(tile_backing.SnapshotBacking, 'pin', blocked)
    reader_chapter, reader = repository.load_chapter(chapter.chapter_id)
    assert entered.wait(2)
    # A blocked unrelated tile must not hold the shared pin lookup lock.
    assert reader.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('red')
    writer.paint_dab(oid, QPointF(3*256+40, 50), 20, QColor('blue'))
    def publish():
        publishing.set()
        repository.save_chapter(chapter, writer)
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(publish)
        try:
            assert publishing.wait(2)
            assert not future.done()
            assert not (repository.chapter_root(chapter.chapter_id)/'.save_pending').exists()
        finally:
            release.set()
        future.result(timeout=5)
    assert reader.tile(oid, (3, 0)).pixelColor(40, 50) == QColor('red')
    _, latest = repository.load_chapter(chapter.chapter_id)
    assert latest.tile(oid, (3, 0)).pixelColor(40, 50) == QColor('blue')
    # Private generation numbers can coincide across stores. The destination
    # inode must also match before an incremental save skips its frozen pixels.
    repository.save_chapter(reader_chapter, reader)
    _, restored = repository.load_chapter(chapter.chapter_id)
    assert restored.tile(oid, (3, 0)).pixelColor(40, 50) == QColor('red')
    assert latest.tile(oid, (3, 0)).pixelColor(40, 50) == QColor('blue')


def test_lazy_residency_evicts_clean_pixels_and_preserves_borrowed_edit(tmp_path):
    repository, chapter, oid = project(tmp_path)
    root = repository.chapter_root(chapter.chapter_id) / 'raster'
    tiles = TileStore(cache_budget=256*256*4)
    tiles.load_directory(root, {oid})
    first = tiles.tile(oid, (0, 0))
    first.setPixelColor(1, 1, QColor('blue'))
    for x in range(1, 4):
        tiles.tile(oid, (x, 0))
    assert tiles.residency.bytes <= tiles.residency.budget
    assert tiles.residency.evictions >= 3
    assert tiles.tile(oid, (0, 0)).pixelColor(1, 1) == QColor('blue')
    assert (oid, 0, 0) in tiles.dirty
    tiles.save_directory(root, {oid}, complete=True)
    tiles.commit_directory(root)
    reopened = TileStore()
    reopened.load_directory(root, {oid})
    assert reopened.tile(oid, (0, 0)).pixelColor(1, 1) == QColor('blue')


def test_metadata_save_preserves_resource_files_and_previous_revision(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    root = repository.chapter_root(chapter.chapter_id)
    times = {p.name: p.stat().st_mtime_ns for p in (root/'raster'/oid).glob('*.png')}
    chapter.name = 'Metadata only'
    repository.save_chapter(chapter, tiles)
    assert times == {p.name: p.stat().st_mtime_ns for p in (root/'raster'/oid).glob('*.png')}
    assert tiles.residency.decodes == 0
    assert (root/'last_good'/'chapter.json').is_file()
    assert (root/'last_good'/'raster'/oid/'0_0.png').read_bytes() == (root/'raster'/oid/'0_0.png').read_bytes()


def test_reused_backup_links_retain_exact_previous_revision_after_failure(tmp_path, monkeypatch):
    from comic_editor.core import persistence
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    chapter.name = 'Second revision'
    repository.save_chapter(chapter, tiles)
    links = []
    original_link = persistence.link_or_copy
    def counted(source, destination):
        links.append(Path(source).name)
        return original_link(source, destination)
    monkeypatch.setattr(persistence, 'link_or_copy', counted)
    chapter.name = 'Metadata third revision'
    repository.save_chapter(chapter, tiles)
    assert not any(name.endswith('.png') for name in links)
    tiles.paint_dab(oid, QPointF(40, 50), 20, QColor('blue'))
    chapter.name = 'Blue committed'
    repository.save_chapter(chapter, tiles)
    tiles.paint_dab(oid, QPointF(40, 50), 20, QColor('green'))
    chapter.name = 'Green interrupted'
    images = ImageStore()
    monkeypatch.setattr(images, 'save_directory', lambda *_a, **_k:
        (_ for _ in ()).throw(OSError('Interrupted mirror save')))
    with pytest.raises(OSError, match='Interrupted'):
        repository.save_chapter(chapter, tiles, images)
    recovered, pixels = repository.load_chapter(chapter.chapter_id)
    assert recovered.name == 'Blue committed'
    assert pixels.tile(oid, (0, 0)).pixelColor(40, 50) == QColor('blue')


def test_manual_and_autosave_track_independent_resource_revisions(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    repository.save_chapter(chapter, tiles, autosave=True)
    tiles.paint_dab(oid, QPointF(100,100), 20, QColor('green'))
    repository.save_chapter(chapter, tiles)
    # The manual save clears dirty flags, but the next autosave must still copy
    # the change and must not depend on its removed earlier backing directory.
    repository.save_chapter(chapter, tiles, autosave=True)
    _, recovered = repository.load_chapter(chapter.chapter_id, recover=True)
    assert recovered.tile(oid, (0,0)).pixelColor(100,100) == QColor('green')
    tiles.paint_dab(oid, QPointF(130,100), 20, QColor('blue'))
    repository.save_chapter(chapter, tiles, autosave=True)
    repository.save_chapter(chapter, tiles)
    _, reopened = repository.load_chapter(chapter.chapter_id)
    assert reopened.tile(oid, (0,0)).pixelColor(130,100) == QColor('blue')
    assert not (repository.chapter_root(chapter.chapter_id)/'autosave').exists()


def test_failed_save_recovers_previous_pixels_and_retry_keeps_unsaved_paint(tmp_path, monkeypatch):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    original = QImage(tiles.tile(oid, (0,0)))
    tiles.paint_dab(oid, QPointF(110,110), 20, QColor('blue'))
    images = ImageStore()
    with monkeypatch.context() as patch:
        def fail(*args, **kwargs):
            raise OSError('Injected image publication failure')
        patch.setattr(images, 'save_directory', fail)
        with pytest.raises(OSError, match='Injected'):
            repository.save_chapter(chapter, tiles, images)
    _, recovered = repository.load_chapter(chapter.chapter_id)
    assert recovered.tile(oid, (0,0)) == original
    assert tiles.tile(oid, (0,0)).pixelColor(110,110) == QColor('blue')
    repository.save_chapter(chapter, tiles, images)
    _, reopened = repository.load_chapter(chapter.chapter_id)
    assert reopened.tile(oid, (0,0)).pixelColor(110,110) == QColor('blue')


def test_decoded_image_lru_owns_pixels_within_budget():
    store = ImageStore(decoded_budget=4*16*16)
    image = QImage(16,16,QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    for index in range(4):
        store.put_decoded(str(index), 'image.png', b'already-validated', image)
    assert store.decoded_bytes <= store.decoded_budget
    assert len(store._decoded) == 1
    assert set(store.snapshot()) == {'0','1','2','3'}
    # Editing a returned Qt copy must leave the retained display pixels intact.
    borrowed = store.image('3')
    borrowed.fill(QColor('blue'))
    assert store.image('3').pixelColor(0,0) == QColor('red')


def test_deleted_object_pixels_survive_saves_for_metadata_undo(tmp_path):
    repository, chapter, oid = project(tmp_path)
    chapter, tiles = repository.load_chapter(chapter.chapter_id)
    obj = chapter.objects.pop(oid)
    parent = chapter.layers[obj.parent_layer_id]
    children = list(parent.children)
    parent.children = [child for child in parent.children if child.entity_id != oid]
    repository.save_chapter(chapter, tiles)
    # A later save replaces last_good. History must own pixels independently.
    repository.save_chapter(chapter, tiles)
    chapter.objects[oid] = obj
    parent.children = children
    assert tiles.tile(oid,(0,0)).pixelColor(40,50) == QColor('red')
    repository.save_chapter(chapter, tiles)
    _, reopened = repository.load_chapter(chapter.chapter_id)
    assert reopened.tile(oid,(0,0)).pixelColor(40,50) == QColor('red')
