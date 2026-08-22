from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import PropertyMock, patch

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.assets import extract_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BlenderComicViewSourceDescriptor, BoundGeometry, ChapterDocument,
    EmbeddedImageSourceDescriptor, ImageObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.integrations import blender_controller as controller_module
from comic_editor.integrations.blender_controller import (
    BlenderImageSourceController, load_published_png,
)
from comic_editor.integrations.blender_source import BlenderSourceClient, ComicViewInfo
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow


PROJECT_UUID = uuid.UUID(int=101).hex
VIEW_UUID = uuid.UUID(int=202).hex
OTHER_VIEW_UUID = uuid.UUID(int=303).hex


def _image(width: int, height: int, color: str) -> QImage:
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    return image


def _png(width: int = 80, height: int = 40, color: str = "#ff7138") -> bytes:
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert _image(width, height, color).save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _published(tmp_path: Path, width=300, height=200, color="#43a047") -> Path:
    path = tmp_path / "3.png"
    path.write_bytes(_png(width, height, color))
    return path


def _chapter_with_linked_images(count: int = 1):
    chapter = ChapterDocument(height=900)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 800))
    images = ImageStore()
    objects = []
    for index in range(count):
        obj = chapter.add_object(page.layer_id, ImageObject(
            name=f"Linked {index + 1}",
            pixel_width=640,
            pixel_height=360,
            transform_frame=(0, 0, 640, 360),
            transform_quad=[
                (100 + index * 30, 120), (740 + index * 30, 110),
                (720 + index * 30, 480), (90 + index * 30, 470),
            ],
            source=BlenderComicViewSourceDescriptor(
                project_uuid=PROJECT_UUID,
                view_uuid=VIEW_UUID,
                display_name="Panel 12",
                last_revision=2,
            ),
        ))
        images.put(obj.object_id, "last-frame.png", _png(), "image/png")
        objects.append(obj)
    return chapter, page, objects, images


def _view(path: Path | str = "", *, revision=3, width=300, height=200, dirty=False):
    return ComicViewInfo(
        PROJECT_UUID, VIEW_UUID, "Panel 12", revision, width, height,
        dirty, QImage(), str(path),
    )


def test_schema_15_image_migrates_to_embedded_source_without_eager_rewrite():
    chapter = ChapterDocument()
    page = chapter.add_page()
    image = chapter.add_object(page.layer_id, ImageObject(
        source_filename="legacy.webp",
        source_mime_type="image/webp",
        pixel_width=40,
        pixel_height=30,
    ))
    payload = chapter.to_dict()
    payload["schema_version"] = 15
    record = next(item for item in payload["objects"] if item["id"] == image.object_id)
    record.pop("source")

    restored = ChapterDocument.from_dict(payload)
    linked = restored.objects[image.object_id]
    assert isinstance(linked.source, EmbeddedImageSourceDescriptor)
    assert linked.source.filename == "legacy.webp"
    assert linked.source.mime_type == "image/webp"


def test_blender_source_round_trip_keeps_existing_project_schema():
    chapter, _page, objects, _images = _chapter_with_linked_images()
    payload = chapter.to_dict()
    restored = ChapterDocument.from_dict(payload)
    obj = restored.objects[objects[0].object_id]
    assert isinstance(obj.source, BlenderComicViewSourceDescriptor)
    assert obj.source.project_uuid == PROJECT_UUID
    assert obj.source.view_uuid == VIEW_UUID
    assert obj.source.last_revision == 2
    assert obj.source_filename == "last-frame.png"

    record = next(item for item in payload["objects"] if item["id"] == obj.object_id)
    record["source"]["view_uuid"] = "not-a-uuid"
    with pytest.raises(ValueError, match="Comic View UUID"):
        ChapterDocument.from_dict(payload)


def test_predecoded_png_persists_without_runtime_frame_state(tmp_path):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Published images")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    page = chapter.layers[chapter.root_page_ids[0]]
    obj = chapter.add_object(page.layer_id, ImageObject(
        pixel_width=320,
        pixel_height=180,
        transform_frame=(0, 0, 320, 180),
        source=BlenderComicViewSourceDescriptor(
            project_uuid=PROJECT_UUID, view_uuid=VIEW_UUID,
            display_name="Panel 12", last_revision=4,
        ),
    ))
    images = ImageStore()
    raw = _png(320, 180, "#1976d2")
    images.put_decoded(obj.object_id, "last-frame.png", raw, _image(320, 180, "#1976d2"))
    assert not hasattr(images, "runtime_frame")

    repository.save_chapter(chapter, tiles, images)
    loaded, _loaded_tiles, loaded_images = repository.load_chapter(
        chapter.chapter_id, include_images=True
    )
    restored = loaded.objects[obj.object_id]
    assert restored.to_dict() == obj.to_dict()
    assert loaded_images.source(obj.object_id).data == raw
    assert loaded_images.image(obj.object_id).pixelColor(10, 10) == QColor("#1976d2")


def test_controller_imports_atomic_png_once_and_updates_matching_objects(qapp, tmp_path):
    chapter, _page, objects, images = _chapter_with_linked_images(2)
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    controller = BlenderImageSourceController(canvas)
    imported = []
    controller.frameImported.connect(lambda: imported.append(True))
    originals = {obj.object_id: list(obj.transform_quad) for obj in objects}
    path = _published(tmp_path)

    controller._set_views([_view(path)])
    assert imported == [True]
    for obj in objects:
        assert images.source(obj.object_id).data == path.read_bytes()
        assert images.source(obj.object_id).filename == "last-frame.png"
        assert obj.source.last_revision == 3
        assert (obj.pixel_width, obj.pixel_height) == (300, 200)
        old_center = tuple(sum(p[i] for p in originals[obj.object_id]) / 4 for i in (0, 1))
        new_center = tuple(sum(p[i] for p in obj.transform_quad) / 4 for i in (0, 1))
        assert new_center == pytest.approx(old_center)
        assert obj.transform_frame == (0.0, 0.0, 300.0, 200.0)
    assert not canvas.command_stack.can_undo

    controller._set_views([_view(path)])
    assert imported == [True]
    controller.shutdown()
    controller.deleteLater()


def test_invalid_or_outdated_publication_preserves_last_good_frame(qapp, tmp_path):
    chapter, _page, objects, images = _chapter_with_linked_images()
    obj = objects[0]
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore(), images)
    controller = BlenderImageSourceController(canvas)
    errors = []
    controller.errorOccurred.connect(errors.append)
    before_model = obj.to_dict()
    before_bytes = images.source(obj.object_id).data

    controller._set_views([_view(tmp_path / "missing.png")])
    assert errors
    assert obj.to_dict() == before_model
    assert images.source(obj.object_id).data == before_bytes

    old_path = _published(tmp_path, 300, 200, "#d32f2f")
    controller._set_views([_view(old_path, revision=1)])
    assert obj.to_dict() == before_model
    assert images.source(obj.object_id).data == before_bytes
    controller.shutdown()
    controller.deleteLater()


def test_published_png_validation_rejects_unsafe_or_inconsistent_files(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match="absolute"):
        load_published_png(_view("relative.png"))

    not_png = tmp_path / "3.jpg"
    not_png.write_bytes(_png(300, 200))
    with pytest.raises(ValueError, match="not a PNG"):
        load_published_png(_view(not_png))

    wrong_size = tmp_path / "wrong.png"
    wrong_size.write_bytes(_png(100, 100))
    with pytest.raises(ValueError, match="dimensions"):
        load_published_png(_view(wrong_size))

    path = _published(tmp_path)
    monkeypatch.setattr(controller_module, "MAX_FRAME_BYTES", 8)
    with pytest.raises(ValueError, match="file size"):
        load_published_png(_view(path))


def test_controller_reports_unsaved_and_needs_render_without_streaming(qapp):
    chapter, _page, objects, images = _chapter_with_linked_images()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", objects[0].object_id)
    controller = BlenderImageSourceController(canvas)
    statuses = []
    controller.statusChanged.connect(statuses.append)

    controller._views[VIEW_UUID] = _view("", dirty=False)
    with patch.object(
        BlenderSourceClient, "connected", new_callable=PropertyMock, return_value=True,
    ):
        controller.handle_selection()
    assert statuses[-1] == "needs-render"

    controller._views[VIEW_UUID] = _view(r"C:\frames\3.png", dirty=True)
    controller._imported.add((PROJECT_UUID, VIEW_UUID, 3, r"C:\frames\3.png"))
    with patch.object(
        BlenderSourceClient, "connected", new_callable=PropertyMock, return_value=True,
    ):
        controller.handle_selection()
    assert statuses[-1] == "unsaved"
    controller.shutdown()
    controller.deleteLater()


def test_linked_image_transform_keeps_front_image_at_its_stack_position(qapp):
    chapter, page, objects, images = _chapter_with_linked_images()
    linked = objects[0]
    images.put(linked.object_id, "last-frame.png", _png(640, 360, "#1565c0"), "image/png")
    foreground = chapter.add_object(page.layer_id, ImageObject(
        name="Foreground artwork", pixel_width=120, pixel_height=120,
        transform_frame=(0, 0, 120, 120),
        transform_quad=[(280, 230), (400, 230), (400, 350), (280, 350)],
    ), index=0)
    images.put(foreground.object_id, "foreground.png", _png(120, 120, "#e53935"), "image/png")
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", linked.object_id)
    canvas._transform_start_quad = list(linked.transform_quad)
    canvas._transform_preview_quad = [(x + 20, y + 10) for x, y in linked.transform_quad]
    canvas._build_raster_transform_cache()

    rendered = QImage(900, 700, QImage.Format_ARGB32_Premultiplied)
    rendered.fill(Qt.transparent)
    canvas.render(rendered)
    sample = canvas.camera_transform().map(QPointF(340, 290))
    color = rendered.pixelColor(round(sample.x()), round(sample.y()))
    assert color.red() > 180 and color.blue() < 120


def test_copy_as_asset_freezes_linked_source_and_preserves_transform(qapp):
    chapter, _page, objects, images = _chapter_with_linked_images()
    obj = objects[0]
    manifest, _tiles, asset_images = extract_asset(
        chapter, TileStore(), "object", obj.object_id, "Frozen Panel",
        source_images=images, include_images=True,
    )
    frozen = manifest.document.objects[obj.object_id]
    assert isinstance(frozen.source, EmbeddedImageSourceDescriptor)
    assert frozen.transform_frame == obj.transform_frame
    assert asset_images.source(obj.object_id).filename == "Panel 12.png"


def test_relink_is_undoable_and_render_once_ui_is_removed(qapp, tmp_path):
    chapter, _page, objects, images = _chapter_with_linked_images()
    obj = objects[0]
    original_cache = images.source(obj.object_id).data
    original_quad = list(obj.transform_quad)
    window = MainWindow()
    try:
        assert not hasattr(window.selection_settings.image_controls, "render_once")
        window._set_chapter(chapter, TileStore(), images)
        window.canvas.set_selection("object", obj.object_id)
        window._begin_relink_selected_blender_source()
        view = ComicViewInfo(
            PROJECT_UUID, OTHER_VIEW_UUID, "Panel 99", 7, 1920, 1080,
            False, QImage(), str(tmp_path / "7.png"),
        )
        window._add_blender_comic_view(view)
        relinked = window.canvas.chapter.objects[obj.object_id]
        assert relinked.source.view_uuid == OTHER_VIEW_UUID
        assert relinked.transform_quad == original_quad
        assert window.canvas.images.source(obj.object_id).data == original_cache
        window.canvas.command_stack.undo()
        restored = window.canvas.chapter.objects[obj.object_id]
        assert restored.source.view_uuid == VIEW_UUID
        assert restored.transform_quad == original_quad
        assert window.canvas.images.source(obj.object_id).data == original_cache
    finally:
        window._dirty = False
        window.close()
