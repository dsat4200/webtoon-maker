from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PySide6.QtGui import QColor, QImage
import pytest

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ImageObject, PathNode, RasterObject,
    VectorDrawingObject, VectorStroke, VectorStrokePoint,
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
