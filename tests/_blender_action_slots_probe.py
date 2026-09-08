"""Exercise real layered Actions in isolated Blender 4.5/5.2 processes."""
from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import bpy


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))
sys.path.insert(0, str(Path(__file__).parent))

import webtoon_comic_views as addon  # noqa: E402
from webtoon_comic_views import action_channels, timeline  # noqa: E402
from webtoon_comic_views.state import (  # noqa: E402
    STATE_VERSION, ensure_uuid, state_digest,
)
from _blender_action_test_utils import (  # noqa: E402
    action_contents, owner_curve,
)


def make_owner(name):
    owner = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(owner)
    return owner


def make_action(name, slot_names):
    action = bpy.data.actions.new(name)
    strip = action.layers.new("Original Layer").strips.new(type="KEYFRAME")
    slots = [action.slots.new(id_type="OBJECT", name=name) for name in slot_names]
    bags = [strip.channelbags.new(slot) for slot in slots]
    return action, slots, bags


def bind(owner, action, slot):
    animation = owner.animation_data_create()
    animation.action = action
    animation.action_slot = slot


def key(bag, path, frame, value):
    curve = bag.fcurves.new(path, index=0)
    point = curve.keyframe_points.insert(frame, value)
    point.interpolation = "BEZIER"
    point.handle_left_type = "FREE"
    point.handle_right_type = "FREE"
    point.handle_left = (frame - 0.5, value - 0.1)
    point.handle_right = (frame + 0.5, value + 0.1)
    return curve


def snapshot(owner, *, location=None, properties=None):
    return {
        "version": STATE_VERSION,
        "stream_frame": [0.0, 0.0, 1.0, 1.0],
        "output_resolution": [64, 64],
        "objects": [{
            "uuid": ensure_uuid(owner),
            "transform": {} if location is None else {"location": location},
            "custom_properties": properties or {},
        }],
    }


def view(name, saved=None):
    return SimpleNamespace(
        name=name, view_uuid=uuid.uuid4().hex, timeline_frame=0, bake_hash="",
        state_json=json.dumps(saved) if saved else "",
    )


def bake(owner, *, location=None, properties=None):
    candidate = snapshot(owner, location=location, properties=properties)
    target = view(owner.name)
    transaction = timeline.prepare_bake(
        bpy.context.scene, [target], target, candidate,
    )
    return target, transaction


def test_owner_slot_and_range():
    owner = make_owner("Second Slot Owner")
    owner["other_slot_only"] = 50.0
    action, slots, bags = make_action(
        "Multi-slot Range", ["Other Owner", owner.name, "Unassigned Late Slot"],
    )
    key(bags[0], "location", 2.0, 111.0)
    key(bags[0], '["other_slot_only"]', 2.0, 50.0)
    key(bags[1], "location", 2.0, 222.0)
    key(bags[2], "location", 1234.25, 333.0)
    bind(owner, action, slots[1])
    assert action_channels.find_action_curve(owner, "location") == owner_curve(
        owner, "location",
    )
    assert action_channels.find_action_curve(owner, '["other_slot_only"]') is None
    assert len(list(action_channels.iter_action_curves(action))) == 4
    assert timeline._max_animation_frame() == 1235
    before = action_contents(action)
    target, transaction = bake(
        owner, location=[12.0, 0.0, 0.0], properties={"other_slot_only": 50.0},
    )
    assert target.timeline_frame == 1236
    assert transaction.keyed_channels == 1
    transaction.commit()
    assert owner.animation_data.action == action
    assert owner.animation_data.action_slot == slots[1]
    assert action_contents(action)[0] == before[0]
    assert action_contents(action)[2] == before[2]
    assert owner_curve(owner, '["other_slot_only"]') is None
    bpy.context.scene.frame_set(target.timeline_frame)
    assert owner.location.x == 12.0


def test_shared_action_isolation():
    owner = make_owner("Shared Action Second Owner")
    peer = make_owner("Shared Action First Owner")
    owner["comic_pose"] = 2.0
    peer["comic_pose"] = 1.0
    action, slots, bags = make_action("Shared Multi-slot", [peer.name, owner.name])
    key(bags[0], '["comic_pose"]', 2.0, 1.0)
    key(bags[1], '["comic_pose"]', 2.0, 2.0)
    bind(peer, action, slots[0])
    bind(owner, action, slots[1])
    original = action_contents(action)
    original_slot_identifier = slots[1].identifier
    target, transaction = bake(owner, properties={"comic_pose": 3.0})
    transaction.commit()
    replacement = owner.animation_data.action
    assert replacement != action
    assert peer.animation_data.action == action
    assert peer.animation_data.action_slot == slots[0]
    assert owner.animation_data.action_slot.identifier == original_slot_identifier
    assert action_contents(action) == original
    assert action_contents(replacement)[0] == original[0]
    bpy.context.scene.frame_set(target.timeline_frame)
    assert owner["comic_pose"] == 3.0
    assert peer["comic_pose"] == 1.0


def test_failed_shared_bake_restores_slot_and_keys():
    owner = make_owner("Rollback Second Owner")
    peer = make_owner("Rollback First Owner")
    owner["comic_pose"] = 2.0
    peer["comic_pose"] = 1.0
    action, slots, bags = make_action("Rollback Multi-slot", [peer.name, owner.name])
    key(bags[0], '["comic_pose"]', 2.0, 1.0)
    key(bags[1], '["comic_pose"]', 2.0, 2.0)
    bind(peer, action, slots[0])
    bind(owner, action, slots[1])
    original = action_contents(action)
    count = len(bpy.data.actions)
    scene = bpy.context.scene
    frame_end = scene.frame_end
    cursor = scene.webtoon_comic_settings.next_timeline_frame
    candidate = snapshot(owner, properties={"comic_pose": 3.0})
    target = view("Injected Shared Failure")
    write_point = timeline._write_point
    calls = []

    def fail_after_writing(*args, **kwargs):
        write_point(*args, **kwargs)
        calls.append(True)
        if len(calls) == 2:
            assert owner.animation_data.action != action
            assert owner.animation_data.action_slot.identifier == slots[1].identifier
            raise RuntimeError("injected failure after slot keys were written")

    timeline._write_point = fail_after_writing
    try:
        try:
            timeline.prepare_bake(scene, [target], target, candidate)
        except RuntimeError as error:
            assert "injected failure" in str(error)
        else:
            raise AssertionError("injected failure did not abort the bake")
    finally:
        timeline._write_point = write_point
    assert len(calls) == 2
    assert owner.animation_data.action == action
    assert owner.animation_data.action_slot == slots[1]
    assert peer.animation_data.action == action
    assert peer.animation_data.action_slot == slots[0]
    assert action_contents(action) == original
    assert len(bpy.data.actions) == count
    assert target.timeline_frame == 0 and not target.bake_hash
    assert scene.frame_end == frame_end
    assert scene.webtoon_comic_settings.next_timeline_frame == cursor


def test_created_structure_rollback():
    # Each case promotes a varying custom property into an Action that has
    # progressively less pre-existing animation structure.
    for mode in (
        "empty-bag", "slot-without-bag", "unassigned-slot", "empty-action",
        "no-action", "no-animation",
    ):
        owner = make_owner(f"Structure Rollback {mode}")
        owner["comic_pose"] = 2.0
        original_action = None
        original_slot = None
        if mode == "no-action":
            owner.animation_data_create()
        elif mode != "no-animation":
            original_action = bpy.data.actions.new(f"Structure {mode}")
            if mode != "empty-action":
                layer = original_action.layers.new("Original Layer")
                strip = layer.strips.new(type="KEYFRAME")
                slot = original_action.slots.new(id_type="OBJECT", name=owner.name)
                if mode == "empty-bag":
                    strip.channelbags.new(slot)
                bind(owner, original_action, slot)
                if mode == "unassigned-slot":
                    owner.animation_data.action_slot = None
                else:
                    original_slot = slot
            else:
                owner.animation_data_create().action = original_action
        original = action_contents(original_action) if original_action else None
        last_slot_identifier = (
            owner.animation_data.last_slot_identifier
            if owner.animation_data is not None else None
        )
        structure = (
            len(original_action.layers),
            sum(len(layer.strips) for layer in original_action.layers),
            sum(len(strip.channelbags) for layer in original_action.layers for strip in layer.strips),
        ) if original_action else None
        count = len(bpy.data.actions)
        saved = view("Structure Previous", snapshot(owner, properties={"comic_pose": 1.0}))
        target = view("Structure Candidate")
        transaction = timeline.prepare_bake(
            bpy.context.scene, [saved, target], target,
            snapshot(owner, properties={"comic_pose": 2.0}),
        )
        assert owner_curve(owner, '["comic_pose"]') is not None
        transaction.rollback()
        if mode == "no-animation":
            assert owner.animation_data is None
        elif mode == "no-action":
            assert owner.animation_data is not None
            assert owner.animation_data.action is None
        else:
            assert owner.animation_data.action == original_action
            assert owner.animation_data.action_slot == original_slot
            assert action_contents(original_action) == original
            assert (
                len(original_action.layers),
                sum(len(layer.strips) for layer in original_action.layers),
                sum(len(strip.channelbags) for layer in original_action.layers for strip in layer.strips),
            ) == structure
        if owner.animation_data is not None:
            assert owner.animation_data.last_slot_identifier == last_slot_identifier
        assert len(bpy.data.actions) == count
        assert target.timeline_frame == 0 and saved.timeline_frame == 0


def test_legacy_bake_migration_preserves_colliding_slot_keys():
    owner = make_owner("Legacy Second Slot Owner")
    action, slots, bags = make_action("Legacy Slot Collision", ["Other", owner.name])
    key(bags[0], "location", 2.0, 111.0)
    key(bags[1], "location", 251.0, 222.0)
    bind(owner, action, slots[1])
    candidate = snapshot(owner, location=[12.0, 0.0, 0.0])
    other_state = snapshot(owner, location=[13.0, 0.0, 0.0])
    target = view("Legacy Target", candidate)
    other = view("Legacy Other", other_state)
    target.timeline_frame = 251
    other.timeline_frame = 252
    target.bake_hash = f"1:{state_digest(candidate)}"
    other.bake_hash = f"1:{state_digest(other_state)}"
    original_markers = target.bake_hash, other.bake_hash
    original = action_contents(action)
    original_collision_key = original[1][2][0][2][0]
    maximum = timeline._max_animation_frame()
    scene = bpy.context.scene
    old_range = scene.frame_end
    old_cursor = scene.webtoon_comic_settings.next_timeline_frame

    transaction = timeline.prepare_bake(scene, [target, other], target, candidate)
    assert target.timeline_frame > max(252, maximum)
    assert other.timeline_frame > target.timeline_frame
    assert original_collision_key in action_contents(action)[1][2][0][2]
    transaction.rollback()
    assert (target.timeline_frame, other.timeline_frame) == (251, 252)
    assert (target.bake_hash, other.bake_hash) == original_markers
    assert action_contents(action) == original
    assert owner.animation_data.action_slot == slots[1]
    assert scene.frame_end == old_range
    assert scene.webtoon_comic_settings.next_timeline_frame == old_cursor

    transaction = timeline.prepare_bake(scene, [target, other], target, candidate)
    transaction.commit()
    migrated_frames = target.timeline_frame, other.timeline_frame
    assert all(
        item.bake_hash.startswith(f"{timeline.BAKE_VERSION}:")
        for item in (target, other)
    )
    assert original_collision_key in action_contents(action)[1][2][0][2]
    assert action_contents(action)[0] == original[0]
    bpy.context.scene.frame_set(target.timeline_frame)
    assert owner.location.x == 12.0
    # A later forced repair of a current bake keeps both newly assigned frames.
    repair = timeline.prepare_bake(
        scene, [target, other], target, candidate, force=True,
    )
    repair.commit()
    assert (target.timeline_frame, other.timeline_frame) == migrated_frames


addon.register()
try:
    addon._initialize_scenes()
    test_owner_slot_and_range()
    test_shared_action_isolation()
    test_failed_shared_bake_restores_slot_and_keys()
    test_created_structure_rollback()
    test_legacy_bake_migration_preserves_colliding_slot_keys()
finally:
    addon.unregister()

print("WEBTOON_ACTION_SLOTS_PROBE_OK")
