"""Empty committed disk indexes are misses before document dependency traversal.

Prospective additive lifecycle coverage. Tests use the production MainWindow,
private repository, detached recording service and original native output path.
No fabricated semantic keys or pixel results stand in for a committed cache.
"""
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
import copy
from threading import Event
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, Qt
from PySide6.QtGui import QColor, QImage
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication

from comic_editor.core.models import ImageObject
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.outputs import capture_document, render_snapshot
from comic_editor.render.pixels import FLOAT_PIXELS, LEGACY_PIXELS, premultiplied_pixels
from test_disk_render_cache import editor, finish
from test_disk_cache_live_status_policy import begin_contact, wait
from test_render_outputs import encoded


def forbid_status_traversal(cache, patch):
    def forbidden(*args, **kwargs):
        raise AssertionError("A definite empty-index miss traversed document dependencies")
    for owner, name in ((cache, "capture"), (cache, "requests"), (cache, "tile_key"),
                        (cache.canvas, "_projection_configuration"),
                        (cache.dependencies, "projection_key"), (cache.backing, "has")):
        patch.setattr(owner, name, forbidden)


def record_rows(window, qapp, count=1):
    cache = window.disk_cache
    cache.set_range(0, count * cache.canvas._document_projection.tile_size)
    assert cache.start()
    finish(window, qapp)
    assert not cache.canvas.document_read_only
    assert all(cache.row_ready(row) for row in range(count))
    return cache


def wait_until(qapp, predicate, *, advance=None):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        if advance is not None:
            advance()
        qapp.processEvents()
        if predicate():
            return
        time.sleep(.001)
    assert predicate(), "Committed cache lifecycle did not finish"


def test_empty_committed_index_skips_capture_and_semantic_dependency_work(editor, monkeypatch):
    window, _, _ = editor
    cache = window.disk_cache
    assert not cache.backing.entries
    before = copy.deepcopy(window.chapter.to_dict())
    with monkeypatch.context() as patch:
        forbid_status_traversal(cache, patch)
        for _ in range(3):
            assert not any(cache.row_ready(row) for row in (0, 1, 2))
    assert window.chapter.to_dict() == before
    assert not cache._keys and not cache.backing.entries
    assert not cache.backing.recording and not cache.backing.pending


def test_empty_status_tick_keeps_storage_polling_before_misses(editor, monkeypatch):
    window, _, _ = editor
    cache = window.disk_cache
    calls = []
    original_poll, original_ready = cache.backing.poll, cache.row_ready
    def poll():
        calls.append("poll")
        return original_poll()
    def row_ready(row):
        calls.append(("row", row))
        return original_ready(row)
    monkeypatch.setattr(cache.backing, "poll", poll)
    monkeypatch.setattr(cache, "row_ready", row_ready)
    cache._revision = cache.canvas._document_projection.revision
    cache.status.clear()
    cache._status_queue = deque((0, 1, 2))
    with monkeypatch.context() as patch:
        forbid_status_traversal(cache, patch)
        for _ in range(3):
            cache.tick()
            if not cache._status_queue:
                break
    assert calls[0] == "poll"
    assert [call for call in calls if call != "poll"] == [("row", 0), ("row", 1), ("row", 2)]
    assert cache.status == {0: False, 1: False, 2: False}
    assert not cache._status_queue and not cache.backing.entries


def test_sibling_full_clear_drops_stale_index_before_any_status_traversal(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = record_rows(window, qapp)
    sibling = PersistentRenderCache(cache.backing.root,
        contract=cache.backing.contract, environment=cache.backing.environment)
    try:
        sibling.clear()
        assert cache.backing.entries  # GUI reader has not yet observed the epoch.
        assert cache.backing._root_epoch != cache.backing._root_state.epoch
        with monkeypatch.context() as patch:
            forbid_status_traversal(cache, patch)
            assert not cache.row_ready(0)
        assert not cache.backing.entries and not cache.backing.verified
        assert cache.backing._root_epoch == cache.backing._root_state.epoch
        assert cache.backing._reload is not None
    finally:
        sibling.close()


def test_epoch_reload_from_empty_restores_ordinary_keys_and_blob_validation(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = record_rows(window, qapp, count=2)
    sibling = PersistentRenderCache(cache.backing.root,
        contract=cache.backing.contract, environment=cache.backing.environment)
    try:
        # Match the ordinary selected-row clear: remove shared intermediates,
        # keep the other row's exact projection tiles in the committed index.
        sibling.clear(lambda entry: entry["kind"] != "projection" or entry["key"][3] == 0)
        assert sibling.entries
        with monkeypatch.context() as patch:
            forbid_status_traversal(cache, patch)
            assert not cache.row_ready(1)
        assert not cache.backing.entries and not cache.backing.verified
        cache.backing._reload.result(timeout=5)
        keys, reads = [], []
        original_key, original_read = cache.tile_key, cache.backing._read
        def tile_key(request, configuration):
            key = original_key(request, configuration)
            keys.append(key)
            return key
        def read(entry, **kwargs):
            reads.append((entry["kind"], kwargs.get("verify")))
            return original_read(entry, **kwargs)
        monkeypatch.setattr(cache, "tile_key", tile_key)
        monkeypatch.setattr(cache.backing, "_read", read)
        wait_until(qapp, lambda: cache.row_ready(1), advance=cache.tick)
        assert keys and reads and any(kind == "projection" and verify for kind, verify in reads)
        assert cache.backing.entries and cache.backing.verified
        assert not cache.row_ready(0)
    finally:
        sibling.close()


@pytest.mark.parametrize("damage", ["source", "seal", "missing_blob", "corrupt_blob"])
def test_nonempty_index_retains_dependency_seal_and_blob_validation(editor, qapp, monkeypatch, damage):
    window, _, identifier = editor
    cache = record_rows(window, qapp)
    with cache.capture():
        configuration = (*cache.canvas._projection_configuration(), None)
        request = next(iter(cache.requests(0)))
        key = cache.tile_key(request, configuration)
    identity = cache.backing.descriptor("projection", key).identity
    entry = cache.backing.entries[identity]
    path = cache.backing._path(entry)
    if damage == "source":
        cache.canvas.tiles.paint_dab(identifier, QPointF(90, 80), 16, QColor("red"))
        cache.canvas._invalidate_scene_cache()
    elif damage == "seal":
        entry["seal"] = "invalid"
    elif damage == "missing_blob":
        path.unlink()
    else:
        payload = bytearray(path.read_bytes())
        assert len(payload) == entry["size"] > 0
        payload[len(payload) // 2] ^= 1
        path.write_bytes(payload)
        assert path.stat().st_size == entry["size"]
        cache.backing.verified.discard(identity)
    calls = []
    original_has = cache.backing.has
    def has(kind, semantic_key, **kwargs):
        calls.append((kind, semantic_key))
        return original_has(kind, semantic_key, **kwargs)
    monkeypatch.setattr(cache.backing, "has", has)
    assert not cache.row_ready(0)
    assert calls and calls[0][0] == "projection"
    if damage == "source":
        assert calls[0][1] != key and identity in cache.backing.entries
    elif damage == "corrupt_blob":
        with pytest.raises(ValueError, match="checksum"):
            cache.backing.verifications[identity].result(timeout=5)
        cache.backing.poll()
        assert identity not in cache.backing.entries
        assert not cache.row_ready(0)
    else:
        assert identity not in cache.backing.entries


@pytest.mark.parametrize("populated", [False, True])
def test_pending_color_remains_a_miss_before_epoch_or_dependency_work(editor, qapp, monkeypatch, populated):
    window, _, _ = editor
    cache = record_rows(window, qapp) if populated else window.disk_cache
    before = copy.deepcopy(cache.backing.entries)
    def forbidden():
        raise AssertionError("Pending color bypassed its existing readiness guard")
    with monkeypatch.context() as patch:
        patch.setattr("comic_editor.ui.disk_cache.semantic_color_identity",
            lambda *args: ("color-resources-pending",))
        patch.setattr(cache.backing, "_sync_epoch", forbidden)
        forbid_status_traversal(cache, patch)
        assert not cache.row_ready(0)
    assert cache.backing.entries == before


def test_clear_maintenance_remains_a_miss_with_a_populated_reader(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = record_rows(window, qapp)
    entries = copy.deepcopy(cache.backing.entries)
    with monkeypatch.context() as patch:
        patch.setattr(cache, "_maintenance", (Future(), cache._serial, "clear"))
        forbid_status_traversal(cache, patch)
        def forbidden():
            raise AssertionError("Clear maintenance bypassed its existing readiness guard")
        patch.setattr(cache.backing, "_sync_epoch", forbidden)
        assert not cache.row_ready(0)
    assert cache.backing.entries == entries


def test_unpublished_recording_stays_false_then_manifest_requeues_and_unlocks(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = window.disk_cache
    entered, release = Event(), Event()
    original_publish = PersistentRenderCache._publish_index
    original_adopt = cache._adopt_manifest
    manifests = []
    def publish(owner, *args):
        entered.set()
        assert release.wait(15), "Test did not release the detached publication"
        return original_publish(owner, *args)
    def adopt(manifest):
        original_adopt(manifest)
        if manifest is not None:
            manifests.append((bool(cache.backing.entries), tuple(cache._status_queue)))
    monkeypatch.setattr(PersistentRenderCache, "_publish_index", publish)
    monkeypatch.setattr(cache, "_adopt_manifest", adopt)
    cache.set_range(0, 256)
    cache.status[0] = False
    cache._status_queue.clear()
    try:
        assert cache.start() and cache.canvas.document_read_only
        wait_until(qapp, entered.is_set, advance=cache.tick)
        assert cache.building and not cache.backing.entries
        assert not manifests
        with monkeypatch.context() as patch:
            forbid_status_traversal(cache, patch)
            assert not cache.row_ready(0)
        release.set()
        finish(window, qapp)
        assert manifests and any(populated and 0 in queued for populated, queued in manifests)
        assert cache.row_ready(0) and cache.status[0]
        assert not cache.building and not cache._work and not cache._pending_rows
        assert not cache.canvas.document_read_only and not cache.canvas.command_stack.read_only
        assert cache.message == "Cached to disk"
    finally:
        release.set()


@pytest.mark.parametrize("kind", ["mask", "transform"])
@pytest.mark.parametrize("outcome", ["commit", "cancel"])
def test_empty_index_live_contact_still_pauses_status_until_commit_or_cancel(editor, qapp, monkeypatch, kind, outcome):
    window, _, _ = editor
    cache = window.disk_cache
    last, before, history = begin_contact(window, qapp, kind)
    assert not cache.backing.entries
    cache._revision = cache.canvas._document_projection.revision
    cache._status_queue = deque((0, 1, 2))
    rows, polls = [], []
    original_ready, original_poll = cache.row_ready, cache.backing.poll
    def row_ready(row):
        assert not cache.canvas._drawing and not cache.canvas._projection_has_live_preview()
        rows.append(row)
        return original_ready(row)
    def poll():
        polls.append(True)
        return original_poll()
    monkeypatch.setattr(cache, "row_ready", row_ready)
    monkeypatch.setattr(cache.backing, "poll", poll)
    for _ in range(3):
        cache.tick()
    assert not rows and len(polls) == 3 and tuple(cache._status_queue) == (0, 1, 2)
    if outcome == "commit":
        QTest.mouseRelease(cache.canvas, Qt.LeftButton, pos=last)
    else:
        QTest.keyClick(cache.canvas, Qt.Key_Escape)
    wait(qapp, lambda: not cache.canvas._drawing and not cache.canvas._projection_has_live_preview())
    if outcome == "cancel":
        QTest.mouseRelease(cache.canvas, Qt.LeftButton, pos=last)
    assert QApplication.mouseButtons() == Qt.NoButton
    if outcome == "cancel":
        assert cache.canvas.chapter.to_dict() == before
        assert tuple(cache.canvas.command_stack._undo) == history
    else:
        assert len(cache.canvas.command_stack._undo) == len(history) + 1
    with monkeypatch.context() as patch:
        forbid_status_traversal(cache, patch)
        cache.tick()
    assert rows and not cache.backing.entries and not any(cache.status.values())


@pytest.mark.parametrize("contract", [LEGACY_PIXELS, FLOAT_PIXELS])
def test_repeated_empty_status_and_camera_changes_preserve_full_native_output(editor, monkeypatch, contract):
    window, _, identifier = editor
    cache, canvas = window.disk_cache, window.canvas
    canvas.chapter.pixel_contract = contract
    raster = canvas.chapter.objects[identifier]
    image = canvas.chapter.add_object(raster.parent_layer_id, ImageObject(
        pixel_width=7, pixel_height=5,
        transform_quad=[(220, 60), (227, 60), (227, 65), (220, 65)]))
    original = QImage(7, 5, QImage.Format_RGBA64)
    pixels = np.frombuffer(original.bits(), np.uint16).reshape(5, 7, 4)
    pixels[:] = [32769, 16385, 8193, 10001]
    pixels[0, 0] = [65535, 12345, 23456, 1]
    source = encoded(original)
    canvas.images.put(image.object_id, "native.png", source)
    native_tiles = {address: (tile.format(), bytes(tile.constBits()))
                    for address, tile in canvas.tiles.iter_tiles(identifier)}
    before_document = copy.deepcopy(canvas.chapter.to_dict())
    before = capture_document(canvas.chapter, canvas.tiles, canvas.images)
    with ThreadPoolExecutor(max_workers=1) as worker:
        expected = worker.submit(render_snapshot, before).result(timeout=15)
        with monkeypatch.context() as patch:
            forbid_status_traversal(cache, patch)
            for scale in (.25, 1., 4.):
                canvas.scale = scale
                assert not any(cache.row_ready(row) for row in (0, 1, 2))
        after = capture_document(canvas.chapter, canvas.tiles, canvas.images)
        actual = worker.submit(render_snapshot, after).result(timeout=15)
    assert actual.size() == expected.size()
    assert (actual.width(), actual.height()) == (canvas.chapter.width, canvas.chapter.height)
    assert actual.format() == expected.format() == contract.image_format
    assert actual.colorSpace() == expected.colorSpace()
    np.testing.assert_array_equal(premultiplied_pixels(actual), premultiplied_pixels(expected))
    assert bytes(actual.constBits()) == bytes(expected.constBits())
    assert canvas.images.source(image.object_id).data == source
    assert {address: (tile.format(), bytes(tile.constBits()))
            for address, tile in canvas.tiles.iter_tiles(identifier)} == native_tiles
    assert canvas.chapter.to_dict() == before_document
    assert not cache.backing.entries and not cache.backing.pending and not cache._keys
