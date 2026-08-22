"""Assertions executed inside Blender 4.5 by test_blender_extension.py."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import sys
import time
import uuid

import bpy


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))

import webtoon_comic_views as addon  # noqa: E402
from webtoon_comic_views import (  # noqa: E402
    bridge, diagnostics, renderer, timeline, viewport,
)
from webtoon_comic_views.bridge import BridgeServer, PROTOCOL_VERSION  # noqa: E402
from webtoon_comic_views.state import (  # noqa: E402
    capture_state, ensure_uuid, parse_state, repair_duplicate_uuids,
)


def receive_line(connection: socket.socket) -> dict:
    payload = bytearray()
    deadline = time.monotonic() + 2.0
    while b"\n" not in payload and time.monotonic() < deadline:
        payload.extend(connection.recv(65536))
    return json.loads(bytes(payload).split(b"\n", 1)[0])


addon.register()
original_render = renderer.render_active_camera
original_write = bridge._atomic_write
original_runtime_send = bridge.RUNTIME.send
try:
    assert PROTOCOL_VERSION == 3
    assert bpy.types.Operator.bl_rna_get_subclass_py(
        "WEBTOON_OT_render_comic_view"
    ) is addon.WEBTOON_OT_render_comic_view
    assert bpy.types.Operator.bl_rna_get_subclass_py(
        "WEBTOON_OT_update_comic_view"
    ) is addon.WEBTOON_OT_update_comic_view
    assert bpy.types.Operator.bl_rna_get_subclass_py(
        "WEBTOON_OT_copy_logs"
    ) is addon.WEBTOON_OT_copy_logs
    assert addon._initialize_scenes()
    scene = bpy.context.scene
    view_layer = bpy.context.view_layer
    assert scene.camera is not None
    project_uuid = scene.webtoon_comic_settings.project_uuid
    assert len(project_uuid) == 32

    # State snapshots remain geometry-free and viewport navigation independent.
    captured, warnings = capture_state(
        scene, view_layer, stream_frame=viewport.DEFAULT_FRAME,
        output_resolution=(64, 64),
    )
    assert isinstance(warnings, list)
    assert "viewport" not in captured
    assert tuple(captured["output_resolution"]) == (64, 64)

    # The loopback server distinguishes protocol mismatch from token failure.
    incoming, outgoing = queue.Queue(), queue.Queue()
    server = BridgeServer(incoming, outgoing)
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server.start(port, "probe-token")
    bad = socket.create_connection(("127.0.0.1", port), timeout=2)
    bad.sendall(json.dumps({
        "type": "HELLO", "protocol": 2, "token": "probe-token",
    }).encode("utf-8") + b"\n")
    assert receive_line(bad)["code"] == "PROTOCOL_MISMATCH"
    bad.close()
    deadline = time.monotonic() + 2.0
    while server._client_active and time.monotonic() < deadline:
        time.sleep(0.01)
    good = socket.create_connection(("127.0.0.1", port), timeout=2)
    good.sendall(json.dumps({
        "type": "HELLO", "protocol": PROTOCOL_VERSION,
        "token": "probe-token",
    }).encode("utf-8") + b"\n")
    assert receive_line(good)["type"] == "HELLO"
    good.close()
    server.stop()

    runtime = bridge.RUNTIME
    view = scene.webtoon_comic_views.add()
    view.view_uuid = "00000000000000000000000000000abc"
    view.name = "Published Probe"
    viewport.set_working_resolution(view, 64, 64)
    viewport.set_frame_bounds(view, viewport.DEFAULT_FRAME)
    scene.webtoon_comic_settings.active_index = len(scene.webtoon_comic_views) - 1
    scene.webtoon_comic_settings.loaded_view_uuid = view.view_uuid
    runtime.save_view_state(scene, view)
    saved_resolution = tuple(parse_state(view.state_json)["output_resolution"])
    assert len(saved_resolution) == 2 and min(saved_resolution) >= 64

    cube = bpy.data.objects.get("Cube")
    assert cube is not None
    scene.frame_set(7, subframe=0.25)
    cube.location = (2.0, 3.0, 4.0)
    working_cube_location = tuple(cube.location)
    cube.hide_render = True
    render_calls = []

    def fake_render(_scene, _view_layer, width, height, **kwargs):
        render_calls.append({
            "width": width,
            "height": height,
            "cube_hidden": bool(cube.hide_render),
            "stream_frame": kwargs.get("stream_frame"),
        })
        return renderer.RenderFrame(width, height, bytes(width * height * 4))

    renderer.render_active_camera = fake_render
    messages = []
    runtime.connected = True
    runtime.send = lambda message: messages.append(message)
    assert bpy.ops.webtoon.render_comic_view() == {"FINISHED"}
    assert len(render_calls) == 1
    assert not render_calls[-1]["cube_hidden"]
    assert cube.hide_render
    assert scene.frame_current == 7
    assert abs(scene.frame_subframe - 0.25) < 1.0e-6
    assert tuple(cube.location) == working_cube_location
    assert view.revision == 1
    frame_path = Path(view.published_frame_path)
    assert frame_path.is_file()
    assert frame_path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
    updates = [message for message in messages if message.get("type") == "VIEWS_CHANGED"]
    assert updates[-1]["views"][0]["frame_path"] == str(frame_path)
    assert not hasattr(runtime, "streaming")
    assert not hasattr(runtime, "_open_stream")

    report = diagnostics.build_report(
        bpy.context, runtime, extension_version=addon.EXTENSION_VERSION,
        protocol_version=PROTOCOL_VERSION,
    )
    assert "Extension: 0.5.1" in report
    assert "Authentication token: [redacted]" in report
    assert "Render published" in report
    assert view.view_uuid in report
    assert bpy.ops.webtoon.copy_logs() == {"FINISHED"}
    # Headless Blender accepts the operator but does not expose the OS clipboard.

    # Render and publication failures retain the previous committed metadata.
    prior = (
        view.revision, view.published_frame_path, view.thumbnail_png,
        view.published_width, view.published_height,
    )

    def fail_render(*_args, **_kwargs):
        raise RuntimeError("intentional render failure")

    renderer.render_active_camera = fail_render
    try:
        runtime.render_saved_view(scene, view)
    except RuntimeError:
        pass
    else:
        raise AssertionError("failed render unexpectedly succeeded")
    assert prior == (
        view.revision, view.published_frame_path, view.thumbnail_png,
        view.published_width, view.published_height,
    )

    renderer.render_active_camera = fake_render

    def fail_write(*_args, **_kwargs):
        raise OSError("intentional write failure")

    bridge._atomic_write = fail_write
    try:
        runtime.render_saved_view(scene, view)
    except OSError:
        pass
    else:
        raise AssertionError("failed publication unexpectedly succeeded")
    assert prior == (
        view.revision, view.published_frame_path, view.thumbnail_png,
        view.published_width, view.published_height,
    )
    bridge._atomic_write = original_write

    # Revision files are immutable and bounded to the latest two per view.
    runtime.render_saved_view(scene, view)
    runtime.render_saved_view(scene, view)
    assert view.revision == 3
    files = sorted(Path(view.published_frame_path).parent.glob("*.png"))
    assert len(files) == 2
    assert {int(path.stem) for path in files} == {2, 3}

    duplicate = scene.webtoon_comic_views.add()
    duplicate.view_uuid = "00000000000000000000000000000abd"
    duplicate.name = "Published Probe Copy"
    duplicate.revision = view.revision
    runtime.duplicate_published_frame(scene, view, duplicate)
    duplicate_path = Path(duplicate.published_frame_path)
    assert duplicate_path.is_file()
    assert duplicate_path != Path(view.published_frame_path)
    assert duplicate_path.read_bytes() == Path(view.published_frame_path).read_bytes()
    runtime.delete_published_frames(scene, duplicate.view_uuid)
    assert not duplicate_path.exists()

    # Animated camera and pose channels are baked automatically at distinct
    # extension-owned frames. No manual key insertion is required for either
    # Comic View snapshot, and a newly varying custom property is backfilled.
    while len(scene.webtoon_comic_views):
        scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)
    scene.webtoon_comic_settings.active_index = -1
    scene.webtoon_comic_settings.loaded_view_uuid = ""
    scene.frame_set(2)
    camera = scene.camera
    camera.animation_data_clear()
    camera.rotation_mode = "XYZ"
    camera.location = (8.0, 8.0, 8.0)
    camera.rotation_euler = (0.8, 0.7, 0.6)
    camera.scale = (1.0, 1.0, 1.0)
    for path in ("location", "rotation_euler", "scale"):
        camera.keyframe_insert(path, frame=2, group="Original Camera")
    shared_camera = camera.copy()
    shared_camera.name = "Timeline Probe Shared Camera"
    shared_camera.data = camera.data.copy()
    scene.collection.objects.link(shared_camera)
    assert shared_camera.animation_data.action is camera.animation_data.action

    armature_data = bpy.data.armatures.new("Timeline Probe Armature")
    armature = bpy.data.objects.new("Timeline Probe Rig", armature_data)
    scene.collection.objects.link(armature)
    bpy.context.view_layer.objects.active = armature
    armature.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bone = armature.data.edit_bones.new("Control")
    edit_bone.head = (0.0, 0.0, 0.0)
    edit_bone.tail = (0.0, 0.0, 1.0)
    axis_bone = armature.data.edit_bones.new("Axis Control")
    axis_bone.head = (1.0, 0.0, 0.0)
    axis_bone.tail = (1.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="POSE")
    control = armature.pose.bones["Control"]
    control.rotation_mode = "QUATERNION"
    control.location = (0.0, 0.0, 0.0)
    control.rotation_quaternion = (1.0, 0.0, 0.0, 0.0)
    control.scale = (1.0, 1.0, 1.0)
    for path in ("location", "rotation_quaternion", "scale"):
        control.keyframe_insert(path, frame=2, group="Original Pose")
    axis_control = armature.pose.bones["Axis Control"]
    axis_control.rotation_mode = "AXIS_ANGLE"
    bpy.ops.object.mode_set(mode="OBJECT")

    shape_mesh = bpy.data.meshes.new("Timeline Probe Shape Mesh")
    shape_mesh.from_pydata(
        [(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
        [], [(0, 1, 2)],
    )
    shape_object = bpy.data.objects.new("Timeline Probe Shape", shape_mesh)
    scene.collection.objects.link(shape_object)
    shape_object.shape_key_add(name="Basis")
    expression_key = shape_object.shape_key_add(name="Expression")
    modifier = shape_object.modifiers.new("Timeline Probe Modifier", "SOLIDIFY")
    visibility_collection = bpy.data.collections.new(
        "Timeline Probe Visibility Collection"
    )
    scene.collection.children.link(visibility_collection)
    light = next(obj for obj in scene.objects if obj.type == "LIGHT")
    registered = scene.webtoon_comic_registered.add()
    registered.owner_uuid = ensure_uuid(scene)
    registered.owner_type = "Scene"
    registered.owner_name = scene.name
    registered.rna_path = "render"
    registered.property_id = "film_transparent"
    registered.label = "Scene render transparency"
    bpy.context.view_layer.update()
    original_camera_keys = {
        (curve.data_path, curve.array_index): float(
            next(point for point in curve.keyframe_points if point.co.x == 2).co.y
        )
        for curve in camera.animation_data.action.fcurves
    }
    original_camera_styles = {
        (curve.data_path, curve.array_index): (
            str(point.interpolation), str(point.handle_left_type),
            str(point.handle_right_type), tuple(point.handle_left),
            tuple(point.handle_right),
        )
        for curve in camera.animation_data.action.fcurves
        for point in curve.keyframe_points if point.co.x == 2
    }

    def timeline_view(name):
        item = scene.webtoon_comic_views.add()
        item.view_uuid = uuid.uuid4().hex
        item.name = name
        viewport.set_working_resolution(item, 64, 64)
        viewport.set_frame_bounds(item, viewport.DEFAULT_FRAME)
        scene.webtoon_comic_settings.active_index = len(
            scene.webtoon_comic_views
        ) - 1
        return item

    def current_transform(target, rotation):
        return (
            tuple(target.location), tuple(getattr(target, rotation)),
            tuple(target.scale),
        )

    def close_tuple(actual, expected):
        return all(abs(float(a) - float(b)) < 1.0e-5 for a, b in zip(actual, expected))

    camera.location = (1.0, 2.0, 3.0)
    camera.rotation_euler = (0.1, 0.2, 0.3)
    camera.scale = (1.1, 1.2, 1.3)
    camera.delta_location = (0.01, 0.02, 0.03)
    camera.data.lens = 38.0
    shared_camera.location = (-1.0, -2.0, -3.0)
    camera["comic_pose"] = 1.0
    camera["static_pose"] = 7.0
    control.location = (0.2, 0.3, 0.4)
    control.rotation_quaternion = (0.9, 0.1, 0.2, 0.3)
    control.scale = (0.8, 0.9, 1.1)
    control["pose_strength"] = 0.25
    axis_control.rotation_axis_angle = (0.35, 0.0, 1.0, 0.0)
    expression_key.value = 0.2
    expression_key.mute = False
    light.data.energy = 125.0
    modifier.show_viewport = True
    modifier.show_render = False
    shape_object.hide_viewport = False
    shape_object.hide_render = True
    visibility_collection.hide_viewport = False
    visibility_collection.hide_render = True
    scene.render.film_transparent = False
    expected_camera_one = current_transform(camera, "rotation_euler")
    expected_shared_one = current_transform(shared_camera, "rotation_euler")
    expected_pose_one = current_transform(control, "rotation_quaternion")
    expected_axis_one = tuple(axis_control.rotation_axis_angle)
    timeline_one = timeline_view("Timeline One")
    runtime.save_view_state(scene, timeline_one)
    scene.webtoon_comic_settings.loaded_view_uuid = timeline_one.view_uuid
    first_timeline_frame = timeline_one.timeline_frame
    assert first_timeline_frame > 2

    camera.location = (4.0, 5.0, 6.0)
    camera.rotation_euler = (0.7, 0.8, 0.9)
    camera.scale = (1.4, 1.5, 1.6)
    camera.delta_location = (0.11, 0.12, 0.13)
    camera.data.lens = 72.0
    shared_camera.location = (-4.0, -5.0, -6.0)
    camera["comic_pose"] = 2.0
    camera["static_pose"] = 7.0
    control.location = (0.5, 0.6, 0.7)
    control.rotation_quaternion = (0.7, 0.2, 0.3, 0.6)
    control.scale = (1.2, 1.3, 1.4)
    control["pose_strength"] = 0.75
    axis_control.rotation_axis_angle = (0.8, 1.0, 0.0, 0.0)
    expression_key.value = 0.85
    expression_key.mute = True
    light.data.energy = 450.0
    modifier.show_viewport = False
    modifier.show_render = True
    shape_object.hide_viewport = True
    shape_object.hide_render = False
    visibility_collection.hide_viewport = True
    visibility_collection.hide_render = False
    scene.render.film_transparent = True
    expected_camera_two = current_transform(camera, "rotation_euler")
    expected_shared_two = current_transform(shared_camera, "rotation_euler")
    expected_pose_two = current_transform(control, "rotation_quaternion")
    expected_axis_two = tuple(axis_control.rotation_axis_angle)
    timeline_two = timeline_view("Timeline Two")
    runtime.save_view_state(scene, timeline_two)
    scene.webtoon_comic_settings.loaded_view_uuid = timeline_two.view_uuid
    assert timeline_two.timeline_frame > first_timeline_frame
    assert timeline_one.bake_hash.startswith("1:")
    assert timeline_two.bake_hash.startswith("1:")

    runtime.load_view_state(scene, timeline_one)
    assert scene.frame_current == timeline_one.timeline_frame
    assert current_transform(camera, "rotation_euler") == expected_camera_one
    assert current_transform(shared_camera, "rotation_euler") == expected_shared_one
    assert current_transform(control, "rotation_quaternion") == expected_pose_one
    assert camera["comic_pose"] == 1.0
    assert close_tuple(camera.delta_location, (0.01, 0.02, 0.03))
    assert camera.data.lens == 38.0
    assert control["pose_strength"] == 0.25
    assert close_tuple(axis_control.rotation_axis_angle, expected_axis_one)
    assert abs(expression_key.value - 0.2) < 1.0e-5 and not expression_key.mute
    assert light.data.energy == 125.0
    assert modifier.show_viewport and not modifier.show_render
    assert not shape_object.hide_viewport and shape_object.hide_render
    assert not visibility_collection.hide_viewport
    assert visibility_collection.hide_render
    assert not scene.render.film_transparent
    runtime.load_view_state(scene, timeline_two)
    assert scene.frame_current == timeline_two.timeline_frame
    assert current_transform(camera, "rotation_euler") == expected_camera_two
    assert current_transform(shared_camera, "rotation_euler") == expected_shared_two
    assert current_transform(control, "rotation_quaternion") == expected_pose_two
    assert camera["comic_pose"] == 2.0
    assert close_tuple(camera.delta_location, (0.11, 0.12, 0.13))
    assert camera.data.lens == 72.0
    assert control["pose_strength"] == 0.75
    assert close_tuple(axis_control.rotation_axis_angle, expected_axis_two)
    assert abs(expression_key.value - 0.85) < 1.0e-5 and expression_key.mute
    assert light.data.energy == 450.0
    assert not modifier.show_viewport and modifier.show_render
    assert shape_object.hide_viewport and not shape_object.hide_render
    assert visibility_collection.hide_viewport
    assert not visibility_collection.hide_render
    assert scene.render.film_transparent
    assert visibility_collection.animation_data_create() is None
    assert any(
        "apply_only_channels=" in event for event in diagnostics.recent_events()
    )
    camera_action = camera.animation_data.action
    assert shared_camera.animation_data.action is not camera_action
    assert camera_action.fcurves.find('["comic_pose"]', index=0) is not None
    assert camera_action.fcurves.find('["static_pose"]', index=0) is None
    assert camera_action.fcurves.find("delta_location", index=0) is not None
    assert camera.data.animation_data.action.fcurves.find("lens", index=0) is not None
    assert light.data.animation_data.action.fcurves.find("energy", index=0) is not None
    shape_action = shape_object.data.shape_keys.animation_data.action
    assert shape_action.fcurves.find(
        'key_blocks["Expression"].value', index=0
    ) is not None
    assert shape_object.animation_data.action.fcurves.find(
        'modifiers["Timeline Probe Modifier"].show_viewport', index=0
    ) is not None
    assert scene.animation_data.action.fcurves.find(
        "render.film_transparent", index=0
    ) is not None
    pose_action = armature.animation_data.action
    assert pose_action.fcurves.find(
        'pose.bones["Control"]["pose_strength"]', index=0
    ) is not None
    assert pose_action.fcurves.find(
        'pose.bones["Axis Control"].rotation_axis_angle', index=0
    ) is not None
    for action in (
        camera_action, camera.data.animation_data.action,
        light.data.animation_data.action, shape_action,
        shape_object.animation_data.action, scene.animation_data.action,
        pose_action,
    ):
        for curve in action.fcurves:
            for owned_frame in (timeline_one.timeline_frame, timeline_two.timeline_frame):
                point = next(
                    (
                        candidate for candidate in curve.keyframe_points
                        if candidate.co.x == owned_frame
                    ),
                    None,
                )
                if point is not None:
                    assert point.interpolation == "CONSTANT"
    for curve in camera_action.fcurves:
        original = original_camera_keys.get((curve.data_path, curve.array_index))
        if original is None:
            continue
        point = next(point for point in curve.keyframe_points if point.co.x == 2)
        assert float(point.co.y) == original
        assert (
            str(point.interpolation), str(point.handle_left_type),
            str(point.handle_right_type), tuple(point.handle_left),
            tuple(point.handle_right),
        ) == original_camera_styles[(curve.data_path, curve.array_index)]

    # Cached selection performs no normal rewrite, but externally damaged
    # owned keys are detected after dependency-graph evaluation and repaired.
    location_x = camera_action.fcurves.find("location", index=0)
    damaged = next(
        point for point in location_x.keyframe_points
        if point.co.x == timeline_one.timeline_frame
    )
    damaged.co.y = 999.0
    location_x.update()
    runtime.load_view_state(scene, timeline_one)
    repaired = next(
        point for point in location_x.keyframe_points
        if point.co.x == timeline_one.timeline_frame
    )
    assert abs(repaired.co.y - expected_camera_one[0][0]) < 1.0e-5
    assert current_transform(camera, "rotation_euler") == expected_camera_one
    runtime.load_view_state(scene, timeline_two)

    # Render applies the saved collection visibility after selecting the
    # owned frame, then restores the caller's unsaved visibility and frame.
    collection_render_states = []

    def fake_collection_render(_scene, _view_layer, width, height, **_kwargs):
        collection_render_states.append((
            bool(visibility_collection.hide_viewport),
            bool(visibility_collection.hide_render),
        ))
        return renderer.RenderFrame(width, height, bytes(width * height * 4))

    renderer.render_active_camera = fake_collection_render
    working_frame = scene.frame_current
    visibility_collection.hide_viewport = True
    visibility_collection.hide_render = True
    runtime.render_saved_view(scene, timeline_one)
    assert collection_render_states[-1] == (False, True)
    assert scene.frame_current == working_frame
    assert visibility_collection.hide_viewport
    assert visibility_collection.hide_render

    # Save keeps one reversible history state and Revert rebakes it at the
    # same owned frame without disturbing the other view.
    saved_two_json = timeline_two.state_json
    timeline_two_frame = timeline_two.timeline_frame
    camera.location = (9.0, 8.0, 7.0)
    runtime.save_view_state(scene, timeline_two)
    assert timeline_two.previous_state_json == saved_two_json
    assert timeline_two.timeline_frame == timeline_two_frame
    runtime.revert_view_state(scene, timeline_two)
    assert timeline_two.state_json == saved_two_json
    assert timeline_two.timeline_frame == timeline_two_frame
    assert current_transform(camera, "rotation_euler") == expected_camera_two
    assert visibility_collection.hide_viewport
    assert not visibility_collection.hide_render

    # The dirty-view Save-and-Switch operator captures apply-only collection
    # state without crashing, then activates the destination snapshot.
    runtime.load_view_state(scene, timeline_one)
    visibility_collection.hide_viewport = True
    visibility_collection.hide_render = True
    timeline_one.is_dirty = True
    assert bpy.ops.webtoon.activate_comic_view(
        view_uuid=timeline_two.view_uuid, resolution="SAVE"
    ) == {"FINISHED"}
    assert visibility_collection.hide_viewport
    assert not visibility_collection.hide_render
    runtime.load_view_state(scene, timeline_one)
    assert visibility_collection.hide_viewport
    assert visibility_collection.hide_render
    runtime.load_view_state(scene, timeline_two)

    # Duplicate owns a fresh frame containing the source snapshot. A retired
    # frame is not reused even if the visible scene range is shortened.
    scene.webtoon_comic_settings.active_index = next(
        index for index, item in enumerate(scene.webtoon_comic_views)
        if item == timeline_two
    )
    assert bpy.ops.webtoon.duplicate_comic_view() == {"FINISHED"}
    timeline_copy = scene.webtoon_comic_views[-1]
    assert timeline_copy.timeline_frame > timeline_two.timeline_frame
    assert timeline_copy.state_json == timeline_two.state_json
    assert current_transform(camera, "rotation_euler") == expected_camera_two
    retired_frame = timeline_copy.timeline_frame
    scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)
    scene.frame_end = 2
    after_delete = timeline_view("After Retired Frame")
    runtime.save_view_state(scene, after_delete)
    assert after_delete.timeline_frame > retired_frame
    scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)

    # If Blender unexpectedly refuses animation data for a channel classified
    # as animatable, report that channel and roll back rather than dereferencing
    # None. This deliberately bypasses the real Collection RNA classification.
    original_animatable_check = timeline._channel_is_animatable
    visibility_pointer = int(visibility_collection.as_pointer())

    def force_collection_animatable(owner, data_path):
        if int(owner.as_pointer()) == visibility_pointer:
            return True
        return original_animatable_check(owner, data_path)

    timeline._channel_is_animatable = force_collection_animatable
    unavailable_frame_end = scene.frame_end
    unavailable_cursor = scene.webtoon_comic_settings.next_timeline_frame
    unavailable_actions = len(bpy.data.actions)
    unavailable_view = timeline_view("Unavailable Animation Probe")
    try:
        runtime.save_view_state(scene, unavailable_view)
    except RuntimeError as error:
        assert "Blender did not provide animation data" in str(error)
        assert visibility_collection.name in str(error)
        assert "hide_render" in str(error)
    else:
        raise AssertionError("missing animation data unexpectedly baked")
    finally:
        timeline._channel_is_animatable = original_animatable_check
    assert unavailable_view.timeline_frame == 0
    assert not unavailable_view.state_json
    assert scene.frame_end == unavailable_frame_end
    assert scene.webtoon_comic_settings.next_timeline_frame == unavailable_cursor
    assert len(bpy.data.actions) == unavailable_actions
    scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)

    # A strict camera driver conflict rolls back every partial key, Action,
    # frame assignment, range/cursor change, and saved metadata while keeping
    # the visible working state intact.
    runtime.load_view_state(scene, timeline_two)
    driver_curve = camera.driver_add("location", 0)
    driver_curve.driver.expression = "3.25"
    bpy.context.view_layer.update()
    visible_before_failure = current_transform(camera, "rotation_euler")
    old_frame_end = scene.frame_end
    old_cursor = scene.webtoon_comic_settings.next_timeline_frame
    old_action_count = len(bpy.data.actions)
    old_camera_points = {
        (curve.data_path, curve.array_index): [
            (float(point.co.x), float(point.co.y), str(point.interpolation))
            for point in curve.keyframe_points
        ]
        for curve in camera.animation_data.action.fcurves
    }
    failed_view = timeline_view("Rollback Probe")
    try:
        runtime.save_view_state(scene, failed_view)
    except RuntimeError as error:
        assert "Driver conflicts with" in str(error)
        assert "location[0]" in str(error)
    else:
        raise AssertionError("driver conflict unexpectedly baked")
    assert failed_view.timeline_frame == 0
    assert not failed_view.state_json
    assert scene.frame_end == old_frame_end
    assert scene.webtoon_comic_settings.next_timeline_frame == old_cursor
    assert len(bpy.data.actions) == old_action_count
    assert current_transform(camera, "rotation_euler") == visible_before_failure
    assert old_camera_points == {
        (curve.data_path, curve.array_index): [
            (float(point.co.x), float(point.co.y), str(point.interpolation))
            for point in curve.keyframe_points
        ]
        for curve in camera.animation_data.action.fcurves
    }
    camera.driver_remove("location", 0)
    scene.webtoon_comic_views.remove(len(scene.webtoon_comic_views) - 1)

    # A copied UUID is repaired exactly once. Repeated captures remain stable.
    copied_cube = cube.copy()
    scene.collection.objects.link(copied_cube)
    copied_cube["webtoon_comic_uuid"] = ensure_uuid(cube)
    first_repairs = repair_duplicate_uuids(scene)
    assert first_repairs
    assert ensure_uuid(copied_cube) != ensure_uuid(cube)
    assert not repair_duplicate_uuids(scene)
finally:
    bridge.RUNTIME.send = original_runtime_send
    bridge._atomic_write = original_write
    renderer.render_active_camera = original_render
    addon.unregister()

print("WEBTOON_COMIC_VIEWS_PROBE_OK")
