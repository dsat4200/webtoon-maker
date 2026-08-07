"""Typed Blender 4.5 scene capture without embedding geometry in frame JSON."""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import math
import struct
from typing import Any, Iterable, Mapping

try:
    import bpy  # type: ignore
except ImportError:  # pragma: no cover - pure helpers remain testable.
    bpy = None

from .identities import (
    IDENTITY_PROPERTY, identity_for, load_registry, shape_key_entry_identity,
)


MAX_OPAQUE_SEQUENCE = 64
MAX_OPAQUE_MAPPING = 128
MAX_OPAQUE_DEPTH = 8


@dataclass(frozen=True)
class CaptureResult:
    chapter_data: Mapping[str, Any]
    frame_data: Mapping[str, Any]
    warnings: tuple[str, ...]


def finite_json_value(value: Any, *, _depth: int = 0) -> Any:
    """Convert a simple resolved RNA/ID-property value into bounded JSON."""

    if _depth > MAX_OPAQUE_DEPTH:
        raise ValueError("value is nested too deeply")
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite numbers are not syncable")
        return value
    if isinstance(value, Mapping):
        if len(value) > MAX_OPAQUE_MAPPING:
            raise ValueError("mapping is too large")
        result: dict[str, Any] = {}
        for key, child in value.items():
            if not isinstance(key, str) or key == "_RNA_UI":
                continue
            result[key] = finite_json_value(child, _depth=_depth + 1)
        return result
    if isinstance(value, (bytes, bytearray, memoryview)):
        raise ValueError("binary data is not metadata")
    try:
        values = list(value)
    except TypeError as exc:
        raise ValueError(f"unsupported value type {type(value).__name__}") from exc
    if len(values) > MAX_OPAQUE_SEQUENCE:
        raise ValueError("sequence is too large")
    return [finite_json_value(child, _depth=_depth + 1) for child in values]


def matrix_column_major(matrix: Any) -> list[float]:
    """Serialize a 4x4 matrix without changing its coordinate basis."""

    try:
        values = [float(matrix[row][column]) for column in range(4) for row in range(4)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Expected a 4x4 matrix") from exc
    if not all(math.isfinite(value) for value in values):
        raise ValueError("Matrix contains a non-finite value")
    return values


_GLTF_FROM_BLENDER = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, -1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
_BLENDER_FROM_GLTF = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 0.0, -1.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)


def _matrix_rows(matrix: Any) -> list[list[float]]:
    try:
        rows = [[float(matrix[row][column]) for column in range(4)] for row in range(4)]
    except (IndexError, TypeError, ValueError) as exc:
        raise ValueError("Expected a 4x4 matrix") from exc
    if not all(math.isfinite(value) for row in rows for value in row):
        raise ValueError("Matrix contains a non-finite value")
    return rows


def _multiply(left: Any, right: Any) -> list[list[float]]:
    return [
        [sum(left[row][index] * right[index][column] for index in range(4)) for column in range(4)]
        for row in range(4)
    ]


def matrix_gltf_column_major(matrix: Any) -> list[float]:
    """Convert Blender Z-up coordinates to right-handed glTF Y-up coordinates."""

    converted = _multiply(_multiply(_GLTF_FROM_BLENDER, _matrix_rows(matrix)), _BLENDER_FROM_GLTF)
    return [converted[row][column] for column in range(4) for row in range(4)]


def gltf_column_major_to_blender_rows(values: Any) -> list[list[float]]:
    """Convert a serialized glTF matrix back to Blender's Z-up basis."""

    if not isinstance(values, list) or len(values) != 16:
        raise ValueError("Expected a 16-value column-major matrix")
    numbers = [float(value) for value in values]
    if not all(math.isfinite(value) for value in numbers):
        raise ValueError("Matrix contains a non-finite number")
    gltf_rows = [[numbers[column * 4 + row] for column in range(4)] for row in range(4)]
    return _multiply(_multiply(_BLENDER_FROM_GLTF, gltf_rows), _GLTF_FROM_BLENDER)


def _vector(value: Any) -> list[float]:
    result = [float(component) for component in value]
    if not all(math.isfinite(component) for component in result):
        raise ValueError("Vector contains a non-finite value")
    return result


def _custom_metadata(owner: Any) -> dict[str, Any]:
    result: dict[str, Any] = {}
    try:
        items = owner.items()
    except AttributeError:
        return result
    for key, value in items:
        if key in {IDENTITY_PROPERTY, "_RNA_UI"}:
            continue
        try:
            result[key] = finite_json_value(value)
        except ValueError:
            continue
    return result


def _fcurves(animation_data: Any) -> Iterable[Any]:
    if animation_data is None:
        return ()
    result: list[Any] = []
    action = getattr(animation_data, "action", None)
    direct = getattr(action, "fcurves", None)
    if direct is not None:
        result.extend(direct)
    # Blender 4.4+ layered/slotted Actions keep curves in a channel bag rather
    # than on Action.fcurves. Read only the bag for this owner's active slot.
    action_slot = getattr(animation_data, "action_slot", None)
    for layer in getattr(action, "layers", ()) if action is not None else ():
        for strip in getattr(layer, "strips", ()):
            for channelbag in getattr(strip, "channelbags", ()):
                if action_slot is None or getattr(channelbag, "slot", None) == action_slot:
                    result.extend(getattr(channelbag, "fcurves", ()))
    drivers = getattr(animation_data, "drivers", None)
    if drivers is not None:
        result.extend(drivers)
    return result


def resolved_keyed_values(owner: Any) -> dict[str, Any]:
    """Capture simple evaluated keyframed/driver values as opaque metadata."""

    result: dict[str, Any] = {}
    animation_data = getattr(owner, "animation_data", None)
    seen: set[str] = set()
    for fcurve in _fcurves(animation_data):
        path = getattr(fcurve, "data_path", "")
        if not isinstance(path, str) or not path or len(path) > 512 or path in seen:
            continue
        seen.add(path)
        try:
            resolved = owner.path_resolve(path)
            result[path] = finite_json_value(resolved)
        except (AttributeError, ValueError, TypeError):
            continue
    return result


def mesh_topology_hash(mesh: Any) -> str:
    """Hash topology indices (not mutable vertex positions) for edge mapping."""

    digest = hashlib.sha256()
    digest.update(struct.pack("<III", len(mesh.vertices), len(mesh.edges), len(mesh.polygons)))
    for edge in mesh.edges:
        digest.update(struct.pack("<II", int(edge.vertices[0]), int(edge.vertices[1])))
    for polygon in mesh.polygons:
        vertices = tuple(int(index) for index in polygon.vertices)
        digest.update(struct.pack("<I", len(vertices)))
        digest.update(struct.pack(f"<{len(vertices)}I", *vertices))
    return digest.hexdigest()


def freestyle_metadata(mesh: Any) -> dict[str, Any]:
    marked: list[list[int]] = []
    for edge in mesh.edges:
        if bool(getattr(edge, "use_freestyle_mark", False)):
            marked.append([int(edge.vertices[0]), int(edge.vertices[1])])
    return {
        "topology_sha256": mesh_topology_hash(mesh),
        "marked_edges": marked,
    }


def _pose_state(obj: Any) -> dict[str, Any]:
    pose = getattr(obj, "pose", None)
    if pose is None:
        return {}
    result: dict[str, Any] = {}
    for bone in pose.bones:
        bone_id = identity_for(getattr(bone, "bone", None))
        if bone_id is None:
            continue
        result[bone_id] = {
            "name": bone.name,
            "matrix": matrix_gltf_column_major(bone.matrix),
            "matrix_basis": matrix_gltf_column_major(bone.matrix_basis),
            "location": _vector(bone.location),
            "rotation_mode": bone.rotation_mode,
            "rotation_quaternion": _vector(bone.rotation_quaternion),
            "rotation_euler": _vector(bone.rotation_euler),
            "scale": _vector(bone.scale),
            "custom": _custom_metadata(bone),
            "opaque_keyed": resolved_keyed_values(bone),
        }
    return result


def _shape_key_state(obj: Any, registry: Mapping[str, Any]) -> dict[str, Any]:
    data = getattr(obj, "data", None)
    shape_keys = getattr(data, "shape_keys", None)
    if shape_keys is None:
        return {}
    result: dict[str, Any] = {}
    for index, block in enumerate(shape_keys.key_blocks):
        entry_id = shape_key_entry_identity(registry, shape_keys, block, index)
        if entry_id is None:
            continue
        result[entry_id] = {
            "name": block.name,
            "value": float(block.value),
            "mute": bool(getattr(block, "mute", False)),
            "slider_min": float(block.slider_min),
            "slider_max": float(block.slider_max),
        }
    return result


def _camera_state(camera: Any) -> dict[str, Any]:
    return {
        "type": camera.type,
        "lens_mm": float(camera.lens),
        "fov_radians": float(camera.angle),
        "fov_x_radians": float(camera.angle_x),
        "fov_y_radians": float(camera.angle_y),
        "ortho_scale": float(camera.ortho_scale),
        "clip_start": float(camera.clip_start),
        "clip_end": float(camera.clip_end),
        "shift_x": float(camera.shift_x),
        "shift_y": float(camera.shift_y),
        "sensor_fit": camera.sensor_fit,
        "sensor_width": float(camera.sensor_width),
        "sensor_height": float(camera.sensor_height),
        "dof_enabled": bool(camera.dof.use_dof),
        "custom": _custom_metadata(camera),
        "opaque_keyed": resolved_keyed_values(camera),
    }


def _light_state(light: Any) -> dict[str, Any]:
    result = {
        "type": light.type,
        "color": _vector(light.color),
        "energy": float(light.energy),
        "use_shadow": bool(light.use_shadow),
        "specular_factor": float(light.specular_factor),
        "diffuse_factor": float(light.diffuse_factor),
        "volume_factor": float(light.volume_factor),
        "use_custom_distance": bool(light.use_custom_distance),
        "cutoff_distance": float(light.cutoff_distance),
        "shadow_soft_size": float(light.shadow_soft_size),
        "custom": _custom_metadata(light),
        "opaque_keyed": resolved_keyed_values(light),
    }
    if light.type == "SPOT":
        result.update({
            "spot_size": float(light.spot_size),
            "spot_blend": float(light.spot_blend),
            "show_cone": bool(light.show_cone),
        })
    if light.type == "AREA":
        result.update({
            "shape": light.shape,
            "size": float(light.size),
            "size_y": float(light.size_y),
            "spread": float(getattr(light, "spread", math.pi)),
        })
    return result


def _material_catalog(material: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": identity_for(material),
        "name": material.name,
        "diffuse_color": _vector(material.diffuse_color),
        "metallic": float(material.metallic),
        "roughness": float(material.roughness),
        "use_nodes": bool(material.use_nodes),
        "custom": _custom_metadata(material),
        "opaque_keyed": resolved_keyed_values(material),
    }
    node_tree = material.node_tree if material.use_nodes else None
    textures: list[dict[str, Any]] = []
    if node_tree is not None:
        for node in node_tree.nodes:
            image = getattr(node, "image", None)
            if image is not None:
                textures.append({
                    "node": node.name,
                    "image_name": image.name,
                    "colorspace": image.colorspace_settings.name,
                    "packed": image.packed_file is not None,
                })
    result["image_textures"] = textures
    return result


def _object_transform(obj: Any, evaluated: Any) -> dict[str, Any]:
    return {
        "matrix_local": matrix_gltf_column_major(obj.matrix_local),
        "matrix_world": matrix_gltf_column_major(evaluated.matrix_world),
        "matrix_parent_inverse": matrix_gltf_column_major(obj.matrix_parent_inverse),
        "blender_components": {
            "location": _vector(obj.location),
            "rotation_mode": obj.rotation_mode,
            "rotation_quaternion": _vector(obj.rotation_quaternion),
            "rotation_axis_angle": _vector(obj.rotation_axis_angle),
            "rotation_euler": _vector(obj.rotation_euler),
            "scale": _vector(obj.scale),
            "delta_location": _vector(obj.delta_location),
            "delta_rotation_euler": _vector(obj.delta_rotation_euler),
            "delta_scale": _vector(obj.delta_scale),
        },
    }


def _collection_parent_map() -> dict[Any, Any]:
    parents: dict[Any, Any] = {}
    for parent in bpy.data.collections:
        for child in parent.children:
            parents[child] = parent
    return parents


def _descendant_collections(collection: Any) -> Iterable[Any]:
    yield collection
    for child in collection.children:
        yield from _descendant_collections(child)


def _participating_collections(scene: Any, included_ids: set[str]) -> tuple[Any, ...]:
    included: dict[str, Any] = {}
    for collection in _descendant_collections(scene.collection):
        collection_id = identity_for(collection)
        if collection_id in included_ids:
            included[collection_id] = collection
    return tuple(included.values())


def capture_sync_data(
    context: Any,
    *,
    included_collection_ids: Iterable[str],
    base_state: Mapping[str, Any] | None = None,
) -> CaptureResult:
    """Capture one Blender scene/timeline frame into chapter and frame payloads."""

    if bpy is None:
        raise RuntimeError("Blender is required for scene capture")
    scene = context.scene
    scene_id = identity_for(scene)
    if scene_id is None:
        raise ValueError("Scene has no webtoon_uuid; validate identities first")
    registry = load_registry()
    requested = set(included_collection_ids)
    participating = _participating_collections(scene, requested)
    participating_ids = {identity_for(collection) for collection in participating}
    objects: dict[str, Any] = {}
    for collection in participating:
        for obj in collection.all_objects:
            object_id = identity_for(obj)
            if object_id is not None:
                objects[object_id] = obj
    depsgraph = context.evaluated_depsgraph_get()
    parents = _collection_parent_map()
    collection_catalog: dict[str, Any] = {}
    collection_visibility: dict[str, Any] = {}
    for collection in _descendant_collections(scene.collection):
        collection_id = identity_for(collection)
        if collection_id is None:
            continue
        parent = parents.get(collection)
        collection_catalog[collection_id] = {
            "name": collection.name,
            "parent_id": identity_for(parent),
            "included": collection_id in participating_ids,
            "custom": _custom_metadata(collection),
            "opaque_keyed": resolved_keyed_values(collection),
        }
        collection_visibility[collection_id] = {
            "included": collection_id in participating_ids,
            "hide_viewport": bool(collection.hide_viewport),
            "hide_render": bool(collection.hide_render),
            "hide_select": bool(collection.hide_select),
        }

    object_catalog: dict[str, Any] = {}
    transforms: dict[str, Any] = {}
    visibility: dict[str, Any] = {}
    poses: dict[str, Any] = {}
    shape_keys: dict[str, Any] = {}
    cameras: dict[str, Any] = {}
    lights: dict[str, Any] = {}
    opaque_keyed: dict[str, Any] = {}
    material_assignments: dict[str, Any] = {}
    freestyle: dict[str, Any] = {}
    used_materials: dict[str, Any] = {}
    warnings: list[str] = []
    for object_id, obj in sorted(objects.items()):
        evaluated = obj.evaluated_get(depsgraph)
        data_id = identity_for(getattr(obj, "data", None))
        object_catalog[object_id] = {
            "name": obj.name,
            "type": obj.type,
            "parent_id": identity_for(obj.parent),
            "data_id": data_id,
            "collection_ids": sorted(
                value for value in (identity_for(collection) for collection in obj.users_collection)
                if value is not None
            ),
            "custom": _custom_metadata(obj),
        }
        transforms[object_id] = _object_transform(obj, evaluated)
        visibility[object_id] = {
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "hide_select": bool(obj.hide_select),
            "hide_get": bool(obj.hide_get()),
            "visible": bool(obj.visible_get(view_layer=context.view_layer)),
        }
        keyed = resolved_keyed_values(obj)
        if keyed:
            opaque_keyed[object_id] = keyed
        pose = _pose_state(obj)
        if pose:
            poses[object_id] = pose
        keys = _shape_key_state(obj, registry)
        if keys:
            shape_keys[object_id] = keys
        data = getattr(obj, "data", None)
        if obj.type == "CAMERA" and data is not None:
            cameras[object_id] = _camera_state(data)
        if obj.type == "LIGHT" and data is not None:
            lights[object_id] = _light_state(data)
        slots: list[str | None] = []
        for slot in obj.material_slots:
            material = slot.material
            material_id = identity_for(material)
            slots.append(material_id)
            if material_id is not None:
                used_materials[material_id] = material
        material_assignments[object_id] = slots
        if obj.type == "MESH" and data is not None:
            freestyle[object_id] = freestyle_metadata(data)

    materials = {
        material_id: _material_catalog(material)
        for material_id, material in sorted(used_materials.items())
    }
    captured_state = {
        "transforms": transforms,
        "visibility": visibility,
        "collections": collection_visibility,
        "collection_visibility": {
            collection_id: bool(
                state["included"] and not state["hide_viewport"] and not state["hide_render"]
            )
            for collection_id, state in collection_visibility.items()
        },
        "poses": poses,
        "shape_keys": shape_keys,
        "cameras": cameras,
        "lights": lights,
        "opaque_keyed_values": opaque_keyed,
    }
    frame_data: dict[str, Any] = {
        "schema_version": 1,
        "source_scene_id": scene_id,
        "timeline_frame": int(scene.frame_current),
        "active_camera_id": identity_for(scene.camera),
        "included_collection_ids": sorted(value for value in participating_ids if value),
        "captured_state": captured_state,
    }
    if base_state is not None:
        frame_data["base_state"] = dict(base_state)
    chapter_data = {
        "schema_version": 1,
        "blend_path_hint": str(bpy.data.filepath),
        "source_scene_id": scene_id,
        "scenes": {
            identity_for(value): {"name": value.name}
            for value in bpy.data.scenes if identity_for(value) is not None
        },
        "collections": collection_catalog,
        "objects": object_catalog,
        "materials": materials,
        "material_assignments": material_assignments,
        "freestyle": freestyle,
        "coordinate_system": {
            "handedness": "right",
            "up_axis": "Y",
            "unit": "meter",
            "matrix_order": "column_major",
        },
    }
    return CaptureResult(chapter_data, frame_data, tuple(warnings))


__all__ = [
    "CaptureResult",
    "capture_sync_data",
    "finite_json_value",
    "freestyle_metadata",
    "gltf_column_major_to_blender_rows",
    "matrix_column_major",
    "matrix_gltf_column_major",
    "mesh_topology_hash",
    "resolved_keyed_values",
]
