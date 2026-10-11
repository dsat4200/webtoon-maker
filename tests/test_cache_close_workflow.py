"""Project close remains optional, asynchronous and separate from source saves."""
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    window = MainWindow()
    repositories = []
    for name in ("First", "Second"):
        repository = SeriesRepository(tmp_path / name)
        series = repository.create(name)
        repository.create_chapter(series, "Chapter")
        assert window.open_series(repository.root)
        repositories.append(repository)
    window.disk_cache.timer.stop()
    calls, callbacks = [], []
    cache = window.disk_cache
    monkeypatch.setattr(cache, "can_persist", lambda session: True, raising=False)
    def persist(session, callback):
        calls.append(session.key)
        callbacks.append(callback)
        return True
    monkeypatch.setattr(cache, "persist_for_close", persist, raising=False)
    monkeypatch.setattr(cache, "cancel_persistence", lambda: callbacks[-1](False, ""), raising=False)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.No)
    yield window, calls, callbacks
    if window._cache_close.busy:
        window._cache_close.cancel()
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    monkeypatch.setattr(cache, "can_persist", lambda session: False)
    window.close()
    window.deleteLater()


def test_declining_cache_closes_immediately_without_starting_publication(editor):
    window, calls, _ = editor
    session = window.active_session
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert session.key not in window.sessions
    assert not calls and not window._cache_close.busy


def test_project_without_transferable_cache_closes_without_optional_prompt(editor, monkeypatch):
    window, calls, _ = editor
    session = window.active_session
    monkeypatch.setattr(window.disk_cache, "can_persist", lambda session: False)
    def unexpected(*args):
        pytest.fail("A project with no transferable preview cache asked to save it")
    monkeypatch.setattr(QMessageBox, "question", unexpected)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert session.key not in window.sessions and not calls


def test_accepted_cache_defers_tab_close_until_publication_callback(editor, monkeypatch, qapp):
    window, calls, callbacks = editor
    session = window.active_session
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert calls == [session.key]
    assert session.key in window.sessions and window._cache_close.busy
    assert window._cache_close.dialog.isVisible()
    assert window.canvas.document_read_only and window.canvas.command_stack.read_only
    assert window.hierarchy_model.read_only
    # A second close request cannot detach the source owner during publication.
    window._close_project_tab(0)
    assert window.project_tabs.count() == 2
    callbacks.pop()(True, "")
    qapp.processEvents()
    assert session.key not in window.sessions and not window._cache_close.busy
    assert not window.canvas.document_read_only and not window.canvas.command_stack.read_only


def test_canceling_publication_keeps_project_open(editor, monkeypatch):
    window, _, _ = editor
    session = window.active_session
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    window._close_project_tab(window._tab_index_for_key(session.key))
    resumed = []
    window._deferred_clipboard_sources = ("finished download",)
    monkeypatch.setattr(window, "_clipboard_images_resolved", lambda sources: resumed.append(sources))
    window.project_tabs.setCurrentIndex(0)
    assert window.active_session is session
    assert window.project_tabs.currentIndex() == window._tab_index_for_key(session.key)
    window._cache_close.dialog.findChild(QPushButton).click()
    assert not window._cache_close.busy
    assert not window.canvas.document_read_only and not window.canvas.command_stack.read_only
    assert window.sessions[session.key] is session
    assert window.active_session is session
    assert resumed == [("finished download",)] and window._deferred_clipboard_sources is None


def test_canceling_optional_prompt_keeps_project_open(editor, monkeypatch):
    window, calls, _ = editor
    session = window.active_session
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Cancel)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert window.sessions[session.key] is session
    assert not calls and not window._cache_close.busy


def test_discarded_artwork_is_never_offered_as_saved_cache(editor, monkeypatch):
    window, calls, _ = editor
    session = window.active_session
    window._mark_dirty(None)
    questions = []
    def discard(*args):
        questions.append(args[1])
        return QMessageBox.Discard
    monkeypatch.setattr(QMessageBox, "question", discard)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert questions == ["Unsaved changes"]
    assert not calls and session.key not in window.sessions


def test_source_save_precedes_optional_cache_publication(editor, monkeypatch):
    window, calls, _ = editor
    session = window.active_session
    window._mark_dirty(None)
    order = []
    def answer(*args):
        order.append(args[1])
        return QMessageBox.Save if args[1] == "Unsaved changes" else QMessageBox.Yes
    monkeypatch.setattr(QMessageBox, "question", answer)
    save = window._save_editor_session
    def save_source(owner, **kwargs):
        assert kwargs["wait"]
        result = save(owner, **kwargs)
        order.append("Artwork saved")
        return result
    monkeypatch.setattr(window, "_save_editor_session", save_source)
    window._close_project_tab(window._tab_index_for_key(session.key))
    assert order == ["Unsaved changes", "Artwork saved", "Save preview cache?"]
    assert calls == [session.key] and not session.dirty


def test_inactive_project_cache_does_not_switch_canvas(editor, monkeypatch, qapp):
    window, calls, callbacks = editor
    active = window.active_session
    inactive = next(session for session in window.sessions.values() if session is not active)
    document = window.canvas.chapter
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    window._close_project_tab(window._tab_index_for_key(inactive.key))
    resumed = []
    window._deferred_clipboard_sources = ("other project's download",)
    monkeypatch.setattr(window, "_clipboard_images_resolved", lambda sources: resumed.append(sources))
    assert calls == [inactive.key]
    assert window.active_session is active and window.canvas.chapter is document
    callbacks.pop()(True, "")
    qapp.processEvents()
    assert inactive.key not in window.sessions
    assert window.active_session is active and window.canvas.chapter is document
    assert resumed == [("other project's download",)]


def test_exit_publishes_multiple_projects_sequentially_then_closes(editor, monkeypatch, qapp):
    window, calls, callbacks = editor
    expected = list(window.sessions)
    questions = []
    def yes(*args):
        questions.append(args[1])
        return QMessageBox.Yes
    monkeypatch.setattr(QMessageBox, "question", yes)
    window.show()
    assert not window.close()
    assert calls == expected[:1] and window.isVisible()
    callbacks.pop()(True, "")
    qapp.processEvents()
    assert calls == expected and window.isVisible()
    callbacks.pop()(True, "")
    qapp.processEvents()
    qapp.processEvents()
    assert questions == ["Save preview cache?"]
    assert not window.isVisible() and not window._cache_close.busy


def test_canceling_later_exit_publication_keeps_all_projects_open(editor, monkeypatch, qapp):
    window, calls, callbacks = editor
    expected = list(window.sessions)
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    window.show()
    assert not window.close()
    callbacks.pop()(True, "")
    qapp.processEvents()
    assert calls == expected
    window._cache_close.cancel()
    assert list(window.sessions) == expected and window.isVisible()
    assert not window._cache_exit_approved and not window._cache_close.busy


def test_publication_failure_keeps_project_open_and_reports_error(editor, monkeypatch):
    window, _, callbacks = editor
    session = window.active_session
    messages = []
    monkeypatch.setattr(QMessageBox, "question", lambda *args: QMessageBox.Yes)
    monkeypatch.setattr(QMessageBox, "warning", lambda *args: messages.append(args[-1]))
    window._close_project_tab(window._tab_index_for_key(session.key))
    callbacks.pop()(False, "Disk is full")
    assert window.sessions[session.key] is session and not window._cache_close.busy
    assert messages == ["Disk is full"]


def test_navigator_refresh_continues_while_cache_warms(editor, monkeypatch):
    window, _, _ = editor
    preview = window.preview
    requests = []
    monkeypatch.setattr(preview, "isVisible", lambda: True)
    monkeypatch.setattr(preview, "_interaction_active", lambda: False)
    monkeypatch.setattr(preview, "_navigator_jobs", SimpleNamespace(request=lambda: requests.append(True)))
    window.disk_cache.building = True
    try:
        preview._refresh_cache()
        assert requests == [True]
    finally:
        window.disk_cache.building = False
