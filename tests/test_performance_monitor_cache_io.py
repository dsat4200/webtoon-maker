"""Deferred GUI cache binding carries frozen ownership into its IO lane."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QEvent
from PySide6.QtWidgets import QApplication, QLineEdit

from comic_editor.render import cache as cache_module
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.ui.disk_cache import DiskCacheController
from test_performance_monitor_integration import editor, controller, _phases


def _index(root):
    root.mkdir()
    (root / 'index.json').write_text(json.dumps({
        'version': cache_module.CACHE_VERSION, 'renderer': cache_module.RENDERER_VERSION,
        'entries': {}, 'sources': {},
    }), encoding='utf-8')


def _binding(monkeypatch, root):
    backings = []
    def bind(_controller):
        backing = PersistentRenderCache(root, defer_index=True)
        backings.append(backing)
        return backing
    monkeypatch.setattr(DiskCacheController, 'bind', bind)
    return bind, backings


def test_real_deferred_index_is_registered_before_submit_and_foreign_owner_is_invisible(
        editor, controller, monkeypatch, tmp_path):
    root = tmp_path / 'private cache path must not enter diagnostics'
    _index(root)
    original_bind, backings = _binding(monkeypatch, root)
    original_index = PersistentRenderCache._committed_index
    entered, release = threading.Event(), threading.Event()
    gui = threading.get_ident()
    calls = []
    def read(backing):
        scope = controller._current_scope()
        calls.append((threading.get_ident(), scope.details() if scope else None))
        if scope is not None:
            assert threading.get_ident() != gui
            entered.set()
            assert release.wait(10)
        return original_index(backing)
    monkeypatch.setattr(PersistentRenderCache, '_committed_index', read)
    original_focus = QApplication.focusWidget
    def gui_focus_only():
        assert threading.get_ident() == gui, 'IO instrumentation read Qt focus'
        return original_focus()
    monkeypatch.setattr(QApplication, 'focusWidget', gui_focus_only)
    controller.start()
    reload_wrapper = PersistentRenderCache._reload_index
    try:
        backing = editor[0].disk_cache.bind()
        assert entered.wait(10), 'First submitted task lacked its ownership token'
        assert any(row['name'] == 'scene.disk_cache_reload_index'
            for row in controller.snapshot()['active_spans'])
        release.set()
        result = backing._reload.result(timeout=10)
        assert result[:2] == ({}, {})
        rows = [row for row in controller.snapshot()['timeline']
            if row['name'].startswith(('scene.disk_cache_reload_index', 'scene.disk_cache_committed_index'))]
        assert {row['name'] for row in rows} == {
            'scene.disk_cache_reload_index', 'scene.disk_cache_reload_index.thread_cpu',
            'scene.disk_cache_committed_index', 'scene.disk_cache_committed_index.thread_cpu'}
        assert all(row['details']['scope'] == 'detached_cache_io'
            and row['details']['cache_task'] == 'restore_index'
            and row['details']['thread'] == 'worker'
            and row['details']['thread_id'] != gui
            and row['details']['capture_epoch'] == controller.recorder.capture_epoch for row in rows)
        assert all(row['duration_ms'] >= 0 for row in rows)
        assert str(root) not in json.dumps(rows)
        count = _phases(controller)['scene.disk_cache_reload_index']['count']
        foreign = DiskCacheController.bind(SimpleNamespace())
        foreign._reload.result(timeout=10)
        assert _phases(controller)['scene.disk_cache_reload_index']['count'] == count
        assert calls[-1][1] is None
    finally:
        release.set()
        for backing in backings:
            backing.executor.shutdown(wait=True)
            backing.close()
        controller.stop()
    assert DiskCacheController.bind is original_bind
    # An externally retained wrapper is harmless after stop.
    reload_wrapper(backings[0])
    assert _phases(controller)['scene.disk_cache_reload_index']['count'] == count


@pytest.mark.parametrize('boundary', ['restart', 'clear'])
def test_queued_or_running_io_token_never_crosses_capture_epoch(
        editor, controller, monkeypatch, tmp_path, boundary):
    root = tmp_path / 'cache'
    _index(root)
    _bind, backings = _binding(monkeypatch, root)
    original_index = PersistentRenderCache._committed_index
    entered, release = threading.Event(), threading.Event()
    def read(backing):
        entered.set()
        assert release.wait(10)
        return original_index(backing)
    monkeypatch.setattr(PersistentRenderCache, '_committed_index', read)
    controller.start()
    old_epoch = controller.recorder.capture_epoch
    try:
        backing = editor[0].disk_cache.bind()
        assert entered.wait(10)
        if boundary == 'restart':
            controller.stop()
            controller.start()
        else:
            controller.recorder.clear()
        assert controller.recorder.capture_epoch != old_epoch
        release.set()
        backing._reload.result(timeout=10)
        # Calling the new wrapper on the old backing must not adopt the new
        # epoch either, even though its Python backing is still alive.
        with ThreadPoolExecutor(max_workers=1) as executor:
            executor.submit(backing._reload_index).result(timeout=10)
        assert not any(row['name'].startswith(('scene.disk_cache_reload_index',
            'scene.disk_cache_committed_index')) for row in controller.snapshot()['timeline'])
    finally:
        release.set()
        for backing in backings:
            backing.executor.shutdown(wait=True)
            backing.close()
        controller.stop()


def test_focus_metadata_is_live_bounded_and_background_snapshot_is_qt_free(
        editor, controller, monkeypatch, qapp):
    window = editor[0]
    control = QLineEdit(window)
    control.setText('private entered text must never be recorded')
    control.show()
    canvas_child = QLineEdit(window.canvas)
    canvas_child.show()
    window.activateWindow()
    qapp.processEvents()
    controller.start()
    original_focus = QApplication.focusWidget
    try:
        control.setFocus()
        qapp.processEvents()
        assert controller.context()['focus'] == {
            'widget_class': 'QLineEdit', 'canvas_or_descendant': False}
        assert controller.snapshot()['editor_metrics']['focus'] == controller.context()['focus']
        window.canvas.setFocus()
        qapp.processEvents()
        assert controller.context()['focus']['canvas_or_descendant']
        canvas_child.setFocus()
        qapp.processEvents()
        assert controller.context()['focus'] == {
            'widget_class': 'QLineEdit', 'canvas_or_descendant': True}
        cached = controller.snapshot()['editor_metrics']['focus']
        def forbidden():
            raise AssertionError('Background writer read Qt focus')
        monkeypatch.setattr(QApplication, 'focusWidget', forbidden)
        with ThreadPoolExecutor(max_workers=1) as executor:
            assert executor.submit(controller.snapshot).result()['editor_metrics']['focus'] == cached
            assert executor.submit(controller._focus_metrics).result() == {
                'widget_class': None, 'canvas_or_descendant': False}
        snapshot = controller.snapshot()
        assert 'private entered text' not in json.dumps(snapshot)
        focus_rows = [row for row in snapshot['timeline'] if row['name'] == 'focus.changed']
        assert focus_rows and set(focus_rows[-1]['details']) == {'widget_class', 'canvas_or_descendant'}
    finally:
        monkeypatch.setattr(QApplication, 'focusWidget', original_focus)
        controller.stop()
        control.deleteLater()
        canvas_child.deleteLater()


def test_only_owned_gui_border_and_outer_paint_emit_cpu_pairs(editor, controller, monkeypatch):
    from comic_editor.ui import document_presentation
    canvas = editor[0].canvas
    paint = lambda _event: 17
    patches = lambda *_args: []
    monkeypatch.setattr(canvas, 'paintEvent', paint)
    monkeypatch.setattr(document_presentation, '_border_patches', patches)
    controller.start()
    retained = canvas.paintEvent
    try:
        assert canvas.paintEvent(QEvent(QEvent.Paint)) == 17
        # An unguarded construction never claims GUI ownership.
        document_presentation._border_patches(None, None, 1.)
        assert 'presentation.border_construction' not in _phases(controller)
        with controller._owned_scope(dict(ownership='monitored_editor',
                scope='gui_adapter', presentation_owner='document_border')):
            document_presentation._border_patches(None, None, 1.)
        rows = [row for row in controller.snapshot()['timeline']
            if row['name'] in {'canvas.paintEvent', 'canvas.paintEvent.thread_cpu',
                'presentation.border_construction', 'presentation.border_construction.thread_cpu'}]
        assert len(rows) == 4
        cpu = [row for row in rows if row['name'].endswith('.thread_cpu')]
        assert len(cpu) == 2 and all(row['category'] == 'gui_cpu'
            and row['details']['clock'] == 'thread_cpu'
            and row['details']['thread'] == 'gui'
            and row['duration_ms'] >= 0 for row in cpu)
    finally:
        controller.stop()
    assert document_presentation._border_patches is patches
    retained(QEvent(QEvent.Paint))
    assert _phases(controller)['canvas.paintEvent.thread_cpu']['count'] == 1
