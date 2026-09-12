from __future__ import annotations

from pathlib import Path

from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import QMessageBox, QWidget

from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.file_notification import CanvasFileNotification
from comic_editor.ui.main_window import MainWindow


def test_notification_follows_canvas_and_opens_exact_directory(qapp, tmp_path, monkeypatch):
    canvas = QWidget()
    canvas.resize(700, 400)
    canvas.show()
    banner = CanvasFileNotification(canvas)
    opened = []
    monkeypatch.setattr(QDesktopServices, "openUrl", lambda url: opened.append(url.toLocalFile()))
    destination = tmp_path / "a folder ü" / "paint & ink.png"
    banner.show_result("exported", destination)
    qapp.processEvents()
    assert banner.isVisible()
    assert banner.message.text() == f"Successfully exported {destination.name} to {destination.parent}"
    assert banner.open_button.text() == "Open exported location"
    assert banner.x() == 10 and banner.width() == 680
    canvas.resize(440, 400)
    qapp.processEvents()
    assert banner.width() == 420
    banner.open_button.click()
    assert Path(opened[0]) == destination.parent
    banner.show_result("saved", tmp_path / "series.json")
    assert banner.open_button.text() == "Open saved location"
    assert banner.directory == tmp_path
    canvas.deleteLater()


def test_project_title_and_successful_save_banner(tmp_path):
    repository = SeriesRepository(tmp_path / "my-project")
    series = repository.create("Project title")
    repository.create_chapter(series, "Chapter One")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        assert window.windowTitle() == f"{repository.root} — Project title — Webtoon Maker"
        assert window.save()
        assert not window.file_notification.isHidden()
        assert window.file_notification.directory == repository.root
        assert "series.json" in window.file_notification.message.text()
        window._close_project_tab(0)
        assert window.windowTitle() == "Webtoon Maker"
        assert window.file_notification.isHidden()
    finally:
        window.deleteLater()


def test_failed_save_does_not_report_success(tmp_path, monkeypatch):
    repository = SeriesRepository(tmp_path / "my-project")
    series = repository.create("Project title")
    repository.create_chapter(series, "Chapter One")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        def fail(*args, **kwargs):
            raise OSError("Disk full")
        monkeypatch.setattr(window.repository, "save_chapter", fail)
        errors = []
        monkeypatch.setattr(QMessageBox, "critical", lambda *args: errors.append(args[-1]))
        assert not window.save()
        assert errors == ["Disk full"]
        assert window.file_notification.isHidden()
    finally:
        window.deleteLater()
