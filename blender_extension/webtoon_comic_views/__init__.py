"""Webtoon Comic Views Blender 4.5 extension."""
from __future__ import annotations

import time
import uuid

import bpy
from bpy.app.handlers import persistent
from bpy.props import (
    BoolProperty, CollectionProperty, EnumProperty, FloatProperty, IntProperty,
    PointerProperty, StringProperty,
)
from bpy.types import AddonPreferences, Operator, Panel, PropertyGroup, UIList

from . import bridge, diagnostics, renderer, viewport
from .state import (
    ensure_uuid, migrate_legacy_presentation, parse_state, state_digest, state_json,
)


EXTENSION_VERSION = "0.5.1"


def _resolution_changed(item: object, _context: object) -> None:
    if viewport.resolution_assignment_active():
        return
    width, height = viewport.update_working_resolution(item)
    if item.state_json:
        item.updated_at = time.time()
        bridge.RUNTIME.mark_scene_dirty()
        viewport.tag_redraw()


def _view_name_changed(item: object, _context: object) -> None:
    if item.state_json:
        item.updated_at = time.time()
        bridge.RUNTIME.send_views(bpy.context.scene)


def _overlay_visibility_changed(_item: object, _context: object) -> None:
    viewport.tag_redraw()


_pending_selection_uuid = ""
_selection_timer_registered = False
_selection_update_suppressed = False


def _select_index(scene: bpy.types.Scene, view_uuid: str) -> None:
    global _selection_update_suppressed
    for index, view in enumerate(scene.webtoon_comic_views):
        if view.view_uuid == view_uuid:
            _selection_update_suppressed = True
            try:
                scene.webtoon_comic_settings.active_index = index
            finally:
                _selection_update_suppressed = False
            return


def _activate_selected_timer() -> None:
    global _pending_selection_uuid, _selection_timer_registered
    _selection_timer_registered = False
    view_uuid, _pending_selection_uuid = _pending_selection_uuid, ""
    scene = getattr(bpy.context, "scene", None)
    if scene is None or not view_uuid:
        return None
    settings = getattr(scene, "webtoon_comic_settings", None)
    if settings is None or settings.loaded_view_uuid == view_uuid:
        return None
    try:
        bpy.ops.webtoon.activate_comic_view(
            "INVOKE_DEFAULT", view_uuid=view_uuid,
        )
    except (AttributeError, RuntimeError):
        _select_index(scene, settings.loaded_view_uuid)
    return None


def _active_index_changed(item: object, context: object) -> None:
    global _pending_selection_uuid, _selection_timer_registered
    if _selection_update_suppressed:
        return
    scene = getattr(context, "scene", None) or getattr(bpy.context, "scene", None)
    if scene is None:
        return
    views = scene.webtoon_comic_views
    index = int(item.active_index)
    if not 0 <= index < len(views):
        return
    destination = views[index]
    if destination.view_uuid == item.loaded_view_uuid:
        return
    _pending_selection_uuid = destination.view_uuid
    if not _selection_timer_registered:
        _selection_timer_registered = True
        bpy.app.timers.register(_activate_selected_timer, first_interval=0.01)


class WebtoonComicView(PropertyGroup):
    view_uuid: StringProperty(name="UUID")
    name: StringProperty(
        name="Name", default="Comic View", update=_view_name_changed,
    )
    revision: IntProperty(name="Revision", min=0, default=0)
    width: IntProperty(
        name="Width", min=64, max=4096, default=1920,
        update=_resolution_changed,
    )
    height: IntProperty(
        name="Height", min=64, max=4096, default=1080,
    )
    published_width: IntProperty(name="Published Width", min=0, max=4096, default=0)
    published_height: IntProperty(name="Published Height", min=0, max=4096, default=0)
    frame_min_x: FloatProperty(default=0.0)
    frame_min_y: FloatProperty(default=0.0)
    frame_max_x: FloatProperty(default=1.0)
    frame_max_y: FloatProperty(default=1.0)
    state_json: StringProperty(name="State")
    state_hash: StringProperty(name="State Hash")
    previous_state_json: StringProperty(name="Previous State")
    previous_state_hash: StringProperty(name="Previous State Hash")
    thumbnail_image: StringProperty(name="Thumbnail Image")
    thumbnail_png: StringProperty(name="Thumbnail PNG")
    published_frame_path: StringProperty(name="Published Frame Path")
    timeline_frame: IntProperty(
        name="Timeline Frame", default=0, min=0,
        description="Extension-managed frame containing this Comic View snapshot",
    )
    bake_hash: StringProperty(name="Timeline Bake Hash", options={"HIDDEN"})
    is_dirty: BoolProperty(name="Dirty", default=False)
    created_at: FloatProperty(name="Created", default=0.0)
    updated_at: FloatProperty(name="Updated", default=0.0)


class WebtoonRegisteredProperty(PropertyGroup):
    owner_uuid: StringProperty()
    owner_type: StringProperty()
    owner_name: StringProperty()
    rna_path: StringProperty()
    property_id: StringProperty()
    label: StringProperty()


class WebtoonComicSettings(PropertyGroup):
    project_uuid: StringProperty(name="Project UUID")
    active_index: IntProperty(
        name="Active Comic View", default=-1, update=_active_index_changed,
    )
    registered_index: IntProperty(name="Registered Property", default=-1)
    loaded_view_uuid: StringProperty(name="Loaded Comic View")
    next_timeline_frame: IntProperty(
        name="Next Comic View Timeline Frame", default=0, min=0,
        options={"HIDDEN"},
    )
    show_stream_frame_overlay: BoolProperty(
        name="Show Stream Frame Overlay", default=True,
        update=_overlay_visibility_changed,
    )


class WebtoonComicPreferences(AddonPreferences):
    bl_idname = __package__

    port: IntProperty(name="Port", min=1024, max=65535, default=47837)
    token: StringProperty(name="Token", subtype="PASSWORD")
    always_hide_overlays: BoolProperty(
        name="Always Hide Overlays", default=False,
        description="Temporarily hide 3D View overlays while rendering",
    )
    # Retained so existing extension preferences continue to deserialize.
    # Retained for compatibility with settings saved by extension 0.2.x.
    max_fps: IntProperty(
        name="Maximum FPS", min=1, max=30, default=15, options={"HIDDEN"}
    )

    def draw(self, _context: object) -> None:
        layout = self.layout
        layout.label(text="Loopback bridge")
        layout.label(text="Host: 127.0.0.1")
        layout.prop(self, "port")
        layout.prop(self, "token")
        layout.prop(self, "always_hide_overlays")
        layout.label(text="Publishing: Render uses the latest saved state")


def _scene() -> bpy.types.Scene:
    return bpy.context.scene


def _settings(scene: bpy.types.Scene | None = None) -> WebtoonComicSettings:
    return (scene or _scene()).webtoon_comic_settings


def _views(scene: bpy.types.Scene | None = None) -> object:
    return (scene or _scene()).webtoon_comic_views


def _active_view(scene: bpy.types.Scene | None = None) -> object | None:
    scene = scene or _scene()
    index = int(_settings(scene).active_index)
    views = _views(scene)
    return views[index] if 0 <= index < len(views) else None


def _ensure_project_uuid(scene: bpy.types.Scene) -> None:
    settings = _settings(scene)
    try:
        settings.project_uuid = uuid.UUID(settings.project_uuid).hex
    except (ValueError, AttributeError):
        settings.project_uuid = uuid.uuid4().hex


def _report_warnings(operator: Operator, warnings: list[str]) -> None:
    if warnings:
        diagnostics.record(
            "WARNING", "Operation completed with warnings", count=len(warnings),
            warnings="; ".join(warnings),
        )
        operator.report({"WARNING"}, f"Applied with {len(warnings)} warning(s)")


def _operator_failed(operator: Operator, action: str, error: BaseException) -> None:
    message = f"{action}: {error}"
    bridge.RUNTIME.last_error = message
    diagnostics.record_exception(f"{action} failed", error)
    operator.report({"ERROR"}, message)


def _restore_view_presentation(view: object, stored_state: dict) -> None:
    viewport.set_frame_bounds(
        view, stored_state.get("stream_frame", viewport.DEFAULT_FRAME)
    )
    resolution = stored_state.get("output_resolution", [1920, 1080])
    if isinstance(resolution, list) and len(resolution) == 2:
        try:
            viewport.set_working_resolution(
                view, int(resolution[0]), int(resolution[1])
            )
        except (TypeError, ValueError):
            pass
    viewport.tag_redraw()


class WEBTOON_OT_new_comic_view(Operator):
    bl_idname = "webtoon.new_comic_view"
    bl_label = "New Comic View"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        _ensure_project_uuid(scene)
        views = _views(scene)
        view = views.add()
        view.view_uuid = uuid.uuid4().hex
        view.name = f"Comic View {len(views)}"
        width = max(64, int(scene.render.resolution_x))
        height = max(64, int(scene.render.resolution_y))
        if width * height > renderer.MAX_PIXELS:
            ratio = (renderer.MAX_PIXELS / (width * height)) ** 0.5
            width, height = int(width * ratio), int(height * ratio)
        viewport.set_working_resolution(
            view, min(4096, width), min(4096, height)
        )
        view.created_at = time.time()
        view.updated_at = view.created_at
        _settings(scene).active_index = len(views) - 1
        viewport.set_frame_bounds(view, viewport.default_frame(scene))
        viewport.update_working_resolution(view)
        try:
            warnings = bridge.RUNTIME.save_view_state(scene, view)
            warnings.extend(bridge.RUNTIME.render_saved_view(scene, view))
            _settings(scene).loaded_view_uuid = view.view_uuid
        except Exception as error:
            views.remove(len(views) - 1)
            _operator_failed(self, "Create Comic View", error)
            return {"CANCELLED"}
        _report_warnings(self, warnings)
        return {"FINISHED"}


class WEBTOON_OT_duplicate_comic_view(Operator):
    bl_idname = "webtoon.duplicate_comic_view"
    bl_label = "Duplicate Comic View"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        source = _active_view(context.scene)
        if source is None:
            return {"CANCELLED"}
        target = _views(context.scene).add()
        target.view_uuid = uuid.uuid4().hex
        target.name = f"{source.name} Copy"
        target.revision = source.revision
        viewport.set_working_resolution(target, source.width, source.height)
        target.published_width = source.published_width
        target.published_height = source.published_height
        viewport.set_frame_bounds(target, viewport.frame_bounds(source))
        target.state_json, target.state_hash = source.state_json, source.state_hash
        target.previous_state_json = ""
        target.previous_state_hash = ""
        target.thumbnail_image = source.thumbnail_image
        target.thumbnail_png = source.thumbnail_png
        target.created_at = target.updated_at = time.time()
        target.is_dirty = False
        _settings(context.scene).active_index = len(_views(context.scene)) - 1
        try:
            bridge.RUNTIME.load_view_state(context.scene, target)
            bridge.RUNTIME.duplicate_published_frame(
                context.scene, source, target
            )
        except (OSError, RuntimeError, ValueError) as error:
            if not target.timeline_frame:
                _views(context.scene).remove(len(_views(context.scene)) - 1)
                _select_index(context.scene, source.view_uuid)
                _operator_failed(self, "Duplicate Comic View", error)
                return {"CANCELLED"}
            target.published_frame_path = ""
        bridge.RUNTIME.send_views(context.scene)
        return {"FINISHED"}


class WEBTOON_OT_save_comic_view(Operator):
    bl_idname = "webtoon.save_comic_view"
    bl_label = "Save Comic View"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        view = _active_view(context.scene)
        if view is None:
            return {"CANCELLED"}
        try:
            warnings = bridge.RUNTIME.save_view_state(context.scene, view)
        except Exception as error:
            _operator_failed(self, "Save Comic View", error)
            return {"CANCELLED"}
        _report_warnings(self, warnings)
        return {"FINISHED"}


class WEBTOON_OT_load_comic_view(Operator):
    bl_idname = "webtoon.load_comic_view"
    bl_label = "Load Comic View"
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context: bpy.types.Context) -> set[str]:
        view = _active_view(context.scene)
        if view is None or not view.state_json:
            return {"CANCELLED"}
        try:
            warnings = bridge.RUNTIME.load_view_state(context.scene, view)
        except Exception as error:
            _operator_failed(self, "Load Comic View", error)
            return {"CANCELLED"}
        _settings(context.scene).loaded_view_uuid = view.view_uuid
        _report_warnings(self, warnings)
        return {"FINISHED"}


def _render_active_view(
    operator: Operator, context: bpy.types.Context,
) -> set[str]:
    view = _active_view(context.scene)
    if view is None or not view.state_json:
        diagnostics.record(
            "WARNING", "Render canceled because the active Comic View has no saved state",
        )
        operator.report({"ERROR"}, "Save the Comic View before rendering")
        return {"CANCELLED"}
    try:
        warnings = bridge.RUNTIME.render_saved_view(context.scene, view)
    except Exception as error:
        _operator_failed(operator, "Render Comic View", error)
        return {"CANCELLED"}
    _report_warnings(operator, warnings)
    operator.report({"INFO"}, f"Published revision {view.revision}")
    return {"FINISHED"}


class WEBTOON_OT_render_comic_view(Operator):
    bl_idname = "webtoon.render_comic_view"
    bl_label = "Render Saved Comic View"

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _render_active_view(self, context)


class WEBTOON_OT_update_comic_view(Operator):
    """Compatibility alias retained for scripts using the old operator."""

    bl_idname = "webtoon.update_comic_view"
    bl_label = "Render Saved Comic View"

    def execute(self, context: bpy.types.Context) -> set[str]:
        return _render_active_view(self, context)


class WEBTOON_OT_delete_comic_view(Operator):
    bl_idname = "webtoon.delete_comic_view"
    bl_label = "Delete Comic View"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        return _active_view(context.scene) is not None

    def execute(self, context: bpy.types.Context) -> set[str]:
        scene = context.scene
        index = int(_settings(scene).active_index)
        view = _active_view(scene)
        if view is None:
            return {"CANCELLED"}
        deleted_view_uuid = view.view_uuid
        thumbnail = view.thumbnail_image
        _views(scene).remove(index)
        _settings(scene).active_index = min(index, len(_views(scene)) - 1)
        if not _views(scene):
            _settings(scene).loaded_view_uuid = ""
        if thumbnail and not any(item.thumbnail_image == thumbnail for item in _views(scene)):
            image = bpy.data.images.get(thumbnail)
            if image is not None:
                bpy.data.images.remove(image)
        bridge.RUNTIME.delete_published_frames(scene, deleted_view_uuid)
        bridge.RUNTIME.send_views(scene)
        return {"FINISHED"}


class WEBTOON_OT_revert_comic_view(Operator):
    bl_idname = "webtoon.revert_comic_view"
    bl_label = "Revert Comic View"
    bl_options = {"REGISTER", "UNDO"}

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        view = _active_view(context.scene)
        return bool(view is not None and view.previous_state_json)

    def execute(self, context: bpy.types.Context) -> set[str]:
        view = _active_view(context.scene)
        if view is None:
            return {"CANCELLED"}
        try:
            warnings = bridge.RUNTIME.revert_view_state(context.scene, view)
        except Exception as error:
            _operator_failed(self, "Revert Comic View", error)
            return {"CANCELLED"}
        _report_warnings(self, warnings)
        return {"FINISHED"}


class WEBTOON_OT_set_stream_frame(Operator):
    bl_idname = "webtoon.set_stream_frame"
    bl_label = "Set Stream Frame"
    bl_options = {"BLOCKING"}

    def invoke(self, context: bpy.types.Context, _event: object) -> set[str]:
        view = _active_view(context.scene)
        if view is None or context.area is None or context.area.type != "VIEW_3D":
            return {"CANCELLED"}
        viewport.bind(context)
        if not viewport.camera_view_active(context.scene):
            self.report({"WARNING"}, "Enter Camera View to edit the Stream Frame")
            return {"CANCELLED"}
        viewport.set_frame_edit_active(True)
        self._view_uuid = view.view_uuid
        self._original = viewport.frame_bounds(view)
        self._start = None
        context.window.cursor_modal_set("CROSSHAIR")
        context.window_manager.modal_handler_add(self)
        return {"RUNNING_MODAL"}

    def _window_point(self, context: bpy.types.Context, event: object):
        _window, _area, _space, region = viewport.find_view3d()
        if region is None:
            return None
        x = max(0.0, min(float(region.width), float(event.mouse_x - region.x)))
        y = max(0.0, min(float(region.height), float(event.mouse_y - region.y)))
        return x, y, region

    def _finish(self, context: bpy.types.Context) -> None:
        viewport.set_frame_edit_active(False)
        context.window.cursor_modal_restore()
        viewport.tag_redraw()

    def modal(self, context: bpy.types.Context, event: object) -> set[str]:
        view = bridge.RUNTIME._view(context.scene, self._view_uuid)
        if view is None:
            self._finish(context)
            return {"CANCELLED"}
        if event.type in {"ESC", "RIGHTMOUSE"}:
            viewport.set_frame_bounds(view, self._original)
            viewport.update_working_resolution(view)
            self._finish(context)
            return {"CANCELLED"}
        point = self._window_point(context, event)
        if point is None:
            self._finish(context)
            return {"CANCELLED"}
        x, y, _region = point
        if event.type == "LEFTMOUSE" and event.value == "PRESS":
            self._start = (x, y)
            return {"RUNNING_MODAL"}
        if self._start is not None and event.type == "MOUSEMOVE":
            sx, sy = self._start
            if abs(x - sx) >= viewport.MIN_FRAME_PIXELS and abs(y - sy) >= viewport.MIN_FRAME_PIXELS:
                bounds = viewport.screen_to_camera_bounds(
                    context.scene,
                    (min(sx, x), min(sy, y), max(sx, x), max(sy, y)),
                )
                if bounds is not None:
                    viewport.set_frame_bounds(view, bounds)
                viewport.update_working_resolution(view)
                viewport.tag_redraw()
            return {"RUNNING_MODAL"}
        if event.type == "LEFTMOUSE" and event.value == "RELEASE" and self._start is not None:
            sx, sy = self._start
            if abs(x - sx) < viewport.MIN_FRAME_PIXELS or abs(y - sy) < viewport.MIN_FRAME_PIXELS:
                viewport.set_frame_bounds(view, self._original)
                self._finish(context)
                return {"CANCELLED"}
            bounds = viewport.screen_to_camera_bounds(
                context.scene,
                (min(sx, x), min(sy, y), max(sx, x), max(sy, y)),
            )
            if bounds is None:
                viewport.set_frame_bounds(view, self._original)
                self._finish(context)
                return {"CANCELLED"}
            viewport.set_frame_bounds(view, bounds)
            viewport.update_working_resolution(view)
            view.updated_at = time.time()
            bridge.RUNTIME.mark_scene_dirty()
            self._finish(context)
            return {"FINISHED"}
        return {"RUNNING_MODAL"}


class WEBTOON_OT_activate_comic_view(Operator):
    bl_idname = "webtoon.activate_comic_view"
    bl_label = "Activate Comic View"
    bl_options = {"REGISTER", "UNDO"}

    view_uuid: StringProperty()
    resolution: EnumProperty(items=(
        ("SAVE", "Save and Switch", "Save the current changes before switching"),
        ("DISCARD", "Discard and Switch", "Discard current changes and switch"),
        ("CANCEL", "Cancel", "Keep the current view"),
    ), default="SAVE")

    def invoke(self, context: bpy.types.Context, _event: object) -> set[str]:
        current = bridge.RUNTIME._active_view(context.scene)
        destination = bridge.RUNTIME._view(context.scene, self.view_uuid)
        if current is not None and destination != current and current.is_dirty:
            return context.window_manager.invoke_props_dialog(self)
        return self.execute(context)

    def cancel(self, context: bpy.types.Context) -> None:
        current = bridge.RUNTIME._active_view(context.scene)
        if current is not None:
            _select_index(context.scene, current.view_uuid)

    def execute(self, context: bpy.types.Context) -> set[str]:
        current = bridge.RUNTIME._active_view(context.scene)
        destination = bridge.RUNTIME._view(context.scene, self.view_uuid)
        if destination is None:
            self.report({"ERROR"}, "Comic View not found")
            return {"CANCELLED"}
        if current == destination:
            _select_index(context.scene, destination.view_uuid)
            viewport.bind(context)
            viewport.tag_redraw()
            return {"FINISHED"}
        if current is not None and current != destination and current.is_dirty:
            if self.resolution == "CANCEL":
                _select_index(context.scene, current.view_uuid)
                return {"CANCELLED"}
            if self.resolution == "SAVE":
                try:
                    bridge.RUNTIME.save_view_state(context.scene, current)
                except Exception as error:
                    _operator_failed(self, "Save Comic View before switching", error)
                    _select_index(context.scene, current.view_uuid)
                    return {"CANCELLED"}
        try:
            bridge.RUNTIME._activate(context.scene, destination)
        except Exception as error:
            _operator_failed(self, "Activate Comic View", error)
            if current is not None:
                _select_index(context.scene, current.view_uuid)
            return {"CANCELLED"}
        return {"FINISHED"}


class WEBTOON_OT_start_bridge(Operator):
    bl_idname = "webtoon.start_bridge"
    bl_label = "Start Comic View Bridge"

    def execute(self, _context: bpy.types.Context) -> set[str]:
        preferences = bridge.addon_preferences()
        if preferences is None:
            return {"CANCELLED"}
        if not preferences.token:
            preferences.token = uuid.uuid4().hex
        try:
            bridge.RUNTIME.start_server(preferences.port, preferences.token)
        except OSError as error:
            _operator_failed(self, "Start bridge", error)
            return {"CANCELLED"}
        return {"FINISHED"}


class WEBTOON_OT_stop_bridge(Operator):
    bl_idname = "webtoon.stop_bridge"
    bl_label = "Stop Comic View Bridge"

    def execute(self, _context: bpy.types.Context) -> set[str]:
        bridge.RUNTIME.stop_server()
        return {"FINISHED"}


class WEBTOON_OT_copy_logs(Operator):
    bl_idname = "webtoon.copy_logs"
    bl_label = "Copy Webtoon Comic Views Logs"
    bl_description = "Copy extension diagnostics and recent errors to the clipboard"

    def execute(self, context: bpy.types.Context) -> set[str]:
        report = diagnostics.build_report(
            context,
            bridge.RUNTIME,
            extension_version=EXTENSION_VERSION,
            protocol_version=bridge.PROTOCOL_VERSION,
        )
        context.window_manager.clipboard = report
        diagnostics.record("INFO", "Diagnostic report copied to clipboard")
        self.report({"INFO"}, "Webtoon Comic Views logs copied")
        return {"FINISHED"}


class WEBTOON_OT_include_property(Operator):
    bl_idname = "webtoon.include_property"
    bl_label = "Include in Comic Views"

    @classmethod
    def poll(cls, context: bpy.types.Context) -> bool:
        pointer = getattr(context, "button_pointer", None)
        prop = getattr(context, "button_prop", None)
        return pointer is not None and prop is not None and prop.type in {
            "BOOLEAN", "INT", "FLOAT", "ENUM", "STRING",
        }

    def execute(self, context: bpy.types.Context) -> set[str]:
        pointer = context.button_pointer
        prop = context.button_prop
        owner = getattr(pointer, "id_data", None)
        if owner is None or owner.bl_rna.identifier in {
            "Mesh", "Curve", "Curves", "Lattice", "GreasePencil",
        }:
            self.report({"ERROR"}, "Geometry properties cannot be captured")
            return {"CANCELLED"}
        if getattr(prop, "is_array", False) and int(prop.array_length) > 32:
            self.report({"ERROR"}, "Arrays longer than 32 values are not supported")
            return {"CANCELLED"}
        try:
            value = getattr(pointer, prop.identifier)
            if prop.type != "ENUM" and hasattr(value, "__len__") \
                    and not isinstance(value, str) and len(value) > 32:
                raise ValueError("The property value is too large")
            rna_path = pointer.path_from_id()
        except (AttributeError, TypeError, ValueError) as error:
            self.report({"ERROR"}, str(error))
            return {"CANCELLED"}
        owner_uuid = ensure_uuid(owner)
        entries = context.scene.webtoon_comic_registered
        if any(
            item.owner_uuid == owner_uuid and item.rna_path == rna_path
            and item.property_id == prop.identifier
            for item in entries
        ):
            self.report({"INFO"}, "This property is already included")
            return {"CANCELLED"}
        item = entries.add()
        item.owner_uuid = owner_uuid
        item.owner_type = owner.bl_rna.identifier
        item.owner_name = getattr(owner, "name", "")
        item.rna_path = rna_path
        item.property_id = prop.identifier
        item.label = prop.name
        bridge.RUNTIME.mark_scene_dirty()
        self.report({"INFO"}, f"Included {prop.name} in Comic Views")
        return {"FINISHED"}


class WEBTOON_OT_remove_registered_property(Operator):
    bl_idname = "webtoon.remove_registered_property"
    bl_label = "Remove Registered Comic View Property"

    def execute(self, context: bpy.types.Context) -> set[str]:
        settings = _settings(context.scene)
        entries = context.scene.webtoon_comic_registered
        index = int(settings.registered_index)
        if not 0 <= index < len(entries):
            return {"CANCELLED"}
        entries.remove(index)
        settings.registered_index = min(index, len(entries) - 1)
        bridge.RUNTIME.mark_scene_dirty()
        return {"FINISHED"}


class WEBTOON_UL_comic_views(UIList):
    def draw_item(
        self, _context: object, layout: object, _data: object, item: object,
        _icon: object, _active_data: object, _active_propname: object,
        _index: object,
    ) -> None:
        row = layout.row(align=True)
        image = bpy.data.images.get(item.thumbnail_image)
        if image is not None:
            image.preview_ensure()
            row.label(text="", icon_value=image.preview.icon_id)
        row.prop(item, "name", text="", emboss=False)
        row.label(text=f"r{item.revision}")
        if item.is_dirty:
            row.label(text="", icon="ERROR")


class WEBTOON_UL_registered_properties(UIList):
    def draw_item(
        self, _context: object, layout: object, _data: object, item: object,
        _icon: object, _active_data: object, _active_propname: object,
        _index: object,
    ) -> None:
        layout.label(text=f"{item.owner_name}: {item.label}")


class WEBTOON_PT_comic_views(Panel):
    bl_label = "Comic Views"
    bl_idname = "WEBTOON_PT_comic_views"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "Comic Views"

    def draw(self, context: bpy.types.Context) -> None:
        scene = context.scene
        viewport.bind(context)
        settings = _settings(scene)
        layout = self.layout
        bridge_box = layout.box()
        preferences = bridge.addon_preferences()
        status = (
            "Connected" if bridge.RUNTIME.connected else
            "Listening" if bridge.RUNTIME.server.running else "Stopped"
        )
        bridge_box.label(text=f"Bridge: {status}")
        bridge_box.label(text="Host: 127.0.0.1")
        if preferences is not None:
            bridge_box.prop(preferences, "port")
            bridge_box.prop(preferences, "token")
            bridge_box.prop(preferences, "always_hide_overlays")
            bridge_box.label(text="Publishing: Render uses the latest saved state")
        row = bridge_box.row(align=True)
        row.operator("webtoon.start_bridge", text="Start / Restart")
        row.operator("webtoon.stop_bridge", text="Stop")
        bridge_box.operator("webtoon.copy_logs", text="Copy Logs")
        if bridge.RUNTIME.last_error:
            bridge_box.label(text=bridge.RUNTIME.last_error, icon="ERROR")

        layout.template_list(
            "WEBTOON_UL_comic_views", "", scene, "webtoon_comic_views",
            settings, "active_index", rows=4,
        )
        row = layout.row(align=True)
        row.operator("webtoon.new_comic_view", text="New", icon="ADD")
        row.operator("webtoon.duplicate_comic_view", text="Duplicate")
        row.operator("webtoon.delete_comic_view", text="Delete", icon="TRASH")
        view = _active_view(scene)
        if view is not None:
            image = bpy.data.images.get(view.thumbnail_image)
            if image is not None:
                image.preview_ensure()
                layout.template_icon(icon_value=image.preview.icon_id, scale=6.0)
            layout.prop(view, "name")
            row = layout.row(align=True)
            row.prop(view, "width")
            row.label(text=f"Height {view.height}")
            overlay_label = (
                "Hide Stream Frame Overlay"
                if settings.show_stream_frame_overlay
                else "Show Stream Frame Overlay"
            )
            layout.prop(
                settings, "show_stream_frame_overlay",
                text=overlay_label, toggle=True,
            )
            frame_row = layout.row()
            frame_row.enabled = viewport.camera_view_active(scene)
            frame_row.operator("webtoon.set_stream_frame", text="Set Stream Frame")
            row = layout.row(align=True)
            row.operator("webtoon.save_comic_view", text="Save")
            row.operator("webtoon.load_comic_view", text="Load")
            revert_row = row.row(align=True)
            revert_row.enabled = bool(view.previous_state_json)
            revert_row.operator("webtoon.revert_comic_view", text="Revert")
            layout.operator("webtoon.render_comic_view", text="Render")
            layout.label(text=f"Revision {view.revision}")
            layout.label(text=(
                f"Timeline frame {view.timeline_frame}"
                if view.timeline_frame > 0 else
                "Timeline frame will be assigned on first use"
            ))
            layout.label(text=(
                "Bake status: Ready"
                if view.timeline_frame > 0 and view.bake_hash else
                "Bake status: Pending"
            ))
            if view.is_dirty:
                layout.label(text="Stored view has unsaved scene changes", icon="ERROR")

        layout.separator()
        layout.label(text="Extra captured properties")
        layout.template_list(
            "WEBTOON_UL_registered_properties", "", scene,
            "webtoon_comic_registered", settings, "registered_index", rows=2,
        )
        layout.operator("webtoon.remove_registered_property", icon="REMOVE")


def _draw_button_context(self: object, context: bpy.types.Context) -> None:
    if WEBTOON_OT_include_property.poll(context):
        self.layout.separator()
        self.layout.operator("webtoon.include_property")


@persistent
def _depsgraph_updated(_scene: object, _depsgraph: object) -> None:
    bridge.RUNTIME.mark_scene_dirty()


@persistent
def _load_post(_unused: object) -> None:
    _initialize_scenes()


CLASSES = (
    WebtoonComicView,
    WebtoonRegisteredProperty,
    WebtoonComicSettings,
    WebtoonComicPreferences,
    WEBTOON_OT_new_comic_view,
    WEBTOON_OT_duplicate_comic_view,
    WEBTOON_OT_save_comic_view,
    WEBTOON_OT_load_comic_view,
    WEBTOON_OT_render_comic_view,
    WEBTOON_OT_update_comic_view,
    WEBTOON_OT_delete_comic_view,
    WEBTOON_OT_revert_comic_view,
    WEBTOON_OT_set_stream_frame,
    WEBTOON_OT_activate_comic_view,
    WEBTOON_OT_start_bridge,
    WEBTOON_OT_stop_bridge,
    WEBTOON_OT_copy_logs,
    WEBTOON_OT_include_property,
    WEBTOON_OT_remove_registered_property,
    WEBTOON_UL_comic_views,
    WEBTOON_UL_registered_properties,
    WEBTOON_PT_comic_views,
)


def _initialize_scenes() -> bool:
    """Initialize scene data only after Blender leaves _RestrictData mode."""
    scenes = getattr(bpy.data, "scenes", None)
    if scenes is None:
        return False
    for scene in scenes:
        _ensure_project_uuid(scene)
        for view in scene.webtoon_comic_views:
            if not view.published_width:
                view.published_width = max(64, int(view.width))
            if not view.published_height:
                view.published_height = max(64, int(view.height))
            if view.state_json:
                try:
                    stored = parse_state(
                        view.state_json,
                        fallback_stream_frame=viewport.default_frame(scene),
                        fallback_resolution=(int(view.width), int(view.height)),
                    )
                    if stored.get("stream_frame_space") == "viewport_legacy":
                        stored, warning = migrate_legacy_presentation(scene, stored)
                        if warning:
                            print(f"Webtoon Comic Views: {warning}")
                    view.state_json = state_json(stored)
                    view.state_hash = state_digest(stored)
                    _restore_view_presentation(view, stored)
                except (TypeError, ValueError):
                    pass
        settings = scene.webtoon_comic_settings
        if not settings.loaded_view_uuid:
            index = int(settings.active_index)
            if 0 <= index < len(scene.webtoon_comic_views):
                settings.loaded_view_uuid = scene.webtoon_comic_views[index].view_uuid
    active_scene = getattr(bpy.context, "scene", None)
    active = _active_view(active_scene) if active_scene is not None else None
    bridge.RUNTIME.scene_matches_snapshot = bool(
        active is not None and active.state_json and not active.is_dirty
    )
    bridge.RUNTIME.ignore_updates_until = time.monotonic() + 0.3
    migration_pending = any(
        view.state_json and (not view.timeline_frame or not view.bake_hash)
        for view in getattr(active_scene, "webtoon_comic_views", ())
    ) if active_scene is not None else False
    if active is not None and active.state_json and migration_pending:
        try:
            bridge.RUNTIME.load_view_state(active_scene, active)
        except Exception as error:
            bridge.RUNTIME.last_error = f"Timeline migration failed: {error}"
            diagnostics.record_exception("Timeline migration failed", error)
    return True


def _start_after_register() -> float | None:
    if not _initialize_scenes():
        return 0.2
    preferences = bridge.addon_preferences()
    if preferences is None:
        return 0.2
    if not preferences.token:
        preferences.token = uuid.uuid4().hex
    try:
        bridge.RUNTIME.start_server(preferences.port, preferences.token)
    except OSError as error:
        bridge.RUNTIME.last_error = str(error)
        diagnostics.record_exception("Automatic bridge start failed", error)
    return None


def register() -> None:
    for cls in CLASSES:
        bpy.utils.register_class(cls)
    bpy.types.Scene.webtoon_comic_views = CollectionProperty(type=WebtoonComicView)
    bpy.types.Scene.webtoon_comic_registered = CollectionProperty(
        type=WebtoonRegisteredProperty
    )
    bpy.types.Scene.webtoon_comic_settings = PointerProperty(type=WebtoonComicSettings)
    if _depsgraph_updated not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_depsgraph_updated)
    if _load_post not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_load_post)
    menu = getattr(bpy.types, "WM_MT_button_context", None)
    if menu is not None:
        menu.append(_draw_button_context)
    if not bpy.app.timers.is_registered(bridge.RUNTIME.tick):
        bpy.app.timers.register(bridge.RUNTIME.tick, first_interval=0.1, persistent=True)
    if not bpy.app.timers.is_registered(_start_after_register):
        bpy.app.timers.register(_start_after_register, first_interval=0.2)
    viewport.register_overlay()
    diagnostics.record(
        "INFO", "Extension registered", version=EXTENSION_VERSION,
        protocol=bridge.PROTOCOL_VERSION,
    )


def unregister() -> None:
    global _selection_timer_registered, _pending_selection_uuid
    diagnostics.record("INFO", "Extension unregistering", version=EXTENSION_VERSION)
    viewport.unregister_overlay()
    bridge.RUNTIME.stop_server()
    if bpy.app.timers.is_registered(_activate_selected_timer):
        bpy.app.timers.unregister(_activate_selected_timer)
    _selection_timer_registered = False
    _pending_selection_uuid = ""
    if bpy.app.timers.is_registered(_start_after_register):
        bpy.app.timers.unregister(_start_after_register)
    if bpy.app.timers.is_registered(bridge.RUNTIME.tick):
        bpy.app.timers.unregister(bridge.RUNTIME.tick)
    menu = getattr(bpy.types, "WM_MT_button_context", None)
    if menu is not None:
        try:
            menu.remove(_draw_button_context)
        except RuntimeError:
            pass
    for handler, collection in (
        (_depsgraph_updated, bpy.app.handlers.depsgraph_update_post),
        (_load_post, bpy.app.handlers.load_post),
    ):
        if handler in collection:
            collection.remove(handler)
    del bpy.types.Scene.webtoon_comic_settings
    del bpy.types.Scene.webtoon_comic_registered
    del bpy.types.Scene.webtoon_comic_views
    for cls in reversed(CLASSES):
        bpy.utils.unregister_class(cls)
