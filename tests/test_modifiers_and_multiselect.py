from __future__ import annotations

import pytest
from PySide6.QtCore import QItemSelectionModel, QPointF, QRectF, Qt
from PySide6.QtGui import QColor, QImage

from comic_editor.core.assets import extract_asset, instantiate_asset
from comic_editor.core.models import (
    BlurModifier, BoundGeometry, ChapterDocument,
    HueSaturationLightnessModifier, RasterObject, TextObject,
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
    assert restored.schema_version == 17
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
