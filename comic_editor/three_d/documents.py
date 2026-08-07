"""Versioned, renderer-independent documents for Blender-linked 3D frames.

The records in this module intentionally contain no Qt, Blender, or ModernGL
types.  They are the stable JSON boundary shared by persistence, sync, and the
renderer.  Matrices are serialized as sixteen column-major floats so signed
scale and residual shear are not lost to transform decomposition.
"""
from __future__ import annotations

import copy
import json
import math
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Literal, Mapping

from comic_editor.core.models import canonical_argb, new_id


THREE_D_DOCUMENT_VERSION = 1
COORDINATE_SYSTEM = "gltf_y_up_right_handed_meters"
MATRIX_ORDER = "column_major"
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_GEOMETRY_PAYLOAD_KEYS = {
    "meshbytes", "glbbytes", "vertexbuffer", "indexbuffer",
    "vertices", "indices", "positions", "vertexpositions",
    "normals", "vertexnormals", "faces", "faceindices",
    "triangles", "triangleindices",
}
_UV_PAYLOAD_KEYS = {
    "uv", "uvs", "uvdata", "uvcoordinate", "uvcoordinates",
    "texcoord", "texcoords", "texturecoordinate", "texturecoordinates",
}

Matrix4 = tuple[float, ...]
IDENTITY_MATRIX4: Matrix4 = (
    1.0, 0.0, 0.0, 0.0,
    0.0, 1.0, 0.0, 0.0,
    0.0, 0.0, 1.0, 0.0,
    0.0, 0.0, 0.0, 1.0,
)


def matrix4(value: Iterable[Any]) -> Matrix4:
    """Normalize a flat or four-column matrix to exact column-major form."""
    raw = list(value)
    if len(raw) == 4 and all(
        isinstance(column, (list, tuple)) and len(column) == 4
        for column in raw
    ):
        raw = [component for column in raw for component in column]
    if len(raw) != 16:
        raise ValueError("4x4 matrices require exactly sixteen values")
    result = tuple(float(component) for component in raw)
    if not all(math.isfinite(component) for component in result):
        raise ValueError("4x4 matrices require finite values")
    return result


def _version(data: Mapping[str, Any], label: str) -> int:
    version = int(data.get("schema_version", 1))
    if version < 1:
        raise ValueError(f"Invalid {label} schema: {version}")
    if version > THREE_D_DOCUMENT_VERSION:
        raise ValueError(f"Unsupported future {label} schema: {version}")
    return version


def _coordinate_contract(data: Mapping[str, Any], label: str) -> None:
    coordinate_system = str(data.get("coordinate_system", COORDINATE_SYSTEM))
    order = str(data.get("matrix_order", MATRIX_ORDER))
    if coordinate_system != COORDINATE_SYSTEM or order != MATRIX_ORDER:
        raise ValueError(
            f"{label} must use right-handed glTF Y-up meter coordinates "
            "and column-major matrices"
        )


def _unknown_fields(
    data: Mapping[str, Any], known: set[str],
) -> dict[str, Any]:
    return copy.deepcopy({key: value for key, value in data.items() if key not in known})


def _mapping(value: Any, label: str) -> dict[str, Any]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be an object")
    return {str(key): copy.deepcopy(item) for key, item in value.items()}


def _json_safe(value: Any, label: str = "value") -> Any:
    """Deep-copy JSON data while rejecting bytes, NaN, and infinities."""
    if value is None or isinstance(value, (str, bool, int)):
        return copy.deepcopy(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{label} must contain finite numbers")
        return value
    if isinstance(value, Mapping):
        return {
            str(key): _json_safe(item, f"{label}.{key}")
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [
            _json_safe(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    raise ValueError(f"{label} contains a non-JSON value: {type(value).__name__}")


def _normalize_matrices(value: Any, label: str = "state") -> Any:
    """Validate every explicitly named matrix without interpreting transforms."""
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            text = str(key)
            folded = text.casefold()
            if (
                folded == "matrix" or folded.startswith("matrix_")
                or folded.endswith("_matrix")
            ):
                result[text] = list(matrix4(item))
            else:
                result[text] = _normalize_matrices(item, f"{label}.{text}")
        return result
    if isinstance(value, (list, tuple)):
        return [
            _normalize_matrices(item, f"{label}[{index}]")
            for index, item in enumerate(value)
        ]
    return _json_safe(value, label)


def _reject_embedded_geometry(value: Any, label: str = "frame") -> None:
    """Keep mesh payloads in the content-addressed cache, never frame JSON."""
    if isinstance(value, Mapping):
        for key, item in value.items():
            folded = str(key).casefold()
            if folded == "geometry":
                is_hash = (
                    isinstance(item, str) and _SHA256.fullmatch(item.lower())
                )
                is_reference = (
                    isinstance(item, Mapping) and bool(item)
                    and set(map(str, item)) <= {
                        "hash", "content_hash", "resource_hash", "variant_hash",
                    }
                    and all(
                        isinstance(digest, str)
                        and _SHA256.fullmatch(digest.lower())
                        for digest in item.values()
                    )
                )
                if is_hash or is_reference:
                    continue
                raise ValueError(
                    f"{label} embeds geometry; use a content hash instead"
                )
            # A frame-local cylinder stores only its parametric segment count.
            # Keep that scalar legal while continuing to reject vertex arrays.
            if (
                folded == "vertices" and isinstance(item, int)
                and not isinstance(item, bool) and 3 <= item <= 256
                and label.endswith(".parameters")
            ):
                continue
            compact = re.sub(r"[^a-z0-9]", "", folded)
            compact_without_set = re.sub(r"\d+$", "", compact)
            if (
                compact in _GEOMETRY_PAYLOAD_KEYS
                or compact_without_set in _UV_PAYLOAD_KEYS
            ):
                raise ValueError(
                    f"{label} embeds geometry; use a content hash instead"
                )
            _reject_embedded_geometry(item, f"{label}.{key}")
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _reject_embedded_geometry(item, f"{label}[{index}]")


def _hash(value: Any, label: str, *, optional: bool = False) -> str | None:
    if optional and (value is None or value == ""):
        return None
    text = str(value).lower()
    if not _SHA256.fullmatch(text):
        raise ValueError(f"{label} must be a lowercase SHA-256 digest")
    return text


@dataclass
class DrawingMaterial3D:
    material_id: str = field(default_factory=new_id)
    name: str = "Material"
    shader: Literal["diffuse", "toon", "unshaded"] = "diffuse"
    tint: str = "#FFFFFFFF"
    use_texture: bool = True
    use_vertex_color: bool = True
    toon_ramp: list[tuple[float, str]] = field(default_factory=lambda: [
        (0.0, "#FF333333"), (0.5, "#FFAAAAAA"), (1.0, "#FFFFFFFF"),
    ])
    outline_enabled: bool = True
    outline_color: str = "#FF000000"
    outline_width: float = 1.0
    source_material_ids: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def validate(self) -> None:
        self.material_id = str(self.material_id).strip()
        if not self.material_id:
            raise ValueError("Drawing materials require an ID")
        self.name = str(self.name).strip() or "Material"
        if self.shader not in {"diffuse", "toon", "unshaded"}:
            raise ValueError(f"Unknown 3D material shader: {self.shader}")
        self.tint = canonical_argb(self.tint, "#FFFFFFFF")
        self.outline_color = canonical_argb(self.outline_color)
        self.use_texture = bool(self.use_texture)
        self.use_vertex_color = bool(self.use_vertex_color)
        self.outline_enabled = bool(self.outline_enabled)
        self.outline_width = float(self.outline_width)
        if not math.isfinite(self.outline_width) or self.outline_width < 0:
            raise ValueError("Outline width must be a finite non-negative value")
        normalized_ramp: list[tuple[float, str]] = []
        for item in self.toon_ramp:
            if isinstance(item, Mapping):
                position, color = item.get("position", 0.0), item.get("color")
            else:
                position, color = item
            position = float(position)
            if not math.isfinite(position) or not 0.0 <= position <= 1.0:
                raise ValueError("Toon ramp positions must be between zero and one")
            normalized_ramp.append((position, canonical_argb(color)))
        if not normalized_ramp:
            raise ValueError("Toon ramps require at least one stop")
        self.toon_ramp = sorted(normalized_ramp, key=lambda item: item[0])
        self.source_material_ids = list(dict.fromkeys(
            str(item) for item in self.source_material_ids if str(item)
        ))
        self.extensions = _mapping(self.extensions, "material extensions")
        _json_safe(self.extensions, "material extensions")
        _json_safe(self.unknown_fields, "unknown material fields")

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = copy.deepcopy(self.unknown_fields)
        payload.update({
            "id": self.material_id,
            "name": self.name,
            "shader": self.shader,
            "tint": self.tint,
            "use_texture": self.use_texture,
            "use_vertex_color": self.use_vertex_color,
            "toon_ramp": [
                {"position": position, "color": color}
                for position, color in self.toon_ramp
            ],
            "outline": {
                "enabled": self.outline_enabled,
                "color": self.outline_color,
                "width": self.outline_width,
            },
            "source_material_ids": list(self.source_material_ids),
            "extensions": copy.deepcopy(self.extensions),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "DrawingMaterial3D":
        known = {
            "id", "name", "shader", "tint", "use_texture",
            "use_vertex_color", "toon_ramp", "outline",
            "source_material_ids", "extensions",
        }
        outline = _mapping(data.get("outline"), "material outline")
        result = cls(
            material_id=str(data.get("id") or new_id()),
            name=str(data.get("name", "Material")),
            shader=str(data.get("shader", "diffuse")),
            tint=str(data.get("tint", "#FFFFFFFF")),
            use_texture=bool(data.get("use_texture", True)),
            use_vertex_color=bool(data.get("use_vertex_color", True)),
            toon_ramp=copy.deepcopy(data.get("toon_ramp") or [
                (0.0, "#FF333333"), (0.5, "#FFAAAAAA"),
                (1.0, "#FFFFFFFF"),
            ]),
            outline_enabled=bool(outline.get("enabled", True)),
            outline_color=str(outline.get("color", "#FF000000")),
            outline_width=float(outline.get("width", 1.0)),
            source_material_ids=[
                str(item) for item in data.get("source_material_ids", [])
            ],
            extensions=_mapping(data.get("extensions"), "material extensions"),
            unknown_fields=_unknown_fields(data, known),
        )
        result.validate()
        return result


@dataclass
class ComicFrameDocument:
    frame_id: str = field(default_factory=new_id)
    chapter_id: str = ""
    source_scene_id: str = ""
    source_timeline_frame: int = 1
    source_revision: int = 0
    base_revision: int = 0
    revision: int = 0
    included_collection_ids: list[str] = field(default_factory=list)
    source_state: dict[str, Any] = field(default_factory=dict)
    presentation_overrides: dict[str, Any] = field(default_factory=dict)
    local_entities: list[dict[str, Any]] = field(default_factory=list)
    baked_variant_hashes: dict[str, str] = field(default_factory=dict)
    warnings: list[str] = field(default_factory=list)
    extensions: dict[str, Any] = field(default_factory=dict)
    schema_version: int = THREE_D_DOCUMENT_VERSION
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def renderer_settings(self) -> dict[str, Any]:
        value = self.presentation_overrides.setdefault("renderer_settings", {})
        if not isinstance(value, dict):
            raise ValueError("renderer_settings override must be an object")
        return value

    def is_collection_visible(self, collection_id: str) -> bool:
        """Return False for collections absent when this frame was captured."""
        collection_id = str(collection_id)
        if collection_id not in self.included_collection_ids:
            return False
        overrides = self.presentation_overrides.get("collection_visibility", {})
        if isinstance(overrides, Mapping) and collection_id in overrides:
            return bool(overrides[collection_id])
        source = self.source_state.get("collection_visibility", {})
        return bool(source.get(collection_id, True)) if isinstance(source, Mapping) else True

    def collection_visible(self, collection_id: str) -> bool:
        """Compatibility spelling used by scene builders and the outliner."""
        return self.is_collection_visible(collection_id)

    def validate(self) -> None:
        self.frame_id = str(self.frame_id).strip()
        self.chapter_id = str(self.chapter_id).strip()
        if not self.frame_id or not self.chapter_id:
            raise ValueError("Comic frames require frame and chapter IDs")
        self.source_scene_id = str(self.source_scene_id)
        self.source_timeline_frame = int(self.source_timeline_frame)
        for field_name in ("source_revision", "base_revision", "revision"):
            value = int(getattr(self, field_name))
            if value < 0:
                raise ValueError(f"{field_name} cannot be negative")
            setattr(self, field_name, value)
        self.included_collection_ids = list(dict.fromkeys(
            str(item) for item in self.included_collection_ids if str(item)
        ))
        self.source_state = _normalize_matrices(
            _mapping(self.source_state, "source state"), "source_state",
        )
        self.presentation_overrides = _normalize_matrices(
            _mapping(self.presentation_overrides, "presentation overrides"),
            "presentation_overrides",
        )
        self.local_entities = [
            _normalize_matrices(_mapping(item, "local entity"), "local_entity")
            for item in self.local_entities
        ]
        _reject_embedded_geometry(self.source_state, "source_state")
        _reject_embedded_geometry(
            self.presentation_overrides, "presentation_overrides",
        )
        _reject_embedded_geometry(self.local_entities, "local_entities")
        self.baked_variant_hashes = {
            str(key): _hash(value, f"baked variant {key}") or ""
            for key, value in self.baked_variant_hashes.items()
        }
        self.warnings = [str(item) for item in self.warnings]
        self.extensions = _mapping(self.extensions, "frame extensions")
        _reject_embedded_geometry(self.extensions, "extensions")
        _reject_embedded_geometry(self.unknown_fields, "unknown_fields")
        _json_safe(self.extensions, "frame extensions")
        _json_safe(self.unknown_fields, "unknown frame fields")
        self.schema_version = THREE_D_DOCUMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = copy.deepcopy(self.unknown_fields)
        payload.update({
            "schema_version": self.schema_version,
            "coordinate_system": COORDINATE_SYSTEM,
            "matrix_order": MATRIX_ORDER,
            "id": self.frame_id,
            "chapter_id": self.chapter_id,
            "source": {
                "scene_id": self.source_scene_id,
                "timeline_frame": self.source_timeline_frame,
                "revision": self.source_revision,
                "state": copy.deepcopy(self.source_state),
            },
            "base_revision": self.base_revision,
            "revision": self.revision,
            "included_collection_ids": list(self.included_collection_ids),
            "presentation_overrides": copy.deepcopy(self.presentation_overrides),
            "local_entities": copy.deepcopy(self.local_entities),
            "baked_variant_hashes": dict(self.baked_variant_hashes),
            "warnings": list(self.warnings),
            "extensions": copy.deepcopy(self.extensions),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ComicFrameDocument":
        _version(data, "comic frame")
        _coordinate_contract(data, "Comic frames")
        known = {
            "schema_version", "coordinate_system", "matrix_order",
            "id", "chapter_id", "source",
            "base_revision", "revision", "included_collection_ids",
            "presentation_overrides", "local_entities",
            "baked_variant_hashes", "warnings", "extensions",
        }
        source = _mapping(data.get("source"), "comic frame source")
        result = cls(
            frame_id=str(data.get("id") or new_id()),
            chapter_id=str(data.get("chapter_id", "")),
            source_scene_id=str(source.get("scene_id", "")),
            source_timeline_frame=int(source.get("timeline_frame", 1)),
            source_revision=int(source.get("revision", 0)),
            base_revision=int(data.get("base_revision", 0)),
            revision=int(data.get("revision", 0)),
            included_collection_ids=[
                str(item) for item in data.get("included_collection_ids", [])
            ],
            source_state=_mapping(source.get("state"), "source state"),
            presentation_overrides=_mapping(
                data.get("presentation_overrides"), "presentation overrides",
            ),
            local_entities=[
                _mapping(item, "local entity")
                for item in data.get("local_entities", [])
            ],
            baked_variant_hashes={
                str(key): str(value) for key, value in _mapping(
                    data.get("baked_variant_hashes"), "baked variants",
                ).items()
            },
            warnings=[str(item) for item in data.get("warnings", [])],
            extensions=_mapping(data.get("extensions"), "frame extensions"),
            unknown_fields=_unknown_fields(data, known),
        )
        result.validate()
        return result


def _catalog(value: Any, label: str) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key, item in _mapping(value, label).items():
        result[key] = _normalize_matrices(_mapping(item, f"{label}.{key}"), label)
    return result


@dataclass
class BlenderChapterDocument:
    chapter_id: str = ""
    series_id: str = ""
    file_uuid: str = ""
    blend_path_hint: str = ""
    revision: int = 0
    source_revision: int = 0
    scene_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    collection_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    object_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    material_catalog: dict[str, dict[str, Any]] = field(default_factory=dict)
    material_mappings: dict[str, str] = field(default_factory=dict)
    drawing_materials: list[DrawingMaterial3D] = field(default_factory=list)
    frame_ids: list[str] = field(default_factory=list)
    cache_revisions: list[str] = field(default_factory=list)
    current_cache_revision: str | None = None
    warnings: list[str] = field(default_factory=list)
    tombstones: dict[str, Any] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    schema_version: int = THREE_D_DOCUMENT_VERSION
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def validate(self) -> None:
        self.chapter_id = str(self.chapter_id).strip()
        if not self.chapter_id:
            raise ValueError("Blender chapter documents require a chapter ID")
        self.series_id = str(self.series_id)
        self.file_uuid = str(self.file_uuid).strip()
        self.blend_path_hint = str(self.blend_path_hint)
        self.revision = int(self.revision)
        self.source_revision = int(self.source_revision)
        if self.revision < 0 or self.source_revision < 0:
            raise ValueError("Chapter revisions cannot be negative")
        self.scene_catalog = _catalog(self.scene_catalog, "scene catalog")
        self.collection_catalog = _catalog(
            self.collection_catalog, "collection catalog",
        )
        self.object_catalog = _catalog(self.object_catalog, "object catalog")
        self.material_catalog = _catalog(
            self.material_catalog, "material catalog",
        )
        material_ids: set[str] = set()
        for material in self.drawing_materials:
            material.validate()
            if material.material_id in material_ids:
                raise ValueError("Duplicate drawing material ID")
            material_ids.add(material.material_id)
        self.material_mappings = {
            str(source): str(target)
            for source, target in self.material_mappings.items()
        }
        missing = set(self.material_mappings.values()) - material_ids
        if missing:
            raise ValueError("Material mappings reference missing drawing materials")
        self.frame_ids = list(dict.fromkeys(
            str(item) for item in self.frame_ids if str(item)
        ))
        self.cache_revisions = list(dict.fromkeys(
            str(item) for item in self.cache_revisions if str(item)
        ))
        if self.current_cache_revision is not None:
            self.current_cache_revision = str(self.current_cache_revision)
            if self.current_cache_revision not in self.cache_revisions:
                self.cache_revisions.append(self.current_cache_revision)
        self.warnings = [str(item) for item in self.warnings]
        self.tombstones = _mapping(self.tombstones, "tombstones")
        self.extensions = _mapping(self.extensions, "chapter extensions")
        _json_safe(self.tombstones, "tombstones")
        _json_safe(self.extensions, "chapter extensions")
        _json_safe(self.unknown_fields, "unknown Blender chapter fields")
        self.schema_version = THREE_D_DOCUMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = copy.deepcopy(self.unknown_fields)
        payload.update({
            "schema_version": self.schema_version,
            "coordinate_system": COORDINATE_SYSTEM,
            "matrix_order": MATRIX_ORDER,
            "chapter_id": self.chapter_id,
            "series_id": self.series_id,
            "blend_file": {
                "uuid": self.file_uuid,
                "path_hint": self.blend_path_hint,
            },
            "revision": self.revision,
            "source_revision": self.source_revision,
            "catalogs": {
                "scenes": copy.deepcopy(self.scene_catalog),
                "collections": copy.deepcopy(self.collection_catalog),
                "objects": copy.deepcopy(self.object_catalog),
                "materials": copy.deepcopy(self.material_catalog),
            },
            "material_mappings": dict(self.material_mappings),
            "drawing_materials": [item.to_dict() for item in self.drawing_materials],
            "frame_ids": list(self.frame_ids),
            "cache_revisions": list(self.cache_revisions),
            "current_cache_revision": self.current_cache_revision,
            "warnings": list(self.warnings),
            "tombstones": copy.deepcopy(self.tombstones),
            "extensions": copy.deepcopy(self.extensions),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "BlenderChapterDocument":
        _version(data, "Blender chapter")
        _coordinate_contract(data, "Blender chapter documents")
        known = {
            "schema_version", "coordinate_system", "matrix_order",
            "chapter_id", "series_id", "blend_file",
            "revision", "source_revision", "catalogs", "material_mappings",
            "drawing_materials", "frame_ids", "cache_revisions",
            "current_cache_revision", "warnings", "tombstones", "extensions",
        }
        blend_file = _mapping(data.get("blend_file"), "blend file")
        catalogs = _mapping(data.get("catalogs"), "catalogs")
        result = cls(
            chapter_id=str(data.get("chapter_id", "")),
            series_id=str(data.get("series_id", "")),
            file_uuid=str(blend_file.get("uuid", "")),
            blend_path_hint=str(blend_file.get("path_hint", "")),
            revision=int(data.get("revision", 0)),
            source_revision=int(data.get("source_revision", 0)),
            scene_catalog=_catalog(catalogs.get("scenes"), "scene catalog"),
            collection_catalog=_catalog(
                catalogs.get("collections"), "collection catalog",
            ),
            object_catalog=_catalog(catalogs.get("objects"), "object catalog"),
            material_catalog=_catalog(
                catalogs.get("materials"), "material catalog",
            ),
            material_mappings={
                str(key): str(value) for key, value in _mapping(
                    data.get("material_mappings"), "material mappings",
                ).items()
            },
            drawing_materials=[
                DrawingMaterial3D.from_dict(item)
                for item in data.get("drawing_materials", [])
            ],
            frame_ids=[str(item) for item in data.get("frame_ids", [])],
            cache_revisions=[
                str(item) for item in data.get("cache_revisions", [])
            ],
            current_cache_revision=(
                str(data["current_cache_revision"])
                if data.get("current_cache_revision") is not None else None
            ),
            warnings=[str(item) for item in data.get("warnings", [])],
            tombstones=_mapping(data.get("tombstones"), "tombstones"),
            extensions=_mapping(data.get("extensions"), "chapter extensions"),
            unknown_fields=_unknown_fields(data, known),
        )
        result.validate()
        return result


@dataclass
class CacheManifest:
    revision: str = ""
    source_revision: int = 0
    source_hashes: dict[str, str] = field(default_factory=dict)
    base_glb_hash: str | None = None
    object_resources: dict[str, str] = field(default_factory=dict)
    baked_variants: dict[str, str] = field(default_factory=dict)
    freestyle_edges: dict[str, dict[str, Any]] = field(default_factory=dict)
    extensions: dict[str, Any] = field(default_factory=dict)
    schema_version: int = THREE_D_DOCUMENT_VERSION
    unknown_fields: dict[str, Any] = field(default_factory=dict, repr=False)

    def referenced_hashes(self) -> set[str]:
        result = set(self.source_hashes.values())
        result.update(self.object_resources.values())
        result.update(self.baked_variants.values())
        if self.base_glb_hash:
            result.add(self.base_glb_hash)
        for record in self.freestyle_edges.values():
            topology_hash = record.get("topology_hash")
            if topology_hash:
                result.add(str(topology_hash))
        return result

    def validate(self) -> None:
        self.revision = str(self.revision).strip()
        if not self.revision:
            raise ValueError("Cache manifests require a revision")
        self.source_revision = int(self.source_revision)
        if self.source_revision < 0:
            raise ValueError("Cache source revision cannot be negative")
        self.source_hashes = {
            str(key): _hash(value, f"source hash {key}") or ""
            for key, value in self.source_hashes.items()
        }
        self.base_glb_hash = _hash(
            self.base_glb_hash, "base GLB hash", optional=True,
        )
        self.object_resources = {
            str(key): _hash(value, f"object resource {key}") or ""
            for key, value in self.object_resources.items()
        }
        self.baked_variants = {
            str(key): _hash(value, f"baked variant {key}") or ""
            for key, value in self.baked_variants.items()
        }
        normalized_edges: dict[str, dict[str, Any]] = {}
        for object_id, raw in self.freestyle_edges.items():
            record = _mapping(raw, f"Freestyle record {object_id}")
            if record.get("topology_hash"):
                record["topology_hash"] = _hash(
                    record["topology_hash"],
                    f"Freestyle topology hash {object_id}",
                )
            record = _json_safe(record, f"Freestyle record {object_id}")
            normalized_edges[str(object_id)] = record
        self.freestyle_edges = normalized_edges
        self.extensions = _mapping(self.extensions, "cache extensions")
        _json_safe(self.extensions, "cache extensions")
        _json_safe(self.unknown_fields, "unknown cache fields")
        self.schema_version = THREE_D_DOCUMENT_VERSION

    def to_dict(self) -> dict[str, Any]:
        self.validate()
        payload = copy.deepcopy(self.unknown_fields)
        payload.update({
            "schema_version": self.schema_version,
            "revision": self.revision,
            "source_revision": self.source_revision,
            "source_hashes": dict(self.source_hashes),
            "base_glb_hash": self.base_glb_hash,
            "object_resources": dict(self.object_resources),
            "baked_variants": dict(self.baked_variants),
            "freestyle_edges": copy.deepcopy(self.freestyle_edges),
            "extensions": copy.deepcopy(self.extensions),
        })
        return payload

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CacheManifest":
        _version(data, "3D cache")
        known = {
            "schema_version", "revision", "source_revision", "source_hashes",
            "base_glb_hash", "object_resources", "baked_variants",
            "freestyle_edges", "extensions",
        }
        result = cls(
            revision=str(data.get("revision", "")),
            source_revision=int(data.get("source_revision", 0)),
            source_hashes={
                str(key): str(value) for key, value in _mapping(
                    data.get("source_hashes"), "source hashes",
                ).items()
            },
            base_glb_hash=(
                str(data["base_glb_hash"])
                if data.get("base_glb_hash") else None
            ),
            object_resources={
                str(key): str(value) for key, value in _mapping(
                    data.get("object_resources"), "object resources",
                ).items()
            },
            baked_variants={
                str(key): str(value) for key, value in _mapping(
                    data.get("baked_variants"), "baked variants",
                ).items()
            },
            freestyle_edges={
                str(key): _mapping(value, f"Freestyle record {key}")
                for key, value in _mapping(
                    data.get("freestyle_edges"), "Freestyle edges",
                ).items()
            },
            extensions=_mapping(data.get("extensions"), "cache extensions"),
            unknown_fields=_unknown_fields(data, known),
        )
        result.validate()
        return result


def json_bytes(payload: Mapping[str, Any]) -> bytes:
    """Canonical JSON bytes used when a sidecar record needs hashing."""
    return json.dumps(
        payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
