from __future__ import annotations

import json
from pathlib import Path
import struct

import pytest

from blender_addon.webtoon_sync.geometry import (
    GLBExportIntegrityError,
    collect_export_identity_expectations,
    parse_staged_glb_json,
    validate_staged_glb_export,
)


OBJECT_ID = "a0000000-0000-4000-8000-000000000001"
SECOND_OBJECT_ID = "b0000000-0000-4000-8000-000000000002"
DATA_ID = "c0000000-0000-4000-8000-000000000001"
MATERIAL_ID = "d0000000-0000-4000-8000-000000000001"
BONE_ID = "e0000000-0000-4000-8000-000000000001"


def _document() -> dict:
    return {
        "asset": {"version": "2.0", "generator": "pytest"},
        "nodes": [{"name": "Cube", "extras": {"webtoon_uuid": OBJECT_ID}}],
        "meshes": [{"name": "Cube", "extras": {"webtoon_uuid": DATA_ID}}],
        "materials": [{
            "name": "Ink",
            "extras": {"webtoon_uuid": MATERIAL_ID},
        }],
    }


def _glb_bytes(encoded_json: bytes, *chunks: tuple[int, bytes]) -> bytes:
    encoded_json += b" " * ((-len(encoded_json)) % 4)
    body = struct.pack("<II", len(encoded_json), 0x4E4F534A) + encoded_json
    for chunk_type, payload in chunks:
        payload += b"\x00" * ((-len(payload)) % 4)
        body += struct.pack("<II", len(payload), chunk_type) + payload
    return struct.pack("<4sII", b"glTF", 2, 12 + len(body)) + body


def _write_glb(path: Path, document: dict, *chunks: tuple[int, bytes]) -> None:
    encoded = json.dumps(
        document, allow_nan=False, separators=(",", ":"), sort_keys=True,
    ).encode("utf-8")
    path.write_bytes(_glb_bytes(encoded, *chunks))


def _validate(path: Path):
    return validate_staged_glb_export(
        path,
        expected_identities={
            OBJECT_ID: "object",
            DATA_ID: "data",
            MATERIAL_ID: "material",
        },
        bone_ids=(BONE_ID,),
    )


def test_staged_glb_integrity_accepts_embedded_export_and_reports_bone_limit(
    tmp_path: Path,
):
    path = tmp_path / "scene.glb"
    _write_glb(path, _document(), (0x004E4942, b"\x01\x02\x03\x04"))

    report = _validate(path)

    assert report.document["asset"]["version"] == "2.0"
    assert report.identity_locations == {
        OBJECT_ID: ("/nodes/0/extras/webtoon_uuid",),
        DATA_ID: ("/meshes/0/extras/webtoon_uuid",),
        MATERIAL_ID: ("/materials/0/extras/webtoon_uuid",),
    }
    assert len(report.warnings) == 1
    assert "sidecar-validated" in report.warnings[0]


@pytest.mark.parametrize(
    ("section", "kind", "identity"),
    [
        ("nodes", "object", OBJECT_ID),
        ("meshes", "data", DATA_ID),
        ("materials", "material", MATERIAL_ID),
    ],
)
def test_staged_glb_integrity_rejects_missing_expected_extras(
    tmp_path: Path, section: str, kind: str, identity: str,
):
    document = _document()
    document[section][0].pop("extras")
    path = tmp_path / f"missing-{section}.glb"
    _write_glb(path, document)

    with pytest.raises(
        GLBExportIntegrityError,
        match=rf"omitted expected {kind} webtoon_uuid {identity}",
    ):
        _validate(path)


def test_staged_glb_integrity_rejects_duplicate_or_misplaced_identity(
    tmp_path: Path,
):
    duplicated = _document()
    duplicated["nodes"].append({"extras": {"webtoon_uuid": OBJECT_ID}})
    duplicate_path = tmp_path / "duplicate.glb"
    _write_glb(duplicate_path, duplicated)
    with pytest.raises(GLBExportIntegrityError, match="repeats webtoon_uuid"):
        _validate(duplicate_path)

    misplaced = _document()
    misplaced["nodes"][0]["extras"]["webtoon_uuid"] = DATA_ID
    misplaced["meshes"][0]["extras"]["webtoon_uuid"] = OBJECT_ID
    misplaced_path = tmp_path / "misplaced.glb"
    _write_glb(misplaced_path, misplaced)
    with pytest.raises(GLBExportIntegrityError, match="unsupported section"):
        _validate(misplaced_path)


@pytest.mark.parametrize(
    "reference",
    [
        {"buffers": [{"byteLength": 4, "uri": "payload.bin"}]},
        {"images": [{"uri": "https://example.invalid/texture.png"}]},
        {"extras": {"notes": "payload.py"}},
        {"extensions": {"vendor": {"source": " javascript:alert(1)"}}},
        {"extras": {"script": {"path": "payload.py"}}},
    ],
)
def test_staged_glb_integrity_rejects_external_or_executable_references(
    tmp_path: Path, reference: dict,
):
    document = _document()
    document.update(reference)
    path = tmp_path / "unsafe.glb"
    _write_glb(path, document)

    with pytest.raises(GLBExportIntegrityError, match="reference"):
        _validate(path)


def test_staged_glb_integrity_rejects_noncanonical_uuid_and_duplicate_json_key(
    tmp_path: Path,
):
    document = _document()
    document["nodes"][0]["extras"]["webtoon_uuid"] = OBJECT_ID.upper()
    noncanonical = tmp_path / "noncanonical.glb"
    _write_glb(noncanonical, document)
    with pytest.raises(GLBExportIntegrityError, match="canonical UUID"):
        _validate(noncanonical)

    duplicate_key = tmp_path / "duplicate-key.glb"
    duplicate_key.write_bytes(_glb_bytes(
        b'{"asset":{"version":"2.0"},"asset":{"version":"2.0"}}',
    ))
    with pytest.raises(GLBExportIntegrityError, match="duplicate JSON key"):
        parse_staged_glb_json(duplicate_key)


def test_staged_glb_integrity_rejects_second_json_chunk(tmp_path: Path):
    path = tmp_path / "two-json.glb"
    _write_glb(path, _document(), (0x4E4F534A, b"{}"))

    with pytest.raises(GLBExportIntegrityError, match="more than one JSON"):
        parse_staged_glb_json(path)


class _FakeDatablock:
    def __init__(self, name: str, identity: str):
        self.name = name
        self._identity = identity

    def get(self, key: str, default=None):
        if key == "webtoon_uuid":
            return self._identity
        return default


class _FakeSlot:
    def __init__(self, material: _FakeDatablock | None):
        self.material = material


class _FakeObject(_FakeDatablock):
    def __init__(
        self, name: str, identity: str, data: _FakeDatablock,
        material: _FakeDatablock,
    ):
        super().__init__(name, identity)
        self.type = "MESH"
        self.data = data
        self.material_slots = (_FakeSlot(material),)


def test_export_expectations_allow_shared_owners_and_reject_uuid_ambiguity():
    data = _FakeDatablock("Shared Mesh", DATA_ID)
    material = _FakeDatablock("Shared Ink", MATERIAL_ID)
    first = _FakeObject("First", OBJECT_ID, data, material)
    second = _FakeObject("Second", SECOND_OBJECT_ID, data, material)

    result = collect_export_identity_expectations((first, second))

    assert result.identities == {
        OBJECT_ID: "object",
        SECOND_OBJECT_ID: "object",
        DATA_ID: "data",
        MATERIAL_ID: "material",
    }

    ambiguous = _FakeObject("Ambiguous", OBJECT_ID, data, material)
    with pytest.raises(GLBExportIntegrityError, match="ambiguously identifies"):
        collect_export_identity_expectations((first, ambiguous))
