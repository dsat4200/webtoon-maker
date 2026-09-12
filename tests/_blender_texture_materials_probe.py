"""Exercise the supplied shader, slot isolation, live controls and image reload."""
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys
from unittest.mock import patch

import bpy


extension = Path(os.environ["WEBTOON_EXTENSION_ROOT"])
sys.path.insert(0, str(extension.parent))
addon = importlib.import_module(extension.name)
addon.register()
materials, textures = addon.materials, addon.textures
root = Path(os.environ["WEBTOON_COMIC_VIEW_FRAME_ROOT"]).parent / "material-probe"
root.mkdir()
context = bpy.context
obj = context.active_object
obj.name = "Material Hero"


def close(a, b):
    try:
        assert len(a) == len(b)
        assert all(abs(float(x) - float(y)) < 0.00001 for x, y in zip(a, b)), (a, b)
    except TypeError:
        assert abs(float(a) - float(b)) < 0.00001, (a, b)


def links(material):
    return sorted((link.from_node.name, link.from_socket.identifier,
                   link.to_node.name, link.to_socket.identifier)
                  for link in material.node_tree.links)


def assert_reference_graph(actual, reference):
    assert links(actual) == links(reference)
    assert len(actual.node_tree.nodes) == len(reference.node_tree.nodes) == 18
    for node in actual.node_tree.nodes:
        other = reference.node_tree.nodes[node.name]
        assert node.bl_idname == other.bl_idname
        close(node.location, other.location)
        for attr in ("blend_type", "data_type", "factor_mode", "clamp_factor",
                     "clamp_result", "interpolation", "extension", "projection",
                     "distribution", "layer_name", "attribute_name", "mode"):
            if hasattr(node, attr):
                assert getattr(node, attr) == getattr(other, attr), (node.name, attr)
        for socket in node.inputs:
            if not hasattr(socket, "default_value"):
                continue
            if (node.name, socket.identifier) in {
                ("Mix Shader", "Fac"), ("Principled BSDF", "Metallic"),
                ("Principled BSDF", "Roughness"),
            }:
                continue
            reference_socket = next((item for item in other.inputs
                                     if item.identifier == socket.identifier), None)
            assert reference_socket is not None, (node.name, socket.identifier)
            close(socket.default_value, reference_socket.default_value)
        if hasattr(node, "color_ramp"):
            ramp, expected = node.color_ramp, other.color_ramp
            assert ramp.interpolation == expected.interpolation
            assert ramp.color_mode == expected.color_mode
            assert len(ramp.elements) == len(expected.elements)
            for first, second in zip(ramp.elements, expected.elements):
                close(first.position, second.position)
                close(first.color, second.color)
    assert actual.surface_render_method == reference.surface_render_method == "BLENDED"


# The bundled asset contains only the user's mug shader, without its old image.
source = extension.parent / "material.blend"
original_hash = hashlib.sha256(source.read_bytes()).hexdigest()
with bpy.data.libraries.load(str(source), link=False) as (src, dst):
    dst.materials = ["mug"]
reference = dst.materials[0]
image_names = set(bpy.data.images.keys())
with bpy.data.libraries.load(str(materials.PRESET_PATH), link=False) as (src, dst):
    assert src.materials == [materials.PRESET_NAME]
    assert not src.objects and not src.scenes and not src.images
    dst.materials = [materials.PRESET_NAME]
preset = dst.materials[0]
assert set(bpy.data.images.keys()) == image_names
assert preset.node_tree.nodes["Image Texture"].image is None
assert_reference_graph(preset, reference)
assert materials.preset_nodes(reference) is not None
assert preset.node_tree.nodes["Principled BSDF"].subsurface_method
bpy.data.materials.remove(preset)

# Slot 1 is the default independently of Blender's active material index. A
# selected slot override does not alter linked geometry or unrelated materials.
obj.data.materials.clear()
unrelated = bpy.data.materials.new("Original Shared Material")
unrelated.use_nodes = True
other_slot = bpy.data.materials.new("Other Slot")
obj.data.materials.append(unrelated)
obj.data.materials.append(other_slot)
obj.active_material_index = 1
other = bpy.data.objects.new("Linked Hero", obj.data)
context.collection.objects.link(other)
settings = obj.webtoon_texture_material_settings
assert settings.slot == "0"
assert other.webtoon_texture_material_settings.slot == "0"
settings.transparency, settings.metallic, settings.roughness = 0.25, 0.42, 0.73
settings.color = (0.5, 0.7, 0.9, 1.0)
texture_settings = context.scene.webtoon_texture_settings
texture_settings.width = texture_settings.height = 32
texture_settings.name_mode = "FULL"
texture_settings.name_text = "Hero Paint"
texture_settings.use_custom_directory = True
texture_settings.directory = str(root)
texture_settings.color = (0.8, 0.1, 0.2, 1.0)
with patch.object(textures, "edit_texture_externally"):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
image = texture_settings.image
material = obj.material_slots[0].material
assert material.name == image.name == "Hero Paint"
assert obj.material_slots[0].link == "OBJECT"
assert obj.data == other.data
assert other.material_slots[0].material == unrelated
assert other.material_slots[1].material == other_slot
assert obj.material_slots[1].material == other_slot
assert unrelated.node_tree.nodes.get("Principled BSDF") is not None
assert len(unrelated.node_tree.nodes) == 2
assert_reference_graph(material, reference)
nodes = materials.preset_nodes(material)
assert nodes["surface"].subsurface_method == "RANDOM_WALK"
assert nodes["image"].image == image
assert material.node_tree.nodes.active == nodes["image"]
close(nodes["transparency"].inputs[0].default_value, 0.25)
close(nodes["surface"].inputs["Metallic"].default_value, 0.42)
close(nodes["surface"].inputs["Roughness"].default_value, 0.73)
close(nodes["color"].outputs[0].default_value, (0.5, 0.7, 0.9, 1.0))

# Controls mutate exactly their requested socket and read edits made in the
# Shader Editor immediately. Switching slots/objects cannot show stale values.
settings.transparency, settings.metallic, settings.roughness = 0.33, 0.21, 0.87
settings.color = (0.2, 0.4, 0.6, 0.8)
close(nodes["transparency"].inputs[0].default_value, 0.33)
close(nodes["surface"].inputs["Metallic"].default_value, 0.21)
close(nodes["surface"].inputs["Roughness"].default_value, 0.87)
close(nodes["color"].outputs[0].default_value, (0.2, 0.4, 0.6, 0.8))
nodes["surface"].inputs["Roughness"].default_value = 0.46
close(settings.roughness, 0.46)
settings.slot = "1"
close(settings.roughness, materials.DEFAULTS["roughness"])
settings.roughness = 0.19
settings.slot = "0"
close(settings.roughness, 0.46)
context.view_layer.objects.active = other
close(other.webtoon_texture_material_settings.roughness, materials.DEFAULTS["roughness"])
context.view_layer.objects.active = obj

# Repeated creation updates the selected preset in place, retaining all shader
# edits without appending a new material or rebuilding its nodes.
node_pointers = [node.as_pointer() for node in material.node_tree.nodes]
material_count = len(bpy.data.materials)
with patch.object(textures, "edit_texture_externally"):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
assert obj.material_slots[0].material == material
assert len(bpy.data.materials) == material_count
assert [node.as_pointer() for node in material.node_tree.nodes] == node_pointers
image = texture_settings.image
assert material.name == image.name == "Hero Paint.001"
assert nodes["image"].image == image
close(settings.roughness, 0.46)
settings.slot = "1"
with patch.object(textures, "edit_texture_externally"):
    assert bpy.ops.webtoon.create_texture() == {"FINISHED"}
second_material = obj.material_slots[1].material
assert second_material != material
assert second_material.name == texture_settings.image.name
close(settings.roughness, 0.19)
assert other.material_slots[1].material == other_slot
settings.slot = "0"

# Directly imported copies of the untagged supplied preset are also updated in
# place rather than replaced; a generic shader with a matching name is not.
untagged = reference.copy()
untagged.use_fake_user = False
obj.material_slots[0].material = untagged
untagged.node_tree.nodes["Principled BSDF"].inputs["Metallic"].default_value = 0.62
assert materials.apply_texture(obj, image) == untagged
close(settings.metallic, 0.62)
assert untagged.get(materials.PRESET_KEY) == 1
assert untagged.node_tree.nodes["Image Texture"].image == image

# Shared preset controls create a local material, protecting other objects.
other.material_slots[0].link = "OBJECT"
other.material_slots[0].material = untagged
before_roughness = other.webtoon_texture_material_settings.roughness
settings.roughness = 0.07
local_material = obj.material_slots[0].material
assert local_material != untagged
assert other.material_slots[0].material == untagged
close(other.webtoon_texture_material_settings.roughness, before_roughness)
close(settings.roughness, 0.07)
# Renaming our tagged nodes does not disconnect the controls.
materials.preset_nodes(local_material)["color"].name = "User's Tint"
settings.color = (0.1, 0.2, 0.3, 1.0)
close(materials.preset_nodes(local_material)["color"].outputs[0].default_value, (0.1, 0.2, 0.3, 1.0))

# Empty shared meshes acquire slot 1 only on the selected object.
empty_mesh = bpy.data.meshes.new("Empty Shared Mesh")
empty = bpy.data.objects.new("Empty", empty_mesh)
empty_other = bpy.data.objects.new("Empty Other", empty_mesh)
context.collection.objects.link(empty)
context.collection.objects.link(empty_other)
assert materials.apply_texture(empty, image) is not None
assert len(empty.material_slots) == 1 and len(empty_other.material_slots) == 0
assert empty.data != empty_other.data
assert empty.webtoon_texture_material_settings.slot == "0"
empty_material = empty.material_slots[0].material
assert materials.apply_texture(empty, image) == empty_material

# Reload uses the same image datablock, including all material users. It neither
# reassigns the preset nor regenerates an external editor/UV sidecar.
texture_settings.image = image
path = Path(image.filepath)
close(image.pixels[0], 0.8)
replacement = bpy.data.images.new("External Edit", width=32, height=32, alpha=True)
replacement.generated_color = (0.1, 0.9, 0.3, 1.0)
replacement.filepath_raw = str(path)
replacement.file_format = "PNG"
replacement.save()
bpy.data.images.remove(replacement)
old_pointer = image.as_pointer()
old_users = image.users
with patch.object(materials, "apply_texture", side_effect=AssertionError("Must not reassign")), patch.object(textures, "write_uv_handoff", side_effect=AssertionError("Must not recreate UVs")):
    assert bpy.ops.webtoon.reload_texture() == {"FINISHED"}
assert texture_settings.image.as_pointer() == old_pointer
assert image.users == old_users
assert obj.material_slots[0].material == local_material
assert materials.preset_nodes(local_material)["image"].image == image
assert abs(image.pixels[0] - 0.1) < 0.01
assert abs(image.pixels[1] - 0.9) < 0.01
original_path = image.filepath
image.filepath = str(root / "missing.png")
try:
    bpy.ops.webtoon.reload_texture()
except RuntimeError as error:
    assert "not found" in str(error)
else:
    raise AssertionError("Missing texture should report an error")
assert texture_settings.image.as_pointer() == old_pointer
image.filepath = original_path
image.pack()
try:
    bpy.ops.webtoon.reload_texture()
except RuntimeError as error:
    assert "unpacked" in str(error)
else:
    raise AssertionError("Packed texture should report an error")
image.unpack(method="REMOVE")

# Object-local slot choice and all direct shader controls survive .blend reopen.
settings.slot = "1"
settings.transparency = 0.57
settings.color = (0.6, 0.4, 0.2, 1.0)
settings.metallic, settings.roughness = 0.32, 0.81
blend_path = root / "material-settings.blend"
bpy.ops.wm.save_as_mainfile(filepath=str(blend_path))
bpy.ops.wm.open_mainfile(filepath=str(blend_path))
obj = bpy.data.objects["Material Hero"]
settings = obj.webtoon_texture_material_settings
assert settings.slot == "1"
close(settings.transparency, 0.57)
close(settings.color, (0.6, 0.4, 0.2, 1.0))
close(settings.metallic, 0.32)
close(settings.roughness, 0.81)
assert materials.preset_nodes(obj.material_slots[1].material)["image"].image is not None
assert hashlib.sha256(source.read_bytes()).hexdigest() == original_hash
addon.unregister()
assert not hasattr(bpy.types.Object, "webtoon_texture_material_settings")
print("WEBTOON_TEXTURE_MATERIALS_PROBE_OK", flush=True)
