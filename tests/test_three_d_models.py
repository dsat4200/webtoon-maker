from __future__ import annotations

import pytest

from comic_editor.core.models import (
    SCHEMA_VERSION, BoundGeometry, ChapterDocument,
)
from comic_editor.three_d.documents import (
    BlenderChapterDocument, CacheManifest, ComicFrameDocument,
    DrawingMaterial3D, IDENTITY_MATRIX4,
)
from comic_editor.three_d.repository import BlenderSidecarData


HASH_A = "a" * 64


def chapter_with_blender_layer():
    chapter = ChapterDocument()
    page = chapter.add_page("Page")
    layer = chapter.add_blender_layer(
        page.layer_id, bound=BoundGeometry.circle(250, 250, 200),
    )
    return chapter, page, layer


def test_schema_16_blender_layer_round_trip_and_invariants():
    chapter, _page, layer = chapter_with_blender_layer()
    chapter.validate()

    assert SCHEMA_VERSION == 16
    assert layer.layer_kind == "blender"
    assert layer.bound.closed is True
    assert layer.fill_color is None
    assert layer.border_color == "#FF000000"
    assert layer.border_width == 4
    assert layer.children == []
    assert layer.comic_frame_id

    restored = ChapterDocument.from_dict(chapter.to_dict())
    restored_layer = restored.layers[layer.layer_id]
    assert restored.schema_version == 16
    assert restored_layer.comic_frame_id == layer.comic_frame_id
    assert restored_layer.bound.primitive == "ellipse"


def test_blender_layer_duplicate_and_delete_use_independent_frames():
    chapter, page, layer = chapter_with_blender_layer()
    layer.translate_x = 12
    layer.opacity = 0.5
    duplicate = chapter.duplicate_blender_layer(layer.layer_id)

    assert duplicate.parent_id == page.layer_id
    assert duplicate.layer_id != layer.layer_id
    assert duplicate.comic_frame_id != layer.comic_frame_id
    assert duplicate.bound.to_dict() == layer.bound.to_dict()
    assert duplicate.translate_x == 12
    assert duplicate.opacity == 0.5
    chapter.validate()

    deleted_frame_id = chapter.delete_blender_layer(layer.layer_id)
    assert deleted_frame_id == layer.comic_frame_id
    assert layer.layer_id not in chapter.layers
    chapter.validate()


def test_blender_layer_rejects_children_open_bounds_compound_and_assets():
    chapter, _page, layer = chapter_with_blender_layer()
    with pytest.raises(ValueError, match="container|Leaf"):
        chapter.add_layer(layer.layer_id)
    with pytest.raises(ValueError, match="container|Leaf"):
        chapter.add_object(layer.layer_id, object())

    layer.compound_enabled = True
    with pytest.raises(ValueError, match="compound"):
        chapter.validate()
    layer.compound_enabled = False
    layer.fill_color = "#FFFFFFFF"
    with pytest.raises(ValueError, match="fill"):
        chapter.validate()

    asset = ChapterDocument(document_kind="asset", width=512, height=512)
    page = asset.add_page(bound=BoundGeometry.rectangle(0, 0, 512, 512))
    with pytest.raises(ValueError, match="assets"):
        asset.add_blender_layer(page.layer_id)

    normal = ChapterDocument()
    normal_page = normal.add_page()
    with pytest.raises(ValueError, match="closed"):
        normal.add_blender_layer(
            normal_page.layer_id,
            bound=BoundGeometry.path([
                normal_page.bound.nodes[0], normal_page.bound.nodes[1],
            ]),
        )


def test_comic_frame_preserves_exact_matrices_source_override_split_and_extensions():
    matrix = list(IDENTITY_MATRIX4)
    matrix[12:15] = [2.5, -4.0, 8.25]
    frame = ComicFrameDocument(
        frame_id="frame-1",
        chapter_id="chapter-1",
        included_collection_ids=["existing"],
        source_state={
            "objects": {"cube": {"world_matrix": matrix}},
            "collection_visibility": {"existing": True, "future": True},
        },
        presentation_overrides={
            "objects": {"cube": {"local_matrix": matrix}},
            "renderer_settings": {"projection": "fisheye_equidistant"},
        },
        extensions={"vendor.test": {"enabled": True}},
        unknown_fields={"future_top_level": {"kept": 1}},
    )

    payload = frame.to_dict()
    restored = ComicFrameDocument.from_dict(payload)
    assert restored.source_state["objects"]["cube"]["world_matrix"] == matrix
    assert (
        restored.presentation_overrides["objects"]["cube"]["local_matrix"]
        == matrix
    )
    assert restored.renderer_settings["projection"] == "fisheye_equidistant"
    assert restored.is_collection_visible("existing") is True
    assert restored.collection_visible("existing") is True
    assert restored.is_collection_visible("future") is False
    assert restored.extensions == {"vendor.test": {"enabled": True}}
    assert restored.to_dict()["future_top_level"] == {"kept": 1}

    payload["source"]["state"]["objects"]["cube"]["world_matrix"] = [1, 2]
    with pytest.raises(ValueError, match="sixteen"):
        ComicFrameDocument.from_dict(payload)


def test_frame_rejects_geometry_and_sidecar_manages_frame_lifecycle():
    document = BlenderChapterDocument(chapter_id="chapter-1")
    sidecar = BlenderSidecarData(document)
    frame = sidecar.create_frame(
        frame_id="frame-1", source_state={"mesh_hash": HASH_A},
    )
    duplicate = sidecar.duplicate_frame(frame.frame_id, "frame-2")
    assert duplicate.frame_id == "frame-2"
    assert document.frame_ids == ["frame-1", "frame-2"]

    deleted = sidecar.delete_frame("frame-1")
    assert deleted is frame
    assert document.frame_ids == ["frame-2"]
    assert "frame-1" in document.tombstones["frames"]

    with pytest.raises(ValueError, match="geometry"):
        ComicFrameDocument(
            frame_id="bad", chapter_id="chapter-1",
            source_state={"vertices": [[0, 0, 0]]},
        ).validate()


@pytest.mark.parametrize(
    "payload_key",
    [
        "positions", "vertex_positions", "normals", "faces", "triangles",
        "uvs", "TEXCOORD_0", "texture_coordinates_1",
    ],
)
@pytest.mark.parametrize(
    "field_name",
    ["source_state", "presentation_overrides", "extensions", "unknown_fields"],
)
def test_frame_rejects_geometry_payload_aliases_in_all_metadata(
    payload_key, field_name,
):
    values = {
        field_name: {"nested": {payload_key: [[0.0, 0.0, 0.0]]}},
    }
    with pytest.raises(ValueError, match="geometry"):
        ComicFrameDocument(
            frame_id="bad", chapter_id="chapter-1", **values,
        ).validate()


def test_frame_allows_parametric_values_and_geometry_hash_metadata():
    frame = ComicFrameDocument(
        frame_id="parametric", chapter_id="chapter-1",
        local_entities=[{
            "id": "cylinder", "type": "cylinder",
            "parameters": {
                "vertices": 32,
                "position": [0.0, 1.0, 0.0],
                "normal": [0.0, 1.0, 0.0],
                "uv_scale": [1.0, 1.0],
            },
        }],
        extensions={
            "vendor.test": {
                "positions_hash": HASH_A,
                "uv_map": "UVMap",
            },
        },
    )

    frame.validate()
    assert frame.local_entities[0]["parameters"]["vertices"] == 32


def test_blender_chapter_material_and_cache_documents_round_trip():
    material = DrawingMaterial3D(
        material_id="ink", shader="toon", tint="#80ff0000",
        extensions={"vendor.test": [1, 2]},
    )
    chapter = BlenderChapterDocument(
        chapter_id="chapter-1", series_id="series-1", file_uuid="blend-uuid",
        blend_path_hint="missing.blend", scene_catalog={"scene": {"name": "Main"}},
        material_mappings={"blender-mat": "ink"},
        drawing_materials=[material], frame_ids=["frame-1"],
        current_cache_revision="r1", unknown_fields={"future": 9},
    )
    restored = BlenderChapterDocument.from_dict(chapter.to_dict())
    assert restored.material_mappings == {"blender-mat": "ink"}
    assert restored.drawing_materials[0].shader == "toon"
    assert restored.current_cache_revision == "r1"
    assert restored.cache_revisions == ["r1"]
    assert restored.to_dict()["future"] == 9

    cache = CacheManifest(
        revision="r1", source_hashes={"blend": HASH_A},
        base_glb_hash=HASH_A, object_resources={"cube": HASH_A},
        freestyle_edges={"cube": {"topology_hash": HASH_A, "edges": [[0, 1]]}},
    )
    assert CacheManifest.from_dict(cache.to_dict()).referenced_hashes() == {HASH_A}

    future = cache.to_dict()
    future["schema_version"] = 999
    with pytest.raises(ValueError, match="future"):
        CacheManifest.from_dict(future)
