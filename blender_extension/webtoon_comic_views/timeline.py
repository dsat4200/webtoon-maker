"""Automatic timeline baking for persistent Comic View snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

import bpy

from . import diagnostics
from .state import ensure_uuid, parse_state, state_digest


TRANSFORM_FIELDS = (
    "location", "rotation_mode", "rotation_euler", "rotation_quaternion",
    "rotation_axis_angle", "scale",
)
OBJECT_DELTA_FIELDS = (
    "delta_location", "delta_rotation_euler",
    "delta_rotation_quaternion", "delta_scale",
)
FRAME_EPSILON = 1.0e-4
VALUE_EPSILON = 1.0e-5
UUID_PROPERTY = "webtoon_comic_uuid"
BAKE_VERSION = 1


def _escape(value: object) -> str:
    return bpy.utils.escape_identifier(str(value))


def _owner_key(owner: object) -> tuple[int, str]:
    pointer = int(getattr(owner, "as_pointer", lambda: 0)())
    return pointer, ensure_uuid(owner)


def _enum_number(owner: object, data_path: str, value: str) -> float | None:
    prefix, separator, identifier = data_path.rpartition(".")
    if not separator or identifier.startswith("["):
        target, identifier = owner, data_path
    else:
        try:
            target = owner.path_resolve(prefix)
        except (AttributeError, ValueError):
            return None
    prop = getattr(getattr(target, "bl_rna", None), "properties", {}).get(
        identifier
    )
    if prop is None or getattr(prop, "type", "") != "ENUM":
        return None
    item = prop.enum_items.get(value)
    return float(item.value) if item is not None else None


def _channel_is_animatable(owner: object, data_path: str) -> bool:
    """Return Blender's RNA capability for a channel.

    Numeric custom properties use bracket paths and are animatable even though
    they do not have a conventional RNA property descriptor.
    """
    if data_path.rstrip().endswith("]"):
        return True
    prefix, separator, identifier = data_path.rpartition(".")
    target = owner
    if separator:
        try:
            target = owner.path_resolve(prefix)
        except (AttributeError, ValueError):
            return True
    prop = getattr(getattr(target, "bl_rna", None), "properties", {}).get(
        identifier if separator else data_path
    )
    return True if prop is None else bool(getattr(prop, "is_animatable", False))


def _components(
    owner: object, data_path: str, value: object,
) -> list[tuple[int, float]]:
    if isinstance(value, bool):
        return [(0, float(value))]
    if isinstance(value, (int, float)) and math.isfinite(float(value)):
        return [(0, float(value))]
    if isinstance(value, str):
        number = _enum_number(owner, data_path, value)
        return [] if number is None else [(0, number)]
    if isinstance(value, list):
        result = []
        for index, item in enumerate(value):
            if isinstance(item, bool):
                result.append((index, float(item)))
            elif isinstance(item, (int, float)) and math.isfinite(float(item)):
                result.append((index, float(item)))
        return result
    return []


@dataclass(frozen=True)
class ChannelKey:
    owner_pointer: int
    owner_uuid: str
    data_path: str
    array_index: int


@dataclass
class SnapshotChannel:
    key: ChannelKey
    owner: object
    value: float
    label: str
    discrete: bool = False
    strict_driver: bool = False
    animatable: bool = True


def _add_channel(
    result: dict[ChannelKey, SnapshotChannel], owner: object | None,
    data_path: str, value: object, label: str, *, strict_driver: bool = False,
) -> None:
    if owner is None:
        return
    pointer, identifier = _owner_key(owner)
    for index, number in _components(owner, data_path, value):
        key = ChannelKey(pointer, identifier, data_path, index)
        result[key] = SnapshotChannel(
            key=key,
            owner=owner,
            value=number,
            label=f"{label}[{index}]" if isinstance(value, list) else label,
            discrete=isinstance(value, (bool, str)),
            strict_driver=strict_driver,
            animatable=_channel_is_animatable(owner, data_path),
        )


def _object_lookup(scene: bpy.types.Scene) -> dict[str, bpy.types.Object]:
    return {ensure_uuid(obj): obj for obj in scene.objects}


def _collection_lookup(scene: bpy.types.Scene) -> dict[str, object]:
    result: dict[str, object] = {}

    def walk(collection: object) -> None:
        result.setdefault(ensure_uuid(collection), collection)
        for child in collection.children:
            walk(child)

    walk(scene.collection)
    return result


def snapshot_channels(
    scene: bpy.types.Scene, snapshot: dict[str, Any],
) -> dict[ChannelKey, SnapshotChannel]:
    """Flatten one saved snapshot into writable, numeric RNA channels."""
    result: dict[ChannelKey, SnapshotChannel] = {}
    objects = _object_lookup(scene)
    for record in snapshot.get("objects", []):
        identifier = str(record.get("uuid", ""))
        obj = objects.get(identifier)
        if obj is None:
            continue
        transform = record.get("transform", {})
        for field in (*TRANSFORM_FIELDS, *OBJECT_DELTA_FIELDS):
            _add_channel(
                result, obj, field, transform.get(field),
                f"{obj.name}.{field}",
                strict_driver=obj.type == "CAMERA",
            )
        for field in ("hide_viewport", "hide_render"):
            _add_channel(
                result, obj, field, record.get(field), f"{obj.name}.{field}"
            )
        for name, value in record.get("custom_properties", {}).items():
            path = f'["{_escape(name)}"]'
            _add_channel(result, obj, path, value, f"{obj.name}[{name}]")

    for record in snapshot.get("poses", []):
        obj = objects.get(str(record.get("object_uuid", "")))
        bone_name = str(record.get("bone_name", ""))
        bone = None
        if obj is not None and obj.pose:
            wanted = str(record.get("bone_uuid", ""))
            bone = next(
                (
                    candidate for candidate in obj.pose.bones
                    if ensure_uuid(candidate.bone) == wanted
                ),
                None,
            )
            bone = bone or obj.pose.bones.get(bone_name)
        if bone is None:
            continue
        bone_name = bone.name
        prefix = f'pose.bones["{_escape(bone_name)}"]'
        transform = record.get("transform", {})
        for field in TRANSFORM_FIELDS:
            _add_channel(
                result, obj, f"{prefix}.{field}", transform.get(field),
                f"{obj.name}.{bone_name}.{field}",
            )
        for name, value in record.get("custom_properties", {}).items():
            path = f'{prefix}["{_escape(name)}"]'
            _add_channel(
                result, obj, path, value, f"{obj.name}.{bone_name}[{name}]"
            )

    for group in ("cameras", "lights"):
        for record in snapshot.get(group, []):
            obj = objects.get(str(record.get("object_uuid", "")))
            owner = getattr(obj, "data", None)
            if owner is None:
                continue
            for path, value in record.get("state", {}).items():
                _add_channel(
                    result, owner, str(path), value,
                    f"{getattr(owner, 'name', group)}.{path}",
                    strict_driver=True,
                )

    for record in snapshot.get("shape_keys", []):
        obj = objects.get(str(record.get("object_uuid", "")))
        owner = getattr(getattr(obj, "data", None), "shape_keys", None)
        name = str(record.get("name", ""))
        if owner is None or owner.key_blocks.get(name) is None:
            continue
        prefix = f'key_blocks["{_escape(name)}"]'
        for field in ("value", "mute"):
            _add_channel(
                result, owner, f"{prefix}.{field}", record.get(field),
                f"{obj.name}.{name}.{field}",
            )

    for record in snapshot.get("modifiers", []):
        obj = objects.get(str(record.get("object_uuid", "")))
        name = str(record.get("name", ""))
        modifier = None
        if obj is not None:
            wanted = str(record.get("uuid", ""))
            modifier = next(
                (
                    candidate for candidate in obj.modifiers
                    if ensure_uuid(candidate) == wanted
                ),
                None,
            )
            modifier = modifier or obj.modifiers.get(name)
        if modifier is None:
            continue
        name = modifier.name
        prefix = f'modifiers["{_escape(name)}"]'
        for field, value in record.get("state", {}).items():
            _add_channel(
                result, obj, f"{prefix}.{field}", value,
                f"{obj.name}.{name}.{field}",
            )

    collections = _collection_lookup(scene)
    for record in snapshot.get("collections", []):
        owner = collections.get(str(record.get("uuid", "")))
        if owner is None:
            continue
        for field in ("hide_viewport", "hide_render"):
            _add_channel(
                result, owner, field, record.get(field),
                f"{owner.name}.{field}",
            )

    registered = snapshot.get("registered", [])
    wanted_ids = {str(record.get("owner_uuid", "")) for record in registered}
    all_ids = {}
    if wanted_ids:
        for collection_name in (
            "scenes", "objects", "collections", "cameras", "lights",
            "materials", "worlds", "node_groups", "armatures", "shape_keys",
        ):
            for owner in getattr(bpy.data, collection_name, ()):
                identifier = str(owner.get(UUID_PROPERTY, ""))
                if identifier in wanted_ids:
                    all_ids.setdefault(identifier, owner)
    for record in registered:
        owner = all_ids.get(str(record.get("owner_uuid", "")))
        if owner is None:
            continue
        prefix = str(record.get("rna_path", ""))
        prop = str(record.get("property_id", ""))
        path = f"{prefix}.{prop}" if prefix else prop
        _add_channel(
            result, owner, path, record.get("value"),
            str(record.get("label", path)),
            strict_driver=True,
        )
    return result


def _action_curve(owner: object, key: ChannelKey) -> object | None:
    animation = getattr(owner, "animation_data", None)
    action = getattr(animation, "action", None)
    if action is None:
        return None
    return action.fcurves.find(key.data_path, index=key.array_index)


def _values_differ(values: Iterable[float]) -> bool:
    values = list(values)
    return bool(values) and any(
        abs(value - values[0]) > VALUE_EPSILON for value in values[1:]
    )


def _max_animation_frame() -> int:
    maximum = 0.0
    for action in bpy.data.actions:
        for curve in action.fcurves:
            for point in curve.keyframe_points:
                maximum = max(maximum, float(point.co.x))
    for collection_name in (
        "scenes", "objects", "collections", "cameras", "lights",
        "materials", "worlds", "node_groups", "armatures", "shape_keys",
    ):
        for owner in getattr(bpy.data, collection_name, ()):
            animation = getattr(owner, "animation_data", None)
            for track in getattr(animation, "nla_tracks", ()) if animation else ():
                for strip in track.strips:
                    maximum = max(maximum, float(strip.frame_end))
    return int(math.ceil(maximum))


@dataclass
class _PointMutation:
    curve: object
    frame: float
    created: bool
    old_value: float = 0.0
    old_interpolation: str = "BEZIER"


@dataclass
class _ActionMutation:
    owner: object
    original: object | None
    replacement: object


@dataclass
class _PointStyle:
    curve: object
    frame: float
    interpolation: str
    easing: str
    handle_left_type: str
    handle_right_type: str
    handle_left: tuple[float, float]
    handle_right: tuple[float, float]


@dataclass
class BakeTransaction:
    scene: bpy.types.Scene
    frame_end: int
    next_frame_cursor: int = 0
    frame_assignments: list[tuple[object, int]] = field(default_factory=list)
    point_mutations: list[_PointMutation] = field(default_factory=list)
    new_curves: list[tuple[object, object]] = field(default_factory=list)
    action_mutations: list[_ActionMutation] = field(default_factory=list)
    bake_assignments: list[tuple[object, str, str]] = field(default_factory=list)
    preserved_styles: list[_PointStyle] = field(default_factory=list)
    target_frame: int = 0
    keyed_channels: int = 0
    migrated_views: int = 0
    skipped_driver_channels: int = 0
    apply_only_channels: int = 0
    cached: bool = False
    _recorded_points: set[tuple[int, int]] = field(default_factory=set)
    _preserved_curves: set[int] = field(default_factory=set)
    _finished: bool = False

    def commit(self) -> None:
        changed: list[tuple[object, str]] = []
        try:
            for view, old_hash, new_hash in self.bake_assignments:
                view.bake_hash = new_hash
                changed.append((view, old_hash))
        except Exception:
            for view, old_hash in changed:
                try:
                    view.bake_hash = old_hash
                except (AttributeError, ReferenceError, RuntimeError, TypeError):
                    pass
            raise
        self._finished = True

    def update_bake_marker(self, view: object, snapshot: dict[str, Any]) -> None:
        marker = _bake_marker(snapshot)
        for index, (candidate, old_hash, _new_hash) in enumerate(
            self.bake_assignments
        ):
            if candidate == view:
                self.bake_assignments[index] = (candidate, old_hash, marker)
                return

    def rollback(self) -> None:
        if self._finished:
            return
        changed_curves: dict[int, object] = {}
        for mutation in reversed(self.point_mutations):
            try:
                point = _point_at(mutation.curve, mutation.frame)
                if mutation.created:
                    if point is not None:
                        mutation.curve.keyframe_points.remove(point, fast=True)
                elif point is not None:
                    point.co.y = mutation.old_value
                    point.interpolation = mutation.old_interpolation
                changed_curves[int(mutation.curve.as_pointer())] = mutation.curve
            except (ReferenceError, RuntimeError, TypeError, ValueError):
                pass
        for curve in changed_curves.values():
            try:
                curve.update()
            except (ReferenceError, RuntimeError):
                pass
        _restore_point_styles(self)
        for action, curve in reversed(self.new_curves):
            try:
                action.fcurves.remove(curve)
            except (ReferenceError, RuntimeError):
                pass
        for mutation in reversed(self.action_mutations):
            animation = getattr(mutation.owner, "animation_data", None)
            if animation is not None:
                try:
                    animation.action = mutation.original
                except (AttributeError, RuntimeError, TypeError):
                    pass
            try:
                if mutation.replacement.users == 0:
                    bpy.data.actions.remove(mutation.replacement)
            except (ReferenceError, RuntimeError):
                pass
        for view, old_frame in self.frame_assignments:
            try:
                view.timeline_frame = old_frame
            except (AttributeError, ReferenceError):
                pass
        for view, old_hash, _new_hash in self.bake_assignments:
            try:
                view.bake_hash = old_hash
            except (AttributeError, ReferenceError, RuntimeError, TypeError):
                pass
        settings = getattr(self.scene, "webtoon_comic_settings", None)
        if settings is not None:
            settings.next_timeline_frame = self.next_frame_cursor
        self.scene.frame_end = self.frame_end
        self._finished = True


def _ensure_action(
    transaction: BakeTransaction, owner: object,
    cache: dict[int, object], channel_label: str,
) -> object:
    pointer = int(owner.as_pointer())
    if pointer in cache:
        return cache[pointer]
    if getattr(owner, "library", None) is not None:
        raise RuntimeError(
            f"{channel_label} belongs to linked data and cannot receive Comic View keys"
        )
    owner_name = getattr(owner, "name", type(owner).__name__)
    try:
        creator = getattr(owner, "animation_data_create")
        animation = creator()
    except (AttributeError, RuntimeError, TypeError) as error:
        raise RuntimeError(
            f"Cannot create animation data for {channel_label} on {owner_name}"
        ) from error
    if animation is None:
        raise RuntimeError(
            f"Blender did not provide animation data for animatable channel "
            f"{channel_label} on {owner_name}"
        )
    for track in getattr(animation, "nla_tracks", ()):
        if not track.mute and any(not strip.mute for strip in track.strips):
            raise RuntimeError(
                f"Active NLA evaluation conflicts with {channel_label} on "
                f"{owner.name}"
            )
    action = animation.action
    if action is not None and getattr(action, "library", None) is not None:
        raise RuntimeError(
            f"{channel_label} uses linked read-only Action {action.name}"
        )
    if action is None:
        action = bpy.data.actions.new(f"Webtoon Comic Views - {owner.name}")
        animation.action = action
        transaction.action_mutations.append(_ActionMutation(owner, None, action))
    elif action.users > 1:
        original = action
        action = action.copy()
        action.name = f"{original.name} - Webtoon Comic Views - {owner.name}"
        animation.action = action
        transaction.action_mutations.append(
            _ActionMutation(owner, original, action)
        )
    cache[pointer] = action
    return action


def _driver_conflict(owner: object, channel: SnapshotChannel) -> bool:
    animation = getattr(owner, "animation_data", None)
    for curve in getattr(animation, "drivers", ()) if animation is not None else ():
        if (
            curve.data_path == channel.key.data_path
            and int(curve.array_index) == channel.key.array_index
            and not curve.mute
        ):
            return True
    return False


def _point_at(curve: object, frame: int) -> object | None:
    return next(
        (
            point for point in curve.keyframe_points
            if abs(float(point.co.x) - frame) <= FRAME_EPSILON
        ),
        None,
    )


def _preserve_point_styles(
    transaction: BakeTransaction, curve: object, owned_frames: set[int],
) -> None:
    pointer = int(curve.as_pointer())
    if pointer in transaction._preserved_curves:
        return
    transaction._preserved_curves.add(pointer)
    for point in curve.keyframe_points:
        if any(
            abs(float(point.co.x) - frame) <= FRAME_EPSILON
            for frame in owned_frames
        ):
            continue
        transaction.preserved_styles.append(_PointStyle(
            curve=curve,
            frame=float(point.co.x),
            interpolation=str(point.interpolation),
            easing=str(point.easing),
            handle_left_type=str(point.handle_left_type),
            handle_right_type=str(point.handle_right_type),
            handle_left=tuple(float(value) for value in point.handle_left),
            handle_right=tuple(float(value) for value in point.handle_right),
        ))


def _restore_point_styles(
    transaction: BakeTransaction, curve: object | None = None,
) -> None:
    for style in transaction.preserved_styles:
        if (
            curve is not None
            and int(style.curve.as_pointer()) != int(curve.as_pointer())
        ):
            continue
        try:
            point = _point_at(style.curve, style.frame)
            if point is None:
                continue
            point.interpolation = style.interpolation
            point.easing = style.easing
            point.handle_left_type = style.handle_left_type
            point.handle_right_type = style.handle_right_type
            point.handle_left = style.handle_left
            point.handle_right = style.handle_right
        except (AttributeError, ReferenceError, RuntimeError, TypeError, ValueError):
            pass


def _write_point(
    transaction: BakeTransaction, curve: object,
    frame: int, value: float, *, discrete: bool,
) -> None:
    point = _point_at(curve, frame)
    mutation_key = (int(curve.as_pointer()), int(round(frame / FRAME_EPSILON)))
    recorded = mutation_key in transaction._recorded_points
    if point is None:
        point = curve.keyframe_points.insert(frame, value, options={"FAST"})
        if not recorded:
            transaction.point_mutations.append(_PointMutation(curve, frame, True))
    else:
        if not recorded:
            transaction.point_mutations.append(_PointMutation(
                curve, frame, False, float(point.co.y), str(point.interpolation),
            ))
        point.co.y = value
    transaction._recorded_points.add(mutation_key)
    point.interpolation = "CONSTANT" if discrete or frame > transaction.frame_end else str(
        point.interpolation
    )


def _ensure_curve(
    transaction: BakeTransaction, action: object, channel: SnapshotChannel,
) -> object:
    curve = action.fcurves.find(
        channel.key.data_path, index=channel.key.array_index
    )
    if curve is None:
        curve = action.fcurves.new(
            channel.key.data_path, index=channel.key.array_index,
            action_group="Webtoon Comic Views",
        )
        transaction.new_curves.append((action, curve))
    return curve


def _allocate_frames(
    scene: bpy.types.Scene, views: list[object], transaction: BakeTransaction,
) -> dict[str, int]:
    used: set[int] = set()
    result: dict[str, int] = {}
    existing_frames = {
        int(getattr(view, "timeline_frame", 0))
        for view in views if int(getattr(view, "timeline_frame", 0)) > 0
    }
    settings = getattr(scene, "webtoon_comic_settings", None)
    cursor = int(getattr(settings, "next_timeline_frame", 0))
    transaction.next_frame_cursor = cursor
    next_frame = max(
        int(scene.frame_end), _max_animation_frame(),
        max(existing_frames, default=0), cursor - 1,
    ) + 1
    for view in views:
        identifier = str(getattr(view, "view_uuid", ""))
        frame = int(getattr(view, "timeline_frame", 0))
        if frame <= 0 or frame in used:
            old_frame = frame
            while next_frame in used:
                next_frame += 1
            frame = next_frame
            next_frame += 1
            transaction.frame_assignments.append((view, old_frame))
            view.timeline_frame = frame
            transaction.migrated_views += 1
        used.add(frame)
        result[identifier] = frame
    if used:
        scene.frame_end = max(int(scene.frame_end), max(used))
        if settings is not None:
            settings.next_timeline_frame = max(
                int(settings.next_timeline_frame), max(used) + 1, next_frame
            )
    return result


def _bake_marker(snapshot: dict[str, Any]) -> str:
    return f"{BAKE_VERSION}:{state_digest(snapshot)}"


def _cached_bake(
    usable_views: list[object], target: object, candidate: dict[str, Any],
) -> bool:
    frames: set[int] = set()
    target_uuid = str(getattr(target, "view_uuid", ""))
    for view in usable_views:
        frame = int(getattr(view, "timeline_frame", 0))
        if frame <= 0 or frame in frames:
            return False
        frames.add(frame)
        snapshot = (
            candidate
            if str(getattr(view, "view_uuid", "")) == target_uuid
            else parse_state(view.state_json)
        )
        if str(getattr(view, "bake_hash", "")) != _bake_marker(snapshot):
            return False
    return True


def prepare_bake(
    scene: bpy.types.Scene, views: Iterable[object], target: object,
    candidate: dict[str, Any], *, force: bool = False,
) -> BakeTransaction:
    """Bake a candidate and backfill promoted channels transactionally."""
    target_uuid = str(getattr(target, "view_uuid", ""))
    usable_views = [
        view for view in views
        if (
            str(getattr(view, "state_json", ""))
            or str(getattr(view, "view_uuid", "")) == target_uuid
        )
    ]
    transaction = BakeTransaction(scene=scene, frame_end=int(scene.frame_end))
    try:
        if not force and _cached_bake(usable_views, target, candidate):
            transaction.target_frame = int(target.timeline_frame)
            transaction.cached = True
            scene.frame_end = max(
                int(scene.frame_end),
                *(int(view.timeline_frame) for view in usable_views),
            )
            return transaction
        frames = _allocate_frames(scene, usable_views, transaction)
        transaction.target_frame = frames[target_uuid]
        snapshots: list[tuple[object, dict[str, Any], dict[ChannelKey, SnapshotChannel]]] = []
        for view in usable_views:
            state = (
                candidate
                if str(getattr(view, "view_uuid", "")) == target_uuid
                else parse_state(view.state_json)
            )
            snapshots.append((view, state, snapshot_channels(scene, state)))
            transaction.bake_assignments.append((
                view, str(getattr(view, "bake_hash", "")), _bake_marker(state)
            ))

        all_keys: set[ChannelKey] = set()
        for _view, _state, channels in snapshots:
            all_keys.update(channels)
        needed: set[ChannelKey] = set()
        for key in all_keys:
            channels = [mapping[key] for _view, _state, mapping in snapshots if key in mapping]
            sample = channels[0]
            if not sample.animatable:
                transaction.apply_only_channels += 1
                continue
            if _values_differ(channel.value for channel in channels):
                needed.add(key)
                continue
            if _action_curve(sample.owner, key) is not None:
                needed.add(key)

        action_cache: dict[int, object] = {}
        boundary_curves: set[int] = set()
        owned_frames = set(frames.values())
        for key in sorted(
            needed,
            key=lambda item: (item.owner_uuid, item.data_path, item.array_index),
        ):
            sample = next(
                mapping[key] for _view, _state, mapping in snapshots if key in mapping
            )
            if _driver_conflict(sample.owner, sample):
                if sample.strict_driver:
                    raise RuntimeError(f"Driver conflicts with {sample.label}")
                # Rig deformation channels commonly contain driver-evaluated output.
                # Reproduce those values by baking their controller channels, then let
                # Blender evaluate the rig instead of fighting the driver directly.
                transaction.skipped_driver_channels += 1
                continue
            action = _ensure_action(
                transaction, sample.owner, action_cache, sample.label
            )
            curve = _ensure_curve(transaction, action, sample)
            _preserve_point_styles(transaction, curve, owned_frames)
            curve_pointer = int(curve.as_pointer())
            if (
                curve_pointer not in boundary_curves
                and _point_at(curve, transaction.frame_end) is None
                and (
                    not curve.keyframe_points
                    or max(float(point.co.x) for point in curve.keyframe_points)
                        < transaction.target_frame
                )
            ):
                boundary_value = (
                    float(curve.evaluate(transaction.frame_end))
                    if curve.keyframe_points else _read_channel(sample)
                )
                if boundary_value is None:
                    boundary_value = sample.value
                _write_point(
                    transaction, curve, transaction.frame_end,
                    boundary_value, discrete=True,
                )
                boundary_curves.add(curve_pointer)
            for view, _state, mapping in snapshots:
                channel = mapping.get(key)
                if channel is None:
                    continue
                _write_point(
                    transaction, curve, frames[str(view.view_uuid)],
                    channel.value, discrete=True,
                )
            curve.update()
            _restore_point_styles(transaction, curve)
            transaction.keyed_channels += 1
        diagnostics.record(
            "INFO", "Comic View timeline prepared",
            view=getattr(target, "name", ""),
            frame=transaction.target_frame,
            keyed_channels=transaction.keyed_channels,
            migrated_views=transaction.migrated_views,
            skipped_driver_channels=transaction.skipped_driver_channels,
            apply_only_channels=transaction.apply_only_channels,
        )
        return transaction
    except Exception:
        transaction.rollback()
        raise


def _read_channel(channel: SnapshotChannel) -> float | None:
    try:
        value = channel.owner.path_resolve(channel.key.data_path)
    except (AttributeError, ValueError):
        return None
    if hasattr(value, "__len__") and not isinstance(value, (str, bytes)):
        try:
            value = value[channel.key.array_index]
        except (IndexError, TypeError):
            return None
    components = _components(channel.owner, channel.key.data_path, value)
    if not components:
        return None
    if len(components) == 1:
        return components[0][1]
    for index, number in components:
        if index == channel.key.array_index:
            return number
    return None


def verify_snapshot(
    scene: bpy.types.Scene, snapshot: dict[str, Any], *, limit: int = 20,
) -> list[str]:
    failures: list[str] = []
    for channel in snapshot_channels(scene, snapshot).values():
        actual = _read_channel(channel)
        if actual is None or abs(actual - channel.value) > VALUE_EPSILON:
            failures.append(channel.label)
            if len(failures) >= limit:
                break
    return failures
