from __future__ import annotations

import base64

from PySide6.QtCore import (
    QBuffer, QByteArray, QEvent, QIODevice, QMimeData, QPointF, QRectF, Qt,
    QUrl,
)
from PySide6.QtGui import QColor, QImage, QMouseEvent, QPainter
from PySide6.QtNetwork import QNetworkReply
import pytest

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, PathNode, RasterObject,
    TextObject, VectorDrawingObject, VectorStroke, VectorStrokePoint,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.main_window import MainWindow


def _png_bytes(width: int = 80, height: int = 40) -> bytes:
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor("#ff7a31"))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.OpenModeFlag.WriteOnly)
    assert image.save(buffer, "PNG")
    buffer.close()
    return bytes(payload)


def _document():
    chapter = ChapterDocument(height=1080)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1080)
    )
    shape = chapter.add_layer(
        page.layer_id, "Shape", BoundGeometry.rectangle(100, 100, 400, 300)
    )
    return chapter, page, shape


class _DragEvent:
    def __init__(self, mime: QMimeData, position: QPointF):
        self._mime = mime
        self._position = QPointF(position)
        self.accepted = False
        self.drop_action = None

    def mimeData(self):
        return self._mime

    def position(self):
        return QPointF(self._position)

    def setDropAction(self, action):
        self.drop_action = action

    def accept(self):
        self.accepted = True

    def acceptProposedAction(self):
        self.accepted = True

    def ignore(self):
        self.accepted = False


class _FinishedReply:
    def __init__(self, data: bytes, url: str):
        self._data = data
        self._url = QUrl(url)
        self.deleted = False

    def attribute(self, _attribute):
        return 200

    def readAll(self):
        return QByteArray(self._data)

    def url(self):
        return QUrl(self._url)

    def error(self):
        return QNetworkReply.NetworkError.NoError

    def deleteLater(self):
        self.deleted = True


def test_image_store_and_chapter_persistence_preserve_original_bytes(tmp_path):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Images")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    parent = chapter.layers[chapter.root_page_ids[0]]
    obj = chapter.add_object(parent.layer_id, ImageObject(
        source_filename="original.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
    ))
    images = ImageStore()
    original = _png_bytes()
    images.put(obj.object_id, obj.source_filename, original, obj.source_mime_type)
    repository.save_chapter(chapter, tiles, images)

    loaded, loaded_tiles, loaded_images = repository.load_chapter(
        chapter.chapter_id, include_images=True
    )
    restored = loaded.objects[obj.object_id]
    assert isinstance(restored, ImageObject)
    assert loaded_images.source(obj.object_id).data == original
    assert loaded_images.image(obj.object_id).size() == QImage(80, 40, QImage.Format_ARGB32_Premultiplied).size()
    assert loaded_tiles.object_tiles(obj.object_id) == {}


def test_native_and_parent_fit_image_placement_is_one_undo_command(qapp):
    chapter, _page, shape = _document()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("layer", shape.layer_id, activate_default_tool=False)
    source = ("wide.png", "image/png", _png_bytes(200, 100))

    created = canvas.place_image_sources(
        [source], shape.layer_id, QPointF(300, 250), fit_parent=False
    )
    assert len(created) == 1
    free = canvas.chapter.objects[created[0]]
    assert isinstance(free, ImageObject)
    assert free.placement_mode == "free"
    assert canvas._rect_from_quad(free.transform_quad).size().toSize() == QImage(200, 100, QImage.Format_ARGB32_Premultiplied).size()
    canvas.command_stack.undo()
    assert created[0] not in canvas.chapter.objects
    assert canvas.images.source(created[0]) is None
    canvas.command_stack.redo()
    assert canvas.images.source(created[0]).filename == "wide.png"

    fitted_ids = canvas.place_image_sources(
        [source], shape.layer_id, QPointF(300, 250), fit_parent=True
    )
    fitted = canvas.chapter.objects[fitted_ids[0]]
    fitted_rect = canvas._rect_from_quad(canvas._image_fit_quad(fitted))
    assert fitted.placement_mode == "fit_parent"
    assert fitted.fit_mode == "auto_height"
    assert fitted_rect.height() == 300
    assert fitted_rect.width() == 600


def test_image_asset_round_trip_copies_embedded_source(qapp):
    chapter, _page, shape = _document()
    tiles = TileStore()
    images = ImageStore()
    obj = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="asset.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(100, 100), (180, 100), (180, 140), (100, 140)],
    ))
    images.put(obj.object_id, obj.source_filename, _png_bytes(), "image/png")
    manifest, asset_tiles, asset_images = extract_asset(
        chapter, tiles, "object", obj.object_id, "asset.png",
        source_images=images, include_images=True,
    )
    target, page, _shape = _document()
    target_tiles = TileStore()
    target_images = ImageStore()
    kind, root_id, _created = instantiate_asset(
        manifest, asset_tiles, target, target_tiles,
        page.layer_id, 400, 400,
        source_images=asset_images, target_images=target_images,
    )
    assert kind == "object"
    assert isinstance(target.objects[root_id], ImageObject)
    assert target_images.source(root_id).data == images.source(obj.object_id).data


def test_persistent_raster_transform_keeps_tiles_and_quad(qapp):
    chapter, _page, shape = _document()
    raster = chapter.add_object(
        shape.layer_id,
        RasterObject(x=120, y=120, interaction_rect=(0, 0, 120, 80)),
    )
    tiles = TileStore()
    tiles.paint_dab(raster.object_id, QPointF(20, 20), 10, QColor("black"))
    before_tiles = {
        key: image.cacheKey() for key, image in tiles.iter_tiles(raster.object_id)
    }
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, tiles, ImageStore())
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    assert canvas._begin_selected_raster_transform(QPointF(120, 120))
    canvas._update_transform_preview(QPointF(80, 90))
    canvas._commit_object_transform()
    transformed = canvas.chapter.objects[raster.object_id]
    assert transformed.transform_quad is not None
    assert {
        key: image.cacheKey() for key, image in tiles.iter_tiles(raster.object_id)
    } == before_tiles
    canvas.command_stack.undo()
    assert canvas.chapter.objects[raster.object_id].transform_quad is None


def test_image_and_vector_handle_drags_commit_through_normal_pointer_release(qapp):
    chapter, _page, shape = _document()
    images = ImageStore()
    image_obj = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="drag.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(120, 120), (200, 120), (200, 160), (120, 160)],
    ))
    images.put(image_obj.object_id, "drag.png", _png_bytes(), "image/png")
    vector = chapter.add_object(shape.layer_id, VectorDrawingObject(
        strokes=[VectorStroke(points=[
            VectorStrokePoint(x=0, y=0),
            VectorStrokePoint(x=80, y=40),
        ])],
    ))
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)

    canvas.set_selection("object", image_obj.object_id)
    before_image = list(image_obj.transform_quad)
    start = canvas.document_to_widget(QPointF(*before_image[0]))
    canvas._tool_press(start, 1.0)
    canvas._tool_move(start + QPointF(-30, -20), 1.0)
    canvas._tool_release()
    assert image_obj.transform_quad != before_image
    canvas.command_stack.undo()
    assert canvas.chapter.objects[image_obj.object_id].transform_quad == before_image

    vector = canvas.chapter.objects[vector.object_id]
    canvas.set_selection("object", vector.object_id)
    assert canvas.set_tool(ToolKind.TRANSFORM)
    before_vector = canvas.object_world_quad(vector.object_id)
    start = canvas.document_to_widget(QPointF(*before_vector[0]))
    canvas._tool_press(start, 1.0)
    canvas._tool_move(start + QPointF(-25, -15), 1.0)
    canvas._tool_release()
    assert vector.transform_quad is not None
    assert canvas.object_world_quad(vector.object_id) != before_vector


def test_vector_pencil_cage_handle_intercepts_without_drawing(qapp):
    chapter, _page, shape = _document()
    vector = chapter.add_object(shape.layer_id, VectorDrawingObject(
        strokes=[VectorStroke(points=[
            VectorStrokePoint(x=130, y=130),
            VectorStrokePoint(x=230, y=190),
        ])],
    ))
    canvas = CanvasWidget(EditorSettings(
        snap_to_grid=False,
        pencil_transform_handles_visible=True,
    ))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("object", vector.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    handle = QPointF(*canvas.object_world_quad(vector.object_id)[0])
    canvas._tool_press(canvas.document_to_widget(handle), 1.0)
    assert canvas._transform_drag_mode == "handle"
    assert canvas._vector_gesture_mode is None
    canvas._tool_release()


def test_image_translation_cage_updates_live_and_mode_gizmo_is_global(qapp):
    chapter, _page, shape = _document()
    images = ImageStore()
    obj = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="live.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(130, 130), (210, 130), (210, 170), (130, 170)],
    ))
    images.put(obj.object_id, "live.png", _png_bytes(), "image/png")
    settings = EditorSettings(transform_mode="free", snap_to_grid=False)
    canvas = CanvasWidget(settings)
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", obj.object_id)
    canvas.scale = 1.0
    canvas.center_x = 250
    canvas.center_y = 250
    canvas.show()
    qapp.processEvents()

    before = list(obj.transform_quad)
    before_rect = canvas.selected_widget_rect()
    start = canvas.document_to_widget(QPointF(150, 145))
    canvas.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, start,
        QPointF(canvas.mapToGlobal(start.toPoint())),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.OpenHandCursor
    canvas.mousePressEvent(QMouseEvent(
        QEvent.Type.MouseButtonPress, start,
        QPointF(canvas.mapToGlobal(start.toPoint())),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    ))
    assert canvas._pending_raster_transform_press is not None
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    canvas._tool_move(start + QPointF(32, 20), 1.0)
    moved_widget = start + QPointF(34, 22)
    canvas.mouseMoveEvent(QMouseEvent(
        QEvent.Type.MouseMove, moved_widget,
        QPointF(canvas.mapToGlobal(moved_widget.toPoint())),
        Qt.MouseButton.NoButton, Qt.MouseButton.NoButton,
        Qt.KeyboardModifier.NoModifier,
    ))
    assert canvas.cursor().shape() == Qt.CursorShape.ClosedHandCursor
    assert canvas._transform_preview_quad is not None
    assert obj.transform_quad == before
    cage, kind = canvas._active_transform_cage()
    assert kind == "object"
    assert cage == canvas._transform_preview_quad
    assert canvas.selected_widget_rect() != before_rect
    canvas._tool_release()
    assert obj.transform_quad != before

    changes = []
    canvas.transformModeChanged.connect(changes.append)
    gizmo = canvas._transform_mode_gizmo_rect()
    assert not gizmo.isEmpty()
    canvas._tool_press(gizmo.center(), 1.0)
    assert settings.transform_mode == "uniform"
    assert changes == ["uniform"]
    obj.transform_quad = [
        (-10000, -10000), (-9900, -10000),
        (-9900, -9940), (-10000, -9940),
    ]
    clamped = canvas._transform_mode_gizmo_rect()
    assert clamped.left() >= 6 and clamped.top() >= 6
    assert clamped.right() <= canvas.width() - 6
    assert clamped.bottom() <= canvas.height() - 6
    obj.placement_mode = "fit_parent"
    assert canvas._transform_mode_gizmo_rect().isEmpty()


@pytest.mark.parametrize("object_kind", ["raster", "vector"])
def test_transform_mode_gizmo_keeps_one_slot_during_camera_changes(
    qapp, object_kind,
):
    chapter, _page, shape = _document()
    if object_kind == "raster":
        obj = chapter.add_object(shape.layer_id, RasterObject(
            interaction_rect=(0, 0, 120, 80),
            transform_frame=(0, 0, 120, 80),
            transform_quad=[
                (150, 130), (290, 115), (300, 230), (135, 215),
            ],
        ))
    else:
        obj = chapter.add_object(shape.layer_id, VectorDrawingObject(
            strokes=[VectorStroke(points=[
                VectorStrokePoint(x=0, y=0),
                VectorStrokePoint(x=120, y=80),
            ])],
            transform_frame=(0, 0, 120, 80),
            transform_quad=[
                (150, 130), (290, 115), (300, 230), (135, 215),
            ],
        ))
    canvas = CanvasWidget(EditorSettings(
        snap_to_grid=False, pencil_transform_handles_visible=True,
    ))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("object", obj.object_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    canvas.center_x = 250
    canvas.center_y = 190
    canvas.scale = 1.0

    assert not canvas._transform_mode_gizmo_rect().isEmpty()
    initial_slot = canvas._transform_gizmo_slot
    assert initial_slot is not None
    for scale, rotation in (
        (0.35, 0.0), (2.2, 0.0), (2.2, 48.0), (0.7, -31.0),
    ):
        canvas.scale = scale
        canvas.rotation = rotation
        assert not canvas._transform_mode_gizmo_rect().isEmpty()
        assert canvas._transform_gizmo_slot == initial_slot


def test_projective_raster_remains_renderable_and_drawable(qapp):
    chapter, _page, shape = _document()
    raster = chapter.add_object(shape.layer_id, RasterObject(
        interaction_rect=(0, 0, 120, 120),
        transform_frame=(0, 0, 120, 120),
        transform_quad=[
            (60, 70), (220, 100), (220, 220), (100, 220),
        ],
    ))
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(20, 20), 16, QColor("#111111")
    )
    canvas = CanvasWidget(EditorSettings(
        snap_to_grid=False, pencil_transform_handles_visible=True,
    ))
    canvas.resize(900, 700)
    canvas.set_document(chapter, tiles, ImageStore())
    canvas.set_selection("object", raster.object_id)
    canvas.center_x = 450
    canvas.center_y = 350
    canvas.scale = 1.0
    original_quad = list(raster.transform_quad)
    original_frame = tuple(raster.transform_frame)

    image = QImage(900, 700, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._render_raster_content(
        painter, raster, QRectF(0, 0, 1080, 1080),
        use_transform_preview=False,
    )
    painter.end()

    first_local = QPointF(35, 45)
    second_local = QPointF(55, 65)
    canvas._tool_press(
        canvas.document_to_widget(
            canvas._raster_world_point(raster, first_local)
        ),
        1.0,
    )
    assert canvas._drawing
    canvas._tool_move(
        canvas.document_to_widget(
            canvas._raster_world_point(raster, second_local)
        ),
        1.0,
    )
    canvas._tool_release()

    assert raster.transform_quad == original_quad
    assert raster.transform_frame == original_frame
    assert tiles.content_bounds(raster.object_id).contains(second_local)
    canvas.command_stack.undo()
    assert raster.transform_quad == original_quad
    canvas.command_stack.redo()
    assert raster.transform_quad == original_quad

    canvas.set_tool(ToolKind.RASTER_ERASER)
    canvas._tool_press(
        canvas.document_to_widget(
            canvas._raster_world_point(raster, second_local)
        ),
        1.0,
    )
    assert canvas._drawing
    canvas._tool_release()
    assert raster.transform_quad == original_quad
    assert raster.transform_frame == original_frame
    canvas.command_stack.undo()
    assert raster.transform_quad == original_quad


def test_transform_pivot_double_click_resets_to_follow_live_center(qapp):
    chapter, _page, shape = _document()
    obj = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="pivot.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(130, 130), (210, 130), (210, 170), (130, 170)],
    ))
    images = ImageStore()
    images.put(obj.object_id, "pivot.png", _png_bytes(), "image/png")
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", obj.object_id)
    canvas._transform_pivot = QPointF(180, 155)
    canvas._transform_pivot_custom = True
    widget = canvas.document_to_widget(canvas._transform_pivot)
    canvas.mouseDoubleClickEvent(QMouseEvent(
        QEvent.Type.MouseButtonDblClick, widget,
        QPointF(canvas.mapToGlobal(widget.toPoint())),
        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
        Qt.KeyboardModifier.NoModifier,
    ))
    assert canvas._transform_pivot is None
    assert not canvas._transform_pivot_custom
    _handles, _rotate, pivot = canvas._transform_control_points(
        [(150, 150), (250, 150), (250, 210), (150, 210)], None
    )
    assert pivot == QPointF(200, 180)


def test_external_drag_normalizes_and_imports_explorer_and_browser_payloads(
    qapp, tmp_path,
):
    chapter, _page, shape = _document()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("layer", shape.layer_id, activate_default_tool=False)
    source_path = tmp_path / "Explorer Sample.png"
    source_path.write_bytes(_png_bytes(96, 48))
    mime = QMimeData()
    mime.setUrls([QUrl.fromLocalFile(str(source_path))])
    position = canvas.document_to_widget(QPointF(250, 220))
    enter = _DragEvent(mime, position)
    assert canvas._begin_external_image_drag(enter)
    assert enter.accepted
    assert enter.drop_action == Qt.DropAction.CopyAction
    assert canvas._asset_drag_valid
    drop = _DragEvent(mime, position)
    canvas.dropEvent(drop)
    assert drop.accepted
    imported = [
        obj for obj in chapter.objects.values() if isinstance(obj, ImageObject)
    ]
    assert len(imported) == 1
    assert imported[0].source_filename == source_path.name
    assert canvas.images.source(imported[0].object_id).data == source_path.read_bytes()

    encoded = base64.b64encode(_png_bytes(20, 10)).decode("ascii")
    browser = QMimeData()
    browser.setHtml(f'<img src="data:image/png;base64,{encoded}">')
    entries = canvas._external_image_entries(browser)
    assert len(entries) == 1 and entries[0]["data"]

    chromium = QMimeData()
    chromium.setData(
        "DownloadURL",
        QByteArray(b"image/png:browser-name.png:https://example.invalid/image.png"),
    )
    chromium_entries = canvas._external_image_entries(chromium)
    assert chromium_entries[0]["filename"] == "browser-name.png"
    assert chromium_entries[0]["url"].startswith("https://")
    assert chromium_entries[0]["pending"]

    direct = QMimeData()
    direct.setImageData(QImage.fromData(_png_bytes(24, 12)))
    assert canvas._external_image_sources(direct)[0][2]

    fallback = QMimeData()
    fallback.setHtml('<img src="https://example.invalid/original.png">')
    fallback.setImageData(QImage.fromData(_png_bytes(30, 15)))
    fallback_entries = canvas._external_image_entries(fallback)
    assert len(fallback_entries) == 1
    assert fallback_entries[0]["pending"] and fallback_entries[0]["data"]


def test_remote_only_drop_commits_after_async_original_decodes(qapp):
    chapter, _page, shape = _document()
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    entry = {
        "filename": "remote.png", "mime_type": "image/png", "data": b"",
        "url": "https://example.invalid/remote.png", "pending": True,
        "failed": False,
    }
    canvas._external_drag_entries = [entry]
    canvas._pending_external_drop = {
        "entries": [entry], "parent_id": shape.layer_id,
        "world": QPointF(260, 220), "insertion_index": None,
        "fit_parent": True,
    }
    reply = _FinishedReply(_png_bytes(64, 32), entry["url"])
    canvas._external_drag_replies[reply] = (
        canvas._external_drag_generation, entry
    )
    canvas._finish_external_drag_download(reply)
    imported = [
        obj for obj in chapter.objects.values() if isinstance(obj, ImageObject)
    ]
    assert len(imported) == 1
    assert imported[0].placement_mode == "fit_parent"
    assert canvas.images.source(imported[0].object_id).data == _png_bytes(64, 32)
    assert canvas.command_stack.can_undo
    assert reply.deleted


def test_foreign_image_selection_wins_before_page_fallback(qapp):
    chapter, page, shape = _document()
    image = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="select.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(300, 220), (380, 220), (380, 260), (300, 260)],
    ))
    text = chapter.add_object(shape.layer_id, TextObject(
        text="Selected text", layout_mode="free",
        transform_quad=[(120, 120), (240, 120), (240, 180), (120, 180)],
    ))
    raster = chapter.add_object(shape.layer_id, RasterObject(
        x=430, y=320, interaction_rect=(0, 0, 80, 80),
    ))
    images = ImageStore()
    images.put(image.object_id, "select.png", _png_bytes(), "image/png")
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), images)
    canvas.set_selection("object", text.object_id)
    assert canvas.tool == ToolKind.TEXT_EDIT
    canvas._tool_press(canvas.document_to_widget(QPointF(340, 240)), 1.0)
    assert canvas.selected_object_id == image.object_id

    canvas.set_selection("object", text.object_id)
    assert canvas.set_tool(ToolKind.TRANSFORM)
    canvas._tool_press(canvas.document_to_widget(QPointF(340, 240)), 1.0)
    assert canvas.selected_object_id == image.object_id

    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    widget = canvas.document_to_widget(QPointF(340, 240))
    canvas._tool_press(widget, 1.0)
    assert canvas._pending_raster_press is not None
    canvas._tool_release()
    assert canvas.selected_object_id == image.object_id
    assert canvas.tool == ToolKind.OBJECT_SELECT

    canvas.set_selection("layer", shape.layer_id, activate_default_tool=False)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    canvas._tool_press(canvas.document_to_widget(QPointF(340, 240)), 1.0)
    assert canvas.selected_id == shape.layer_id
    assert canvas.selected_object_id == ""
    assert canvas._active_shape_control == "translate"


def test_draft_point_deletion_clears_all_stale_references(qapp):
    chapter, _page, shape = _document()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("layer", shape.layer_id, activate_default_tool=False)
    assert canvas.set_tool(ToolKind.SHAPE_CREATE)
    canvas._creation_nodes = [
        PathNode(x=10, y=10), PathNode(x=30, y=20), PathNode(x=50, y=40),
    ]
    canvas._creation_selected_node_id = canvas._creation_nodes[-1].node_id
    canvas._creation_active_control = "outgoing"
    while canvas._creation_nodes:
        assert canvas._delete_creation_node(canvas._creation_selected_node_id)
    assert canvas._creation_selected_node_id == ""
    assert canvas._creation_active_control is None
    assert canvas._shape_hover_target is None
    assert not canvas._delete_creation_node()


def test_transform_cache_cleanup_survives_render_exception(qapp, monkeypatch):
    chapter, _page, shape = _document()
    obj = chapter.add_object(
        shape.layer_id,
        RasterObject(x=120, y=120, interaction_rect=(0, 0, 80, 40)),
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore(), ImageStore())
    canvas.set_selection("object", obj.object_id)

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("injected render failure")

    monkeypatch.setattr(canvas, "_render_layer", fail_render)
    with pytest.raises(RuntimeError, match="injected render failure"):
        canvas._build_raster_transform_cache()
    assert canvas._render_excluded_object_id == ""
    assert canvas._transform_static_cache.isNull()


def test_rasterize_image_preserves_identity_and_is_undoable(qapp):
    chapter, _page, shape = _document()
    obj = chapter.add_object(shape.layer_id, ImageObject(
        source_filename="source.png", source_mime_type="image/png",
        pixel_width=80, pixel_height=40,
        transform_frame=(0, 0, 80, 40),
        transform_quad=[(120, 120), (220, 115), (215, 180), (125, 175)],
    ))
    images = ImageStore()
    original = _png_bytes()
    images.put(obj.object_id, obj.source_filename, original, obj.source_mime_type)
    window = MainWindow()
    try:
        window._set_chapter(chapter, TileStore(), images)
        window._rasterize_image(obj.object_id)
        raster = window.canvas.chapter.objects[obj.object_id]
        assert isinstance(raster, RasterObject)
        assert raster.transform_quad == obj.transform_quad
        assert window.canvas.images.source(obj.object_id) is None
        assert window.canvas.tiles.object_tiles(obj.object_id)
        window.canvas.command_stack.undo()
        restored = window.canvas.chapter.objects[obj.object_id]
        assert isinstance(restored, ImageObject)
        assert window.canvas.images.source(obj.object_id).data == original
        window.canvas.command_stack.redo()
        assert isinstance(window.canvas.chapter.objects[obj.object_id], RasterObject)
    finally:
        window._dirty = False
        window.close()
