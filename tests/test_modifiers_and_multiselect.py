from __future__ import annotations

import pytest
import numpy as np
from PySide6.QtCore import QItemSelectionModel, QPoint, QPointF, QRectF, Qt
from PySide6.QtGui import (
    QColor, QContextMenuEvent, QImage, QPainter, QTransform,
)
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
from comic_editor.ui.mask_controls import MaskAlphaSlider, MaskButton
from comic_editor.ui.modifier_rendering import (
    BlurPyramidCache, OutlineDistanceCache, apply_modifier_stack,
)
from comic_editor.ui.tool_ribbon_pages import ToolSettingsControls


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
    assert restored.schema_version == 20
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
    chapter, _page, first, second, raster, vector = _document()
    controller = chapter.add_object(
        second.layer_id, RasterObject(name="Mask source")
    )
    mask = ToneMask(
        name="Shared", saved=True,
        contributors=[("object", controller.object_id)],
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
    assert restored_mask.contributors == [("object", controller.object_id)]
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


def test_exact_outline_distance_is_reused_for_warmed_parameter_changes():
    image = QImage(9, 9, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    image.setPixelColor(4, 4, QColor("white"))
    cache = OutlineDistanceCache()

    first = apply_modifier_stack(
        image, [OutlineModifier(thickness=1.2, color="#FFFF0000")],
        (0, 0), outline_distance_cache=cache,
    )
    second = apply_modifier_stack(
        image, [OutlineModifier(thickness=3.0, color="#800000FF")],
        (0, 0), outline_distance_cache=cache,
    )

    assert cache.computations == 1
    assert first.pixelColor(5, 4).alpha() > first.pixelColor(5, 5).alpha()
    assert second.pixelColor(7, 4).blue() > 0
    assert second.pixelColor(7, 4).alpha() < 255


def test_blue_mask_overlay_has_zero_to_thirty_five_percent_alpha(qapp):
    values = np.array([[0.0, 0.5, 1.0]], dtype=np.float32)
    overlay = CanvasWidget._blue_mask_image(values)

    assert overlay.pixelColor(0, 0).alpha() == 0
    assert overlay.pixelColor(1, 0).alpha() in {44, 45}
    assert overlay.pixelColor(2, 0).alpha() == 89
    assert overlay.pixelColor(2, 0).name() == "#64b5f6"


def test_assigned_mask_context_menu_detaches_only_after_action(qapp):
    button = MaskButton()
    button.setChecked(True)
    detached = []
    button.detachRequested.connect(lambda: detached.append(True))
    position = QPoint(3, 3)
    event = QContextMenuEvent(
        QContextMenuEvent.Reason.Mouse, position,
        button.mapToGlobal(position),
    )

    button.contextMenuEvent(event)

    assert detached == []
    action = button._context_menu.actions()[0]
    assert action.text() == "Remove Mask"
    action.trigger()
    assert detached == [True]
    button._context_menu.close()


def test_mask_stroke_consumes_curve_and_touches_revision_once(qapp):
    chapter, _page, _first, _second, _raster, _vector = _document()
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, TileStore())
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    revision = mask.revision

    canvas._begin_mask_stroke(QPointF(20, 20), 0.25)
    canvas._continue_mask_stroke(QPointF(30, 35), 0.5)
    canvas._continue_mask_stroke(QPointF(42, 24), 0.75)
    canvas._continue_mask_stroke(QPointF(55, 40), 1.0)
    canvas._end_mask_stroke()

    assert mask.revision == revision + 1
    tile = canvas.tiles.tile(mask.mask_id, (0, 0))
    assert tile is not None
    assert tile.pixelColor(30, 35).alpha() > 0
    assert tile.pixelColor(42, 24).alpha() > 0
    canvas.command_stack.undo()
    assert canvas.tiles.tile(mask.mask_id, (0, 0)) is None


def test_mask_pencil_pressure_replaces_alpha_and_allows_inversion(qapp):
    chapter, _page, _first, _second, _raster, _vector = _document()
    mask = ToneMask()
    chapter.masks[mask.mask_id] = mask
    settings = EditorSettings(
        mask_pencil_pressure_sensitive=True,
        mask_pencil_from_alpha=0.0,
        mask_pencil_to_alpha=1.0,
    )
    canvas = CanvasWidget(settings)
    canvas.set_document(chapter, TileStore())
    canvas.set_tone_mask_mode(mask.mask_id)
    canvas.set_tool(ToolKind.RASTER_PENCIL)
    canvas._device_supports_pressure = True

    canvas._begin_mask_stroke(QPointF(30, 30), 1.0)
    canvas._end_mask_stroke()
    assert canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(30, 30).alpha() == 255

    canvas._begin_mask_stroke(QPointF(30, 30), 0.5)
    canvas._end_mask_stroke()
    assert canvas.tiles.tile(mask.mask_id, (0, 0)).pixelColor(30, 30).alpha() in {
        127, 128,
    }

    settings.mask_pencil_from_alpha = 1.0
    settings.mask_pencil_to_alpha = 0.0
    canvas._begin_mask_stroke(QPointF(30, 30), 1.0)
    canvas._end_mask_stroke()
    remaining = canvas.tiles.tile(mask.mask_id, (0, 0))
    assert remaining is None or remaining.pixelColor(30, 30).alpha() == 0


def test_mask_pencil_pressure_off_uses_to_and_retains_from(qapp):
    settings = EditorSettings(
        mask_pencil_pressure_sensitive=False,
        mask_pencil_from_alpha=0.2,
        mask_pencil_to_alpha=0.7,
    )
    controls = ToolSettingsControls(settings)
    controls.set_context(
        ToolKind.RASTER_PENCIL, vector_active=False, mask_active=True
    )
    assert controls.stack.currentWidget() is controls.mask_pencil_page
    assert controls.context_label.text() == "Mask Pencil"
    assert controls.mask_pencil_alpha.pressure_sensitive is False
    assert controls.mask_pencil_alpha.from_alpha == pytest.approx(0.2)
    assert controls.mask_pencil_alpha.to_alpha == pytest.approx(0.7)

    controls.mask_pencil_pressure.setChecked(True)
    assert settings.mask_pencil_pressure_sensitive is True
    assert settings.mask_pencil_from_alpha == pytest.approx(0.2)
    assert controls.mask_pencil_alpha.pressure_sensitive is True


def test_mask_alpha_slider_switches_between_one_and_two_handles(qapp):
    slider = MaskAlphaSlider(0.8, 0.2, True)
    slider.resize(240, 40)
    assert slider.pressure_sensitive is True
    slider.setPressureSensitive(False)
    slider.setValues(0.8, 0.6)
    assert slider.pressure_sensitive is False
    assert slider.from_alpha == pytest.approx(0.8)
    assert slider.to_alpha == pytest.approx(0.6)
    slider.setPressureSensitive(True)
    assert slider.from_alpha == pytest.approx(0.8)


def test_blur_pyramid_reuses_source_for_scalar_and_masked_strengths():
    image = QImage(65, 49, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    painter = QPainter(image)
    painter.fillRect(20, 12, 24, 25, QColor("#ff8040"))
    painter.end()
    cache = BlurPyramidCache()

    first = apply_modifier_stack(
        image, [BlurModifier(strength=3)], (0, 0),
        blur_pyramid_cache=cache,
    )
    modifier = BlurModifier(strength=30)
    mask_id = "vary"
    modifier.parameter_masks["strength"] = ParameterMaskBinding(
        mask_id, 1, 30
    )
    field = np.tile(np.linspace(0, 1, 65, dtype=np.float32), (49, 1))
    second = apply_modifier_stack(
        image, [modifier], (0, 0),
        {(modifier.modifier_id, "strength"): field},
        blur_pyramid_cache=cache,
    )

    assert cache.builds == 1
    assert cache.bytes <= 64 * 1024 * 1024
    assert first.pixelColor(19, 24).alpha() > 0
    assert second.pixelColor(15, 24).alpha() > 0

    changed = QImage(image)
    changed.setPixelColor(0, 0, QColor("white"))
    apply_modifier_stack(
        changed, [BlurModifier(strength=4)], (0, 0),
        blur_pyramid_cache=cache,
    )
    assert cache.builds == 2


def test_blur_pyramid_budget_omits_oversized_entries():
    image = QImage(32, 32, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(QColor("white"))
    cache = BlurPyramidCache(budget=64)
    apply_modifier_stack(
        image, [BlurModifier(strength=8)], (0, 0),
        blur_pyramid_cache=cache,
    )
    assert cache.bytes == 0
    assert not cache._values


def test_mask_paint_reuses_cached_contributor_coverage(qapp, monkeypatch):
    chapter, _page, _first, _second, raster, _vector = _document()
    mask = ToneMask(contributors=[("object", raster.object_id)])
    chapter.masks[mask.mask_id] = mask
    tiles = TileStore()
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    calls = 0
    original = canvas._render_base_mask_contributor

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas, "_render_base_mask_contributor", counted)
    visible = QRectF(0, 0, 100, 100)
    first = canvas.render_tone_mask_field(
        mask.mask_id, 100, 100, QTransform(), visible
    )
    tiles.paint_dab(
        mask.mask_id, QPointF(20, 20), 12, QColor("white")
    )
    second = canvas.render_tone_mask_field(
        mask.mask_id, 100, 100, QTransform(), visible
    )

    assert calls == 1
    assert first[20, 20] == 0
    assert second[20, 20] > 0


def test_canvas_outline_reuses_source_and_distance_for_property_changes(
    qapp, monkeypatch,
):
    chapter = ChapterDocument(name="Cached outline", height=300)
    page = chapter.add_page(
        "Page", BoundGeometry.rectangle(0, 0, 1080, 300)
    )
    layer = chapter.add_layer(
        page.layer_id, "Layer", BoundGeometry.rectangle(0, 0, 200, 200)
    )
    raster = chapter.add_object(
        layer.layer_id, RasterObject(interaction_rect=(0, 0, 60, 60))
    )
    modifier = OutlineModifier(thickness=4, color="#FFFF0000")
    chapter.add_modifier(modifier, [("object", raster.object_id)])
    tiles = TileStore()
    tiles.paint_dab(
        raster.object_id, QPointF(30, 30), 20, QColor("white")
    )
    canvas = CanvasWidget(EditorSettings())
    canvas.set_document(chapter, tiles)
    source_renders = 0
    original = canvas._render_object_content

    def counted(*args, **kwargs):
        nonlocal source_renders
        source_renders += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(canvas, "_render_object_content", counted)
    image = QImage(1080, 300, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)
    canvas.render_preview(image)
    modifier.thickness = 12
    modifier.color = "#800000FF"
    canvas.render_preview(image)

    assert source_renders == 1
    assert canvas._outline_distance_cache.computations == 1


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


def test_shape_opacity_mask_multiplies_scalar_opacity_in_isolated_pass(qapp):
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
    assert image.pixelColor(60, 60).alpha() == pytest.approx(51, abs=2)
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
