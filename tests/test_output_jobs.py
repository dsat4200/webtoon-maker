"""The real export action captures on its owner and evaluates off-thread."""
import threading
import time

import pytest
from PySide6.QtCore import QThread
from PySide6.QtTest import QTest

from comic_editor.core.models import ChapterDocument
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow


def wait_for(predicate, seconds=15):
    deadline = time.monotonic()+seconds
    while not predicate() and time.monotonic() < deadline:
        QTest.qWait(5)
    assert predicate()


@pytest.fixture
def window(qapp, monkeypatch):
    monkeypatch.setattr('comic_editor.ui.canvas.create_network_manager', lambda _: None)
    owner = MainWindow()
    owner.chapter = ChapterDocument(width=8, height=6, background='#FF124578')
    owner.canvas.set_document(owner.chapter, TileStore())
    owner.hierarchy_model.set_chapter(owner.chapter)
    yield owner
    if getattr(owner, '_output_jobs', None) is not None:
        owner._output_jobs.shutdown()
        owner._output_jobs.executor.shutdown(wait=True)
    owner._dirty = False
    owner.deleteLater()


def test_real_action_is_nonblocking_and_only_active_capture_retains_sources(window, tmp_path, monkeypatch):
    from comic_editor.ui import output_jobs
    original = output_jobs.write_export
    started, release = threading.Event(), threading.Event()
    worker_threads = []
    completed = []
    monkeypatch.setattr(window, '_export_completed', lambda *args: completed.append(args))
    def encode(*args, **kwargs):
        worker_threads.append(QThread.currentThread())
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(output_jobs, 'write_export', encode)
    try:
        for index in range(4):
            assert window._write_export_image(tmp_path/f'{index}.png')
        assert not started.is_set()  # capture begins in a later owner-thread turn
        wait_for(started.is_set)
        assert len(window._output_jobs.jobs) == 4
        assert all(job.capture is None for job in list(window._output_jobs.jobs)[1:])
        assert worker_threads[0] != window.thread()
        release.set()
        wait_for(lambda: not window._output_jobs.busy)
        assert len(completed) == 4 and all(error is None for _path, error in completed)
        assert len(list(tmp_path.glob('*.png'))) == 4
    finally:
        release.set()


def test_export_waits_for_accepted_cage_commit_before_capturing(window, tmp_path, monkeypatch):
    from PySide6.QtCore import QRectF
    from PySide6.QtGui import QImage
    completed = []
    monkeypatch.setattr(window, '_export_completed', lambda *args: completed.append(args))
    monkeypatch.setattr(window.canvas, 'commit_active_cage', lambda: True)
    monkeypatch.setattr(window.canvas, 'export_source_rect', lambda: QRectF(0, 0, 8, 6))
    destination = tmp_path/'committed-cage.png'
    for pending in ('_cage_prepare', '_cage_commit_pending', '_cage_session'):
        setattr(window.canvas, pending, object())
        if getattr(window, '_output_jobs', None) is None:
            assert window._write_export_image(destination)
        window._output_jobs.advance()
        job = window._output_jobs.jobs[0]
        assert job.capture is None and job.future is None
        assert not destination.exists()
        setattr(window.canvas, pending, None)
    # The region and model are read after the accepted source transaction.
    monkeypatch.setattr(window.canvas, 'export_source_rect', lambda: QRectF(0, 0, 3, 2))
    window.chapter.background = '#FF336699'
    wait_for(lambda: not window._output_jobs.busy)
    output = QImage(str(destination))
    assert (output.width(), output.height()) == (3, 2)
    assert output.pixelColor(1, 1).name() == '#336699'
    assert completed == [(destination, None)]


def test_failed_cage_commit_reports_export_failure_without_publishing(window, tmp_path, monkeypatch):
    completed = []
    monkeypatch.setattr(window, '_export_completed', lambda *args: completed.append(args))
    monkeypatch.setattr(window.canvas, 'commit_active_cage', lambda: True)
    window.canvas._cage_commit_error = ValueError('Native source failed')
    destination = tmp_path/'failed-cage.png'
    assert window._write_export_image(destination)
    wait_for(lambda: not window._output_jobs.busy)
    assert not destination.exists()
    assert len(completed) == 1 and completed[0][0] == destination
    assert 'Native source failed' in str(completed[0][1])


def test_old_document_completion_never_updates_new_document(window, tmp_path, monkeypatch):
    from comic_editor.ui import output_jobs
    original = output_jobs.write_export
    started, release = threading.Event(), threading.Event()
    completed = []
    monkeypatch.setattr(window, '_export_completed', lambda *args: completed.append(args))
    def encode(*args, **kwargs):
        started.set()
        assert release.wait(10)
        return original(*args, **kwargs)
    monkeypatch.setattr(output_jobs, 'write_export', encode)
    try:
        assert window._write_export_image(tmp_path/'old-document.png')
        wait_for(started.is_set)
        window.chapter = ChapterDocument(width=3, height=2)
        window.canvas.set_document(window.chapter, TileStore())
        release.set()
        wait_for(lambda: not window._output_jobs.busy)
        assert (tmp_path/'old-document.png').exists()
        assert not completed
    finally:
        release.set()
