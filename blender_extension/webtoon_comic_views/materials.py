"""Object-local texture slots and controls for the supplied comic shader preset."""
from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import bpy
from bpy.props import EnumProperty, FloatProperty, FloatVectorProperty
from bpy.types import PropertyGroup


PRESET_PATH = Path(__file__).parent / "assets" / "comic_texture.blend"
PRESET_NAME = "Webtoon Comic Texture"
PRESET_KEY = "webtoon_texture_preset"
ROLE_KEY = "webtoon_texture_role"
DEFAULTS = {
    "transparency": 0.0, "color": (1.0, 1.0, 1.0, 1.0),
    "metallic": 0.12727272510528564, "roughness": 0.7681818008422852,
}
_ROLES = {
    "image": ("Image Texture", "ShaderNodeTexImage"),
    "color": ("Color", "ShaderNodeRGB"),
    "surface": ("Principled BSDF", "ShaderNodeBsdfPrincipled"),
    "transparency": ("Mix Shader", "ShaderNodeMixShader"),
}
# The untagged mug material from the supplied material.blend is also our preset.
# Compare its actual topology, not its name, so unrelated materials stay intact.
_REFERENCE_LINKS = frozenset({
    ("Shader to RGB", "Color", "Separate Color.001", "Color"),
    ("Separate Color.001", "Red", "Color Ramp.001", "Fac"),
    ("Color Ramp.001", "Color", "Combine Color", "Red"),
    ("Separate Color.001", "Blue", "Color Ramp.002", "Fac"),
    ("Combine Color", "Color", "Mix.008", "A_Color"),
    ("Color Ramp.002", "Color", "Combine Color", "Blue"),
    ("Separate Color.001", "Green", "Invert Color", "Color"),
    ("Color Attribute", "Color", "Hue/Saturation/Value", "Color"),
    ("Hue/Saturation/Value", "Color", "Mix.008", "B_Color"),
    ("Separate Color.001", "Green", "Color Ramp", "Fac"),
    ("Color Ramp", "Color", "Combine Color", "Green"),
    ("Mix Shader", "Shader", "Material Output", "Surface"),
    ("Principled BSDF", "BSDF", "Shader to RGB", "Shader"),
    ("Mix.010", "Result_Color", "Mix Shader", "Shader"),
    ("Transparent BSDF", "BSDF", "Mix Shader", "Shader_001"),
    ("Mix.008", "Result_Color", "Mix.009", "A_Color"),
    ("Image Texture", "Color", "Mix.009", "B_Color"),
    ("Mix.009", "Result_Color", "Mix.010", "A_Color"),
    ("Color", "Color", "Mix.010", "B_Color"),
})


def supports_material(obj) -> bool:
    return obj is not None and obj.type != "GREASEPENCIL" and hasattr(obj.data, "materials")


def slot_index(settings) -> int:
    count = len(settings.id_data.material_slots)
    return min(max(int(settings.get("slot", 0)), 0), max(0, count - 1))


def selected_material(settings):
    slots = settings.id_data.material_slots
    return slots[slot_index(settings)].material if slots else None


@lru_cache(maxsize=128)
def _cached_slot_items(names):
    # Retain dynamic enum strings: Blender keeps references to this storage.
    return tuple((str(index), f"{index + 1}: {name}",
                  "Replace this slot, or update its existing Comic Views preset", index)
                 for index, name in enumerate(names or ("New Material",)))


def _slot_items(settings, _context):
    return _cached_slot_items(tuple(slot.material.name if slot.material else "Empty"
                                    for slot in settings.id_data.material_slots))


def _set_slot(settings, value):
    settings["slot"] = int(value)


def preset_nodes(material):
    if material is None or material.node_tree is None:
        return None
    tree = material.node_tree
    tagged = material.get(PRESET_KEY) == 1
    if not tagged:
        if len(tree.nodes) != 18 or len(tree.links) != len(_REFERENCE_LINKS):
            return None
        links = frozenset((link.from_node.name, link.from_socket.identifier,
                           link.to_node.name, link.to_socket.identifier)
                          for link in tree.links)
        if links != _REFERENCE_LINKS:
            return None
    found = {}
    for role, (name, node_type) in _ROLES.items():
        node = tree.nodes.get(name)
        if node is None or node.bl_idname != node_type or (tagged and node.get(ROLE_KEY) != role):
            node = next((item for item in tree.nodes
                         if item.get(ROLE_KEY) == role and item.bl_idname == node_type), None)
        if node is None:
            return None
        found[role] = node
    return found


def _socket(nodes, name):
    if name == "transparency":
        return nodes["transparency"].inputs[0]
    if name == "color":
        return nodes["color"].outputs[0]
    return nodes["surface"].inputs[name.capitalize()]


def _get_control(settings, name):
    nodes = preset_nodes(selected_material(settings))
    if nodes is not None:
        return _socket(nodes, name).default_value
    return settings.get(f"slot_{slot_index(settings)}_{name}", DEFAULTS[name])


def _assign_slot(obj, index, material):
    if not obj.material_slots:
        # A new slot on shared geometry needs its own data block; otherwise the
        # slot and its material would also appear on every linked duplicate.
        if obj.data.users > 1:
            obj.data = obj.data.copy()
        obj.data.materials.append(material)
    slot = obj.material_slots[index]
    # Existing slots can be isolated without copying any mesh/UV geometry.
    if obj.data.users > 1:
        slot.link = "OBJECT"
    slot.material = material


def _local_material(settings, material):
    if material.library is not None or material.users - int(material.use_fake_user) > 1:
        material = material.copy()
        material.use_fake_user = False
        _assign_slot(settings.id_data, slot_index(settings), material)
    return material


def _set_control(settings, name, value):
    material = selected_material(settings)
    nodes = preset_nodes(material)
    if nodes is not None:
        material = _local_material(settings, material)
        nodes = preset_nodes(material)
        socket = _socket(nodes, name)
        # A manually connected input must not silently defeat the UI control.
        for link in tuple(socket.links):
            if name != "color":
                material.node_tree.links.remove(link)
        socket.default_value = value
    else:
        settings[f"slot_{slot_index(settings)}_{name}"] = value


def _control_getter(name):
    def get_value(settings):
        return _get_control(settings, name)
    return get_value


def _control_setter(name):
    def set_value(settings, value):
        _set_control(settings, name, value)
    return set_value


class WebtoonTextureMaterialSettings(PropertyGroup):
    slot: EnumProperty(name="Material Slot", items=_slot_items, get=slot_index, set=_set_slot)
    transparency: FloatProperty(
        name="Transparency", min=0.0, max=1.0,
        description="Factor of the final Mix Shader: 0 is opaque, 1 is transparent",
        get=_control_getter("transparency"), set=_control_setter("transparency"),
    )
    color: FloatVectorProperty(
        name="Color", subtype="COLOR", size=4, min=0.0, max=1.0,
        description="Color node multiplied with the texture and comic shading",
        get=_control_getter("color"), set=_control_setter("color"),
    )
    metallic: FloatProperty(
        name="Metallic", min=0.0, max=1.0,
        description="Metallic input of the preset's Principled BSDF",
        get=_control_getter("metallic"), set=_control_setter("metallic"),
    )
    roughness: FloatProperty(
        name="Roughness", min=0.0, max=1.0,
        description="Roughness input of the preset's Principled BSDF",
        get=_control_getter("roughness"), set=_control_setter("roughness"),
    )


def apply_texture(obj, image):
    """Assign the texture to one object slot, preserving the supplied node graph."""
    if not supports_material(obj):
        return None
    if not obj.is_editable or not obj.data.is_editable:
        raise ValueError("Select an editable object to assign the texture material")
    settings = obj.webtoon_texture_material_settings
    current = selected_material(settings)
    nodes = preset_nodes(current)
    created = None
    if nodes is None:
        values = {name: tuple(settings.color) if name == "color" else getattr(settings, name)
                  for name in DEFAULTS}
        with bpy.data.libraries.load(str(PRESET_PATH), link=False) as (source, destination):
            if PRESET_NAME not in source.materials:
                raise ValueError("The Comic Views material preset is missing from the extension")
            destination.materials = [PRESET_NAME]
        material = created = destination.materials[0]
        material.use_fake_user = False
        nodes = preset_nodes(material)
        if nodes is None:
            bpy.data.materials.remove(material)
            raise ValueError("The Comic Views material preset is damaged")
        # The 4.5 library's enum is migrated to a legacy variant by 5.2. The
        # supplied shader uses RANDOM_WALK; normalize the identifier on import.
        nodes["surface"].subsurface_method = "RANDOM_WALK"
        for name, value in values.items():
            _socket(nodes, name).default_value = value
    else:
        material = _local_material(settings, current)
        nodes = preset_nodes(material)
    try:
        _assign_slot(obj, slot_index(settings), material)
        material.name = image.name
        material[PRESET_KEY] = 1
        for role, node in nodes.items():
            node[ROLE_KEY] = role
        nodes["image"].image = image
        # Texture painting and the Image Editor pick up the new texture, too.
        material.node_tree.nodes.active = nodes["image"]
        return material
    except Exception:
        if created is not None and created.users == 0:
            bpy.data.materials.remove(created)
        raise


def draw_settings(layout, context):
    header, body = layout.panel("webtoon_texture_material", default_closed=True)
    header.label(text="Material Settings")
    if body is None:
        return
    obj = context.active_object
    if not supports_material(obj):
        body.label(text="Select an object to apply the texture material", icon="INFO")
        return
    settings = obj.webtoon_texture_material_settings
    body.enabled = obj.is_editable and obj.data.is_editable
    body.prop(settings, "slot")
    if preset_nodes(selected_material(settings)) is not None:
        body.label(text="Comic Views preset: changes apply immediately")
    else:
        body.label(text="Create Texture applies the Comic Views preset")
    body.prop(settings, "transparency", slider=True)
    body.prop(settings, "color")
    body.prop(settings, "metallic", slider=True)
    body.prop(settings, "roughness", slider=True)


CLASSES = (WebtoonTextureMaterialSettings,)
