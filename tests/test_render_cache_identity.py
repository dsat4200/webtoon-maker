"""Descriptor memoization preserves the existing exact durable identities."""
from concurrent.futures import ThreadPoolExecutor
import sys

import numpy as np
import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.render import cache as storage


@pytest.fixture(autouse=True)
def empty_identity_memo():
    with storage._identity_lock:
        storage._identities.clear()
        storage._identity_bytes = 0
    yield
    with storage._identity_lock:
        storage._identities.clear()
        storage._identity_bytes = 0


def original_identity(descriptor):
    return storage.digest((storage.CACHE_VERSION, storage.RENDERER_VERSION,
        descriptor.kind, descriptor.key, descriptor.contract,
        descriptor.environment, sys.byteorder))


def counted_digests(monkeypatch):
    calls = []
    original = storage.digest
    def digest(value):
        calls.append(value)
        return original(value)
    monkeypatch.setattr(storage, "digest", digest)
    return calls


@pytest.mark.parametrize("value", [None, True, False, 0, 1, -5, 2**90,
    0., -0., 1., -1.25, 1e-300, "chapter \u2603", np.int64(14), np.float32(.25)])
def test_immutable_identity_matches_original_and_reuses_hash(monkeypatch, value):
    descriptor = storage.CacheDescriptor("effect", ("stage", value, (2, .125)),
        ("float32", "linear_srgb"), ("original-source",))
    expected = original_identity(descriptor)
    calls = counted_digests(monkeypatch)
    assert descriptor.identity == expected
    # Rebuilt equal immutable keys also reuse the digest.
    assert storage.CacheDescriptor(descriptor.kind, tuple(list(descriptor.key)),
        descriptor.contract, descriptor.environment).identity == expected
    assert len(calls) == 1


def test_python_equal_numeric_keys_remain_distinct():
    values = [True, 1, 1., False, 0, 0., -0.]
    descriptors = [storage.CacheDescriptor("effect", (value,)) for value in values]
    identities = [descriptor.identity for descriptor in descriptors]
    assert len(set(identities)) == len(values)
    assert identities == [original_identity(descriptor) for descriptor in descriptors]


@pytest.mark.parametrize("mutable", [[1, 2], {"radius": 2}, QRectF(1, 2, 3, 4)])
def test_mutable_keys_are_never_memoized(monkeypatch, mutable):
    descriptor = storage.CacheDescriptor("effect", ("stage", mutable))
    calls = counted_digests(monkeypatch)
    first = descriptor.identity
    assert descriptor.identity == first
    assert not storage._identities
    if isinstance(mutable, list):
        mutable.append(3)
    elif isinstance(mutable, dict):
        mutable["radius"] = 3
    else:
        mutable.translate(.000001, .000002)
    assert descriptor.identity != first
    assert len(calls) == 3


def test_mutable_contract_and_environment_are_checked_each_time():
    contract, environment = ["srgb"], {"sources": 1}
    descriptor = storage.CacheDescriptor("effect", ("stage",), (contract,), (environment,))
    first = descriptor.identity
    contract[0] = "linear_srgb"
    second = descriptor.identity
    environment["sources"] = 2
    third = descriptor.identity
    assert len({first, second, third}) == 3
    assert not storage._identities


def test_descriptor_versions_contract_and_environment_are_part_of_memo(monkeypatch):
    descriptor = storage.CacheDescriptor("effect", ("stage",), ("srgb",), ("source",))
    first = descriptor.identity
    with monkeypatch.context() as patch:
        patch.setattr(storage, "RENDERER_VERSION", "future-native-renderer")
        assert descriptor.identity != first
    with monkeypatch.context() as patch:
        patch.setattr(storage, "CACHE_VERSION", storage.CACHE_VERSION + 1)
        assert descriptor.identity != first
    assert descriptor.identity == first
    assert storage.CacheDescriptor("effect", ("stage",), ("linear_srgb",), ("source",)).identity != first
    assert storage.CacheDescriptor("effect", ("stage",), ("srgb",), ("replaced-source",)).identity != first


@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_invalid_nonfinite_keys_keep_original_error(value):
    descriptor = storage.CacheDescriptor("effect", (value,))
    for _ in range(2):
        with pytest.raises(ValueError):
            _ = descriptor.identity
    assert not storage._identities


def test_identity_memo_bounds_entries_and_retained_bytes(monkeypatch):
    monkeypatch.setattr(storage, "IDENTITY_LIMIT", 2)
    calls = counted_digests(monkeypatch)
    for number in range(3):
        _ = storage.CacheDescriptor("effect", (number,)).identity
    assert len(storage._identities) == 2
    _ = storage.CacheDescriptor("effect", (0,)).identity
    assert len(calls) == 4
    monkeypatch.setattr(storage, "IDENTITY_BUDGET", 1)
    for _ in range(2):
        _ = storage.CacheDescriptor("effect", ("oversized",)).identity
    assert len(calls) == 6
    # An oversized token is never admitted or given an unbounded exception.
    assert all("oversized" not in repr(key) for key in storage._identities)


def test_identity_memo_is_thread_safe():
    descriptors = [storage.CacheDescriptor("effect", (number, -0., ("dependency",)))
                   for number in range(16)]
    expected = [original_identity(descriptor) for descriptor in descriptors]
    with ThreadPoolExecutor(max_workers=4) as executor:
        identities = list(executor.map(lambda i: descriptors[i % 16].identity, range(256)))
    assert identities == [expected[i % 16] for i in range(256)]
    assert len(storage._identities) == 16
    assert 0 < storage._identity_bytes <= storage.IDENTITY_BUDGET


def test_empty_index_miss_does_not_hash_and_publication_remains_visible(tmp_path, monkeypatch):
    cache = storage.PersistentRenderCache(tmp_path)
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    try:
        with monkeypatch.context() as patch:
            patch.setattr(cache, "descriptor", lambda *args: pytest.fail("Empty index hashed a key"))
            assert cache.lookup("effect", ("stage",)) is None
            assert not cache.has("effect", ("stage",))
        assert cache.misses == 1
        with cache.record():
            cache.retain("effect", ("stage",), image)
        assert cache.lookup("effect", ("stage",)) is None
        cache.drain()
        assert cache.has("effect", ("stage",))
        assert bytes(cache.lookup("effect", ("stage",), wait=True).constBits()) == bytes(image.constBits())
        identity = cache.descriptor("effect", ("stage",)).identity
        # Neither a descriptor memo hit nor a ready read bypasses validation.
        cache._ready_put(identity, image)
        cache.entries[identity]["seal"] = "invalid"
        assert cache.lookup("effect", ("stage",)) is None
        assert not cache.ready and cache.ready_bytes == 0
        assert not cache.has("effect", ("stage",))
        with cache.record():
            cache.retain("effect", ("stage",), image)
        cache.drain()
        assert cache.has("effect", ("stage",))
    finally:
        cache.close()


@pytest.mark.parametrize("old_renderer", ["native-artwork-3", "native-artwork-4",
    "native-artwork-20261007-source-context-2"])
def test_new_scene_renderer_rejects_old_exact_entries_without_rewriting_them(tmp_path, monkeypatch, old_renderer):
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    with monkeypatch.context() as patch:
        patch.setattr(storage, "RENDERER_VERSION", old_renderer)
        old = storage.PersistentRenderCache(tmp_path)
        try:
            with old.record():
                old.retain("effect", ("stage",), image)
            old.drain()
            assert old.has("effect", ("stage",))
        finally:
            old.close()
    original_index = (tmp_path / "index.json").read_bytes()
    current = storage.PersistentRenderCache(tmp_path)
    try:
        assert current.entries == {} and current.source_digests == {}
        assert current.lookup("effect", ("stage",)) is None
        assert not current.has("effect", ("stage",))
        assert current.saved == 0 and not current.reads
        assert (tmp_path / "index.json").read_bytes() == original_index
    finally:
        current.close()
