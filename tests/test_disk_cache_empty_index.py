"""Definite disk misses avoid scene work without bypassing exact recording."""
from contextlib import nullcontext
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage

from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.service import RenderPending
from comic_editor.ui import cache_dependencies
from comic_editor.ui.disk_cache import DiskCacheController


@pytest.fixture
def cache(tmp_path):
    value = PersistentRenderCache(tmp_path)
    yield value
    value.close()


def image():
    result = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    result.fill(QColor("#478fd1"))
    return result


def canvas(backing):
    return SimpleNamespace(_persistent_render_cache=backing,
        _projection_exact=True, _projection_defer_effects=False,
        _effect_preview_channel="canvas", _projection_has_live_preview=lambda: False)


def controller(backing, calls):
    request = SimpleNamespace(address=(0, 0, 0))
    def tile_key(request, configuration):
        calls.append((request, configuration))
        return ("test-native-tile", request.address)
    return SimpleNamespace(backing=backing, tile_key=tile_key,
        reusable=lambda configuration: True, capture=nullcontext,
        requests=lambda row: [request],
        canvas=SimpleNamespace(_projection_configuration=lambda: ())), request


def lookup(controller, request):
    while True:
        try:
            return DiskCacheController.lookup_tile(controller, request, ())
        except RenderPending:
            # Await only this tiny test payload; production reads remain async.
            for future in tuple(controller.backing.reads.values()):
                future.result(timeout=2)


def test_readability_tracks_publication_clear_and_close(cache):
    assert not cache.can_lookup
    with cache.record():
        assert cache.retain("effect", ("stage", "native"), image())
    # A completed blob alone is not a published reusable value.
    for future, _, _ in cache.writes.values():
        future.result(timeout=2)
    assert not cache.can_lookup
    cache.poll()
    assert cache.staged and cache.publication is not None
    assert not cache.can_lookup
    cache.drain()
    assert cache.can_lookup
    cache.clear()
    assert not cache.can_lookup
    with cache.record():
        cache.retain("effect", ("stage", "native"), image())
    cache.drain()
    assert cache.can_lookup
    cache.close()
    assert not cache.can_lookup


def test_empty_controller_skips_lookup_status_and_nonrecording_keys(cache):
    calls = []
    owner, request = controller(cache, calls)
    assert DiskCacheController.lookup_tile(owner, request, ()) is None
    assert not DiskCacheController.row_ready(owner, 0)
    DiskCacheController.retain_tile(owner, request, (), image())
    assert not calls
    assert not cache.writes and not cache.entries


def test_recording_builds_keys_and_publication_enables_normal_validation(cache):
    calls = []
    owner, request = controller(cache, calls)
    expected = image()
    with cache.record():
        DiskCacheController.retain_tile(owner, request, (), expected)
    assert len(calls) == 1
    assert DiskCacheController.lookup_tile(owner, request, ()) is None
    assert not DiskCacheController.row_ready(owner, 0)
    assert len(calls) == 1  # Pending writes do not trigger scene key work.
    cache.drain()
    assert DiskCacheController.row_ready(owner, 0)
    restored = lookup(owner, request)
    assert bytes(restored.constBits()) == bytes(expected.constBits())
    assert len(calls) > 1
    identity = next(iter(cache.entries))
    cache.entries[identity]["seal"] = "corrupt"
    assert DiskCacheController.lookup_tile(owner, request, ()) is None
    assert not cache.entries
    previous = len(calls)
    assert DiskCacheController.lookup_tile(owner, request, ()) is None
    assert not DiskCacheController.row_ready(owner, 0)
    assert len(calls) == previous


def test_empty_intermediate_lookup_skips_deep_eligibility(cache, monkeypatch):
    def forbidden(*args):
        raise AssertionError("A definite empty read must not scan a semantic key")
    monkeypatch.setattr(cache_dependencies, "exact_cache_allowed", forbidden)
    assert cache_dependencies.cache_get(canvas(cache), "effect", ("stage", "native")) is None


def test_recording_yields_until_first_dependency_key_is_ready(cache):
    calls = []
    owner, request = controller(cache, calls)
    normal_key = owner.tile_key
    def pending_key(request, configuration):
        raise RenderPending("Checking saved sources")
    owner.tile_key = pending_key
    with cache.record():
        DiskCacheController.retain_tile(owner, request, (), image())
        assert not cache.writes and not cache.entries
        # A later collect offers the unchanged valid memory tile again.
        owner.tile_key = normal_key
        DiskCacheController.retain_tile(owner, request, (), image())
    cache.drain()
    assert len(calls) == 1 and cache.can_lookup
    assert lookup(owner, request) is not None


def test_ready_handoff_still_validates_and_cleans_a_missing_entry(cache, monkeypatch):
    key = ("stage", "native")
    cache._ready_put(cache.descriptor("effect", key).identity, image())
    assert cache.can_lookup
    calls = []
    original = cache_dependencies.exact_cache_allowed
    monkeypatch.setattr(cache_dependencies, "exact_cache_allowed",
        lambda owner, semantic: calls.append(semantic) or original(owner, semantic))
    assert cache_dependencies.cache_get(canvas(cache), "effect", key) is None
    assert calls == [key]
    assert not cache.ready and cache.ready_bytes == 0
    assert not cache.can_lookup


def test_empty_read_does_not_suppress_warm_metadata_or_draft_exclusion(cache):
    owner, observed = canvas(cache), []
    owner._disk_cache_controller = SimpleNamespace(
        observe=lambda kind, key, state: observed.append((kind, key, state)))
    key, state = ("stage", "native"), (1, QRectF(0, 0, 8, 8))
    cache_dependencies.cache_put(owner, "effect", key, image(), state=state)
    assert observed == [("effect", key, state)]
    assert not cache.entries and not cache.writes
    with cache.record():
        cache_dependencies.cache_put(owner, "effect", ("stage", ("preview-source",)), image())
        cache_dependencies.cache_put(owner, "effect", ("stage", ("draft-source",)), image())
    assert observed == [("effect", key, state)]
    assert not cache.writes
    with cache.record():
        cache_dependencies.cache_put(owner, "effect", key, image(), state=state)
    cache.drain()
    restored = cache_dependencies.cache_get(owner, "effect", key)
    assert restored is not None
    assert next(iter(cache.entries.values()))["state"] == [1, {"rect": [0., 0., 8., 8.]}]


def test_read_through_backing_without_optional_hint_remains_supported():
    calls = []
    backing = SimpleNamespace(lookup=lambda *args, **kwargs: calls.append((args, kwargs)))
    cache_dependencies.cache_get(canvas(backing), "effect", ("stage", "native"))
    assert calls == [(("effect", ("stage", "native")), {"wait": True})]


def test_empty_fast_miss_keeps_real_source_fingerprints(editor):
    window, _, identifier = editor
    cache, owner = window.disk_cache, window.canvas
    assert not cache.backing.can_lookup
    assert owner.tiles.render_fingerprint.__self__ is cache.dependencies
    assert owner.images.render_fingerprint.__self__ is cache.dependencies
    before = owner.tiles.object_signature(identifier)
    # An unsignaled resident borrower edit must still change the native key.
    tile = owner.tiles.tile(identifier, (0, 0))
    tile.setPixelColor(90, 80, QColor("red"))
    after = owner.tiles.object_signature(identifier)
    assert before != after
    assert not cache.backing.can_lookup


# Reuse the real temporary-project fixture for the source mutation proof.
from test_disk_render_cache import editor  # noqa: E402,F401
