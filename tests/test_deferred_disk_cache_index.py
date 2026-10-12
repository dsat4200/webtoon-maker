"""Saved exact manifests cannot make a tab binding wait for index IO."""
from threading import Event, get_ident
import time

import pytest
from PySide6.QtCore import QTimer
from PySide6.QtGui import QColor, QImage

from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.resident_cache import ResidentRenderCache
from comic_editor.render.service import RenderPending
from test_disk_render_cache import editor, finish


def image():
    result = QImage(7, 9, QImage.Format_RGBA8888_Premultiplied)
    result.fill(QColor("#6e416790"))
    return result


def saved_cache(root):
    result = PersistentRenderCache(root)
    with result.record():
        result.retain("projection", ("native",), image())
    result.drain()
    return result


def wait_until(qapp, predicate, advance=lambda: None):
    deadline = time.monotonic() + 5
    while not predicate() and time.monotonic() < deadline:
        advance()
        qapp.processEvents()
        time.sleep(.001)
    assert predicate()


def test_initial_index_read_and_validation_do_not_hold_the_interface(qapp, tmp_path, monkeypatch):
    writer = saved_cache(tmp_path)
    writer.close()
    gui, entered, release = get_ident(), Event(), Event()
    original = PersistentRenderCache._committed_index

    def blocked(cache):
        assert get_ident() != gui
        entered.set()
        assert release.wait(5)
        return original(cache)

    monkeypatch.setattr(PersistentRenderCache, "_committed_index", blocked)
    reader = PersistentRenderCache(tmp_path, defer_index=True)
    try:
        assert entered.wait(2)
        assert reader.index_loading and not reader.entries
        with pytest.raises(RenderPending):
            reader.lookup("projection", ("native",))
        callbacks = []
        QTimer.singleShot(0, lambda: callbacks.append(True))
        qapp.processEvents()
        assert callbacks == [True]
        assert reader.index_loading and not reader.entries
        release.set()
        wait_until(qapp, lambda: not reader.index_loading, reader.poll)
        restored = reader.lookup("projection", ("native",), wait=True)
        assert restored.format() == image().format()
        assert bytes(restored.constBits()) == bytes(image().constBits())
    finally:
        release.set()
        reader.close()


@pytest.mark.parametrize("change", ["clear", "publication"])
def test_late_initial_index_obeys_existing_root_epoch_and_generation(qapp, tmp_path, monkeypatch, change):
    writer = saved_cache(tmp_path)
    entered, release = Event(), Event()
    original = PersistentRenderCache._reload_index
    first = [True]

    def held(cache):
        result = original(cache)
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(5)
        return result

    monkeypatch.setattr(PersistentRenderCache, "_reload_index", held)
    reader = PersistentRenderCache(tmp_path, defer_index=True)
    try:
        assert entered.wait(2)
        if change == "clear":
            writer.clear()
        else:
            with writer.record():
                writer.retain("projection", ("second",), image())
            writer.drain()
        release.set()
        wait_until(qapp, lambda: not reader.index_loading, reader.poll)
        assert reader.entries == writer.entries
        assert len(reader.entries) == (0 if change == "clear" else 2)
    finally:
        release.set()
        reader.close()
        writer.close()


def test_late_index_keeps_source_aliases_remembered_by_current_resident_owner(qapp, tmp_path, monkeypatch):
    writer = saved_cache(tmp_path)
    writer.close()
    entered, release = Event(), Event()
    original = PersistentRenderCache._reload_index

    def held(cache):
        result = original(cache)
        entered.set()
        assert release.wait(5)
        return result

    monkeypatch.setattr(PersistentRenderCache, "_reload_index", held)
    memory = ResidentRenderCache()
    view = memory.view(str(tmp_path))
    reader = PersistentRenderCache(tmp_path, resident=view, defer_index=True)
    try:
        assert entered.wait(2)
        from comic_editor.ui.cache_dependencies import RenderDependencies
        dependencies = RenderDependencies(None, reader)
        dependencies._remember_file("source-alias", [1, 2, 3, 4, 5], "a" * 64)
        current = dict(reader.source_digests["source-alias"])
        release.set()
        wait_until(qapp, lambda: not reader.index_loading, reader.poll)
        assert reader.source_digests["source-alias"] == current
        assert reader.source_digests == view.source_digests()
    finally:
        release.set()
        reader.close()


def test_rebinding_a_tab_retires_its_pending_index_and_uses_current_owner(editor, qapp, monkeypatch):
    window, _, _ = editor
    finish(window, qapp)
    writer = saved_cache(window.disk_cache.backing.root)
    writer.close()
    gui, entered, release = get_ident(), Event(), Event()
    original = PersistentRenderCache._committed_index
    first = [True]

    def blocked(cache):
        assert get_ident() != gui, "A tab binding parsed its saved index on the interface"
        if first[0]:
            first[0] = False
            entered.set()
            assert release.wait(5)
        return original(cache)

    monkeypatch.setattr(PersistentRenderCache, "_committed_index", blocked)
    controller = window.disk_cache
    controller.bind()
    old = controller.backing
    try:
        assert entered.wait(2)
        assert old.index_loading
        controller.bind()
        current = controller.backing
        assert current is not old
        callbacks = []
        QTimer.singleShot(0, lambda: callbacks.append(True))
        qapp.processEvents()
        assert callbacks == [True]
        assert current.index_loading
        release.set()
        wait_until(qapp, lambda: not current.index_loading, controller.tick)
        wait_until(qapp, lambda: old.closed)
        assert controller.backing is current
        assert window.canvas._persistent_render_cache is current
        assert controller.dependencies.backing is current
        assert not current.closed
    finally:
        release.set()
