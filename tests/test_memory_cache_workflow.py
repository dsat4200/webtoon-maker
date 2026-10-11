"""The real project close/export/reopen path retains native exact preview pixels."""
import hashlib
import time

from PySide6.QtWidgets import QMessageBox

from comic_editor.render.scene import DetachedSceneBackend
from test_disk_render_cache import editor, finish


def source_hashes(repository):
    return {str(path.relative_to(repository.root)): hashlib.sha256(path.read_bytes()).hexdigest()
            for path in repository.root.rglob('*')
            if path.is_file() and '.render-cache' not in path.parts and '.autosave' not in path.parts}


def test_real_tab_close_exports_then_reopen_restores_exact_memory_without_rendering(editor, qapp, monkeypatch):
    window, reopen, _ = editor
    session, cache = window.active_session, window.disk_cache
    assert window.save(wait=True)
    before = source_hashes(session.context.repository)
    cache.set_range(0, 512)
    assert cache.start()
    finish(window, qapp)
    view = cache.backing.resident
    expected = {entry.descriptor.identity: bytes(entry.value.constBits())
                for entry in view.snapshot() if entry.descriptor.kind == 'projection'}
    assert expected and cache.row_ready(0) and cache.row_ready(1)
    assert not cache.backing.entries and not list(cache.backing.root.glob('**/*'))
    root = cache.backing.root
    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.Yes)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert window._cache_close.busy and session.key in window.sessions
    deadline = time.monotonic() + 20
    while window._cache_close.busy and time.monotonic() < deadline:
        cache.tick()
        qapp.processEvents()
        time.sleep(.001)
    assert not window._cache_close.busy and session.key not in window.sessions
    assert (root / 'index.json').is_file()
    assert view.bytes == 0 and not cache.contexts and not cache.memory.eligible
    assert source_hashes(session.context.repository) == before

    monkeypatch.setattr(QMessageBox, 'question', lambda *args: QMessageBox.No)
    second = reopen()
    def forbidden(*args, **kwargs):
        raise AssertionError('Validated saved projection tiles must restore without rendering')
    monkeypatch.setattr(DetachedSceneBackend, 'capture', forbidden)
    finish(second, qapp)
    restored = {entry.descriptor.identity: bytes(entry.value.constBits())
                for entry in second.disk_cache.backing.resident.snapshot()
                if entry.descriptor.kind == 'projection'}
    assert restored == expected
    assert second.disk_cache.row_ready(0) and second.disk_cache.row_ready(1)
    assert source_hashes(session.context.repository) == before
