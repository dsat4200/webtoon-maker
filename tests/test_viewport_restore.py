"""Reopening documents restores the camera without changing saved artwork."""
import json
import os
from pathlib import Path
import subprocess
import sys

from PIL import Image
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtNetwork import QNetworkAccessManager
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QMessageBox

from comic_editor.core import settings as settings_module
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings, load_settings, save_settings
from comic_editor.core.viewport_state import MAX_SAVED_VIEWPORTS, VIEWPORT_FIELDS
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def editor(qapp, tmp_path, monkeypatch):
    monkeypatch.setattr(settings_module, "settings_path", lambda: tmp_path / "preferences.json")
    monkeypatch.setattr("comic_editor.ui.canvas.create_network_manager", QNetworkAccessManager)
    monkeypatch.setattr("comic_editor.ui.clipboard_history.create_network_manager", QNetworkAccessManager)
    def unexpected_dialog(*args):
        pytest.fail(str(args[1:]))
    for method in ("question", "critical", "warning"):
        monkeypatch.setattr(QMessageBox, method, unexpected_dialog)
    window = MainWindow()
    yield window
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.close()
    window.canvas._effect_jobs.cancel()
    window.deleteLater()


def image_file(path):
    Image.new("RGBA", (320, 2400), (90, 150, 200, 255)).save(path)
    return path


def camera(canvas):
    return {field: getattr(canvas, field) for field in VIEWPORT_FIELDS}


def navigate(canvas, center_x, center_y, scale, rotation):
    for field, value in zip(VIEWPORT_FIELDS, (center_x, center_y, scale, rotation)):
        setattr(canvas, field, value)
    canvas.cameraChanged.emit()
    canvas.update()
    return camera(canvas)


def test_reopen_image_tab_preserves_pan_zoom_rotation_without_saving_artwork(editor, tmp_path, qapp):
    path = image_file(tmp_path / "long image.png")
    assert editor.open_path(path)
    original_image = path.read_bytes()
    chapter_file = next((editor.repository.root / "chapters").glob("*/chapter.json"))
    original_chapter = chapter_file.read_bytes()
    original_model = editor.chapter.to_dict()
    expected = navigate(editor.canvas, 183.5, 1780.25, 1.7, -23.5)
    assert not editor._dirty and not editor.active_session.dirty
    assert editor.chapter.to_dict() == original_model
    editor._close_project_tab(0)
    assert not editor.sessions
    assert editor.open_path(path)
    assert camera(editor.canvas) == expected
    assert editor.canvas.widget_to_document(QPointF(editor.canvas.width()/2, editor.canvas.height()/2)) == QPointF(183.5, 1780.25)
    editor.resize(1320, 820)
    editor.show()
    QTest.qWait(40)
    qapp.processEvents()
    assert camera(editor.canvas) == expected
    assert not editor._dirty
    assert chapter_file.read_bytes() == original_chapter
    assert path.read_bytes() == original_image


def test_multiple_images_restore_independent_views_after_restart(editor, tmp_path):
    first = image_file(tmp_path / "first.png")
    second = image_file(tmp_path / "second.png")
    assert editor.open_path(first)
    expected_first = navigate(editor.canvas, 100, 1500, 2, 31)
    assert editor.open_path(second)
    expected_second = navigate(editor.canvas, 250, 800, .6, -45)
    assert editor.open_path(first)
    assert camera(editor.canvas) == expected_first
    # Close the inactive image, then exit with the other image still open.
    editor._close_project_tab(1)
    assert editor.close()
    preferences = settings_module.settings_path()
    assert len(load_settings().document_viewports) == 2
    script = """
import json, sys
from pathlib import Path
from PySide6.QtWidgets import QApplication
from PySide6.QtNetwork import QNetworkAccessManager
from comic_editor.core import settings
import comic_editor.ui.canvas as canvas_module
import comic_editor.ui.clipboard_history as clipboard_module
from comic_editor.ui.main_window import MainWindow
settings.settings_path = lambda: Path(sys.argv[1])
canvas_module.create_network_manager = QNetworkAccessManager
clipboard_module.create_network_manager = QNetworkAccessManager
app = QApplication([])
window = MainWindow()
views = []
for path in sys.argv[2:]:
    assert window.open_path(path)
    views.append({field: getattr(window.canvas, field) for field in ('center_x','center_y','scale','rotation')})
print('VIEWS=' + json.dumps(views))
window.close()
"""
    environment = dict(os.environ, OPENBLAS_NUM_THREADS="1", OMP_NUM_THREADS="1", MKL_NUM_THREADS="1")
    result = subprocess.run([sys.executable, "-c", script, str(preferences), str(first), str(second)],
                            text=True, capture_output=True, timeout=30, env=environment)
    assert result.returncode == 0, result.stderr
    record = next(line[6:] for line in result.stdout.splitlines() if line.startswith("VIEWS="))
    assert json.loads(record) == [expected_first, expected_second]


def test_chapters_restore_views_and_new_documents_keep_default_view(editor, tmp_path):
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Views")
    first, _ = repository.create_chapter(series, "First")
    second, _ = repository.create_chapter(series, "Second")
    assert editor.open_series(repository.root)
    initial = camera(editor.canvas)
    expected = navigate(editor.canvas, 670, 2100, .8, 15)
    editor._load_chapter(second.chapter_id)
    assert camera(editor.canvas) == initial
    other = navigate(editor.canvas, 310, 1100, 1.3, -20)
    editor._load_chapter(first.chapter_id)
    assert camera(editor.canvas) == expected
    editor._load_chapter(second.chapter_id)
    assert camera(editor.canvas) == other
    assert not editor._dirty


def test_navigation_debounces_settings_and_reset_view_replaces_saved_camera(editor, tmp_path, qapp):
    path = image_file(tmp_path / "navigate.png")
    assert editor.open_path(path)
    editor.canvas.scroll_to_fraction(.7)
    expected = camera(editor.canvas)
    assert editor.layout_settings_timer.isActive()
    QTest.qWait(650)
    qapp.processEvents()
    assert load_settings().document_viewports[editor._document_view_key()] == expected
    assert not editor._dirty
    editor.canvas.reset_view()
    reset = camera(editor.canvas)
    assert reset != expected
    editor._close_project_tab(0)
    assert editor.open_path(path)
    assert camera(editor.canvas) == reset


def test_viewport_settings_validate_old_corrupt_and_bounded_records(tmp_path, monkeypatch):
    path = tmp_path / "settings.json"
    monkeypatch.setattr(settings_module, "settings_path", lambda: path)
    path.write_text('{}', encoding="utf-8")
    assert not load_settings().document_viewports
    valid = dict(center_x=100, center_y=200, scale=1, rotation=0)
    records = {str(index): dict(valid) for index in range(MAX_SAVED_VIEWPORTS + 10)}
    records.update(broken={}, invalid=dict(valid, center_y=float('nan')),
                   negative=dict(valid, scale=-1), malformed="camera", zero=dict(valid, scale=0))
    settings = EditorSettings(document_viewports=records)
    assert len(settings.document_viewports) == MAX_SAVED_VIEWPORTS
    assert "0" not in settings.document_viewports and "209" in settings.document_viewports
    save_settings(settings)
    assert load_settings().document_viewports == settings.document_viewports
    path.write_text(json.dumps({'document_viewports': []}), encoding="utf-8")
    assert not load_settings().document_viewports
