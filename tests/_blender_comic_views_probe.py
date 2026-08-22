"""Assertions executed inside Blender 4.5 by test_blender_extension.py."""
from __future__ import annotations

import json
import os
from pathlib import Path
import queue
import socket
import sys
import time

import bpy


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))

import webtoon_comic_views as addon  # noqa: E402
from webtoon_comic_views import renderer, viewport  # noqa: E402
from webtoon_comic_views.bridge import (  # noqa: E402
    BridgeRuntime, BridgeServer, PROTOCOL_VERSION,
)
from webtoon_comic_views.state import (  # noqa: E402
    UUID_KEY, apply_state, capture_state, ensure_uuid,
    migrate_legacy_presentation, parse_state, repair_duplicate_uuids,
    state_digest, state_json, view_layer_for_state,
)


def receive_line(connection: socket.socket) -> dict:
    payload = bytearray()
    deadline = time.monotonic() + 2.0
    while b"\n" not in payload and time.monotonic() < deadline:
        payload.extend(connection.recv(65536))
    return json.loads(bytes(payload).split(b"\n", 1)[0])


addon.register()
try:
    scene = bpy.context.scene
    cube = bpy.data.objects.get("Cube")
    camera = bpy.data.objects.get("Camera")
    light = bpy.data.objects.get("Light")
    assert cube is not None and camera is not None and light is not None
    scene.camera = camera

    entry = scene.webtoon_comic_registered.add()
    entry.owner_uuid = ensure_uuid(camera.data)
    entry.owner_type = camera.data.bl_rna.identifier
    entry.owner_name = camera.data.name
    entry.rna_path = ""
    entry.property_id = "lens"
    entry.label = "Lens (registered)"

    state, capture_warnings = capture_state(
        scene, bpy.context.view_layer,
        stream_frame=(-0.25, 0.1, 1.5, 0.9),
        output_resolution=(640, 360),
    )
    assert state["version"] == 3
    assert "viewport" not in state
    assert state["stream_frame"] == [-0.25, 0.1, 1.5, 0.9]
    assert state["stream_frame_space"] == "camera"
    assert state["local_view"] == {"enabled": False, "object_uuids": []}
    assert state["active_layer_collection_uuid"]
    assert state["render_settings"]["resolution_x"] == scene.render.resolution_x
    assert capture_warnings == []
    assert state["registered"][0]["property_id"] == "lens"
    serialized = state_json(state)
    assert parse_state(serialized) == state
    assert state_digest(state) == state_digest(parse_state(serialized))
    legacy = dict(state)
    legacy["version"] = 2
    legacy["viewport"] = {
        "view_perspective": "CAMERA",
        "view_camera_zoom": 0.0,
        "view_camera_offset": [0.0, 0.0],
    }
    legacy["stream_frame"] = [0.2, 0.2, 0.8, 0.8]
    legacy.pop("stream_frame_space")
    legacy.pop("local_view")
    legacy.pop("active_layer_collection_uuid")
    legacy.pop("render_settings")
    migrated = parse_state(
        state_json(legacy), fallback_stream_frame=(0.2, 0.2, 0.8, 0.8),
        fallback_resolution=(640, 480),
    )
    assert migrated["version"] == 3
    assert migrated["stream_frame"] == [0.2, 0.2, 0.8, 0.8]
    assert migrated["stream_frame_space"] == "viewport_legacy"
    migrated, migration_warning = migrate_legacy_presentation(scene, migrated)
    assert migrated["stream_frame_space"] == "camera"
    assert "viewport" not in migrated
    assert len(migrated["stream_frame"]) == 4
    if migration_warning:
        assert migrated["stream_frame"] == list(viewport.DEFAULT_FRAME)

    # Camera-gate crop coordinates may extend beyond the gate, and output
    # aspect derives from the saved camera-gate aspect rather than a region.
    parsed_outside = parse_state(state_json(state))
    assert parsed_outside["stream_frame"][0] < 0.0
    scene.render.resolution_x, scene.render.resolution_y = 1600, 800
    assert viewport.derive_resolution(800, (0.0, 0.0, 1.0, 1.0), scene) == (
        800, 400,
    )
    assert viewport.derive_resolution(800, (-0.5, 0.0, 1.5, 1.0), scene) == (
        800, 200,
    )
    scene.render.resolution_x, scene.render.resolution_y = 1920, 1080

    # Ordinary camera-view navigation is absent from snapshots and cannot
    # influence camera-derived render matrices. Moving the camera itself can.
    original_navigation = viewport.capture_viewport()
    view_matrix_a, projection_a = viewport.render_matrices(
        scene, bpy.context.view_layer, (-0.25, 0.1, 1.5, 0.9)
    )
    navigated = dict(original_navigation)
    navigated.update({
        "view_camera_zoom": 180.0,
        "view_camera_offset": [0.23, -0.17],
        "view_distance": 91.0,
    })
    viewport.apply_viewport(navigated)
    navigated_state, _warnings = capture_state(
        scene, bpy.context.view_layer,
        stream_frame=(-0.25, 0.1, 1.5, 0.9),
        output_resolution=(640, 360),
    )
    view_matrix_b, projection_b = viewport.render_matrices(
        scene, bpy.context.view_layer, (-0.25, 0.1, 1.5, 0.9)
    )
    assert state_digest(navigated_state) == state_digest(state)
    assert view_matrix_a == view_matrix_b
    assert projection_a == projection_b
    camera.location.x += 1.0
    bpy.context.view_layer.update()
    moved_matrix, _projection = viewport.render_matrices(
        scene, bpy.context.view_layer, (-0.25, 0.1, 1.5, 0.9)
    )
    assert moved_matrix != view_matrix_a
    camera.location.x -= 1.0
    bpy.context.view_layer.update()
    viewport.apply_viewport(original_navigation)

    # Geometry datablock contents are never serialized.
    forbidden_keys = {
        "vertices", "edges", "polygons", "loops", "splines",
        "mesh_data", "curve_data", "texture_pixels",
    }

    def visit(value):
        if isinstance(value, dict):
            assert forbidden_keys.isdisjoint(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(state)

    expected_location = tuple(cube.location)
    expected_lens = camera.data.lens
    expected_energy = light.data.energy
    cube.location = (20, 30, 40)
    camera.data.lens = 19
    light.data.energy = 12
    warnings = apply_state(scene, state)
    assert not [item for item in warnings if item.startswith("Missing")]
    assert tuple(round(value, 5) for value in cube.location) == tuple(
        round(value, 5) for value in expected_location
    )
    assert camera.data.lens == expected_lens
    assert light.data.energy == expected_energy

    # The active layer collection is part of the saved panel state.
    active_collection = bpy.data.collections.new("Panel Active Collection")
    scene.collection.children.link(active_collection)
    active_layer = bpy.context.view_layer.layer_collection.children[
        active_collection.name
    ]
    bpy.context.view_layer.active_layer_collection = active_layer
    collection_state, _warnings = capture_state(scene, bpy.context.view_layer)
    bpy.context.view_layer.active_layer_collection = (
        bpy.context.view_layer.layer_collection
    )
    apply_state(scene, collection_state)
    assert bpy.context.view_layer.active_layer_collection == active_layer

    # Whole-rig snapshot behavior restores every pose-bone transform and
    # custom control without inserting keyframes.
    armature = bpy.data.armatures.new("Probe Rig Data")
    rig = bpy.data.objects.new("Probe Rig", armature)
    scene.collection.objects.link(rig)
    bpy.context.view_layer.objects.active = rig
    rig.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    edit_bone = armature.edit_bones.new("Control")
    edit_bone.head = (0.0, 0.0, 0.0)
    edit_bone.tail = (0.0, 0.0, 1.0)
    bpy.ops.object.mode_set(mode="OBJECT")
    control = rig.pose.bones["Control"]
    control.location = (1.0, 2.0, 3.0)
    control.rotation_mode = "XYZ"
    control.rotation_euler = (0.1, 0.2, 0.3)
    control.scale = (1.1, 1.2, 1.3)
    control["expression"] = 0.75
    pose_state, _warnings = capture_state(scene, bpy.context.view_layer)
    control.location = (9.0, 9.0, 9.0)
    control.rotation_euler = (0.0, 0.0, 0.0)
    control.scale = (2.0, 2.0, 2.0)
    control["expression"] = 0.0
    apply_state(scene, pose_state)
    assert tuple(round(value, 5) for value in control.location) == (1.0, 2.0, 3.0)
    assert tuple(round(value, 5) for value in control.rotation_euler) == (
        0.1, 0.2, 0.3,
    )
    assert tuple(round(value, 5) for value in control.scale) == (1.1, 1.2, 1.3)
    assert control["expression"] == 0.75
    assert rig.animation_data is None

    introduced = bpy.data.objects.new("Introduced", None)
    scene.collection.objects.link(introduced)
    modifier = cube.modifiers.new("Introduced Modifier", "BEVEL")
    warnings = apply_state(scene, state)
    assert introduced.hide_viewport and introduced.hide_render
    assert any("Introduced Modifier" in item for item in warnings)
    assert modifier in cube.modifiers[:]

    duplicate = bpy.data.objects.new("Duplicate UUID", None)
    scene.collection.objects.link(duplicate)
    duplicate[UUID_KEY] = cube[UUID_KEY]
    warnings = repair_duplicate_uuids(scene)
    assert duplicate[UUID_KEY] != cube[UUID_KEY]
    assert any("duplicated Comic UUID" in item for item in warnings)

    panel_layer = scene.view_layers.new("Panel View Layer")
    layer_state, _warnings = capture_state(scene, panel_layer)
    assert view_layer_for_state(scene, layer_state) == panel_layer

    bpy.data.objects.remove(cube, do_unlink=True)
    warnings = apply_state(scene, state)
    assert any("Missing object Cube" in item for item in warnings)

    # Raw OpenGL-style premultiplied, bottom-up pixels become straight-alpha,
    # top-down transport pixels.
    bottom = bytes((10, 20, 30, 128))
    top = bytes((90, 80, 70, 255))
    converted = renderer._to_top_down_straight_alpha(bottom + top, 1, 2)
    assert converted[:4] == top
    assert converted[4:] == bytes((20, 40, 60, 128))
    assert renderer.png_bytes(renderer.RenderFrame(1, 2, converted)).startswith(
        b"\x89PNG\r\n\x1a\n"
    )
    try:
        renderer.validate_resolution(5000, 10)
    except ValueError:
        pass
    else:
        raise AssertionError("invalid resolution was accepted")

    # The socket worker authenticates without touching Blender RNA and rejects
    # a second editor while the authenticated connection is alive.
    incoming, outgoing = queue.Queue(), queue.Queue()
    server = BridgeServer(incoming, outgoing)
    port_probe = socket.socket()
    port_probe.bind(("127.0.0.1", 0))
    port = port_probe.getsockname()[1]
    port_probe.close()
    server.start(port, "probe-token")
    bad = socket.create_connection(("127.0.0.1", port), timeout=2)
    bad.sendall(b'{"type":"HELLO","protocol":1,"token":"wrong"}\n')
    assert receive_line(bad)["code"] == "AUTHENTICATION_FAILED"
    bad.close()
    deadline = time.monotonic() + 2.0
    while server._client_active and time.monotonic() < deadline:
        time.sleep(0.01)
    good = socket.create_connection(("127.0.0.1", port), timeout=2)
    good.sendall(
        json.dumps({
            "type": "HELLO", "protocol": PROTOCOL_VERSION,
            "token": "probe-token",
        }).encode("utf-8") + b"\n"
    )
    assert receive_line(good)["type"] == "HELLO"
    busy = socket.create_connection(("127.0.0.1", port), timeout=2)
    assert receive_line(busy)["code"] == "BUSY"
    busy.close()
    good.close()
    server.stop()

    # Save and Load are state-only. A changed Save rotates one backup, and
    # Revert swaps the two snapshots without rendering.
    runtime = BridgeRuntime()
    views = scene.webtoon_comic_views
    view = views.add()
    view.view_uuid = "00000000000000000000000000000abc"
    view.name = "Probe"
    view.revision = 0
    view.width = view.height = 64
    stream_state, _warnings = capture_state(
        scene, panel_layer, output_resolution=(64, 64)
    )
    view.state_json = ""
    view.state_hash = ""
    view.published_width = view.published_height = 64
    scene.webtoon_comic_settings.active_index = len(views) - 1
    scene.webtoon_comic_settings.loaded_view_uuid = view.view_uuid
    saved_energy = float(light.data.energy)
    light.data.energy = saved_energy + 10.0
    runtime.save_view_state(scene, view)
    assert not view.previous_state_json
    first_saved_json = view.state_json
    light.data.energy = saved_energy + 20.0
    runtime.save_view_state(scene, view)
    second_saved_json = view.state_json
    assert view.previous_state_json == first_saved_json
    light.data.energy = saved_energy + 30.0
    render_calls = []
    original_render = renderer.render_active_camera

    def fake_render(_scene, _view_layer, width, height, **kwargs):
        render_calls.append({
            "width": width,
            "height": height,
            "stream_frame": kwargs.get("stream_frame"),
            "energy": float(light.data.energy),
        })
        return renderer.RenderFrame(width, height, bytes(width * height * 4))

    renderer.render_active_camera = fake_render
    runtime.load_view_state(scene, view)
    assert light.data.energy == saved_energy + 20.0
    assert render_calls == []
    light.data.energy = saved_energy + 30.0
    runtime.revert_view_state(scene, view)
    assert light.data.energy == saved_energy + 10.0
    assert view.state_json == first_saved_json
    assert view.previous_state_json == second_saved_json
    assert render_calls == []
    runtime.revert_view_state(scene, view)
    assert light.data.energy == saved_energy + 20.0

    # Selecting a different row schedules automatic activation/loading; an
    # already loaded row remains idempotent.
    automatic = views.add()
    automatic.view_uuid = "00000000000000000000000000000abd"
    automatic.name = "Automatic Selection"
    automatic.state_json = view.state_json
    automatic.state_hash = view.state_hash
    viewport.set_working_resolution(automatic, view.width, view.height)
    scene.webtoon_comic_settings.active_index = len(views) - 1
    addon._activate_selected_timer()
    assert scene.webtoon_comic_settings.loaded_view_uuid == automatic.view_uuid
    loaded_state_hash = automatic.state_hash
    scene.webtoon_comic_settings.active_index = len(views) - 1
    addon._activate_selected_timer()
    assert automatic.state_hash == loaded_state_hash
    scene.webtoon_comic_settings.loaded_view_uuid = view.view_uuid
    addon._select_index(scene, view.view_uuid)

    # Render transactionally applies the latest Save, then restores unsaved
    # working state and viewport-independent presentation.
    light.data.energy = saved_energy + 30.0
    old_thumbnail = view.thumbnail_png
    runtime.render_saved_view(scene, view)
    assert render_calls[-1]["energy"] == saved_energy + 20.0
    assert light.data.energy == saved_energy + 30.0
    assert view.revision == 1
    assert view.thumbnail_png != old_thumbnail
    prior_revision = view.revision
    prior_thumbnail = view.thumbnail_png

    def failed_render(*_args, **_kwargs):
        assert light.data.energy == saved_energy + 20.0
        raise RuntimeError("intentional render failure")

    renderer.render_active_camera = failed_render
    try:
        runtime.render_saved_view(scene, view)
    except RuntimeError as error:
        assert "intentional" in str(error)
    else:
        raise AssertionError("failed render unexpectedly succeeded")
    assert light.data.energy == saved_energy + 30.0
    assert view.revision == prior_revision
    assert view.thumbnail_png == prior_thumbnail
    renderer.render_active_camera = fake_render

    # Streaming publishes the latest saved state on start, publishes no
    # dependency-graph edits, and distinguishes preview/committed renders.
    runtime.scene_matches_snapshot = True
    runtime._start_stream(scene, {
        "view_uuid": view.view_uuid,
    })
    assert runtime.streaming
    first_memory_name = runtime._memory.name
    stream_render_count = len(render_calls)
    runtime._render_if_needed(scene, 10.0)
    assert len(render_calls) == stream_render_count + 1
    assert not runtime.frame_dirty

    runtime.mark_scene_dirty()
    runtime._render_if_needed(scene, 10.5)
    assert len(render_calls) == stream_render_count + 1
    assert not runtime.frame_dirty

    runtime._handle(scene, {"type": "RENDER_ONCE"})
    runtime._render_if_needed(scene, 11.1)
    assert len(render_calls) == stream_render_count + 2
    assert runtime.frame_kind == "preview"
    assert not runtime.frame_dirty

    runtime.save_view_state(scene, view)
    assert runtime.frame_kind == "preview"
    runtime.request_committed_frame(scene, view)
    runtime._render_if_needed(scene, 11.2)
    assert len(render_calls) == stream_render_count + 3

    # Lack of a free slot retains the newest dirty state rather than
    # overwriting an unacknowledged frame.
    runtime._outstanding = {0: 1, 1: 2, 2: 3}
    runtime.frame_dirty = True
    runtime._render_if_needed(scene, 12.1)
    assert runtime.frame_dirty
    assert len(render_calls) == stream_render_count + 3

    # Stopping and restarting clears the old cadence, so the replacement
    # stream also produces its first frame immediately.
    runtime.stop_stream()
    assert not runtime.frame_dirty
    runtime.scene_matches_snapshot = True
    runtime._start_stream(scene, {"view_uuid": view.view_uuid})
    second_memory_name = runtime._memory.name
    runtime._render_if_needed(scene, 0.1)
    assert len(render_calls) == stream_render_count + 4
    runtime.stop_stream()
    renderer.render_active_camera = original_render

    # Saving with a new working resolution keeps the published size in sync,
    # and a committed stream opens at the saved output resolution even if the
    # published size was left stale, so the new render replaces the editor's
    # cached frame instead of failing.
    viewport.set_working_resolution(view, 128, 128)
    runtime.save_view_state(scene, view)
    saved_resolution = tuple(parse_state(view.state_json)["output_resolution"])
    assert (view.published_width, view.published_height) == saved_resolution
    view.published_width, view.published_height = 64, 64
    messages = []
    runtime.send = lambda message: messages.append(message)
    runtime.scene_matches_snapshot = True
    runtime._start_stream(scene, {"view_uuid": view.view_uuid})
    opens = [m for m in messages if m.get("type") == "STREAM_OPEN"]
    assert opens and (opens[-1]["width"], opens[-1]["height"]) == saved_resolution
    assert (runtime._stream_width, runtime._stream_height) == saved_resolution

    def fake_size_render(_scene, _view_layer, width, height, **_kwargs):
        return renderer.RenderFrame(width, height, bytes(width * height * 4))

    renderer.render_active_camera = fake_size_render
    errors = []
    runtime.send = lambda message: (
        errors.append(message) if message.get("type") == "ERROR"
        else messages.append(message)
    )
    runtime._render_if_needed(scene, 30.0)
    assert errors == []
    assert not runtime.frame_dirty
    assert runtime.last_error == ""
    runtime.stop_stream()

    # The resize rescue swaps buffers without dropping the live stream, and a
    # failed allocation leaves the current stream and cached frame intact.
    runtime.scene_matches_snapshot = True
    messages = []
    errors = []
    runtime.send = lambda message: (
        errors.append(message) if message.get("type") == "ERROR"
        else messages.append(message)
    )
    runtime._open_stream(scene, view, 64, 64, "committed")
    old_name = runtime._memory.name
    runtime._open_stream(scene, view, 96, 96, "committed", replace=True)
    assert runtime._memory.name != old_name
    assert (runtime._stream_width, runtime._stream_height) == (96, 96)
    opens = [m for m in messages if m.get("type") == "STREAM_OPEN"]
    assert opens[-1]["width"] == 96 and opens[-1]["height"] == 96

    from multiprocessing import shared_memory as shared_memory_module

    original_create = shared_memory_module.SharedMemory

    class FailingMemory:
        def __init__(self, *args, **kwargs):
            raise OSError("simulated allocation failure")

    shared_memory_module.SharedMemory = FailingMemory
    try:
        before_name = runtime._memory.name
        try:
            runtime._open_stream(scene, view, 96, 96, "committed", replace=True)
        except OSError:
            pass
        else:
            raise AssertionError("failed allocation did not raise")
        assert runtime._memory.name == before_name
        assert runtime.streaming
        runtime._pending_render = renderer.RenderFrame(128, 128, bytes(128 * 128 * 4))
        runtime.frame_dirty = True
        runtime._render_if_needed(scene, 31.0)
        assert runtime.frame_dirty
        assert runtime._pending_render is not None
        assert errors == []
        assert runtime._memory.name == before_name
    finally:
        shared_memory_module.SharedMemory = original_create
    runtime.stop_stream()
    renderer.render_active_camera = original_render

    from multiprocessing import shared_memory
    for memory_name in (first_memory_name, second_memory_name):
        try:
            shared_memory.SharedMemory(name=memory_name, create=False)
        except FileNotFoundError:
            pass
        else:
            raise AssertionError("shared memory was not unlinked")
finally:
    if 'original_render' in locals():
        renderer.render_active_camera = original_render
    addon.unregister()

print("WEBTOON_COMIC_VIEWS_PROBE_OK")
