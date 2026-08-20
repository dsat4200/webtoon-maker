from __future__ import annotations

import pytest
import numpy as np
from PySide6.QtCore import QItemSelectionModel, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage, QPainter
from PySide6.QtTest import QTest

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument,
    HueSaturationLightnessModifier, OutlineModifier,
    ParameterMaskBinding, RasterObject, TextObject, ToneMask,
    VectorDrawingObject,
)
from comic_editor.core.settings import EditorSettings
from comic_editor.core.tiles import TileStore
from comic_editor.ui.canvas import CanvasWidget, ToolKind
from comic_editor.ui import canvas as canvas_module
from comic_editor.ui.main_window import MainWindow
from comic_editor.ui.modifier_rendering import apply_modifier_stack


def _document():
    chapter = ChapterDocument(name="Modifiers")
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 600, 600)
    )
    first = chapter.add_layer(
        page.layer_id, "First", BoundGeometry.rectangle(20, 20, 250, 250)
    )
    second = chapter.add_layer(
        page.layer_id, "Second", BoundGeometry.rectangle(300, 20, 250, 250)
    )
    raster = chapter.add_object(
        first.layer_id, RasterObject(name="Raster", x=40, y=50)
    )
    vector = chapter.add_object(
        first.layer_id, VectorDrawingObject(name="Vector", x=90, y=80)
    )
    return chapter, page, first, second, raster, vector


def test_modifier_registry_round_trip_validation_and_garbage_collection():
    chapter, _page, first, _second, raster, vector = _document()
    text = chapter.add_object(first.layer_id, TextObject(text="No modifiers"))
    shared = HueSaturationLightnessModifier(
        intensity=75, hue=42, saturation=18, lightness=-9
    )
    chapter.add_modifier(shared, [
        ("object", raster.object_id), ("object", vector.object_id),
    ])
    shape_blur = BlurModifier(
        strength=12, mode="focal", focal_center=(120, 130),
        focal_radius=70, focal_ramp=.3,
    )
    chapter.add_modifier(shape_blur, [("layer", first.layer_id)])
    text.modifier_ids = [shared.modifier_id]
    orphan = HueSaturationLightnessModifier()
    chapter.modifiers[orphan.modifier_id] = orphan
    first.transform_frame = (20, 20, 250, 250)
    first.transform_quad = [(30, 25), (285, 35), (275, 290), (25, 270)]

    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert restored.schema_version == 18
    assert restored.objects[text.object_id].modifier_ids == []
    assert orphan.modifier_id not in restored.modifiers
    assert restored.objects[raster.object_id].modifier_ids == [
        shared.modifier_id
    ]
    assert restored.objects[vector.object_id].modifier_ids == [
        shared.modifier_id
    ]
    assert restored.layers[first.layer_id].modifier_ids == [
        shape_blur.modifier_id
    ]
    assert restored.layers[first.layer_id].transform_quad == pytest.approx(
        first.transform_quad
    )


def test_multi_move_is_atomic_and_preserves_supplied_front_to_back_order():
    chapter, _page, first, second, raster, vector = _document()
    stationary = chapter.add_object(
        second.layer_id, RasterObject(name="Stationary"), index=0
    )
    chapter.move_entities([
        ("object", vector.object_id), ("object", raster.object_id),
    ], second.layer_id, 0)
    assert [child.entity_id for child in second.children] == [
        vector.object_id, raster.object_id, stationary.object_id,
    ]
    assert first.children == []
    assert vector.parent_layer_id == second.layer_id
    assert raster.parent_layer_id == second.layer_id

    before = chapter.to_dict()
    with pytest.raises(ValueError):
        chapter.move_entities(
            [("layer", first.layer_id), ("object", raster.object_id)],
            second.layer_id, 0,
        )
    assert chapter.to_dict() == before


def test_assets_clone_modifier_identity_and_preserve_only_internal_sharing():
    chapter, page, first, _second, raster, vector = _document()
    shared = HueSaturationLightnessModifier(hue=25)
    chapter.add_modifier(shared, [
        ("layer", first.layer_id),
        ("object", raster.object_id),
        ("object", vector.object_id),
    ])
    manifest, asset_tiles = extract_asset(
        chapter, TileStore(), "layer", first.layer_id, "Styled"
    )
    asset_ids = set(manifest.document.modifiers)
    assert len(asset_ids) == 1
    asset_modifier_id = next(iter(asset_ids))
    assert asset_modifier_id != shared.modifier_id
    assert manifest.document.layers[first.layer_id].modifier_ids == [
        asset_modifier_id
    ]
    assert manifest.document.objects[raster.object_id].modifier_ids == [
        asset_modifier_id
    ]

    target = ChapterDocument(name="Target")
    target_page = target.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 600, 600)
    )
    target_layer = target.add_layer(
        target_page.layer_id, "Destination",
        BoundGeometry.rectangle(0, 0, 600, 600),
    )
    _kind, cloned_root, cloned_objects = instantiate_asset(
        manifest, asset_tiles, target, TileStore(), target_layer.layer_id,
        300, 300,
    )
    cloned_ids = set(target.modifiers)
    assert len(cloned_ids) == 1
    cloned_modifier_id = next(iter(cloned_ids))
    assert cloned_modifier_id not in {shared.modifier_id, asset_modifier_id}
    assert target.layers[cloned_root].modifier_ids == [cloned_modifier_id]
    assert all(
        target.objects[object_id].modifier_ids == [cloned_modifier_id]
        for object_id in cloned_objects
        if isinstance(
            target.objects[object_id], (RasterObject, VectorDrawingObject)
        )
    )


def test_asset_extraction_rejects_external_mask_contributor():
    chapter, _page, first, second, raster, _vector = _document()
    outside = chapter.add_object(second.layer_id, RasterObject(name="Outside"))
    mask = ToneMask(contributors=[("object", outside.object_id)])
    chapter.masks[mask.mask_id] = mask
    raster.opacity_locked = False
    raster.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)

    with pytest.raises(ValueError, match="outside the copied subtree"):
        extract_asset(
            chapter, TileStore(), "layer", first.layer_id, "Masked"
        )


def test_hsl_intensity_and_premultiplied_blur_pixels():
    image = QImage(9, 9, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.setPixelColor(4, 4, QColor(255, 0, 0, 255))

    shifted = apply_modifier_stack(
        image, [HueSaturationLightnessModifier(hue=120)], (0, 0)
    )
    center = shifted.pixelColor(4, 4)
    assert center.green() > 245
    assert center.red() < 10
    assert center.alpha() == 255

    untouched = apply_modifier_stack(
        image, [HueSaturationLightnessModifier(hue=120, intensity=0)],
        (0, 0),
    )
    assert untouched == image

    blurred = apply_modifier_stack(
        image, [BlurModifier(strength=2)], (0, 0)
    )
    assert 0 < blurred.pixelColor(3, 4).alpha() < 255
    assert blurred.pixelColor(4, 4).red() > 0


def test_parameter_masks_round_trip_share_contents_but_keep_endpoints():
    chapter, _page, first, _second, raster, vector = _document()
    mask = ToneMask(
        name="Shared", saved=True,
        contributors=[("object", raster.object_id)],
    )
    chapter.masks[mask.mask_id] = mask
    modifier = HueSaturationLightnessModifier(hue=120)
    modifier.parameter_masks["hue"] = ParameterMaskBinding(
        mask.mask_id, -60, 120
    )
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    vector.opacity_locked = False
    vector.opacity_mask = ParameterMaskBinding(mask.mask_id, .15, .8)

    restored = ChapterDocument.from_dict(chapter.to_dict())
    restored_mask = restored.masks[mask.mask_id]
    restored_hue = restored.modifiers[modifier.modifier_id].parameter_masks["hue"]
    restored_opacity = restored.objects[vector.object_id].opacity_mask
    assert restored_mask.saved and restored_mask.name == "Shared"
    assert restored_mask.contributors == [("object", raster.object_id)]
    assert (restored_hue.black_value, restored_hue.white_value) == (-60, 120)
    assert (restored_opacity.black_value, restored_opacity.white_value) == (.15, .8)


def test_masked_hue_interpolates_and_outline_is_outside_only():
    image = QImage(9, 3, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.setPixelColor(3, 1, QColor(255, 0, 0, 255))
    image.setPixelColor(5, 1, QColor(255, 0, 0, 255))
    mask_id = "mask"
    hue = HueSaturationLightnessModifier(hue=120)
    hue.parameter_masks["hue"] = ParameterMaskBinding(mask_id, 0, 120)
    field = np.zeros((3, 9), dtype=np.float32)
    field[:, 5:] = 1.0
    shifted = apply_modifier_stack(
        image, [hue], (0, 0), {(hue.modifier_id, "hue"): field}
    )
    assert shifted.pixelColor(3, 1).red() > 245
    assert shifted.pixelColor(5, 1).green() > 245

    outlined = apply_modifier_stack(
        image, [OutlineModifier(thickness=2, color="#FF0000FF")], (0, 0)
    )
    assert outlined.pixelColor(3, 1).red() == 255
    assert outlined.pixelColor(2, 1).blue() > 0
    assert outlined.pixelColor(2, 1).alpha() > 0


def test_object_modifier_render_cache_updates_pixels_and_reuses_result(
    qapp, monkeypatch,
):
    chapter = ChapterDocument(name="Render modifiers", height=400)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 400)
    )
    layer = chapter.add_layer(
        page.layer_id, "Layer", BoundGeometry.rectangle(0, 0, 300, 300)
    )
    raster = chapter.add_object(
        layer.layer_id,
        RasterObject(interaction_rect=(0, 0, 80, 80)),
    )
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(30, 30), 28, QColor("#FFFF0000")
    )
    chapter.add_modifier(
        HueSaturationLightnessModifier(hue=120),
        [("object", raster.object_id)],
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    calls = 0
    original = canvas_module.apply_modifier_stack

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas_module, "apply_modifier_stack", counted)
    for _ in range(2):
        image = QImage(
            1080, 400, QImage.Format.Format_ARGB32_Premultiplied
        )
        image.fill(Qt.GlobalColor.transparent)
        canvas.render_preview(image)
    pixel = image.pixelColor(30, 30)
    assert pixel.green() > 230
    assert pixel.red() < 20
    assert calls == 1
    assert 0 < canvas._modifier_render_cache_bytes <= 64 * 1024 * 1024


def test_group_and_shape_transform_persist_projective_quads(qapp):
    chapter, _page, first, _second, raster, vector = _document()
    raster.interaction_rect = (0, 0, 40, 30)
    # Give the vector a real bound without coupling this test to stroke input.
    vector.transform_frame = (90, 80, 30, 30)
    vector.transform_quad = [(90, 80), (120, 80), (120, 110), (90, 110)]
    canvas = CanvasWidget(EditorSettings(snap_to_grid=False))
    canvas.resize(900, 700)
    canvas.set_document(chapter, TileStore())

    canvas.set_selection_set([
        ("object", raster.object_id), ("object", vector.object_id),
    ], ("object", vector.object_id))
    assert canvas.tool == ToolKind.TRANSFORM
    cage = canvas._multi_selection_cage()
    center = QRectF(
        QPointF(*cage[0]), QPointF(*cage[2])
    ).center()
    press = center + QPointF(22, 0)
    assert canvas._begin_multi_transform(press)
    canvas._update_multi_transform_preview(press + QPointF(35, 20))
    canvas._commit_geometry_transform()
    assert chapter.objects[raster.object_id].transform_quad is not None
    assert chapter.objects[vector.object_id].transform_quad is not None
    assert canvas.command_stack.can_undo

    canvas.set_selection("layer", first.layer_id)
    canvas.set_tool(ToolKind.TRANSFORM)
    shape_center = canvas.entity_world_rect(
        "layer", first.layer_id
    ).center()
    shape_press = shape_center + QPointF(25, 0)
    assert canvas._begin_geometry_transform(shape_press)
    canvas._update_geometry_transform_preview(shape_press + QPointF(15, 10))
    canvas._commit_geometry_transform()
    assert first.transform_frame is not None
    assert first.transform_quad is not None
    assert first.transform_quad[0][0] == pytest.approx(35)
    assert first.transform_quad[0][1] == pytest.approx(30)
    local_point = QPointF(5, 6)
    world_point = canvas._raster_world_point(raster, local_point)
    restored_local = canvas._raster_local_point(raster, world_point)
    assert restored_local.toTuple() == pytest.approx(local_point.toTuple())
    assert canvas._point_inside_layer_masks(
        raster.parent_layer_id, world_point, raster
    )

    canvas.set_selection("layer", first.layer_id)
    canvas.set_tool(ToolKind.SHAPE_EDIT)
    node_world = canvas.layer_world_transform(first.layer_id).map(
        QPointF(*first.bound.nodes[0].position)
    )
    canvas._update_shape_hover(node_world)
    assert canvas._shape_hover_target is not None


def test_outliner_multiselect_routes_tools_and_adds_one_shared_modifier(qapp):
    chapter, _page, first, _second, raster, vector = _document()
    text = chapter.add_object(first.layer_id, TextObject(text="Single only"))
    window = MainWindow()
    window._set_chapter(chapter, TileStore())
    try:
        selection = window.tree.selectionModel()
        raster_index = window.hierarchy_model.index_for_entity(
            "object", raster.object_id
        )
        vector_index = window.hierarchy_model.index_for_entity(
            "object", vector.object_id
        )
        selection.select(
            raster_index,
            QItemSelectionModel.SelectionFlag.ClearAndSelect
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        selection.setCurrentIndex(
            vector_index, QItemSelectionModel.SelectionFlag.NoUpdate
        )
        selection.select(
            vector_index,
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        qapp.processEvents()
        assert set(window.canvas.selected_entities) == {
            ("object", raster.object_id), ("object", vector.object_id),
        }
        assert window.canvas.tool == ToolKind.TRANSFORM
        assert window.tool_buttons[ToolKind.TRANSFORM].isVisibleTo(window)
        assert not window.tool_buttons[ToolKind.RASTER_PENCIL].isVisibleTo(window)

        window.modifier_controls.add_modifier("hsl")
        modifier_ids = set(chapter.modifiers)
        assert len(modifier_ids) == 1
        modifier_id = next(iter(modifier_ids))
        assert raster.modifier_ids == [modifier_id]
        assert vector.modifier_ids == [modifier_id]
        assert window.modifier_controls.common_ids() == [modifier_id]

        # An unsupported row deliberately collapses ordinary multi-selection.
        text_index = window.hierarchy_model.index_for_entity(
            "object", text.object_id
        )
        selection.select(
            text_index,
            QItemSelectionModel.SelectionFlag.Select
            | QItemSelectionModel.SelectionFlag.Rows,
        )
        window.tree.setCurrentIndex(text_index)
        qapp.processEvents()
        assert window.canvas.selected_entities == [
            ("object", text.object_id)
        ]
    finally:
        window.deleteLater()


def test_mask_ui_attachment_contributors_and_document_paint_are_undoable(qapp):
    chapter, _page, _first, _second, raster, vector = _document()
    window = MainWindow()
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", raster.object_id)
        context = (
            "opacity", "object", raster.object_id,
            0.0, 100.0, 0.0, 100.0,
        )
        window._request_parameter_mask(context)
        binding = raster.opacity_mask
        assert binding is not None
        assert (binding.black_value, binding.white_value) == (0.0, 1.0)
        assert window.canvas.active_tone_mask_id == binding.mask_id
        assert window._toggle_mask_contributor("object", vector.object_id)
        assert chapter.masks[binding.mask_id].contributors == [
            ("object", vector.object_id)
        ]
        assert window._finish_mask_mode(True)
        window.canvas.command_stack.undo()
        assert chapter.masks[binding.mask_id].contributors == []
        window.canvas.command_stack.redo()
        assert chapter.masks[binding.mask_id].contributors == [
            ("object", vector.object_id)
        ]

        window.canvas.set_tone_mask_mode(binding.mask_id)
        window.canvas.set_tool(ToolKind.RASTER_PENCIL)
        window.canvas._begin_mask_stroke(QPointF(75, 85), 1.0)
        window.canvas._end_mask_stroke()
        assert list(window.canvas.tiles.iter_tiles(binding.mask_id))
        window.canvas.command_stack.undo()
        assert not list(window.canvas.tiles.iter_tiles(binding.mask_id))
    finally:
        window.deleteLater()


def test_tone_mask_contributor_uses_transformed_base_alpha(qapp):
    chapter, _page, _first, _second, raster, _vector = _document()
    raster.interaction_rect = (0, 0, 30, 30)
    mask = ToneMask(contributors=[("object", raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    tiles = TileStore()
    tiles.paint_dab(raster.object_id, QPointF(10, 10), 12, QColor("black"))
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    field = canvas.render_tone_mask_field(
        mask.mask_id, 600, 600, canvas_module.QTransform(),
        QRectF(0, 0, 600, 600),
    )
    assert field[60, 50] > .9
    assert field[100, 100] == 0


def test_shape_opacity_mask_replaces_scalar_opacity_in_isolated_pass(qapp):
    chapter, _page, first, _second, _raster, _vector = _document()
    first.fill_color = "#FFFF0000"
    first.opacity = .2
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    first.opacity_mask = ParameterMaskBinding(mask.mask_id, 0, 1)
    tiles = TileStore()
    tiles.paint_dab(mask.mask_id, QPointF(60, 60), 24, QColor("white"))
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    image = QImage(300, 300, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.transparent)
    painter = QPainter(image)
    canvas._render_layer(painter, first, 1.0, QRectF(0, 0, 300, 300))
    painter.end()
    assert image.pixelColor(60, 60).alpha() > 245
    assert image.pixelColor(120, 120).alpha() == 0


def test_outliner_mouse_selection_is_deferred_until_release(qapp):
    chapter, _page, _first, _second, raster, vector = _document()
    window = MainWindow()
    window._set_chapter(chapter, TileStore())
    try:
        window.show()
        window.canvas.set_selection("object", raster.object_id)
        qapp.processEvents()
        index = window.hierarchy_model.index_for_entity(
            "object", vector.object_id
        )
        point = window.tree.visualRect(index).center()
        QTest.mousePress(window.tree.viewport(), Qt.LeftButton, pos=point)
        assert window.canvas.selected_id == raster.object_id
        QTest.mouseRelease(window.tree.viewport(), Qt.LeftButton, pos=point)
        qapp.processEvents()
        assert window.canvas.selected_id == vector.object_id
    finally:
        window.deleteLater()


def test_modifier_link_mode_toggles_shape_and_is_one_undoable_change(qapp):
    chapter, _page, first, _second, raster, _vector = _document()
    modifier = BlurModifier(mode="focal")
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    window = MainWindow()
    window._set_chapter(chapter, TileStore())
    try:
        window.canvas.set_selection("object", raster.object_id)
        controls = window.modifier_controls
        controls.toggle_link_mode(modifier.modifier_id)
        assert controls.toggle_link_target("layer", first.layer_id)
        controls.commit_link_mode()
        assert set(chapter.modifier_target_ids(modifier.modifier_id)) == {
            ("object", raster.object_id), ("layer", first.layer_id),
        }
        assert window.canvas.command_stack.can_undo
        window.canvas.command_stack.undo()
        assert window.canvas.chapter.modifier_target_ids(
            modifier.modifier_id
        ) == [("object", raster.object_id)]
    finally:
        window.deleteLater()
