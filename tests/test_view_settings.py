from __future__ import annotations

import pytest
from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage
from PySide6.QtWidgets import QFileDialog

from comic_editor.core.images import ImageStore
from comic_editor.core.models import BoundGeometry, ChapterDocument, ImageObject, RasterObject
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow


def scene(qapp, *, raster=False):
    chapter = ChapterDocument(width=160, height=140, document_kind="image")
    page = chapter.add_page(bound=BoundGeometry.rectangle(0, 0, 100, 100))
    page.translate_x, page.translate_y = 30, 20
    page.fill_color, page.border_width = None, 0
    layer = chapter.add_layer(page.layer_id, "Nested", BoundGeometry.rectangle(-70, 0, 160, 100))
    layer.translate_x = 10
    layer.fill_color, layer.border_width = None, 0
    images, tiles = ImageStore(), TileStore()
    if raster:
        obj = chapter.add_object(layer.layer_id, RasterObject(x=-60, y=40))
        tiles.paint_dab(obj.object_id, QPointF(50, 10), 20, QColor("red"),
                        square=True, antialias=False)
        # A broad opaque strip crosses both the page and chapter's left edges.
        for x in range(0, 100, 10):
            tiles.paint_dab(obj.object_id, QPointF(x, 10), 20, QColor("red"),
                            square=True, antialias=False)
    else:
        obj = chapter.add_object(layer.layer_id, ImageObject(
            x=-60, y=40, pixel_width=100, pixel_height=20))
        image = QImage(100, 20, QImage.Format.Format_ARGB32_Premultiplied)
        image.fill(QColor("red"))
        images.put_decoded(obj.object_id, "red.png", b"", image)
    canvas = CanvasWidget(EditorSettings(grid_overlay_visible=False))
    canvas.resize(320, 240)
    canvas.set_document(chapter, tiles, images)
    canvas.scale, canvas.center_x, canvas.center_y = 1.0, 80.0, 70.0
    return canvas, chapter, obj


def pixel(canvas, point):
    position = canvas.camera_transform().map(QPointF(*point)).toPoint()
    return canvas._scene_cache.pixelColor(position)


@pytest.mark.parametrize("raster", [False, True])
def test_overflow_fades_beyond_page_and_chapter_preserving_nested_transforms(qapp, raster):
    canvas, chapter, _obj = scene(qapp, raster=raster)
    try:
        for opacity in (0, 0.5, 1):
            canvas.set_view_overflow(opacity)
            canvas._ensure_scene_cache()
            inside = pixel(canvas, (50, 70))
            assert inside.red() == 255 and inside.green() == 0
            for point in ((-10, 70), (20, 70)):
                outside = pixel(canvas, point)
                assert abs(outside.red() - round(36 + (255 - 36) * opacity)) <= 1
                assert abs(outside.green() - round(36 * (1 - opacity))) <= 1
        # Neither preview nor export includes overflow outside page masks.
        exported = canvas.render_export_image()
        assert exported.pixelColor(20, 70).alpha() == 0
        assert exported.pixelColor(50, 70) == QColor("red")
    finally:
        canvas.deleteLater()


def test_crop_allocates_only_source_region_and_preserves_world_coordinates(qapp):
    canvas, chapter, _obj = scene(qapp)
    try:
        chapter.export_rect = (-15, 55, 80, 40)
        chapter.export_rect_enabled = True
        image = canvas.render_export_image()
        assert (image.width(), image.height()) == (80, 40)
        assert image.pixelColor(5, 15).alpha() == 0
        assert image.pixelColor(50, 15) == QColor("red")
        page = chapter.layers[chapter.root_page_ids[0]]
        page.bound = BoundGeometry.rectangle(-60, 0, 160, 100)
        canvas._compound_path_cache.clear()
        outside_chapter = canvas.render_export_image()
        assert outside_chapter.pixelColor(5, 15) == QColor("red")
        chapter.export_rect = (30.2, 60.3, 10.2, 8.1)
        assert canvas.export_source_rect() == QRectF(30, 60, 11, 9)
        chapter.export_rect_enabled = False
        assert canvas.export_source_rect() == QRectF(0, 0, chapter.width, chapter.height)
    finally:
        canvas.deleteLater()


def test_export_rect_preserves_geometry_and_supports_move_resize_and_undo(qapp, monkeypatch):
    canvas, chapter, _obj = scene(qapp)
    chapter.export_rect = (20, 30, 80, 70)
    try:
        canvas.set_export_rect_editing(True)
        assert chapter.export_rect == (20, 30, 80, 70)
        canvas._ensure_scene_cache()
        cache_key = canvas._scene_cache.cacheKey()
        start = canvas.camera_transform().map(QPointF(60, 65))
        canvas._export_rect_pointer_press(start)
        canvas._export_rect_pointer_move(start + QPointF(10, 15))
        canvas._ensure_scene_cache()
        assert canvas._scene_cache.cacheKey() == cache_key
        canvas._export_rect_pointer_release(start + QPointF(10, 15))
        assert chapter.export_rect == (30, 45, 80, 70)
        corner = canvas.camera_transform().map(QPointF(110, 115))
        canvas._export_rect_pointer_press(corner)
        canvas._export_rect_pointer_release(corner + QPointF(20, 10))
        assert chapter.export_rect == (30, 45, 100, 80)
        canvas.set_export_rect_editing(False)
        canvas.command_stack.undo()
        assert chapter.export_rect == (20, 30, 80, 70)
        canvas.command_stack.redo()
        assert chapter.export_rect == (30, 45, 100, 80)
        canvas.set_export_rect_enabled(True)
        canvas.set_export_rect_enabled(False)
        canvas.set_export_rect_editing(True)
        assert chapter.export_rect == (30, 45, 100, 80)
        canvas.set_export_rect_editing(False)
        restored = ChapterDocument.from_dict(chapter.to_dict())
        assert restored.export_rect == chapter.export_rect
        assert not restored.export_rect_enabled
    finally:
        canvas.deleteLater()


@pytest.mark.parametrize("command", ["_export_again", "_export_png"])
def test_cropped_export_asks_first_and_keeps_original_destination(qapp, tmp_path, monkeypatch, command):
    original = tmp_path / "texture.png"
    image = QImage(70, 50, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("red"))
    image.save(str(original))
    window = MainWindow()
    try:
        assert window.open_path(original)
        normal_key = window._export_destination_key()
        normal_bytes = original.read_bytes()
        window.canvas.set_export_rect_enabled(True)
        window.chapter.export_rect = (10, 5, 25, 30)
        destination = tmp_path / "crop.png"
        requests = []
        monkeypatch.setattr(QFileDialog, "getSaveFileName", lambda *args: (
            requests.append(args) or (str(destination), "PNG image (*.png)")))
        getattr(window, command)()
        assert len(requests) == 1
        assert original.read_bytes() == normal_bytes
        assert QImage(str(destination)).size().toTuple() == (25, 30)
        rect_key = window._export_destination_key()
        assert rect_key != normal_key
        assert window.settings.export_destinations[normal_key] == str(original)
        assert window.settings.export_destinations[rect_key] == str(destination)
        window._export_again()
        assert len(requests) == 1
        window.canvas.set_export_rect_enabled(False)
        assert window._export_destination_key() == normal_key
    finally:
        window.deleteLater()


def test_view_fields_default_for_existing_projects_and_validate():
    chapter = ChapterDocument()
    raw = chapter.to_dict()
    for key in ("view_overflow", "export_rect", "export_rect_enabled"):
        raw.pop(key)
    restored = ChapterDocument.from_dict(raw)
    assert restored.view_overflow == 0
    assert restored.export_rect is None and not restored.export_rect_enabled
    restored.view_overflow = float("nan")
    restored.validate()
    assert restored.view_overflow == 0
    restored.export_rect = (0, 0, -1, 20)
    with pytest.raises(ValueError, match="Export rectangle"):
        restored.validate()


def test_overflow_setting_is_saved_and_single_drag_is_one_undo(qapp):
    canvas, chapter, _obj = scene(qapp)
    try:
        from comic_editor.ui.view_settings import ViewSettingsPanel
        from PySide6.QtGui import QAction
        from PySide6.QtWidgets import QCheckBox, QToolButton
        panel = ViewSettingsPanel(canvas, QCheckBox(), QToolButton(), QAction())
        before_revision = canvas.command_stack.revision
        panel._begin_overflow()
        for value in (10, 20, 30, 40, 50):
            panel.overflow.setValue(value)
        assert canvas.command_stack.revision == before_revision
        panel._finish_overflow()
        assert canvas.command_stack.revision == before_revision + 1
        assert ChapterDocument.from_dict(chapter.to_dict()).view_overflow == 0.5
        canvas.command_stack.undo()
        assert chapter.view_overflow == 0 and panel.overflow.value() == 0
        panel.deleteLater()
    finally:
        canvas.deleteLater()


@pytest.mark.parametrize("compound", [False, True])
def test_overflow_keeps_page_opacity_nested_effects_masks_and_compound_shapes(qapp, compound):
    from comic_editor.core.models import HueSaturationLightnessModifier, ParameterMaskBinding, ToneMask
    canvas, chapter, _obj = scene(qapp)
    page = chapter.layers[chapter.root_page_ids[0]]
    layer = chapter.layers[chapter.objects[_obj.object_id].parent_layer_id]
    layer.compound_enabled = compound
    page.opacity = 0.8
    mask = ToneMask(name="Half opacity", saved=True)
    chapter.masks[mask.mask_id] = mask
    layer.opacity_mask = ParameterMaskBinding(mask.mask_id, 0.5, 0.5)
    chapter.add_modifier(HueSaturationLightnessModifier(hue=120), [("layer", layer.layer_id)])
    try:
        # Warm ordinary effect captures before exposing overflow so that both
        # rendering contexts must coexist without recycling a clipped source.
        canvas._ensure_scene_cache()
        for amount in (1, 0.5, 1):
            canvas.set_view_overflow(amount)
            canvas._ensure_scene_cache()
            outside = pixel(canvas, (-10, 70))
            assert abs(outside.green() - round(36 + (255 - 36) * 0.4 * amount)) <= 2
            assert abs(outside.red() - round(36 * (1 - 0.4 * amount))) <= 2
            exported = canvas.render_export_image()
            assert exported.pixelColor(20, 70).alpha() == 0
            inside = exported.pixelColor(50, 70)
            assert inside.green() >= 250 and inside.red() <= 2
            assert abs(inside.alpha() - 102) <= 2
    finally:
        canvas.deleteLater()


def test_undo_model_replacement_closes_export_editor_safely(qapp):
    canvas, chapter, _obj = scene(qapp)
    try:
        before = chapter.to_dict()
        chapter.add_object(chapter.root_page_ids[0], RasterObject())
        canvas.push_model_change(before, chapter.to_dict(), "Add raster")
        canvas.set_export_rect_editing(True)
        canvas.command_stack.undo()
        assert not canvas.export_rect_editing
        assert canvas.chapter.export_rect is None
        canvas._export_rect_pointer_move(QPointF(50, 50))
        assert canvas._export_rect_handles() == {}
    finally:
        canvas.deleteLater()


def test_overflow_remains_visible_and_moves_during_live_raster_transform(qapp):
    from comic_editor.ui.canvas import ToolKind
    canvas, chapter, obj = scene(qapp, raster=True)
    try:
        canvas.set_selection("object", obj.object_id)
        canvas.set_tool(ToolKind.TRANSFORM)
        canvas.set_view_overflow(1)
        point = canvas.camera_transform().map(QPointF(-10, 70)).toPoint()
        assert canvas.grab().toImage().pixelColor(point) == QColor("red")
        assert canvas._begin_selected_raster_transform(QPointF(30, 70))
        assert canvas.grab().toImage().pixelColor(point) == QColor("red")
        canvas._update_transform_preview(QPointF(30, 90))
        canvas.grab()
        assert pixel(canvas, (-10, 70)) == QColor("#242428")
        assert pixel(canvas, (-10, 90)) == QColor("red")
        canvas._clear_transform_preview()
    finally:
        canvas.deleteLater()


def test_overflow_keeps_other_vector_ink_visible_during_live_eraser(qapp):
    from comic_editor.core.models import VectorDrawingObject, VectorStroke, VectorStrokePoint
    from comic_editor.ui.canvas import ToolKind
    canvas, chapter, original = scene(qapp)
    original.visible = False
    strokes = [VectorStroke(color="#FFFF0000", points=[
        VectorStrokePoint(x=0, y=y, width=10),
        VectorStrokePoint(x=100, y=y, width=10),
    ]) for y in (10, 30)]
    obj = chapter.add_object(original.parent_layer_id, VectorDrawingObject(x=-60, y=40, strokes=strokes))
    try:
        canvas.settings.vector_eraser_mode = "stroke"
        canvas.set_selection("object", obj.object_id)
        canvas.set_tool(ToolKind.RASTER_ERASER)
        canvas.set_view_overflow(1)
        canvas.grab()
        assert pixel(canvas, (-10, 70)) == QColor("red")
        assert pixel(canvas, (-10, 90)) == QColor("red")
        canvas._begin_vector_gesture(obj, QPointF(50, 70), 1.0)
        canvas.grab()
        assert pixel(canvas, (-10, 70)) == QColor("#242428")
        assert pixel(canvas, (-10, 90)) == QColor("red")
        canvas._finish_vector_eraser(obj)
    finally:
        canvas.deleteLater()
