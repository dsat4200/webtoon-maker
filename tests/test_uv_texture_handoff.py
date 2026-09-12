from __future__ import annotations

import json

import pytest
from PIL import Image
from PySide6.QtCore import QRectF
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtWidgets import QMessageBox

from comic_editor.core.external_images import import_uv_overlay, prepare_image_project
from comic_editor.core.models import ImageObject
from comic_editor.core.persistence import SeriesRepository
from comic_editor.ui.main_window import MainWindow


def texture_with_uv(tmp_path):
    image = tmp_path / "Cube.png"
    Image.new("RGBA", (64, 32), (80, 90, 100, 255)).save(image)
    sidecar = image.with_name(image.name + ".webtoon.json")
    data = {"schema": "webtoon.texture.v1", "image": image.name,
            "uv_overlay": {"name": "Cube UV Map", "width": 64, "height": 32,
                           "segments": [[0.25, 0.75, 0.75, 0.75]]}}
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    return image, sidecar, data


def test_uv_is_separate_embedded_guide_with_blender_y_orientation(tmp_path):
    path, _, _ = texture_with_uv(tmp_path)
    original = path.read_bytes()
    repository = SeriesRepository(prepare_image_project(path))
    series = repository.load_series()
    chapter, _, images = repository.load_chapter(series.chapters[0].chapter_id, include_images=True)
    guide = next(obj for obj in chapter.objects.values() if obj.reference_role == "uv_map")
    assert len(chapter.objects) == 2
    assert guide.name == "Cube UV Map"
    assert chapter.layers[guide.parent_layer_id].children[0].entity_id == guide.object_id
    assert images.image(guide.object_id).pixelColor(32, 8).alpha() > 0
    assert images.image(guide.object_id).pixelColor(32, 24).alpha() == 0
    assert not import_uv_overlay(chapter, images, path)
    assert path.read_bytes() == original


def test_reopening_refreshes_uv_without_replacing_paint_or_duplicating_guide(tmp_path):
    path, sidecar, data = texture_with_uv(tmp_path)
    window = MainWindow()
    try:
        assert window.open_path(path)
        session = window.active_session
        guide = next(obj for obj in window.chapter.objects.values() if obj.reference_role == "uv_map")
        guide.visible = False
        data["uv_overlay"]["segments"] = [[0.25, 0.25, 0.75, 0.25]]
        sidecar.write_text(json.dumps(data), encoding="utf-8")
        assert window.open_path(path)
        assert window.active_session is session
        assert len(window.chapter.objects) == 2
        assert not guide.visible
        assert window.canvas.images.image(guide.object_id).pixelColor(32, 24).alpha() > 0
        assert window._dirty
        assert window.save()
    finally:
        window.deleteLater()


def test_uv_visible_in_editor_but_never_burned_into_export(tmp_path):
    path, _, _ = texture_with_uv(tmp_path)
    window = MainWindow()
    try:
        assert window.open_path(path)
        preview = QImage(64, 32, QImage.Format_ARGB32_Premultiplied)
        window.canvas.render_preview(preview)
        assert preview.pixelColor(32, 8) == QColor(80, 90, 100)
        guide = next(obj for obj in window.chapter.objects.values() if obj.reference_role == "uv_map")
        assert guide.visible
        live = QImage(64, 32, QImage.Format_ARGB32_Premultiplied)
        live.fill(QColor("transparent"))
        painter = QPainter(live)
        window.canvas._render_object(painter, guide, 1.0, QRectF(0, 0, 64, 32))
        painter.end()
        assert live.pixelColor(32, 8).alpha() > 0
        assert window._write_export_png(tmp_path / "painted.png")
        with Image.open(tmp_path / "painted.png") as image:
            assert image.getpixel((32, 8)) == (80, 90, 100, 255)
    finally:
        window.deleteLater()


@pytest.mark.parametrize("segment", [[0, 1, 2], [0, 0, float("nan"), 1], [0, 0, 1e10, 1]])
def test_invalid_uv_metadata_never_publishes_partial_project(tmp_path, segment):
    path, sidecar, data = texture_with_uv(tmp_path)
    data["uv_overlay"]["segments"] = [segment]
    sidecar.write_text(json.dumps(data), encoding="utf-8")
    with pytest.raises(ValueError):
        prepare_image_project(path)
    assert not path.with_suffix("").exists()
