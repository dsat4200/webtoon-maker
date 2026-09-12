"""Create file-backed textures and hand their UV guide to the external editor."""
from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import tempfile

import bpy
import bmesh
from bpy.props import (
    BoolProperty, EnumProperty, FloatVectorProperty, IntProperty,
    PointerProperty, StringProperty,
)
from bpy.types import Operator, Panel, PropertyGroup

from . import diagnostics, materials


MAX_UV_SEGMENTS = 200_000
SUPPORTED_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".tga", ".webp"}


class WebtoonTextureSettings(PropertyGroup):
    name_text: StringProperty(name="Name", default="", description="Text used by the naming mode; type any desired separator")
    name_mode: EnumProperty(
        name="Naming Mode", default="SUFFIX",
        items=(
            ("PREFIX", "Prefix", "Place Name before the active object's name"),
            ("SUFFIX", "Suffix", "Place Name after the active object's name"),
            ("FULL", "Full Name", "Use Name as the complete texture name"),
        ),
    )
    width: IntProperty(name="Width", min=1, max=8192, default=1024)
    height: IntProperty(name="Height", min=1, max=8192, default=1024)
    generated_type: EnumProperty(
        name="Fill", default="BLANK",
        items=(("BLANK", "Blank", "Fill with a solid color"),
               ("UV_GRID", "UV Grid", "Generate Blender's UV test grid"),
               ("COLOR_GRID", "Color Grid", "Generate Blender's colored test grid")),
    )
    color: FloatVectorProperty(name="Color", subtype="COLOR", size=4, min=0, max=1, default=(1, 1, 1, 0))
    alpha: BoolProperty(name="Alpha Channel", default=True)
    use_custom_directory: BoolProperty(
        name="Use Custom Directory", default=False,
        description="Choose a directory instead of the tex folder beside the saved blend file",
    )
    directory: StringProperty(name="Directory", subtype="DIR_PATH", options={"PATH_SUPPORTS_BLEND_RELATIVE"})
    include_uv_map: BoolProperty(
        name="Include UV Map Layer", default=True,
        description="Send the active mesh object's active UV map as a separate guide layer",
    )
    image: PointerProperty(name="Texture", type=bpy.types.Image)


def texture_name(context: bpy.types.Context, settings: WebtoonTextureSettings) -> str:
    """Derive a safe Windows filename while preserving the requested spelling."""
    active = context.active_object
    base = active.name if active is not None else (Path(bpy.data.filepath).stem or "Texture")
    text = settings.name_text
    if settings.name_mode == "FULL":
        name = text or "Texture"
    elif settings.name_mode == "PREFIX":
        name = text + base
    else:
        name = base + text
    name = name.strip(" .")
    if name.lower().endswith(".png"):
        name = name[:-4]
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", name).strip(" .") or "Texture"
    if name.split(".", 1)[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}:
        name = "_" + name
    # Blender 4.5 ID names are limited to 63 UTF-8 bytes. Leave room for .001.
    return name.encode("utf-8")[:56].decode("utf-8", errors="ignore").rstrip(" .") or "Texture"


def texture_directory(settings: WebtoonTextureSettings) -> Path:
    if not settings.use_custom_directory:
        if not bpy.data.filepath:
            raise ValueError("Save the blend file first, or enable Use Custom Directory")
        return Path(bpy.data.filepath).parent / "tex"
    raw = settings.directory.strip()
    if not raw:
        raise ValueError("Choose a texture directory in Save File Settings")
    if raw.startswith("//"):
        if not bpy.data.filepath:
            raise ValueError("Save the blend file before using a relative directory")
        path = Path(bpy.path.abspath(raw))
    else:
        path = Path(raw).expanduser()
    if not path.is_absolute():
        raise ValueError("Choose an absolute directory or a // blend-relative directory")
    return path.resolve()


def _reserve_texture_path(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    for index in range(100_000):
        stem = name if index == 0 else f"{name}.{index:03d}"
        path = directory / f"{stem}.png"
        if (bpy.data.images.get(stem) is not None or path.with_suffix("").exists()
                or Path(str(path) + ".webtoon.json").exists()):
            continue
        try:
            with path.open("xb"):
                pass
        except FileExistsError:
            continue
        return path
    raise ValueError("Too many textures share this name; choose another name")


def create_texture(context: bpy.types.Context, settings: WebtoonTextureSettings) -> tuple[bpy.types.Image, Path]:
    width, height = int(settings.width), int(settings.height)
    path = _reserve_texture_path(texture_directory(settings), texture_name(context, settings))
    image = None
    temporary = None
    try:
        image = bpy.data.images.new(path.stem, width=width, height=height, alpha=settings.alpha)
        image.generated_type = settings.generated_type
        color = tuple(settings.color)
        image.generated_color = color if settings.alpha else (*color[:3], 1.0)
        image.use_fake_user = True
        # Blender fills/encodes in native code; no full-size Python pixel list.
        descriptor, temporary = tempfile.mkstemp(prefix=".webtoon-texture-", suffix=".png", dir=path.parent)
        os.close(descriptor)
        image.filepath_raw = temporary
        image.file_format = "PNG"
        image.save()
        os.replace(temporary, path)
        temporary = None
        image.filepath_raw = str(path)
        image.source = "FILE"
        settings.image = image
        return image, path
    except Exception:
        if image is not None:
            bpy.data.images.remove(image)
        path.unlink(missing_ok=True)
        raise
    finally:
        if temporary is not None:
            Path(temporary).unlink(missing_ok=True)


def uv_overlay(context: bpy.types.Context, image: bpy.types.Image) -> dict | None:
    obj = context.active_object
    if obj is None or obj.type != "MESH":
        return None
    mesh = obj.data
    if obj.mode == "EDIT":
        edit_mesh = bmesh.from_edit_mesh(mesh)
        layer = edit_mesh.loops.layers.uv.active
        if layer is None:
            return None
        polygons = ([tuple(loop[layer].uv) for loop in face.loops] for face in edit_mesh.faces)
    else:
        layer = mesh.uv_layers.active
        if layer is None:
            return None
        coordinates = layer.uv
        polygons = ([tuple(coordinates[index].vector) for index in polygon.loop_indices] for polygon in mesh.polygons)
    segments = set()
    for polygon in polygons:
        points = [tuple(round(float(value), 7) for value in point) for point in polygon]
        for offset, first in enumerate(points):
            second = points[(offset + 1) % len(points)]
            if first == second or not all(math.isfinite(value) for value in (*first, *second)):
                continue
            if any(abs(value) > 1_000_000 for value in (*first, *second)):
                raise ValueError("UV coordinates must be within one million UV tiles")
            segment = (*first, *second) if first < second else (*second, *first)
            segments.add(segment)
            if len(segments) > MAX_UV_SEGMENTS:
                raise ValueError("UV map exceeds 200,000 edges; simplify it or disable Include UV Map Layer")
    if not segments:
        return None
    return {"name": f"{obj.name} UV Map", "width": int(image.size[0]),
            "height": int(image.size[1]), "segments": sorted(segments)}


def write_uv_handoff(context: bpy.types.Context, image: bpy.types.Image, path: Path) -> Path:
    settings = context.scene.webtoon_texture_settings
    payload = {"schema": "webtoon.texture.v1", "image": path.name,
               "uv_overlay": uv_overlay(context, image) if settings.include_uv_map else None}
    sidecar = Path(str(path) + ".webtoon.json")
    descriptor, temporary = tempfile.mkstemp(prefix=".webtoon-uv-", suffix=".json", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, sidecar)
    finally:
        Path(temporary).unlink(missing_ok=True)
    return sidecar


def edit_texture_externally(context: bpy.types.Context, image: bpy.types.Image, path: Path) -> None:
    write_uv_handoff(context, image, path)
    # Keep the user's already-configured Webtoon Maker launch mechanism.
    result = bpy.ops.image.external_edit(filepath=str(path))
    if "FINISHED" not in result:
        raise RuntimeError("The external image editor could not be launched")


class WEBTOON_OT_texture_directory(Operator):
    bl_idname = "webtoon.texture_directory"
    bl_label = "Choose Texture Directory"
    bl_description = "Choose where newly created textures will be saved"
    directory: StringProperty(subtype="DIR_PATH", options={"PATH_SUPPORTS_BLEND_RELATIVE"})
    filter_folder: BoolProperty(default=True, options={"HIDDEN"})

    def invoke(self, context, _event):
        settings = context.scene.webtoon_texture_settings
        self.directory = settings.directory or (str(Path(bpy.data.filepath).parent / "tex") if bpy.data.filepath else "")
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}

    def execute(self, context):
        if not self.directory:
            self.report({"ERROR"}, "Choose a directory")
            return {"CANCELLED"}
        settings = context.scene.webtoon_texture_settings
        settings.directory = self.directory
        settings.use_custom_directory = True
        return {"FINISHED"}


class WEBTOON_OT_create_texture(Operator):
    bl_idname = "webtoon.create_texture"
    bl_label = "Create Texture"
    bl_description = "Create and save a PNG texture, then open it with its UV guide in the external image editor"

    def execute(self, context):
        try:
            image, path = create_texture(context, context.scene.webtoon_texture_settings)
        except Exception as error:
            diagnostics.record_exception("Texture creation failed", error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        try:
            materials.apply_texture(context.active_object, image)
        except Exception as error:
            diagnostics.record_exception("Saved texture material could not be assigned", error)
            self.report({"WARNING"}, f"Texture saved; material assignment failed: {error}")
        try:
            edit_texture_externally(context, image, path)
        except Exception as error:
            diagnostics.record_exception("Saved texture could not be opened externally", error)
            self.report({"WARNING"}, f"Saved {path}; external edit failed: {error}. Use Edit Texture Externally to retry")
        else:
            self.report({"INFO"}, f"Saved {path.name} to {path.parent}")
        return {"FINISHED"}


def _context_image(context):
    space = context.space_data
    if space is not None and space.type == "IMAGE_EDITOR":
        return space.image
    return context.scene.webtoon_texture_settings.image


class WEBTOON_OT_edit_texture_externally(Operator):
    bl_idname = "webtoon.edit_texture_externally"
    bl_label = "Edit Texture Externally"
    bl_description = "Save the texture and send its active object's UV map to the configured external image editor"

    @classmethod
    def poll(cls, context):
        return context.scene is not None and _context_image(context) is not None

    def execute(self, context):
        image = _context_image(context)
        try:
            if image.source not in {"FILE", "GENERATED"} or image.packed_file:
                raise ValueError("Use an unpacked single-file texture image")
            if not image.filepath:
                raise ValueError("Save the image first or use Create Texture")
            path = Path(bpy.path.abspath(image.filepath, library=image.library))
            if path.suffix.lower() not in SUPPORTED_IMAGE_SUFFIXES:
                raise ValueError("Save the texture as PNG, JPEG, BMP, TIFF, Targa, or WebP first")
            if image.is_dirty:
                image.save()
            if not path.is_file():
                raise ValueError("Save the image to disk first")
            edit_texture_externally(context, image, path)
        except Exception as error:
            diagnostics.record_exception("External texture edit failed", error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        return {"FINISHED"}


class WEBTOON_OT_reload_texture(Operator):
    bl_idname = "webtoon.reload_texture"
    bl_label = "Reload Texture"
    bl_description = "Reload the texture from disk after editing externally; discard unsaved Blender image edits"

    @classmethod
    def poll(cls, context):
        return context.scene is not None and _context_image(context) is not None

    def invoke(self, context, event):
        if _context_image(context).is_dirty:
            return context.window_manager.invoke_confirm(self, event)
        return self.execute(context)

    def execute(self, context):
        image = _context_image(context)
        try:
            if image.source != "FILE" or image.packed_file:
                raise ValueError("Use an unpacked single-file texture image")
            if not image.filepath:
                raise ValueError("Save the image first or use Create Texture")
            path = Path(bpy.path.abspath(image.filepath, library=image.library))
            if not path.is_file():
                raise ValueError(f"Texture file not found: {path}")
            image.reload()
            # Reload invalidates lazily. Reading dimensions decodes once without
            # copying a full texture into a Python list or checking stale data.
            if not image.size[0] or not image.size[1]:
                raise ValueError("Blender could not read the texture image")
            image.update()
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    area.tag_redraw()
        except Exception as error:
            diagnostics.record_exception("Texture reload failed", error)
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        self.report({"INFO"}, f"Reloaded {path.name}")
        return {"FINISHED"}


class WEBTOON_PT_textures(Panel):
    bl_label = "Textures"
    bl_idname = "WEBTOON_PT_textures"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Comic Views"
    bl_order = 1

    def draw(self, context):
        settings = context.scene.webtoon_texture_settings
        layout = self.layout
        layout.prop(settings, "name_text")
        layout.prop(settings, "name_mode")
        layout.label(text=f"Filename: {texture_name(context, settings)}.png")
        layout.prop(settings, "use_custom_directory")
        layout.prop(settings, "include_uv_map")
        header, body = layout.panel("webtoon_texture_creation", default_closed=True)
        header.label(text="Texture Creation Settings")
        if body is not None:
            row = body.row(align=True)
            row.prop(settings, "width")
            row.prop(settings, "height")
            body.prop(settings, "generated_type")
            if settings.generated_type == "BLANK":
                body.prop(settings, "color")
            body.prop(settings, "alpha")
            body.label(text=f"{settings.width * settings.height / 1_000_000:.1f} megapixels")
        header, body = layout.panel("webtoon_texture_save", default_closed=True)
        header.label(text="Save File Settings")
        if body is not None:
            body.label(text="Format: PNG (lossless)")
            if settings.use_custom_directory:
                body.prop(settings, "directory")
            else:
                body.label(text="Directory: //tex/")
            body.operator("webtoon.texture_directory", icon="FILE_FOLDER")
        materials.draw_settings(layout, context)
        if not settings.use_custom_directory and not bpy.data.filepath:
            layout.label(text="Save the blend file or choose a directory", icon="INFO")
        layout.operator("webtoon.create_texture", icon="IMAGE_DATA")
        layout.separator()
        layout.prop(settings, "image")
        layout.operator("webtoon.edit_texture_externally", icon="EXPORT")
        layout.operator("webtoon.reload_texture", icon="FILE_REFRESH")


def draw_image_menu(self, _context):
    self.layout.separator()
    self.layout.operator("webtoon.edit_texture_externally", text="Edit in Webtoon Maker (with UV Map)")


CLASSES = (WebtoonTextureSettings, WEBTOON_OT_texture_directory,
           WEBTOON_OT_create_texture, WEBTOON_OT_edit_texture_externally,
           WEBTOON_OT_reload_texture, WEBTOON_PT_textures)
