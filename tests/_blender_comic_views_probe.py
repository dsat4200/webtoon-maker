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
from webtoon_comic_views import renderer  # noqa: E402
from webtoon_comic_views.bridge import (  # noqa: E402
    BridgeRuntime, BridgeServer, PROTOCOL_VERSION,
)
from webtoon_comic_views.state import (  # noqa: E402
    UUID_KEY, apply_state, capture_state, ensure_uuid, parse_state,
    repair_duplicate_uuids, state_digest, state_json, view_layer_for_state,
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

    state, capture_warnings = capture_state(scene, bpy.context.view_layer)
    assert state["version"] == 2
    assert "viewport" in state and "stream_frame" in state
    assert capture_warnings == []
    assert state["registered"][0]["property_id"] == "lens"
    serialized = state_json(state)
    assert parse_state(serialized) == state
    assert state_digest(state) == state_digest(parse_state(serialized))
    legacy = dict(state)
    legacy["version"] = 1
    legacy.pop("viewport")
    legacy.pop("stream_frame")
    legacy.pop("output_resolution")
    migrated = parse_state(
        state_json(legacy), fallback_stream_frame=(0.2, 0.2, 0.8, 0.8),
        fallback_resolution=(640, 480),
    )
    assert migrated["version"] == 2
    assert migrated["stream_frame"] == [0.2, 0.2, 0.8, 0.8]
    assert migrated["output_resolution"] == [640, 480]

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

    # Streaming publishes saved state on start, publishes no dependency-graph
    # edits, and distinguishes explicit preview and committed renders.
    runtime = BridgeRuntime()
    views = scene.webtoon_comic_views
    view = views.add()
    view.view_uuid = "00000000000000000000000000000abc"
    view.name = "Probe"
    view.revision = 1
    view.width = view.height = 64
    stream_state, _warnings = capture_state(
        scene, panel_layer, output_resolution=(64, 64)
    )
    view.state_json = state_json(stream_state)
    view.state_hash = state_digest(stream_state)
    view.published_width = view.published_height = 64
    scene.webtoon_comic_settings.active_index = len(views) - 1
    runtime.scene_matches_snapshot = True
    runtime._start_stream(scene, {
        "view_uuid": view.view_uuid,
    })
    assert runtime.streaming
    first_memory_name = runtime._memory.name
    render_calls = []
    original_render = renderer.render_active_camera

    def fake_render(_scene, _view_layer, width, height, **kwargs):
        render_calls.append((width, height, kwargs.get("stream_frame")))
        return renderer.RenderFrame(width, height, bytes(width * height * 4))

    renderer.render_active_camera = fake_render
    runtime._render_if_needed(scene, 10.0)
    assert len(render_calls) == 1
    assert not runtime.frame_dirty

    runtime.mark_scene_dirty()
    runtime._render_if_needed(scene, 10.5)
    assert len(render_calls) == 1
    assert not runtime.frame_dirty

    runtime._handle(scene, {"type": "RENDER_ONCE"})
    runtime._render_if_needed(scene, 11.1)
    assert len(render_calls) == 2
    assert runtime.frame_kind == "preview"
    assert not runtime.frame_dirty

    runtime.capture_into_view(scene, view, thumbnail=False)
    assert runtime.frame_kind == "committed"
    runtime._render_if_needed(scene, 11.2)
    assert len(render_calls) == 3

    # Lack of a free slot retains the newest dirty state rather than
    # overwriting an unacknowledged frame.
    runtime._outstanding = {0: 1, 1: 2, 2: 3}
    runtime.frame_dirty = True
    runtime._render_if_needed(scene, 12.1)
    assert runtime.frame_dirty
    assert len(render_calls) == 3

    # Stopping and restarting clears the old cadence, so the replacement
    # stream also produces its first frame immediately.
    runtime.stop_stream()
    assert not runtime.frame_dirty
    runtime.scene_matches_snapshot = True
    runtime._start_stream(scene, {"view_uuid": view.view_uuid})
    second_memory_name = runtime._memory.name
    runtime._render_if_needed(scene, 0.1)
    assert len(render_calls) == 4
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
