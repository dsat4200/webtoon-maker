from __future__ import annotations

import json

import pytest

from comic_editor.core.commands import ObjectPatchCommand
from comic_editor.core.models import (
    BoundGeometry,
    ChapterDocument,
    ColorPalette,
    PaletteSwatch,
    SeriesDocument,
    VectorDrawingObject,
    VectorFillObject,
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


def test_vector_drawing_and_fill_round_trip_with_owned_hierarchy():
    chapter, _page, layer, drawing = _vector_chapter()
    fill = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            geometry=BoundGeometry.rectangle(20, 30, 40, 50),
            fill_color="#445566",
            source_seed=(35, 40),
            fill_settings={"close_gaps": True, "gap": 8},
        ),
    )
    chapter.validate()

    assert all(
        reference.entity_id != fill.object_id
        for reference in layer.children
    )
    assert drawing.fill_child_ids == [fill.object_id]
    assert fill.parent_layer_id == layer.layer_id
    assert fill.fill_color == "#FF445566"
    assert drawing.strokes[0].color == "#80336699"
    assert drawing.derived_bounds() == pytest.approx((4, 4, 92, 92))

    restored = ChapterDocument.from_dict(chapter.to_dict())
    restored_drawing = restored.objects[drawing.object_id]
    restored_fill = restored.objects[fill.object_id]
    assert restored.schema_version == 13
    assert isinstance(restored_drawing, VectorDrawingObject)
    assert isinstance(restored_fill, VectorFillObject)
    assert restored_drawing.fill_child_ids == [fill.object_id]
    assert restored_fill.owner_drawing_id == drawing.object_id
    assert restored_fill.geometry.bbox() == (20, 30, 40, 50)
    assert restored.to_dict() == chapter.to_dict()


def test_vector_fill_move_reorder_and_cascade_delete():
    chapter, page, layer, drawing = _vector_chapter()
    first = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            name="First", geometry=BoundGeometry.rectangle(0, 0, 20, 20),
        ),
    )
    second = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            name="Second", geometry=BoundGeometry.rectangle(30, 0, 20, 20),
        ),
    )
    chapter.move_entity("object", second.object_id, drawing.object_id, 0)
    assert drawing.fill_child_ids == [second.object_id, first.object_id]

    other_layer = chapter.add_layer(page.layer_id, "Other")
    chapter.move_entity("object", drawing.object_id, other_layer.layer_id, 0)
    assert drawing.parent_layer_id == other_layer.layer_id
    assert first.parent_layer_id == other_layer.layer_id
    assert second.parent_layer_id == other_layer.layer_id
    chapter.validate()

    deleted = chapter.delete_entity("object", drawing.object_id)
    assert deleted == {drawing.object_id, first.object_id, second.object_id}
    assert not deleted.intersection(chapter.objects)
    assert all(
        reference.entity_id != drawing.object_id
        for reference in other_layer.children
    )


def test_vector_fill_cannot_be_layer_child_or_change_owner():
    chapter, page, layer, drawing = _vector_chapter()
    fill = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(geometry=BoundGeometry.rectangle(0, 0, 10, 10)),
    )
    other = chapter.add_object(
        layer.layer_id, VectorDrawingObject(name="Other")
    )
    with pytest.raises(ValueError, match="within their owner"):
        chapter.move_entity("object", fill.object_id, other.object_id, 0)

    layer.children.append(type(layer.children[0])("object", fill.object_id))
    with pytest.raises(ValueError, match="Invalid child object"):
        chapter.validate()


def test_render_order_places_owned_fills_behind_drawing_strokes():
    chapter, _page, _layer, drawing = _vector_chapter()
    front = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            name="Front", geometry=BoundGeometry.rectangle(0, 0, 10, 10),
        ),
    )
    back = chapter.add_vector_fill(
        drawing.object_id,
        VectorFillObject(
            name="Back", geometry=BoundGeometry.rectangle(0, 0, 20, 20),
        ),
    )
    vector_ids = [
        entity.object_id
        for kind, entity in chapter.iter_render_order()
        if kind == "object"
    ]
    assert vector_ids == [back.object_id, front.object_id, drawing.object_id]


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
    assert restored.schema_version == 13
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
