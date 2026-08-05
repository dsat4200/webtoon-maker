from pathlib import Path

import pytest
import json

from PySide6.QtCore import QMimeData, QPointF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QInputDialog, QMessageBox

from comic_editor.core.assets import (
    AssetRepository, extract_asset, instantiate_asset,
)
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ColorFillGradientObject, RasterObject,
    PathNode, ShapeStyle, VectorDrawingObject, VectorFillObject,
)
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.canvas import ASSET_MIME, CanvasWidget, ToolKind
from comic_editor.core.settings import EditorSettings


def _asset_source():
    chapter = ChapterDocument(name="Source")
    page = chapter.add_page("Page")
    layer = chapter.add_layer(
        page.layer_id, "Character", BoundGeometry.rectangle(30, 40, 260, 180)
    )
    raster = chapter.add_object(layer.layer_id, RasterObject(name="Ink"))
    drawing = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Lines")
    )
    fill = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            geometry=BoundGeometry.rectangle(10, 10, 40, 40),
            fill_color="#FF00AAFF",
        ),
    )
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(15, 20), 18, QColor("#ff2244")
    )
    chapter.validate()
    return chapter, layer, raster, drawing, fill, tiles


def _solid_asset():
    chapter = ChapterDocument(name="Solid source", height=400)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 400, 400),
        style=ShapeStyle(),
    )
    layer = chapter.add_layer(
        page.layer_id, "Solid", BoundGeometry.rectangle(0, 0, 200, 160),
        style=ShapeStyle(primary_color="#FFFF0000"),
    )
    return extract_asset(chapter, TileStore(), "layer", layer.layer_id, "Solid")


def _window_with_asset(tmp_path, name="Hero"):
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Series")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    source_layer = next(layer for layer in chapter.layers.values() if not layer.is_page)
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", source_layer.layer_id, name
    )
    thumbnail = QImage(32, 32, QImage.Format_ARGB32_Premultiplied)
    thumbnail.fill(Qt.transparent)
    AssetRepository(repository.root).create(manifest, asset_tiles, thumbnail)
    window = MainWindow()
    assert window.open_series(repository.root)
    return window, repository, manifest


def _dispose_window(window):
    for session in window.sessions.values():
        session.dirty = False
    window._dirty = False
    window.deleteLater()


def test_asset_documents_allow_fitted_width_and_old_docs_default_to_chapter():
    asset = ChapterDocument(width=333, height=222, document_kind="asset")
    page = asset.add_page("Asset Canvas", BoundGeometry.rectangle(0, 0, 333, 222))
    asset.validate()
    payload = asset.to_dict()
    assert payload["document_kind"] == "asset"
    payload.pop("document_kind")
    payload["size"][0] = 1080
    restored = ChapterDocument.from_dict(payload)
    assert restored.document_kind == "chapter"
    assert page.layer_id in restored.layers


def test_extract_and_instantiate_asset_remaps_graph_and_raster_tiles():
    chapter, layer, raster, drawing, fill, tiles = _asset_source()
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", layer.layer_id, "Character"
    )
    assert manifest.document.document_kind == "asset"
    assert manifest.root_id == layer.layer_id
    assert asset_tiles.content_bounds(raster.object_id) is not None
    copied_drawing = manifest.document.objects[drawing.object_id]
    assert copied_drawing.fill_child_ids == [fill.object_id]

    target = ChapterDocument(name="Target")
    target_page = target.add_page("Page")
    target_tiles = TileStore()
    kind, root_id, object_ids = instantiate_asset(
        manifest, asset_tiles, target, target_tiles,
        target_page.layer_id, 500, 600,
    )
    assert kind == "layer"
    assert root_id != manifest.root_id
    assert not object_ids.intersection(manifest.document.objects)
    copied_raster_id = next(
        object_id for object_id in object_ids
        if isinstance(target.objects[object_id], RasterObject)
    )
    assert target_tiles.content_bounds(copied_raster_id) is not None
    placed_drawing = next(
        obj for obj in target.objects.values()
        if isinstance(obj, VectorDrawingObject)
    )
    assert placed_drawing.fill_child_ids[0] in target.objects
    assert placed_drawing.fill_child_ids[0] != fill.object_id
    target.validate()


def test_asset_repository_is_per_series_and_rename_keeps_stable_folder(
    qapp, tmp_path,
):
    chapter, layer, _raster, _drawing, _fill, tiles = _asset_source()
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", layer.layer_id, "Character"
    )
    repository = AssetRepository(tmp_path / "Series")
    thumbnail = QImage(256, 256, QImage.Format_ARGB32_Premultiplied)
    thumbnail.fill(Qt.transparent)
    repository.create(manifest, asset_tiles, thumbnail)
    original_root = repository.asset_root(manifest.asset_id)
    repository.rename(manifest.asset_id, "Hero")
    assert repository.asset_root(manifest.asset_id) == original_root
    assert repository.list_assets()[0].name == "Hero"
    with pytest.raises(ValueError):
        second, second_tiles = extract_asset(
            chapter, tiles, "layer", layer.layer_id, "Hero"
        )
        repository.create(second, second_tiles, thumbnail)
    assert AssetRepository(tmp_path / "Other Series").list_assets() == []


def test_asset_repository_replace_preserves_identity_and_replaces_contents(
    qapp, tmp_path,
):
    chapter, layer, raster, _drawing, _fill, tiles = _asset_source()
    repository = AssetRepository(tmp_path / "Series")
    original, original_tiles = extract_asset(
        chapter, tiles, "layer", layer.layer_id, "Hero"
    )
    original_thumbnail = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    original_thumbnail.fill(Qt.transparent)
    repository.create(original, original_tiles, original_thumbnail)
    original_root = repository.asset_root(original.asset_id)
    original_bounds = repository.load(original.asset_id)[1].content_bounds(
        raster.object_id
    )

    chapter.layers[layer.layer_id].name = "Replacement"
    tiles.paint_dab(raster.object_id, QPointF(190, 140), 18, QColor("#2266ff"))
    replacement, replacement_tiles = extract_asset(
        chapter, tiles, "layer", layer.layer_id, " hero "
    )
    replacement_thumbnail = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    replacement_thumbnail.fill(QColor("#2266ff"))
    result = repository.replace(
        original.asset_id, replacement, replacement_tiles, replacement_thumbnail
    )

    assert result.asset_id == original.asset_id
    assert result.name == "Hero"
    assert repository.asset_root(result.asset_id) == original_root
    assert len(repository.list_assets()) == 1
    loaded, loaded_tiles = repository.load(result.asset_id)
    assert loaded.document.layers[loaded.root_id].name == "Replacement"
    assert loaded_tiles.content_bounds(raster.object_id) != original_bounds
    saved_thumbnail = QImage(str(repository.thumbnail_path(result.asset_id)))
    assert saved_thumbnail.pixelColor(0, 0) == QColor("#2266ff")
    assert repository.find_by_name("  hErO  ").asset_id == original.asset_id


def test_copy_as_asset_duplicate_decline_leaves_existing_asset_unchanged(
    qapp, tmp_path, monkeypatch,
):
    window, _repository, existing = _window_with_asset(tmp_path)
    try:
        before, _tiles = window.active_session.context.assets.load(existing.asset_id)
        source = next(
            layer for layer in window.chapter.layers.values() if not layer.is_page
        )
        source.name = "Replacement"
        questions = []
        warnings = []
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, **kwargs: ("  hErO  ", True)
        )
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: questions.append(args) or QMessageBox.No,
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *args, **kwargs: warnings.append(args) or QMessageBox.Ok,
        )

        window._copy_selected_as_asset("layer", source.layer_id)

        after, _tiles = window.active_session.context.assets.load(existing.asset_id)
        assert after.to_dict() == before.to_dict()
        assert len(window.active_session.context.assets.list_assets()) == 1
        assert len(questions) == 1
        assert questions[0][1] == "Replace asset?"
        assert "Asset Library version" in questions[0][2]
        assert questions[0][4] == QMessageBox.No
        assert warnings == []
    finally:
        _dispose_window(window)


def test_copy_as_asset_reloads_clean_active_asset_tab_and_clears_undo(
    qapp, tmp_path, monkeypatch,
):
    window, _repository, existing = _window_with_asset(tmp_path)
    try:
        window._open_asset(existing.asset_id)
        session = window.active_session
        root = window.chapter.layers[session.asset_manifest.root_id]
        before = window.chapter.to_dict()
        root.name = "Edited asset"
        window.canvas.push_model_change(before, window.chapter.to_dict(), "Edit asset")
        assert window.canvas.command_stack.can_undo
        assert window.save()
        assert not session.dirty

        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, **kwargs: ("hero", True)
        )
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes
        )
        window._copy_selected_as_asset(
            session.asset_manifest.root_kind, session.asset_manifest.root_id
        )

        assert session is window.active_session
        assert session.asset_manifest.asset_id == existing.asset_id
        assert session.chapter is window.chapter is window.canvas.chapter
        assert session.chapter.layers[session.asset_manifest.root_id].name == "Edited asset"
        assert not session.dirty
        assert not window._dirty
        assert not window.canvas.command_stack.can_undo
        assert not window.canvas.command_stack.can_redo
        assert window.canvas.selected_id == session.asset_manifest.root_id
        assert window.statusBar().currentMessage() == "Replaced asset Hero"
    finally:
        _dispose_window(window)


def test_copy_as_asset_dirty_tab_requires_discard_before_replacement(
    qapp, tmp_path, monkeypatch,
):
    window, repository, existing = _window_with_asset(tmp_path)
    try:
        window._open_asset(existing.asset_id)
        asset_session = window.active_session
        asset_root = asset_session.chapter.layers[asset_session.asset_manifest.root_id]
        asset_root.name = "Unsaved asset edit"
        window._mark_dirty(None)
        asset_tab = window.project_tabs.currentIndex()
        series_tab = window._tab_index_for_key(window._series_session_key(repository.root))
        window.project_tabs.setCurrentIndex(series_tab)
        source = next(
            layer for layer in window.chapter.layers.values() if not layer.is_page
        )
        source.name = "Replacement"
        stored_before, _tiles = asset_session.context.assets.load(existing.asset_id)
        answers = [QMessageBox.Yes, QMessageBox.No]
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, **kwargs: (" HERO ", True)
        )
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: answers.pop(0)
        )

        window._copy_selected_as_asset("layer", source.layer_id)

        stored_after, _tiles = asset_session.context.assets.load(existing.asset_id)
        assert stored_after.to_dict() == stored_before.to_dict()
        assert asset_session.dirty
        assert asset_session.chapter.layers[asset_session.asset_manifest.root_id].name == (
            "Unsaved asset edit"
        )

        answers.extend([QMessageBox.Yes, QMessageBox.Yes])
        window._copy_selected_as_asset("layer", source.layer_id)

        assert window._tab_index_for_key(asset_session.key) == asset_tab
        assert asset_session.asset_manifest.asset_id == existing.asset_id
        assert asset_session.chapter.layers[asset_session.asset_manifest.root_id].name == (
            "Replacement"
        )
        assert asset_session.canvas_state is None
        assert not asset_session.dirty
        window.project_tabs.setCurrentIndex(asset_tab)
        assert window.active_session is asset_session
        assert not window.canvas.command_stack.can_undo
    finally:
        _dispose_window(window)


def test_copy_as_asset_replace_error_preserves_existing_asset(
    qapp, tmp_path, monkeypatch,
):
    window, _repository, existing = _window_with_asset(tmp_path)
    try:
        assets = window.active_session.context.assets
        before, _tiles = assets.load(existing.asset_id)
        source = next(
            layer for layer in window.chapter.layers.values() if not layer.is_page
        )
        source.name = "Replacement"
        warnings = []
        monkeypatch.setattr(
            QInputDialog, "getText", lambda *args, **kwargs: ("Hero", True)
        )
        monkeypatch.setattr(
            QMessageBox, "question", lambda *args, **kwargs: QMessageBox.Yes
        )
        monkeypatch.setattr(
            assets, "replace",
            lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )
        monkeypatch.setattr(
            QMessageBox, "warning",
            lambda *args, **kwargs: warnings.append(args) or QMessageBox.Ok,
        )

        window._copy_selected_as_asset("layer", source.layer_id)

        after, _tiles = assets.load(existing.asset_id)
        assert after.to_dict() == before.to_dict()
        assert len(warnings) == 1
        assert warnings[0][1] == "Unable to create asset"
        assert warnings[0][2] == "disk full"
    finally:
        _dispose_window(window)


def test_project_tabs_reuse_paths_and_restore_full_canvas_state(qapp, tmp_path):
    first_repository = SeriesRepository(tmp_path / "One")
    first = first_repository.create("One")
    first_repository.create_chapter(first, "First")
    second_repository = SeriesRepository(tmp_path / "Two")
    second = second_repository.create("Two")
    second_repository.create_chapter(second, "Second")

    window = MainWindow()
    try:
        assert window.open_series(first_repository.root)
        first_chapter = window.chapter
        window.canvas.center_x = 123.0
        window.canvas.scale = 1.25
        before = first_chapter.to_dict()
        first_chapter.name = "Unsaved First"
        after = first_chapter.to_dict()
        window.canvas.push_model_change(before, after, "Rename chapter")
        cache = window.canvas._vector_render_cache
        cache[("session-sentinel",)] = (QImage(), None)
        assert window.open_series(second_repository.root)
        assert window.project_tabs.count() == 2
        assert window.open_series(first_repository.root)
        assert window.project_tabs.count() == 2
        assert window.chapter is first_chapter
        assert window.chapter.name == "Unsaved First"
        assert window.canvas.center_x == 123.0
        assert window.canvas.scale == 1.25
        assert window.canvas._vector_render_cache is cache
        assert window.canvas.command_stack.can_undo
        assert window._dirty is True
        assert window.project_tabs.tabText(window.project_tabs.currentIndex()).endswith("*")
        window.canvas.command_stack.undo()
        assert window.chapter.name == "First"
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.deleteLater()


def test_closing_last_project_tab_cancels_shape_draft_and_queued_move(
    qapp, tmp_path, monkeypatch,
):
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Series")
    repository.create_chapter(series, "Chapter")
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        window.show()
        qapp.processEvents()
        window.canvas.set_tool(ToolKind.SHAPE_CREATE)
        window.canvas._creation_nodes = [
            PathNode(x=100, y=100), PathNode(x=300, y=100),
            PathNode(x=250, y=300),
        ]
        window.canvas._creation_active_control = "draft_node"
        monkeypatch.setattr(
            QMessageBox, "question",
            lambda *args, **kwargs: QMessageBox.Discard,
        )
        window._close_project_tab(window.project_tabs.currentIndex())
        assert window.project_tabs.count() == 0
        assert window.canvas.chapter is None
        assert not window.canvas._creation_nodes
        QTest.mouseMove(window.canvas, window.canvas.rect().center())
        QTest.mouseRelease(
            window.canvas, Qt.LeftButton,
            pos=window.canvas.rect().center(),
        )
        window.canvas._tool_move(QPointF(20, 20), 1.0)
        window.canvas._tool_release()
        assert window.canvas.chapter is None
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.close()


def test_asset_tab_hides_container_and_save_refreshes_thumbnail(qapp, tmp_path):
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Series")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    source_layer = next(layer for layer in chapter.layers.values() if not layer.is_page)
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", source_layer.layer_id, "Drawing"
    )

    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        thumbnail = window.canvas.render_asset_thumbnail(manifest, asset_tiles)
        window.active_session.context.assets.create(manifest, asset_tiles, thumbnail)
        window.asset_library.refresh()
        window._open_asset(manifest.asset_id)
        assert window.active_session.kind == "asset"
        assert window.hierarchy_model.rowCount() == 1
        assert window.ribbon.is_page_visible("asset_library")
        window.chapter.layers[manifest.root_id].name = "Edited"
        window._mark_dirty(None)
        assert window.save()
        assert window.active_session.context.assets.thumbnail_path(
            manifest.asset_id
        ).is_file()
        loaded, _ = window.active_session.context.assets.load(manifest.asset_id)
        assert loaded.document.layers[manifest.root_id].name == "Edited"
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.deleteLater()


def test_canvas_asset_drag_previews_without_mutation_then_drops_once(qapp, tmp_path):
    repository = SeriesRepository(tmp_path / "Series")
    series = repository.create("Series")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    source_layer = next(layer for layer in chapter.layers.values() if not layer.is_page)
    manifest, asset_tiles = extract_asset(
        chapter, tiles, "layer", source_layer.layer_id, "Drawing"
    )
    window = MainWindow()
    try:
        assert window.open_series(repository.root)
        thumbnail = window.canvas.render_asset_thumbnail(manifest, asset_tiles)
        window.active_session.context.assets.create(manifest, asset_tiles, thumbnail)
        window.canvas.resize(800, 800)

        mime = QMimeData()
        mime.setData(
            ASSET_MIME,
            json.dumps({"asset_id": manifest.asset_id}).encode("utf-8"),
        )

        class Event:
            def __init__(self, position):
                self._position = position
                self.accepted = False

            def mimeData(self):
                return mime

            def position(self):
                return self._position

            def acceptProposedAction(self):
                self.accepted = True

            def accept(self):
                self.accepted = True

            def ignore(self):
                self.accepted = False

        point = window.canvas.document_to_widget(QPointF(540, 540))
        before = window.chapter.to_dict()
        event = Event(point)
        window.canvas.dragEnterEvent(event)
        assert event.accepted
        assert window.canvas._asset_drag_valid
        assert window.chapter.to_dict() == before
        window.canvas.dragMoveEvent(event)
        assert window.chapter.to_dict() == before
        window.canvas.dropEvent(event)
        assert event.accepted
        assert window.chapter.to_dict() != before
        assert window.canvas.command_stack.can_undo
        window.canvas.command_stack.undo()
        assert window.chapter.to_dict() == before
    finally:
        for session in window.sessions.values():
            session.dirty = False
        window._dirty = False
        window.deleteLater()


def test_asset_drag_ghost_is_clipped_like_the_committed_drop(qapp):
    manifest, asset_tiles = _solid_asset()
    chapter = ChapterDocument(
        name="Target", height=500, background="#00000000"
    )
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 500, 500),
        style=ShapeStyle(),
    )
    target = chapter.add_layer(
        page.layer_id, "Mask", BoundGeometry.rectangle(200, 100, 200, 250)
    )
    target_tiles = TileStore()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, target_tiles)
    canvas._asset_drag_manifest = manifest
    canvas._asset_drag_tiles = asset_tiles
    canvas._asset_drag_image = canvas._render_entity_crop(
        manifest.document, asset_tiles, manifest.root_kind, manifest.root_id
    )
    canvas._asset_drag_parent_id = target.layer_id
    canvas._asset_drag_world = QPointF(200, 200)
    canvas._asset_drag_valid = True

    before_document = chapter.to_dict()
    before_tiles = dict(target_tiles._tiles)
    preview = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    preview.fill(Qt.transparent)
    painter = QPainter(preview)
    canvas._draw_asset_drag_preview(painter)
    painter.end()

    assert preview.pixelColor(150, 200).alpha() == 0
    assert preview.pixelColor(250, 200).red() > 180
    assert preview.pixelColor(250, 200).alpha() in range(175, 181)
    assert any(
        (
            preview.pixelColor(x, y).blue() > 220
            and preview.pixelColor(x, y).blue()
            > preview.pixelColor(x, y).red() + 80
        )
        for x in range(197, 200)
        for y in range(115, 335)
    )
    assert chapter.to_dict() == before_document
    assert target_tiles._tiles == before_tiles

    instantiate_asset(
        manifest, asset_tiles, chapter, target_tiles,
        target.layer_id, 200, 200,
    )
    committed = QImage(
        chapter.width, chapter.height, QImage.Format_ARGB32_Premultiplied
    )
    committed.fill(Qt.transparent)
    canvas.render_preview(committed)
    assert committed.pixelColor(150, 200).alpha() == 0
    assert committed.pixelColor(250, 200).red() > 180
    assert committed.pixelColor(250, 200).alpha() > 250


def test_asset_drag_mask_respects_nested_compound_and_bypass_rules(qapp):
    manifest, asset_tiles = _solid_asset()
    chapter = ChapterDocument(height=500, background="#00000000")
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 500, 500),
        style=ShapeStyle(),
    )
    outer = chapter.add_layer(
        page.layer_id, "Outer", BoundGeometry.rectangle(100, 100, 250, 250)
    )
    inner = chapter.add_layer(
        outer.layer_id, "Inner", BoundGeometry.rectangle(200, 50, 200, 250)
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas._asset_drag_manifest = manifest
    canvas._asset_drag_tiles = asset_tiles

    mask = canvas._asset_drag_clip_path(inner.layer_id)
    assert mask is not None
    assert mask.contains(QPointF(225, 150))
    assert not mask.contains(QPointF(150, 150))
    assert not mask.contains(QPointF(225, 75))

    inner.ignore_parent_mask = True
    canvas._asset_drag_clip_cache.clear()
    mask = canvas._asset_drag_clip_path(inner.layer_id)
    assert mask is not None and mask.contains(QPointF(225, 75))

    inner.ignore_parent_mask = False
    manifest.document.layers[manifest.root_id].ignore_parent_mask = True
    canvas._asset_drag_clip_cache.clear()
    mask = canvas._asset_drag_clip_path(inner.layer_id)
    assert mask is not None and mask.contains(QPointF(150, 150))

    manifest.document.layers[manifest.root_id].ignore_parent_mask = False
    compound = chapter.add_layer(
        page.layer_id, "Compound", BoundGeometry.rectangle(100, 100, 100, 100)
    )
    compound.compound_enabled = True
    addition = chapter.add_layer(
        compound.layer_id, "Add", BoundGeometry.rectangle(200, 100, 100, 100)
    )
    addition.compound_operation = "add"
    subtraction = chapter.add_layer(
        compound.layer_id, "Subtract", BoundGeometry.rectangle(120, 120, 30, 30)
    )
    subtraction.compound_operation = "subtract"
    canvas._clear_compound_path_cache()
    mask = canvas._asset_drag_clip_path(compound.layer_id)
    assert mask is not None
    assert mask.contains(QPointF(250, 150))
    assert not mask.contains(QPointF(135, 135))

    canvas._asset_drag_world = QPointF(250, 150)
    canvas._asset_drag_clip_cache.clear()
    assert not canvas.layer_effective_path(compound.layer_id).contains(
        QPointF(325, 150)
    )
    mask = canvas._asset_drag_clip_path(compound.layer_id)
    assert mask is not None and mask.contains(QPointF(325, 150))

    manifest.document.layers[manifest.root_id].compound_operation = "subtract"
    canvas._asset_drag_world = QPointF(175, 175)
    canvas._asset_drag_clip_cache.clear()
    assert canvas.layer_effective_path(compound.layer_id).contains(
        QPointF(175, 175)
    )
    mask = canvas._asset_drag_clip_path(compound.layer_id)
    assert mask is not None and not mask.contains(QPointF(175, 175))
    manifest.document.layers[manifest.root_id].compound_operation = "add"

    circle = chapter.add_layer(
        page.layer_id, "Circle", BoundGeometry.circle(420, 300, 60)
    )
    canvas._asset_drag_clip_cache.clear()
    mask = canvas._asset_drag_clip_path(circle.layer_id)
    assert mask is not None and mask.contains(QPointF(420, 300))
    assert not mask.contains(QPointF(350, 300))

    open_shape = chapter.add_layer(
        page.layer_id, "Open",
        BoundGeometry.path([
            PathNode(x=100, y=420), PathNode(x=300, y=420),
        ]),
        layer_kind="open_shape", style=ShapeStyle(base_thickness=40),
    )
    canvas._asset_drag_clip_cache.clear()
    mask = canvas._asset_drag_clip_path(open_shape.layer_id)
    assert mask is not None and mask.contains(QPointF(200, 420))
    assert not mask.contains(QPointF(200, 460))

    gradient_source = ChapterDocument(height=400)
    gradient_page = gradient_source.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 400, 400)
    )
    gradient_parent = gradient_source.add_layer(
        gradient_page.layer_id, "Parent",
        BoundGeometry.rectangle(0, 0, 300, 300),
    )
    gradient = gradient_source.add_object(
        gradient_parent.layer_id,
        ColorFillGradientObject(field_type="radial"),
    )
    gradient.radial_field.reverse_direction = True
    outward, outward_tiles = extract_asset(
        gradient_source, TileStore(), "object", gradient.object_id, "Outward"
    )
    canvas._asset_drag_manifest = outward
    canvas._asset_drag_tiles = outward_tiles
    canvas._asset_drag_clip_cache.clear()
    mask = canvas._asset_drag_clip_path(inner.layer_id)
    assert mask is not None and mask.contains(QPointF(150, 150))

    outer.visible = False
    canvas._asset_drag_clip_cache.clear()
    assert not canvas._asset_parent_accepts(inner.layer_id, outward)
    canvas.active_page_id = page.layer_id
    assert canvas._asset_target_parent(
        QPointF(50, 50), outward
    ) == page.layer_id
    canvas._clear_asset_drag_preview()
    assert canvas._asset_drag_manifest is None
    assert not canvas._asset_drag_clip_cache
