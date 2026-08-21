"""Geometry-free Comic View capture and restore for Blender 4.5."""
from __future__ import annotations

import hashlib
import json
import math
import uuid
from typing import Any, Iterable

import bpy

from . import viewport


STATE_VERSION = 3
UUID_KEY = "webtoon_comic_uuid"
INTERNAL_KEYS = {UUID_KEY, "_RNA_UI"}


def _finite(value: object, fallback: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return fallback
    return result if math.isfinite(result) else fallback


def _json_value(value: object) -> Any:
    if isinstance(value, bool):
        return value
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return _finite(value)
    if isinstance(value, str):
        return value
    if hasattr(value, "to_list"):
        value = value.to_list()
    if isinstance(value, Iterable) and not isinstance(value, (bytes, bytearray)):
        result = [_json_value(item) for item in value]
        if len(result) <= 32 and all(
            isinstance(item, (bool, int, float, str)) for item in result
        ):
            return result
    raise TypeError(f"Unsupported Comic View value: {type(value).__name__}")


def _custom_properties(value: object) -> dict[str, Any]:
    result: dict[str, Any] = {}
    keys = getattr(value, "keys", None)
    if not callable(keys):
        return result
    for key in keys():
        if str(key) in INTERNAL_KEYS:
            continue
        try:
            result[str(key)] = _json_value(value[key])
        except (KeyError, TypeError, ValueError):
            continue
    return result


def _set_custom_properties(value: object, values: dict[str, Any]) -> None:
    for key, item in values.items():
        if key in INTERNAL_KEYS:
            continue
        try:
            value[key] = item
        except (AttributeError, KeyError, TypeError, ValueError):
            continue


def _editable(value: object) -> bool:
    owner = getattr(value, "id_data", None)
    library = getattr(value, "library", None) or getattr(owner, "library", None)
    return library is None and not bool(
        getattr(value, "is_library_indirect", False)
        or getattr(owner, "is_library_indirect", False)
    )


def ensure_uuid(value: object) -> str:
    """Return a persistent local UUID, or a stable linked-data fallback key."""
    existing = ""
    try:
        existing = str(value.get(UUID_KEY, ""))
        if existing:
            existing = uuid.UUID(existing).hex
    except (AttributeError, TypeError, ValueError):
        existing = ""
    if existing:
        return existing
    if _editable(value):
        generated = uuid.uuid4().hex
        try:
            value[UUID_KEY] = generated
            return generated
        except (AttributeError, TypeError):
            pass
    owner = getattr(value, "id_data", None)
    library_value = getattr(value, "library", None) or getattr(owner, "library", None)
    library = getattr(library_value, "filepath", "")
    kind = getattr(getattr(value, "bl_rna", None), "identifier", type(value).__name__)
    name = getattr(value, "name", "")
    owner_kind = getattr(
        getattr(owner, "bl_rna", None), "identifier", type(owner).__name__
    )
    owner_name = getattr(owner, "name", "")
    try:
        path = value.path_from_id()
    except (AttributeError, RuntimeError, TypeError):
        path = ""
    fallback = (
        f"linked:{library}:{owner_kind}:{owner_name}:{kind}:{path}:{name}"
    )
    return hashlib.sha256(fallback.encode("utf-8")).hexdigest()[:32]


def repair_duplicate_uuids(scene: bpy.types.Scene) -> list[str]:
    """Repair copied custom UUIDs deterministically before a capture."""
    warnings: list[str] = []
    candidates: list[object] = []
    candidates.extend(scene.objects)
    candidates.extend(_scene_collections(scene))
    for obj in scene.objects:
        if obj.data is not None and obj.type in {"CAMERA", "LIGHT"}:
            candidates.append(obj.data)
        if obj.pose is not None:
            candidates.extend(bone.bone for bone in obj.pose.bones)
        candidates.extend(obj.modifiers)
    known_names: dict[str, set[str]] = {}
    for view in getattr(scene, "webtoon_comic_views", ()):
        try:
            stored = json.loads(view.state_json)
        except (AttributeError, TypeError, json.JSONDecodeError):
            continue
        for group in ("objects", "collections"):
            for record in stored.get(group, []):
                identifier = str(record.get("uuid", ""))
                if identifier:
                    known_names.setdefault(identifier, set()).add(
                        str(record.get("name", ""))
                    )
        for group in ("cameras", "lights"):
            for record in stored.get(group, []):
                identifier = str(record.get("data_uuid", ""))
                if identifier:
                    known_names.setdefault(identifier, set()).add(
                        str(record.get("data_name", ""))
                    )
        for record in stored.get("poses", []):
            identifier = str(record.get("bone_uuid", ""))
            if identifier:
                known_names.setdefault(identifier, set()).add(
                    str(record.get("bone_name", ""))
                )
        for record in stored.get("modifiers", []):
            identifier = str(record.get("uuid", ""))
            if identifier:
                known_names.setdefault(identifier, set()).add(
                    str(record.get("name", ""))
                )

    groups: dict[str, list[tuple[int, object]]] = {}
    for order, item in enumerate(candidates):
        if not _editable(item):
            continue
        identifier = ensure_uuid(item)
        groups.setdefault(identifier, []).append((order, item))
    for identifier, duplicates in groups.items():
        if len(duplicates) < 2:
            continue
        expected_names = known_names.get(identifier, set())

        def priority(candidate: tuple[int, object]) -> tuple[int, int, int]:
            order, item = candidate
            known = 0 if getattr(item, "name", "") in expected_names else 1
            session_uid = int(getattr(item, "session_uid", 0) or 0)
            return known, session_uid if session_uid > 0 else 2**63, order

        original = min(duplicates, key=priority)[1]
        for _order, item in duplicates:
            if item is original:
                continue
            replacement = uuid.uuid4().hex
            try:
                item[UUID_KEY] = replacement
                warnings.append(
                    f"Reassigned a duplicated Comic UUID on "
                    f"{getattr(item, 'name', type(item).__name__)}"
                )
            except (AttributeError, TypeError):
                warnings.append(
                    f"Could not repair a duplicated Comic UUID on "
                    f"{getattr(item, 'name', type(item).__name__)}"
                )
    return warnings


def _scene_collections(scene: bpy.types.Scene) -> list[bpy.types.Collection]:
    result: list[bpy.types.Collection] = []
    seen: set[int] = set()

    def walk(collection: bpy.types.Collection) -> None:
        pointer = collection.as_pointer()
        if pointer in seen:
            return
        seen.add(pointer)
        result.append(collection)
        for child in collection.children:
            walk(child)

    walk(scene.collection)
    return result


def _vector(value: object) -> list[float]:
    return [_finite(item) for item in value]


def _transform(value: object) -> dict[str, Any]:
    return {
        "location": _vector(value.location),
        "rotation_mode": str(value.rotation_mode),
        "rotation_euler": _vector(value.rotation_euler),
        "rotation_quaternion": _vector(value.rotation_quaternion),
        "rotation_axis_angle": _vector(value.rotation_axis_angle),
        "scale": _vector(value.scale),
    }


def _object_transform(obj: bpy.types.Object) -> dict[str, Any]:
    result = _transform(obj)
    result.update({
        "delta_location": _vector(obj.delta_location),
        "delta_rotation_euler": _vector(obj.delta_rotation_euler),
        "delta_rotation_quaternion": _vector(obj.delta_rotation_quaternion),
        "delta_scale": _vector(obj.delta_scale),
    })
    return result


def _assign_vector(target: object, attribute: str, values: object) -> None:
    if not isinstance(values, list):
        return
    try:
        current = getattr(target, attribute)
        if len(current) != len(values):
            return
        setattr(target, attribute, tuple(_finite(item) for item in values))
    except (AttributeError, TypeError, ValueError):
        return


def _apply_transform(target: object, values: dict[str, Any], *, obj=False) -> None:
    mode = str(values.get("rotation_mode", getattr(target, "rotation_mode", "XYZ")))
    try:
        target.rotation_mode = mode
    except (AttributeError, TypeError, ValueError):
        pass
    for attribute in (
        "location", "rotation_euler", "rotation_quaternion",
        "rotation_axis_angle", "scale",
    ):
        _assign_vector(target, attribute, values.get(attribute))
    if obj:
        for attribute in (
            "delta_location", "delta_rotation_euler",
            "delta_rotation_quaternion", "delta_scale",
        ):
            _assign_vector(target, attribute, values.get(attribute))


def _attrs(value: object, names: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for name in names:
        if not hasattr(value, name):
            continue
        try:
            result[name] = _json_value(getattr(value, name))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _apply_attrs(value: object, values: dict[str, Any]) -> None:
    for name, item in values.items():
        if not hasattr(value, name):
            continue
        try:
            setattr(value, name, item)
        except (AttributeError, TypeError, ValueError):
            continue


CAMERA_FIELDS = (
    "type", "lens", "sensor_fit", "sensor_width", "sensor_height",
    "shift_x", "shift_y", "clip_start", "clip_end", "ortho_scale",
    "dof.use_dof", "dof.focus_distance", "dof.aperture_fstop",
    "dof.aperture_blades", "dof.aperture_rotation", "dof.aperture_ratio",
)
LIGHT_FIELDS = (
    "type", "energy", "color", "use_shadow", "specular_factor",
    "volume_factor", "shape", "size", "size_y", "spot_size", "spot_blend",
    "use_custom_distance", "cutoff_distance", "use_temperature", "temperature",
)
SHADING_FIELDS = (
    "type", "light", "studio_light", "color_type", "single_color",
    "show_shadows", "show_cavity", "cavity_type", "curvature_ridge_factor",
    "curvature_valley_factor", "show_specular_highlight", "show_xray",
    "xray_alpha", "show_object_outline", "object_outline_color", "use_dof",
    "use_scene_lights", "use_scene_lights_render", "use_scene_world",
    "use_scene_world_render", "studiolight_rotate_z",
    "studiolight_background_alpha", "studiolight_background_blur",
    "studiolight_intensity", "use_studiolight_view_rotation",
)


def _nested_attrs(value: object, fields: Iterable[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for field in fields:
        owner = value
        parts = field.split(".")
        try:
            for part in parts[:-1]:
                owner = getattr(owner, part)
            result[field] = _json_value(getattr(owner, parts[-1]))
        except (AttributeError, TypeError, ValueError):
            continue
    return result


def _apply_nested_attrs(value: object, values: dict[str, Any]) -> None:
    for field, item in values.items():
        owner = value
        parts = field.split(".")
        try:
            for part in parts[:-1]:
                owner = getattr(owner, part)
            setattr(owner, parts[-1], item)
        except (AttributeError, TypeError, ValueError):
            continue


def find_view3d() -> tuple[object | None, object | None, object | None]:
    window, _area, space, region = viewport.find_view3d()
    return window, space, region


def _capture_layer_collections(
    layer: bpy.types.LayerCollection,
) -> list[dict[str, Any]]:
    result = [{
        "collection_uuid": ensure_uuid(layer.collection),
        "collection_name": layer.collection.name,
        "exclude": bool(layer.exclude),
        "hide_viewport": bool(layer.hide_viewport),
        "holdout": bool(layer.holdout),
        "indirect_only": bool(layer.indirect_only),
    }]
    for child in layer.children:
        result.extend(_capture_layer_collections(child))
    return result


def _all_id_blocks() -> Iterable[object]:
    for collection_name in (
        "scenes", "objects", "collections", "cameras", "lights", "materials",
        "worlds", "node_groups", "armatures",
    ):
        yield from getattr(bpy.data, collection_name, ())


def _id_by_uuid(identifier: str) -> object | None:
    for item in _all_id_blocks():
        if ensure_uuid(item) == identifier:
            return item
    return None


def _registered_values(scene: bpy.types.Scene) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in getattr(scene, "webtoon_comic_registered", ()):
        owner = _id_by_uuid(str(item.owner_uuid))
        if owner is None:
            continue
        try:
            pointer = owner.path_resolve(item.rna_path) if item.rna_path else owner
            value = _json_value(getattr(pointer, item.property_id))
        except (AttributeError, TypeError, ValueError):
            continue
        result.append({
            "owner_uuid": item.owner_uuid,
            "owner_type": item.owner_type,
            "owner_name": item.owner_name,
            "rna_path": item.rna_path,
            "property_id": item.property_id,
            "label": item.label,
            "value": value,
        })
    return result


def capture_state(
    scene: bpy.types.Scene, view_layer: bpy.types.ViewLayer | None = None,
    *, repair_ids: bool = True, stream_frame: object = None,
    output_resolution: object = None,
) -> tuple[dict[str, Any], list[str]]:
    """Capture only panel-variable state. Mesh/curve geometry is never read."""
    warnings = repair_duplicate_uuids(scene) if repair_ids else []
    view_layer = view_layer or bpy.context.view_layer
    objects: list[dict[str, Any]] = []
    poses: list[dict[str, Any]] = []
    shape_keys: list[dict[str, Any]] = []
    modifiers: list[dict[str, Any]] = []
    cameras: list[dict[str, Any]] = []
    lights: list[dict[str, Any]] = []
    fallback_warnings: set[str] = set()

    def note_linked(value: object, label: str) -> None:
        if _editable(value):
            return
        key = f"{label}:{getattr(value, 'name', '')}"
        if key in fallback_warnings:
            return
        fallback_warnings.add(key)
        warnings.append(
            f"{label} {getattr(value, 'name', '')} uses a name/library "
            "fallback identity because linked data is read-only"
        )

    for obj in scene.objects:
        note_linked(obj, "Object")
        object_uuid = ensure_uuid(obj)
        objects.append({
            "uuid": object_uuid,
            "name": obj.name,
            "type": obj.type,
            "transform": _object_transform(obj),
            "hide_viewport": bool(obj.hide_viewport),
            "hide_render": bool(obj.hide_render),
            "hidden_in_view_layer": bool(obj.hide_get(view_layer=view_layer)),
            "custom_properties": _custom_properties(obj),
        })
        if obj.pose is not None:
            for bone in obj.pose.bones:
                poses.append({
                    "object_uuid": object_uuid,
                    "bone_uuid": ensure_uuid(bone.bone),
                    "bone_name": bone.name,
                    "transform": _transform(bone),
                    "custom_properties": _custom_properties(bone),
                })
        keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        if keys is not None:
            for key in keys.key_blocks:
                shape_keys.append({
                    "object_uuid": object_uuid,
                    "name": key.name,
                    "value": _finite(key.value),
                    "mute": bool(key.mute),
                })
        for modifier in obj.modifiers:
            modifiers.append({
                "object_uuid": object_uuid,
                "uuid": ensure_uuid(modifier),
                "name": modifier.name,
                "type": modifier.type,
                "state": _attrs(modifier, (
                    "show_viewport", "show_render", "show_in_editmode",
                    "show_on_cage", "show_expanded",
                )),
            })
        if obj.type == "CAMERA" and obj.data is not None:
            note_linked(obj.data, "Camera data")
            cameras.append({
                "object_uuid": object_uuid,
                "data_uuid": ensure_uuid(obj.data),
                "data_name": obj.data.name,
                "state": _nested_attrs(obj.data, CAMERA_FIELDS),
            })
        if obj.type == "LIGHT" and obj.data is not None:
            note_linked(obj.data, "Light data")
            lights.append({
                "object_uuid": object_uuid,
                "data_uuid": ensure_uuid(obj.data),
                "data_name": obj.data.name,
                "state": _nested_attrs(obj.data, LIGHT_FIELDS),
            })

    collections = []
    for collection in _scene_collections(scene):
        note_linked(collection, "Collection")
        collections.append({
            "uuid": ensure_uuid(collection),
            "name": collection.name,
            "hide_viewport": bool(collection.hide_viewport),
            "hide_render": bool(collection.hide_render),
        })
    _window, space, _region = find_view3d()
    shading = _attrs(space.shading, SHADING_FIELDS) if space is not None else {}
    local_enabled = bool(
        space is not None and getattr(space, "local_view", None) is not None
    )
    local_members = [
        ensure_uuid(obj) for obj in scene.objects
        if local_enabled and obj.visible_get(view_layer=view_layer, viewport=space)
    ]
    active_collection = getattr(view_layer, "active_layer_collection", None)
    render = scene.render
    state = {
        "version": STATE_VERSION,
        "scene_uuid": ensure_uuid(scene),
        "scene_name": scene.name,
        "view_layer_name": view_layer.name,
        "active_camera_uuid": ensure_uuid(scene.camera) if scene.camera else "",
        "objects": objects,
        "poses": poses,
        "shape_keys": shape_keys,
        "collections": collections,
        "layer_collections": _capture_layer_collections(
            view_layer.layer_collection
        ),
        "active_layer_collection_uuid": (
            ensure_uuid(active_collection.collection)
            if active_collection is not None else ""
        ),
        "local_view": {
            "enabled": local_enabled,
            "object_uuids": local_members,
        },
        "modifiers": modifiers,
        "cameras": cameras,
        "lights": lights,
        "registered": _registered_values(scene),
        "viewport_shading": shading,
        "render_settings": {
            "resolution_x": int(render.resolution_x),
            "resolution_y": int(render.resolution_y),
            "pixel_aspect_x": _finite(render.pixel_aspect_x, 1.0),
            "pixel_aspect_y": _finite(render.pixel_aspect_y, 1.0),
        },
        "stream_frame_space": "camera",
        "stream_frame": list(
            stream_frame if stream_frame is not None else viewport.DEFAULT_FRAME
        ),
        "output_resolution": list(
            output_resolution if output_resolution is not None else (1920, 1080)
        ),
    }
    return _validate_presentation(state), warnings


def state_json(state: dict[str, Any]) -> str:
    return json.dumps(state, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def state_digest(state: dict[str, Any]) -> str:
    return hashlib.sha256(state_json(state).encode("utf-8")).hexdigest()


def _validate_presentation(state: dict[str, Any]) -> dict[str, Any]:
    result = dict(state)
    try:
        frame = [float(item) for item in result["stream_frame"]]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Comic View stream frame must contain four numbers")
    if len(frame) != 4 or not all(math.isfinite(item) for item in frame):
        raise ValueError("Comic View stream frame must contain four finite numbers")
    left, right = sorted((frame[0], frame[2]))
    bottom, top = sorted((frame[1], frame[3]))
    if right - left <= 1e-6 or top - bottom <= 1e-6:
        raise ValueError("Comic View stream frame is empty")
    result["stream_frame"] = [left, bottom, right, top]
    try:
        resolution = [int(item) for item in result["output_resolution"]]
    except (KeyError, TypeError, ValueError):
        raise ValueError("Comic View output resolution must contain two integers")
    if (
        len(resolution) != 2
        or not all(64 <= item <= 4096 for item in resolution)
        or resolution[0] * resolution[1] > 16_777_216
    ):
        raise ValueError("Comic View output resolution is invalid")
    result["output_resolution"] = resolution
    frame_space = str(result.get("stream_frame_space", "camera"))
    if frame_space not in {"camera", "viewport_legacy"}:
        raise ValueError("Comic View stream frame space is invalid")
    result["stream_frame_space"] = frame_space
    local_view = result.get("local_view", {})
    if not isinstance(local_view, dict):
        raise ValueError("Comic View Local View state must be an object")
    members = local_view.get("object_uuids", [])
    if not isinstance(members, list):
        raise ValueError("Comic View Local View members must be a list")
    result["local_view"] = {
        "enabled": bool(local_view.get("enabled", False)),
        "object_uuids": [str(item) for item in members if str(item)],
    }
    render_settings = result.get("render_settings", {})
    if not isinstance(render_settings, dict):
        raise ValueError("Comic View render settings must be an object")
    normalized_render = {}
    for name, fallback in (("resolution_x", 1920), ("resolution_y", 1080)):
        try:
            normalized_render[name] = max(
                1, min(65536, int(render_settings.get(name, fallback)))
            )
        except (TypeError, ValueError):
            normalized_render[name] = fallback
    for name in ("pixel_aspect_x", "pixel_aspect_y"):
        number = _finite(render_settings.get(name, 1.0), 1.0)
        normalized_render[name] = max(0.01, min(100.0, number))
    result["render_settings"] = normalized_render
    if "viewport" in result:
        raw_viewport = result["viewport"]
        if not isinstance(raw_viewport, dict):
            raise ValueError("Comic View viewport state must be an object")
        values = dict(raw_viewport)
        perspective = values.get("view_perspective")
        if (
            perspective is not None
            and perspective not in {"PERSP", "ORTHO", "CAMERA"}
        ):
            raise ValueError("Comic View viewport projection is invalid")
        for name, length in (
            ("view_location", 3), ("view_rotation", 4),
            ("view_camera_offset", 2),
        ):
            if name not in values:
                continue
            try:
                sequence = [float(item) for item in values[name]]
            except (TypeError, ValueError):
                raise ValueError(f"Comic View {name} is invalid")
            if (
                len(sequence) != length
                or not all(math.isfinite(item) for item in sequence)
            ):
                raise ValueError(f"Comic View {name} is invalid")
            if name == "view_rotation":
                magnitude = math.sqrt(sum(item * item for item in sequence))
                sequence = (
                    [1.0, 0.0, 0.0, 0.0]
                    if magnitude <= 1e-12
                    else [item / magnitude for item in sequence]
                )
            values[name] = sequence
        for name, minimum, maximum in (
            ("view_distance", 1e-6, 1e12),
            ("view_camera_zoom", -30.0, 600.0),
            ("lens", 1.0, 250.0),
        ):
            if name in values:
                number = float(values[name])
                if not math.isfinite(number):
                    raise ValueError(f"Comic View {name} is invalid")
                values[name] = max(minimum, min(maximum, number))
        result["viewport"] = values
    return result


def parse_state(
    raw: str, *, fallback_stream_frame: object = None,
    fallback_resolution: object = None,
) -> dict[str, Any]:
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("Comic View state must be an object")
    version = int(value.get("version", 0))
    if version in {1, 2}:
        value = dict(value)
        value["version"] = STATE_VERSION
        value["stream_frame_space"] = "viewport_legacy"
        value.setdefault("stream_frame", list(
            fallback_stream_frame
            if fallback_stream_frame is not None else viewport.DEFAULT_FRAME
        ))
        value.setdefault("output_resolution", list(
            fallback_resolution
            if fallback_resolution is not None else (1920, 1080)
        ))
        value.setdefault("local_view", {
            "enabled": False, "object_uuids": [],
        })
        value.setdefault("active_layer_collection_uuid", "")
        value.setdefault("render_settings", {
            "resolution_x": 1920, "resolution_y": 1080,
            "pixel_aspect_x": 1.0, "pixel_aspect_y": 1.0,
        })
        return _validate_presentation(value)
    if version != STATE_VERSION:
        raise ValueError(f"Unsupported Comic View state version: {version}")
    return _validate_presentation(value)


def migrate_legacy_presentation(
    scene: bpy.types.Scene, value: dict[str, Any],
) -> tuple[dict[str, Any], str | None]:
    """Map a legacy viewport-relative frame into attached-camera space."""
    if value.get("stream_frame_space") != "viewport_legacy":
        return value, None
    result = dict(value)
    navigation = viewport.capture_viewport()
    original_camera = scene.camera
    wanted_camera_uuid = str(value.get("active_camera_uuid", ""))
    if wanted_camera_uuid:
        saved_camera = next(
            (
                obj for obj in scene.objects
                if obj.type == "CAMERA" and ensure_uuid(obj) == wanted_camera_uuid
            ),
            None,
        )
        if saved_camera is not None:
            scene.camera = saved_camera
    warning = None
    try:
        legacy_navigation = value.get("viewport", {})
        if isinstance(legacy_navigation, dict):
            viewport.apply_viewport(legacy_navigation)
        _window, _area, _space, region = viewport.find_view3d()
        old = value.get("stream_frame", viewport.DEFAULT_FRAME)
        converted = None
        if region is not None:
            converted = viewport.screen_to_camera_bounds(scene, (
                float(old[0]) * region.width,
                float(old[1]) * region.height,
                float(old[2]) * region.width,
                float(old[3]) * region.height,
            ))
        if converted is None:
            converted = viewport.DEFAULT_FRAME
            warning = (
                "Legacy Stream Frame could not be mapped; used the full "
                "camera gate"
            )
        result["stream_frame"] = list(converted)
    except (IndexError, TypeError, ValueError):
        result["stream_frame"] = list(viewport.DEFAULT_FRAME)
        warning = (
            "Legacy Stream Frame was invalid; used the full camera gate"
        )
    finally:
        viewport.apply_viewport(navigation)
        scene.camera = original_camera
    render = scene.render
    result["render_settings"] = {
        "resolution_x": int(render.resolution_x),
        "resolution_y": int(render.resolution_y),
        "pixel_aspect_x": _finite(render.pixel_aspect_x, 1.0),
        "pixel_aspect_y": _finite(render.pixel_aspect_y, 1.0),
    }
    result["stream_frame_space"] = "camera"
    result.pop("viewport", None)
    return _validate_presentation(result), warning


def _object_lookup(scene: bpy.types.Scene) -> dict[str, bpy.types.Object]:
    return {ensure_uuid(obj): obj for obj in scene.objects}


def _collection_lookup(scene: bpy.types.Scene) -> dict[str, bpy.types.Collection]:
    return {ensure_uuid(item): item for item in _scene_collections(scene)}


def _layer_collection_lookup(
    root: bpy.types.LayerCollection,
) -> dict[str, bpy.types.LayerCollection]:
    result: dict[str, bpy.types.LayerCollection] = {}

    def walk(item: bpy.types.LayerCollection) -> None:
        result[ensure_uuid(item.collection)] = item
        for child in item.children:
            walk(child)

    walk(root)
    return result


def view_layer_for_state(
    scene: bpy.types.Scene, state: dict[str, Any],
    fallback: bpy.types.ViewLayer | None = None,
) -> bpy.types.ViewLayer:
    """Resolve the captured view layer without depending on UI context."""
    name = str(state.get("view_layer_name", ""))
    selected = scene.view_layers.get(name) if name else None
    if selected is not None:
        return selected
    if fallback is not None:
        return fallback
    current = getattr(bpy.context, "view_layer", None)
    return current if current in scene.view_layers.values() else scene.view_layers[0]


def apply_state(
    scene: bpy.types.Scene, state: dict[str, Any],
    view_layer: bpy.types.ViewLayer | None = None,
) -> list[str]:
    """Apply one snapshot while leaving scene geometry untouched."""
    if int(state.get("version", 0)) not in {1, STATE_VERSION}:
        raise ValueError("Unsupported Comic View state")
    warnings: list[str] = []
    requested_layer = str(state.get("view_layer_name", ""))
    view_layer = view_layer_for_state(scene, state, view_layer)
    if requested_layer and view_layer.name != requested_layer:
        warnings.append(f"Missing view layer {requested_layer}")
    objects = _object_lookup(scene)
    stored_objects = {
        str(item.get("uuid", "")): item for item in state.get("objects", [])
    }
    for identifier, obj in objects.items():
        if identifier in stored_objects:
            continue
        try:
            obj.hide_viewport = True
            obj.hide_render = True
            obj.hide_set(True, view_layer=view_layer)
        except (AttributeError, RuntimeError, TypeError):
            pass
        warnings.append(f"Hid uncaptured object {obj.name}")
    for identifier, item in stored_objects.items():
        obj = objects.get(identifier)
        if obj is None:
            warnings.append(f"Missing object {item.get('name', identifier)}")
            continue
        try:
            obj.hide_viewport = bool(item.get("hide_viewport", False))
            obj.hide_render = bool(item.get("hide_render", False))
            obj.hide_set(
                bool(item.get("hidden_in_view_layer", False)),
                view_layer=view_layer,
            )
        except (AttributeError, RuntimeError, TypeError):
            pass
    collections = _collection_lookup(scene)
    stored_collections = {
        str(item.get("uuid", "")): item
        for item in state.get("collections", [])
    }
    for identifier, collection in collections.items():
        if identifier not in stored_collections and collection != scene.collection:
            collection.hide_viewport = True
            collection.hide_render = True
            warnings.append(f"Hid uncaptured collection {collection.name}")
    for identifier, item in stored_collections.items():
        collection = collections.get(identifier)
        if collection is None:
            warnings.append(f"Missing collection {item.get('name', identifier)}")
            continue
        collection.hide_viewport = bool(item.get("hide_viewport", False))
        collection.hide_render = bool(item.get("hide_render", False))

    layer_collections = _layer_collection_lookup(view_layer.layer_collection)
    for item in state.get("layer_collections", []):
        layer = layer_collections.get(str(item.get("collection_uuid", "")))
        if layer is None:
            continue
        for name in ("exclude", "hide_viewport", "holdout", "indirect_only"):
            try:
                setattr(layer, name, bool(item.get(name, False)))
            except (AttributeError, RuntimeError):
                continue
    active_collection_uuid = str(
        state.get("active_layer_collection_uuid", "")
    )
    active_collection = layer_collections.get(active_collection_uuid)
    if active_collection is not None:
        try:
            view_layer.active_layer_collection = active_collection
        except (AttributeError, RuntimeError, TypeError):
            warnings.append("Could not restore the active collection")

    # All visibility and collection membership switches are complete before
    # transforms, controls, modifiers, or data parameters are restored.
    for identifier, item in stored_objects.items():
        obj = objects.get(identifier)
        if obj is None:
            continue
        _apply_transform(obj, item.get("transform", {}), obj=True)
        _set_custom_properties(obj, item.get("custom_properties", {}))
        stored_custom = set(item.get("custom_properties", {}))
        for key in _custom_properties(obj):
            if key not in stored_custom:
                warnings.append(
                    f"New control {obj.name}[{key}] was left unchanged; "
                    "save this view to capture it"
                )

    pose_lookup: dict[tuple[str, str], object] = {}
    for object_uuid, obj in objects.items():
        if obj.pose is not None:
            for bone in obj.pose.bones:
                pose_lookup[(object_uuid, ensure_uuid(bone.bone))] = bone
    stored_pose_keys: set[tuple[str, str]] = set()
    for item in state.get("poses", []):
        key = (str(item.get("object_uuid", "")), str(item.get("bone_uuid", "")))
        stored_pose_keys.add(key)
        bone = pose_lookup.get(key)
        if bone is None:
            warnings.append(f"Missing pose bone {item.get('bone_name', key[1])}")
            continue
        _apply_transform(bone, item.get("transform", {}))
        _set_custom_properties(bone, item.get("custom_properties", {}))
    for key, bone in pose_lookup.items():
        if key[0] in stored_objects and key not in stored_pose_keys:
            warnings.append(
                f"New pose control {bone.name} was left unchanged; save this view"
            )

    stored_shape_keys: set[tuple[str, str]] = set()
    for item in state.get("shape_keys", []):
        stored_shape_keys.add((
            str(item.get("object_uuid", "")), str(item.get("name", ""))
        ))
        obj = objects.get(str(item.get("object_uuid", "")))
        keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        key = keys.key_blocks.get(str(item.get("name", ""))) if keys else None
        if key is None:
            warnings.append(f"Missing shape key {item.get('name', '')}")
            continue
        key.value = _finite(item.get("value", 0.0))
        key.mute = bool(item.get("mute", False))
    for object_uuid, obj in objects.items():
        if object_uuid not in stored_objects:
            continue
        keys = getattr(getattr(obj, "data", None), "shape_keys", None)
        for key in keys.key_blocks if keys is not None else ():
            if (object_uuid, key.name) not in stored_shape_keys:
                warnings.append(
                    f"New shape key {key.name} was left unchanged; save this view"
                )

    stored_modifiers: set[tuple[str, str]] = set()
    for item in state.get("modifiers", []):
        obj = objects.get(str(item.get("object_uuid", "")))
        modifier = None
        if obj is not None:
            wanted = str(item.get("uuid", ""))
            stored_modifiers.add((str(item.get("object_uuid", "")), wanted))
            modifier = next(
                (candidate for candidate in obj.modifiers if ensure_uuid(candidate) == wanted),
                None,
            )
        if modifier is None:
            warnings.append(f"Missing modifier {item.get('name', '')}")
            continue
        _apply_attrs(modifier, item.get("state", {}))
    for object_uuid, obj in objects.items():
        if object_uuid not in stored_objects:
            continue
        for modifier in obj.modifiers:
            if (object_uuid, ensure_uuid(modifier)) not in stored_modifiers:
                warnings.append(
                    f"New modifier {modifier.name} was left unchanged; save this view"
                )

    for group, expected_type in (("cameras", "CAMERA"), ("lights", "LIGHT")):
        for item in state.get(group, []):
            obj = objects.get(str(item.get("object_uuid", "")))
            if obj is None or obj.type != expected_type or obj.data is None:
                warnings.append(f"Missing {group[:-1]} {item.get('data_name', '')}")
                continue
            _apply_nested_attrs(obj.data, item.get("state", {}))

    for item in state.get("registered", []):
        owner = _id_by_uuid(str(item.get("owner_uuid", "")))
        if owner is None:
            warnings.append(f"Missing registered target {item.get('label', '')}")
            continue
        try:
            path = str(item.get("rna_path", ""))
            pointer = owner.path_resolve(path) if path else owner
            setattr(pointer, str(item.get("property_id", "")), item.get("value"))
        except (AttributeError, TypeError, ValueError):
            warnings.append(f"Could not apply registered property {item.get('label', '')}")

    camera_uuid = str(state.get("active_camera_uuid", ""))
    if camera_uuid:
        camera = objects.get(camera_uuid)
        if camera is not None and camera.type == "CAMERA":
            scene.camera = camera
        else:
            warnings.append("The active camera is missing")
    render_settings = state.get("render_settings", {})
    for name in (
        "resolution_x", "resolution_y", "pixel_aspect_x", "pixel_aspect_y",
    ):
        if name not in render_settings:
            continue
        try:
            setattr(scene.render, name, render_settings[name])
        except (AttributeError, TypeError, ValueError):
            warnings.append(f"Could not restore render setting {name}")
    _window, space, _region = find_view3d()
    if space is not None:
        _apply_attrs(space.shading, state.get("viewport_shading", {}))
    window = getattr(bpy.context, "window", None)
    if window is not None and window.scene == scene and window.view_layer != view_layer:
        try:
            window.view_layer = view_layer
        except (AttributeError, RuntimeError, TypeError):
            warnings.append(f"Could not activate view layer {view_layer.name}")
    local = state.get("local_view", {})
    warning = viewport.apply_local_view(
        objects,
        bool(local.get("enabled", False)),
        {str(item) for item in local.get("object_uuids", [])},
    )
    if warning:
        warnings.append(warning)
    viewport.enter_camera_view()
    view_layer.update()
    return warnings
