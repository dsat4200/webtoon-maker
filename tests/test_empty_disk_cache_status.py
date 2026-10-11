"""Unknown RAM keys are misses before document dependency traversal.

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


def publish(window, qapp):
    assert window.save(wait=True)
    result = []
    assert window.disk_cache.persist_for_close(window.active_session, lambda *value: result.append(value))
    wait_until(qapp, lambda: bool(result), advance=window.disk_cache.tick)
    assert result == [(True, "")]


def test_saved_disk_clear_keeps_independently_resident_exact_pixels(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = record_rows(window, qapp, count=2)
    publish(window, qapp)
    before = cache.backing.resident.bytes
    cache.clear_saved()
    wait_until(qapp, lambda: cache._maintenance is None, advance=cache.tick)
    assert cache.backing.resident.bytes == before
    with monkeypatch.context() as patch:
        patch.setattr(cache.dependencies, "projection_key", lambda *args: pytest.fail("RAM status rehashed dependencies"))
        patch.setattr(cache.backing, "has", lambda *args: pytest.fail("RAM status read disk"))
        assert cache.row_ready(0) and cache.row_ready(1)
    assert not PersistentRenderCache._valid_entry("invalid", {})


@pytest.mark.parametrize("damage", ["source", "seal", "missing_blob", "corrupt_blob"])
def test_saved_corruption_or_changed_source_never_becomes_resident_green(editor, qapp, damage):
    window, reopen, identifier = editor
    cache = record_rows(window, qapp)
    with cache.capture():
        configuration = (*cache.canvas._projection_configuration(), None)
        request = next(iter(cache.requests(0)))
        key = cache.tile_key(request, configuration)
    publish(window, qapp)
    saved = PersistentRenderCache(cache.backing.root, contract=cache.backing.contract, environment=cache.backing.environment)
    try:
        descriptor = saved.descriptor("projection", key)
        entry = saved.entries[descriptor.identity]
        path = saved._path(entry)
        if damage == "source":
            cache.canvas.tiles.paint_dab(identifier, QPointF(90, 80), 16, QColor("red"))
            cache.canvas._invalidate_scene_cache()
            assert window.save(wait=True)
        elif damage == "seal":
            entry["seal"] = "invalid"
            saved.flush()
        elif damage == "missing_blob":
            path.unlink()
        else:
            payload = bytearray(path.read_bytes())
            payload[len(payload) // 2] ^= 1
            path.write_bytes(payload)
    finally:
        saved.close()
    second = reopen()
    loading = second.disk_cache
    wait_until(qapp, lambda: loading._capture is None and loading._metadata is None, advance=loading.tick)
    assert not loading.row_ready(0)
    assert loading.lookup_memory_tile(next(iter(loading.requests(0))), (*second.canvas._projection_configuration(), None)) is None


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


def test_memory_warming_publishes_ready_rows_without_disk_writes(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = window.disk_cache
    def forbidden(*args):
        pytest.fail("Memory warming published to disk")
    monkeypatch.setattr(PersistentRenderCache, "_publish_index", forbidden)
    monkeypatch.setattr(window, "save", lambda **kw: pytest.fail("Memory warming saved the document"))
    cache.set_range(0, 256)
    assert cache.start() and cache.canvas.document_read_only
    finish(window, qapp)
    assert cache.row_ready(0) and cache.status[0]
    assert cache.backing.resident.bytes > 0 and not cache.backing.entries
    assert not cache.canvas.document_read_only and not cache.canvas.command_stack.read_only
    assert cache.message == "Cached to memory"


def test_completed_native_work_waits_for_remaining_readiness_rows(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = record_rows(window, qapp, count=3)
    cache.building = True
    cache._lock(True)
    cache._evaluation_done = True
    cache._work = deque((0, 1, 2))
    cache._status_queue = deque((0, 1, 2))
    ready = cache.row_ready
    def slow_status(row):
        time.sleep(.009)
        return ready(row)
    monkeypatch.setattr(cache, 'row_ready', slow_status)
    cache.tick()
    assert cache.building and cache._status_queue
    assert 'full' not in cache.message
    for _ in range(3):
        cache.tick()
    assert not cache.building and not cache._work
    assert cache.message == 'Cached to memory'


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
