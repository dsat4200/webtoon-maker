"""Strict, per-node glTF 2.0/GLB importer.

The importer intentionally returns a neutral :class:`SceneData`; it has no
knowledge of assets, projects, people, or frame libraries. It supports the
subset Blender emits for the sync cache: triangle primitives, images,
base-color materials, vertex colors/UVs, skins, morph targets, cameras and
``KHR_lights_punctual`` lights.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from io import BytesIO
import json
import math
import os
from pathlib import Path
import struct
from typing import Any
from urllib.parse import unquote_to_bytes

import numpy as np
from numpy.typing import NDArray
from PIL import Image

from .camera import quaternion_matrix
from .import_options import ImportOptions, NormalPolicy, import_model_matrix
from .mesh import (
    AlphaMode,
    MeshData,
    MeshPrimitive,
    MorphTarget,
    SkinData,
    SourceMaterial,
    TextureData,
    TextureFilter,
    TextureWrap,
    compute_vertex_normals,
)
from .scene import LightType, SceneCamera, SceneData, SceneLight, SceneNode


class GltfLoadError(ValueError):
    pass


_COMPONENT_DTYPES: dict[int, np.dtype[Any]] = {
    5120: np.dtype("i1"), 5121: np.dtype("u1"), 5122: np.dtype("<i2"),
    5123: np.dtype("<u2"), 5125: np.dtype("<u4"), 5126: np.dtype("<f4"),
}
_COMPONENT_COUNTS = {"SCALAR": 1, "VEC2": 2, "VEC3": 3, "VEC4": 4, "MAT2": 4, "MAT3": 9, "MAT4": 16}


@dataclass(frozen=True, slots=True)
class LoadedGltf:
    scene: SceneData
    source_json: dict[str, Any]
    default_scene_index: int


def _decode_data_uri(uri: str) -> bytes:
    if not uri.startswith("data:") or "," not in uri:
        raise GltfLoadError("invalid data URI")
    header, payload = uri.split(",", 1)
    try:
        return base64.b64decode(payload, validate=True) if ";base64" in header else unquote_to_bytes(payload)
    except Exception as exc:
        raise GltfLoadError("invalid data URI payload") from exc


def _safe_external(base: Path, uri: str) -> Path:
    if not uri or ":" in uri or uri.startswith(("/", "\\")):
        raise GltfLoadError("external glTF URI must be relative")
    candidate = (base / uri.replace("/", os.sep)).resolve()
    try:
        candidate.relative_to(base.resolve())
    except ValueError as exc:
        raise GltfLoadError("external glTF URI escapes its source directory") from exc
    return candidate


def _parse_glb(data: bytes) -> tuple[dict[str, Any], bytes | None]:
    if len(data) < 12:
        raise GltfLoadError("truncated GLB header")
    magic, version, declared_length = struct.unpack_from("<4sII", data)
    if magic != b"glTF" or version != 2 or declared_length != len(data):
        raise GltfLoadError("invalid GLB 2.0 header")
    offset, document, binary = 12, None, None
    while offset < len(data):
        if offset + 8 > len(data):
            raise GltfLoadError("truncated GLB chunk header")
        length, kind = struct.unpack_from("<I4s", data, offset)
        offset += 8
        if offset + length > len(data):
            raise GltfLoadError("truncated GLB chunk")
        chunk = data[offset : offset + length]
        offset += length
        if kind == b"JSON":
            if document is not None:
                raise GltfLoadError("GLB contains more than one JSON chunk")
            try:
                document = json.loads(chunk.rstrip(b"\x00 \t\r\n"))
            except Exception as exc:
                raise GltfLoadError("invalid GLB JSON") from exc
        elif kind == b"BIN\x00" and binary is None:
            binary = chunk
    if not isinstance(document, dict):
        raise GltfLoadError("GLB is missing its JSON chunk")
    return document, binary


class GltfImporter:
    def __init__(self, document: dict[str, Any], buffers: list[bytes], *, base_dir: Path | None = None) -> None:
        self.document = document
        self.buffers = buffers
        self.base_dir = base_dir
        self.warnings: list[str] = []

    @classmethod
    def from_path(cls, path: str | Path) -> "GltfImporter":
        source = Path(path)
        data = source.read_bytes()
        if data[:4] == b"glTF":
            document, binary = _parse_glb(data)
        else:
            try:
                document = json.loads(data.decode("utf-8-sig"))
            except Exception as exc:
                raise GltfLoadError("invalid glTF JSON") from exc
            binary = None
        return cls(document, cls._load_buffers(document, source.parent, binary), base_dir=source.parent)

    @classmethod
    def from_bytes(cls, data: bytes, *, base_dir: str | Path | None = None) -> "GltfImporter":
        directory = Path(base_dir) if base_dir is not None else None
        if data[:4] == b"glTF":
            document, binary = _parse_glb(data)
        else:
            try:
                document = json.loads(data.decode("utf-8-sig"))
            except Exception as exc:
                raise GltfLoadError("invalid glTF JSON") from exc
            binary = None
        return cls(document, cls._load_buffers(document, directory, binary), base_dir=directory)

    @staticmethod
    def _load_buffers(document: dict[str, Any], base_dir: Path | None, binary: bytes | None) -> list[bytes]:
        result: list[bytes] = []
        for index, description in enumerate(document.get("buffers", [])):
            uri = description.get("uri")
            if uri is None:
                if index != 0 or binary is None:
                    raise GltfLoadError("buffer without URI has no GLB binary chunk")
                value = binary
            elif str(uri).startswith("data:"):
                value = _decode_data_uri(str(uri))
            else:
                if base_dir is None:
                    raise GltfLoadError("external buffer requires a source directory")
                value = _safe_external(base_dir, str(uri)).read_bytes()
            expected = int(description.get("byteLength", 0))
            if expected < 0 or len(value) < expected:
                raise GltfLoadError("glTF buffer is shorter than declared")
            result.append(value)
        return result

    def _slice_view(self, index: int) -> bytes:
        try:
            view = self.document["bufferViews"][index]
            source = self.buffers[int(view["buffer"])]
            start = int(view.get("byteOffset", 0))
            length = int(view["byteLength"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GltfLoadError("invalid glTF buffer view") from exc
        if start < 0 or length < 0 or start + length > len(source):
            raise GltfLoadError("glTF buffer view is out of bounds")
        return source[start : start + length]

    def accessor(self, index: int) -> NDArray[Any]:
        try:
            accessor = self.document["accessors"][index]
            dtype = _COMPONENT_DTYPES[int(accessor["componentType"])]
            components = _COMPONENT_COUNTS[str(accessor["type"])]
            count = int(accessor["count"])
        except (KeyError, IndexError, TypeError, ValueError) as exc:
            raise GltfLoadError("invalid glTF accessor") from exc
        if count < 0:
            raise GltfLoadError("negative accessor count")
        if "bufferView" not in accessor:
            result = np.zeros((count, components), dtype=dtype)
        else:
            view_description = self.document["bufferViews"][int(accessor["bufferView"])]
            view = self._slice_view(int(accessor["bufferView"]))
            offset = int(accessor.get("byteOffset", 0))
            packed = dtype.itemsize * components
            stride = int(view_description.get("byteStride", packed))
            if offset < 0 or stride < packed or (count and offset + (count - 1) * stride + packed > len(view)):
                raise GltfLoadError("glTF accessor is out of bounds")
            if stride == packed:
                result = np.frombuffer(view, dtype=dtype, count=count * components, offset=offset).reshape((count, components)).copy()
            else:
                result = np.ndarray((count, components), dtype=dtype, buffer=view, offset=offset, strides=(stride, dtype.itemsize)).copy()
        if accessor.get("normalized") and np.issubdtype(result.dtype, np.integer):
            if np.issubdtype(result.dtype, np.signedinteger):
                result = np.maximum(result.astype(np.float32) / float(np.iinfo(result.dtype).max), -1.0)
            else:
                result = result.astype(np.float32) / float(np.iinfo(result.dtype).max)
        sparse = accessor.get("sparse")
        if sparse:
            sparse_count = int(sparse["count"])
            index_desc = sparse["indices"]
            value_desc = sparse["values"]
            index_dtype = _COMPONENT_DTYPES[int(index_desc["componentType"])]
            index_view = self._slice_view(int(index_desc["bufferView"]))
            sparse_indices = np.frombuffer(index_view, dtype=index_dtype, count=sparse_count, offset=int(index_desc.get("byteOffset", 0))).astype(np.int64)
            value_view = self._slice_view(int(value_desc["bufferView"]))
            sparse_values = np.frombuffer(value_view, dtype=dtype, count=sparse_count * components, offset=int(value_desc.get("byteOffset", 0))).reshape((sparse_count, components))
            if len(sparse_indices) and (sparse_indices.min() < 0 or sparse_indices.max() >= count):
                raise GltfLoadError("sparse accessor index is out of bounds")
            result[sparse_indices] = sparse_values
        return result[:, 0] if components == 1 else result

    def _image_bytes(self, image: dict[str, Any]) -> bytes:
        if "bufferView" in image:
            return self._slice_view(int(image["bufferView"]))
        uri = str(image.get("uri", ""))
        if uri.startswith("data:"):
            return _decode_data_uri(uri)
        if self.base_dir is None:
            raise GltfLoadError("external image requires a source directory")
        return _safe_external(self.base_dir, uri).read_bytes()

    def _textures(self) -> tuple[TextureData, ...]:
        images: list[NDArray[np.uint8]] = []
        for image in self.document.get("images", []):
            try:
                with Image.open(BytesIO(self._image_bytes(image))) as decoded:
                    images.append(np.asarray(decoded.convert("RGBA"), dtype=np.uint8).copy())
            except Exception as exc:
                self.warnings.append(f"glTF image could not be decoded and uses a white fallback: {exc}")
                images.append(np.full((1, 1, 4), 255, dtype=np.uint8))
        result = []
        samplers = self.document.get("samplers", [])
        for index, texture in enumerate(self.document.get("textures", [])):
            source_index = int(texture.get("source", -1))
            if not 0 <= source_index < len(images):
                raise GltfLoadError("texture references a missing image")
            sampler = samplers[int(texture["sampler"])] if "sampler" in texture else {}
            result.append(TextureData(
                str(texture.get("extras", {}).get("webtoon_uuid") or f"texture:{index}"),
                str(texture.get("name") or f"Texture {index}"), images[source_index],
                TextureFilter(int(sampler.get("magFilter", TextureFilter.LINEAR))),
                TextureFilter(int(sampler.get("minFilter", TextureFilter.LINEAR))),
                TextureWrap(int(sampler.get("wrapS", TextureWrap.REPEAT))),
                TextureWrap(int(sampler.get("wrapT", TextureWrap.REPEAT))),
            ))
        return tuple(result)

    def _materials(self, texture_count: int) -> tuple[SourceMaterial, ...]:
        result = []
        for index, material in enumerate(self.document.get("materials", [])):
            pbr = material.get("pbrMetallicRoughness", {})
            texture_info = pbr.get("baseColorTexture")
            texture_index = int(texture_info["index"]) if texture_info else None
            if texture_index is not None and not 0 <= texture_index < texture_count:
                raise GltfLoadError("material references a missing texture")
            result.append(SourceMaterial(
                str(material.get("extras", {}).get("webtoon_uuid") or f"material:{index}"),
                str(material.get("name") or f"Material {index}"),
                np.asarray(pbr.get("baseColorFactor", [1,1,1,1]), dtype=np.float32),
                texture_index, AlphaMode(str(material.get("alphaMode", "OPAQUE"))),
                float(material.get("alphaCutoff", 0.5)), bool(material.get("doubleSided", False)),
                int(texture_info.get("texCoord", 0)) if texture_info else 0,
            ))
        if not result:
            result.append(SourceMaterial("material:default"))
        return tuple(result)

    def _meshes(self, materials: tuple[SourceMaterial, ...], options: ImportOptions | None) -> tuple[MeshData, ...]:
        result = []
        for mesh_index, mesh in enumerate(self.document.get("meshes", [])):
            target_names = tuple(str(value) for value in mesh.get("extras", {}).get("targetNames", []))
            primitives = []
            for primitive_index, primitive in enumerate(mesh.get("primitives", [])):
                if int(primitive.get("mode", 4)) != 4:
                    self.warnings.append(f"mesh {mesh_index} primitive {primitive_index} is not triangles and was skipped")
                    continue
                attributes = primitive.get("attributes", {})
                if "POSITION" not in attributes:
                    raise GltfLoadError("triangle primitive is missing POSITION")
                positions = np.asarray(self.accessor(int(attributes["POSITION"])), dtype=np.float32)
                if positions.ndim != 2 or positions.shape[1] != 3:
                    raise GltfLoadError("POSITION accessor must be VEC3")
                if "indices" in primitive:
                    raw_indices = np.asarray(self.accessor(int(primitive["indices"])), dtype=np.uint32).reshape(-1)
                else:
                    raw_indices = np.arange(len(positions), dtype=np.uint32)
                if len(raw_indices) % 3:
                    raise GltfLoadError("triangle primitive index count is not divisible by three")
                indices = raw_indices.reshape((-1, 3))
                authored_normals = np.asarray(self.accessor(int(attributes["NORMAL"])), dtype=np.float32) if "NORMAL" in attributes else None
                policy = options.normals if options is not None else NormalPolicy.AUTO
                invalid_authored = authored_normals is None or authored_normals.shape != positions.shape or bool(np.any(np.linalg.norm(authored_normals, axis=1) <= 1e-12))
                if policy is NormalPolicy.REQUIRE_AUTHORED and invalid_authored:
                    raise GltfLoadError("glTF normals are missing or invalid")
                normals = compute_vertex_normals(positions, indices) if invalid_authored or policy in (NormalPolicy.RECOMPUTE_FLAT, NormalPolicy.RECOMPUTE_SMOOTH) else authored_normals
                material_index = int(primitive.get("material", 0))
                if not 0 <= material_index < len(materials):
                    raise GltfLoadError("primitive references a missing material")
                texcoord_attribute = f"TEXCOORD_{materials[material_index].base_color_texcoord}"
                texcoords = np.asarray(self.accessor(int(attributes[texcoord_attribute])), dtype=np.float32) if texcoord_attribute in attributes else None
                if materials[material_index].base_color_texture is not None and texcoords is None:
                    self.warnings.append(f"mesh {mesh_index} primitive {primitive_index} material requires {texcoord_attribute}; texture disabled for this primitive")
                colors = np.asarray(self.accessor(int(attributes["COLOR_0"])), dtype=np.float32) if "COLOR_0" in attributes else None
                if colors is not None:
                    if colors.shape[1] == 3:
                        colors = np.column_stack((colors, np.ones(len(colors), dtype=np.float32)))
                    if colors.shape[1] != 4:
                        raise GltfLoadError("COLOR_0 accessor must be VEC3 or VEC4")
                joints = np.asarray(self.accessor(int(attributes["JOINTS_0"])), dtype=np.uint16) if "JOINTS_0" in attributes else None
                weights = np.asarray(self.accessor(int(attributes["WEIGHTS_0"])), dtype=np.float32) if "WEIGHTS_0" in attributes else None
                if weights is not None:
                    totals = weights.sum(axis=1)
                    valid = totals > 1e-12
                    weights[valid] /= totals[valid, None]
                morphs: list[MorphTarget] = []
                for target_index, target in enumerate(primitive.get("targets", [])):
                    name = target_names[target_index] if target_index < len(target_names) else f"Target {target_index}"
                    morphs.append(MorphTarget(
                        name,
                        np.asarray(self.accessor(int(target["POSITION"])), dtype=np.float32) if "POSITION" in target else None,
                        np.asarray(self.accessor(int(target["NORMAL"])), dtype=np.float32) if "NORMAL" in target else None,
                    ))
                if policy is NormalPolicy.RECOMPUTE_FLAT:
                    expanded = indices.reshape(-1)
                    positions = positions[expanded]
                    texcoords = texcoords[expanded] if texcoords is not None else None
                    colors = colors[expanded] if colors is not None else None
                    joints = joints[expanded] if joints is not None else None
                    weights = weights[expanded] if weights is not None else None
                    morphs = [MorphTarget(target.name, target.position_deltas[expanded] if target.position_deltas is not None else None, target.normal_deltas[expanded] if target.normal_deltas is not None else None) for target in morphs]
                    indices = np.arange(len(positions), dtype=np.uint32).reshape((-1, 3))
                    normals = compute_vertex_normals(positions, indices)
                primitives.append(MeshPrimitive(positions, indices, material_index, normals, texcoords, colors, joints, weights, tuple(morphs)))
            if not primitives:
                # Preserve node/mesh indexes while making the absence explicit.
                tiny = np.asarray([[0,0,0],[0,0,0],[0,0,0]], dtype=np.float32)
                primitives.append(MeshPrimitive(tiny, np.asarray([[0,1,2]], dtype=np.uint32), normals=np.asarray([[0,1,0]]*3, dtype=np.float32)))
            result.append(MeshData(
                str(mesh.get("extras", {}).get("webtoon_uuid") or f"mesh:{mesh_index}"),
                str(mesh.get("name") or f"Mesh {mesh_index}"), tuple(primitives),
                tuple(float(v) for v in mesh.get("weights", [])), dict(mesh.get("extras", {})),
            ))
        return tuple(result)

    @staticmethod
    def _node_matrix(description: dict[str, Any]) -> NDArray[np.float64]:
        if "matrix" in description:
            values = np.asarray(description["matrix"], dtype=np.float64)
            if values.shape != (16,):
                raise GltfLoadError("node matrix must contain 16 values")
            return values.reshape((4, 4), order="F")
        translation = np.asarray(description.get("translation", [0,0,0]), dtype=np.float64)
        rotation = np.asarray(description.get("rotation", [0,0,0,1]), dtype=np.float64)
        scale = np.asarray(description.get("scale", [1,1,1]), dtype=np.float64)
        if translation.shape != (3,) or rotation.shape != (4,) or scale.shape != (3,):
            raise GltfLoadError("invalid node TRS values")
        matrix = np.identity(4, dtype=np.float64)
        matrix[:3, 3] = translation
        matrix = matrix @ quaternion_matrix(np.array([rotation[3], rotation[0], rotation[1], rotation[2]]))
        scale_matrix = np.identity(4, dtype=np.float64)
        scale_matrix[0,0], scale_matrix[1,1], scale_matrix[2,2] = scale
        return matrix @ scale_matrix

    def load(self, options: ImportOptions | None = None) -> LoadedGltf:
        asset = self.document.get("asset", {})
        if not str(asset.get("version", "")).startswith("2"):
            raise GltfLoadError("only glTF 2.x is supported")
        textures = self._textures()
        materials = self._materials(len(textures))
        meshes = self._meshes(materials, options)
        descriptions = self.document.get("nodes", [])
        node_ids: list[str] = []
        used: set[str] = set()
        for index, description in enumerate(descriptions):
            base = str(description.get("extras", {}).get("webtoon_uuid") or f"node:{index}")
            identifier, suffix = base, 1
            while identifier in used:
                identifier, suffix = f"{base}#{suffix}", suffix + 1
            used.add(identifier)
            node_ids.append(identifier)
        parent_indexes: dict[int, int] = {}
        for parent_index, description in enumerate(descriptions):
            for child_index in description.get("children", []):
                child_index = int(child_index)
                if child_index in parent_indexes or not 0 <= child_index < len(descriptions):
                    raise GltfLoadError("node has multiple parents or invalid child")
                parent_indexes[child_index] = parent_index

        scenes = self.document.get("scenes", [])
        default_scene = int(self.document.get("scene", 0)) if scenes else 0
        if scenes:
            if not 0 <= default_scene < len(scenes):
                raise GltfLoadError("default scene index is out of range")
            scene_description = scenes[default_scene]
            if not isinstance(scene_description, dict):
                raise GltfLoadError("scene description must be an object")
            active_indexes: set[int] = set()

            def include_descendants(index: int) -> None:
                if not 0 <= index < len(descriptions):
                    raise GltfLoadError("scene references a missing node")
                if index in active_indexes:
                    return
                active_indexes.add(index)
                for child in descriptions[index].get("children", []):
                    include_descendants(int(child))

            for root in scene_description.get("nodes", []):
                include_descendants(int(root))

            # A skin may legally keep joints outside the mesh node's subtree.
            # Include those joints and their ancestors without merging the
            # unrelated roots of other glTF scenes.
            skin_descriptions = self.document.get("skins", [])
            required = set(active_indexes)
            for index in tuple(active_indexes):
                if "skin" not in descriptions[index]:
                    continue
                skin_index = int(descriptions[index]["skin"])
                if not 0 <= skin_index < len(skin_descriptions):
                    raise GltfLoadError("node references a missing skin")
                skin = skin_descriptions[skin_index]
                required.update(int(item) for item in skin.get("joints", []))
                if "skeleton" in skin:
                    required.add(int(skin["skeleton"]))
            for index in tuple(required):
                if not 0 <= index < len(descriptions):
                    raise GltfLoadError("skin references a missing node")
                active_indexes.add(index)
                while index in parent_indexes:
                    index = parent_indexes[index]
                    active_indexes.add(index)
        else:
            active_indexes = set(range(len(descriptions)))

        light_descriptions = self.document.get("extensions", {}).get("KHR_lights_punctual", {}).get("lights", [])
        lights: list[SceneLight] = []
        light_map: dict[int, int] = {}
        cameras: list[SceneCamera] = []
        for index, description in enumerate(self.document.get("cameras", [])):
            if description.get("type") == "orthographic":
                ortho = description.get("orthographic", {})
                cameras.append(SceneCamera(str(description.get("extras", {}).get("webtoon_uuid") or f"camera:{index}"), str(description.get("name") or f"Camera {index}"), False, ortho_height=float(ortho.get("ymag", 5.0))*2.0, near=float(ortho.get("znear", .01)), far=float(ortho.get("zfar", 1000))))
            else:
                perspective = description.get("perspective", {})
                cameras.append(SceneCamera(str(description.get("extras", {}).get("webtoon_uuid") or f"camera:{index}"), str(description.get("name") or f"Camera {index}"), True, float(perspective.get("yfov", math.radians(50))), near=float(perspective.get("znear", .01)), far=float(perspective.get("zfar", 1000))))

        nodes: dict[str, SceneNode] = {}
        for index, description in enumerate(descriptions):
            if index not in active_indexes:
                continue
            extension = description.get("extensions", {}).get("KHR_lights_punctual", {})
            light_index = None
            if "light" in extension:
                source_index = int(extension["light"])
                if not 0 <= source_index < len(light_descriptions):
                    raise GltfLoadError("node references a missing light")
                if source_index not in light_map:
                    source = light_descriptions[source_index]
                    kind = str(source.get("type", "point"))
                    mapped = LightType.SUN if kind == "directional" else LightType.SPOT if kind == "spot" else LightType.POINT
                    spot = source.get("spot", {})
                    light_map[source_index] = len(lights)
                    lights.append(SceneLight(
                        str(source.get("extras", {}).get("webtoon_uuid") or f"light:{source_index}"),
                        str(source.get("name") or f"Light {source_index}"), mapped,
                        tuple(float(v) for v in source.get("color", [1,1,1])), float(source.get("intensity", 1)),
                        float(source.get("range", 0) or 0), spot_outer_angle=float(spot.get("outerConeAngle", math.pi/4)), spot_inner_angle=float(spot.get("innerConeAngle", 0)), raw_source=dict(source),
                    ))
                light_index = light_map[source_index]
            node = SceneNode(
                node_ids[index], str(description.get("name") or f"Node {index}"), self._node_matrix(description),
                parent_id=(
                    node_ids[parent_indexes[index]]
                    if index in parent_indexes
                    and parent_indexes[index] in active_indexes else None
                ),
                child_ids=tuple(
                    node_ids[int(value)]
                    for value in description.get("children", [])
                    if int(value) in active_indexes
                ),
                mesh_index=int(description["mesh"]) if "mesh" in description else None,
                skin_index=int(description["skin"]) if "skin" in description else None,
                camera_index=int(description["camera"]) if "camera" in description else None,
                light_index=light_index,
                morph_weights=tuple(float(v) for v in description.get("weights", [])),
                extras=dict(description.get("extras", {})),
            )
            nodes[node.node_id] = node
        skins = []
        for skin_index, description in enumerate(self.document.get("skins", [])):
            joints = tuple(node_ids[int(value)] for value in description.get("joints", []))
            if "inverseBindMatrices" in description:
                raw = np.asarray(self.accessor(int(description["inverseBindMatrices"])), dtype=np.float64)
                matrices = raw.reshape((-1, 4, 4)).transpose((0,2,1))
            else:
                matrices = np.tile(np.identity(4), (len(joints), 1, 1))
            skeleton = node_ids[int(description["skeleton"])] if "skeleton" in description else None
            skins.append(SkinData(str(description.get("extras", {}).get("webtoon_uuid") or f"skin:{skin_index}"), str(description.get("name") or f"Skin {skin_index}"), joints, matrices, skeleton))

        root_ids = tuple(
            node_ids[index] for index in range(len(node_ids))
            if index in active_indexes
            and (
                index not in parent_indexes
                or parent_indexes[index] not in active_indexes
            )
        )
        scene = SceneData(
            str(self.document.get("extras", {}).get("webtoon_uuid") or "gltf:scene"), nodes, root_ids,
            meshes, textures, materials, skins=tuple(skins), cameras=tuple(cameras), lights=tuple(lights), warnings=tuple(self.warnings),
        )
        if options is not None and nodes:
            low, high = scene.bounds()
            adjustment = import_model_matrix(low, high, options)
            for root_id in scene.root_node_ids:
                scene.nodes[root_id].local_matrix = adjustment @ scene.nodes[root_id].local_matrix
            scene.recompute_world_matrices()
        return LoadedGltf(scene, self.document, default_scene)


def load_gltf(source: str | Path | bytes, options: ImportOptions | None = None, *, base_dir: str | Path | None = None) -> LoadedGltf:
    importer = GltfImporter.from_bytes(source, base_dir=base_dir) if isinstance(source, bytes) else GltfImporter.from_path(source)
    return importer.load(options)


class GltfLoader:
    @staticmethod
    def load(source: str | Path | bytes, options: ImportOptions | None = None, *, base_dir: str | Path | None = None) -> SceneData:
        return load_gltf(source, options, base_dir=base_dir).scene
