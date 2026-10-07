"""Save/New recovery and optional Fourth Axis snapshot integration."""
from __future__ import annotations

import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import bpy

sys.path.insert(0, str(Path(os.environ["WEBTOON_EXTENSION_ROOT"]).parent))
import webtoon_comic_views as addon
from webtoon_comic_views import bridge, renderer, state, timeline


def new_view(name):
    view = bpy.context.scene.webtoon_comic_views.add()
    view.view_uuid = uuid.uuid4().hex
    view.name = name
    view.width = view.height = 64
    return view


addon.register()
runtime = bridge.RUNTIME
original_save = runtime.save_view_state
original_render = runtime.render_saved_view
original_camera_render = renderer.render_active_camera
try:
    addon._initialize_scenes()
    scene = bpy.context.scene
    settings = scene.webtoon_comic_settings
    saved = new_view("Previous panel")
    original_save(scene, saved)
    addon._select_index(scene, saved.view_uuid)
    loaded_uuid = settings.loaded_view_uuid
    previous_count = len(scene.webtoon_comic_views)
    reports = []
    operator = SimpleNamespace(report=lambda level, message: reports.append(message))

    def fail_save(*args):
        raise RuntimeError("intentional bake verification failure")

    runtime.save_view_state = fail_save
    assert addon.WEBTOON_OT_new_comic_view.execute(operator, bpy.context) == {"CANCELLED"}
    assert len(scene.webtoon_comic_views) == previous_count
    assert addon._active_view(scene) == saved
    assert settings.loaded_view_uuid == loaded_uuid

    def fake_save(_scene, view):
        settings.loaded_view_uuid = view.view_uuid
        return []

    def fail_render(*args):
        raise RuntimeError("intentional render failure")

    runtime.save_view_state = fake_save
    runtime.render_saved_view = fail_render
    assert addon.WEBTOON_OT_new_comic_view.execute(operator, bpy.context) == {"CANCELLED"}
    assert len(scene.webtoon_comic_views) == previous_count
    assert addon._active_view(scene) == saved
    assert settings.loaded_view_uuid == loaded_uuid
    runtime.save_view_state = original_save
    runtime.render_saved_view = original_render

    cube = bpy.data.objects["Cube"]
    cube.location.x = 2.125
    saved.is_dirty = True
    settings.active_index = len(scene.webtoon_comic_views)
    addon._initialize_scenes()
    assert addon._active_view(scene) == saved
    assert cube.location.x == 2.125

    if hasattr(scene.camera.data, "spatial_optics"):
        field = bpy.data.objects.new("Saved optical field", None)
        exit_obj = bpy.data.objects.new("Saved portal exit", None)
        scene.collection.objects.link(field)
        scene.collection.objects.link(exit_obj)
        camera_optics = scene.camera.data.spatial_optics
        camera_optics.max_steps = 1024
        camera_optics.enabled = False
        plain = new_view("Ordinary optical role")
        original_save(scene, plain)
        field.spatial_effect.type = "DISTORTION"
        field.spatial_effect.profile = "CORKSCREW"
        field.spatial_effect.strength = 1.2
        field.spatial_effect.twist = 2.0
        field.spatial_effect.portal_target = exit_obj
        camera_optics.enabled = True
        camera_optics.max_steps = 4096
        optical = new_view("Warped optical role")
        original_save(scene, optical)
        runtime.load_view_state(scene, plain)
        assert field.spatial_effect.type == "NONE"
        assert not camera_optics.enabled and camera_optics.max_steps == 1024
        runtime.load_view_state(scene, optical)
        assert field.spatial_effect.type == "DISTORTION"
        assert field.spatial_effect.profile == "CORKSCREW"
        assert abs(field.spatial_effect.strength - 1.2) < 1e-5
        assert field.spatial_effect.portal_target == exit_obj
        assert camera_optics.enabled and camera_optics.max_steps == 4096
        assert timeline.verify_snapshot(scene, state.parse_state(optical.state_json)) == []

        # Rendering selects the saved optical settings and restores working edits.
        scene.frame_set(7, subframe=0.25)
        field.spatial_effect.strength = 2.3
        field.spatial_effect.portal_target = None
        camera_optics.max_steps = 3000
        calls = []

        def fake_camera_render(_scene, _layer, width, height, **kwargs):
            calls.append((field.spatial_effect.strength,
                          field.spatial_effect.portal_target,
                          camera_optics.max_steps))
            return renderer.RenderFrame(width, height, bytes(width * height * 4))

        renderer.render_active_camera = fake_camera_render
        original_render(scene, optical)
        assert len(calls) == 1
        assert abs(calls[0][0] - 1.2) < 1e-5
        assert calls[0][1:] == (exit_obj, 4096)
        assert abs(field.spatial_effect.strength - 2.3) < 1e-5
        assert field.spatial_effect.portal_target is None
        assert camera_optics.max_steps == 3000
        assert scene.frame_current == 7 and abs(scene.frame_subframe - 0.25) < 1e-6

        # Older snapshots retain live native controls until the next Save.
        legacy = state.parse_state(optical.state_json)
        for record in legacy["objects"]:
            record.pop("spatial_effect", None)
            record.pop("spatial_effect_target_uuid", None)
        for record in legacy["cameras"]:
            record["state"] = {key: value for key, value in record["state"].items()
                               if not key.startswith("spatial_optics.")}
        state.apply_state(scene, legacy, bpy.context.view_layer)
        assert abs(field.spatial_effect.strength - 2.3) < 1e-5
        assert camera_optics.max_steps == 3000
        print("WEBTOON_NATIVE_OPTICS_PROBE_OK", flush=True)
finally:
    runtime.save_view_state = original_save
    runtime.render_saved_view = original_render
    renderer.render_active_camera = original_camera_render
    addon.unregister()

print("WEBTOON_COMIC_REGRESSIONS_PROBE_OK", flush=True)
