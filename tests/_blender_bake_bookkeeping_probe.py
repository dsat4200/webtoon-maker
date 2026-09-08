"""Functional and complexity checks for repeated Save/Switch timeline baking."""
from __future__ import annotations

import cProfile
import json
import os
from pathlib import Path
import pstats
import sys
import time
from types import SimpleNamespace
import uuid

import bpy


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))
sys.path.insert(0, str(Path(__file__).parent))

import webtoon_comic_views as addon  # noqa: E402
from webtoon_comic_views import state as state_module, timeline  # noqa: E402
from webtoon_comic_views.state import STATE_VERSION, ensure_uuid  # noqa: E402
from _blender_action_test_utils import action_contents, owner_curve  # noqa: E402


def make_owner(name, values):
    owner = bpy.data.objects.new(name, None)
    bpy.context.scene.collection.objects.link(owner)
    for name, value in values.items():
        owner[name] = value
    return owner


def snapshot(owner, values):
    return {
        "version": STATE_VERSION,
        "stream_frame": [0.0, 0.0, 1.0, 1.0],
        "output_resolution": [64, 64],
        "objects": [{
            "uuid": ensure_uuid(owner), "transform": {},
            "custom_properties": dict(values),
        }],
    }


def view(name, saved=None):
    return SimpleNamespace(
        name=name, view_uuid=uuid.uuid4().hex, timeline_frame=0, bake_hash="",
        state_json=json.dumps(saved) if saved else "",
    )


def save(views, target, candidate, *, force=False):
    transaction = timeline.prepare_bake(
        bpy.context.scene, views, target, candidate, force=force,
    )
    transaction.commit()
    target.state_json = json.dumps(candidate)
    return transaction


def assert_key(owner, property_name, frame, expected):
    curve = owner_curve(owner, f'["{property_name}"]')
    assert curve is not None, property_name
    point = next(point for point in curve.keyframe_points if point.co.x == frame)
    assert abs(point.co.y - expected) < 1.0e-5, (property_name, tuple(point.co), expected)
    assert point.interpolation == "CONSTANT"


def test_reapplying_identical_transforms_avoids_rna_writes():
    owner = make_owner("Unchanged Transform", {})

    class ObservedTransform:
        def __init__(self):
            object.__setattr__(self, "writes", [])

        def __getattr__(self, attribute):
            return getattr(owner, attribute)

        def __setattr__(self, attribute, value):
            self.writes.append(attribute)
            setattr(owner, attribute, value)

    observed = ObservedTransform()
    values = state_module._object_transform(owner)
    state_module._apply_transform(observed, values, obj=True)
    assert observed.writes == []

    values["location"] = [2.0, 3.0, 4.0]
    values["rotation_mode"] = "QUATERNION"
    state_module._apply_transform(observed, values, obj=True)
    assert observed.writes == ["rotation_mode", "location"]
    assert tuple(owner.location) == (2.0, 3.0, 4.0)
    assert owner.rotation_mode == "QUATERNION"
    observed.writes.clear()
    state_module._apply_transform(observed, values, obj=True)
    assert observed.writes == []


def test_duplicate_repair_preserves_saved_name_and_skips_unneeded_parsing():
    scene = bpy.context.scene
    earlier = make_owner("Earlier Collision", {})
    recorded = make_owner("Recorded Collision Owner", {})
    identifier = ensure_uuid(earlier)
    recorded["webtoon_comic_uuid"] = identifier
    saved = scene.webtoon_comic_views.add()
    saved.view_uuid = uuid.uuid4().hex
    saved.state_json = json.dumps({
        "objects": [{"uuid": identifier, "name": recorded.name}],
    })
    try:
        assert state_module.repair_duplicate_uuids(scene)
        assert ensure_uuid(recorded) == identifier
        assert ensure_uuid(earlier) != identifier
        identifiers = ensure_uuid(recorded), ensure_uuid(earlier)

        original_loads = state_module.json.loads

        def unexpected_parse(*_args, **_kwargs):
            raise AssertionError("duplicate-free capture parsed every saved view")

        state_module.json.loads = unexpected_parse
        try:
            assert not state_module.repair_duplicate_uuids(scene)
        finally:
            state_module.json.loads = original_loads
        assert (ensure_uuid(recorded), ensure_uuid(earlier)) == identifiers
    finally:
        scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)


def test_promoted_channels_backfill_and_existing_frames_do_not_scan():
    owner = make_owner("Progressive Backfill", {"pose": 1.0, "expression": 1.0})
    initial = snapshot(owner, {"pose": 1.0, "expression": 1.0})
    first, second = view("First"), view("Second")
    save([first], first, initial)
    assert owner.animation_data is None

    second_state = snapshot(owner, {"pose": 2.0, "expression": 1.0})
    views = [first, second]
    save(views, second, second_state)
    assert_key(owner, "pose", first.timeline_frame, 1.0)
    assert_key(owner, "pose", second.timeline_frame, 2.0)
    assert owner_curve(owner, '["expression"]') is None
    frames = first.timeline_frame, second.timeline_frame

    # These frames are already owned. Saving a new variation must backfill the
    # newly animated channel without scanning unrelated animation or moving them.
    original_maximum = timeline._max_animation_frame

    def unexpected_scan():
        raise AssertionError("existing v2 frames triggered a global animation scan")

    timeline._max_animation_frame = unexpected_scan
    try:
        changed = snapshot(owner, {"pose": 2.0, "expression": 3.0})
        save(views, second, changed)
        assert_key(owner, "expression", first.timeline_frame, 1.0)
        assert_key(owner, "expression", second.timeline_frame, 3.0)
        assert_key(owner, "pose", first.timeline_frame, 1.0)
        assert_key(owner, "pose", second.timeline_frame, 2.0)
        assert (first.timeline_frame, second.timeline_frame) == frames

        original = action_contents(owner.animation_data.action)
        cached = save(views, second, changed)
        assert cached.cached and cached.keyed_channels == 0
        assert action_contents(owner.animation_data.action) == original

        # Forced repair still repairs an externally damaged owned key even when
        # every view has a current bake marker.
        curve = owner_curve(owner, '["pose"]')
        broken = next(p for p in curve.keyframe_points if p.co.x == first.timeline_frame)
        broken.co.y = 999.0
        curve.update()
        save(views, first, initial, force=True)
        assert_key(owner, "pose", first.timeline_frame, 1.0)
        assert_key(owner, "expression", second.timeline_frame, 3.0)
        assert (first.timeline_frame, second.timeline_frame) == frames
    finally:
        timeline._max_animation_frame = original_maximum


def test_driver_lookup_is_fresh_on_each_bake():
    owner = make_owner("Driver Cache Freshness", {"pose": 1.0})
    first_state = snapshot(owner, {"pose": 1.0})
    second_state = snapshot(owner, {"pose": 2.0})
    first, second = view("Driver First", first_state), view("Driver Second")
    views = [first, second]
    driver = owner.driver_add('["pose"]')
    driver.driver.expression = "1.0"
    transaction = save(views, second, second_state)
    assert transaction.skipped_driver_channels == 1
    assert owner_curve(owner, '["pose"]') is None

    owner.driver_remove('["pose"]')
    transaction = save(views, second, second_state, force=True)
    assert transaction.skipped_driver_channels == 0
    assert_key(owner, "pose", first.timeline_frame, 1.0)
    assert_key(owner, "pose", second.timeline_frame, 2.0)


def test_failed_rebake_restores_all_curve_styles_and_metadata():
    owner = make_owner("Many Curve Rollback", {f"pose_{i}": 1.0 for i in range(24)})
    initial = snapshot(owner, {f"pose_{i}": 1.0 for i in range(24)})
    changed = snapshot(owner, {f"pose_{i}": 2.0 for i in range(24)})
    first, second = view("Rollback First", initial), view("Rollback Second")
    views = [first, second]
    save(views, second, changed)
    action = owner.animation_data.action
    # Nonstandard styles on owned and unowned keys must all survive rollback.
    for index in range(24):
        curve = owner_curve(owner, f'["pose_{index}"]')
        point = curve.keyframe_points.insert(2.0, float(index))
        for point in curve.keyframe_points:
            point.interpolation = "BEZIER"
            point.easing = "EASE_IN_OUT"
            point.handle_left_type = "FREE"
            point.handle_right_type = "FREE"
            point.handle_left = (point.co.x - 0.3, point.co.y - 0.2)
            point.handle_right = (point.co.x + 0.4, point.co.y + 0.5)
        curve.update()
    before = action_contents(action)
    markers = tuple(item.bake_hash for item in views)
    frames = tuple(item.timeline_frame for item in views)
    candidate = snapshot(owner, {f"pose_{i}": 3.0 for i in range(24)})
    write_point = timeline._write_point
    calls = 0

    def fail_after_several_curves(*args, **kwargs):
        nonlocal calls
        write_point(*args, **kwargs)
        calls += 1
        if calls == 17:
            raise RuntimeError("injected bookkeeping failure")

    timeline._write_point = fail_after_several_curves
    try:
        try:
            timeline.prepare_bake(bpy.context.scene, views, second, candidate)
        except RuntimeError as error:
            assert "injected bookkeeping failure" in str(error)
        else:
            raise AssertionError("expected failure was not raised")
    finally:
        timeline._write_point = write_point
    assert calls == 17
    assert owner.animation_data.action == action
    assert action_contents(action) == before
    assert tuple(item.bake_hash for item in views) == markers
    assert tuple(item.timeline_frame for item in views) == frames


def measure_rebake_rna_accesses(channel_count):
    values = {f"pose_{i}": 1.0 for i in range(channel_count)}
    owner = make_owner(f"Bookkeeping Scale {channel_count}", values)
    states = [
        snapshot(owner, {name: float(index + 1) for name in values})
        for index in range(8)
    ]
    views = [view(f"Scale {channel_count} / {index}", state) for index, state in enumerate(states)]
    save(views, views[-1], states[-1])
    before = action_contents(owner.animation_data.action)

    profile = cProfile.Profile()
    start = time.perf_counter()
    profile.enable()
    transaction = timeline.prepare_bake(
        bpy.context.scene, views, views[-1], states[-1], force=True,
    )
    profile.disable()
    elapsed = time.perf_counter() - start
    assert transaction.keyed_channels == channel_count
    transaction.rollback()
    assert action_contents(owner.animation_data.action) == before
    accesses = sum(
        counters[1]
        for (_filename, _line, function), counters in pstats.Stats(profile).stats.items()
        if "as_pointer" in function
    )
    assert accesses > 0, "RNA pointer accesses were not captured by the profiler"
    print(
        f"WEBTOON_BAKE_TIMING channels={channel_count} views=8 "
        f"forced_seconds={elapsed:.6f} rna_pointer_accesses={accesses}"
    )
    return accesses


addon.register()
try:
    addon._initialize_scenes()
    test_reapplying_identical_transforms_avoids_rna_writes()
    test_duplicate_repair_preserves_saved_name_and_skips_unneeded_parsing()
    test_promoted_channels_backfill_and_existing_frames_do_not_scan()
    test_driver_lookup_is_fresh_on_each_bake()
    test_failed_rebake_restores_all_curve_styles_and_metadata()
    small = measure_rebake_rna_accesses(128)
    large = measure_rebake_rna_accesses(512)
    # Four times as many channels should do proportional RNA work. The previous
    # all-curves style scan did about sixteen times as much work here.
    assert large < small * 7, (small, large)
finally:
    addon.unregister()

print("WEBTOON_BAKE_BOOKKEEPING_PROBE_OK")
