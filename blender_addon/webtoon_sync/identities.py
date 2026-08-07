"""Persistent Blender-file and datablock identities with conservative repair."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
from typing import Any, Callable, Iterable, Mapping
import uuid

try:  # The planning/validation helpers are intentionally importable in pytest.
    import bpy  # type: ignore
except ImportError:  # pragma: no cover - exercised outside Blender by design.
    bpy = None


IDENTITY_PROPERTY = "webtoon_uuid"
REGISTRY_TEXT_NAME = ".webtoon_sync_registry"
REGISTRY_SCHEMA_VERSION = 1


class IdentityRegistryError(ValueError):
    pass


def normalized_uuid(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        return str(uuid.UUID(value))
    except ValueError:
        return None


@dataclass(frozen=True)
class IdentityRecord:
    kind: str
    owner_key: str
    name: str
    identity: str | None
    linked: bool = False
    target: Any = field(default=None, compare=False, repr=False)


@dataclass(frozen=True)
class IdentityRepairPlan:
    assignments: Mapping[str, str]
    ambiguous: Mapping[str, tuple[IdentityRecord, ...]]
    linked: tuple[IdentityRecord, ...]
    warnings: tuple[str, ...]

    @property
    def can_publish(self) -> bool:
        return not self.ambiguous and not self.linked


@dataclass(frozen=True)
class BlenderIdentityReport:
    file_uuid: str
    assigned_count: int
    repaired_duplicate_count: int
    ambiguous: Mapping[str, tuple[str, ...]]
    linked: tuple[str, ...]
    warnings: tuple[str, ...]

    @property
    def can_publish(self) -> bool:
        return not self.ambiguous and not self.linked


def plan_identity_repairs(
    records: Iterable[IdentityRecord],
    owner_hints: Mapping[str, str] | None = None,
    *,
    uuid_factory: Callable[[], str] = lambda: str(uuid.uuid4()),
) -> IdentityRepairPlan:
    """Plan safe assignments, blocking duplicates with ambiguous ownership.

    Blender copies custom properties when datablocks are duplicated.  A prior
    registry owner hint lets us retain the original UUID and mint IDs for the
    copies.  Without exactly one matching hint, choosing an owner would be data
    corruption, so publication is blocked and the UI reports the candidates.
    """

    records = tuple(records)
    owner_hints = dict(owner_hints or {})
    assignments: dict[str, str] = {}
    ambiguous: dict[str, tuple[IdentityRecord, ...]] = {}
    linked = tuple(record for record in records if record.linked)
    warnings: list[str] = []
    groups: dict[str, list[IdentityRecord]] = {}
    used: set[str] = set()
    for record in records:
        identity = normalized_uuid(record.identity)
        if record.identity is not None and identity is None:
            warnings.append(f"{record.kind} {record.name!r} had an invalid UUID and needs a new one.")
        if identity is not None:
            groups.setdefault(identity, []).append(record)
            used.add(identity)

    def fresh() -> str:
        for _attempt in range(1000):
            candidate = normalized_uuid(uuid_factory())
            if candidate is not None and candidate not in used:
                used.add(candidate)
                return candidate
        raise IdentityRegistryError("UUID generator failed to produce a unique valid UUID")

    for record in records:
        if normalized_uuid(record.identity) is None and not record.linked:
            assignments[record.owner_key] = fresh()

    for identity, duplicates in sorted(groups.items()):
        if len(duplicates) < 2:
            continue
        matching = [record for record in duplicates if record.owner_key == owner_hints.get(identity)]
        if len(matching) != 1 or any(record.linked for record in duplicates):
            ambiguous[identity] = tuple(duplicates)
            continue
        original = matching[0]
        for record in duplicates:
            if record is not original:
                assignments[record.owner_key] = fresh()
        warnings.append(
            f"Repaired {len(duplicates) - 1} copied {original.kind.lower()} UUID(s); "
            f"kept the registered owner {original.name!r}."
        )
    return IdentityRepairPlan(assignments, ambiguous, linked, tuple(warnings))


def _empty_registry() -> dict[str, Any]:
    return {
        "schema_version": REGISTRY_SCHEMA_VERSION,
        "file_uuid": str(uuid.uuid4()),
        "owners": {},
        "shape_key_entries": {},
        "shape_key_entry_meta": {},
        "bindings": [],
        "source_revision": 0,
    }


def load_registry() -> dict[str, Any]:
    if bpy is None:
        raise RuntimeError("Blender is required to load the identity registry")
    text = bpy.data.texts.get(REGISTRY_TEXT_NAME)
    if text is None or not text.as_string().strip():
        return _empty_registry()
    try:
        value = json.loads(text.as_string())
    except json.JSONDecodeError as exc:
        raise IdentityRegistryError("Webtoon identity registry is malformed JSON") from exc
    if not isinstance(value, dict):
        raise IdentityRegistryError("Webtoon identity registry must be an object")
    version = value.get("schema_version")
    if version != REGISTRY_SCHEMA_VERSION:
        direction = "newer" if isinstance(version, int) and version > REGISTRY_SCHEMA_VERSION else "unsupported"
        raise IdentityRegistryError(f"Webtoon identity registry is {direction} (schema {version!r})")
    defaults = _empty_registry()
    for key, default in defaults.items():
        value.setdefault(key, default)
    if normalized_uuid(value.get("file_uuid")) is None:
        raise IdentityRegistryError("Webtoon file UUID is invalid; use Fork Source Identity")
    if (
        not isinstance(value["owners"], dict)
        or not isinstance(value["shape_key_entries"], dict)
        or not isinstance(value["shape_key_entry_meta"], dict)
    ):
        raise IdentityRegistryError("Webtoon identity owner maps are invalid")
    if not isinstance(value["bindings"], list):
        raise IdentityRegistryError("Webtoon chapter bindings are invalid")
    return value


def save_registry(registry: Mapping[str, Any]) -> None:
    if bpy is None:
        raise RuntimeError("Blender is required to save the identity registry")
    encoded = json.dumps(registry, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2)
    text = bpy.data.texts.get(REGISTRY_TEXT_NAME) or bpy.data.texts.new(REGISTRY_TEXT_NAME)
    text.clear()
    text.write(encoded)
    text.use_fake_user = True


def _target_owner_key(kind: str, target: Any) -> str:
    session_uid = getattr(target, "session_uid", 0)
    if isinstance(session_uid, int) and session_uid:
        return f"{kind}:{session_uid}"
    return f"{kind}:name:{getattr(target, 'name_full', getattr(target, 'name', 'unnamed'))}"


def _record(kind: str, target: Any) -> IdentityRecord:
    linked = bool(getattr(target, "library", None) or getattr(target, "override_library", None))
    try:
        identity = target.get(IDENTITY_PROPERTY)
    except (AttributeError, TypeError):
        identity = None
    return IdentityRecord(
        kind=kind,
        owner_key=_target_owner_key(kind, target),
        name=getattr(target, "name_full", getattr(target, "name", "unnamed")),
        identity=identity,
        linked=linked,
        target=target,
    )


def collect_blender_identity_records(registry: Mapping[str, Any]) -> tuple[IdentityRecord, ...]:
    if bpy is None:
        raise RuntimeError("Blender is required to inspect datablock identities")
    records: list[IdentityRecord] = []
    collections = (
        ("SCENE", bpy.data.scenes),
        ("COLLECTION", bpy.data.collections),
        ("OBJECT", bpy.data.objects),
        ("MESH", bpy.data.meshes),
        ("ARMATURE", bpy.data.armatures),
        ("CAMERA", bpy.data.cameras),
        ("LIGHT", bpy.data.lights),
        ("MATERIAL", bpy.data.materials),
        ("SHAPE_KEYS", bpy.data.shape_keys),
    )
    for kind, values in collections:
        records.extend(_record(kind, value) for value in values)
    # Bones support custom properties but are not ID datablocks and have no
    # session_uid.  Their owner key incorporates the armature datablock identity.
    for armature in bpy.data.armatures:
        armature_key = normalized_uuid(armature.get(IDENTITY_PROPERTY)) or _target_owner_key("ARMATURE", armature)
        for bone in armature.bones:
            linked = bool(getattr(armature, "library", None) or getattr(armature, "override_library", None))
            records.append(IdentityRecord(
                kind="BONE",
                owner_key=f"BONE:{armature_key}:{bone.name}",
                name=f"{armature.name}/{bone.name}",
                identity=bone.get(IDENTITY_PROPERTY),
                linked=linked,
                target=bone,
            ))
    # KeyBlock custom-property support is not stable across Blender releases;
    # keep per-entry UUIDs in the private file registry instead.
    entries = registry.get("shape_key_entries", {})
    entry_meta = registry.get("shape_key_entry_meta", {})
    for key_data in bpy.data.shape_keys:
        parent_key = _target_owner_key("SHAPE_KEYS", key_data)
        used: set[str] = set()
        for index, block in enumerate(key_data.key_blocks):
            candidates = [
                identity for identity, metadata in entry_meta.items()
                if identity not in used and isinstance(metadata, dict)
                and metadata.get("parent_key") == parent_key
            ]
            by_name = [
                identity for identity in candidates
                if entry_meta[identity].get("name") == block.name
            ]
            by_index = [
                identity for identity in candidates
                if entry_meta[identity].get("index") == index
            ]
            identity = (
                by_name[0] if len(by_name) == 1
                else by_index[0] if len(by_index) == 1
                else entries.get(f"SHAPE_KEY_ENTRY:{parent_key}:{index}")
            )
            if isinstance(identity, str):
                used.add(identity)
            owner_key = f"SHAPE_KEY_ENTRY:{parent_key}:{index}"
            records.append(IdentityRecord(
                kind="SHAPE_KEY_ENTRY",
                owner_key=owner_key,
                name=f"{key_data.name}/{block.name}",
                identity=identity,
                linked=bool(getattr(key_data, "library", None) or getattr(key_data, "override_library", None)),
                target=(key_data, block, index, parent_key),
            ))
    return tuple(records)


def ensure_blender_identities() -> BlenderIdentityReport:
    """Assign missing IDs, repair proven duplicates, and persist owner hints."""

    registry = load_registry()
    records = collect_blender_identity_records(registry)
    plan = plan_identity_repairs(records, registry.get("owners", {}))
    entry_map: dict[str, str] = {}
    entry_meta: dict[str, dict[str, Any]] = {}
    by_key = {record.owner_key: record for record in records}
    repaired_duplicate_count = 0
    for owner_key, identity in plan.assignments.items():
        record = by_key[owner_key]
        if normalized_uuid(record.identity) is not None:
            repaired_duplicate_count += 1
        if record.kind == "SHAPE_KEY_ENTRY":
            entry_map[owner_key] = identity
        elif record.target is not None:
            record.target[IDENTITY_PROPERTY] = identity
    for record in records:
        if record.kind != "SHAPE_KEY_ENTRY":
            continue
        identity = plan.assignments.get(record.owner_key) or normalized_uuid(record.identity)
        if identity is None:
            continue
        key_data, block, index, parent_key = record.target
        entry_map[record.owner_key] = identity
        entry_meta[identity] = {
            "parent_key": parent_key,
            "parent_name": key_data.name,
            "name": block.name,
            "index": index,
        }
    registry["shape_key_entries"] = entry_map
    registry["shape_key_entry_meta"] = entry_meta

    owners: dict[str, str] = {}
    for record in collect_blender_identity_records(registry):
        identity = normalized_uuid(
            record.identity if record.kind == "SHAPE_KEY_ENTRY"
            else record.target.get(IDENTITY_PROPERTY) if record.target is not None
            else record.identity
        )
        if identity is not None and identity not in plan.ambiguous:
            owners[identity] = record.owner_key
    registry["owners"] = owners
    save_registry(registry)
    return BlenderIdentityReport(
        file_uuid=str(uuid.UUID(registry["file_uuid"])),
        assigned_count=len(plan.assignments),
        repaired_duplicate_count=repaired_duplicate_count,
        ambiguous={
            identity: tuple(record.name for record in values)
            for identity, values in plan.ambiguous.items()
        },
        linked=tuple(record.name for record in plan.linked),
        warnings=plan.warnings,
    )


def fork_file_identity() -> str:
    """Create a new source identity while preserving stable datablock IDs."""

    registry = load_registry()
    registry["file_uuid"] = str(uuid.uuid4())
    registry["bindings"] = []
    registry["source_revision"] = 0
    save_registry(registry)
    return registry["file_uuid"]


def update_binding(
    registry: dict[str, Any], *, series_id: str, chapter_id: str,
    comic_frame_id: str, base_revision: int,
) -> None:
    bindings = [
        item for item in registry.get("bindings", [])
        if not (
            isinstance(item, dict)
            and item.get("series_id") == series_id
            and item.get("chapter_id") == chapter_id
            and item.get("comic_frame_id") == comic_frame_id
        )
    ]
    bindings.append({
        "series_id": series_id,
        "chapter_id": chapter_id,
        "comic_frame_id": comic_frame_id,
        "base_revision": base_revision,
    })
    registry["bindings"] = bindings


def identity_for(target: Any) -> str | None:
    try:
        return normalized_uuid(target.get(IDENTITY_PROPERTY))
    except (AttributeError, TypeError):
        return None


def shape_key_entry_identity(
    registry: Mapping[str, Any], shape_keys: Any, block: Any, index: int,
) -> str | None:
    """Resolve a key-block UUID across renames and list reordering."""

    parent_key = _target_owner_key("SHAPE_KEYS", shape_keys)
    metadata = registry.get("shape_key_entry_meta", {})
    if isinstance(metadata, Mapping):
        by_name = [
            identity for identity, value in metadata.items()
            if isinstance(value, Mapping)
            and value.get("parent_key") == parent_key
            and value.get("name") == block.name
        ]
        if len(by_name) == 1:
            return normalized_uuid(by_name[0])
        by_index = [
            identity for identity, value in metadata.items()
            if isinstance(value, Mapping)
            and value.get("parent_key") == parent_key
            and value.get("index") == index
        ]
        if len(by_index) == 1:
            return normalized_uuid(by_index[0])
    entries = registry.get("shape_key_entries", {})
    if isinstance(entries, Mapping):
        return normalized_uuid(entries.get(f"SHAPE_KEY_ENTRY:{parent_key}:{index}"))
    return None


__all__ = [
    "BlenderIdentityReport",
    "IDENTITY_PROPERTY",
    "IdentityRecord",
    "IdentityRegistryError",
    "IdentityRepairPlan",
    "REGISTRY_SCHEMA_VERSION",
    "REGISTRY_TEXT_NAME",
    "collect_blender_identity_records",
    "ensure_blender_identities",
    "fork_file_identity",
    "identity_for",
    "load_registry",
    "normalized_uuid",
    "plan_identity_repairs",
    "save_registry",
    "shape_key_entry_identity",
    "update_binding",
]
