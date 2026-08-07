"""Apply one comic frame's typed Blender state to a neutral render scene."""

from __future__ import annotations

import copy
import math
from collections.abc import Mapping

import numpy as np

from .renderer.camera import quaternion_from_rotation_matrix, quaternion_rotate
from .renderer.projection import ProjectionMode
from .renderer.scene import SceneData


def _merged(base: object, override: object) -> object:
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        result = copy.deepcopy(dict(base))
        for key, value in override.items():
            result[str(key)] = _merged(result.get(str(key)), value)
        return result
    return copy.deepcopy(override)


def _matrix(value: object) -> np.ndarray | None:
    if not isinstance(value, (list, tuple)) or len(value) != 16:
        return None
    result = np.asarray(value, dtype=np.float64).reshape((4, 4), order="F")
    return result if np.all(np.isfinite(result)) else None


def apply_pose_and_shape_state(scene: SceneData, frame: object) -> tuple[str, ...]:
    """Apply captured/effective pose matrices and morph weights in-place.

    Pose-bone matrices are captured in armature-object space.  For a child bone
    they are converted back to a parent-relative node matrix.  Shape-key UUIDs
    remain authoritative in the sidecar; glTF 2 stores target names rather than
    target UUIDs, so the current captured name provides the bridge.
    """

    source = getattr(frame, "source_state", {})
    overrides = getattr(frame, "presentation_overrides", {})
    fallback_ids = {
        str(value) for value in getattr(frame, "baked_variant_hashes", {})
        if str(value) != "scene"
    }
    warnings: list[str] = []

    pose_state = _merged(
        source.get("poses", {}) if isinstance(source, Mapping) else {},
        overrides.get("poses", {}) if isinstance(overrides, Mapping) else {},
    )
    if isinstance(pose_state, Mapping):
        for armature_id, raw_bones in pose_state.items():
            if str(armature_id) in fallback_ids or not isinstance(raw_bones, Mapping):
                continue
            absolute: dict[str, np.ndarray] = {}
            for bone_id, record in raw_bones.items():
                if not isinstance(record, Mapping):
                    continue
                value = _matrix(record.get("matrix"))
                if value is not None and str(bone_id) in scene.nodes:
                    absolute[str(bone_id)] = value
                elif "matrix_basis" in record:
                    warnings.append(
                        f"Pose override for {bone_id} has no evaluated matrix and was preserved as metadata."
                    )
            for bone_id, value in absolute.items():
                node = scene.nodes[bone_id]
                parent_pose = absolute.get(str(node.parent_id))
                if parent_pose is None:
                    node.local_matrix = value.copy()
                else:
                    try:
                        inverse_parent = np.linalg.inv(parent_pose)
                    except np.linalg.LinAlgError:
                        inverse_parent = np.linalg.pinv(parent_pose)
                    node.local_matrix = inverse_parent @ value

    shape_state = _merged(
        source.get("shape_keys", {}) if isinstance(source, Mapping) else {},
        overrides.get("shape_keys", {}) if isinstance(overrides, Mapping) else {},
    )
    if isinstance(shape_state, Mapping):
        for object_id, raw_keys in shape_state.items():
            object_id = str(object_id)
            node = scene.nodes.get(object_id)
            if (
                node is None or node.mesh_index is None
                or object_id in fallback_ids or not isinstance(raw_keys, Mapping)
            ):
                continue
            mesh = scene.meshes[node.mesh_index]
            if not mesh.primitives:
                continue
            values_by_name: dict[str, float] = {}
            for record in raw_keys.values():
                if isinstance(record, Mapping):
                    name = str(record.get("name", ""))
                    value = 0.0 if bool(record.get("mute", False)) else record.get("value", 0.0)
                else:
                    name, value = "", record
                try:
                    numeric = float(value)
                except (TypeError, ValueError):
                    continue
                if name and math.isfinite(numeric):
                    values_by_name[name] = numeric
            targets = mesh.primitives[0].morph_targets
            defaults = node.morph_weights or mesh.default_morph_weights
            node.morph_weights = tuple(
                values_by_name.get(
                    target.name,
                    defaults[index] if index < len(defaults) else 0.0,
                )
                for index, target in enumerate(targets)
            )

    scene.recompute_world_matrices()
    if warnings:
        scene.warnings = tuple(dict.fromkeys((*scene.warnings, *warnings)))
    return tuple(warnings)


def apply_blender_camera_state(
    scene: SceneData, frame: object, renderer_settings: Mapping[str, object],
) -> bool:
    """Use the captured active Blender camera until Webtoon navigation overrides it."""

    source = getattr(frame, "source_state", {})
    overrides = getattr(frame, "presentation_overrides", {})
    camera_state = _merged(
        source.get("cameras", {}) if isinstance(source, Mapping) else {},
        overrides.get("cameras", {}) if isinstance(overrides, Mapping) else {},
    )
    if not isinstance(camera_state, Mapping):
        return False
    extensions = getattr(frame, "extensions", {})
    active_id = str(
        extensions.get("active_camera_id", "")
        if isinstance(extensions, Mapping) else ""
    )
    if not active_id:
        active_id = next((
            node.node_id for node in scene.nodes.values()
            if node.camera_index is not None
        ), "")
    node = scene.nodes.get(active_id)
    record = camera_state.get(active_id)
    if node is None or node.camera_index is None or not isinstance(record, Mapping):
        return False

    camera = scene.cameras[node.camera_index]
    camera.raw_source = copy.deepcopy(dict(record))
    camera_type = str(record.get("type", "PERSP")).upper()
    camera.perspective = camera_type != "ORTHO"
    camera.ortho_height = max(
        0.001, float(record.get("ortho_scale", camera.ortho_height))
    )
    camera.near = max(1.0e-6, float(record.get("clip_start", camera.near)))
    camera.far = max(camera.near + 1.0e-6, float(
        record.get("clip_end", camera.far)
    ))
    raw_fov = record.get("fov_y_radians", record.get("fov_radians", camera.yfov))
    camera.yfov = max(1.0e-6, min(math.pi - 1.0e-6, float(raw_fov)))
    camera.shift_x = float(record.get("shift_x", camera.shift_x))
    camera.shift_y = float(record.get("shift_y", camera.shift_y))
    if not math.isfinite(camera.shift_x) or not math.isfinite(camera.shift_y):
        camera.shift_x = camera.shift_y = 0.0
        scene.warnings = tuple(dict.fromkeys((
            *scene.warnings,
            f"Camera {node.name!r} has invalid lens-shift values; zero shift is used.",
        )))
    sensor_fit = str(record.get("sensor_fit", camera.sensor_fit)).upper()
    camera.sensor_fit = (
        sensor_fit
        if sensor_fit in ("AUTO", "HORIZONTAL", "VERTICAL")
        else "AUTO"
    )

    if "projection" not in renderer_settings:
        scene.projection.mode = (
            ProjectionMode.ORTHOGRAPHIC
            if camera_type == "ORTHO" else ProjectionMode.PERSPECTIVE
        )
    if "fov" not in renderer_settings:
        scene.projection.vertical_fov_deg = math.degrees(camera.yfov)
    if "ortho_height" not in renderer_settings:
        scene.projection.ortho_height = camera.ortho_height
    requested_shift_x = float(renderer_settings.get("shift_x", camera.shift_x))
    requested_shift_y = float(renderer_settings.get("shift_y", camera.shift_y))
    scene.projection.shift_x = (
        requested_shift_x if math.isfinite(requested_shift_x) else camera.shift_x
    )
    scene.projection.shift_y = (
        requested_shift_y if math.isfinite(requested_shift_y) else camera.shift_y
    )
    requested_fit = str(
        renderer_settings.get("sensor_fit", camera.sensor_fit)
    ).upper()
    scene.projection.sensor_fit = (
        requested_fit
        if requested_fit in ("AUTO", "HORIZONTAL", "VERTICAL")
        else camera.sensor_fit
    )

    navigation = (
        overrides.get("camera_navigation", {})
        if isinstance(overrides, Mapping) else {}
    )
    if isinstance(navigation, Mapping) and navigation:
        return True

    linear = node.world_matrix[:3, :3]
    u, _singular, vh = np.linalg.svd(linear)
    rotation = u @ vh
    if np.linalg.det(rotation) < 0.0:
        u[:, -1] *= -1.0
        rotation = u @ vh
    orientation = quaternion_from_rotation_matrix(rotation)
    position = node.world_origin
    distance = 1.0
    forward = quaternion_rotate(
        orientation, np.array([0.0, 0.0, -1.0], dtype=np.float64)
    )
    scene.active_camera.orientation = orientation
    scene.active_camera.distance = distance
    scene.active_camera.target = position + forward * distance
    return True


__all__ = ["apply_blender_camera_state", "apply_pose_and_shape_state"]
