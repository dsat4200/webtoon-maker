"""Qt lifecycle and atomic publication for Blender comic-frame sync.

The protocol/server layer remains Qt-free.  This coordinator owns one active
chapter binding, marshals every in-memory read/write onto its Qt thread, writes
validated immutable GLB blobs off-thread, and delegates the single undoable UI
commit to a host callback.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
import threading
from typing import Any, Callable, Mapping

from PySide6.QtCore import QObject, QThread, Qt, Signal, Slot

from comic_editor.three_d.documents import (
    BlenderChapterDocument,
    CacheManifest,
    ComicFrameDocument,
)
from comic_editor.three_d.protocol import (
    ConflictDescriptor,
    ConflictResolution,
    SyncProtocolError,
    SyncReceipt,
    SyncStatus,
    ValidatedBundle,
)
from comic_editor.three_d.repository import (
    BLENDER_DIR,
    INBOX_DIR,
    BlenderSidecarData,
    BlenderSidecarRepository,
)
from comic_editor.three_d.sync_server import (
    ConcurrentRevisionError,
    SyncInboxProcessor,
    SyncNotification,
    SyncNotificationServer,
)


SidecarProvider = Callable[[], BlenderSidecarData | None]
CommitUpdate = Callable[[BlenderSidecarData, BlenderSidecarData, str], None]
_SOURCE_DIGEST_KEY = "accepted_source_digest"


@dataclass(frozen=True)
class SyncSessionBinding:
    """Stable paths/identity plus UI-owned sidecar callbacks for one chapter."""

    chapter_root: Path
    series_id: str
    chapter_id: str
    sidecar_provider: SidecarProvider
    commit_update: CommitUpdate

    def __post_init__(self) -> None:
        root = Path(self.chapter_root).expanduser().resolve()
        if not root.is_dir():
            raise ValueError(f"Chapter folder does not exist: {root}")
        object.__setattr__(self, "chapter_root", root)
        if not str(self.series_id).strip() or not str(self.chapter_id).strip():
            raise ValueError("Sync bindings require series and chapter IDs")

    @property
    def blender_root(self) -> Path:
        return self.chapter_root / BLENDER_DIR

    @property
    def inbox_root(self) -> Path:
        return self.blender_root / INBOX_DIR

    @classmethod
    def for_editor_session(
        cls, session: Any, commit_update: CommitUpdate,
    ) -> "SyncSessionBinding":
        if getattr(session, "kind", None) != "series":
            raise ValueError("Blender sync is available only for series sessions")
        context = session.context
        return cls(
            chapter_root=context.repository.chapter_root(session.chapter.chapter_id),
            series_id=context.series.series_id,
            chapter_id=session.chapter.chapter_id,
            sidecar_provider=lambda: session.blender_sidecar,
            commit_update=commit_update,
        )


@dataclass
class _UiCall:
    callback: Callable[[], Any]
    completed: threading.Event
    result: Any = None
    error: BaseException | None = None


class _UiBridge(QObject):
    requested = Signal(object)

    def __init__(self, parent: QObject) -> None:
        super().__init__(parent)
        self.requested.connect(self._execute, Qt.ConnectionType.QueuedConnection)

    @Slot(object)
    def _execute(self, call: _UiCall) -> None:
        try:
            call.result = call.callback()
        except BaseException as error:  # Propagate to the waiting server thread.
            call.error = error
        finally:
            call.completed.set()

    def call(self, callback: Callable[[], Any], *, timeout: float = 30.0) -> Any:
        if QThread.currentThread() == self.thread():
            return callback()
        call = _UiCall(callback, threading.Event())
        self.requested.emit(call)
        if not call.completed.wait(timeout):
            raise RuntimeError("Timed out waiting for the Webtoon UI thread")
        if call.error is not None:
            raise call.error
        return call.result


def _sidecar_revision(sidecar: BlenderSidecarData) -> int:
    return max(
        int(sidecar.document.revision),
        *(int(frame.revision) for frame in sidecar.frames.values()),
    )


def _deep_merge(base: Any, override: Any) -> Any:
    if isinstance(base, Mapping) and isinstance(override, Mapping):
        result = copy.deepcopy(dict(base))
        for key, value in override.items():
            result[str(key)] = _deep_merge(result.get(str(key)), value)
        return result
    return copy.deepcopy(override)


def _effective_frame_state(frame: ComicFrameDocument) -> dict[str, Any]:
    """Overlay presentation fields onto source state for three-way comparison."""

    state = copy.deepcopy(frame.source_state)
    overrides = frame.presentation_overrides
    for category in (
        "transforms", "poses", "shape_keys", "cameras", "lights",
        "collection_visibility",
    ):
        value = overrides.get(category)
        if isinstance(value, Mapping):
            state[category] = _deep_merge(state.get(category, {}), value)
    visibility = overrides.get("visibility")
    if isinstance(visibility, Mapping):
        target = copy.deepcopy(state.get("visibility", {}))
        for object_id, value in visibility.items():
            if isinstance(value, bool):
                record = copy.deepcopy(target.get(object_id, {}))
                if not isinstance(record, dict):
                    record = {}
                record.update({
                    "visible": value,
                    "hide_viewport": not value,
                    "hide_render": not value,
                })
                target[object_id] = record
            else:
                target[object_id] = _deep_merge(target.get(object_id), value)
        state["visibility"] = target
    return state


def _remove_mapping_path(root: dict[str, Any], parts: list[str]) -> bool:
    """Remove one override leaf, pruning empty parents.

    A compact scalar override (for example ``visibility[id] = false``) owns
    the whole subtree. Removing a deeper conflicting field therefore removes
    that scalar entry instead of silently retaining the Blender conflict.
    """

    if not parts or parts[0] not in root:
        return False
    parents: list[tuple[dict[str, Any], str]] = []
    target = root
    for index, part in enumerate(parts[:-1]):
        child = target.get(part)
        if not isinstance(child, dict):
            target.pop(part, None)
            removed = True
            break
        parents.append((target, part))
        target = child
    else:
        removed = parts[-1] in target
        target.pop(parts[-1], None)
    if not removed:
        return False
    for parent, key in reversed(parents):
        child = parent.get(key)
        if isinstance(child, dict) and not child:
            parent.pop(key, None)
    return True


def _remove_blender_won_override(
    overrides: dict[str, Any], conflict_path: str,
) -> None:
    parts = [
        part.replace("~1", "/").replace("~0", "~")
        for part in conflict_path.split("/")[1:]
    ]
    if not parts:
        return
    candidates = [parts]
    if parts[0] in {"collections", "collection_visibility"}:
        candidates = [
            ["collection_visibility", *parts[1:]],
            ["collections", *parts[1:]],
        ]
    for candidate in candidates:
        _remove_mapping_path(overrides, candidate)


class ThreeDSyncCoordinator(QObject):
    """Own one active chapter's inbox processor and loopback server."""

    receiptReady = Signal(object)
    accepted = Signal(object)
    conflicts = Signal(object)
    rejected = Signal(object)
    queued = Signal(object)
    statusMessage = Signal(str)
    registrationChanged = Signal(object)
    runningChanged = Signal(bool)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._bridge = _UiBridge(self)
        self._binding: SyncSessionBinding | None = None
        self._repository: BlenderSidecarRepository | None = None
        self._processor: SyncInboxProcessor | None = None
        self._server: SyncNotificationServer | None = None
        self._generation = 0
        self._offline_thread: threading.Thread | None = None
        self._source_revision_watermark = 0
        self._source_digest_watermark = ""

    @property
    def is_running(self) -> bool:
        return self._server is not None and self._server.is_running

    @property
    def endpoint(self) -> str:
        return self._server.endpoint if self._server is not None else ""

    @property
    def auth_token(self) -> str:
        return self._server.auth_token if self._server is not None else ""

    @property
    def binding(self) -> SyncSessionBinding | None:
        return self._binding

    def activate(
        self, binding: SyncSessionBinding, *, process_offline: bool = True,
    ) -> Mapping[str, Any]:
        self.stop()
        binding.inbox_root.mkdir(parents=True, exist_ok=True)
        self._binding = binding
        self._repository = BlenderSidecarRepository(binding.blender_root)
        self._generation += 1
        generation = self._generation
        initial = self._bridge.call(binding.sidecar_provider)
        if initial is not None:
            self._source_revision_watermark = int(initial.document.source_revision)
            self._source_digest_watermark = str(
                initial.document.extensions.get(_SOURCE_DIGEST_KEY, "")
            )

        self._processor = SyncInboxProcessor(
            binding.inbox_root,
            series_id=binding.series_id,
            chapter_id=binding.chapter_id,
            expected_blender_file_uuid=lambda: self._bridge.call(
                lambda: self._expected_file_uuid(binding, generation)
            ),
            current_revision=lambda: self._bridge.call(
                lambda: self._current_revision(binding, generation)
            ),
            current_source_revision=lambda: self._bridge.call(
                lambda: self._current_source_revision(binding, generation)
            ),
            current_source_digest=lambda: self._bridge.call(
                lambda: self._current_source_digest(binding, generation)
            ),
            current_state=lambda frame_id: self._bridge.call(
                lambda: self._current_state(binding, generation, frame_id)
            ),
            publish=lambda validated, merged, expected: self._publish(
                binding, generation, validated, merged, expected,
            ),
            publish_resolved=(
                lambda validated, merged, expected, conflicts, choices:
                self._publish_resolved(
                    binding,
                    generation,
                    validated,
                    merged,
                    expected,
                    conflicts,
                    choices,
                )
            ),
        )
        self._server = SyncNotificationServer(self._handle_notification).start()
        payload = self.registration_payload()
        self.runningChanged.emit(True)
        self.registrationChanged.emit(payload)
        if process_offline:
            self.process_offline_async()
        return payload

    def activate_editor_session(
        self, session: Any, commit_update: CommitUpdate, *,
        process_offline: bool = True,
    ) -> Mapping[str, Any]:
        return self.activate(
            SyncSessionBinding.for_editor_session(session, commit_update),
            process_offline=process_offline,
        )

    def stop(self) -> None:
        self._generation += 1
        server, self._server = self._server, None
        if server is not None:
            server.stop()
        self._processor = None
        self._repository = None
        self._binding = None
        self._source_revision_watermark = 0
        self._source_digest_watermark = ""
        self.registrationChanged.emit({})
        self.runningChanged.emit(False)

    close = stop

    def registration_payload(self, comic_frame_id: str = "") -> dict[str, Any]:
        binding = self._binding
        if binding is None or self._server is None:
            return {}
        sidecar = self._bridge.call(binding.sidecar_provider)
        file_uuid = sidecar.document.file_uuid if sidecar is not None else ""
        revision = _sidecar_revision(sidecar) if sidecar is not None else 0
        frame_ids = list(sidecar.document.frame_ids) if sidecar is not None else []
        return {
            "endpoint": self.endpoint,
            "auth_token": self.auth_token,
            "chapter_root": str(binding.chapter_root),
            "inbox_root": str(binding.inbox_root),
            "series_id": binding.series_id,
            "chapter_id": binding.chapter_id,
            "comic_frame_id": str(comic_frame_id),
            "comic_frame_ids": frame_ids,
            "blender_file_uuid": file_uuid,
            "base_revision": revision,
            "source_revision": self._source_revision_watermark,
        }

    def process_notification(self, notification: SyncNotification) -> SyncReceipt:
        if self._processor is None:
            raise RuntimeError("No chapter is active for Blender sync")
        receipt = self._processor.process(notification)
        self._emit_receipt(receipt)
        return receipt

    def resolve_conflicts(
        self,
        receipt: SyncReceipt,
        choices: Mapping[str, ConflictResolution | str],
    ) -> SyncReceipt:
        """Apply exact-path or category conflict choices and retry atomically.

        Omitted choices keep the Webtoon override. ``use_blender_value`` also
        removes the matching sparse override, so Reset to Blender and future
        captures share the same source of truth.
        """

        if self._processor is None:
            raise RuntimeError("No chapter is active for Blender sync")
        resolved = self._processor.resolve_conflicts(receipt, choices)
        self._emit_receipt(resolved)
        return resolved

    def _handle_notification(self, notification: SyncNotification) -> SyncReceipt:
        return self.process_notification(notification)

    def process_offline_now(self) -> list[SyncReceipt]:
        if self._processor is None:
            return []
        receipts = self._processor.process_ready_transactions()
        for receipt in receipts:
            self._emit_receipt(receipt)
        return receipts

    def process_offline_async(self) -> None:
        if self._processor is None:
            return
        generation = self._generation

        def run() -> None:
            if generation != self._generation:
                return
            try:
                self.process_offline_now()
            except Exception as error:
                self.statusMessage.emit(
                    f"Offline Blender sync failed: {type(error).__name__}"
                )

        self._offline_thread = threading.Thread(
            target=run, name="webtoon-blender-offline-inbox", daemon=True,
        )
        self._offline_thread.start()

    def _assert_active(
        self, binding: SyncSessionBinding, generation: int,
    ) -> BlenderSidecarData:
        if binding is not self._binding or generation != self._generation:
            raise SyncProtocolError("The target chapter is no longer active", code="wrong_identity")
        sidecar = binding.sidecar_provider()
        if sidecar is None:
            raise SyncProtocolError("The active chapter has no 3D sidecar", code="wrong_identity")
        if sidecar.document.chapter_id != binding.chapter_id:
            raise SyncProtocolError("The active 3D sidecar belongs to another chapter", code="wrong_identity")
        return sidecar

    def _expected_file_uuid(
        self, binding: SyncSessionBinding, generation: int,
    ) -> str | None:
        sidecar = self._assert_active(binding, generation)
        return sidecar.document.file_uuid or None

    def _current_revision(
        self, binding: SyncSessionBinding, generation: int,
    ) -> int:
        return _sidecar_revision(self._assert_active(binding, generation))

    def _current_source_revision(
        self, binding: SyncSessionBinding, generation: int,
    ) -> int:
        sidecar = self._assert_active(binding, generation)
        return max(
            int(sidecar.document.source_revision),
            self._source_revision_watermark,
        )

    def _current_source_digest(
        self, binding: SyncSessionBinding, generation: int,
    ) -> str | None:
        sidecar = self._assert_active(binding, generation)
        revision = int(sidecar.document.source_revision)
        if revision < self._source_revision_watermark:
            return self._source_digest_watermark or None
        digest = str(sidecar.document.extensions.get(_SOURCE_DIGEST_KEY, ""))
        if revision == self._source_revision_watermark:
            return digest or self._source_digest_watermark or None
        return digest or None

    def _current_state(
        self, binding: SyncSessionBinding, generation: int, frame_id: str,
    ) -> Mapping[str, Any]:
        sidecar = self._assert_active(binding, generation)
        frame = sidecar.frames.get(frame_id)
        if frame is None:
            raise SyncProtocolError("Comic frame is not part of the active chapter", code="wrong_identity")
        return _effective_frame_state(frame)

    def _copy_validated_blobs(
        self,
        binding: SyncSessionBinding,
        generation: int,
        validated: ValidatedBundle,
    ) -> None:
        # A notification may finish validation while the user is switching
        # tabs.  Never let that old worker publish into the newly active
        # chapter's repository.
        if binding is not self._binding or generation != self._generation:
            raise SyncProtocolError(
                "The target chapter is no longer active", code="wrong_identity",
            )
        repository = BlenderSidecarRepository(binding.blender_root)
        entries = {entry.path: entry for entry in validated.bundle.files}
        for relative, path in validated.files.items():
            if path.suffix.casefold() != ".glb":
                continue
            entry = entries[relative]
            repository.write_blob(path.read_bytes(), entry.sha256)

    def _publish(
        self,
        binding: SyncSessionBinding,
        generation: int,
        validated: ValidatedBundle,
        merged_state: Mapping[str, Any],
        expected_revision: int,
    ) -> int:
        # Immutable content can be copied on the HTTP/offline worker. A lost CAS
        # merely leaves an unreferenced hash for conservative later GC.
        self._copy_validated_blobs(binding, generation, validated)
        return int(self._bridge.call(lambda: self._publish_ui(
            binding, generation, validated, merged_state, expected_revision,
        )))

    def _publish_resolved(
        self,
        binding: SyncSessionBinding,
        generation: int,
        validated: ValidatedBundle,
        merged_state: Mapping[str, Any],
        expected_revision: int,
        conflicts: tuple[ConflictDescriptor, ...],
        choices: Mapping[str, ConflictResolution | str],
    ) -> int:
        self._copy_validated_blobs(binding, generation, validated)
        return int(self._bridge.call(lambda: self._publish_ui(
            binding,
            generation,
            validated,
            merged_state,
            expected_revision,
            conflicts=conflicts,
            choices=choices,
        )))

    def _publish_ui(
        self,
        binding: SyncSessionBinding,
        generation: int,
        validated: ValidatedBundle,
        merged_state: Mapping[str, Any],
        expected_revision: int,
        *,
        conflicts: tuple[ConflictDescriptor, ...] = (),
        choices: Mapping[str, ConflictResolution | str] | None = None,
    ) -> int:
        current = self._assert_active(binding, generation)
        actual_revision = _sidecar_revision(current)
        if actual_revision != expected_revision:
            raise ConcurrentRevisionError(actual_revision)
        bundle = validated.bundle
        if bundle.comic_frame_id not in current.document.frame_ids:
            raise SyncProtocolError(
                "Blender selected a comic frame not owned by this chapter",
                code="wrong_identity",
            )
        before = copy.deepcopy(current)
        working = copy.deepcopy(current)
        document = working.document
        if document.file_uuid and document.file_uuid != bundle.blender_file_uuid:
            raise SyncProtocolError("The chapter is linked to another Blender file", code="wrong_identity")

        chapter_data = bundle.chapter_data
        document.file_uuid = bundle.blender_file_uuid
        document.series_id = binding.series_id
        document.blend_path_hint = str(chapter_data.get("blend_path_hint", document.blend_path_hint))
        document.scene_catalog = copy.deepcopy(dict(chapter_data.get("scenes", {})))
        document.collection_catalog = copy.deepcopy(dict(chapter_data.get("collections", {})))
        document.object_catalog = copy.deepcopy(dict(chapter_data.get("objects", {})))
        document.material_catalog = copy.deepcopy(dict(chapter_data.get("materials", {})))
        document.source_revision = bundle.source_revision
        document.extensions[_SOURCE_DIGEST_KEY] = bundle.source_digest()
        document.extensions["source_material_assignments"] = copy.deepcopy(
            chapter_data.get("material_assignments", {})
        )
        document.extensions["last_sync_transaction_id"] = bundle.transaction_id
        document.warnings = list(dict.fromkeys((*document.warnings, *bundle.warnings)))

        previous_cache = working.cache_manifest
        cache_manifest = CacheManifest.from_dict(copy.deepcopy(bundle.cache_manifest))
        if cache_manifest.source_revision != bundle.source_revision:
            raise SyncProtocolError(
                "Cache and bundle Blender source revisions do not match",
                code="stale_source_revision",
            )
        geometry_changed = bool(
            previous_cache is not None and (
                previous_cache.base_glb_hash != cache_manifest.base_glb_hash
                or previous_cache.object_resources != cache_manifest.object_resources
            )
        )
        working.cache_manifest = cache_manifest
        if cache_manifest.revision not in document.cache_revisions:
            document.cache_revisions.append(cache_manifest.revision)
        document.current_cache_revision = cache_manifest.revision

        frame = working.frames.get(bundle.comic_frame_id)
        if frame is None:
            frame = ComicFrameDocument(
                frame_id=bundle.comic_frame_id,
                chapter_id=binding.chapter_id,
            )
            working.frames[bundle.comic_frame_id] = frame
        frame_data = bundle.frame_data
        captured_state = frame_data.get("captured_state")
        if not isinstance(captured_state, Mapping):
            raise SyncProtocolError("Captured comic-frame state is missing", code="malformed_manifest")
        # Source state remains Blender's exact capture. Presentation overrides
        # survive non-conflicting updates and remain independently resettable.
        resolution_choices = choices or {}
        for conflict in conflicts:
            raw_choice = resolution_choices.get(
                conflict.path,
                resolution_choices.get(
                    conflict.category, conflict.default_resolution,
                ),
            )
            try:
                choice = ConflictResolution(raw_choice)
            except (TypeError, ValueError) as error:
                raise SyncProtocolError(
                    f"Unknown resolution for {conflict.path}",
                    code="invalid_resolution",
                ) from error
            if choice is ConflictResolution.USE_BLENDER_VALUE:
                _remove_blender_won_override(
                    frame.presentation_overrides, conflict.path,
                )

        frame.source_state = copy.deepcopy(dict(captured_state))
        frame.source_scene_id = str(frame_data.get(
            "source_scene_id", frame_data.get("scene_id", ""),
        ))
        frame.source_timeline_frame = int(frame_data.get("timeline_frame", 1))
        frame.source_revision = bundle.source_revision
        frame.included_collection_ids = [
            str(item) for item in frame_data.get("included_collection_ids", [])
        ]
        frame.extensions["active_camera_id"] = frame_data.get("active_camera_id")
        frame.extensions["last_sync_transaction_id"] = bundle.transaction_id
        # Retain the safe merged effective state for diagnostics/conflict UI;
        # it contains no geometry and is not used as the reset-to-Blender base.
        frame.extensions["last_merged_effective_state"] = copy.deepcopy(dict(merged_state))
        # Per-frame fallbacks are object resources, not alternate whole-scene
        # snapshots.  The current chapter base GLB remains authoritative for
        # every frame so geometry/material changes propagate chapter-wide.
        frame.baked_variant_hashes = copy.deepcopy(
            cache_manifest.baked_variants
        )
        frame.extensions.pop("stale_baked_variant_ids", None)
        frame.warnings = [
            warning for warning in frame.warnings
            if not warning.startswith("Baked modifier cache may be stale:")
        ]
        if geometry_changed:
            for other_id, other_frame in working.frames.items():
                if other_id == frame.frame_id:
                    continue
                stale_ids = sorted(
                    key for key in other_frame.baked_variant_hashes
                    if key != "scene"
                )
                if not stale_ids:
                    continue
                other_frame.extensions["stale_baked_variant_ids"] = stale_ids
                warning = (
                    "Baked modifier cache may be stale: "
                    + ", ".join(stale_ids)
                )
                other_frame.warnings = list(dict.fromkeys((
                    *other_frame.warnings, warning,
                )))
        frame.warnings = list(dict.fromkeys((*frame.warnings, *bundle.warnings)))

        accepted_revision = expected_revision + 1
        document.revision = accepted_revision
        frame.base_revision = accepted_revision
        frame.revision = max(int(frame.revision) + 1, accepted_revision)
        working.validate()

        repository = self._repository
        if repository is None:
            raise SyncProtocolError("No active sidecar repository", code="wrong_identity")
        repository.save(working)
        try:
            binding.commit_update(before, copy.deepcopy(working), "Update Blender comic frame")
        except Exception:
            # Metadata publication is reversible; immutable newly written blobs
            # remain harmless and unreferenced if the UI commit fails.
            repository.save(before)
            raise
        self._source_revision_watermark = bundle.source_revision
        self._source_digest_watermark = bundle.source_digest()
        return accepted_revision

    def _emit_receipt(self, receipt: SyncReceipt) -> None:
        self.receiptReady.emit(receipt)
        if receipt.status is SyncStatus.ACCEPTED:
            self.accepted.emit(receipt)
            self.statusMessage.emit(
                f"Blender comic frame accepted as revision {receipt.accepted_revision}."
            )
        elif receipt.status is SyncStatus.CONFLICTS:
            self.conflicts.emit(receipt)
            self.statusMessage.emit("Blender sync needs presentation conflict resolution.")
        elif receipt.status is SyncStatus.REJECTED:
            self.rejected.emit(receipt)
            self.statusMessage.emit(
                receipt.errors[0] if receipt.errors else "Blender sync was rejected."
            )
        else:
            self.queued.emit(receipt)
            self.statusMessage.emit("Blender sync bundle is queued, not yet accepted.")


__all__ = ["SyncSessionBinding", "ThreeDSyncCoordinator"]
