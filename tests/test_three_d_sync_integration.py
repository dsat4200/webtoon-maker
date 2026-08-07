from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path
import struct
import threading
import time
import uuid

from PySide6.QtCore import QThread

from blender_addon.webtoon_sync.transport import notify_webtoon
from blender_addon.webtoon_sync.wire import stage_ready_bundle
from comic_editor.core.commands import CallbackCommand, CommandStack
from comic_editor.three_d.documents import (
    BlenderChapterDocument,
    ComicFrameDocument,
    DrawingMaterial3D,
)
from comic_editor.three_d.repository import (
    BlenderSidecarData,
    BlenderSidecarRepository,
)
from comic_editor.three_d.sync_server import SyncNotification
from comic_editor.ui.three_d_sync import (
    SyncSessionBinding,
    ThreeDSyncCoordinator,
)


SERIES_ID = "series-sync"
CHAPTER_ID = "chapter-sync"
FRAME_ID = "frame-sync"
FILE_UUID = "00000000-0000-4000-8000-000000000011"


def _write_glb(path: Path) -> str:
    document = json.dumps({
        "asset": {"version": "2.0"},
        "nodes": [{"name": "Cube", "extras": {"webtoon_uuid": "cube"}}],
    }, separators=(",", ":")).encode()
    document += b" " * ((-len(document)) % 4)
    length = 20 + len(document)
    data = (
        struct.pack("<4sII", b"glTF", 2, length)
        + struct.pack("<II", len(document), 0x4E4F534A)
        + document
    )
    path.write_bytes(data)
    return hashlib.sha256(data).hexdigest()


def _initial_sidecar(chapter_root: Path, *, override_x: float | None = None):
    material = DrawingMaterial3D(name="Keep Webtoon Material")
    document = BlenderChapterDocument(
        chapter_id=CHAPTER_ID,
        series_id=SERIES_ID,
        file_uuid=FILE_UUID,
        revision=0,
        drawing_materials=[material],
        material_mappings={"source-material": material.material_id},
        frame_ids=[FRAME_ID],
    )
    frame = ComicFrameDocument(
        frame_id=FRAME_ID,
        chapter_id=CHAPTER_ID,
        revision=0 if override_x is None else 1,
        source_state={"transforms": {"cube": {"matrix_local": [
            1, 0, 0, 0, 0, 1, 0, 0,
            0, 0, 1, 0, 0, 0, 0, 1,
        ], "x": 0}}},
        presentation_overrides=(
            {} if override_x is None
            else {"transforms": {"cube": {"x": override_x}}}
        ),
        included_collection_ids=["collection-old"],
    )
    sidecar = BlenderSidecarData(document, frames={FRAME_ID: frame})
    BlenderSidecarRepository(chapter_root / "blender").save(sidecar)
    return sidecar


def _stage(
    chapter_root: Path,
    *,
    base_revision: int = 0,
    incoming_x: float = 2,
    legacy_scene_key: bool = False,
    source_revision: int | None = None,
):
    source = chapter_root / f"source-{uuid.uuid4().hex}.glb"
    digest = _write_glb(source)
    source_revision = base_revision + 1 if source_revision is None else source_revision
    cache = {
        "schema_version": 1,
        "revision": digest,
        "source_revision": source_revision,
        "source_hashes": {f"cache/blobs/{digest}.glb": digest},
        "base_glb_hash": digest,
        "object_resources": {},
        "baked_variants": {},
        "freestyle_edges": {},
        "extensions": {"resource_paths": {digest: f"cache/blobs/{digest}.glb"}},
    }
    frame_data = {
        "schema_version": 1,
        ("scene_id" if legacy_scene_key else "source_scene_id"): "scene",
        "timeline_frame": 24,
        "active_camera_id": "camera",
        "included_collection_ids": ["collection-new"],
        "base_state": {"transforms": {"cube": {"x": 0}}},
        "captured_state": {"transforms": {"cube": {"x": incoming_x}}},
    }
    return stage_ready_bundle(
        chapter_root / "blender" / "inbox",
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        comic_frame_id=FRAME_ID,
        blender_file_uuid=FILE_UUID,
        base_revision=base_revision,
        source_revision=source_revision,
        chapter_data={
            "schema_version": 1,
            "blend_path_hint": "C:/Comics/source.blend",
            "scenes": {"scene": {"name": "Scene"}},
            "collections": {
                "collection-new": {"name": "Characters", "parent_id": None},
            },
            "objects": {"cube": {"name": "Cube", "type": "MESH"}},
            "materials": {"source-material": {"name": "Ink"}},
            "material_assignments": {"cube": ["source-material"]},
            "freestyle": {},
        },
        frame_data=frame_data,
        cache_manifest=cache,
        source_files={f"cache/blobs/{digest}.glb": source},
        transaction_id=str(uuid.uuid4()),
    )


def _binding(chapter_root: Path, holder: list, commits: list):
    stack = CommandStack()

    def replace(snapshot, *, monotonic: bool) -> None:
        current_revision = holder[0].document.revision
        restored = copy.deepcopy(snapshot)
        if monotonic:
            restored.document.revision = max(
                current_revision + 1, restored.document.revision + 1,
            )
        holder[0].__dict__.clear()
        holder[0].__dict__.update(restored.__dict__)

    def commit(before, after, label):
        # The coordinator has not exposed a partial in-memory mutation.
        assert holder[0].document.object_catalog != after.document.object_catalog
        replace(after, monotonic=False)
        stack.push(CallbackCommand(
            label,
            lambda: replace(after, monotonic=True),
            lambda: replace(before, monotonic=True),
        ), already_done=True)
        commits.append((before, after, label, QThread.currentThread()))

    return SyncSessionBinding(
        chapter_root=chapter_root,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        sidecar_provider=lambda: holder[0],
        commit_update=commit,
    ), stack


def test_coordinator_publishes_validated_bundle_as_one_undoable_update(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root)]
    commits = []
    binding, stack = _binding(chapter_root, holder, commits)
    ready = _stage(chapter_root)
    coordinator = ThreeDSyncCoordinator()
    registration = coordinator.activate(binding, process_offline=False)

    receipt = coordinator.process_notification(
        SyncNotification(ready.transaction_id, ready.bundle_sha256)
    )

    assert receipt.status.value == "accepted"
    assert receipt.accepted_revision == 1
    assert len(commits) == 1
    assert stack.can_undo
    assert registration["endpoint"].startswith("http://127.0.0.1:")
    assert registration["auth_token"]
    assert registration["base_revision"] == 0

    sidecar = holder[0]
    assert sidecar.document.object_catalog["cube"]["name"] == "Cube"
    assert sidecar.document.material_mappings == commits[0][0].document.material_mappings
    assert sidecar.document.drawing_materials[0].name == "Keep Webtoon Material"
    frame = sidecar.frames[FRAME_ID]
    assert frame.source_scene_id == "scene"
    assert frame.source_timeline_frame == 24
    assert frame.included_collection_ids == ["collection-new"]
    assert frame.source_state["transforms"]["cube"]["x"] == 2

    persisted = BlenderSidecarRepository(chapter_root / "blender").load(
        expected_chapter_id=CHAPTER_ID
    )
    assert persisted.document.revision == 1
    assert persisted.cache_manifest.base_glb_hash
    assert "scene" not in frame.baked_variant_hashes
    assert (chapter_root / "blender" / "cache" / "blobs" / (
        persisted.cache_manifest.base_glb_hash + ".glb"
    )).is_file()

    stack.undo()
    assert holder[0].document.revision > 1
    assert holder[0].document.object_catalog == {}
    coordinator.stop()
    assert not coordinator.is_running
    assert coordinator.registration_payload() == {}


def test_stale_overlapping_presentation_edit_returns_conflicts_without_commit(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root, override_x=9)]
    commits = []
    binding, _stack = _binding(chapter_root, holder, commits)
    ready = _stage(chapter_root, base_revision=0, incoming_x=2)
    coordinator = ThreeDSyncCoordinator()
    coordinator.activate(binding, process_offline=False)

    receipt = coordinator.process_notification(
        SyncNotification(ready.transaction_id, ready.bundle_sha256)
    )

    assert receipt.status.value == "conflicts"
    assert receipt.conflicts[0].category == "transforms"
    assert not commits
    assert holder[0].frames[FRAME_ID].presentation_overrides["transforms"]["cube"]["x"] == 9
    coordinator.stop()


def test_use_blender_conflict_choice_removes_override_and_accepts_once(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root, override_x=9)]
    commits = []
    binding, _stack = _binding(chapter_root, holder, commits)
    ready = _stage(
        chapter_root,
        base_revision=0,
        incoming_x=2,
        legacy_scene_key=True,
    )
    coordinator = ThreeDSyncCoordinator()
    coordinator.activate(binding, process_offline=False)
    conflict_receipt = coordinator.process_notification(
        SyncNotification(ready.transaction_id, ready.bundle_sha256)
    )

    accepted = coordinator.resolve_conflicts(
        conflict_receipt,
        {"transforms": "use_blender_value"},
    )

    assert accepted.status.value == "accepted"
    assert accepted.accepted_revision == 2
    assert len(commits) == 1
    frame = holder[0].frames[FRAME_ID]
    assert frame.presentation_overrides == {}
    assert frame.source_state["transforms"]["cube"]["x"] == 2
    assert frame.source_scene_id == "scene"
    assert coordinator.resolve_conflicts(conflict_receipt, {}) == accepted
    assert len(commits) == 1
    coordinator.stop()


def test_coordinator_rejects_changed_content_reusing_source_revision(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root)]
    commits = []
    binding, _stack = _binding(chapter_root, holder, commits)
    first = _stage(chapter_root, source_revision=1, incoming_x=2)
    changed = _stage(chapter_root, source_revision=1, incoming_x=7)
    coordinator = ThreeDSyncCoordinator()
    coordinator.activate(binding, process_offline=False)

    accepted = coordinator.process_notification(
        SyncNotification(first.transaction_id, first.bundle_sha256)
    )
    rejected = coordinator.process_notification(
        SyncNotification(changed.transaction_id, changed.bundle_sha256)
    )

    assert accepted.status.value == "accepted"
    assert rejected.status.value == "rejected"
    assert rejected.error_code == "source_revision_reuse"
    assert len(commits) == 1
    assert holder[0].document.source_revision == 1
    assert holder[0].document.extensions["accepted_source_digest"]
    assert holder[0].frames[FRAME_ID].source_state["transforms"]["cube"]["x"] == 2
    coordinator.stop()


def test_coordinator_acknowledges_exact_cross_transaction_source_replay(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root)]
    commits = []
    binding, _stack = _binding(chapter_root, holder, commits)
    first = _stage(chapter_root, source_revision=1, incoming_x=2)
    replay = _stage(chapter_root, source_revision=1, incoming_x=2)
    coordinator = ThreeDSyncCoordinator()
    coordinator.activate(binding, process_offline=False)

    accepted = coordinator.process_notification(
        SyncNotification(first.transaction_id, first.bundle_sha256)
    )
    replayed = coordinator.process_notification(
        SyncNotification(replay.transaction_id, replay.bundle_sha256)
    )

    assert accepted.status.value == "accepted"
    assert replayed.status.value == "accepted"
    assert replayed.accepted_revision == accepted.accepted_revision
    assert "already accepted" in replayed.warnings[-1]
    assert len(commits) == 1
    coordinator.stop()


def test_http_publication_is_marshaled_to_qt_thread_and_offline_scan_works(tmp_path, qapp):
    chapter_root = tmp_path / "chapters" / CHAPTER_ID
    chapter_root.mkdir(parents=True)
    holder = [_initial_sidecar(chapter_root)]
    commits = []
    binding, _stack = _binding(chapter_root, holder, commits)
    ready = _stage(chapter_root)
    coordinator = ThreeDSyncCoordinator()
    coordinator.activate(binding, process_offline=False)
    result = []

    worker = threading.Thread(target=lambda: result.append(notify_webtoon(
        coordinator.endpoint,
        coordinator.auth_token,
        transaction_id=ready.transaction_id,
        bundle_sha256=ready.bundle_sha256,
    )))
    worker.start()
    deadline = time.monotonic() + 5
    while worker.is_alive() and time.monotonic() < deadline:
        qapp.processEvents()
        time.sleep(0.005)
    worker.join(timeout=1)

    assert not worker.is_alive()
    assert result[0].accepted
    assert commits[0][3] == coordinator.thread()
    coordinator.stop()

    # A fresh coordinator recognizes the persisted receipt during offline scan
    # and does not invoke its commit callback a second time.
    second_commits = []
    second_binding, _stack = _binding(chapter_root, holder, second_commits)
    second = ThreeDSyncCoordinator()
    second.activate(second_binding, process_offline=False)
    receipts = second.process_offline_now()
    assert receipts and receipts[0].status.value == "accepted"
    assert not second_commits
    second.stop()
