"""Isolated real-Blender texture creation, file safety, and UV handoff probe."""
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import bpy
import bmesh


extension = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension.parent))
addon = importlib.import_module(extension.name)
addon.register()
textures = addon.textures
root = Path(os.environ["WEBTOON_COMIC_VIEW_FRAME_ROOT"]).parent / "texture-probe"
root.mkdir()
context = bpy.context
settings = context.scene.webtoon_texture_settings
obj = context.active_object
obj.name = "Hero"
initial_names = set(bpy.data.images.keys())


def expect_value_error(callback, message):
    try:
        callback()
    except ValueError as error:
        assert message in str(error), str(error)
    else:
        raise AssertionError("Expected a validation error")


expect_value_error(lambda: textures.create_texture(context, settings), "Save the blend file")
assert set(bpy.data.images.keys()) == initial_names
settings.name_mode = "PREFIX"
settings.name_text = "paint_"
assert textures.texture_name(context, settings) == "paint_Hero"
settings.name_text = "Sketch "
assert textures.texture_name(context, settings) == "Sketch Hero"
settings.name_mode = "SUFFIX"
settings.name_text = "_color"
assert textures.texture_name(context, settings) == "Hero_color"
settings.name_text = " diffuse"
assert textures.texture_name(context, settings) == "Hero diffuse"
settings.name_mode = "FULL"
settings.name_text = "  Hero Color  "
assert textures.texture_name(context, settings) == "Hero Color"
settings.name_text = "ink.png"
assert textures.texture_name(context, settings) == "ink"
settings.name_text = "  ink.png  "
assert textures.texture_name(context, settings) == "ink"
settings.name_text = "../../CON:bad?"
assert "/" not in textures.texture_name(context, settings)
assert ":" not in textures.texture_name(context, settings)
settings.name_text = "CON"
assert textures.texture_name(context, settings) == "_CON"
settings.name_text = "墨" * 100
assert len(textures.texture_name(context, settings).encode("utf-8")) <= 56
settings.name_text = "Hero Color"
settings.use_custom_directory = True
expect_value_error(lambda: textures.texture_directory(settings), "Choose a texture directory")
settings.directory = "relative/path"
expect_value_error(lambda: textures.texture_directory(settings), "absolute directory")
settings.directory = "//relative"
expect_value_error(lambda: textures.texture_directory(settings), "Save the blend file")

custom = root / "custom ü textures"
assert bpy.ops.webtoon.texture_directory(directory=str(custom)) == {"FINISHED"}
assert settings.use_custom_directory
settings.width, settings.height = 48, 32
settings.color = (0.1, 0.6, 0.9, 0.5)
commands = []
context.preferences.filepaths.image_editor = str(root / "WebtoonMaker.exe")


def launched(command):
    # Exercise Blender's actual external-edit operator without launching a UI.
    path = Path(command[-1])
    assert path.is_file()
    payload = json.loads(Path(str(path) + ".webtoon.json").read_text(encoding="utf-8"))
    assert payload["image"] == path.name
    commands.append(command)


with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
first = settings.image
path = custom / "Hero Color.png"
assert commands[-1] == [context.preferences.filepaths.image_editor, str(path)]
assert first.name == "Hero Color"
assert first.source == "FILE" and first.use_fake_user
assert list(first.size) == [48, 32]
assert first.filepath == str(path)
assert path.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")
assert sorted(item.name for item in custom.iterdir()) == ["Hero Color.png", "Hero Color.png.webtoon.json"]
loaded = bpy.data.images.load(str(path), check_existing=False)
assert list(loaded.size) == [48, 32]
assert abs(loaded.pixels[3] - 0.5) < 0.01
bpy.data.images.remove(loaded)
payload = json.loads(Path(str(path) + ".webtoon.json").read_text(encoding="utf-8"))
assert payload["schema"] == "webtoon.texture.v1"
overlay = payload["uv_overlay"]
assert overlay["name"] == "Hero UV Map"
assert (overlay["width"], overlay["height"]) == (48, 32)
assert 0 < len(overlay["segments"]) <= len(obj.data.loops)
assert len({tuple(segment) for segment in overlay["segments"]}) == len(overlay["segments"])
assert all(len(segment) == 4 for segment in overlay["segments"])

# Repeated creation never overwrites an image or its native editing project.
original = path.read_bytes()
with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
assert settings.image.name == "Hero Color.001"
assert path.read_bytes() == original
settings.name_text = "ExistingProject"
(custom / "ExistingProject").mkdir()
with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
assert settings.image.name == "ExistingProject.001"

# Read the live edit mesh without changing object mode or selection.
bpy.ops.object.mode_set(mode="EDIT")
edit_mesh = bmesh.from_edit_mesh(obj.data)
edit_layer = edit_mesh.loops.layers.uv.active
edit_loop = next(iter(edit_mesh.faces)).loops[0]
edit_uv = tuple(edit_loop[edit_layer].uv)
edit_loop[edit_layer].uv = (0.3333333, 0.6666667)
assert any(0.3333333 in segment for segment in textures.uv_overlay(context, first)["segments"])
assert obj.mode == "EDIT"
edit_loop[edit_layer].uv = edit_uv
bpy.ops.object.mode_set(mode="OBJECT")
old_uv = tuple(obj.data.uv_layers.active.uv[0].vector)
obj.data.uv_layers.active.uv[0].vector = (0.12345, 0.45678)
settings.image = first
with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.edit_texture_externally() == {"FINISHED"}
updated = json.loads(Path(str(path) + ".webtoon.json").read_text(encoding="utf-8"))
assert updated["uv_overlay"]["segments"] != overlay["segments"]
obj.data.uv_layers.active.uv[0].vector = old_uv
with patch.object(textures, "MAX_UV_SEGMENTS", 1):
    expect_value_error(lambda: textures.uv_overlay(context, first), "200,000 edges")
settings.include_uv_map = False
with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.edit_texture_externally() == {"FINISHED"}
assert json.loads(Path(str(path) + ".webtoon.json").read_text(encoding="utf-8"))["uv_overlay"] is None
settings.include_uv_map = True

# The texture remains available for retry if only the editor launch fails.
settings.name_text = "Retry"
with patch.object(textures, "edit_texture_externally", side_effect=RuntimeError("Test editor unavailable")):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
assert settings.image.name == "Retry" and (custom / "Retry.png").is_file()

# Failed encoding/publication removes only the newly reserved file and image.
settings.name_text = "DiskFailure"
names_before = set(bpy.data.images.keys())
files_before = set(custom.iterdir())
with patch.object(textures.os, "replace", side_effect=OSError("Test disk full")):
    try:
        textures.create_texture(context, settings)
    except OSError:
        pass
    else:
        raise AssertionError("Expected save failure")
assert set(bpy.data.images.keys()) == names_before
assert set(custom.iterdir()) == files_before
# Settings survive a .blend reopen and default paths create tex beside that file.
settings.name_text = "Persisted"
settings.generated_type = "UV_GRID"
settings.alpha = False
blend_path = root / "scene.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
settings.name_text = "Changed"
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
settings = bpy.context.scene.webtoon_texture_settings
assert settings.name_text == "Persisted"
assert settings.generated_type == "UV_GRID"
assert not settings.alpha
assert settings.directory == str(custom)
assert settings.image.name == "Retry"
settings.use_custom_directory = False
assert textures.texture_directory(settings) == root / "tex"
with patch("subprocess.Popen", side_effect=launched):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
assert Path(settings.image.filepath) == root / "tex" / "Persisted.png"
assert (root / "tex" / "Persisted.png").is_file()
loaded = bpy.data.images.load(settings.image.filepath, check_existing=False)
assert loaded.pixels[3] == 1
bpy.data.images.remove(loaded)
addon.unregister()
assert not hasattr(bpy.types.Scene, "webtoon_texture_settings")
print("WEBTOON_TEXTURES_PROBE_OK", flush=True)
