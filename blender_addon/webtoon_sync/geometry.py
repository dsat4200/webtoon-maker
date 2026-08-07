"""Evaluated GLB staging hooks and explicit modifier policy."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import math
from pathlib import Path
import re
import struct
from typing import Any, Iterable, Mapping, Sequence
import uuid

try:
    import bpy  # type: ignore
except ImportError:  # pragma: no cover
    bpy = None

from .identities import identity_for


SIMULATION_MODIFIERS = {
    "CLOTH", "COLLISION", "DYNAMIC_PAINT", "FLUID", "PARTICLE_SYSTEM",
    "SOFT_BODY",
}
REJECTED_MODIFIERS = SIMULATION_MODIFIERS | {"NODES"}


class UnsupportedSceneError(ValueError):
    pass


class GLBExportIntegrityError(UnsupportedSceneError):
    pass


MAX_STAGED_GLB_BYTES = 256 * 1024 * 1024
MAX_STAGED_JSON_BYTES = 16 * 1024 * 1024
_JSON_CHUNK = 0x4E4F534A
_EXECUTABLE_KEY_NAMES = {
    "code", "command", "entrypoint", "executable", "href", "python",
    "script", "shell", "uri", "url",
}
_EXTERNAL_SCHEME = re.compile(
    r"^(?:data|file|ftp|https?|javascript):", re.IGNORECASE,
)
_EXECUTABLE_PATH = re.compile(
    r"(?:^|[\\/])[^\\/]+\.(?:bat|cmd|com|dll|exe|js|msi|ps1|py|sh|vbs)(?:$|[?#])",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class GLBIntegrityReport:
    document: Mapping[str, Any]
    identity_locations: Mapping[str, tuple[str, ...]]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class ExportIdentityExpectations:
    identities: Mapping[str, str]
    bone_ids: tuple[str, ...] = ()


def _reject_constant(value: str) -> None:
    raise GLBExportIntegrityError(
        f"Staged GLB contains non-finite JSON number {value!r}"
    )


def _without_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise GLBExportIntegrityError(
                f"Staged GLB contains duplicate JSON key {key!r}"
            )
        result[key] = value
    return result


def parse_staged_glb_json(path: str | Path) -> Mapping[str, Any]:
    """Parse one bounded GLB v2 JSON chunk using only the standard library."""

    source = Path(path)
    size = source.stat().st_size
    if size < 20 or size > MAX_STAGED_GLB_BYTES:
        raise GLBExportIntegrityError("Staged GLB size is invalid")
    with source.open("rb") as handle:
        header = handle.read(12)
        if len(header) != 12:
            raise GLBExportIntegrityError("Staged GLB header is truncated")
        magic, version, declared_length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or declared_length != size:
            raise GLBExportIntegrityError("Staged GLB header is invalid")
        chunk_header = handle.read(8)
        if len(chunk_header) != 8:
            raise GLBExportIntegrityError("Staged GLB has no JSON chunk")
        chunk_length, chunk_type = struct.unpack("<II", chunk_header)
        if (
            chunk_type != _JSON_CHUNK
            or chunk_length <= 0
            or chunk_length > MAX_STAGED_JSON_BYTES
            or chunk_length % 4
            or 20 + chunk_length > size
        ):
            raise GLBExportIntegrityError("Staged GLB JSON chunk is invalid")
        # GLB 2.0 requires JSON chunk padding bytes to be ASCII spaces.  Do
        # not silently strip NUL or other bytes that could hide trailing data.
        encoded = handle.read(chunk_length).rstrip(b" ")
        try:
            document = json.loads(
                encoded.decode("utf-8"),
                object_pairs_hook=_without_duplicate_keys,
                parse_constant=_reject_constant,
            )
        except GLBExportIntegrityError:
            raise
        except (
            UnicodeDecodeError, json.JSONDecodeError, RecursionError, ValueError,
        ) as exc:
            raise GLBExportIntegrityError(
                "Staged GLB JSON chunk is malformed"
            ) from exc
        offset = 20 + chunk_length
        while offset < size:
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                raise GLBExportIntegrityError("Staged GLB chunk header is truncated")
            child_length, child_type = struct.unpack("<II", chunk_header)
            offset += 8
            if child_length % 4 or offset + child_length > size:
                raise GLBExportIntegrityError("Staged GLB chunk bounds are invalid")
            if child_type == _JSON_CHUNK:
                raise GLBExportIntegrityError(
                    "Staged GLB contains more than one JSON chunk"
                )
            handle.seek(child_length, 1)
            offset += child_length
        if offset != size or not isinstance(document, Mapping):
            raise GLBExportIntegrityError("Staged GLB container is malformed")
    return document


def _json_pointer(parts: Sequence[str]) -> str:
    return "/" + "/".join(
        part.replace("~", "~0").replace("/", "~1") for part in parts
    )


def _section_for_path(parts: Sequence[str]) -> str:
    if not parts:
        return "root"
    if parts[0] in {"nodes", "meshes", "materials", "cameras", "skins"}:
        return parts[0]
    if (
        len(parts) >= 3
        and parts[0] == "extensions"
        and parts[1] == "KHR_lights_punctual"
        and parts[2] == "lights"
    ):
        return "lights"
    return parts[0]


def _scan_export_json(
    document: Mapping[str, Any],
) -> dict[str, list[tuple[str, str]]]:
    identities: dict[str, list[tuple[str, str]]] = {}
    stack: list[tuple[Any, tuple[str, ...]]] = [(document, ())]
    while stack:
        value, path = stack.pop()
        if isinstance(value, Mapping):
            for key, child in value.items():
                lowered = key.casefold()
                reference_is_empty = (
                    child is None or child is False or child == "" or child == 0
                )
                if lowered in _EXECUTABLE_KEY_NAMES and not reference_is_empty:
                    raise GLBExportIntegrityError(
                        f"Staged GLB contains forbidden reference {_json_pointer((*path, key))}"
                    )
                if key == "extras" and isinstance(child, Mapping):
                    identity = child.get("webtoon_uuid")
                    if identity is not None:
                        try:
                            normalized = str(uuid.UUID(identity))
                        except (ValueError, AttributeError, TypeError) as exc:
                            raise GLBExportIntegrityError(
                                "Staged GLB contains an invalid webtoon_uuid extra"
                            ) from exc
                        if identity != normalized:
                            raise GLBExportIntegrityError(
                                "Staged GLB webtoon_uuid extras must be canonical UUIDs"
                            )
                        location = _json_pointer((*path, key, "webtoon_uuid"))
                        identities.setdefault(normalized, []).append((
                            _section_for_path(path), location,
                        ))
                stack.append((child, (*path, key)))
        elif isinstance(value, list):
            for index, child in enumerate(value):
                stack.append((child, (*path, str(index))))
        elif isinstance(value, str):
            candidate = value.strip()
            if _EXTERNAL_SCHEME.match(candidate) or _EXECUTABLE_PATH.search(candidate):
                raise GLBExportIntegrityError(
                    f"Staged GLB contains an external or executable reference at {_json_pointer(path)}"
                )
        elif isinstance(value, float) and not math.isfinite(value):
            raise GLBExportIntegrityError("Staged GLB contains a non-finite number")
    return identities


def collect_export_identity_expectations(
    objects: Iterable[Any],
) -> ExportIdentityExpectations:
    """Collect the exact UUID extras a participating export must preserve.

    A shared Blender datablock or material may be referenced by many objects,
    but one UUID may never identify two distinct owners or owner kinds.
    """

    expected: dict[str, str] = {}
    owners: dict[str, tuple[str, int, str]] = {}
    bone_ids: set[str] = set()

    def add(target: Any, kind: str, label: str) -> str:
        identity = identity_for(target)
        if identity is None:
            raise GLBExportIntegrityError(
                f"{label} is missing a valid canonical webtoon_uuid"
            )
        owner = (kind, id(target), label)
        existing = owners.get(identity)
        if existing is not None and existing[:2] != owner[:2]:
            raise GLBExportIntegrityError(
                f"webtoon_uuid {identity} ambiguously identifies "
                f"{existing[2]} and {label}"
            )
        owners.setdefault(identity, owner)
        expected.setdefault(identity, kind)
        return identity

    for obj in objects:
        name = getattr(obj, "name", "unnamed object")
        add(obj, "object", f"object {name!r}")
        data = getattr(obj, "data", None)
        if data is not None:
            data_name = getattr(data, "name", "unnamed data")
            add(data, "data", f"data {data_name!r} used by object {name!r}")
        for slot in getattr(obj, "material_slots", ()):
            material = getattr(slot, "material", None)
            if material is None:
                continue
            material_name = getattr(material, "name", "unnamed material")
            add(material, "material", f"material {material_name!r}")
        if getattr(obj, "type", None) == "ARMATURE" and data is not None:
            for bone in getattr(data, "bones", ()):
                bone_name = getattr(bone, "name", "unnamed bone")
                bone_identity = identity_for(bone)
                if bone_identity is None:
                    raise GLBExportIntegrityError(
                        f"bone {data_name!r}/{bone_name!r} is missing a valid "
                        "canonical webtoon_uuid for sidecar capture"
                    )
                bone_ids.add(bone_identity)
    return ExportIdentityExpectations(
        identities=dict(sorted(expected.items())),
        bone_ids=tuple(sorted(bone_ids)),
    )


def validate_staged_glb_export(
    path: str | Path,
    *,
    expected_identities: Mapping[str, str],
    bone_ids: Iterable[str] = (),
) -> GLBIntegrityReport:
    """Validate security and stable identity extras in a staged GLB.

    Expected kinds are ``object``, ``data``, and ``material``. Bone extras are
    deliberately not required because Blender's glTF exporter does not provide
    a proven v1 contract for custom bone properties; their IDs remain captured
    in the typed frame sidecar instead.
    """

    allowed_sections = {
        "object": {"nodes"},
        "data": {"meshes", "cameras", "lights", "skins"},
        "material": {"materials"},
    }
    normalized_expected: dict[str, str] = {}
    for identity, kind in expected_identities.items():
        try:
            normalized = str(uuid.UUID(identity))
        except (ValueError, AttributeError, TypeError) as exc:
            raise GLBExportIntegrityError(
                "Expected export identity is not a UUID"
            ) from exc
        if identity != normalized or kind not in allowed_sections:
            raise GLBExportIntegrityError(
                "Expected export identity contract is invalid"
            )
        if normalized in normalized_expected:
            raise GLBExportIntegrityError(
                "Expected export identity is ambiguous before GLB validation"
            )
        normalized_expected[normalized] = kind

    try:
        normalized_bones = {str(uuid.UUID(identity)) for identity in bone_ids}
    except (ValueError, AttributeError, TypeError) as exc:
        raise GLBExportIntegrityError(
            "Expected sidecar bone identity is not a UUID"
        ) from exc

    document = parse_staged_glb_json(path)
    asset = document.get("asset")
    if not isinstance(asset, Mapping) or asset.get("version") != "2.0":
        raise GLBExportIntegrityError("Staged GLB does not declare glTF 2.0")
    occurrences = _scan_export_json(document)
    for identity, locations in occurrences.items():
        if len(locations) > 1:
            raise GLBExportIntegrityError(
                f"Staged GLB repeats webtoon_uuid {identity} at "
                + ", ".join(location for _section, location in locations)
            )
    for identity, kind in normalized_expected.items():
        locations = occurrences.get(identity, [])
        if not locations:
            raise GLBExportIntegrityError(
                f"Staged GLB omitted expected {kind} webtoon_uuid {identity}"
            )
        section, location = locations[0]
        if section not in allowed_sections[kind]:
            raise GLBExportIntegrityError(
                f"Staged GLB placed {kind} webtoon_uuid {identity} in "
                f"unsupported section {section} at {location}"
            )

    warnings: tuple[str, ...] = ()
    if normalized_bones:
        warnings = (
            "Bone webtoon_uuid extras are not an enforced Blender glTF v1 "
            f"invariant; {len(normalized_bones)} bone ID(s) remain sidecar-validated.",
        )
    return GLBIntegrityReport(
        document=document,
        identity_locations={
            identity: tuple(location for _section, location in locations)
            for identity, locations in occurrences.items()
        },
        warnings=warnings,
    )


@dataclass(frozen=True)
class ModifierDescriptor:
    name: str
    type: str
    show_viewport: bool = True
    show_render: bool = True


@dataclass(frozen=True)
class ModifierPolicy:
    mode: str
    reason: str = ""
    rejected: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeometryExportResult:
    source_files: Mapping[str, Path]
    cache_manifest: Mapping[str, Any]
    warnings: tuple[str, ...]


def classify_modifier_stack(
    *,
    object_type: str,
    has_shape_keys: bool,
    modifiers: Sequence[ModifierDescriptor],
) -> ModifierPolicy:
    enabled = tuple(
        modifier for modifier in modifiers
        if modifier.show_viewport or modifier.show_render
    )
    rejected = tuple(
        f"{modifier.name} ({modifier.type})"
        for modifier in enabled if modifier.type in REJECTED_MODIFIERS
    )
    if rejected:
        return ModifierPolicy(
            "rejected",
            "Geometry Nodes and simulation modifiers are not supported by sync v1.",
            rejected,
        )
    if object_type not in {"MESH", "CURVE", "SURFACE", "FONT", "META"}:
        return ModifierPolicy("metadata_only")
    if not enabled:
        return ModifierPolicy("reusable")
    types = {modifier.type for modifier in enabled}
    deformable = has_shape_keys or "ARMATURE" in types
    if deformable and types.issubset({"ARMATURE"}):
        return ModifierPolicy("reusable_deform", "Armature and morph data remain reusable.")
    if deformable:
        return ModifierPolicy(
            "baked_fallback",
            "The evaluated result is cached for this comic frame because the deform stack cannot remain reusable.",
        )
    return ModifierPolicy(
        "evaluated_static",
        "Ordinary modifiers are exported through Blender's evaluated dependency graph.",
    )


def _modifier_descriptors(obj: Any) -> tuple[ModifierDescriptor, ...]:
    return tuple(ModifierDescriptor(
        name=modifier.name,
        type=modifier.type,
        show_viewport=bool(modifier.show_viewport),
        show_render=bool(modifier.show_render),
    ) for modifier in obj.modifiers)


def classify_blender_object(obj: Any) -> ModifierPolicy:
    data = getattr(obj, "data", None)
    has_shape_keys = getattr(data, "shape_keys", None) is not None
    return classify_modifier_stack(
        object_type=obj.type,
        has_shape_keys=has_shape_keys,
        modifiers=_modifier_descriptors(obj),
    )


def validate_export_objects(objects: Iterable[Any]) -> dict[str, ModifierPolicy]:
    policies: dict[str, ModifierPolicy] = {}
    errors: list[str] = []
    for obj in objects:
        object_id = identity_for(obj)
        if object_id is None:
            errors.append(f"{obj.name}: missing webtoon_uuid")
            continue
        data = getattr(obj, "data", None)
        if (
            getattr(obj, "library", None) is not None
            or getattr(obj, "override_library", None) is not None
            or (data is not None and (
                getattr(data, "library", None) is not None
                or getattr(data, "override_library", None) is not None
            ))
        ):
            errors.append(f"{obj.name}: linked-library data is unsupported")
            continue
        policy = classify_blender_object(obj)
        policies[object_id] = policy
        if policy.mode == "rejected":
            errors.append(f"{obj.name}: {', '.join(policy.rejected)}")
    if errors:
        raise UnsupportedSceneError("Sync validation failed:\n" + "\n".join(errors))
    return policies


def _operator_kwargs(operator: Any, requested: Mapping[str, Any]) -> dict[str, Any]:
    """Filter exporter options against Blender 4.5's runtime RNA contract."""

    try:
        supported = {property_.identifier for property_ in operator.get_rna_type().properties}
    except (AttributeError, RuntimeError):
        # These core arguments have been stable across the supported glTF add-on.
        supported = {"filepath", "export_format", "use_selection"}
    return {name: value for name, value in requested.items() if name in supported}


def _export_glb(path: Path, *, apply_modifiers: bool) -> None:
    operator = bpy.ops.export_scene.gltf
    kwargs = _operator_kwargs(operator, {
        "filepath": str(path),
        "export_format": "GLB",
        "use_selection": True,
        "export_yup": True,
        "export_apply": apply_modifiers,
        "export_animations": False,
        "export_skins": not apply_modifiers,
        "export_morph": not apply_modifiers,
        "export_morph_normal": not apply_modifiers,
        # The sidecar carries the evaluated comic-frame pose.  Keep the GLB's
        # reusable skin bound to Blender's actual rest armature so applying the
        # captured pose in Webtoon never treats the current frame as a new bind
        # pose.  Runtime RNA filtering keeps this compatible with 4.5.x patch
        # releases that expose a smaller option set.
        "export_rest_position_armature": True,
        "export_def_bones": False,
        "export_armature_object_remove": False,
        "export_flatten_bones_hierarchy": False,
        "export_current_frame": False,
        "export_materials": "EXPORT",
        "export_extras": True,
        "export_attributes": True,
        "export_cameras": True,
        "export_lights": True,
        "export_hierarchy_full_collections": True,
    })
    result = operator(**kwargs)
    if "FINISHED" not in result or not path.is_file():
        raise RuntimeError("Blender glTF exporter did not produce a GLB")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _select_only(context: Any, objects: Iterable[Any]) -> None:
    selected = set(objects)
    for obj in context.view_layer.objects:
        obj.select_set(obj in selected)
    context.view_layer.objects.active = next(iter(selected), None)


def export_geometry_staging(
    context: Any,
    objects: Iterable[Any],
    output_directory: str | Path,
) -> GeometryExportResult:
    """Export a reusable base GLB plus per-frame baked fallback GLBs."""

    if bpy is None:
        raise RuntimeError("Blender is required for GLB export")
    objects = tuple(objects)
    if not objects:
        raise UnsupportedSceneError("At least one participating object is required")
    if context.mode != "OBJECT":
        raise UnsupportedSceneError("Update Comic Frame must run in Object Mode")
    policies = validate_export_objects(objects)
    expectations = collect_export_identity_expectations(objects)
    output = Path(output_directory)
    output.mkdir(parents=True, exist_ok=True)
    prior_selected = tuple(context.selected_objects)
    prior_active = context.view_layer.objects.active
    source_files: dict[str, Path] = {}
    warnings: list[str] = []
    fallbacks: dict[str, Any] = {}
    object_resources: dict[str, Any] = {}
    try:
        _select_only(context, objects)
        base_temp = output / "base.glb"
        _export_glb(base_temp, apply_modifiers=False)
        base_report = validate_staged_glb_export(
            base_temp,
            expected_identities=expectations.identities,
            bone_ids=expectations.bone_ids,
        )
        warnings.extend(base_report.warnings)
        base_hash = _sha256(base_temp)
        base_relative = f"cache/blobs/{base_hash}.glb"
        source_files[base_relative] = base_temp

        for obj in objects:
            object_id = identity_for(obj)
            policy = policies.get(object_id)
            if object_id is None or policy is None:
                continue
            if policy.mode in {"baked_fallback", "evaluated_static"}:
                _select_only(context, (obj,))
                resource_temp = output / f"evaluated-{object_id}.glb"
                _export_glb(resource_temp, apply_modifiers=True)
                resource_report = validate_staged_glb_export(
                    resource_temp,
                    expected_identities={object_id: "object"},
                )
                warnings.extend(resource_report.warnings)
                fallback_hash = _sha256(resource_temp)
                relative = f"cache/blobs/{fallback_hash}.glb"
                source_files.setdefault(relative, resource_temp)
                resource = {
                    "sha256": fallback_hash,
                    "path": relative,
                    "reason": policy.reason,
                    "modifiers_applied": True,
                }
                if policy.mode == "baked_fallback":
                    resource["pose_and_shape_controls_available"] = False
                    fallbacks[object_id] = resource
                    warnings.append(f"{obj.name}: {policy.reason}")
                else:
                    object_resources[object_id] = resource
            elif policy.reason:
                warnings.append(f"{obj.name}: {policy.reason}")
    finally:
        _select_only(context, prior_selected)
        context.view_layer.objects.active = prior_active

    revision_payload = {
        "base": base_hash,
        "objects": {
            object_id: record["sha256"] for object_id, record in object_resources.items()
        },
        "fallbacks": {
            object_id: record["sha256"] for object_id, record in fallbacks.items()
        },
    }
    revision = hashlib.sha256(json.dumps(
        revision_payload, sort_keys=True, separators=(",", ":"),
    ).encode("utf-8")).hexdigest()
    cache_manifest = {
        "schema_version": 1,
        "revision": revision,
        "source_revision": 0,
        "source_hashes": {
            relative: _sha256(path) for relative, path in source_files.items()
        },
        "base_glb_hash": base_hash,
        "object_resources": {
            object_id: record["sha256"] for object_id, record in object_resources.items()
        },
        "baked_variants": {
            object_id: record["sha256"] for object_id, record in fallbacks.items()
        },
        "freestyle_edges": {},
        "extensions": {
            "resource_paths": {
                base_hash: base_relative,
                **{record["sha256"]: record["path"] for record in object_resources.values()},
                **{record["sha256"]: record["path"] for record in fallbacks.values()},
            },
            "object_policies": {
                object_id: {
                    "mode": policy.mode,
                    "reason": policy.reason,
                }
                for object_id, policy in sorted(policies.items())
            },
            "baked_fallback_metadata": fallbacks,
        },
    }
    return GeometryExportResult(source_files, cache_manifest, tuple(warnings))


__all__ = [
    "ExportIdentityExpectations",
    "GLBExportIntegrityError",
    "GLBIntegrityReport",
    "GeometryExportResult",
    "ModifierDescriptor",
    "ModifierPolicy",
    "REJECTED_MODIFIERS",
    "SIMULATION_MODIFIERS",
    "UnsupportedSceneError",
    "classify_blender_object",
    "classify_modifier_stack",
    "collect_export_identity_expectations",
    "export_geometry_staging",
    "parse_staged_glb_json",
    "validate_staged_glb_export",
    "validate_export_objects",
]
