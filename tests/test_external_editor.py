from __future__ import annotations

import subprocess
import sys
import time
import uuid
from pathlib import Path

from PIL import Image
import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor
from PySide6.QtWidgets import QFileDialog, QMessageBox

from comic_editor.core.external_images import prepare_image_project
from comic_editor.core.models import BoundGeometry, ChapterDocument, RasterObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import load_settings
from comic_editor.launch import FileLaunchBroker
from comic_editor.ui.main_window import MainWindow


@pytest.fixture
def editor(monkeypatch):
    def unexpected(*args):
        pytest.fail(str(args[1:]))
    monkeypatch.setattr(QMessageBox, "critical", unexpected)
    monkeypatch.setattr(QFileDialog, "getSaveFileName", unexpected)
    window = MainWindow()
    yield window
    window.deleteLater()


def make_image(path, size=(37, 23), color=(17, 83, 191, 128)):
    image = Image.new("RGBA", size, color)
    if path.suffix.lower() in {".jpg", ".jpeg", ".bmp"}:
        image = image.convert("RGB")
    image.save(path)
    return path


def test_open_image_creates_native_project_and_export_preserves_rgba(editor, tmp_path):
    path = make_image(tmp_path / "Blender texture ü & 100%.png")
    original = path.read_bytes()
    assert editor.open_path(path)
    assert editor.repository.root == path.with_suffix("")
    assert (editor.chapter.width, editor.chapter.height) == (37, 23)
    assert editor.chapter.document_kind == "image"
    assert path.read_bytes() == original
    assert editor.settings.export_destinations[editor._export_destination_key()] == str(path)
    assert load_settings().export_destinations[editor._export_destination_key()] == str(path)
    assert len(editor.chapter.objects) == 1
    assert (path.with_suffix("") / "series.json").is_file()
    editor._export_again()
    with Image.open(path) as result:
        assert result.size == (37, 23)
        assert result.mode == "RGBA"
        assert result.getpixel((18, 11)) == (18, 84, 191, 128)  # 8-bit premultiplication rounding
        assert result.getpixel((0, 0)) == result.getpixel((18, 11))
        assert result.getpixel((36, 22)) == result.getpixel((18, 11))


def test_repeat_open_keeps_unsaved_edits_then_reopens_saved_layers(editor, tmp_path):
    path = make_image(tmp_path / "Texture.png")
    assert editor.open_path(path)
    original_session = editor.active_session
    page = editor.chapter.layers[editor.chapter.root_page_ids[0]]
    obj = editor.chapter.add_object(page.layer_id, RasterObject(name="My edits"), index=0)
    editor.canvas.tiles.paint_dab(obj.object_id, QPointF(2, 2), 5, QColor("red"), square=True, antialias=False)
    editor._dirty = True
    assert editor.open_path(path)
    assert editor.active_session is original_session
    assert editor._dirty
    assert obj.object_id in editor.chapter.objects
    assert editor.project_tabs.count() == 1
    assert editor.save()
    editor._close_project_tab(0)
    assert editor.open_path(path)
    assert obj.object_id in editor.chapter.objects
    assert len(editor.chapter.objects) == 2


@pytest.mark.parametrize("suffix,format", [
    (".jpg", "JPEG"), (".jpeg", "JPEG"), (".bmp", "BMP"),
    (".tif", "TIFF"), (".tiff", "TIFF"), (".tga", "TGA"), (".webp", "WEBP"),
])
def test_export_again_uses_original_format(editor, tmp_path, suffix, format):
    path = make_image(tmp_path / ("texture" + suffix))
    assert editor.open_path(path)
    editor._export_again()
    with Image.open(path) as result:
        assert result.format == format
        assert result.size == (37, 23)
        assert result.convert("RGB").getpixel((18, 11)) != (0, 0, 0)
    assert not path.with_suffix(".png").exists()


def test_export_applies_edits_and_saved_project_remembers_destination(editor, tmp_path):
    path = make_image(tmp_path / "paint.png")
    assert editor.open_path(path)
    page = editor.chapter.layers[editor.chapter.root_page_ids[0]]
    obj = editor.chapter.add_object(page.layer_id, RasterObject(), index=0)
    editor.canvas.tiles.paint_dab(obj.object_id, QPointF(2, 2), 5, QColor("red"), square=True, antialias=False)
    assert editor.save()
    root = editor.repository.root
    editor._close_project_tab(0)
    assert editor.open_path(root / "series.json")
    editor.settings.export_destinations.clear()
    editor._export_again()
    with Image.open(path) as result:
        assert result.getpixel((2, 2)) == (255, 0, 0, 255)
        assert result.size == (37, 23)


def test_open_other_images_uses_separate_tabs(editor, tmp_path):
    one = make_image(tmp_path / "one.png")
    two = make_image(tmp_path / "two.png", (62, 17))
    assert editor.open_path(one)
    first = editor.chapter.chapter_id
    assert editor.open_path(two)
    assert editor.project_tabs.count() == 2
    assert (editor.chapter.width, editor.chapter.height) == (62, 17)
    assert editor.open_path(one)
    assert editor.chapter.chapter_id == first
    assert editor.settings.export_destinations[editor._export_destination_key()] == str(one)


def test_bad_image_and_folder_collision_leave_files_untouched(tmp_path):
    invalid = tmp_path / "bad.png"
    invalid.write_bytes(b"not an image")
    with pytest.raises(ValueError):
        prepare_image_project(invalid)
    assert not invalid.with_suffix("").exists()
    path = make_image(tmp_path / "taken.png")
    path.with_suffix("").mkdir()
    sentinel = path.with_suffix("") / "notes.txt"
    sentinel.write_text("keep me")
    with pytest.raises(FileExistsError):
        prepare_image_project(path)
    assert sentinel.read_text() == "keep me"
    assert not (path.with_suffix("") / "series.json").exists()


def test_same_stem_different_format_does_not_reuse_project(tmp_path):
    one = make_image(tmp_path / "texture.png")
    two = make_image(tmp_path / "texture.jpg")
    prepare_image_project(one)
    with pytest.raises(FileExistsError):
        prepare_image_project(two)


def test_failed_project_save_does_not_publish_partial_folder(tmp_path, monkeypatch):
    path = make_image(tmp_path / "texture.png")
    original = path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("Disk full")
    monkeypatch.setattr(SeriesRepository, "save_chapter", fail)
    with pytest.raises(OSError):
        prepare_image_project(path)
    assert path.read_bytes() == original
    assert list(tmp_path.iterdir()) == [path]


def test_image_canvas_keeps_size_and_comic_width_rule_stays():
    chapter = ChapterDocument(width=90, height=40, document_kind="image")
    chapter.add_page("Oversize", BoundGeometry.rectangle(0, 0, 200, 200))
    chapter.validate()
    assert chapter.height == 40
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert (restored.width, restored.height) == (90, 40)
    with pytest.raises(ValueError, match="width"):
        ChapterDocument(width=90).validate()


def test_second_process_forwards_paths_and_owner_can_restart(qapp, tmp_path):
    name = "webtoon-test-" + uuid.uuid4().hex
    broker = FileLaunchBroker(name=name)
    assert broker.start([])
    received = []
    broker.files_requested.connect(received.append)
    path = str(tmp_path / "ü & texture.png")
    script = (
        "import sys; from PySide6.QtCore import QCoreApplication; "
        "from comic_editor.launch import FileLaunchBroker; "
        "app=QCoreApplication([]); b=FileLaunchBroker(name=sys.argv[1]); "
        "assert not b.start([sys.argv[2]])"
    )
    process = subprocess.Popen([sys.executable, "-c", script, name, path],
                               stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while process.poll() is None and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.01)
        assert process.poll() is not None
        assert process.returncode == 0, process.communicate()[1].decode()
        assert received == [[path]]
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()
        broker.close()
    replacement = FileLaunchBroker(name=name)
    assert replacement.start([])
    replacement.close()


def test_main_opens_relative_file_arguments_after_window_is_ready(tmp_path):
    first = make_image(tmp_path / "first image.png")
    second = make_image(tmp_path / "second ü.png", (63, 21))
    root = Path(__file__).resolve().parents[1]
    script = """
import sys
from pathlib import Path
sys.path.insert(0, sys.argv.pop(1))
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication
from comic_editor.core import settings
settings.settings_path = lambda: Path('isolated-settings.json').resolve()
import main
from comic_editor.launch import FileLaunchBroker
from comic_editor.ui.main_window import MainWindow
original_show = MainWindow.show
errors = []
def show(window):
    original_show(window)
    def inspect():
        try:
            assert window.project_tabs.count() == 2
            assert window.repository.root.name == 'second ü'
            assert (window.chapter.width, window.chapter.height) == (63, 21)
            assert window.chapter.external_image_path == str(Path('second ü.png').resolve())
        except Exception as error:
            errors.append(error)
        QApplication.instance().exit(0)
    QTimer.singleShot(250, inspect)
MainWindow.show = show
# The broker name is an internal test value, not a file argument.
name = sys.argv.pop(1)
main.FileLaunchBroker = lambda app: FileLaunchBroker(app, name=name)
assert main.main() == 0
assert not errors, errors
"""
    result = subprocess.run(
        [sys.executable, "-c", script, str(root), "webtoon-main-" + uuid.uuid4().hex,
         first.name, second.name], cwd=tmp_path, capture_output=True, text=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert (first.with_suffix("") / "series.json").is_file()
    assert (second.with_suffix("") / "series.json").is_file()
