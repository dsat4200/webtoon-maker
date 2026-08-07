"""Webtoon Maker comic-frame synchronization add-on for Blender 4.5.5 LTS."""

# Do not enable postponed annotations in this file. Blender discovers RNA
# properties from evaluated class annotations such as ``name: StringProperty``.

bl_info = {
    "name": "Webtoon Maker Comic Frame Sync",
    "author": "Webtoon Maker contributors",
    "version": (1, 1, 0),
    "blender": (4, 5, 5),
    "location": "3D Viewport > Sidebar > Webtoon",
    "description": "Publish Blender scene state to linked Webtoon Maker comic frames",
    "category": "3D View",
}

import json
from pathlib import Path
import tempfile
from typing import Any, Iterable, Mapping

try:
    import bpy  # type: ignore
    from bpy.props import (  # type: ignore
        BoolProperty,
        CollectionProperty,
        EnumProperty,
        IntProperty,
        PointerProperty,
        StringProperty,
    )
    from bpy.types import Operator, Panel, PropertyGroup, UIList  # type: ignore
except ImportError:  # pragma: no cover - helper package is importable in pytest.
    bpy = None

from .capture import capture_sync_data
from .geometry import UnsupportedSceneError, export_geometry_staging
from .identities import (
    IdentityRegistryError,
    ensure_blender_identities,
    fork_file_identity,
    identity_for,
    load_registry,
    save_registry,
    update_binding,
)
from .preview import (
    apply_preview,
    load_comic_frame_overrides,
    preview_active,
    restore_preview,
)
from .registration import (
    RegistrationError,
    load_frame_collection_ids,
    parse_registration_json,
)
from .transport import notify_webtoon
from .wire import stage_ready_bundle


def _iter_scene_collections(scene: Any) -> Iterable[tuple[Any, int]]:
    def visit(collection: Any, depth: int) -> Iterable[tuple[Any, int]]:
        for child in collection.children:
            yield child, depth
            yield from visit(child, depth + 1)

    return visit(scene.collection, 0)


def _refresh_collection_choices(
    scene: Any, *, included_override: set[str] | None = None,
) -> None:
    settings = scene.webtoon_sync
    previous = {item.collection_uuid: bool(item.included) for item in settings.collections}
    registered: set[str] = set()
    if included_override is None and settings.chapter_root and settings.comic_frame_id:
        try:
            registered = set(load_frame_collection_ids(
                settings.chapter_root, settings.comic_frame_id,
            ))
        except (OSError, RegistrationError, ValueError):
            registered = set()
    settings.collections.clear()
    for collection, depth in _iter_scene_collections(scene):
        collection_uuid = identity_for(collection)
        if collection_uuid is None:
            continue
        item = settings.collections.add()
        item.collection_uuid = collection_uuid
        item.collection_name = collection.name
        item.depth = depth
        # Collections discovered after a frame was created are intentionally
        # hidden until the user explicitly includes them.
        if included_override is not None:
            item.included = collection_uuid in included_override
        else:
            item.included = previous.get(
                collection_uuid, collection_uuid in registered,
            )


def _included_collection_ids(scene: Any) -> tuple[str, ...]:
    return tuple(item.collection_uuid for item in scene.webtoon_sync.collections if item.included)


def _participating_objects(scene: Any, included: set[str]) -> tuple[Any, ...]:
    objects: dict[str, Any] = {}
    for collection, _depth in _iter_scene_collections(scene):
        if identity_for(collection) not in included:
            continue
        for obj in collection.all_objects:
            object_id = identity_for(obj)
            if object_id is not None:
                objects[object_id] = obj
    return tuple(objects.values())


def _load_base_state(chapter_root: str, frame_id: str) -> Mapping[str, Any] | None:
    try:
        root = Path(chapter_root).expanduser().resolve(strict=True)
        candidate = root / "blender" / "frames" / f"{frame_id}.json"
        if candidate.is_symlink():
            return None
        path = candidate.resolve(strict=True)
        path.relative_to(root)
        if path.stat().st_size > 16 * 1024 * 1024:
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(value, dict):
        return None
    source = value.get("source", {})
    base = value.get(
        "blender_base_state",
        source.get("state") if isinstance(source, dict) else value.get("captured_state"),
    )
    return base if isinstance(base, dict) else None


if bpy is not None:
    _FRAME_ENUM_CACHE: dict[int, list[tuple[str, str, str]]] = {}


    def _frame_enum_items(settings: Any, _context: Any) -> list[tuple[str, str, str]]:
        items = [
            (item.frame_id, item.frame_id, f"Use comic frame {item.frame_id}")
            for item in settings.frames if item.frame_id
        ]
        if not items:
            items = [("__none__", "No registered frames", "Import Webtoon registration first")]
        try:
            key = int(settings.as_pointer())
        except (AttributeError, TypeError, ValueError):
            key = id(settings)
        _FRAME_ENUM_CACHE[key] = items
        return _FRAME_ENUM_CACHE[key]


    def _apply_registered_frame(scene: Any) -> int:
        settings = scene.webtoon_sync
        frame_id = str(settings.comic_frame_choice)
        available = {item.frame_id for item in settings.frames}
        if frame_id not in available:
            raise RegistrationError("Choose a registered comic frame")
        included = set(load_frame_collection_ids(settings.chapter_root, frame_id))
        settings.comic_frame_id = frame_id
        _refresh_collection_choices(scene, included_override=included)
        return len(included)


    class WEBTOON_PG_collection_choice(PropertyGroup):
        collection_uuid: StringProperty(name="Collection UUID")
        collection_name: StringProperty(name="Collection")
        depth: IntProperty(default=0, min=0)
        included: BoolProperty(name="Include", default=False)


    class WEBTOON_PG_frame_choice(PropertyGroup):
        frame_id: StringProperty(name="Comic Frame ID")


    class WEBTOON_PG_sync_settings(PropertyGroup):
        endpoint: StringProperty(
            name="RPC Endpoint",
            description="Ephemeral endpoint shown by Webtoon Maker",
            default="",
            options={"SKIP_SAVE"},
        )
        auth_token: StringProperty(
            name="Auth Token",
            description="Ephemeral bearer token shown by Webtoon Maker",
            subtype="PASSWORD",
            default="",
            options={"SKIP_SAVE"},
        )
        chapter_root: StringProperty(
            name="Chapter Folder",
            description="Linked chapters/<chapter-id> folder",
            subtype="DIR_PATH",
            default="",
        )
        inbox_root: StringProperty(
            name="Sync Inbox",
            description="Linked chapter's blender/inbox folder",
            subtype="DIR_PATH",
            default="",
        )
        series_id: StringProperty(name="Series ID", default="")
        chapter_id: StringProperty(name="Chapter ID", default="")
        comic_frame_id: StringProperty(name="Comic Frame ID", default="")
        frames: CollectionProperty(type=WEBTOON_PG_frame_choice)
        comic_frame_choice: EnumProperty(
            name="Comic Frame",
            description="Registered Webtoon comic frame",
            items=_frame_enum_items,
        )
        base_revision: IntProperty(name="Webtoon Revision", default=0, min=0)
        collections: CollectionProperty(type=WEBTOON_PG_collection_choice)
        collection_index: IntProperty(default=0, min=0)
        last_sync_report: StringProperty(name="Last Sync", default="Not synced")


    class WEBTOON_UL_collection_choices(UIList):
        def draw_item(
            self, _context: Any, layout: Any, _data: Any, item: Any,
            _icon: int, _active_data: Any, _active_property: str, _index: int,
        ) -> None:
            row = layout.row(align=True)
            for _level in range(item.depth):
                row.label(text="", icon="BLANK1")
            row.prop(item, "included", text="")
            row.label(text=item.collection_name, icon="OUTLINER_COLLECTION")


    class WEBTOON_OT_import_registration(Operator):
        bl_idname = "webtoon.import_registration"
        bl_label = "Paste / Import Webtoon Registration"
        bl_description = (
            "Validate registration JSON from the clipboard and configure this session"
        )

        def execute(self, context: Any) -> set[str]:
            try:
                registration = parse_registration_json(
                    context.window_manager.clipboard
                )
                file_uuid = str(load_registry()["file_uuid"])
                if (
                    registration.blender_file_uuid
                    and registration.blender_file_uuid != file_uuid
                ):
                    raise RegistrationError(
                        "Registration is associated with a different Blender file"
                    )
                included = set(load_frame_collection_ids(
                    registration.chapter_root,
                    registration.selected_frame_id,
                ))
                settings = context.scene.webtoon_sync
                settings.endpoint = registration.endpoint
                settings.auth_token = registration.auth_token
                settings.chapter_root = str(registration.chapter_root)
                settings.inbox_root = str(registration.inbox_root)
                settings.series_id = registration.series_id
                settings.chapter_id = registration.chapter_id
                settings.base_revision = registration.base_revision
                settings.frames.clear()
                for frame_id in registration.comic_frame_ids:
                    item = settings.frames.add()
                    item.frame_id = frame_id
                settings.comic_frame_choice = registration.selected_frame_id
                settings.comic_frame_id = registration.selected_frame_id
                _refresh_collection_choices(
                    context.scene, included_override=included,
                )
                included_count = len(included)
            except (
                IdentityRegistryError, OSError, RegistrationError,
                RuntimeError, TypeError, ValueError,
            ) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            settings.last_sync_report = (
                f"Imported registration for {len(registration.comic_frame_ids)} "
                f"frame(s); selected {registration.selected_frame_id} with "
                f"{included_count} collection(s)"
            )
            self.report({"INFO"}, settings.last_sync_report)
            return {"FINISHED"}


    class WEBTOON_OT_apply_registered_frame(Operator):
        bl_idname = "webtoon.apply_registered_frame"
        bl_label = "Apply Selected Comic Frame"
        bl_description = "Use the selected Webtoon frame and restore its collection participation"

        def execute(self, context: Any) -> set[str]:
            settings = context.scene.webtoon_sync
            try:
                included_count = _apply_registered_frame(context.scene)
            except (OSError, RegistrationError, RuntimeError, ValueError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            settings.last_sync_report = (
                f"Selected {settings.comic_frame_id}; "
                f"{included_count} participating collection(s)"
            )
            self.report({"INFO"}, settings.last_sync_report)
            return {"FINISHED"}


    class WEBTOON_OT_refresh_collections(Operator):
        bl_idname = "webtoon.refresh_collections"
        bl_label = "Refresh Collections"
        bl_description = "Refresh the participating collection tree; new collections default hidden"

        def execute(self, context: Any) -> set[str]:
            try:
                report = ensure_blender_identities()
                if not report.can_publish:
                    raise ValueError("Resolve identity or linked-data errors before selecting collections")
                _refresh_collection_choices(context.scene)
            except (IdentityRegistryError, ValueError, RuntimeError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}


    class WEBTOON_OT_validate_ids(Operator):
        bl_idname = "webtoon.validate_ids"
        bl_label = "Validate / Repair IDs"
        bl_description = "Assign missing UUIDs and repair only duplicates with an unambiguous prior owner"
        bl_options = {"UNDO"}

        def execute(self, context: Any) -> set[str]:
            try:
                report = ensure_blender_identities()
                _refresh_collection_choices(context.scene)
            except (IdentityRegistryError, ValueError, RuntimeError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            if report.ambiguous:
                names = next(iter(report.ambiguous.values()))
                self.report({"ERROR"}, "Ambiguous copied UUID: " + ", ".join(names[:3]))
                return {"CANCELLED"}
            if report.linked:
                self.report({"ERROR"}, "Linked data is unsupported: " + ", ".join(report.linked[:3]))
                return {"CANCELLED"}
            self.report(
                {"INFO"},
                f"IDs valid; assigned {report.assigned_count}, repaired {report.repaired_duplicate_count}",
            )
            return {"FINISHED"}


    class WEBTOON_OT_fork_source_identity(Operator):
        bl_idname = "webtoon.fork_source_identity"
        bl_label = "Fork Source Identity"
        bl_description = "Give this .blend a new file identity and clear its chapter bindings"
        bl_options = {"UNDO"}

        def invoke(self, context: Any, event: Any) -> set[str]:
            return context.window_manager.invoke_confirm(self, event)

        def execute(self, _context: Any) -> set[str]:
            try:
                identity = fork_file_identity()
            except (IdentityRegistryError, RuntimeError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self.report({"INFO"}, f"Forked Blender source identity: {identity}")
            return {"FINISHED"}


    class WEBTOON_OT_apply_preview(Operator):
        bl_idname = "webtoon.apply_frame_preview"
        bl_label = "Apply Comic Frame Preview"
        bl_description = "Apply Webtoon presentation overrides to this session without keyframes or saving"

        def execute(self, context: Any) -> set[str]:
            settings = context.scene.webtoon_sync
            try:
                overrides = load_comic_frame_overrides(settings.chapter_root, settings.comic_frame_id)
                apply_preview(context.scene, overrides)
            except (OSError, ValueError, RuntimeError) as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            self.report({"INFO"}, "Comic frame preview applied; Blender has not been saved")
            return {"FINISHED"}


    class WEBTOON_OT_restore_preview(Operator):
        bl_idname = "webtoon.restore_frame_preview"
        bl_label = "Restore Blender State"
        bl_description = "Restore the in-memory state captured before preview"

        def execute(self, context: Any) -> set[str]:
            try:
                restored = restore_preview(context.scene)
            except RuntimeError as exc:
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            if not restored:
                self.report({"WARNING"}, "No Webtoon preview is active")
                return {"CANCELLED"}
            self.report({"INFO"}, "Blender state restored")
            return {"FINISHED"}


    class WEBTOON_OT_update_comic_frame(Operator):
        bl_idname = "webtoon.update_comic_frame"
        bl_label = "Update Comic Frame"
        bl_description = "Save Blender, publish one atomic frame bundle, and notify Webtoon Maker"

        save_before_sync: BoolProperty(
            name="Save and Sync",
            description="Save the named .blend before capturing its authoritative state",
            default=True,
        )

        def invoke(self, context: Any, _event: Any) -> set[str]:
            if not bpy.data.filepath:
                self.report({"ERROR"}, "Save this .blend with a filename before syncing")
                return {"CANCELLED"}
            if bpy.data.is_dirty:
                return context.window_manager.invoke_props_dialog(self)
            return self.execute(context)

        def draw(self, _context: Any) -> None:
            column = self.layout.column()
            column.label(text="Blender has unsaved source changes.", icon="ERROR")
            column.prop(self, "save_before_sync")

        def execute(self, context: Any) -> set[str]:
            settings = context.scene.webtoon_sync
            try:
                if not bpy.data.filepath:
                    raise ValueError("Save this .blend with a filename before syncing")
                if not settings.chapter_root or not Path(settings.chapter_root).expanduser().is_dir():
                    raise ValueError("Choose the existing linked chapter folder")
                if not settings.inbox_root:
                    raise ValueError("Choose the linked chapter's blender/inbox folder")
                if bpy.data.is_dirty and not self.save_before_sync:
                    raise ValueError("Update cancelled: choose Save and Sync for authoritative capture")
                if preview_active(context.scene):
                    restore_preview(context.scene)
                identity_report = ensure_blender_identities()
                if identity_report.ambiguous:
                    names = next(iter(identity_report.ambiguous.values()))
                    raise ValueError("Ambiguous copied UUIDs: " + ", ".join(names[:5]))
                if identity_report.linked:
                    raise ValueError("Linked-library data is unsupported: " + ", ".join(identity_report.linked[:5]))
                _refresh_collection_choices(context.scene)
                included = set(_included_collection_ids(context.scene))
                if not included:
                    raise ValueError("Select at least one participating collection")
                objects = _participating_objects(context.scene, included)
                if not objects:
                    raise ValueError("Participating collections contain no exportable objects")
                # Source revisions belong to the Blender file, independently
                # of Webtoon's chapter CAS revision. Persist the increment
                # before staging so queued bundles remain globally ordered.
                registry = load_registry()
                source_revision = int(registry.get("source_revision", 0)) + 1
                registry["source_revision"] = source_revision
                save_registry(registry)
                # Identity generation and the source watermark modify the
                # .blend. Save both before authoritative capture/publication.
                bpy.ops.wm.save_mainfile()
                base_state = _load_base_state(settings.chapter_root, settings.comic_frame_id)
                capture = capture_sync_data(
                    context,
                    included_collection_ids=included,
                    base_state=base_state,
                )
                with tempfile.TemporaryDirectory(prefix="webtoon-sync-") as temporary:
                    geometry = export_geometry_staging(context, objects, temporary)
                    cache_manifest = dict(geometry.cache_manifest)
                    cache_manifest["source_revision"] = source_revision
                    cache_manifest["freestyle_edges"] = {
                        object_id: {
                            "topology_hash": record.get("topology_sha256"),
                            "marked_edges": record.get("marked_edges", []),
                        }
                        for object_id, record in capture.chapter_data.get("freestyle", {}).items()
                    }
                    ready = stage_ready_bundle(
                        settings.inbox_root,
                        series_id=settings.series_id,
                        chapter_id=settings.chapter_id,
                        comic_frame_id=settings.comic_frame_id,
                        blender_file_uuid=identity_report.file_uuid,
                        base_revision=int(settings.base_revision),
                        source_revision=source_revision,
                        chapter_data=capture.chapter_data,
                        frame_data=capture.frame_data,
                        cache_manifest=cache_manifest,
                        source_files=geometry.source_files,
                        warnings=(*capture.warnings, *geometry.warnings, *identity_report.warnings),
                    )
                if settings.endpoint and settings.auth_token:
                    notification = notify_webtoon(
                        settings.endpoint,
                        settings.auth_token,
                        transaction_id=ready.transaction_id,
                        bundle_sha256=ready.bundle_sha256,
                    )
                else:
                    from .transport import NotifyResult
                    notification = NotifyResult(
                        "queued",
                        message="Bundle is queued on disk; no Webtoon endpoint/token is configured.",
                    )
                if notification.state == "accepted" and notification.receipt is not None:
                    accepted_revision = int(notification.receipt["accepted_revision"])
                    settings.base_revision = accepted_revision
                    registry = load_registry()
                    update_binding(
                        registry,
                        series_id=settings.series_id,
                        chapter_id=settings.chapter_id,
                        comic_frame_id=settings.comic_frame_id,
                        base_revision=accepted_revision,
                    )
                    save_registry(registry)
                    bpy.ops.wm.save_mainfile()
                    settings.last_sync_report = f"Accepted revision {accepted_revision}"
                    self.report({"INFO"}, settings.last_sync_report)
                elif notification.state == "conflicts":
                    settings.last_sync_report = "Webtoon reported presentation conflicts"
                    self.report({"WARNING"}, settings.last_sync_report)
                elif notification.state == "rejected":
                    settings.last_sync_report = "Webtoon rejected the bundle; it remains available for diagnosis"
                    self.report({"ERROR"}, settings.last_sync_report)
                else:
                    settings.last_sync_report = notification.message or "Queued, not yet accepted"
                    self.report({"WARNING"}, settings.last_sync_report)
            except (
                IdentityRegistryError, UnsupportedSceneError, ValueError,
                OSError, RuntimeError,
            ) as exc:
                settings.last_sync_report = str(exc)
                self.report({"ERROR"}, str(exc))
                return {"CANCELLED"}
            return {"FINISHED"}


    class WEBTOON_PT_sync(Panel):
        bl_label = "Webtoon Comic Frame"
        bl_idname = "WEBTOON_PT_sync"
        bl_space_type = "VIEW_3D"
        bl_region_type = "UI"
        bl_category = "Webtoon"

        def draw(self, context: Any) -> None:
            layout = self.layout
            settings = context.scene.webtoon_sync
            try:
                file_uuid = load_registry()["file_uuid"]
            except (IdentityRegistryError, RuntimeError):
                file_uuid = "Invalid registry"
            identity_box = layout.box()
            identity_box.label(text="Source Identity")
            identity_box.label(text=file_uuid, icon="FILE_BLEND")
            row = identity_box.row(align=True)
            row.operator("webtoon.validate_ids", icon="CHECKMARK")
            row.operator("webtoon.fork_source_identity", icon="DUPLICATE")

            connection = layout.box()
            connection.label(text="Webtoon Connection")
            connection.operator("webtoon.import_registration", icon="PASTEDOWN")
            connection.prop(settings, "endpoint")
            connection.prop(settings, "auth_token")
            connection.prop(settings, "chapter_root")
            connection.prop(settings, "inbox_root")
            connection.prop(settings, "series_id")
            connection.prop(settings, "chapter_id")
            frame_row = connection.row(align=True)
            frame_row.prop(settings, "comic_frame_choice", text="Comic Frame")
            frame_row.operator(
                "webtoon.apply_registered_frame", text="Apply", icon="CHECKMARK",
            )
            connection.prop(settings, "base_revision")
            try:
                source_revision = int(load_registry().get("source_revision", 0))
            except (IdentityRegistryError, RuntimeError, TypeError, ValueError):
                source_revision = 0
            connection.label(text=f"Blender source revision: {source_revision}")

            frame = layout.box()
            frame.label(text=f"Scene: {context.scene.name}", icon="SCENE_DATA")
            frame.label(text=f"Timeline frame: {context.scene.frame_current}")
            frame.template_list(
                "WEBTOON_UL_collection_choices", "",
                settings, "collections", settings, "collection_index", rows=5,
            )
            frame.operator("webtoon.refresh_collections", icon="FILE_REFRESH")

            preview = layout.row(align=True)
            preview.operator("webtoon.apply_frame_preview", icon="HIDE_OFF")
            preview.operator("webtoon.restore_frame_preview", icon="LOOP_BACK")
            layout.operator("webtoon.update_comic_frame", icon="EXPORT")
            layout.label(text=settings.last_sync_report, icon="INFO")


    _CLASSES = (
        WEBTOON_PG_collection_choice,
        WEBTOON_PG_frame_choice,
        WEBTOON_PG_sync_settings,
        WEBTOON_UL_collection_choices,
        WEBTOON_OT_import_registration,
        WEBTOON_OT_apply_registered_frame,
        WEBTOON_OT_refresh_collections,
        WEBTOON_OT_validate_ids,
        WEBTOON_OT_fork_source_identity,
        WEBTOON_OT_apply_preview,
        WEBTOON_OT_restore_preview,
        WEBTOON_OT_update_comic_frame,
        WEBTOON_PT_sync,
    )


    def register() -> None:
        for cls in _CLASSES:
            bpy.utils.register_class(cls)
        bpy.types.Scene.webtoon_sync = PointerProperty(type=WEBTOON_PG_sync_settings)


    def unregister() -> None:
        if hasattr(bpy.types.Scene, "webtoon_sync"):
            del bpy.types.Scene.webtoon_sync
        for cls in reversed(_CLASSES):
            bpy.utils.unregister_class(cls)
        _FRAME_ENUM_CACHE.clear()


else:
    def register() -> None:  # pragma: no cover
        raise RuntimeError("The Webtoon Sync add-on can only register inside Blender 4.5.5")


    def unregister() -> None:  # pragma: no cover
        return


__all__ = ["bl_info", "register", "unregister"]
