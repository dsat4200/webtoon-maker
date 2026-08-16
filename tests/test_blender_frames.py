from __future__ import annotations

import math

import pytest

from comic_editor.core.models import (
    BlenderViewObject,
    BlenderViewportState,
    BoundGeometry,
    ChapterDocument,
    PathNode,
    RasterObject,
)
from comic_editor.core.assets import extract_asset
from comic_editor.core.tiles import TileStore


def framed_chapter() -> tuple[ChapterDocument, str, BlenderViewObject]:
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    shape = chapter.add_layer(
        page.layer_id, "3D panel", BoundGeometry.rectangle(10, 20, 400, 300)
    )
    frame = BlenderViewObject()
    chapter.add_object(shape.layer_id, frame)
    return chapter, shape.layer_id, frame


def test_blender_view_state_round_trip_and_clamping():
    chapter, _, frame = framed_chapter()
    frame.view_state = BlenderViewportState(
        rotation=(2.0, 0.0, 0.0, 0.0),
        location=(1.0, 2.0, 3.0),
        distance=12.5,
        perspective="ORTHO",
        lens=999.0,
        camera_zoom=-99.0,
        camera_offset=(0.25, -0.5),
    )

    restored = ChapterDocument.from_dict(chapter.to_dict())
    candidate = restored.objects[frame.object_id]

    assert isinstance(candidate, BlenderViewObject)
    assert candidate.view_state is not None
    assert candidate.view_state.rotation == (1.0, 0.0, 0.0, 0.0)
    assert candidate.view_state.location == (1.0, 2.0, 3.0)
    assert candidate.view_state.perspective == "ORTHO"
    assert candidate.view_state.lens == 250.0
    assert candidate.view_state.camera_zoom == -30.0
    assert restored.schema_version == 16


def test_blender_view_state_rejects_non_finite_values():
    state = BlenderViewportState(location=(math.nan, 0.0, 0.0))
    with pytest.raises(ValueError, match="finite"):
        state.validate()


@pytest.mark.parametrize("parent_kind", ["page", "open_shape", "asset"])
def test_blender_frames_require_chapter_bounded_non_page_shape(parent_kind):
    chapter = ChapterDocument(
        width=300 if parent_kind == "asset" else 1080,
        document_kind="asset" if parent_kind == "asset" else "chapter",
    )
    page = chapter.add_page("Page")
    if parent_kind == "page":
        parent_id = page.layer_id
    else:
        parent = chapter.add_layer(
            page.layer_id,
            "Shape",
            BoundGeometry.path(
                [PathNode(x=0, y=0), PathNode(x=100, y=0)], closed=False
            )
            if parent_kind == "open_shape"
            else BoundGeometry.rectangle(0, 0, 100, 100),
            layer_kind="open_shape" if parent_kind == "open_shape" else "bounded",
        )
        parent_id = parent.layer_id

    with pytest.raises(ValueError, match="closed non-page bounded shape"):
        chapter.add_object(parent_id, BlenderViewObject())


def test_one_blender_frame_per_shape_and_background_ordering():
    chapter, shape_id, frame = framed_chapter()
    raster = RasterObject(name="Foreground")
    chapter.add_object(shape_id, raster)

    assert chapter.layers[shape_id].children[-1].entity_id == frame.object_id
    assert chapter.layers[shape_id].children[-2].entity_id == raster.object_id
    with pytest.raises(ValueError, match="already has"):
        chapter.add_object(shape_id, BlenderViewObject())
    chapter.validate()


def test_moving_frame_reparents_to_back_and_rejects_occupied_shape():
    chapter, first_shape_id, frame = framed_chapter()
    page_id = chapter.root_page_ids[0]
    second = chapter.add_layer(
        page_id, "Second", BoundGeometry.rectangle(0, 0, 200, 200)
    )
    third = chapter.add_layer(
        page_id, "Third", BoundGeometry.rectangle(0, 0, 200, 200)
    )
    other = BlenderViewObject()
    chapter.add_object(third.layer_id, other)

    chapter.move_entity("object", frame.object_id, second.layer_id, 0)
    assert chapter.blender_view_for_layer(first_shape_id) is None
    assert chapter.layers[second.layer_id].children[-1].entity_id == frame.object_id

    with pytest.raises(ValueError, match="already has"):
        chapter.move_entity("object", frame.object_id, third.layer_id, 0)


def test_deleting_parent_shape_cascades_blender_frame():
    chapter, shape_id, frame = framed_chapter()
    deleted = chapter.delete_entity("layer", shape_id)
    assert frame.object_id in deleted
    assert frame.object_id not in chapter.objects


def test_asset_extraction_rejects_blender_frame_subtree():
    chapter, shape_id, _frame = framed_chapter()
    with pytest.raises(ValueError, match="cannot be stored as reusable assets"):
        extract_asset(chapter, TileStore(), "layer", shape_id, "3D panel")
