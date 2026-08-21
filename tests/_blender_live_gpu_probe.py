"""Visible opt-in GPU smoke test, executed inside Blender 4.5."""
from __future__ import annotations

import os
from pathlib import Path
import sys
import traceback

import bpy
import gpu
import numpy as np


extension_root = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension_root.parent))

from webtoon_comic_views import renderer, viewport  # noqa: E402
from webtoon_comic_views.state import capture_state, ensure_uuid  # noqa: E402


def assert_gpu_buffer_layout() -> None:
    """Exercise the non-square readback shape that used to become stripes."""
    width, height = 13, 7
    split = 3
    bottom = np.array((32, 96, 160, 255), dtype=np.uint8)
    top = np.array((208, 144, 80, 255), dtype=np.uint8)
    offscreen = gpu.types.GPUOffScreen(width, height, format="RGBA8")
    try:
        with offscreen.bind():
            framebuffer = gpu.state.active_framebuffer_get()
            gpu.state.scissor_test_set(True)
            gpu.state.scissor_set(0, 0, width, split)
            framebuffer.clear(color=tuple(float(value) / 255 for value in bottom))
            gpu.state.scissor_set(0, split, width, height - split)
            framebuffer.clear(color=tuple(float(value) / 255 for value in top))
            gpu.state.scissor_test_set(False)
            buffer = framebuffer.read_color(
                0, 0, width, height, 4, 0, "UBYTE"
            )
            raw = renderer._buffer_bytes(buffer, width, height)
    finally:
        gpu.state.scissor_test_set(False)
        offscreen.free()

    assert len(raw) == width * height * 4
    rows = np.frombuffer(raw, dtype=np.uint8).reshape(height, width, 4)
    assert np.all(np.abs(rows[:split].astype(int) - bottom) <= 1)
    assert np.all(np.abs(rows[split:].astype(int) - top) <= 1)
    top_down = np.frombuffer(
        renderer._to_top_down_straight_alpha(raw, width, height),
        dtype=np.uint8,
    ).reshape(height, width, 4)
    assert np.all(np.abs(top_down[0].astype(int) - top) <= 1)
    assert np.all(np.abs(top_down[-1].astype(int) - bottom) <= 1)


def run() -> None:
    try:
        assert_gpu_buffer_layout()
        scene = bpy.context.scene
        camera = bpy.data.objects.get("Camera")
        if camera is None:
            raise RuntimeError("factory scene camera is missing")
        scene.camera = camera
        _window, _area, space, _region = viewport.find_view3d()
        assert space is not None
        space.region_3d.view_perspective = "CAMERA"
        navigation = viewport.capture_viewport()
        view_a, projection_a = viewport.render_matrices(
            scene, bpy.context.view_layer, (-0.2, 0.1, 1.3, 0.9)
        )
        moved_navigation = dict(navigation)
        moved_navigation.update({
            "view_camera_zoom": 220.0,
            "view_camera_offset": [0.31, -0.24],
            "view_distance": 88.0,
        })
        viewport.apply_viewport(moved_navigation)
        view_b, projection_b = viewport.render_matrices(
            scene, bpy.context.view_layer, (-0.2, 0.1, 1.3, 0.9)
        )
        assert view_a == view_b and projection_a == projection_b
        viewport.apply_viewport(navigation)

        # Local View state is contextual to the bound viewport and round-trips
        # exact object membership.
        objects = {ensure_uuid(obj): obj for obj in scene.objects}
        cube = bpy.data.objects.get("Cube")
        assert cube is not None
        cube_uuid = ensure_uuid(cube)
        space.region_3d.view_perspective = "PERSP"
        assert viewport.apply_local_view(objects, True, {cube_uuid}) is None
        assert viewport.local_view_enabled()
        _window, _area, local_space, _region = viewport.find_view3d()
        assert cube.visible_get(
            view_layer=bpy.context.view_layer, viewport=local_space,
        )
        assert all(
            not obj.visible_get(
                view_layer=bpy.context.view_layer, viewport=local_space,
            )
            for obj in scene.objects if obj != cube
        )
        local_state, _warnings = capture_state(scene, bpy.context.view_layer)
        assert local_state["local_view"] == {
            "enabled": True, "object_uuids": [cube_uuid],
        }
        assert viewport.apply_local_view(objects, False, set()) is None
        assert not viewport.local_view_enabled()

        # Overlay suppression is temporary both on success and after an
        # offscreen readback error.
        space.overlay.show_overlays = True
        frame = renderer.render_active_camera(
            scene, bpy.context.view_layer, 320, 180, hide_overlays=True,
        )
        assert space.overlay.show_overlays
        assert frame.width == 320 and frame.height == 180
        assert len(frame.rgba) == 320 * 180 * 4
        pixels = np.frombuffer(frame.rgba, dtype=np.uint8).reshape(180, 320, 4)
        assert int(pixels[:, :, 3].max()) == 255
        assert np.count_nonzero(pixels[:, :, 3]) > 1000
        # A valid factory-scene render contains many opaque neutral pixels;
        # channel-planar corruption instead produces narrow colored stripes.
        opaque_rgb = pixels[pixels[:, :, 3] == 255, :3]
        neutral = np.max(opaque_rgb, axis=1) - np.min(opaque_rgb, axis=1) <= 2
        neutral_count = int(np.count_nonzero(neutral))
        print(
            "WCV_GPU_STATS", len(opaque_rgb), neutral_count,
            int(np.count_nonzero(pixels[:, :, 3])), flush=True,
        )
        assert neutral_count > 100
        original_buffer_bytes = renderer._buffer_bytes

        def fail_readback(*_args, **_kwargs):
            assert not space.overlay.show_overlays
            raise RuntimeError("intentional readback failure")

        renderer._buffer_bytes = fail_readback
        try:
            renderer.render_active_camera(
                scene, bpy.context.view_layer, 64, 64,
                hide_overlays=True,
            )
        except RuntimeError as error:
            assert "intentional" in str(error)
        else:
            raise AssertionError("intentional readback failure did not propagate")
        finally:
            renderer._buffer_bytes = original_buffer_bytes
        assert space.overlay.show_overlays
        print("WEBTOON_LIVE_GPU_PROBE_OK", flush=True)
    except Exception:
        traceback.print_exc()
    finally:
        bpy.ops.wm.quit_blender()


bpy.app.timers.register(run, first_interval=1.0)
print("WEBTOON_LIVE_GPU_PROBE_SCHEDULED", flush=True)
