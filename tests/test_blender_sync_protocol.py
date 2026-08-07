from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import struct
from urllib.error import HTTPError
from urllib.request import Request, urlopen
import uuid

import pytest

import comic_editor.three_d.protocol as protocol_module

from blender_addon.webtoon_sync.capture import (
    gltf_column_major_to_blender_rows,
    matrix_column_major,
    matrix_gltf_column_major,
)
from blender_addon.webtoon_sync.geometry import (
    ModifierDescriptor,
    classify_modifier_stack,
)
from blender_addon.webtoon_sync.identities import (
    IdentityRecord,
    plan_identity_repairs,
    shape_key_entry_identity,
)
from blender_addon.webtoon_sync.transport import notify_webtoon
from blender_addon.webtoon_sync.wire import stage_ready_bundle
from comic_editor.three_d.protocol import (
    BundleFile,
    ConflictResolution,
    SyncProtocolError,
    SyncReceipt,
    SyncStatus,
    grouped_conflicts,
    resolve_conflicts,
    three_way_merge_frame_state,
    validate_bundle_directory,
)
from comic_editor.three_d.sync_server import (
    SyncInboxProcessor,
    SyncNotification,
    SyncNotificationServer,
)


SERIES_ID = "series-1"
CHAPTER_ID = "chapter-1"
FRAME_ID = "frame-1"
FILE_UUID = "00000000-0000-4000-8000-000000000001"


def _write_glb(path: Path, document: dict | None = None) -> None:
    encoded = json.dumps(document or {"asset": {"version": "2.0"}}, separators=(",", ":")).encode()
    encoded += b" " * ((-len(encoded)) % 4)
    length = 12 + 8 + len(encoded)
    path.write_bytes(
        struct.pack("<4sII", b"glTF", 2, length)
        + struct.pack("<II", len(encoded), 0x4E4F534A)
        + encoded
    )


def _stage(tmp_path: Path, **overrides):
    source = tmp_path / "source.glb"
    _write_glb(source)
    arguments = {
        "series_id": SERIES_ID,
        "chapter_id": CHAPTER_ID,
        "comic_frame_id": FRAME_ID,
        "blender_file_uuid": FILE_UUID,
        "base_revision": 3,
        "source_revision": 4,
        "chapter_data": {"schema_version": 1, "objects": {}},
        "frame_data": {
            "schema_version": 1,
            "captured_state": {"transforms": {"cube": {"x": 2}}},
            "base_state": {"transforms": {"cube": {"x": 1}}},
        },
        "cache_manifest": {"schema_version": 1, "base_glb": "cache/blobs/source.glb"},
        "source_files": {"cache/blobs/source.glb": source},
        "warnings": [],
        "transaction_id": str(uuid.uuid4()),
        "created_at": 10.5,
    }
    arguments.update(overrides)
    return stage_ready_bundle(tmp_path / "inbox", **arguments)


def test_addon_stages_a_wire_compatible_atomic_ready_bundle(tmp_path):
    ready = _stage(tmp_path)

    assert ready.directory.name == f"{ready.transaction_id}.ready"
    assert not (ready.directory.parent / f".{ready.transaction_id}.staging").exists()
    validated = validate_bundle_directory(
        ready.directory,
        expected_series_id=SERIES_ID,
        expected_chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
    )

    assert validated.bundle.bundle_sha256 == ready.bundle_sha256
    assert validated.bundle.base_revision == 3
    assert set(validated.files) == {"cache/blobs/source.glb"}


def test_blender_source_revision_is_independent_from_webtoon_base_revision(tmp_path):
    ready = _stage(tmp_path, base_revision=10, source_revision=1)
    validated = validate_bundle_directory(
        ready.directory,
        expected_series_id=SERIES_ID,
        expected_chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
    )
    assert validated.bundle.base_revision == 10
    assert validated.bundle.source_revision == 1


def test_bundle_hash_tampering_is_rejected_before_publication(tmp_path):
    ready = _stage(tmp_path)
    glb = ready.directory / "cache" / "blobs" / "source.glb"
    glb.write_bytes(glb.read_bytes() + b"tamper")

    with pytest.raises(SyncProtocolError, match="Size mismatch") as error:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert error.value.code == "hash_mismatch"


@pytest.mark.parametrize("path", ["../escape.glb", "C:/escape.glb", "payload\\escape.glb", "tool.py"])
def test_bundle_file_rejects_traversal_and_executable_paths(path):
    with pytest.raises(SyncProtocolError):
        BundleFile(
            path=path,
            sha256="0" * 64,
            size=0,
            media_type="model/gltf-binary",
        )


def test_detached_bin_resources_are_rejected():
    with pytest.raises(SyncProtocolError) as error:
        BundleFile(
            path="cache/payload.bin",
            sha256="0" * 64,
            size=4,
            media_type="application/octet-stream",
        )
    assert error.value.code == "forbidden_file"


def test_glb_external_uri_is_rejected_even_when_hash_matches(tmp_path):
    source = tmp_path / "external.glb"
    _write_glb(source, {
        "asset": {"version": "2.0"},
        "buffers": [{"byteLength": 0, "uri": "file:///secret.bin"}],
    })
    ready = _stage(tmp_path, source_files={"cache/blobs/external.glb": source})

    with pytest.raises(SyncProtocolError, match="external URI") as error:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert error.value.code == "external_uri"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("texture.png", b"MZ-not-a-png"),
        ("texture.jpg", b"MZ-not-a-jpeg"),
        ("texture.webp", b"MZ-not-a-webp"),
    ],
)
def test_image_members_require_matching_magic(tmp_path, name, content):
    source = tmp_path / name
    source.write_bytes(content)
    ready = _stage(tmp_path, source_files={f"textures/{name}": source})

    with pytest.raises(SyncProtocolError, match="declared type") as error:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert error.value.code == "forbidden_file"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("texture.png", b"\x89PNG\r\n\x1a\n"),
        ("texture.jpg", b"\xff\xd8\xff\xe0\xff\xd9"),
        ("texture.webp", b"RIFF\x04\x00\x00\x00WEBP"),
    ],
)
def test_image_members_with_matching_magic_are_accepted(tmp_path, name, content):
    source = tmp_path / name
    source.write_bytes(content)
    ready = _stage(tmp_path, source_files={f"textures/{name}": source})
    validated = validate_bundle_directory(
        ready.directory,
        expected_series_id=SERIES_ID,
        expected_chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
    )
    assert set(validated.files) == {f"textures/{name}"}


def test_bundle_directory_depth_and_count_are_bounded(tmp_path, monkeypatch):
    deep_root = tmp_path / "deep"
    deep_root.mkdir()
    deep = _stage(deep_root)
    current = deep.directory
    for index in range(protocol_module.MAX_BUNDLE_PATH_DEPTH + 1):
        current = current / f"d{index}"
        current.mkdir()
    with pytest.raises(SyncProtocolError, match="nested too deeply") as depth_error:
        validate_bundle_directory(
            deep.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert depth_error.value.code == "size_limit"

    counted_root = tmp_path / "counted"
    counted_root.mkdir()
    counted = _stage(counted_root)
    monkeypatch.setattr(protocol_module, "MAX_BUNDLE_DIRECTORY_COUNT", 2)
    (counted.directory / "one").mkdir()
    (counted.directory / "two").mkdir()
    with pytest.raises(SyncProtocolError, match="too many directories") as count_error:
        validate_bundle_directory(
            counted.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert count_error.value.code == "size_limit"


def test_reparse_entries_are_rejected_when_platform_api_reports_them(
    tmp_path, monkeypatch,
):
    ready = _stage(tmp_path)
    suspicious = ready.directory / "linked.png"
    suspicious.write_bytes(b"\x89PNG\r\n\x1a\n")
    original = getattr(Path, "is_junction", None)

    def reports_junction(path):
        if path.name == suspicious.name:
            return True
        return original(path) if original is not None else False

    monkeypatch.setattr(Path, "is_junction", reports_junction, raising=False)
    with pytest.raises(SyncProtocolError, match="reparse-point") as error:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert error.value.code == "path_traversal"


def test_hard_linked_bundle_member_is_rejected_when_supported(tmp_path):
    ready = _stage(tmp_path)
    member = ready.directory / "cache" / "blobs" / "source.glb"
    outside_link = tmp_path / "outside-hardlink.glb"
    try:
        os.link(member, outside_link)
    except (NotImplementedError, OSError) as error:
        pytest.skip(f"Filesystem cannot create a hard link: {error}")

    with pytest.raises(SyncProtocolError, match="hard-link") as caught:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=FILE_UUID,
        )
    assert caught.value.code == "path_traversal"


def test_wrong_blender_file_identity_is_rejected(tmp_path):
    ready = _stage(tmp_path)
    with pytest.raises(SyncProtocolError, match="different Blender file") as error:
        validate_bundle_directory(
            ready.directory,
            expected_series_id=SERIES_ID,
            expected_chapter_id=CHAPTER_ID,
            expected_blender_file_uuid=str(uuid.uuid4()),
        )
    assert error.value.code == "wrong_identity"


def test_three_way_merge_respects_ownership_and_groups_shared_conflicts():
    base = {
        "transforms": {"cube": {"x": 0}},
        "geometry": {"cube": "old"},
        "renderer_settings": {"quality": "draft"},
    }
    webtoon = {
        "transforms": {"cube": {"x": 1}},
        "geometry": {"cube": "webtoon-copy"},
        "renderer_settings": {"quality": "high"},
    }
    blender = {
        "transforms": {"cube": {"x": 2}},
        "geometry": {"cube": "new"},
        "renderer_settings": {"quality": "ignored"},
    }

    merged, conflicts = three_way_merge_frame_state(base, webtoon, blender)

    assert merged["geometry"]["cube"] == "new"
    assert merged["renderer_settings"]["quality"] == "high"
    assert merged["transforms"]["cube"]["x"] == 1
    assert list(grouped_conflicts(conflicts)) == ["transforms"]
    assert conflicts[0].default_resolution is ConflictResolution.KEEP_WEBTOON_OVERRIDE

    resolved = resolve_conflicts(
        merged, conflicts, {"transforms": ConflictResolution.USE_BLENDER_VALUE},
    )
    assert resolved["transforms"]["cube"]["x"] == 2


def test_inbox_processor_validates_then_publishes_once(tmp_path):
    ready = _stage(tmp_path, base_revision=0, source_revision=1)
    publications = []
    revision = 0

    def current_revision():
        return revision

    def publish(validated, state, expected):
        nonlocal revision
        assert validated.bundle.transaction_id == ready.transaction_id
        assert expected == revision == 0
        publications.append(state)
        revision = 1
        return revision

    processor = SyncInboxProcessor(
        ready.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=current_revision,
        publish=publish,
    )
    notification = SyncNotification(ready.transaction_id, ready.bundle_sha256)

    first = processor.process(notification)
    second = processor.process(notification)

    assert first.status is SyncStatus.ACCEPTED
    assert first.accepted_revision == 1
    assert second == first
    assert len(publications) == 1

    restarted = SyncInboxProcessor(
        ready.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=current_revision,
        publish=lambda *_args: pytest.fail("accepted transaction was published twice"),
    )
    assert restarted.process(notification) == first


def test_stale_inbox_bundle_returns_grouped_field_conflicts(tmp_path):
    ready = _stage(tmp_path, base_revision=3, source_revision=4)
    called = False

    def publish(*_args):
        nonlocal called
        called = True
        return 6

    processor = SyncInboxProcessor(
        ready.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: 5,
        current_state=lambda _frame: {"transforms": {"cube": {"x": 9}}},
        base_state=lambda _frame, _revision: {"transforms": {"cube": {"x": 1}}},
        publish=publish,
    )
    receipt = processor.process(SyncNotification(ready.transaction_id, ready.bundle_sha256))

    assert receipt.status is SyncStatus.CONFLICTS
    assert receipt.conflicts[0].category == "transforms"
    assert receipt.conflicts[0].webtoon_value == 9
    assert not called


def test_conflict_choices_are_revalidated_published_and_persisted(tmp_path):
    ready = _stage(tmp_path, base_revision=3, source_revision=4)
    publications = []

    def publish_resolved(
        validated, state, expected, conflicts, choices,
    ):
        assert validated.bundle.transaction_id == ready.transaction_id
        assert expected == 5
        assert conflicts[0].path == "/transforms/cube/x"
        assert choices["transforms"] is ConflictResolution.USE_BLENDER_VALUE
        publications.append(state)
        return 6

    processor = SyncInboxProcessor(
        ready.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: 5,
        current_state=lambda _frame: {"transforms": {"cube": {"x": 9}}},
        base_state=lambda _frame, _revision: {"transforms": {"cube": {"x": 1}}},
        publish=lambda *_args: pytest.fail("unresolved publisher was called"),
        publish_resolved=publish_resolved,
    )
    notification = SyncNotification(ready.transaction_id, ready.bundle_sha256)
    conflict_receipt = processor.process(notification)

    accepted = processor.resolve_conflicts(
        conflict_receipt,
        {"transforms": ConflictResolution.USE_BLENDER_VALUE},
    )

    assert accepted.status is SyncStatus.ACCEPTED
    assert accepted.accepted_revision == 6
    assert publications == [{"transforms": {"cube": {"x": 2}}}]
    assert processor.resolve_conflicts(conflict_receipt, {}) == accepted
    assert len(publications) == 1

    restarted = SyncInboxProcessor(
        ready.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: 6,
        publish=lambda *_args: pytest.fail("resolved transaction was published twice"),
    )
    assert restarted.process(notification) == accepted


def test_offline_transactions_are_processed_in_source_revision_order(tmp_path):
    ready_three = _stage(
        tmp_path, base_revision=0, source_revision=3, created_at=1.0,
    )
    ready_one = _stage(
        tmp_path, base_revision=0, source_revision=1, created_at=3.0,
    )
    ready_two = _stage(
        tmp_path, base_revision=0, source_revision=2, created_at=2.0,
    )
    app_revision = 0
    source_revision = 0
    source_digest = None
    order = []

    def publish(validated, _state, expected):
        nonlocal app_revision, source_revision, source_digest
        assert expected == app_revision
        order.append(validated.bundle.source_revision)
        app_revision += 1
        source_revision = validated.bundle.source_revision
        source_digest = validated.bundle.source_digest()
        return app_revision

    processor = SyncInboxProcessor(
        ready_one.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: app_revision,
        current_source_revision=lambda: source_revision,
        current_source_digest=lambda: source_digest,
        current_state=lambda _frame: {"transforms": {"cube": {"x": 1}}},
        publish=publish,
    )

    receipts = processor.process_ready_transactions()

    assert [item.status for item in receipts] == [
        SyncStatus.ACCEPTED, SyncStatus.ACCEPTED, SyncStatus.ACCEPTED,
    ]
    assert order == [1, 2, 3]
    assert {ready_one.transaction_id, ready_two.transaction_id, ready_three.transaction_id} == {
        item.transaction_id for item in receipts
    }


def test_offline_processing_pauses_before_later_source_when_conflict_needs_ui(tmp_path):
    def frame(x):
        return {
            "schema_version": 1,
            "captured_state": {"transforms": {"cube": {"x": x}}},
            "base_state": {"transforms": {"cube": {"x": 1}}},
        }

    first = _stage(
        tmp_path, base_revision=0, source_revision=1,
        frame_data=frame(2), created_at=1.0,
    )
    second = _stage(
        tmp_path, base_revision=0, source_revision=2,
        frame_data=frame(3), created_at=2.0,
    )
    third = _stage(
        tmp_path, base_revision=0, source_revision=3,
        frame_data=frame(4), created_at=3.0,
    )
    app_revision = 0
    source_revision = 0
    source_digest = None
    current_state = {"transforms": {"cube": {"x": 1}}}
    publications = []

    def publish(validated, state, _expected):
        nonlocal app_revision, source_revision, source_digest, current_state
        publications.append(validated.bundle.transaction_id)
        app_revision += 1
        source_revision = validated.bundle.source_revision
        source_digest = validated.bundle.source_digest()
        current_state = state
        return app_revision

    processor = SyncInboxProcessor(
        first.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: app_revision,
        current_source_revision=lambda: source_revision,
        current_source_digest=lambda: source_digest,
        current_state=lambda _frame: current_state,
        publish=publish,
    )

    receipts = processor.process_ready_transactions()

    assert [item.status for item in receipts] == [
        SyncStatus.ACCEPTED, SyncStatus.CONFLICTS,
    ]
    assert publications == [first.transaction_id]
    assert third.transaction_id not in {item.transaction_id for item in receipts}
    assert second.transaction_id == receipts[-1].transaction_id


def test_source_revision_replay_is_idempotent_but_reuse_and_stale_are_rejected(tmp_path):
    first = _stage(tmp_path, base_revision=0, source_revision=1, created_at=1.0)
    exact_replay = _stage(tmp_path, base_revision=0, source_revision=1, created_at=2.0)
    changed = _stage(
        tmp_path,
        base_revision=0,
        source_revision=1,
        created_at=3.0,
        frame_data={
            "schema_version": 1,
            "captured_state": {"transforms": {"cube": {"x": 99}}},
            "base_state": {"transforms": {"cube": {"x": 1}}},
        },
    )
    newer = _stage(tmp_path, base_revision=1, source_revision=2, created_at=4.0)
    late_stale = _stage(tmp_path, base_revision=0, source_revision=1, created_at=5.0)
    app_revision = 0
    source_revision = 0
    source_digest = None
    publications = []

    def publish(validated, _state, _expected):
        nonlocal app_revision, source_revision, source_digest
        publications.append(validated.bundle.transaction_id)
        app_revision += 1
        source_revision = validated.bundle.source_revision
        source_digest = validated.bundle.source_digest()
        return app_revision

    processor = SyncInboxProcessor(
        first.directory.parent,
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: app_revision,
        current_source_revision=lambda: source_revision,
        current_source_digest=lambda: source_digest,
        publish=publish,
    )

    accepted = processor.process(SyncNotification(first.transaction_id, first.bundle_sha256))
    replayed = processor.process(SyncNotification(exact_replay.transaction_id, exact_replay.bundle_sha256))
    reused = processor.process(SyncNotification(changed.transaction_id, changed.bundle_sha256))
    accepted_newer = processor.process(SyncNotification(newer.transaction_id, newer.bundle_sha256))
    stale = processor.process(SyncNotification(late_stale.transaction_id, late_stale.bundle_sha256))

    assert accepted.status is SyncStatus.ACCEPTED
    assert replayed.status is SyncStatus.ACCEPTED
    assert "already accepted" in replayed.warnings[-1]
    assert reused.status is SyncStatus.REJECTED
    assert reused.error_code == "source_revision_reuse"
    assert accepted_newer.status is SyncStatus.ACCEPTED
    assert stale.status is SyncStatus.REJECTED
    assert stale.error_code == "stale_source_revision"
    assert publications == [first.transaction_id, newer.transaction_id]


def test_missing_ready_directory_is_explicitly_queued(tmp_path):
    transaction = str(uuid.uuid4())
    processor = SyncInboxProcessor(
        tmp_path / "inbox",
        series_id=SERIES_ID,
        chapter_id=CHAPTER_ID,
        expected_blender_file_uuid=FILE_UUID,
        current_revision=lambda: 0,
        publish=lambda *_args: 1,
    )
    receipt = processor.process(SyncNotification(transaction, "0" * 64))
    assert receipt.status is SyncStatus.QUEUED


def test_loopback_server_requires_bearer_and_addon_client_reads_receipt():
    transaction_id = str(uuid.uuid4())
    digest = "a" * 64

    def callback(notification):
        assert notification.transaction_id == transaction_id
        return SyncReceipt(
            transaction_id=notification.transaction_id,
            status=SyncStatus.ACCEPTED,
            accepted_revision=8,
        )

    with SyncNotificationServer(callback) as server:
        unauthorized = Request(
            server.endpoint,
            data=b"{}",
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with pytest.raises(HTTPError) as error:
            urlopen(unauthorized, timeout=2)
        assert error.value.code == 401

        result = notify_webtoon(
            server.endpoint,
            server.auth_token,
            transaction_id=transaction_id,
            bundle_sha256=digest,
        )
    assert result.state == "accepted"
    assert result.receipt["accepted_revision"] == 8


def test_identity_repair_is_conservative_and_uses_prior_owner_hint():
    original = IdentityRecord("OBJECT", "OBJECT:1", "Cube", FILE_UUID)
    duplicate = IdentityRecord("OBJECT", "OBJECT:2", "Cube.001", FILE_UUID)
    generated = iter(["00000000-0000-4000-8000-000000000002"])

    ambiguous = plan_identity_repairs((original, duplicate), uuid_factory=lambda: next(generated))
    assert FILE_UUID in ambiguous.ambiguous
    assert not ambiguous.can_publish

    generated = iter(["00000000-0000-4000-8000-000000000002"])
    repaired = plan_identity_repairs(
        (original, duplicate),
        {FILE_UUID: original.owner_key},
        uuid_factory=lambda: next(generated),
    )
    assert repaired.assignments == {
        duplicate.owner_key: "00000000-0000-4000-8000-000000000002",
    }
    assert repaired.can_publish


def test_shape_key_registry_survives_rename_and_reordering():
    class ShapeKeys:
        session_uid = 77

    class Block:
        def __init__(self, name):
            self.name = name

    smile_id = "00000000-0000-4000-8000-000000000003"
    blink_id = "00000000-0000-4000-8000-000000000004"
    registry = {
        "shape_key_entry_meta": {
            smile_id: {
                "parent_key": "SHAPE_KEYS:77", "name": "Smile", "index": 1,
            },
            blink_id: {
                "parent_key": "SHAPE_KEYS:77", "name": "Blink", "index": 2,
            },
        },
        "shape_key_entries": {},
    }

    # A rename retains identity through its prior slot; a reorder retains it by name.
    assert shape_key_entry_identity(registry, ShapeKeys(), Block("Happy"), 1) == smile_id
    assert shape_key_entry_identity(registry, ShapeKeys(), Block("Blink"), 1) == blink_id


def test_modifier_policy_rejects_nodes_and_bakes_incompatible_deformation():
    nodes = classify_modifier_stack(
        object_type="MESH",
        has_shape_keys=False,
        modifiers=(ModifierDescriptor("GeometryNodes", "NODES"),),
    )
    assert nodes.mode == "rejected"

    fallback = classify_modifier_stack(
        object_type="MESH",
        has_shape_keys=True,
        modifiers=(ModifierDescriptor("Subdivision", "SUBSURF"),),
    )
    assert fallback.mode == "baked_fallback"

    static = classify_modifier_stack(
        object_type="MESH",
        has_shape_keys=False,
        modifiers=(ModifierDescriptor("Bevel", "BEVEL"),),
    )
    assert static.mode == "evaluated_static"


def test_matrix_capture_uses_column_major_order():
    matrix = [
        [0, 1, 2, 3],
        [4, 5, 6, 7],
        [8, 9, 10, 11],
        [12, 13, 14, 15],
    ]
    assert matrix_column_major(matrix) == [
        0, 4, 8, 12,
        1, 5, 9, 13,
        2, 6, 10, 14,
        3, 7, 11, 15,
    ]


def test_matrix_capture_converts_blender_z_up_to_gltf_y_up_and_back():
    blender = [
        [1, 0, 0, 1],
        [0, 1, 0, 2],
        [0, 0, 1, 3],
        [0, 0, 0, 1],
    ]
    gltf = matrix_gltf_column_major(blender)
    assert gltf[12:15] == [1, 3, -2]
    assert gltf_column_major_to_blender_rows(gltf) == [
        [1, 0, 0, 1],
        [0, 1, 0, 2],
        [0, 0, 1, 3],
        [0, 0, 0, 1],
    ]
