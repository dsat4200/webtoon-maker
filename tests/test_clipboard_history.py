from __future__ import annotations

from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QMimeData, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainterPath, QTransform

from comic_editor.core.assets import entity_visual_bounds
from comic_editor.core.images import ImageStore
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ColorFillGradientObject, ImageObject,
    ParameterMaskBinding, RasterObject, TextObject, ToneMask, VectorDrawingObject,
    VectorStroke, VectorStrokePoint,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import RasterSelectionClipboard, VectorSelectionClipboard
from comic_editor.ui.clipboard_history import (
    ClipboardImageHistory, HistoryEntry, capture_object, drawing_at, paste_object,
)
from comic_editor.ui.main_window import MainWindow


def _png(width=40, height=20, color="red"):
    image = QImage(width, height, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    image.save(buffer, "PNG")
    return bytes(payload)


def _window():
    window = MainWindow()
    chapter = ChapterDocument(height=600)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 600, 600))
    layer = chapter.add_layer(page.layer_id, "Container", BoundGeometry.rectangle(100, 100, 200, 200))
    window._set_chapter(chapter, TileStore(), images=ImageStore())
    return window, chapter, page, layer


def test_history_bounds_deduplicates_and_rejects_oversized_items(qapp):
    history = ClipboardImageHistory(max_items=2, max_bytes=100)
    for label in "abc":
        history.add(HistoryEntry("image", label, [], QImage(), 40, label))
    assert [entry.label for entry in history.entries] == ["c", "b"]
    history.add(history.entries[-1])
    assert [entry.label for entry in history.entries] == ["b", "c"]
    assert not history.add(HistoryEntry("image", "large", [], QImage(), 101))
    assert history.byte_size == 80


def test_external_paste_uses_cursor_parent_order_and_selection(qapp, monkeypatch):
    window, chapter, page, layer = _window()
    source = [("sample.png", "image/png", _png())]
    world = QPointF(440, 360)
    monkeypatch.setattr("comic_editor.ui.main_window.cursor_world", lambda canvas: world)
    try:
        window.canvas.set_selection("layer", layer.layer_id)
        assert window._paste_image(source)
        first = chapter.objects[window.canvas.selected_id]
        assert first.parent_layer_id == layer.layer_id
        assert first.placement_mode == "free"
        assert entity_visual_bounds(chapter, window.canvas.tiles, "object", first.object_id).center() == world
        assert window._paste_image(source)
        second = chapter.objects[window.canvas.selected_id]
        ids = [child.entity_id for child in layer.children]
        assert ids.index(second.object_id) + 1 == ids.index(first.object_id)
        assert len(window.clipboard_image_history.entries) == 1
    finally:
        window.deleteLater()


def test_history_captures_hotkey_position_and_pastes_only_once(qapp, monkeypatch):
    window, chapter, page, layer = _window()
    initial = QPointF(250, 170)
    current = [initial]
    calls = []
    monkeypatch.setattr("comic_editor.ui.main_window.cursor_world", lambda canvas: current[0])
    monkeypatch.setattr(window, "_paste_image", lambda payload, world: calls.append(QPointF(world)))
    try:
        window.clipboard_image_history.add_images([("copied.png", "image/png", _png())])
        assert window._show_clipboard_image_history()
        popup = window._clipboard_history_popup
        assert popup.list.iconSize().width() == popup.list.iconSize().height()
        current[0] = QPointF(900, 900)
        item = popup.list.item(0)
        popup.list.itemClicked.emit(item)
        popup.list.itemActivated.emit(item)
        assert calls == [initial]
    finally:
        window.deleteLater()


def test_drawing_payload_translation_preserves_original_and_editable_vectors(qapp):
    path = QPainterPath()
    path.addRect(QRectF(10, 20, 40, 60))
    raster = RasterSelectionClipboard({}, path, QTransform.fromTranslate(50, 70), "Raster", 256)
    destination = QPointF(230, 290)
    moved = drawing_at(raster, destination)
    assert moved.source_to_world.map(path.boundingRect().center()) == destination
    assert raster.source_to_world.map(path.boundingRect().center()) == QPointF(80, 120)
    strokes = [VectorStroke(points=[VectorStrokePoint(x=20, y=40), VectorStrokePoint(x=60, y=80)])]
    vector = VectorSelectionClipboard(strokes, QTransform(), "Vector")
    moved = drawing_at(vector, destination)
    assert moved.source_to_world.map(QPointF(40, 60)) == destination
    assert moved.strokes is vector.strokes


def test_object_subtree_pastes_across_scenes_with_independent_resources_and_undo(qapp):
    window, source, page, layer = _window()
    raster = source.add_object(layer.layer_id, RasterObject(name="Paint"))
    vector = source.add_object(layer.layer_id, VectorDrawingObject(name="Ink", strokes=[
        VectorStroke(points=[VectorStrokePoint(x=150, y=150), VectorStrokePoint(x=180, y=160)])]))
    window.canvas.tiles.paint_dab(raster.object_id, QPointF(150, 150), 15, QColor("red"))
    image_id = window.canvas.place_image_sources([("original.png", "image/png", _png())], layer.layer_id, QPointF(180, 180))[0]
    mask = ToneMask(contributors=[("object", raster.object_id)])
    source.masks[mask.mask_id] = mask
    vector.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    modifier = BlurModifier(strength=2)
    source.modifiers[modifier.modifier_id] = modifier
    vector.modifier_ids = [modifier.modifier_id]
    try:
        assert window._copy_outliner_object("layer", layer.layer_id)
        payload = window._object_clipboard
        target = ChapterDocument(height=600)
        target_page = target.add_page()
        window._set_chapter(target, TileStore(), images=ImageStore())
        window.canvas.set_selection("layer", target_page.layer_id)
        assert window._paste_outliner_object(world=QPointF(300, 300)), window.statusBar().currentMessage()
        root_id = window.canvas.selected_id
        assert root_id != layer.layer_id
        assert len(target.objects) == 3
        copied_vector = next(obj for obj in target.objects.values() if isinstance(obj, VectorDrawingObject))
        copied_raster = next(obj for obj in target.objects.values() if isinstance(obj, RasterObject))
        copied_image = next(obj for obj in target.objects.values() if isinstance(obj, ImageObject))
        assert copied_vector.strokes[0].stroke_id != vector.strokes[0].stroke_id
        assert copied_vector.modifier_ids[0] != modifier.modifier_id
        copied_mask = target.masks[copied_vector.opacity_mask.mask_id]
        assert copied_mask.mask_id != mask.mask_id
        assert copied_mask.contributors == [("object", copied_raster.object_id)]
        assert window.canvas.images.source(copied_image.object_id).data == _png()
        assert window.canvas.tiles.content_bounds(copied_raster.object_id) is not None
        window.canvas.command_stack.undo()
        assert not window.canvas.chapter.objects
        assert not window.canvas.images.snapshot()
        window.canvas.command_stack.redo()
        assert window.canvas.selected_id == root_id
        assert len(window.canvas.chapter.objects) == 3
        # Editing a pasted source never changes the captured clipboard resource.
        window.canvas.images.put(copied_image.object_id, "different.png", _png(color="blue"))
        assert payload.images.source(image_id).data == _png()
    finally:
        window.deleteLater()


def test_duplicate_container_is_sibling_with_same_world_bounds(qapp):
    window, chapter, page, layer = _window()
    try:
        before = entity_visual_bounds(chapter, window.canvas.tiles, "layer", layer.layer_id, include_effects=True)
        assert window._duplicate_outliner_object("layer", layer.layer_id), window.statusBar().currentMessage()
        duplicate = chapter.layers[window.canvas.selected_id]
        assert duplicate.parent_id == page.layer_id
        assert [child.entity_id for child in page.children][:2] == [duplicate.layer_id, layer.layer_id]
        after = entity_visual_bounds(chapter, window.canvas.tiles, "layer", duplicate.layer_id, include_effects=True)
        assert before == after
    finally:
        window.deleteLater()


def test_drawing_history_can_paste_into_selected_container(qapp):
    window, chapter, page, layer = _window()
    payload = VectorSelectionClipboard([VectorStroke(points=[
        VectorStrokePoint(x=20, y=30), VectorStrokePoint(x=40, y=50)])], QTransform(), "Vector")
    try:
        window.canvas.set_selection("layer", layer.layer_id)
        assert window._paste_drawing_payload(payload, QPointF(190, 210))
        obj = chapter.objects[window.canvas.selected_id]
        assert isinstance(obj, VectorDrawingObject)
        assert obj.parent_layer_id == layer.layer_id
        assert [point.x for point in obj.strokes[0].points] == [180, 200]
        assert window.clipboard_image_history.entries[0].kind == "drawing"
    finally:
        window.deleteLater()


def test_duplicate_root_page_preserves_children_and_world_translation(qapp):
    window, chapter, page, layer = _window()
    page.translate_x, page.translate_y = 30, 80
    raster = chapter.add_object(layer.layer_id, RasterObject(name="Paint"))
    window.canvas.tiles.paint_dab(raster.object_id, QPointF(150, 150), 10, QColor("red"))
    try:
        before = entity_visual_bounds(chapter, window.canvas.tiles, "layer", page.layer_id, include_effects=True)
        assert window._duplicate_outliner_object("layer", page.layer_id), window.statusBar().currentMessage()
        duplicate = chapter.layers[window.canvas.selected_id]
        assert duplicate.is_page and duplicate.parent_id is None
        assert chapter.root_page_ids == [duplicate.layer_id, page.layer_id]
        assert duplicate.children[0].entity_id != layer.layer_id
        after = entity_visual_bounds(chapter, window.canvas.tiles, "layer", duplicate.layer_id, include_effects=True)
        assert before == after
        chapter.validate()
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.root_page_ids == [page.layer_id]
        window.canvas.command_stack.redo()
        assert window.canvas.chapter.root_page_ids == [duplicate.layer_id, page.layer_id]
    finally:
        window.deleteLater()


def test_copied_external_images_enter_history_and_decode_is_cached(qapp, monkeypatch):
    window, chapter, page, layer = _window()
    try:
        mime = QMimeData()
        mime.setImageData(QImage.fromData(_png()))
        qapp.clipboard().setMimeData(mime)
        assert window._clipboard_image_sources()
        assert window.clipboard_image_history.entries[0].kind == "image"
        monkeypatch.setattr(window.canvas, "_external_image_entries", lambda mime: (_ for _ in ()).throw(AssertionError("Repeated MIME decode")))
        assert window._clipboard_image_sources()
        assert window._clipboard_image_sources()
    finally:
        monkeypatch.undo()
        qapp.clipboard().clear()
        window.deleteLater()


def test_object_history_releases_large_decoded_image_cache(qapp):
    window, chapter, page, layer = _window()
    data = _png(2048, 2048)
    try:
        identifier = window.canvas.place_image_sources(
            [("solid.png", "image/png", data)], layer.layer_id, QPointF(200, 200))[0]
        assert window._copy_outliner_object("object", identifier)
        entry = window.clipboard_image_history.entries[0]
        assert entry.kind == "object"
        assert entry.byte_size < 1024 * 1024
        assert not entry.payload.images._decoded
        assert entry.payload.images.source(identifier).data == data
    finally:
        window.deleteLater()


def test_downloaded_clipboard_paste_keeps_target_and_ignores_scene_switch(qapp):
    window, chapter, page, layer = _window()
    sources = [("download.png", "image/png", _png())]
    original = QPointF(220, 260)
    try:
        window._pending_clipboard_paste = (chapter, layer.layer_id, 0, original)
        window.canvas.set_selection("layer", page.layer_id)
        window._clipboard_images_resolved(sources)
        obj = chapter.objects[window.canvas.selected_id]
        assert obj.parent_layer_id == layer.layer_id
        assert entity_visual_bounds(chapter, window.canvas.tiles, "object", obj.object_id).center() == original
        window._pending_clipboard_paste = (chapter, layer.layer_id, 0, original)
        new_scene = ChapterDocument()
        new_scene.add_page()
        window._set_chapter(new_scene, TileStore(), images=ImageStore())
        window._clipboard_images_resolved(sources)
        assert not new_scene.objects
        assert window._pending_clipboard_paste is None
    finally:
        window.deleteLater()


def test_copy_masked_object_carries_editable_sibling_dependencies(qapp):
    window, chapter, page, layer = _window()
    contributor = chapter.add_object(layer.layer_id, RasterObject(name="Mask source"))
    target = chapter.add_object(layer.layer_id, RasterObject(name="Masked paint"))
    window.canvas.tiles.paint_dab(contributor.object_id, QPointF(150, 150), 15, QColor("white"))
    window.canvas.tiles.paint_dab(target.object_id, QPointF(150, 150), 30, QColor("red"))
    mask = ToneMask(contributors=[("object", contributor.object_id)])
    chapter.masks[mask.mask_id] = mask
    target.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    try:
        assert window._copy_outliner_object("object", target.object_id), window.statusBar().currentMessage()
        assert mask.contributors == [("object", contributor.object_id)]
        assert len(window._object_clipboard.dependencies) == 1
        scene = ChapterDocument()
        destination = scene.add_page()
        window._set_chapter(scene, TileStore(), images=ImageStore())
        window.canvas.set_selection("layer", destination.layer_id)
        assert window._paste_outliner_object(world=QPointF(300, 300)), window.statusBar().currentMessage()
        pasted = scene.objects[window.canvas.selected_id]
        copied_mask = scene.masks[pasted.opacity_mask.mask_id]
        dependency_id = copied_mask.contributors[0][1]
        dependency = scene.objects[dependency_id]
        assert dependency.mask_only
        assert dependency_id != contributor.object_id
        assert window.canvas.tiles.content_bounds(dependency_id) is not None
        assert len(scene.objects) == 2
        scene.validate()
        window.canvas.command_stack.undo()
        assert not window.canvas.chapter.objects
        assert not window.canvas.chapter.masks
        window.canvas.command_stack.redo()
        assert len(window.canvas.chapter.objects) == 2
        assert len(window.canvas.chapter.masks) == 1
    finally:
        window.deleteLater()


def test_drawing_history_handles_text_container_and_empty_selection(qapp):
    window, chapter, page, layer = _window()
    text_container = chapter.add_layer(page.layer_id, "Text", layer_kind="text_container")
    text = chapter.add_object(text_container.layer_id, TextObject(text="Caption"))
    payload = VectorSelectionClipboard([VectorStroke(points=[
        VectorStrokePoint(x=20, y=30), VectorStrokePoint(x=40, y=50)])], QTransform(), "Vector")
    try:
        window.canvas.set_selection("object", text.object_id)
        assert window._paste_drawing_payload(payload, QPointF(180, 210))
        copied = chapter.objects[window.canvas.selected_id]
        assert copied.parent_layer_id == page.layer_id
        siblings = [child.entity_id for child in page.children]
        assert siblings.index(copied.object_id) + 1 == siblings.index(text_container.layer_id)
        window.canvas.selected_kind = ""
        window.canvas.selected_id = ""
        window.canvas.selected_object_id = ""
        assert window._paste_drawing_payload(payload, QPointF(200, 230))
        assert chapter.objects[window.canvas.selected_id].parent_layer_id == page.layer_id
    finally:
        window.deleteLater()


def test_pasted_object_translates_mask_paint_and_gradient_with_content(qapp):
    window, chapter, page, layer = _window()
    raster = chapter.add_object(page.layer_id, RasterObject(name="Masked"))
    window.canvas.tiles.paint_dab(raster.object_id, QPointF(100, 100), 30, QColor("red"))
    mask = ToneMask(gradient=ColorFillGradientObject())
    mask.gradient.radial_field.origin_x = 90
    mask.gradient.radial_field.origin_y = 110
    chapter.masks[mask.mask_id] = mask
    raster.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    window.canvas.tiles.paint_dab(mask.mask_id, QPointF(100, 100), 40, QColor("white"))
    try:
        payload = capture_object(window.canvas, "object", raster.object_id)
        offset = QPointF(200, 220)
        pasted_id = paste_object(window.canvas, payload, page.layer_id, payload.original_center + offset)
        pasted = chapter.objects[pasted_id]
        pasted_mask_id = pasted.opacity_mask.mask_id
        assert window.canvas.tiles.content_bounds(pasted_mask_id).center() == window.canvas.tiles.content_bounds(mask.mask_id).center() + offset
        gradient = chapter.masks[pasted_mask_id].gradient
        assert gradient.radial_field.origin_x == 290
        assert gradient.radial_field.origin_y == 330
        field = window.canvas.render_tone_mask_field(pasted_mask_id, 600, 600, QTransform(), QRectF(0, 0, 600, 600))
        assert field[320, 300] > .99
        assert mask.gradient.radial_field.origin_x == 90
    finally:
        window.deleteLater()
