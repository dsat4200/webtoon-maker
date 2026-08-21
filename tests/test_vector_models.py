from __future__ import annotations

import json

import pytest

from comic_editor.core.commands import ObjectPatchCommand
from comic_editor.core.models import (
    ChapterDocument,
    ColorPalette,
    PaletteSwatch,
    RasterObject,
    SeriesDocument,
    VectorDrawingObject,
    VectorStroke,
    VectorStrokePoint,
    canonical_argb,
)
from comic_editor.core.persistence import SeriesRepository


def _vector_chapter():
    chapter = ChapterDocument()
    page = chapter.add_page()
    layer = chapter.add_layer(page.layer_id, "Vectors")
    drawing = chapter.add_object(
        layer.layer_id,
        VectorDrawingObject(
            strokes=[
                VectorStroke(
                    color="#80336699",
                    points=[
                        VectorStrokePoint(
                            x=10, y=20, outgoing=(30, 10), width=4, opacity=.5,
                        ),
                        VectorStrokePoint(
                            x=90, y=80, incoming=(70, 90), width=12,
                        ),
                    ],
                )
            ],
        ),
    )
    return chapter, page, layer, drawing


def test_vector_drawing_round_trip_has_no_persistent_fill_children():
    chapter, _page, _layer, drawing = _vector_chapter()
    chapter.validate()

    assert drawing.strokes[0].color == "#80336699"
    assert drawing.derived_bounds() == pytest.approx((4, 4, 92, 92))
    assert "fill_child_ids" not in drawing.to_dict()

    restored = ChapterDocument.from_dict(chapter.to_dict())
    restored_drawing = restored.objects[drawing.object_id]
    assert restored.schema_version == 21
    assert isinstance(restored_drawing, VectorDrawingObject)
    assert "fill_child_ids" not in restored_drawing.to_dict()
    assert restored.to_dict() == chapter.to_dict()


def test_vector_drawing_move_and_delete_remain_regular_hierarchy_operations():
    chapter, page, layer, drawing = _vector_chapter()
    other_layer = chapter.add_layer(page.layer_id, "Other")
    chapter.move_entity("object", drawing.object_id, other_layer.layer_id, 0)
    assert drawing.parent_layer_id == other_layer.layer_id
    chapter.validate()

    deleted = chapter.delete_entity("object", drawing.object_id)
    assert deleted == {drawing.object_id}
    assert not deleted.intersection(chapter.objects)
    assert all(
        reference.entity_id != drawing.object_id
        for reference in other_layer.children
    )


def test_render_order_places_raster_color_behind_vector_strokes():
    chapter, _page, layer, drawing = _vector_chapter()
    color = chapter.add_object(layer.layer_id, RasterObject(name="Color"))
    chapter.move_entity("object", color.object_id, layer.layer_id, 1)
    vector_ids = [
        entity.object_id
        for kind, entity in chapter.iter_render_order()
        if kind == "object"
    ]
    assert vector_ids == [color.object_id, drawing.object_id]


def test_series_colors_and_palettes_migrate_and_persist_atomically(tmp_path):
    assert canonical_argb("#abc") == "#FFAABBCC"
    assert canonical_argb("#80112233") == "#80112233"
    legacy = SeriesDocument.from_dict({
        "schema_version": 6,
        "id": "series",
        "name": "Legacy",
        "brush_color": "#123456",
        "chapters": [],
    })
    assert legacy.primary_color == "#FF123456"
    assert legacy.secondary_color == "#FFFFFFFF"
    assert [item.color for item in legacy.palettes[0].swatches] == [
        "#FF000000", "#FFFFFFFF",
    ]

    legacy.palettes.append(ColorPalette(
        name="Skin",
        swatches=[PaletteSwatch(color="#80fedcba")],
    ))
    legacy.active_palette_id = legacy.palettes[1].palette_id
    repository = SeriesRepository(tmp_path / "series")
    repository.root.mkdir()
    repository.save_series(legacy)
    restored = repository.load_series()
    assert restored.schema_version == 17
    assert restored.to_dict() == legacy.to_dict()
    on_disk = json.loads(repository.series_path.read_text(encoding="utf-8"))
    assert on_disk["primary_color"] == "#FF123456"
    assert on_disk["palettes"][1]["swatches"][0]["color"] == "#80FEDCBA"


def test_object_patch_command_preserves_live_vector_object_identity():
    chapter, _page, _layer, drawing = _vector_chapter()
    before = drawing.to_dict()
    drawing.strokes[0].points[0].width = 24
    drawing.touch_revision()
    after = drawing.to_dict()
    command = ObjectPatchCommand(
        "Vector edit", chapter,
        {drawing.object_id: before},
        {drawing.object_id: after},
    )

    command.undo()
    assert chapter.objects[drawing.object_id] is drawing
    assert drawing.strokes[0].points[0].width == 4
    command.redo()
    assert chapter.objects[drawing.object_id] is drawing
    assert drawing.strokes[0].points[0].width == 24
