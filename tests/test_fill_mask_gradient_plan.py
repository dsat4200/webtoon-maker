from __future__ import annotations

import copy

import numpy as np
import pytest
from PySide6.QtCore import QPointF, QRectF, QThreadPool, Qt
from PySide6.QtGui import (
    QColor, QImage, QPainter, QPainterPath, QPen, QTransform,
)

from comic_editor.core.assets import AssetManifest
from comic_editor.core.commands import CallbackCommand
from comic_editor.core.fill_migration import materialize_legacy_fills
from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, ChildRef, ColorFillGradientObject,
    DocumentObject, ImageObject, RasterObject, ShapeStyle, TextObject,
    ParameterMaskBinding, ToneMask, VectorDrawingObject,
)
from comic_editor.core.settings import (
    EditorSettings, FILL_BLEND_MODES, FILL_SUBTOOLS,
)
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui.tool_ribbon_pages import ToolSettingsControls
from comic_editor.ui.tree_model import HierarchyModel


def _canvas_with_shape(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 720, 600),
        style=ShapeStyle(primary_color="#FFFFFFFF", outline_thickness=0),
    )
    layer = chapter.add_layer(
        page.layer_id, "Shape", BoundGeometry.rectangle(40, 40, 360, 300),
        style=ShapeStyle(primary_color="#00000000", outline_thickness=0),
    )
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(0, 0, 500, 400)),
    )
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(160, 150), 56,
        QColor("#FFCC3311"), square=True, antialias=False,
    )
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(720, 600)
    canvas.set_document(chapter, tiles)
    canvas.scale = 1.0
    canvas.center_x = 360
    canvas.center_y = 300
    canvas.show()
    qapp.processEvents()
    return canvas, chapter, page, layer, raster


def test_shape_translation_repaints_child_art_live_and_commits_once(qapp):
    canvas, chapter, _page, layer, raster = _canvas_with_shape(qapp)
    canvas.set_selection("layer", layer.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    source = QPointF(160, 150)
    destination = source + QPointF(100, 40)
    before = canvas.grab().toImage()
    source_widget = canvas.document_to_widget(source).toPoint()
    destination_widget = canvas.document_to_widget(destination).toPoint()
    source_color = before.pixelColor(source_widget)
    destination_color = before.pixelColor(destination_widget)
    assert source_color != destination_color

    canvas._tool_press(canvas.document_to_widget(QPointF(180, 170)), 1.0)
    canvas._tool_move(canvas.document_to_widget(QPointF(280, 210)), 1.0)
    qapp.processEvents()
    preview = canvas.grab().toImage()

    assert (layer.translate_x, layer.translate_y) == pytest.approx((100, 40))
    assert (raster.x, raster.y) == (0, 0)
    assert not canvas.command_stack.can_undo
    assert preview.pixelColor(source_widget) != source_color
    moved_color = preview.pixelColor(destination_widget)
    assert moved_color != destination_color
    assert moved_color.red() > moved_color.green() * 2

    canvas._tool_release()
    assert canvas.command_stack.can_undo
    canvas.command_stack.undo()
    restored = canvas.chapter.layers[layer.layer_id]
    assert (restored.translate_x, restored.translate_y) == (0, 0)


def test_mask_only_is_hidden_from_preview_but_contributes_scaled_alpha(qapp):
    canvas, chapter, _page, layer, _raster = _canvas_with_shape(qapp)
    layer.mask_only = True
    layer.opacity = 0.5
    mask = ToneMask(
        name="Layer alpha", saved=True,
        contributors=[("layer", layer.layer_id)],
    )
    chapter.masks[mask.mask_id] = mask
    chapter.validate()

    preview = QImage(720, 600, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.pixelColor(160, 150) == QColor(chapter.background)
    assert canvas.sample_composited_color(QPointF(160, 150)) == QColor(
        chapter.background
    ).name(QColor.NameFormat.HexArgb).upper()

    field = canvas.render_tone_mask_field(
        mask.mask_id, 720, 600, QTransform(), QRectF(0, 0, 720, 600),
    )
    assert field[150, 160] == pytest.approx(0.5, abs=0.02)
    assert field[20, 20] == pytest.approx(0.0)

    canvas.set_selection("layer", layer.layer_id)
    qapp.processEvents()
    interactive = canvas.grab().toImage()
    point = canvas.document_to_widget(QPointF(160, 150)).toPoint()
    assert interactive.pixelColor(point) != QColor(chapter.background)

    model = HierarchyModel(chapter)
    index = model.index_for_entity("layer", layer.layer_id).siblingAtColumn(2)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "50%"
    assert chapter.layers[layer.layer_id].mask_only


def test_mask_dependency_cycle_is_rejected():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    raster = chapter.add_object(page.layer_id, RasterObject())
    mask = ToneMask(contributors=[("object", raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    raster.opacity_mask = ParameterMaskBinding(mask.mask_id, 0.0, 1.0)
    with pytest.raises(ValueError, match="Mask dependency cycle"):
        chapter.validate()


def test_mask_only_contribution_includes_attached_opacity_mask(qapp):
    canvas, chapter, page, layer, _raster = _canvas_with_shape(qapp)
    layer.mask_only = True
    layer.opacity = 0.5
    controller = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 500, 400))
    )
    controller.opacity = 0.5
    controller.opacity_locked = False
    canvas.tiles.paint_dab(
        controller.object_id, QPointF(160, 150), 56,
        QColor("white"), square=True, antialias=False,
    )
    opacity_mask = ToneMask(
        saved=True, contributors=[("object", controller.object_id)]
    )
    output_mask = ToneMask(
        saved=True, contributors=[("layer", layer.layer_id)]
    )
    chapter.masks[opacity_mask.mask_id] = opacity_mask
    chapter.masks[output_mask.mask_id] = output_mask
    layer.opacity_mask = ParameterMaskBinding(opacity_mask.mask_id, 0.0, 1.0)
    chapter.validate()

    field = canvas.render_tone_mask_field(
        output_mask.mask_id, 720, 600, QTransform(),
        QRectF(0, 0, 720, 600),
    )
    assert field[150, 160] == pytest.approx(0.25, abs=0.03)


def test_mask_only_drawable_objects_round_trip_and_helpers_are_excluded():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    objects = [
        RasterObject(name="Raster"),
        VectorDrawingObject(name="Vector"),
        ImageObject(name="Image"),
        TextObject(name="Text"),
        ColorFillGradientObject(name="Gradient"),
    ]
    for obj in objects:
        obj.mask_only = True
        chapter.add_object(page.layer_id, obj)
    helper = chapter.add_object(
        page.layer_id, DocumentObject(name="Helper", object_type="helper")
    )
    helper.mask_only = True

    chapter.validate()
    assert all(obj.mask_only for obj in objects)
    assert not helper.mask_only

    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.schema_version == 20
    assert all(restored.objects[obj.object_id].mask_only for obj in objects)
    assert not restored.objects[helper.object_id].mask_only

    legacy = copy.deepcopy(chapter.to_dict())
    legacy["schema_version"] = 19
    for raw in legacy["objects"]:
        raw.pop("mask_only", None)
    legacy_restored = ChapterDocument.from_dict(legacy)
    assert not any(obj.mask_only for obj in legacy_restored.objects.values())


def test_mask_only_raster_reveals_only_for_exact_selection_and_masks(qapp):
    canvas, chapter, page, layer, raster = _canvas_with_shape(qapp)
    raster.mask_only = True
    raster.opacity_locked = False
    mask = ToneMask(saved=True, contributors=[("object", raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    chapter.validate()

    preview = QImage(720, 600, QImage.Format.Format_ARGB32_Premultiplied)
    canvas.render_preview(preview)
    assert preview.pixelColor(160, 150) == QColor(chapter.background)

    for opacity, expected in ((0.0, 0.0), (0.5, 0.5), (1.0, 1.0)):
        raster.opacity = opacity
        field = canvas.render_tone_mask_field(
            mask.mask_id, 720, 600, QTransform(), QRectF(0, 0, 720, 600),
        )
        assert field[150, 160] == pytest.approx(expected, abs=0.02)

    raster.opacity = 0.5
    point = canvas.document_to_widget(QPointF(160, 150)).toPoint()
    canvas.set_selection("layer", layer.layer_id)
    qapp.processEvents()
    hidden_color = canvas.grab().toImage().pixelColor(point)

    canvas.set_selection("object", raster.object_id)
    qapp.processEvents()
    assert canvas.grab().toImage().pixelColor(point) != hidden_color
    thumbnail = canvas.render_asset_thumbnail(
        AssetManifest(
            root_kind="object", root_id=raster.object_id,
            document=chapter, visual_bounds=(0, 0, 500, 400),
        ),
        canvas.tiles,
    )
    assert TileStore._alpha_bbox(thumbnail) is None
    live_preview = QImage(
        720, 600, QImage.Format.Format_ARGB32_Premultiplied
    )
    live_preview.fill(Qt.transparent)
    live_painter = QPainter(live_preview)
    canvas._render_selected_raster_preview(
        live_painter, QRectF(0, 0, 720, 600)
    )
    live_painter.end()
    assert live_preview.pixelColor(160, 150).alpha() > 0

    canvas.set_selection("layer", layer.layer_id)
    qapp.processEvents()
    assert canvas.grab().toImage().pixelColor(point) == hidden_color

    canvas.set_selection("object", raster.object_id)
    raster.visible = False
    canvas._invalidate_scene_cache()
    qapp.processEvents()
    assert canvas.grab().toImage().pixelColor(point) == hidden_color
    field = canvas.render_tone_mask_field(
        mask.mask_id, 720, 600, QTransform(), QRectF(0, 0, 720, 600),
    )
    assert field[150, 160] == pytest.approx(0.0)

    raster.visible = True
    raster.fill_reference = True
    target = chapter.add_object(
        page.layer_id, RasterObject(name="Fill target")
    )
    profile = canvas.settings.active_fill_profile().copy()
    profile["reference_mode"] = "reference"
    assert ("object", raster.object_id) not in canvas._fill_reference_entities(
        target, profile
    )

    model = HierarchyModel(chapter)
    index = model.index_for_entity(
        "object", raster.object_id
    ).siblingAtColumn(2)
    assert model.data(index, Qt.ItemDataRole.DisplayRole) == "50%"
    assert raster.mask_only


def test_mask_only_object_contribution_includes_attached_opacity_mask(qapp):
    canvas, chapter, page, _layer, raster = _canvas_with_shape(qapp)
    raster.mask_only = True
    raster.opacity = 0.5
    raster.opacity_locked = False
    controller = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 500, 400))
    )
    controller.mask_only = True
    controller.opacity = 0.5
    controller.opacity_locked = False
    canvas.tiles.paint_dab(
        controller.object_id, QPointF(160, 150), 56,
        QColor("white"), square=True, antialias=False,
    )
    opacity_mask = ToneMask(
        saved=True, contributors=[("object", controller.object_id)]
    )
    output_mask = ToneMask(
        saved=True, contributors=[("object", raster.object_id)]
    )
    chapter.masks[opacity_mask.mask_id] = opacity_mask
    chapter.masks[output_mask.mask_id] = output_mask
    raster.opacity_mask = ParameterMaskBinding(opacity_mask.mask_id, 0.0, 1.0)
    chapter.validate()

    field = canvas.render_tone_mask_field(
        output_mask.mask_id, 720, 600, QTransform(),
        QRectF(0, 0, 720, 600),
    )
    assert field[150, 160] == pytest.approx(0.25, abs=0.03)


def test_advanced_fill_clips_to_selection_and_crosses_tile_edges():
    tiles = TileStore(tile_size=32)
    frame = QRectF(0, 0, 64, 32)
    selection = np.zeros((32, 32), dtype=bool)
    selection[8:24, :] = True
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({
        "connected_pixels_only": True,
        "antialiasing": False,
        "close_gap": False,
    })
    before = {}
    dirty = tiles.advanced_fill(
        "raster", QPointF(30, 16), frame, QColor("#FF229944"),
        profile, before,
        selection_tile=lambda _key: selection,
    )
    assert dirty.left() == 0
    assert dirty.right() >= 63
    assert set(before) == {(0, 0), (1, 0)}
    assert tiles.tile("raster", (0, 0)).pixelColor(10, 16).alpha() == 255
    assert tiles.tile("raster", (1, 0)).pixelColor(10, 16).alpha() == 255
    assert tiles.tile("raster", (0, 0)).pixelColor(10, 2).alpha() == 0


@pytest.mark.parametrize(
    ("gap", "threshold", "outside_filled"),
    ((0, 12, False), (6, 0, True), (6, 6, False), (24, 6, True)),
)
def test_close_gap_preserves_boundaries_and_respects_threshold(
    gap, threshold, outside_filled,
):
    reference = QImage(
        128, 128, QImage.Format.Format_ARGB32_Premultiplied
    )
    reference.fill(Qt.transparent)
    painter = QPainter(reference)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(QColor("red"), 4))
    left_end = 64 - gap // 2
    right_start = 64 + (gap - gap // 2)
    painter.drawLine(32, 32, left_end, 32)
    painter.drawLine(right_start, 32, 96, 32)
    painter.drawLine(32, 32, 32, 96)
    painter.drawLine(96, 32, 96, 96)
    painter.drawLine(32, 96, 96, 96)
    painter.end()

    store = TileStore(tile_size=128)
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({
        "connected_pixels_only": True,
        "close_gap": threshold > 0,
        "gap_threshold": threshold,
        "antialiasing": False,
        "tolerance": 0,
    })
    store.advanced_fill(
        "target", QPointF(64, 64), QRectF(0, 0, 128, 128),
        QColor("green"), profile, {},
        region_policy="transparent",
        reference_tile=lambda key: reference if key == (0, 0) else None,
    )
    result = store.tile("target", (0, 0))

    assert result.pixelColor(64, 64).alpha() == 255
    assert (result.pixelColor(8, 8).alpha() > 0) is outside_filled


def test_close_gap_bridges_reference_boundary_across_tiles():
    reference = QImage(
        128, 128, QImage.Format.Format_ARGB32_Premultiplied
    )
    reference.fill(Qt.transparent)
    painter = QPainter(reference)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, False)
    painter.setPen(QPen(QColor("red"), 4))
    painter.drawLine(16, 24, 61, 24)
    painter.drawLine(67, 24, 112, 24)
    painter.drawLine(16, 24, 16, 112)
    painter.drawLine(112, 24, 112, 112)
    painter.drawLine(16, 112, 112, 112)
    painter.end()

    def reference_tile(key):
        if key[0] not in {0, 1} or key[1] not in {0, 1}:
            return None
        return reference.copy(key[0] * 64, key[1] * 64, 64, 64)

    store = TileStore(tile_size=64)
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({
        "connected_pixels_only": True,
        "close_gap": True,
        "gap_threshold": 6,
        "antialiasing": False,
        "tolerance": 0,
    })
    store.advanced_fill(
        "target", QPointF(64, 64), QRectF(0, 0, 128, 128),
        QColor("green"), profile, {}, region_policy="transparent",
        reference_tile=reference_tile,
    )

    assert store.tile("target", (1, 1)).pixelColor(0, 0).alpha() == 255
    assert store.tile("target", (0, 0)).pixelColor(8, 8).alpha() == 0


def test_transparent_fill_erases_instead_of_drawing_invisible_source():
    tiles = TileStore(tile_size=32)
    tiles.paint_dab(
        "raster", QPointF(16, 16), 32, QColor("#FFFF0000"),
        square=True, antialias=False,
    )
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({"antialiasing": False, "close_gap": False})
    tiles.advanced_fill(
        "raster", QPointF(16, 16), QRectF(0, 0, 32, 32),
        QColor(0, 0, 0, 0), profile, {},
    )
    assert tiles.tile("raster", (0, 0)) is None


@pytest.mark.parametrize("blend_mode", FILL_BLEND_MODES)
def test_every_fill_blend_mode_produces_one_atomic_tile_patch(blend_mode):
    tiles = TileStore(tile_size=8)
    tiles.paint_dab(
        "raster", QPointF(4, 4), 8, QColor("#808080"),
        square=True, antialias=False,
    )
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({
        "blend_mode": blend_mode, "antialiasing": False,
        "close_gap": False, "tolerance": 255,
    })
    before = {}
    dirty = tiles.advanced_fill(
        "raster", QPointF(4, 4), QRectF(0, 0, 8, 8),
        QColor("#C04080FF"), profile, before,
    )
    assert not dirty.isEmpty()
    assert set(before) == {(0, 0)}


@pytest.mark.parametrize("region_policy", ("seed", "transparent", "area"))
def test_internal_fill_region_policies_execute(region_policy):
    tiles = TileStore(tile_size=8)
    profile = EditorSettings().active_fill_profile().copy()
    profile.update({
        "connected_pixels_only": False,
        "antialiasing": False, "close_gap": False,
    })
    dirty = tiles.advanced_fill(
        "raster", None, QRectF(0, 0, 8, 8),
        QColor("#FF336699"), profile, {}, region_policy=region_policy,
    )
    assert isinstance(dirty, QRectF)


def test_large_fill_selection_is_atomic_and_tool_change_cancels(qapp):
    chapter = ChapterDocument(height=1400)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 1300)
    )
    raster = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 1080, 1300))
    )
    canvas = CanvasWidget(EditorSettings())
    tiles = TileStore()
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    selection = QPainterPath()
    selection.addRect(QRectF(0, 0, 1080, 1300))
    canvas._drawing_selection_path = selection

    assert canvas.fill_active_selection()
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    assert QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    assert tiles.object_tiles(raster.object_id) == {}
    assert not canvas.command_stack.can_undo

    canvas.set_tool(ToolKind.FILL)
    assert canvas.fill_active_selection()
    assert QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    assert tiles.object_tiles(raster.object_id)
    assert len(canvas.command_stack._undo) == 1


def test_raster_selection_outline_remains_visible_in_fill_tool(qapp):
    canvas, _chapter, _page, _layer, raster = _canvas_with_shape(qapp)
    canvas.set_selection("object", raster.object_id)
    assert canvas.set_tool(ToolKind.DRAW_SELECT_RECT)
    selection = QPainterPath()
    selection.addRect(QRectF(80, 70, 180, 140))
    canvas._drawing_selection_path = selection

    assert canvas.set_tool(ToolKind.FILL)
    assert canvas._drawing_selection_path == selection

    calls = []
    draw_selection = canvas._draw_drawing_selection

    def record_selection_overlay(painter):
        calls.append(True)
        draw_selection(painter)

    canvas._draw_drawing_selection = record_selection_overlay
    image = QImage(720, 600, QImage.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._draw_selection(painter)
    painter.end()

    assert calls == [True]


def test_legacy_fill_layer_and_vector_fill_materialize_to_raster_tiles():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    shape = chapter.add_layer(
        page.layer_id, "Shape", BoundGeometry.rectangle(20, 30, 140, 90)
    )
    drawing = chapter.add_object(
        shape.layer_id, VectorDrawingObject(name="Lines")
    )
    raw = chapter.to_dict()
    raw["schema_version"] = 18

    fill_layer_id = "legacy-fill-layer"
    fill_layer = {
        "id": fill_layer_id, "name": "Backdrop", "custom_name": False,
        "parent_id": shape.layer_id, "is_page": False,
        "layer_kind": "fill", "position": [0, 0], "visible": True,
        "opacity": 0.6, "opacity_locked": True, "mask_only": False,
        "fill_reference": False, "bound": None,
        "shape_style": {"primary_color": "#FF336699"},
        "children": [], "modifier_ids": [], "opacity_mask": None,
    }
    raw["layers"].append(fill_layer)
    shape_raw = next(item for item in raw["layers"] if item["id"] == shape.layer_id)
    shape_raw["children"].insert(0, {"kind": "layer", "id": fill_layer_id})

    vector_fill_id = "legacy-vector-fill"
    drawing_raw = next(
        item for item in raw["objects"] if item["id"] == drawing.object_id
    )
    drawing_raw["fill_child_ids"] = [vector_fill_id]
    raw["objects"].append({
        "id": vector_fill_id, "type": "vector_fill",
        "name": "Vector Fill", "custom_name": False,
        "parent_layer_id": shape.layer_id, "position": [0, 0],
        "visible": True, "opacity": 1.0, "opacity_locked": True,
        "fill_reference": False, "geometry_reference": "direct",
        "ignore_parent_mask": False, "underlay_opacity": 0.0,
        "modifier_ids": [], "opacity_mask": None,
        "owner_drawing_id": drawing.object_id,
        "geometry": BoundGeometry.circle(80, 70, 24).to_dict(),
        "fill_color": "#FFAA4400", "source_seed": [80, 70],
        "source_lasso": [], "fill_settings": {},
    })

    warnings: list[str] = []
    migrated = ChapterDocument.from_dict(copy.deepcopy(raw), warnings=warnings)
    tiles = TileStore()
    assert materialize_legacy_fills(migrated, tiles) == 2
    assert all(
        isinstance(migrated.objects[item], RasterObject)
        for item in (fill_layer_id, vector_fill_id)
    )
    assert fill_layer_id not in migrated.layers
    assert tiles.content_bounds(fill_layer_id) is not None
    assert tiles.content_bounds(vector_fill_id) is not None
    saved = migrated.to_dict()
    assert saved["schema_version"] == 20
    assert not any(item.get("layer_kind") == "fill" for item in saved["layers"])
    assert not any(item.get("type") == "vector_fill" for item in saved["objects"])
    assert all("fill_child_ids" not in item for item in saved["objects"])
    assert warnings


@pytest.mark.parametrize("field_type", ["line", "radial"])
def test_gradient_tool_creates_by_direct_drag(qapp, field_type):
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(720, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id)
    canvas.set_gradient_field_type(field_type)
    assert canvas.set_tool(ToolKind.GRADIENT)

    canvas._tool_press(canvas.document_to_widget(QPointF(100, 100)), 1.0)
    canvas._tool_move(canvas.document_to_widget(QPointF(280, 180)), 1.0)
    canvas._tool_release()

    gradients = chapter.gradient_children(page.layer_id, field_type)
    assert len(gradients) == 1
    assert canvas.selected_id == gradients[0].object_id
    assert canvas.tool == ToolKind.GRADIENT
    assert canvas.command_stack.can_undo


def test_parent_shape_gradient_is_created_by_click(qapp):
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(720, 600)
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("layer", page.layer_id)
    canvas.set_gradient_field_type("parent_shape")
    assert canvas.set_tool(ToolKind.GRADIENT)
    canvas._tool_press(canvas.document_to_widget(QPointF(200, 200)), 1.0)
    assert len(chapter.gradient_children(page.layer_id, "parent_shape")) == 1


def test_fill_settings_have_five_independent_profiles():
    settings = EditorSettings()
    assert tuple(settings.fill_profiles) == FILL_SUBTOOLS
    settings.fill_profiles["editing_layer"]["tolerance"] = 3
    assert settings.fill_profiles["other_layers"]["tolerance"] == 16


def test_fill_numeric_controls_are_full_width_slider_rows(qapp):
    controls = ToolSettingsControls(EditorSettings())

    assert not hasattr(controls, "fill_color_source")
    assert not hasattr(controls, "fill_specified_color")
    assert not hasattr(controls, "fill_target_mode")
    assert "exclude_text" not in controls.fill_exclusions
    expected = (
        (controls.fill_opacity_slider, controls.fill_opacity, 0, 100),
        (controls.raster_fill_tolerance_slider,
         controls.raster_fill_tolerance, 0, 255),
        (controls.fill_gap_threshold_slider,
         controls.fill_gap_threshold, 0, 16),
        (controls.fill_area_amount_slider,
         controls.fill_area_amount, -64, 64),
        (controls.fill_magnetic_strength_slider,
         controls.fill_magnetic_strength, 0, 100),
        (controls.fill_stabilization_slider,
         controls.fill_stabilization, 0, 100),
        (controls.fill_speed_adjustment_slider,
         controls.fill_speed_adjustment, -100, 100),
        (controls.fill_post_correction_slider,
         controls.fill_post_correction, 0, 100),
    )
    for slider, editor, minimum, maximum in expected:
        assert slider.minimum() == editor.minimum() == minimum
        assert slider.maximum() == editor.maximum() == maximum
        assert bool(slider.parentWidget().property("flowFullWidth"))
        slider.setValue(maximum)
        assert editor.value() == maximum
        editor.setValue(minimum)
        assert slider.value() == minimum


def test_tolerance_replays_latest_fill_with_active_secondary_color(qapp):
    chapter = ChapterDocument(height=100)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 32, 8)
    )
    raster = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 32, 8))
    )
    tiles = TileStore(tile_size=32)
    source = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FF101010"))
    painter = QPainter(source)
    painter.fillRect(QRectF(16, 0, 16, 32), QColor("#FF202020"))
    painter.end()
    tiles.set_tile(raster.object_id, (0, 0), source)
    settings = EditorSettings()
    profile = settings.active_fill_profile()
    profile.update({
        "tolerance": 0, "antialiasing": False, "close_gap": False,
    })
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    canvas.set_active_colors("#FFFF0000", "#FF00FF00")
    canvas.set_active_color_slot("secondary")

    canvas._begin_fill_gesture(raster, QPointF(4, 4))
    canvas._finish_fill_gesture(raster)

    assert len(canvas.command_stack._undo) == 1
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(4, 4) == QColor(
        "#FF00FF00"
    )
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(24, 4) == QColor(
        "#FF202020"
    )

    canvas.set_active_colors("#FF0000FF", "#FFFFFF00")
    canvas.request_fill_tolerance_replay(20, immediate=True)
    assert len(canvas.command_stack._undo) == 1
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(24, 4) == QColor(
        "#FF00FF00"
    )

    canvas.request_fill_tolerance_replay(0, immediate=True)
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(24, 4) == QColor(
        "#FF202020"
    )
    canvas.command_stack.undo()
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(4, 4) == QColor(
        "#FF101010"
    )
    canvas.command_stack.redo()
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(4, 4) == QColor(
        "#FF00FF00"
    )
    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(24, 4) == QColor(
        "#FF202020"
    )


def test_large_tolerance_replay_uses_cancelable_worker(qapp):
    chapter = ChapterDocument(height=160)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 160, 128)
    )
    raster = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 160, 128))
    )
    tiles = TileStore(tile_size=32)
    for y in range(4):
        for x in range(5):
            image = QImage(
                32, 32, QImage.Format.Format_ARGB32_Premultiplied
            )
            image.fill(QColor("#FF101010" if x < 2 else "#FF202020"))
            tiles.set_tile(raster.object_id, (x, y), image)
    settings = EditorSettings()
    settings.active_fill_profile().update({
        "tolerance": 0, "antialiasing": False, "close_gap": False,
    })
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    canvas.set_active_colors("#FF00FF00", "#FFFFFFFF")
    canvas._begin_fill_gesture(raster, QPointF(4, 4))
    canvas._finish_fill_gesture(raster)

    assert tiles.tile(raster.object_id, (4, 0)).pixelColor(4, 4) == QColor(
        "#FF202020"
    )
    canvas.request_fill_tolerance_replay(20, immediate=True)
    assert QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    assert tiles.tile(raster.object_id, (4, 0)).pixelColor(4, 4) == QColor(
        "#FF00FF00"
    )
    assert len(canvas.command_stack._undo) == 1

    canvas.request_fill_tolerance_replay(20, immediate=True)
    canvas.request_fill_tolerance_replay(0, immediate=True)
    assert QThreadPool.globalInstance().waitForDone(5000)
    qapp.processEvents()
    assert tiles.tile(raster.object_id, (4, 0)).pixelColor(4, 4) == QColor(
        "#FF202020"
    )


def test_tolerance_replay_does_not_accumulate_opacity_or_blending(qapp):
    chapter = ChapterDocument(height=100)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 32, 8)
    )
    raster = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 32, 8))
    )
    tiles = TileStore(tile_size=32)
    source = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FF202020"))
    tiles.set_tile(raster.object_id, (0, 0), source)
    settings = EditorSettings()
    settings.active_fill_profile().update({
        "tolerance": 0, "opacity": 50, "blend_mode": "screen",
        "antialiasing": False, "close_gap": False,
    })
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    canvas.set_active_colors("#FF80C0FF", "#FFFFFFFF")
    canvas._begin_fill_gesture(raster, QPointF(4, 4))
    canvas._finish_fill_gesture(raster)
    first = tiles.tile(raster.object_id, (0, 0)).pixelColor(4, 4)

    canvas.request_fill_tolerance_replay(0, immediate=True)

    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(4, 4) == first


def test_intervening_history_change_permanently_ends_fill_replay(qapp):
    chapter = ChapterDocument(height=100)
    page = chapter.add_page("Page")
    raster = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 32, 8))
    )
    tiles = TileStore(tile_size=32)
    source = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    source.fill(QColor("#FF101010"))
    painter = QPainter(source)
    painter.fillRect(QRectF(16, 0, 16, 32), QColor("#FF202020"))
    painter.end()
    tiles.set_tile(raster.object_id, (0, 0), source)
    settings = EditorSettings()
    settings.active_fill_profile().update({
        "tolerance": 0, "antialiasing": False, "close_gap": False,
    })
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, tiles)
    canvas.set_selection("object", raster.object_id)
    canvas.set_tool(ToolKind.FILL)
    canvas.set_active_colors("#FF00FF00", "#FFFFFFFF")
    canvas._begin_fill_gesture(raster, QPointF(4, 4))
    canvas._finish_fill_gesture(raster)
    canvas.command_stack.push(CallbackCommand(
        "Another edit", lambda: None, lambda: None
    ))
    canvas.command_stack.undo()

    canvas.request_fill_tolerance_replay(20, immediate=True)

    assert tiles.tile(raster.object_id, (0, 0)).pixelColor(24, 4) == QColor(
        "#FF202020"
    )
    assert canvas._fill_replay_state is None

@pytest.mark.parametrize(
    "reference_mode", ("all_visible", "reference", "selected", "current_folder")
)
def test_text_is_always_excluded_from_fill_references(qapp, reference_mode):
    chapter = ChapterDocument(height=100)
    page = chapter.add_page("Page")
    target = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 32, 32))
    )
    text = chapter.add_object(page.layer_id, TextObject(text="Boundary"))
    text.fill_reference = True
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_selection("object", target.object_id)
    canvas.selected_entities = [("object", text.object_id)]
    profile = dict(canvas.settings.active_fill_profile())
    profile.update({
        "reference_mode": reference_mode,
        "exclude_editing_target": True,
    })

    assert ("object", text.object_id) not in canvas._fill_reference_entities(
        target, profile
    )


def test_text_inside_referenced_folder_is_not_rendered(qapp):
    chapter = ChapterDocument(height=100)
    page = chapter.add_page("Page")
    folder = chapter.add_layer(page.layer_id, "Reference folder")
    chapter.add_object(
        folder.layer_id,
        TextObject(text="Boundary", x=0, y=0, width=32, height=32),
    )
    target = chapter.add_object(
        page.layer_id, RasterObject(interaction_rect=(0, 0, 32, 32))
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore(tile_size=32))
    canvas.set_selection("object", target.object_id)
    canvas.selected_entities = [("layer", folder.layer_id)]
    profile = dict(canvas.settings.active_fill_profile())
    profile.update({
        "reference_mode": "selected", "exclude_editing_target": True,
    })

    image = canvas._fill_reference_tile(target, (0, 0), profile)

    assert image is not None
    assert TileStore._alpha_bbox(image) is None
