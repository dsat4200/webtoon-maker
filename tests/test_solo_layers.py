"""Solo isolation preserves document state, source masks, and current selection."""
from __future__ import annotations

import pytest
from PySide6.QtCore import QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument, ParameterMaskBinding, RasterObject, ToneMask,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.tree_model import EyeVisibilityDelegate, HierarchyModel


def scene():
    chapter = ChapterDocument(height=160)
    page = chapter.add_page("Page", BoundGeometry.rectangle(0, 0, 1080, 160))
    group = chapter.add_layer(page.layer_id, "Group", BoundGeometry.rectangle(20, 20, 180, 100))
    group.fill_color = "#00ff00"
    group.border_width = 5
    left = chapter.add_object(group.layer_id, RasterObject(name="Left"))
    right = chapter.add_object(group.layer_id, RasterObject(name="Right"))
    hidden = chapter.add_layer(page.layer_id, "Hidden", BoundGeometry.rectangle(220, 20, 40, 40))
    hidden.fill_color = "#0000ff"
    hidden.visible = False
    tiles = TileStore()
    tiles.paint_dab(left.object_id, QPointF(60, 60), 40, QColor("red"))
    tiles.paint_dab(left.object_id, QPointF(220, 60), 20, QColor("red"))
    tiles.paint_dab(right.object_id, QPointF(140, 60), 40, QColor("blue"))
    return chapter, tiles, group, left, right, hidden


@pytest.fixture
def canvas(qapp):
    chapter, tiles, *_ = scene()
    result = CanvasWidget(EditorSettings())
    result.set_document(chapter, tiles)
    yield result
    result._effect_jobs.cancel()
    result.deleteLater()


def render(canvas):
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    canvas.render_preview(image)
    return image


def entities(canvas):
    return {entity.name: entity for entity in [*canvas.chapter.layers.values(), *canvas.chapter.objects.values()]}


@pytest.mark.parametrize("compound", [False, True])
def test_multiple_solos_filter_siblings_and_keep_parent_clipping_without_mutation(canvas, compound):
    nodes = entities(canvas)
    nodes["Group"].compound_enabled = compound
    before = canvas.chapter.to_dict()
    canvas.toggle_solo("object", nodes["Left"].object_id)
    image = render(canvas)
    assert image.pixelColor(60, 60).red() == 255
    assert image.pixelColor(140, 60).alpha() == 0
    assert image.pixelColor(220, 60).alpha() == 0
    assert image.pixelColor(30, 100).alpha() == 0  # Parent fill is isolated too.
    assert nodes["Right"].object_id not in canvas.hit_test_objects(QPointF(140, 60))
    assert canvas.hit_test_objects(QPointF(60, 60)) == [nodes["Left"].object_id]
    assert not canvas._shape_border_contains(nodes["Group"].layer_id, QPointF(20, 60))
    canvas.toggle_solo("object", nodes["Right"].object_id)
    assert render(canvas).pixelColor(140, 60).blue() == 255
    canvas.toggle_solo("object", nodes["Left"].object_id)
    assert render(canvas).pixelColor(60, 60).alpha() == 0
    canvas.toggle_solo("object", nodes["Right"].object_id)
    image = render(canvas)
    assert image.pixelColor(30, 100).green() == 255
    assert image.pixelColor(240, 40).alpha() == 0
    assert canvas.chapter.to_dict() == before


def test_solo_layer_includes_descendants_and_export_uses_original_visibility(canvas):
    nodes = entities(canvas)
    canvas.toggle_solo("object", nodes["Left"].object_id)
    image = canvas.render_export_image()
    assert image.pixelColor(140, 60).blue() == 255
    assert image.pixelColor(30, 100).green() == 255
    assert image.pixelColor(240, 40).alpha() == 0
    assert render(canvas).pixelColor(140, 60).alpha() == 0
    canvas.set_solo_entities({("layer", nodes["Group"].layer_id)})
    image = render(canvas)
    assert image.pixelColor(60, 60).red() == 255
    assert image.pixelColor(140, 60).blue() == 255
    assert image.pixelColor(30, 100).green() == 255


def test_solo_survives_session_switch_and_stale_entries_do_not_hide_document(canvas):
    nodes = entities(canvas)
    solo = ("object", nodes["Left"].object_id)
    canvas.toggle_solo(*solo)
    state = canvas.capture_session_state()
    other, tiles, *_ = scene()
    canvas.set_document(other, tiles)
    assert canvas.solo_entities == set()
    canvas.restore_session_state(state)
    assert canvas.solo_entities == {solo}
    assert render(canvas).pixelColor(140, 60).alpha() == 0
    canvas.set_solo_entities({("object", "removed-object")})
    assert render(canvas).pixelColor(140, 60).blue() == 255


def test_soloed_object_keeps_non_solo_mask_contributors(canvas):
    nodes = entities(canvas)
    left, right = nodes["Left"], nodes["Right"]
    # The otherwise hidden sibling contributes to the selected object's opacity.
    canvas.tiles.paint_dab(right.object_id, QPointF(60, 60), 40, QColor("black"))
    right.mask_only = True
    mask = ToneMask(contributors=[("object", right.object_id)])
    canvas.chapter.masks[mask.mask_id] = mask
    left.opacity_mask = ParameterMaskBinding(mask.mask_id, 0.0, 1.0)
    canvas.toggle_solo("object", left.object_id)
    image = render(canvas)
    assert image.pixelColor(60, 60).alpha() > 240
    assert image.pixelColor(60, 60).red() > 240
    assert image.pixelColor(140, 60).alpha() == 0


def test_parent_modifier_caches_do_not_mix_solo_preview_and_export(canvas):
    nodes = entities(canvas)
    canvas.chapter.add_modifier(BlurModifier(strength=2), [("layer", nodes["Group"].layer_id)])
    assert render(canvas).pixelColor(140, 60).blue() > 240
    canvas.toggle_solo("object", nodes["Left"].object_id)
    assert render(canvas).pixelColor(140, 60).alpha() == 0
    assert canvas.render_export_image().pixelColor(140, 60).blue() > 240
    assert render(canvas).pixelColor(140, 60).alpha() == 0


def test_non_solo_selection_underlay_stays_hidden(canvas):
    nodes = entities(canvas)
    canvas._live_underlay_object_id = nodes["Right"].object_id
    canvas._live_underlay_amount = 0.5
    canvas.toggle_solo("object", nodes["Left"].object_id)
    image = QImage(canvas.chapter.width, canvas.chapter.height, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._render_selected_drawing_underlay(painter, QRectF(image.rect()))
    painter.end()
    assert image.pixelColor(140, 60).alpha() == 0


def test_rasterize_preserves_siblings_hidden_only_by_solo(canvas):
    from comic_editor.ui.baking import rasterize
    nodes = entities(canvas)
    canvas.toggle_solo("object", nodes["Left"].object_id)
    canvas.set_selection("layer", nodes["Group"].layer_id)
    rasterize(canvas, "layer", nodes["Group"].layer_id)
    image = render(canvas)
    assert image.pixelColor(60, 60).red() > 240
    assert image.pixelColor(140, 60).blue() > 240
    canvas.command_stack.undo()
    assert canvas.solo_entities == {("object", nodes["Left"].object_id)}
    assert render(canvas).pixelColor(140, 60).alpha() == 0


def test_asset_crop_ignores_solo_when_asset_retains_source_ids(canvas):
    nodes = entities(canvas)
    original = canvas.chapter
    asset_document = ChapterDocument.from_dict(original.to_dict())
    canvas.toggle_solo("object", nodes["Left"].object_id)
    crop = canvas._render_entity_crop(
        asset_document, canvas.tiles, "layer", nodes["Group"].layer_id,
    )
    assert crop.pixelColor(40, 40).red() > 240
    assert crop.pixelColor(120, 40).blue() > 240
    assert canvas.chapter is original
    assert canvas.solo_entities == {("object", nodes["Left"].object_id)}
    assert render(canvas).pixelColor(140, 60).alpha() == 0


def test_applying_raster_modifier_to_non_solo_selection_preserves_pixels(canvas):
    from comic_editor.ui.baking import apply_raster_modifiers
    nodes = entities(canvas)
    right = nodes["Right"]
    modifier = BlurModifier(strength=2)
    canvas.chapter.add_modifier(modifier, [("object", right.object_id)])
    canvas.toggle_solo("object", nodes["Left"].object_id)
    canvas.set_selection("object", right.object_id)
    apply_raster_modifiers(canvas, modifier.modifier_id)
    assert modifier.modifier_id not in right.modifier_ids
    assert canvas.render_export_image().pixelColor(140, 60).blue() > 240
    assert render(canvas).pixelColor(140, 60).alpha() == 0


def test_toolbar_and_star_remove_solo_without_changing_current_selection(qapp, monkeypatch):
    monkeypatch.setattr("comic_editor.ui.main_window.save_settings", lambda _: None)
    window = MainWindow()
    chapter, tiles, group, left, right, _ = scene()
    window._set_chapter(chapter, tiles)
    window.resize(1280, 900)
    window.show()
    try:
        window.canvas.set_selection("object", left.object_id)
        window.selection_common.solo_button.click()
        assert window.selection_common.solo_button.isChecked()
        window.canvas.set_selection("object", right.object_id)
        assert not window.selection_common.solo_button.isChecked()
        window.selection_common.solo_button.click()
        model = window.hierarchy_model
        left_index = model.index_for_entity("object", left.object_id)
        right_index = model.index_for_entity("object", right.object_id)
        assert left_index.data(HierarchyModel.SoloHighlightRole)
        assert not right_index.data(HierarchyModel.SoloHighlightRole)
        assert left_index.data(Qt.BackgroundRole) == QColor("#c5a137")
        window.tree.expandAll()
        qapp.processEvents()
        star = EyeVisibilityDelegate.star_rect(window.tree.visualRect(left_index))
        QTest.mousePress(window.tree.viewport(), Qt.LeftButton, pos=star.center())
        QTest.mouseRelease(window.canvas, Qt.LeftButton, pos=QPoint(1, 1))
        assert window._solo_star_press is None
        assert len(window.canvas.solo_entities) == 2
        QTest.mouseClick(window.tree.viewport(), Qt.LeftButton, pos=star.center())
        assert window.canvas.solo_entities == {("object", right.object_id)}
        assert window.canvas.selected_id == right.object_id
        assert window.tree.currentIndex().siblingAtColumn(0) == right_index
        assert window.selection_common.solo_button.isChecked()
        assert not left_index.data(HierarchyModel.SoloRole)
        assert left.visible and right.visible
    finally:
        window.autosave_timer.stop()
        window.canvas._effect_jobs.cancel()
        window._dirty = False
        window.hide()
        window.deleteLater()
