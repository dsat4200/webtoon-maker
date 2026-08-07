"""Non-destructive application preview with an in-memory restoration snapshot."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping

try:
    import bpy  # type: ignore
    from mathutils import Matrix  # type: ignore
except ImportError:  # pragma: no cover
    bpy = None
    Matrix = None

from .capture import (
    gltf_column_major_to_blender_rows,
    matrix_gltf_column_major,
)
from .identities import identity_for, load_registry, shape_key_entry_identity


_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PREVIEW_SNAPSHOTS: dict[int, "PreviewSnapshot"] = {}


@dataclass(frozen=True)
class PreviewSnapshot:
    scene_pointer: int
    objects: Mapping[str, Any]
    collections: Mapping[str, Any]


def _scene_pointer(scene: Any) -> int:
    return int(scene.as_pointer())


def preview_active(scene: Any) -> bool:
    return _scene_pointer(scene) in _PREVIEW_SNAPSHOTS


def _object_map() -> dict[str, Any]:
    return {
        object_id: obj
        for obj in bpy.data.objects
        if (object_id := identity_for(obj)) is not None
    }


def _collection_map() -> dict[str, Any]:
    return {
        collection_id: collection
        for collection in bpy.data.collections
        if (collection_id := identity_for(collection)) is not None
    }


def _snapshot(scene: Any) -> PreviewSnapshot:
    registry = load_registry()
    objects: dict[str, Any] = {}
    for object_id, obj in _object_map().items():
        value: dict[str, Any] = {
            "matrix_world": matrix_gltf_column_major(obj.matrix_world),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "hide_select": bool(obj.hide_select),
        }
        if obj.type == "CAMERA":
            camera = obj.data
            value["camera"] = {
                "type": camera.type,
                "lens_mm": camera.lens,
                "fov_y_radians": camera.angle_y,
                "ortho_scale": camera.ortho_scale,
                "clip_start": camera.clip_start,
                "clip_end": camera.clip_end,
                "shift_x": camera.shift_x,
                "shift_y": camera.shift_y,
            }
        if obj.type == "LIGHT":
            light = obj.data
            value["light"] = {
                "type": light.type,
                "color": list(light.color),
                "energy": light.energy,
                "use_shadow": light.use_shadow,
                "use_custom_distance": light.use_custom_distance,
                "cutoff_distance": light.cutoff_distance,
                "shadow_soft_size": light.shadow_soft_size,
                "spot_size": getattr(light, "spot_size", None),
                "spot_blend": getattr(light, "spot_blend", None),
                "shape": getattr(light, "shape", None),
                "size": getattr(light, "size", None),
                "size_y": getattr(light, "size_y", None),
            }
        if obj.pose is not None:
            value["pose"] = {
                identity_for(bone.bone): matrix_gltf_column_major(bone.matrix_basis)
                for bone in obj.pose.bones if identity_for(bone.bone) is not None
            }
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        if shape_keys is not None:
            key_values: dict[str, float] = {}
            for index, block in enumerate(shape_keys.key_blocks):
                key_id = shape_key_entry_identity(registry, shape_keys, block, index)
                if key_id is not None:
                    key_values[key_id] = float(block.value)
            value["shape_keys"] = key_values
        objects[object_id] = value
    collections = {
        collection_id: {
            "hide_viewport": bool(collection.hide_viewport),
            "hide_render": bool(collection.hide_render),
            "hide_select": bool(collection.hide_select),
        }
        for collection_id, collection in _collection_map().items()
    }
    return PreviewSnapshot(_scene_pointer(scene), objects, collections)


def _matrix_from_column_major(values: Any) -> Any:
    return Matrix(gltf_column_major_to_blender_rows(values))


def _assign_if_present(target: Any, state: Mapping[str, Any], source: str, attribute: str | None = None) -> None:
    if source not in state or state[source] is None:
        return
    setattr(target, attribute or source, state[source])


def _apply_overrides(scene: Any, overrides: Mapping[str, Any]) -> None:
    objects = _object_map()
    collections = _collection_map()
    for object_id, state in overrides.get("transforms", {}).items():
        obj = objects.get(object_id)
        if obj is not None and isinstance(state, Mapping) and "matrix_world" in state:
            obj.matrix_world = _matrix_from_column_major(state["matrix_world"])
    for object_id, state in overrides.get("visibility", {}).items():
        obj = objects.get(object_id)
        if obj is None:
            continue
        if isinstance(state, bool):
            obj.hide_viewport = not state
            obj.hide_render = not state
            continue
        if not isinstance(state, Mapping):
            continue
        _assign_if_present(obj, state, "hide_viewport")
        _assign_if_present(obj, state, "hide_render")
        _assign_if_present(obj, state, "hide_select")
    collection_overrides = overrides.get(
        "collections", overrides.get("collection_visibility", {}),
    )
    for collection_id, state in collection_overrides.items():
        collection = collections.get(collection_id)
        if collection is None:
            continue
        if isinstance(state, bool):
            collection.hide_viewport = not state
            collection.hide_render = not state
            continue
        if not isinstance(state, Mapping):
            continue
        _assign_if_present(collection, state, "hide_viewport")
        _assign_if_present(collection, state, "hide_render")
        _assign_if_present(collection, state, "hide_select")
        if state.get("included") is False:
            collection.hide_viewport = True
            collection.hide_render = True
    for object_id, state in overrides.get("cameras", {}).items():
        obj = objects.get(object_id)
        if obj is None or obj.type != "CAMERA" or not isinstance(state, Mapping):
            continue
        camera = obj.data
        _assign_if_present(camera, state, "type")
        _assign_if_present(camera, state, "lens_mm", "lens")
        if "fov_y_radians" in state and hasattr(camera, "angle_y"):
            camera.angle_y = float(state["fov_y_radians"])
        elif "fov_radians" in state and hasattr(camera, "angle"):
            camera.angle = float(state["fov_radians"])
        _assign_if_present(camera, state, "ortho_scale")
        _assign_if_present(camera, state, "clip_start")
        _assign_if_present(camera, state, "clip_end")
        _assign_if_present(camera, state, "shift_x")
        _assign_if_present(camera, state, "shift_y")
    for object_id, state in overrides.get("lights", {}).items():
        obj = objects.get(object_id)
        if obj is None or obj.type != "LIGHT" or not isinstance(state, Mapping):
            continue
        light = obj.data
        for source, attribute in (
            ("type", "type"), ("color", "color"), ("energy", "energy"),
            ("use_shadow", "use_shadow"),
            ("use_custom_distance", "use_custom_distance"),
            ("cutoff_distance", "cutoff_distance"),
            ("shadow_soft_size", "shadow_soft_size"), ("spot_size", "spot_size"),
            ("spot_blend", "spot_blend"), ("shape", "shape"),
            ("size", "size"), ("size_y", "size_y"),
        ):
            if hasattr(light, attribute):
                _assign_if_present(light, state, source, attribute)
    for object_id, pose_state in overrides.get("poses", {}).items():
        obj = objects.get(object_id)
        if obj is None or obj.pose is None or not isinstance(pose_state, Mapping):
            continue
        by_id = {identity_for(bone.bone): bone for bone in obj.pose.bones}
        for bone_id, state in pose_state.items():
            bone = by_id.get(bone_id)
            if bone is not None and isinstance(state, Mapping) and "matrix_basis" in state:
                bone.matrix_basis = _matrix_from_column_major(state["matrix_basis"])
    registry = load_registry()
    for object_id, key_state in overrides.get("shape_keys", {}).items():
        obj = objects.get(object_id)
        shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None) if obj is not None else None
        if shape_keys is None or not isinstance(key_state, Mapping):
            continue
        by_id = {
            shape_key_entry_identity(registry, shape_keys, block, index): block
            for index, block in enumerate(shape_keys.key_blocks)
        }
        for key_id, state in key_state.items():
            block = by_id.get(key_id)
            if block is not None:
                block.value = float(state.get("value", state) if isinstance(state, Mapping) else state)
    scene.view_layers.update()


def apply_preview(scene: Any, overrides: Mapping[str, Any]) -> None:
    """Apply presentation data to the session only; never save or keyframe."""

    if bpy is None:
        raise RuntimeError("Blender is required for preview")
    pointer = _scene_pointer(scene)
    if pointer in _PREVIEW_SNAPSHOTS:
        raise RuntimeError("A Webtoon comic-frame preview is already active")
    snapshot = _snapshot(scene)
    _PREVIEW_SNAPSHOTS[pointer] = snapshot
    try:
        _apply_overrides(scene, overrides)
    except Exception:
        restore_preview(scene)
        raise


def restore_preview(scene: Any) -> bool:
    if bpy is None:
        raise RuntimeError("Blender is required for preview")
    snapshot = _PREVIEW_SNAPSHOTS.pop(_scene_pointer(scene), None)
    if snapshot is None:
        return False
    _apply_overrides(scene, {
        "transforms": {
            object_id: {"matrix_world": value["matrix_world"]}
            for object_id, value in snapshot.objects.items()
        },
        "visibility": {
            object_id: {
                "hide_viewport": value["hide_viewport"],
                "hide_render": value["hide_render"],
                "hide_select": value["hide_select"],
            }
            for object_id, value in snapshot.objects.items()
        },
        "collections": snapshot.collections,
        "cameras": {
            object_id: value["camera"]
            for object_id, value in snapshot.objects.items() if "camera" in value
        },
        "lights": {
            object_id: value["light"]
            for object_id, value in snapshot.objects.items() if "light" in value
        },
        "poses": {
            object_id: {
                bone_id: {"matrix_basis": matrix}
                for bone_id, matrix in value["pose"].items()
            }
            for object_id, value in snapshot.objects.items() if "pose" in value
        },
        "shape_keys": {
            object_id: {
                key_id: {"value": number}
                for key_id, number in value["shape_keys"].items()
            }
            for object_id, value in snapshot.objects.items() if "shape_keys" in value
        },
    })
    return True


def load_comic_frame_overrides(chapter_root: str | Path, comic_frame_id: str) -> Mapping[str, Any]:
    """Read a Webtoon frame sidecar using a fixed, traversal-safe path."""

    if not isinstance(comic_frame_id, str) or not _SAFE_ID.fullmatch(comic_frame_id):
        raise ValueError("Comic frame ID is unsafe")
    root = Path(chapter_root).expanduser().resolve(strict=True)
    path = root / "blender" / "frames" / f"{comic_frame_id}.json"
    if path.is_symlink():
        raise ValueError("Comic frame sidecar cannot be a filesystem link")
    resolved = path.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("Comic frame path escapes the chapter") from exc
    if resolved.stat().st_size > 16 * 1024 * 1024:
        raise ValueError("Comic frame sidecar is linked or too large")
    value = json.loads(
        resolved.read_text(encoding="utf-8"),
        parse_constant=lambda token: (_ for _ in ()).throw(ValueError(f"Invalid number {token}")),
    )
    if not isinstance(value, dict):
        raise ValueError("Comic frame sidecar must be an object")
    overrides = value.get("presentation_overrides", value.get("overrides", {}))
    if not isinstance(overrides, dict):
        raise ValueError("Comic frame presentation overrides must be an object")
    return overrides


__all__ = [
    "PreviewSnapshot",
    "apply_preview",
    "load_comic_frame_overrides",
    "preview_active",
    "restore_preview",
]
