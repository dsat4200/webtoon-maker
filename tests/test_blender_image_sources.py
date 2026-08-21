from __future__ import annotations

import uuid

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
from comic_editor.integrations.blender_controller import (
    BlenderImageSourceController,
)
from comic_editor.integrations.blender_source import ComicViewInfo
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


def _chapter_with_linked_images(count: int = 1):
    chapter = ChapterDocument(height=900)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 800)
    )
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

    assert restored.schema_version == 21
    assert isinstance(linked.source, EmbeddedImageSourceDescriptor)
    assert linked.source.filename == "legacy.webp"
    assert linked.source.mime_type == "image/webp"
    assert linked.to_dict()["source"] == {
        "type": "embedded",
        "filename": "legacy.webp",
        "mime_type": "image/webp",
    }


def test_blender_source_round_trip_validates_and_canonicalizes_uuids():
    chapter, _page, objects, _images = _chapter_with_linked_images()
    payload = chapter.to_dict()
    restored = ChapterDocument.from_dict(payload)
    obj = restored.objects[objects[0].object_id]

    assert isinstance(obj.source, BlenderComicViewSourceDescriptor)
    assert obj.source.project_uuid == PROJECT_UUID
    assert obj.source.view_uuid == VIEW_UUID
    assert obj.source.last_revision == 2
    assert obj.source_filename == "last-frame.png"
    assert obj.source_mime_type == "image/png"

    record = next(item for item in payload["objects"] if item["id"] == obj.object_id)
    record["source"]["view_uuid"] = "not-a-uuid"
    with pytest.raises(ValueError, match="Comic View UUID"):
        ChapterDocument.from_dict(payload)


def test_runtime_frame_persists_as_offline_png_without_changing_image_geometry(
    tmp_path,
):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Live images")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    page = chapter.layers[chapter.root_page_ids[0]]
    obj = chapter.add_object(page.layer_id, ImageObject(
        pixel_width=640,
        pixel_height=360,
        transform_frame=(0, 0, 640, 360),
        transform_quad=[(25, 30), (500, 40), (490, 330), (20, 320)],
        source=BlenderComicViewSourceDescriptor(
            project_uuid=PROJECT_UUID,
            view_uuid=VIEW_UUID,
            display_name="Panel 12",
            last_revision=4,
        ),
    ))
    images = ImageStore()
    images.put(obj.object_id, "last-frame.png", _png(), "image/png")
    live = _image(320, 180, "#1976d2")
    images.set_runtime_frame(obj.object_id, live)
    assert images.persist_runtime_frame(obj.object_id)
    before = obj.to_dict()

    repository.save_chapter(chapter, tiles, images)
    loaded, _loaded_tiles, loaded_images = repository.load_chapter(
        chapter.chapter_id, include_images=True
    )
    restored = loaded.objects[obj.object_id]

    assert restored.to_dict() == before
    assert restored.pixel_width == 640
    assert restored.pixel_height == 360
    assert restored.transform_quad == obj.transform_quad
    cached = loaded_images.image(obj.object_id)
    assert cached.size() == live.size()
    assert cached.pixelColor(10, 10) == QColor("#1976d2")


def test_controller_commits_aspect_and_keeps_preview_runtime_only(qapp):
    chapter, _page, objects, images = _chapter_with_linked_images(2)
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    controller = BlenderImageSourceController(canvas)
    persisted = []
    controller.cachePersisted.connect(lambda: persisted.append(True))
    original_records = {
        obj.object_id: list(obj.transform_quad) for obj in objects
    }

    controller._frame_ready(
        PROJECT_UUID, VIEW_UUID, 1, 1, "committed",
        _image(300, 200, "#111111")
    )
    assert all(images.runtime_frame(obj.object_id).isNull() for obj in objects)

    frame = _image(300, 200, "#43a047")
    controller._frame_ready(
        PROJECT_UUID, VIEW_UUID, 3, 2, "committed", frame
    )
    assert not canvas.command_stack.can_undo
    for obj in objects:
        assert images.runtime_frame(obj.object_id).size() == frame.size()
        assert obj.source.last_revision == 3
        assert (obj.pixel_width, obj.pixel_height) == (300, 200)
        before = original_records[obj.object_id]
        old_center = (
            sum(point[0] for point in before) / 4,
            sum(point[1] for point in before) / 4,
        )
        new_center = (
            sum(point[0] for point in obj.transform_quad) / 4,
            sum(point[1] for point in obj.transform_quad) / 4,
        )
        assert new_center == pytest.approx(old_center)
        assert obj.transform_frame == (0.0, 0.0, 300.0, 200.0)

    assert controller.flush_pending_frames()
    assert persisted == [True]
    for obj in objects:
        assert images.source(obj.object_id).mime_type == "image/png"
        assert images.source(obj.object_id).filename == "last-frame.png"

    committed = {
        obj.object_id: (obj.to_dict(), bytes(images.source(obj.object_id).data))
        for obj in objects
    }
    preview = _image(240, 320, "#7b1fa2")
    controller._frame_ready(
        PROJECT_UUID, VIEW_UUID, 3, 3, "preview", preview
    )
    assert not controller._pending_ids
    for obj in objects:
        assert obj.to_dict() == committed[obj.object_id][0]
        assert bytes(images.source(obj.object_id).data) == committed[obj.object_id][1]
        assert images.runtime_frame(obj.object_id).size() == preview.size()
        assert obj.object_id in canvas._image_runtime_geometry
    controller.clear_previews()
    for obj in objects:
        assert images.runtime_frame(obj.object_id).isNull()
        assert obj.object_id not in canvas._image_runtime_geometry
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
    images.put(
        foreground.object_id, "foreground.png", _png(120, 120, "#e53935"),
        "image/png",
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", linked.object_id)
    canvas._transform_start_quad = list(linked.transform_quad)
    canvas._transform_preview_quad = [
        (x + 20, y + 10) for x, y in linked.transform_quad
    ]
    canvas._build_raster_transform_cache()

    assert canvas._transform_static_cache.isNull()
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

    assert isinstance(frozen, ImageObject)
    assert isinstance(frozen.source, EmbeddedImageSourceDescriptor)
    assert frozen.transform_frame == obj.transform_frame
    assert [
        (x - frozen.transform_quad[0][0], y - frozen.transform_quad[0][1])
        for x, y in frozen.transform_quad
    ] == [
        (x - obj.transform_quad[0][0], y - obj.transform_quad[0][1])
        for x, y in obj.transform_quad
    ]
    assert asset_images.source(obj.object_id).filename == "Panel 12.png"

    empty = ImageStore()
    with pytest.raises(ValueError, match="cached frame"):
        extract_asset(
            chapter, TileStore(), "object", obj.object_id, "Missing",
            source_images=empty, include_images=True,
        )


def test_relink_is_undoable_and_keeps_cached_pixels_and_transform(qapp):
    chapter, _page, objects, images = _chapter_with_linked_images()
    obj = objects[0]
    original_cache = images.source(obj.object_id).data
    original_quad = list(obj.transform_quad)
    window = MainWindow()
    try:
        window._set_chapter(chapter, TileStore(), images)
        window.canvas.set_selection("object", obj.object_id)
        window._begin_relink_selected_blender_source()
        view = ComicViewInfo(
            PROJECT_UUID, OTHER_VIEW_UUID, "Panel 99", 7, 1920, 1080,
            False, QImage(),
        )
        window._add_blender_comic_view(view)
        relinked = window.canvas.chapter.objects[obj.object_id]
        assert relinked.source.view_uuid == OTHER_VIEW_UUID
        assert relinked.source.last_revision == 7
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
