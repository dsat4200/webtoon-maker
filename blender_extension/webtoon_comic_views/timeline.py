"""Automatic timeline baking for persistent Comic View snapshots."""
from __future__ import annotations

from dataclasses import dataclass, field
import math
from typing import Any, Iterable

import bpy

from . import diagnostics
from .action_channels import (
    action_curve_index, find_action_curve, is_layered_action, iter_action_curves,
    iter_slot_channelbags,
)
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
BAKE_VERSION = 2


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
    lookup: _SnapshotLookup | None = None,
) -> None:
    if owner is None:
        return
    if lookup is None:
        pointer, identifier = _owner_key(owner)
    else:
        pointer = int(owner.as_pointer())
        identifier = lookup.owner_uuids.get(pointer)
        if identifier is None:
            identifier = ensure_uuid(owner)
            lookup.owner_uuids[pointer] = identifier
    components = _components(owner, data_path, value)
    if not components:
        return
    if lookup is None:
        animatable = _channel_is_animatable(owner, data_path)
    else:
        capability_key = (pointer, data_path)
        animatable = lookup.animatable.get(capability_key)
        if animatable is None:
            animatable = _channel_is_animatable(owner, data_path)
            lookup.animatable[capability_key] = animatable
    for index, number in components:
        key = ChannelKey(pointer, identifier, data_path, index)
        result[key] = SnapshotChannel(
            key=key,
            owner=owner,
            value=number,
            label=f"{label}[{index}]" if isinstance(value, list) else label,
            discrete=isinstance(value, (bool, str)),
            strict_driver=strict_driver,
            animatable=animatable,
        )


def _object_lookup(scene: bpy.types.Scene) -> dict[str, bpy.types.Object]:
    return {ensure_uuid(obj): obj for obj in scene.objects}


def _collection_lookup(scene: bpy.types.Scene) -> dict[str, object]:
    result: dict[str, object] = {}
    visited: set[int] = set()

    def walk(collection: object) -> None:
        pointer = int(collection.as_pointer())
        if pointer in visited:
            return
        visited.add(pointer)
        result.setdefault(ensure_uuid(collection), collection)
        for child in collection.children:
            walk(child)

    walk(scene.collection)
    return result


@dataclass
class _SnapshotLookup:
    """Scene/RNA metadata shared only while flattening a group of snapshots."""
    objects: dict[str, object]
    collections: dict[str, object]
    bones: dict[int, dict[str, object]] = field(default_factory=dict)
    modifiers: dict[int, dict[str, object]] = field(default_factory=dict)
    owner_uuids: dict[int, str] = field(default_factory=dict)
    animatable: dict[tuple[int, str], bool] = field(default_factory=dict)
    registered_owners: dict[str, object] | None = None

    @classmethod
    def from_scene(cls, scene: bpy.types.Scene) -> _SnapshotLookup:
        return cls(_object_lookup(scene), _collection_lookup(scene))

    def bone(self, obj: object, identifier: str, name: str) -> object | None:
        pointer = int(obj.as_pointer())
        if pointer not in self.bones:
            by_uuid: dict[str, object] = {}
            for candidate in obj.pose.bones:
                by_uuid.setdefault(ensure_uuid(candidate.bone), candidate)
            self.bones[pointer] = by_uuid
        return self.bones[pointer].get(identifier) or obj.pose.bones.get(name)

    def modifier(self, obj: object, identifier: str, name: str) -> object | None:
        pointer = int(obj.as_pointer())
        if pointer not in self.modifiers:
            by_uuid: dict[str, object] = {}
            for candidate in obj.modifiers:
                by_uuid.setdefault(ensure_uuid(candidate), candidate)
            self.modifiers[pointer] = by_uuid
        return self.modifiers[pointer].get(identifier) or obj.modifiers.get(name)

    def registered(self) -> dict[str, object]:
        if self.registered_owners is None:
            self.registered_owners = {}
            for collection_name in (
                "scenes", "objects", "collections", "cameras", "lights",
                "materials", "worlds", "node_groups", "armatures", "shape_keys",
            ):
                for owner in getattr(bpy.data, collection_name, ()):
                    identifier = str(owner.get(UUID_PROPERTY, ""))
                    self.registered_owners.setdefault(identifier, owner)
        return self.registered_owners


def snapshot_channels(
    scene: bpy.types.Scene, snapshot: dict[str, Any],
    *, lookup: _SnapshotLookup | None = None,
) -> dict[ChannelKey, SnapshotChannel]:
    """Flatten one saved snapshot into writable, numeric RNA channels."""
    result: dict[ChannelKey, SnapshotChannel] = {}
    lookup = lookup or _SnapshotLookup.from_scene(scene)
    objects = lookup.objects
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
                lookup=lookup,
            )
        for field in ("hide_viewport", "hide_render"):
            _add_channel(
                result, obj, field, record.get(field), f"{obj.name}.{field}",
                lookup=lookup,
            )
        for name, value in record.get("custom_properties", {}).items():
            path = f'["{_escape(name)}"]'
            _add_channel(
                result, obj, path, value, f"{obj.name}[{name}]", lookup=lookup,
            )

    for record in snapshot.get("poses", []):
        obj = objects.get(str(record.get("object_uuid", "")))
        bone_name = str(record.get("bone_name", ""))
        bone = None
        if obj is not None and obj.pose:
            wanted = str(record.get("bone_uuid", ""))
            bone = lookup.bone(obj, wanted, bone_name)
        if bone is None:
            continue
        bone_name = bone.name
        prefix = f'pose.bones["{_escape(bone_name)}"]'
        transform = record.get("transform", {})
        for field in TRANSFORM_FIELDS:
            _add_channel(
                result, obj, f"{prefix}.{field}", transform.get(field),
                f"{obj.name}.{bone_name}.{field}",
                lookup=lookup,
            )
        for name, value in record.get("custom_properties", {}).items():
            path = f'{prefix}["{_escape(name)}"]'
            _add_channel(
                result, obj, path, value, f"{obj.name}.{bone_name}[{name}]",
                lookup=lookup,
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
                    lookup=lookup,
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
                lookup=lookup,
            )

    for record in snapshot.get("modifiers", []):
        obj = objects.get(str(record.get("object_uuid", "")))
        name = str(record.get("name", ""))
        modifier = None
        if obj is not None:
            wanted = str(record.get("uuid", ""))
            modifier = lookup.modifier(obj, wanted, name)
        if modifier is None:
            continue
        name = modifier.name
        prefix = f'modifiers["{_escape(name)}"]'
        for field, value in record.get("state", {}).items():
            _add_channel(
                result, obj, f"{prefix}.{field}", value,
                f"{obj.name}.{name}.{field}",
                lookup=lookup,
            )

    collections = lookup.collections
    for record in snapshot.get("collections", []):
        owner = collections.get(str(record.get("uuid", "")))
        if owner is None:
            continue
        for field in ("hide_viewport", "hide_render"):
            _add_channel(
                result, owner, field, record.get(field),
                f"{owner.name}.{field}",
                lookup=lookup,
            )

    registered = snapshot.get("registered", [])
    all_ids = lookup.registered() if registered else {}
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
            lookup=lookup,
        )
    return result


def _action_curve(owner: object, key: ChannelKey) -> object | None:
    return find_action_curve(owner, key.data_path, key.array_index)


def _values_differ(values: Iterable[float]) -> bool:
    values = list(values)
    return bool(values) and any(
        abs(value - values[0]) > VALUE_EPSILON for value in values[1:]
    )


def _max_animation_frame() -> int:
    maximum = 0.0
    for action in bpy.data.actions:
        for curve in iter_action_curves(action):
            for point in (*curve.keyframe_points, *curve.sampled_points):
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
    original_slot: object | None
    had_animation_data: bool
    last_slot_identifier: str
    replacement: object | None = None


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
    owned: bool = False


@dataclass
class BakeTransaction:
    scene: bpy.types.Scene
    frame_end: int
    next_frame_cursor: int = 0
    frame_assignments: list[tuple[object, int]] = field(default_factory=list)
    point_mutations: list[_PointMutation] = field(default_factory=list)
    new_curves: list[tuple[object, object]] = field(default_factory=list)
    new_action_structures: list[tuple[object, object]] = field(default_factory=list)
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
    _styles_by_curve: dict[int, list[_PointStyle]] = field(default_factory=dict)
    _curve_indexes: dict[int, dict[tuple[str, int], object]] = field(
        default_factory=dict,
    )
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
        _restore_point_styles(self, rollback=True)
        for curves, curve in reversed(self.new_curves):
            try:
                curves.remove(curve)
            except (ReferenceError, RuntimeError):
                pass
        for mutation in reversed(self.action_mutations):
            animation = getattr(mutation.owner, "animation_data", None)
            if animation is not None:
                try:
                    animation.action = mutation.original
                    if mutation.original is not None:
                        animation.action_slot = mutation.original_slot
                    animation.last_slot_identifier = mutation.last_slot_identifier
                    if not mutation.had_animation_data:
                        mutation.owner.animation_data_clear()
                except (AttributeError, RuntimeError, TypeError):
                    pass
        for collection, item in reversed(self.new_action_structures):
            try:
                # Blender 4.5 can remove an empty group with its final F-Curve.
                if any(candidate == item for candidate in collection):
                    collection.remove(item)
            except (ReferenceError, RuntimeError):
                pass
        for mutation in reversed(self.action_mutations):
            try:
                replacement = mutation.replacement
                if replacement is not None and replacement.users <= int(
                    replacement.use_fake_user
                ):
                    bpy.data.actions.remove(replacement)
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
    had_animation_data = getattr(owner, "animation_data", None) is not None
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
    mutation = _ActionMutation(
        owner, animation.action, animation.action_slot, had_animation_data,
        animation.last_slot_identifier,
    )
    transaction.action_mutations.append(mutation)
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
        mutation.replacement = action
        animation.action = action
    elif action.users > 1:
        original = action
        action = action.copy()
        mutation.replacement = action
        action.name = f"{original.name} - Webtoon Comic Views - {owner.name}"
        animation.action = action
        # Action assignment may auto-select another compatible slot. Copies keep
        # identifiers, so restore the exact assigned slot explicitly.
        animation.action_slot = (
            action.slots[mutation.original_slot.identifier]
            if mutation.original_slot is not None else None
        )
    cache[pointer] = action
    # A shared Action may have been copied (and its assigned slot restored).
    # Never retain F-Curve references belonging to the pre-copy Action.
    transaction._curve_indexes.pop(pointer, None)
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


def _active_drivers(owner: object) -> set[tuple[str, int]]:
    animation = getattr(owner, "animation_data", None)
    return {
        (curve.data_path, int(curve.array_index))
        for curve in getattr(animation, "drivers", ())
        if not curve.mute
    } if animation is not None else set()


def _indexed_curve(
    transaction: BakeTransaction, owner: object, key: ChannelKey,
) -> object | None:
    pointer = int(owner.as_pointer())
    if pointer not in transaction._curve_indexes:
        transaction._curve_indexes[pointer] = action_curve_index(owner)
    return transaction._curve_indexes[pointer].get((key.data_path, key.array_index))


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
    styles: list[_PointStyle] = []
    transaction._styles_by_curve[pointer] = styles
    for point in curve.keyframe_points:
        owned = any(
            abs(float(point.co.x) - frame) <= FRAME_EPSILON
            for frame in owned_frames
        )
        style = _PointStyle(
            curve=curve,
            frame=float(point.co.x),
            interpolation=str(point.interpolation),
            easing=str(point.easing),
            handle_left_type=str(point.handle_left_type),
            handle_right_type=str(point.handle_right_type),
            handle_left=tuple(float(value) for value in point.handle_left),
            handle_right=tuple(float(value) for value in point.handle_right),
            owned=owned,
        )
        transaction.preserved_styles.append(style)
        styles.append(style)


def _restore_point_styles(
    transaction: BakeTransaction, curve: object | None = None, *,
    rollback: bool = False,
) -> None:
    # Each completed curve restores only its own keys. Scanning all styles
    # accumulated so far here makes a bake quadratic in the channel count.
    styles = (
        transaction.preserved_styles if curve is None
        else transaction._styles_by_curve.get(int(curve.as_pointer()), ())
    )
    for style in styles:
        if style.owned and not rollback:
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
    curve = _indexed_curve(transaction, channel.owner, channel.key)
    if curve is not None:
        return curve
    if not is_layered_action(action):
        curves = action.fcurves
        curve = curves.new(
            channel.key.data_path, index=channel.key.array_index,
            action_group="Webtoon Comic Views",
        )
        transaction.new_curves.append((curves, curve))
        transaction._curve_indexes[channel.key.owner_pointer][
            (channel.key.data_path, channel.key.array_index)
        ] = curve
        return curve

    animation = channel.owner.animation_data
    slot = animation.action_slot
    if slot is None:
        slot = action.slots.new(
            id_type=channel.owner.id_type, name=channel.owner.name,
        )
        transaction.new_action_structures.append((action.slots, slot))
        animation.action_slot = slot
    bags = list(iter_slot_channelbags(action, slot))
    if bags:
        bag = bags[0]
    else:
        layer = next(iter(action.layers), None)
        if layer is None:
            layer = action.layers.new("Webtoon Comic Views")
            transaction.new_action_structures.append((action.layers, layer))
        strip = next(
            (item for item in layer.strips if item.type == "KEYFRAME"), None,
        )
        if strip is None:
            strip = layer.strips.new(type="KEYFRAME")
            transaction.new_action_structures.append((layer.strips, strip))
        bag = strip.channelbags.new(slot)
        transaction.new_action_structures.append((strip.channelbags, bag))
    # Assigning the channelbag group works in both 4.5 and 5.2; the group keyword
    # on fcurves.new was only added in 5.0.
    group = bag.groups.get("Webtoon Comic Views")
    if group is None:
        group = bag.groups.new("Webtoon Comic Views")
        transaction.new_action_structures.append((bag.groups, group))
    curve = bag.fcurves.new(
        channel.key.data_path, index=channel.key.array_index,
    )
    transaction.new_curves.append((bag.fcurves, curve))
    curve.group = group
    curve.update_autoflags(channel.owner)
    transaction._curve_indexes[channel.key.owner_pointer][
        (channel.key.data_path, channel.key.array_index)
    ] = curve
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
    # Existing unique v2 frames require no allocation. The next actual
    # allocation still scans every Action slot/NLA strip before reserving a
    # frame, including user animation added since the previous save.
    needs_allocation = (
        len(existing_frames) != len(views)
        or any(
            str(getattr(view, "bake_hash", "")).startswith("1:")
            for view in views
        )
    )
    next_frame = max(
        int(scene.frame_end), _max_animation_frame() if needs_allocation else 0,
        max(existing_frames, default=0), cursor - 1,
    ) + 1
    for view in views:
        identifier = str(getattr(view, "view_uuid", ""))
        frame = int(getattr(view, "timeline_frame", 0))
        # Version 1 scanned only the first Action slot. Its reserved frames can
        # overlap untouched user keys in another slot, so migrate those views
        # past all animation once instead of overwriting the old frame.
        legacy_bake = str(getattr(view, "bake_hash", "")).startswith("1:")
        if frame <= 0 or frame in used or legacy_bake:
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
    *, prepared_states: dict[str, tuple[dict[str, Any], str]] | None = None,
) -> bool:
    frames: set[int] = set()
    target_uuid = str(getattr(target, "view_uuid", ""))
    for view in usable_views:
        frame = int(getattr(view, "timeline_frame", 0))
        if frame <= 0 or frame in frames:
            return False
        frames.add(frame)
        identifier = str(getattr(view, "view_uuid", ""))
        if prepared_states is not None and identifier in prepared_states:
            _snapshot, marker = prepared_states[identifier]
        else:
            snapshot = (
                candidate if identifier == target_uuid
                else parse_state(view.state_json)
            )
            marker = _bake_marker(snapshot)
            if prepared_states is not None:
                prepared_states[identifier] = (snapshot, marker)
        if str(getattr(view, "bake_hash", "")) != marker:
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
    transaction = BakeTransaction(
        scene=scene, frame_end=int(scene.frame_end),
        next_frame_cursor=int(getattr(
            getattr(scene, "webtoon_comic_settings", None),
            "next_timeline_frame", 0,
        )),
    )
    try:
        prepared_states: dict[str, tuple[dict[str, Any], str]] = {}
        if not force and _cached_bake(
            usable_views, target, candidate, prepared_states=prepared_states,
        ):
            transaction.target_frame = int(target.timeline_frame)
            transaction.cached = True
            scene.frame_end = max(
                int(scene.frame_end),
                *(int(view.timeline_frame) for view in usable_views),
            )
            return transaction
        frames = _allocate_frames(scene, usable_views, transaction)
        transaction.target_frame = frames[target_uuid]
        lookup = _SnapshotLookup.from_scene(scene)
        channels_by_key: dict[ChannelKey, list[tuple[int, SnapshotChannel]]] = {}
        for view in usable_views:
            identifier = str(getattr(view, "view_uuid", ""))
            prepared = prepared_states.pop(identifier, None)
            if prepared is None:
                state = (
                    candidate if identifier == target_uuid
                    else parse_state(view.state_json)
                )
                marker = _bake_marker(state)
            else:
                state, marker = prepared
            for key, channel in snapshot_channels(scene, state, lookup=lookup).items():
                channels_by_key.setdefault(key, []).append((frames[identifier], channel))
            transaction.bake_assignments.append((
                view, str(getattr(view, "bake_hash", "")), marker,
            ))

        needed: set[ChannelKey] = set()
        for key, entries in channels_by_key.items():
            sample = entries[0][1]
            if not sample.animatable:
                transaction.apply_only_channels += 1
                continue
            if _values_differ(channel.value for _frame, channel in entries):
                needed.add(key)
                continue
            if _indexed_curve(transaction, sample.owner, key) is not None:
                needed.add(key)

        action_cache: dict[int, object] = {}
        driver_cache: dict[int, set[tuple[str, int]]] = {}
        boundary_curves: set[int] = set()
        owned_frames = set(frames.values())
        for key in sorted(
            needed,
            key=lambda item: (item.owner_uuid, item.data_path, item.array_index),
        ):
            entries = channels_by_key[key]
            sample = entries[0][1]
            if key.owner_pointer not in driver_cache:
                driver_cache[key.owner_pointer] = _active_drivers(sample.owner)
            if (key.data_path, key.array_index) in driver_cache[key.owner_pointer]:
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
            for frame, channel in entries:
                _write_point(
                    transaction, curve, frame,
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
