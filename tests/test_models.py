from __future__ import annotations

import pytest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, GridSettings, PathNode, RasterObject,
    SeriesDocument, TextObject, VectorDrawingObject,
)


def populated_chapter():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    layer = chapter.add_layer(page.layer_id, "Layer")
    raster = chapter.add_object(layer.layer_id, RasterObject())
    return chapter, page, layer, raster


def test_series_color_history_migrates_deduplicates_and_caps():
    data = SeriesDocument(name="Colors").to_dict()
    data["schema_version"] = 16
    data["color_history"] = [
        "#112233", "#FF112233", *(
            f"#FF{index:06X}" for index in range(30)
        ),
    ]
    loaded = SeriesDocument.from_dict(data)
    assert loaded.schema_version == 17
    assert loaded.color_history[0] == "#FF112233"
    assert len(loaded.color_history) == 24
    assert len(set(loaded.color_history)) == 24


def test_shape_style_thickness_values_are_integer_pixels():
    from comic_editor.core.models import ShapeStyle

    style = ShapeStyle(base_thickness=8.49, outline_thickness=3.5)
    style.validate()
    assert style.base_thickness == 8
    assert style.outline_thickness == 4

    style = ShapeStyle(base_thickness=0.2, outline_thickness=900)
    style.validate()
    assert style.base_thickness == 0
    assert style.outline_thickness == 500


def test_page_and_object_parent_invariants():
    chapter, page, layer, raster = populated_chapter()
    chapter.validate()
    text = chapter.add_object(page.layer_id, TextObject())
    direct_raster = chapter.add_object(page.layer_id, RasterObject())
    chapter.move_entity(
        "object", raster.object_id, page.layer_id, len(page.children)
    )
    chapter.validate()
    loaded = ChapterDocument.from_dict(chapter.to_dict())
    assert loaded.objects[text.object_id].parent_layer_id == page.layer_id
    assert (
        loaded.objects[direct_raster.object_id].parent_layer_id
        == page.layer_id
    )
    assert loaded.objects[raster.object_id].parent_layer_id == page.layer_id
    with pytest.raises(ValueError, match="Page layers"):
        chapter.move_entity("layer", page.layer_id, layer.layer_id, 0)


def test_mixed_children_round_trip_and_stable_ids():
    chapter, page, layer, raster = populated_chapter()
    nested = chapter.add_layer(layer.layer_id, "Nested", BoundGeometry.circle(100, 100, 80))
    text = chapter.add_object(layer.layer_id, TextObject(text="Hello"))
    order = [(item.kind, item.entity_id) for item in layer.children]
    loaded = ChapterDocument.from_dict(chapter.to_dict())
    assert [(item.kind, item.entity_id) for item in loaded.layers[layer.layer_id].children] == order
    assert loaded.objects[text.object_id].text == "Hello"
    assert loaded.layers[nested.layer_id].bound.kind == "circle"


def test_add_object_optional_index_preserves_mixed_child_order():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id)
    first = chapter.add_object(layer.layer_id, RasterObject(name="First"))
    last = chapter.add_object(layer.layer_id, TextObject(text="Last"))
    inserted = chapter.add_object(
        layer.layer_id, TextObject(text="Inserted"), index=1
    )
    assert [
        (reference.kind, reference.entity_id)
        for reference in layer.children
    ] == [
        ("object", first.object_id),
        ("object", inserted.object_id),
        ("object", last.object_id),
    ]
    restored = ChapterDocument.from_dict(chapter.to_dict())
    assert [
        reference.entity_id
        for reference in restored.layers[layer.layer_id].children
    ] == [first.object_id, inserted.object_id, last.object_id]


def test_serialized_root_page_order_is_an_independent_snapshot():
    chapter = ChapterDocument()
    first = chapter.add_page("First")
    snapshot = chapter.to_dict()
    chapter.add_page("Second")
    assert snapshot["root_page_ids"] == [first.layer_id]
    ChapterDocument.from_dict(snapshot).validate()


def test_grid_and_opacity_inheritance():
    chapter, page, layer, raster = populated_chapter()
    chapter.grid = GridSettings(size=120, divisions=4)
    page.grid_override = GridSettings(size=80, divisions=2)
    assert chapter.effective_grid(layer.layer_id).size == 80
    layer.grid_override = GridSettings(size=24, divisions=3)
    assert chapter.effective_grid(layer.layer_id).divisions == 3
    page.opacity = 0.5
    chapter.set_layer_opacity(layer.layer_id, 0.4)
    assert raster.opacity == 0.4
    assert chapter.effective_object_opacity(raster.object_id) == pytest.approx(0.2)
    raster.opacity_locked = False
    raster.opacity = 0.25
    assert chapter.effective_object_opacity(raster.object_id) == pytest.approx(0.125)


def test_bound_edit_is_separate_from_translation():
    chapter, page, layer, raster = populated_chapter()
    original = list(page.bound.points)
    layer.translate_x = 20
    layer.translate_y = 30
    assert page.bound.points == original
    assert chapter.layer_world_translation(layer.layer_id) == (20, 30)
    page.translate_y = 4000
    chapter.ensure_height_for(page.layer_id)
    assert chapter.height >= 5080


def test_free_page_overlap_order_and_reorder():
    chapter = ChapterDocument()
    first = chapter.add_page("First", y=0)
    second = chapter.add_page("Second", y=200)
    chapter.move_entity("layer", first.layer_id, None, 2)
    assert chapter.root_page_ids == [second.layer_id, first.layer_id]
    chapter.validate()


def test_trim_refuses_visible_page_loss():
    chapter = ChapterDocument()
    chapter.add_page(y=2000)
    with pytest.raises(ValueError, match="shorter"):
        chapter.trim_height(1000)


def test_validation_preserves_independent_vector_roundness():
    chapter = ChapterDocument()
    page = chapter.add_page()
    nodes = [
        PathNode(
            x=0, y=0, roundness=0, roundness_enabled=True,
        ),
        PathNode(
            x=200, y=0, roundness=0.25, roundness_enabled=True,
        ),
        PathNode(
            x=200, y=200, roundness=80, roundness_enabled=False,
        ),
    ]
    layer = chapter.add_layer(
        page.layer_id, "Independent corners",
        BoundGeometry.path(nodes, True),
    )

    chapter.validate()
    assert [
        (node.roundness, node.roundness_enabled)
        for node in layer.bound.nodes
    ] == [(0, True), (0.25, True), (80, False)]

    data = chapter.to_dict()
    loaded = ChapterDocument.from_dict(data)
    assert [
        (node.roundness, node.roundness_enabled)
        for node in loaded.layers[layer.layer_id].bound.nodes
    ] == [(0, True), (0.25, True), (80, False)]


@pytest.mark.parametrize("value", [-1, float("inf"), float("nan")])
def test_invalid_roundness_clamps_to_sharp_zero(value):
    node = PathNode(roundness=value, roundness_enabled=True)
    node.validate()
    assert node.roundness == 0
    assert node.roundness_enabled is True


def test_legacy_text_migrates_to_editable_free_quad():
    chapter, page, layer, raster = populated_chapter()
    text = chapter.add_object(layer.layer_id, TextObject(text="Legacy", x=10, y=20))
    data = chapter.to_dict()
    data["schema_version"] = 1
    item = next(item for item in data["objects"] if item["id"] == text.object_id)
    item["alignment_mode"] = "layer"
    for key in (
        "layout_mode", "horizontal_alignment", "vertical_alignment",
        "margin", "transform_quad",
    ):
        item.pop(key, None)
    loaded = ChapterDocument.from_dict(data)
    migrated = loaded.objects[text.object_id]
    assert loaded.schema_version == 17
    assert migrated.layout_mode == "free"
    assert len(migrated.transform_quad) == 4
    assert migrated.text == "Legacy"


def test_ignore_direct_parent_mask_round_trips_for_layers_and_objects():
    chapter, _page, layer, raster = populated_chapter()
    layer.ignore_parent_mask = True
    raster.ignore_parent_mask = True

    loaded = ChapterDocument.from_dict(chapter.to_dict())

    assert loaded.layers[layer.layer_id].ignore_parent_mask is True
    assert loaded.objects[raster.object_id].ignore_parent_mask is True


def test_drawing_underlay_migrates_round_trips_and_clamps():
    chapter, _page, layer, raster = populated_chapter()
    vector = chapter.add_object(layer.layer_id, VectorDrawingObject())
    raster.underlay_opacity = 0.375
    vector.underlay_opacity = 2.0

    loaded = ChapterDocument.from_dict(chapter.to_dict())
    assert loaded.schema_version == 17
    assert loaded.objects[raster.object_id].underlay_opacity == pytest.approx(
        0.375
    )
    assert loaded.objects[vector.object_id].underlay_opacity == 1.0

    data = chapter.to_dict()
    data["schema_version"] = 8
    for item in data["objects"]:
        item.pop("underlay_opacity", None)
    migrated = ChapterDocument.from_dict(data)
    assert all(
        obj.underlay_opacity == 0.0
        for obj in migrated.objects.values()
    )
