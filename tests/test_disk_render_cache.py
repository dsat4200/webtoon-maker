"""Durable pixels, reopen reuse, cancellation, and cache mutation boundaries."""
import json
from concurrent.futures import Future
import shutil
import time

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QColorSpace
from PySide6.QtTest import QTest

from comic_editor.core.models import RasterObject, CurvesModifier
from comic_editor.core.persistence import SeriesRepository
from comic_editor.render.cache import PersistentRenderCache, encode_value, decode_value
from comic_editor.render.service import RenderPending
from comic_editor.ui.main_window import MainWindow


@pytest.mark.parametrize("format", [QImage.Format_ARGB32_Premultiplied,
    QImage.Format_RGBA16FPx4_Premultiplied, QImage.Format_RGBA32FPx4_Premultiplied])
def test_codec_preserves_exact_working_pixels(format):
    image = QImage(17, 11, format)
    image.fill(QColor(80, 130, 220, 110))
    image.setColorSpace(QColorSpace(QColorSpace.SRgb))
    raw = image.bits()
    # Include bits which would be lost by PNG or a display-space conversion.
    raw[4:12] = b"\x01\x02\x03\x04\x05\x06\x07\x08"
    restored = decode_value(encode_value(image))
    assert restored.format() == image.format()
    assert restored.size() == image.size()
    assert restored.colorSpace() == image.colorSpace()
    assert bytes(restored.constBits()) == bytes(image.constBits())


def test_float_arrays_and_checkpoint_state_reopen(tmp_path):
    pixels = np.array([-.01, 1.7, np.inf, np.nan], np.float32).reshape(1, 1, 4)
    assert decode_value(encode_value(pixels)).tobytes() == pixels.tobytes()
    cache = PersistentRenderCache(tmp_path)
    with cache.record():
        cache.retain("effect", ("stage", "source"), pixels, state=(2, QRectF(1, 2, 3, 4)))
    assert not cache.entries
    cache.close()
    reopened = PersistentRenderCache(tmp_path)
    assert reopened.lookup("effect", ("stage", "source"), wait=True).tobytes() == pixels.tobytes()
    reopened.close()


def test_corrupt_payload_is_a_miss_and_can_be_rebuilt(tmp_path):
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    cache = PersistentRenderCache(tmp_path)
    with cache.record():
        cache.retain("projection", ("tile", 0), image)
    cache.drain()
    path = cache._path(next(iter(cache.entries.values())))
    path.write_bytes(b"broken")
    assert cache.lookup("projection", ("tile", 0), wait=True) is None
    with cache.record():
        cache.retain("projection", ("tile", 0), image)
    cache.drain()
    # A bad existing content-addressed file must also be replaced.
    assert cache.lookup("projection", ("tile", 0), wait=True) is not None
    cache.close()


def test_malformed_manifest_and_changed_environment_are_cache_misses(tmp_path):
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    cache = PersistentRenderCache(tmp_path, environment=("old",))
    with cache.record():
        cache.retain("projection", ("tile", 0), image)
    cache.close()
    reopened = PersistentRenderCache(tmp_path, environment=("new",))
    assert reopened.lookup("projection", ("tile", 0), wait=True) is None
    reopened.close()
    index = json.loads((tmp_path / "index.json").read_text())
    index["entries"]["bad"] = ["invalid"]
    entry = next(value for value in index["entries"].values() if isinstance(value, dict))
    entry["raw_size"] = -1
    (tmp_path / "index.json").write_text(json.dumps(index))
    reopened = PersistentRenderCache(tmp_path, environment=("old",))
    assert reopened.disk_bytes == 0
    assert not reopened.entries
    reopened.close()


@pytest.mark.parametrize("wait", [False, True])
def test_completed_effect_read_survives_source_read_pressure(tmp_path, monkeypatch, wait):
    import comic_editor.render.cache as storage
    effect = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    effect.fill(QColor("red"))
    source = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    source.fill(QColor("blue"))
    cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            cache.retain("effect", ("finished-stage",), effect, state=(1, QRectF(0, 0, 8, 8)))
            cache.retain("source", ("large-source",), source)
        cache.drain()
        monkeypatch.setattr(storage, "READ_BUDGET", effect.sizeInBytes())
        calls = []
        read = cache._read
        def counted(entry, **kwargs):
            calls.append(entry["kind"])
            return read(entry, **kwargs)
        monkeypatch.setattr(cache, "_read", counted)
        # A previous capture yielded after requesting its final effect. The
        # restarted capture must first reload a source evicted by another target.
        identity = cache.descriptor("effect", ("finished-stage",)).identity
        future = cache.executor.submit(cache._read, dict(cache.entries[identity]))
        cache.reads[identity] = future
        future.result()
        try:
            cache.lookup("source", ("large-source",), wait=wait)
        except RenderPending:
            pass
        restored = cache.lookup("effect", ("finished-stage",))
        assert bytes(restored.constBits()) == bytes(effect.constBits())
        assert calls.count("effect") == 1
        assert cache.entries[identity]["state"] == [1, {"rect": [0., 0., 8., 8.]}]
        assert cache.ready_bytes == 0
    finally:
        cache.close()


@pytest.mark.parametrize("budget", [128, 512])
def test_completed_read_handoff_is_bounded_and_clearable(tmp_path, monkeypatch, budget):
    import comic_editor.render.cache as storage
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            for number in range(5):
                cache.retain("effect", ("stage", number), image)
        cache.drain()
        monkeypatch.setattr(storage, "READ_BUDGET", image.sizeInBytes())
        monkeypatch.setattr(storage, "READY_BUDGET", budget)
        for number in range(4):
            identity = cache.descriptor("effect", ("stage", number)).identity
            future = cache.executor.submit(cache._read, dict(cache.entries[identity]))
            cache.reads[identity] = future
            future.result()
            cache.lookup("effect", ("stage", number + 1), wait=True)
            assert cache.ready_bytes <= max(budget, image.sizeInBytes())
        assert cache.ready
        cache.clear()
        assert not cache.ready and cache.ready_bytes == 0
    finally:
        cache.close()
    assert not cache.ready and cache.ready_bytes == 0


def test_failed_retired_read_is_invalidated_and_not_handed_off(tmp_path, monkeypatch):
    import comic_editor.render.cache as storage
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            for number in range(2):
                cache.retain("effect", ("stage", number), image)
        cache.drain()
        identity = cache.descriptor("effect", ("stage", 0)).identity
        failed = Future()
        failed.set_exception(ValueError("Corrupt saved pixels"))
        cache.reads[identity] = failed
        monkeypatch.setattr(storage, "READ_BUDGET", image.sizeInBytes())
        cache.lookup("effect", ("stage", 1), wait=True)
        assert identity not in cache.entries and identity not in cache.verified
        assert cache.lookup("effect", ("stage", 0)) is None
        assert not cache.ready and cache.invalidated
    finally:
        cache.close()


def test_pending_read_pressure_yields_without_waiting(tmp_path, monkeypatch):
    import comic_editor.render.cache as storage
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            for number in range(2):
                cache.retain("effect", ("stage", number), image)
        cache.drain()
        identity = cache.descriptor("effect", ("stage", 0)).identity
        pending = Future()
        cache.reads[identity] = pending
        monkeypatch.setattr(storage, "READ_BUDGET", image.sizeInBytes())
        with pytest.raises(RenderPending):
            cache.lookup("effect", ("stage", 1))
        assert cache.reads == {identity: pending}
        assert not cache.ready
        pending.set_result(image)
        cache.lookup("effect", ("stage", 1), wait=True)
        assert cache.lookup("effect", ("stage", 0)) is not None
    finally:
        cache.close()


def test_handed_off_value_still_validates_its_entry(tmp_path, monkeypatch):
    import comic_editor.render.cache as storage
    pixels = np.array([-.01, 1.7, np.inf, np.nan], np.float32).reshape(1, 1, 4)
    cache = PersistentRenderCache(tmp_path)
    try:
        with cache.record():
            for number in range(3):
                cache.retain("effect", ("stage", number), pixels)
        cache.drain()
        monkeypatch.setattr(storage, "READ_BUDGET", pixels.nbytes)
        for number in (0, 1):
            identity = cache.descriptor("effect", ("stage", number)).identity
            future = cache.executor.submit(cache._read, dict(cache.entries[identity]))
            cache.reads[identity] = future
            future.result()
            cache.lookup("effect", ("stage", 2), wait=True)
        assert cache.lookup("effect", ("stage", 0)).tobytes() == pixels.tobytes()
        cache.entries[identity]["seal"] = "invalid"
        assert cache.lookup("effect", ("stage", 1)) is None
        assert not cache.ready and cache.ready_bytes == 0
    finally:
        cache.close()


@pytest.fixture
def editor(qapp, tmp_path):
    repository = SeriesRepository(tmp_path / "project")
    series = repository.create("Cache test")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    chapter.height = 768
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    for point in (QPointF(90, 80), QPointF(90, 350), QPointF(90, 650)):
        tiles.paint_dab(raster.object_id, point, 32, QColor("#aa436ddd"))
    repository.save_chapter(chapter, tiles)
    windows = []
    def open_window(path=None):
        window = MainWindow()
        windows.append(window)
        window.settings.grid_overlay_visible = False
        assert window.open_series(path or repository.root)
        window.disk_cache.timer.stop()
        return window
    yield open_window(), open_window, raster.object_id
    for window in windows:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()
        window.deleteLater()


def finish(window, qapp, seconds=15):
    end = time.monotonic() + seconds
    while window.disk_cache.building and time.monotonic() < end:
        window.disk_cache.tick()
        window.canvas._effect_jobs.poll()
        qapp.processEvents()
        time.sleep(.001)
    assert not window.disk_cache.building, window.disk_cache.message
    assert not window.disk_cache.backing.error


def test_manual_range_reopens_without_scene_rendering(editor, qapp, monkeypatch):
    window, reopen, identifier = editor
    cache = window.disk_cache
    cache.set_range(12, 280)
    assert list(cache.selected_rows) == [0, 1]
    assert cache.start()
    finish(window, qapp)
    assert cache.row_ready(0) and cache.row_ready(1)
    assert not cache.row_ready(2)
    first = dict(cache.backing.entries)
    window.disk_cache.detach()
    second = reopen()
    second.disk_cache.timer.stop()
    second_cache = second.disk_cache
    def forbidden(*args, **kwargs):
        raise AssertionError("Green rows must load without scene traversal")
    monkeypatch.setattr(second.canvas._render_service, "render_region", forbidden)
    with second_cache.capture():
        configuration = second.canvas._projection_configuration()
        second.canvas._render_service.configure((*configuration, None), document=configuration[:3])
        document = second.canvas._render_document_state()
        for row in (0, 1):
            requests = second_cache.requests(row)
            for request in requests:
                key = second_cache.tile_key(request, (*document.configuration, None))
                image = second_cache.backing.lookup("projection", key, wait=True)
                assert image is not None
            from comic_editor.render.service import TileBatchPolicy
            # Async read-through publishes into the normal memory LRU.
            for attempt in range(500):
                tiles = second.canvas._render_service.collect_tiles(document, requests, TileBatchPolicy((0, 0)))
                if len(tiles) == len(requests):
                    break
                time.sleep(.001)
            assert len(tiles) == len(requests)
    assert second_cache.backing.entries == first


def test_editing_lock_cancel_and_resume(editor, qapp):
    window, _, identifier = editor
    cache, canvas = window.disk_cache, window.canvas
    cache.set_range(0, 512)
    canvas.set_selection("object", identifier)
    assert cache.start()
    before = window.chapter.to_dict()
    revision = canvas.command_stack.revision
    canvas._tool_press(QPointF(50, 50), 1.)
    canvas.command_stack.undo()
    QTest.keyClick(canvas, Qt.Key_Delete)
    assert window.chapter.to_dict() == before
    assert canvas.command_stack.revision == revision
    assert not window.hierarchy_model.setData(window.hierarchy_model.index_for_entity("object", identifier), "Changed")
    for _ in range(500):
        cache.tick()
        qapp.processEvents()
        if cache.status.get(0):
            break
        time.sleep(.001)
    cache.cancel()
    cache.backing.drain()
    assert not canvas.document_read_only
    assert cache.row_ready(0)
    assert cache.start()
    finish(window, qapp)
    assert cache.row_ready(1)


def test_effect_intermediates_are_saved_and_invalidated(editor, qapp):
    window, _, identifier = editor
    modifier = CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .8), (1, 1)]})
    window.chapter.add_modifier(modifier, [("object", identifier)])
    window.canvas._invalidate_scene_cache()
    cache = window.disk_cache
    cache.set_range(0, 512)
    assert cache.start()
    finish(window, qapp)
    assert {entry["kind"] for entry in cache.backing.entries.values()} >= {"projection", "source", "effect"}
    assert cache.row_ready(0)
    modifier.curves = {"rgb:master": [(0, 0), (.5, .2), (1, 1)]}
    window.canvas._invalidate_scene_cache()
    assert not cache.row_ready(0)


def test_reopen_reuses_effect_sources_after_final_cache_is_cleared(editor, qapp, monkeypatch):
    window, reopen, identifier = editor
    modifier = CurvesModifier(curves={"rgb:master": [(0, 0), (.5, .8), (1, 1)]})
    window.chapter.add_modifier(modifier, [("object", identifier)])
    window.canvas.tiles.paint_dab(identifier, QPointF(90, 80), 12, QColor("red"))
    window.canvas._invalidate_scene_cache()
    # Warm memory from dirty pixels before Cache to disk saves the chapter.
    window.canvas._ensure_scene_cache()
    window.disk_cache.set_range(0, 512)
    assert window.disk_cache.start()
    finish(window, qapp)
    window.disk_cache.backing.clear(lambda entry: entry["kind"] == "projection")
    window.disk_cache.detach()
    copied = window.repository.root.parent / "copied project"
    shutil.copytree(window.repository.root, copied)
    second = reopen(copied)
    original = second.canvas.tiles.iter_tiles
    def no_source_capture(object_id, *args, **kwargs):
        assert object_id != identifier, "An unchanged cached effect source was recaptured"
        return original(object_id, *args, **kwargs)
    monkeypatch.setattr(second.canvas.tiles, "iter_tiles", no_source_capture)
    second.disk_cache.set_range(0, 512)
    assert second.disk_cache.start()
    finish(second, qapp)
    assert second.disk_cache.row_ready(0)


def test_small_raster_edit_preserves_remote_green_rows(editor, qapp):
    window, _, identifier = editor
    cache = window.disk_cache
    cache.set_range(0, 768)
    assert cache.start()
    finish(window, qapp)
    window.canvas.tiles.paint_dab(identifier, QPointF(100, 70), 16, QColor("red"))
    window.canvas._document_visual_changed(QRectF(85, 55, 30, 30))
    assert not cache.row_ready(0)
    assert cache.row_ready(2)


def test_failed_write_unlocks_and_preserves_committed_rows(editor, qapp, monkeypatch):
    window, _, _ = editor
    cache = window.disk_cache
    cache.set_range(0, 256)
    assert cache.start()
    finish(window, qapp)
    import comic_editor.render.cache as storage
    original = storage._atomic_bytes
    def fail(path, data):
        if path.suffix == ".cache":
            raise OSError("Disk full")
        return original(path, data)
    monkeypatch.setattr(storage, "_atomic_bytes", fail)
    cache.set_range(256, 512)
    assert cache.start()
    end = time.monotonic() + 10
    while cache.building and time.monotonic() < end:
        cache.tick()
        time.sleep(.001)
    assert not cache.building
    assert not window.canvas.document_read_only
    assert "Disk full" in cache.message
    assert cache.row_ready(0)
    assert not cache.row_ready(1)


def test_clear_selected_keeps_other_final_rows(editor, qapp):
    window, _, _ = editor
    cache = window.disk_cache
    cache.set_range(0, 512)
    assert cache.start()
    finish(window, qapp)
    cache.set_range(0, 256)
    cache.clear(selected=True)
    assert not cache.row_ready(0)
    assert cache.row_ready(1)
    cache.clear(selected=False)
    assert not cache.backing.entries
    assert not list((cache.backing.root / "values").glob("*.cache"))


def test_cache_handles_choose_range_and_camera_stays_available(editor, qapp):
    window, _, _ = editor
    cache, preview, canvas = window.disk_cache, window.preview, window.canvas
    preview.resize(92, 500)
    cache.set_range(0, 768)
    rect = preview.content_rect()
    def point(y):
        return QPointF(preview.width() - 8, rect.top() + y / canvas.chapter.height * rect.height()).toPoint()
    QTest.mousePress(preview, Qt.LeftButton, pos=point(0))
    QTest.mouseMove(preview, point(70))
    QTest.mouseRelease(preview, Qt.LeftButton, pos=point(70))
    QTest.mousePress(preview, Qt.LeftButton, pos=point(768))
    QTest.mouseRelease(preview, Qt.LeftButton, pos=point(410))
    assert cache.start_y == pytest.approx(70, abs=3)
    assert cache.end_y == pytest.approx(410, abs=3)
    assert list(cache.selected_rows) == [0, 1]
    assert cache.start()
    canvas.scale = 2.
    old = canvas.center_y
    QTest.mouseClick(preview, Qt.LeftButton, pos=QPointF(20, rect.top() + rect.height() * .8).toPoint())
    assert canvas.center_y != old
    assert window.chapter_combo.isEnabled()
    assert window.project_tabs.isEnabled()
    assert canvas.document_read_only
    cache.cancel()


def test_pending_image_drop_waits_for_cache_unlock(editor, monkeypatch):
    window, _, _ = editor
    canvas, cache = window.canvas, window.disk_cache
    assert cache.start()
    canvas._pending_external_drop = dict(entries=[dict(pending=False, failed=False, filename="image.png",
        mime_type="image/png", data=b"source")], parent_id=canvas.active_layer_id,
        world=QPointF(100, 100), insertion_index=0, fit_parent=False)
    applied = []
    monkeypatch.setattr(canvas, "place_image_sources", lambda *args, **kwargs: applied.append(args) or [])
    canvas._finish_pending_external_drop_if_ready()
    assert not applied
    assert canvas._pending_external_drop is not None
    cache.cancel()
    assert len(applied) == 1
    assert canvas._pending_external_drop is None
