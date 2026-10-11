"""Shared cache metadata remains bounded and scoped to source ownership."""
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from PySide6.QtGui import QColor, QImage

from comic_editor.render.cache import CacheDescriptor, PersistentRenderCache, digest
from comic_editor.render.resident_cache import ResidentRenderCache


def test_runtime_pixel_witnesses_share_bounded_lru_across_namespaces():
    cache = ResidentRenderCache(budget=1024, metadata_limit=3)
    first, second = cache.view("first"), cache.view("second")
    first.remember_pixel_identity("ink", (0, 0), (1, 2), None, ("pixels", "red"))
    first.remember_pixel_identity("ink", (1, 0), (1, 3), None, ("pixels", "blue"))
    second.remember_pixel_identity("ink", (0, 0), (0, 0), None, ("encoded", "white"))
    assert first.pixel_identity("ink", (0, 0)) == ((1, 2), "None", ("pixels", "red"))
    second.remember_pixel_identity("ink", (1, 0), (0, 0), None, ("encoded", "black"))
    assert len(cache.pixels) == 3
    assert first.pixel_identity("ink", (1, 0)) is None
    assert first.pixel_identity("ink", (0, 0)) is not None
    assert second.pixel_identity("ink", (0, 0)) is not None
    assert ResidentRenderCache(metadata_limit=10**9).metadata_limit == 65536


def test_rebind_clears_only_namespace_runtime_witnesses(qapp):
    cache = ResidentRenderCache(budget=1024)
    first, second = cache.view("first"), cache.view("second")
    first.remember_pixel_identity("ink", (0, 0), (0, 0), None, ("pixels", "red"))
    second.remember_pixel_identity("ink", (0, 0), (0, 0), None, ("pixels", "blue"))
    descriptor = CacheDescriptor("projection", ("tile", 0), (), ())
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    assert first.retain(descriptor, image)
    first.remember_source("source", {"digest": "validated-file", "stamp": [1, 2]})
    first.clear_runtime()
    assert first.pixel_identity("ink", (0, 0)) is None
    assert second.pixel_identity("ink", (0, 0)) is not None
    assert first.has(descriptor)
    assert bytes(first.lookup(descriptor).constBits()) == bytes(image.constBits())
    assert first.source_digests()["source"]["digest"] == "validated-file"


def test_single_source_publication_preserves_seal_without_aliasing_record(tmp_path):
    resident = ResidentRenderCache().view("chapter")
    backing = PersistentRenderCache(tmp_path, resident=resident)
    try:
        logical = "tile/ink/0_0"
        stamp, content, pixels = [1, 2, 3, 4, 5], "a" * 64, ("pixels", 256, 256, 6, "b" * 64)
        record = dict(stamp=stamp, digest=content, pixels=pixels,
            seal=digest((logical, stamp, content, pixels)))
        backing.remember_source(logical, record)
        record["stamp"][0] = 999
        retained = resident.source_digests()[logical]
        assert retained["stamp"][0] == 1
        assert retained["seal"] == digest((logical, retained["stamp"], retained["digest"], retained["pixels"]))
    finally:
        backing.close()


def test_retirement_invalidates_all_old_views_without_touching_another_namespace(qapp):
    cache = ResidentRenderCache(budget=4096)
    old, sibling, other = cache.view('chapter'), cache.view('chapter'), cache.view('other')
    descriptor = CacheDescriptor('projection', ('tile', 0), (), ())
    red = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    red.fill(QColor('red'))
    assert old.retain(descriptor, red, {'saved': True})
    assert other.retain(descriptor, red)
    old.remember_source('source', {'digest': 'old'})
    old.remember_pixel_identity('ink', (0, 0), (0, 0), None, ('pixels', 'old'))
    cache.eligible.update(('chapter', 'other'))
    assert old.retire()
    assert cache.bytes == red.sizeInBytes()
    assert 'chapter' not in cache.sources
    assert not any(marker[0] == 'chapter' for marker in cache.pixels)
    assert cache.eligible == {'other'}
    fresh = cache.view('chapter')
    blue = QImage(red)
    blue.fill(QColor('blue'))
    assert fresh.generation != old.generation
    assert fresh.retain(descriptor, blue, {'saved': False})
    fresh.remember_source('source', {'digest': 'fresh'})
    fresh.remember_pixel_identity('ink', (0, 0), (0, 0), None, ('pixels', 'fresh'))
    for retired in (old, sibling):
        assert not retired.has(descriptor)
        assert retired.lookup(descriptor) is None
        assert retired.state(descriptor) is None
        assert retired.bytes == 0 and retired.snapshot() == ()
        assert not retired.retain(descriptor, red)
        retired.remember_source('source', {'digest': 'late'})
        retired.remember_sources({'late': {'digest': 'late'}})
        retired.remember_pixel_identity('ink', (0, 0), (0, 0), None, ('pixels', 'late'))
        assert retired.pixel_identity('ink', (0, 0)) is None
        assert retired.source_digests() == {}
        retired.clear()
        retired.clear_runtime()
        assert not retired.retire()
    assert fresh.lookup(descriptor).pixelColor(0, 0) == QColor('blue')
    assert fresh.state(descriptor) == {'saved': False}
    assert fresh.source_digests() == {'source': {'digest': 'fresh'}}
    assert fresh.pixel_identity('ink', (0, 0))[2] == ('pixels', 'fresh')
    assert other.lookup(descriptor).pixelColor(0, 0) == QColor('red')
    # A normal cache clear does not invalidate still-open worker views.
    fresh.clear()
    assert fresh.retain(descriptor, red)


def test_worker_finishing_after_retirement_cannot_repopulate_reopened_namespace(qapp):
    cache = ResidentRenderCache(budget=4096)
    old = cache.view('chapter')
    descriptor = CacheDescriptor('projection', ('tile', 0), (), ())
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor('red'))
    entered, released = Event(), Event()
    def finish_old_job():
        entered.set()
        assert released.wait(5)
        old.remember_source('source', {'digest': 'late'})
        old.remember_pixel_identity('ink', (0, 0), (0, 0), None, ('pixels', 'late'))
        return old.retain(descriptor, image)
    with ThreadPoolExecutor(max_workers=1) as worker:
        future = worker.submit(finish_old_job)
        assert entered.wait(5)
        assert old.retire()
        fresh = cache.view('chapter')
        released.set()
        assert future.result(timeout=5) is False
    assert fresh.bytes == 0 and not cache.entries and cache.bytes == 0
    assert fresh.source_digests() == {}
    assert fresh.pixel_identity('ink', (0, 0)) is None
