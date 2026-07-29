from __future__ import annotations

import pytest

from comic_editor.core.models import (
    BoundGeometry, ChapterDocument, GridSettings, RasterObject, TextObject,
)


def populated_chapter():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    layer = chapter.add_layer(page.layer_id, "Layer")
    raster = chapter.add_object(layer.layer_id, RasterObject())
    return chapter, page, layer, raster


def test_page_and_object_parent_invariants():
    chapter, page, layer, raster = populated_chapter()
    chapter.validate()
    with pytest.raises(ValueError, match="directly"):
        chapter.add_object(page.layer_id, TextObject())
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
    assert loaded.schema_version == 4
    assert migrated.layout_mode == "free"
    assert len(migrated.transform_quad) == 4
    assert migrated.text == "Legacy"
