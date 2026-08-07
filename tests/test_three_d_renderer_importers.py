from __future__ import annotations

import base64
from io import BytesIO
import json
from pathlib import Path
import struct

import numpy as np
import pytest
from PIL import Image

from comic_editor.three_d.renderer.gltf import GltfLoadError, load_gltf
from comic_editor.three_d.renderer.obj import ObjLoadError, load_obj


def _png_fixture() -> bytes:
    output = BytesIO()
    Image.new("RGBA", (1, 1), (255, 128, 64, 255)).save(output, format="PNG")
    return output.getvalue()


def _glb_fixture() -> bytes:
    png = _png_fixture()
    blob = bytearray()
    views = []
    accessors = []

    def add(array: np.ndarray, component_type: int, kind: str, *, normalized: bool = False) -> int:
        while len(blob) % 4:
            blob.append(0)
        offset = len(blob)
        data = np.ascontiguousarray(array).tobytes()
        blob.extend(data)
        view_index = len(views)
        views.append({"buffer": 0, "byteOffset": offset, "byteLength": len(data)})
        accessor = {"bufferView": view_index, "componentType": component_type, "count": len(array), "type": kind}
        if normalized:
            accessor["normalized"] = True
        accessors.append(accessor)
        return len(accessors) - 1

    positions = add(np.array([[0, 0, 0], [1, 0, 0], [0, 1, 0]], dtype="<f4"), 5126, "VEC3")
    normals = add(np.array([[0, 0, 1]] * 3, dtype="<f4"), 5126, "VEC3")
    texcoords = add(np.array([[0, 0], [1, 0], [0, 1]], dtype="<f4"), 5126, "VEC2")
    colors = add(np.array([[255, 0, 0, 255], [0, 255, 0, 255], [0, 0, 255, 255]], dtype="u1"), 5121, "VEC4", normalized=True)
    joints = add(np.zeros((3, 4), dtype="u1"), 5121, "VEC4")
    weights = add(np.array([[1, 0, 0, 0]] * 3, dtype="<f4"), 5126, "VEC4")
    morph = add(np.array([[0, 0, 0.1]] * 3, dtype="<f4"), 5126, "VEC3")
    indices = add(np.array([0, 1, 2], dtype="<u2"), 5123, "SCALAR")
    inverse_bind = add(np.identity(4, dtype="<f4").reshape((1, 16)), 5126, "MAT4")
    while len(blob) % 4:
        blob.append(0)
    image_offset = len(blob)
    blob.extend(png)
    views.append({"buffer": 0, "byteOffset": image_offset, "byteLength": len(png)})
    image_view = len(views) - 1

    local = np.array(
        [[-2.0, 0.3, 0.0, 7.0], [0.0, 1.5, 0.0, 8.0], [0.0, 0.0, 1.0, 9.0], [0.0, 0.0, 0.0, 1.0]]
    )
    document = {
        "asset": {"version": "2.0"},
        "extensionsUsed": ["KHR_lights_punctual"],
        "extensions": {"KHR_lights_punctual": {"lights": [{"name": "Key", "type": "spot", "intensity": 4.0}]}},
        "buffers": [{"byteLength": len(blob)}],
        "bufferViews": views,
        "accessors": accessors,
        "images": [{"bufferView": image_view, "mimeType": "image/png"}],
        "textures": [{"source": 0, "extras": {"webtoon_uuid": "texture-uuid"}}],
        "materials": [{"name": "Paint", "extras": {"webtoon_uuid": "material-uuid"}, "doubleSided": True, "pbrMetallicRoughness": {"baseColorTexture": {"index": 0}, "baseColorFactor": [0.5, 0.6, 0.7, 1.0]}}],
        "meshes": [{"name": "Triangle", "extras": {"webtoon_uuid": "mesh-uuid", "targetNames": ["Smile"]}, "weights": [0.25], "primitives": [{
            "attributes": {"POSITION": positions, "NORMAL": normals, "TEXCOORD_0": texcoords, "COLOR_0": colors, "JOINTS_0": joints, "WEIGHTS_0": weights},
            "indices": indices, "material": 0, "targets": [{"POSITION": morph}],
        }]}],
        "skins": [{"name": "Rig", "joints": [1], "inverseBindMatrices": inverse_bind}],
        "nodes": [
            {"name": "MeshNode", "extras": {"webtoon_uuid": "object-uuid"}, "matrix": local.reshape(16, order="F").tolist(), "mesh": 0, "skin": 0, "children": [1], "extensions": {"KHR_lights_punctual": {"light": 0}}},
            {"name": "Bone", "extras": {"webtoon_uuid": "bone-uuid"}},
        ],
        "scenes": [{"nodes": [0]}],
        "scene": 0,
    }
    json_bytes = json.dumps(document, separators=(",", ":")).encode()
    json_bytes += b" " * ((-len(json_bytes)) % 4)
    binary = bytes(blob) + b"\x00" * ((-len(blob)) % 4)
    total = 12 + 8 + len(json_bytes) + 8 + len(binary)
    return b"glTF" + struct.pack("<II", 2, total) + struct.pack("<I4s", len(json_bytes), b"JSON") + json_bytes + struct.pack("<I4s", len(binary), b"BIN\x00") + binary


def test_glb_import_is_per_node_and_preserves_renderer_inputs() -> None:
    loaded = load_gltf(_glb_fixture())
    scene = loaded.scene
    assert scene.root_node_ids == ("object-uuid",)
    assert scene.nodes["bone-uuid"].parent_id == "object-uuid"
    assert scene.nodes["object-uuid"].determinant < 0.0
    assert scene.nodes["object-uuid"].local_matrix[0, 1] == pytest.approx(0.3)
    primitive = scene.meshes[0].primitives[0]
    assert primitive.colors is not None and primitive.colors[0].tolist() == [1.0, 0.0, 0.0, 1.0]
    assert primitive.texcoords is not None
    assert primitive.joints is not None and primitive.weights is not None
    assert primitive.morph_targets[0].name == "Smile"
    np.testing.assert_allclose(primitive.evaluated_positions((1.0,))[:, 2], 0.1)
    assert scene.skins[0].joint_node_ids == ("bone-uuid",)
    assert scene.textures[0].texture_id == "texture-uuid"
    assert scene.source_materials[0].material_id == "material-uuid"
    assert scene.source_materials[0].double_sided is True
    assert scene.lights[0].name == "Key"


def test_gltf_rejects_external_traversal(tmp_path: Path) -> None:
    document = {"asset": {"version": "2.0"}, "buffers": [{"uri": "../outside.bin", "byteLength": 1}]}
    source = tmp_path / "scene.gltf"
    source.write_text(json.dumps(document), encoding="utf-8")
    with pytest.raises(GltfLoadError, match="escapes"):
        load_gltf(source)


def test_gltf_loads_only_the_declared_default_scene(tmp_path: Path) -> None:
    document = {
        "asset": {"version": "2.0"},
        "nodes": [
            {"name": "First", "children": [1]},
            {"name": "First Child"},
            {"name": "Second"},
        ],
        "scenes": [{"nodes": [0]}, {"nodes": [2]}],
        "scene": 1,
    }
    source = tmp_path / "multi-scene.gltf"
    source.write_text(json.dumps(document), encoding="utf-8")

    loaded = load_gltf(source)

    assert loaded.default_scene_index == 1
    assert loaded.scene.root_node_ids == ("node:2",)
    assert set(loaded.scene.nodes) == {"node:2"}


def test_obj_import_supports_groups_vertex_colors_uvs_and_negative_indices() -> None:
    scene = load_obj(b"""
        o First
        v 0 0 0 1 0 0
        v 1 0 0 0 1 0
        v 0 1 0 0 0 1
        vt 0 0
        vt 1 0
        vt 0 1
        f -3/-3 -2/-2 -1/-1
        o Second
        f 1/1 2/2 3/3
    """)
    assert len(scene.meshes) == 2
    assert [scene.nodes[node].name for node in scene.root_node_ids] == ["First", "Second"]
    primitive = scene.meshes[0].primitives[0]
    assert primitive.colors is not None
    assert primitive.texcoords is not None
    assert primitive.normals is not None


def test_obj_rejects_out_of_range_indices() -> None:
    with pytest.raises(ObjLoadError, match="out of range"):
        load_obj(b"v 0 0 0\nv 1 0 0\nv 0 1 0\nf 1 2 4\n")
