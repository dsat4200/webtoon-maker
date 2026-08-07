from __future__ import annotations

import json
from pathlib import Path

import pytest

from blender_addon.webtoon_sync.registration import (
    RegistrationError,
    load_frame_collection_ids,
    parse_registration_json,
)


FILE_UUID = "00000000-0000-4000-8000-000000000055"
FRAME_ONE = "frame-one"
FRAME_TWO = "frame-two"


def _chapter_tree(tmp_path: Path) -> Path:
    chapter = tmp_path / "chapters" / "chapter-registration"
    (chapter / "blender" / "inbox").mkdir(parents=True)
    frames = chapter / "blender" / "frames"
    frames.mkdir()
    (frames / f"{FRAME_ONE}.json").write_text(json.dumps({
        "schema_version": 1,
        "included_collection_ids": ["collection-a", "collection-b"],
    }), encoding="utf-8")
    (frames / f"{FRAME_TWO}.json").write_text(json.dumps({
        "schema_version": 1,
        "included_collection_ids": [],
    }), encoding="utf-8")
    return chapter


def _payload(chapter: Path) -> dict:
    return {
        "endpoint": "http://127.0.0.1:54321/rpc",
        "auth_token": "t" * 43,
        "chapter_root": str(chapter),
        "inbox_root": str(chapter / "blender" / "inbox"),
        "series_id": "series-registration",
        "chapter_id": "chapter-registration",
        "comic_frame_id": FRAME_TWO,
        "comic_frame_ids": [FRAME_ONE, FRAME_TWO],
        "blender_file_uuid": FILE_UUID,
        "base_revision": 8,
        "source_revision": 5,
    }


def test_registration_parser_validates_and_normalizes_clipboard_payload(tmp_path):
    chapter = _chapter_tree(tmp_path)
    result = parse_registration_json(json.dumps(_payload(chapter)))

    assert result.endpoint == "http://127.0.0.1:54321/rpc"
    assert result.auth_token == "t" * 43
    assert result.chapter_root == chapter.resolve()
    assert result.inbox_root == (chapter / "blender" / "inbox").resolve()
    assert result.series_id == "series-registration"
    assert result.chapter_id == "chapter-registration"
    assert result.comic_frame_ids == (FRAME_ONE, FRAME_TWO)
    assert result.selected_frame_id == FRAME_TWO
    assert result.blender_file_uuid == FILE_UUID
    assert result.base_revision == 8
    assert result.source_revision == 5


def test_registration_defaults_to_first_available_frame_and_supports_older_payload(tmp_path):
    chapter = _chapter_tree(tmp_path)
    payload = _payload(chapter)
    payload["comic_frame_id"] = ""
    payload.pop("source_revision")

    result = parse_registration_json(json.dumps(payload))

    assert result.selected_frame_id == FRAME_ONE
    assert result.source_revision == 0


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("endpoint", "https://example.com/rpc", "127.0.0.1"),
        ("auth_token", "short", "auth token"),
        ("series_id", "../series", "safe identifier"),
        ("comic_frame_ids", [FRAME_ONE, FRAME_ONE], "duplicate"),
        ("comic_frame_id", "not-registered", "not part"),
        ("blender_file_uuid", "not-a-uuid", "UUID"),
        ("base_revision", -1, "non-negative"),
    ],
)
def test_registration_rejects_invalid_identity_transport_and_revision_fields(
    tmp_path, field, value, message,
):
    chapter = _chapter_tree(tmp_path)
    payload = _payload(chapter)
    payload[field] = value
    with pytest.raises(RegistrationError, match=message):
        parse_registration_json(json.dumps(payload))


def test_registration_rejects_duplicate_json_keys_and_wrong_inbox(tmp_path):
    chapter = _chapter_tree(tmp_path)
    encoded = json.dumps(_payload(chapter))
    duplicate = encoded.replace(
        "{", '{"endpoint":"http://127.0.0.1:54321/rpc",', 1,
    )
    with pytest.raises(RegistrationError, match="Duplicate JSON key"):
        parse_registration_json(duplicate)

    other = tmp_path / "other-inbox"
    other.mkdir()
    payload = _payload(chapter)
    payload["inbox_root"] = str(other)
    with pytest.raises(RegistrationError, match="does not belong"):
        parse_registration_json(json.dumps(payload))


def test_registered_frame_loader_returns_explicit_collection_participation(tmp_path):
    chapter = _chapter_tree(tmp_path)
    assert load_frame_collection_ids(chapter, FRAME_ONE) == (
        "collection-a", "collection-b",
    )
    assert load_frame_collection_ids(chapter, FRAME_TWO) == ()
    with pytest.raises(RegistrationError, match="safe identifier"):
        load_frame_collection_ids(chapter, "../escape")


def test_registered_frame_loader_rejects_duplicate_or_malformed_collection_data(tmp_path):
    chapter = _chapter_tree(tmp_path)
    frame = chapter / "blender" / "frames" / f"{FRAME_ONE}.json"
    frame.write_text(json.dumps({
        "included_collection_ids": ["collection-a", "collection-a"],
    }), encoding="utf-8")
    with pytest.raises(RegistrationError, match="duplicate collection"):
        load_frame_collection_ids(chapter, FRAME_ONE)

    frame.write_text('{"included_collection_ids": NaN}', encoding="utf-8")
    with pytest.raises(RegistrationError, match="Non-finite"):
        load_frame_collection_ids(chapter, FRAME_ONE)
