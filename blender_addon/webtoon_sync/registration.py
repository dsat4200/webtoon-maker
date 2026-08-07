"""Strict, Blender-independent parsing of Webtoon registration clipboard data."""
from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Mapping
import uuid

from .transport import validate_loopback_endpoint


MAX_REGISTRATION_BYTES = 256 * 1024
MAX_FRAME_FILE_BYTES = 16 * 1024 * 1024
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")


class RegistrationError(ValueError):
    pass


def _reject_constant(value: str) -> None:
    raise RegistrationError(f"Non-finite JSON number {value!r} is forbidden")


def _without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise RegistrationError(f"Duplicate JSON key {key!r}")
        result[key] = value
    return result


def _strict_json(
    text: str, *, label: str, maximum_bytes: int = MAX_REGISTRATION_BYTES,
) -> Any:
    if not isinstance(text, str) or not text.strip():
        raise RegistrationError(f"{label} is empty")
    if len(text.encode("utf-8")) > maximum_bytes:
        raise RegistrationError(f"{label} exceeds its size limit")
    try:
        return json.loads(
            text,
            object_pairs_hook=_without_duplicates,
            parse_constant=_reject_constant,
        )
    except RegistrationError:
        raise
    except (UnicodeError, json.JSONDecodeError, RecursionError) as exc:
        raise RegistrationError(f"{label} is malformed JSON") from exc


def _safe_id(value: Any, label: str, *, optional: bool = False) -> str:
    if optional and value == "":
        return ""
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise RegistrationError(f"{label} is not a safe identifier")
    return value


def _nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RegistrationError(f"{label} must be a non-negative integer")
    return value


def _existing_directory(value: Any, label: str) -> Path:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise RegistrationError(f"{label} is missing")
    source = Path(value).expanduser()
    if source.is_symlink():
        raise RegistrationError(f"{label} cannot be a filesystem link")
    try:
        resolved = source.resolve(strict=True)
    except OSError as exc:
        raise RegistrationError(f"{label} does not exist") from exc
    if not resolved.is_dir():
        raise RegistrationError(f"{label} is not a directory")
    return resolved


@dataclass(frozen=True)
class WebtoonRegistration:
    endpoint: str
    auth_token: str
    chapter_root: Path
    inbox_root: Path
    series_id: str
    chapter_id: str
    comic_frame_ids: tuple[str, ...]
    selected_frame_id: str
    blender_file_uuid: str
    base_revision: int
    source_revision: int = 0


def parse_registration_json(text: str) -> WebtoonRegistration:
    """Validate the JSON copied by Webtoon Maker's registration action."""

    value = _strict_json(text, label="Webtoon registration")
    if not isinstance(value, Mapping):
        raise RegistrationError("Webtoon registration must be a JSON object")
    required = {
        "endpoint", "auth_token", "chapter_root", "inbox_root",
        "series_id", "chapter_id", "comic_frame_id", "comic_frame_ids",
        "blender_file_uuid", "base_revision",
    }
    optional = {"source_revision"}
    missing = required.difference(value)
    unknown = set(value).difference(required | optional)
    if missing:
        raise RegistrationError(
            "Webtoon registration is missing: " + ", ".join(sorted(missing))
        )
    if unknown:
        raise RegistrationError(
            "Webtoon registration has unsupported fields: "
            + ", ".join(sorted(unknown))
        )
    try:
        endpoint = validate_loopback_endpoint(value["endpoint"])
    except ValueError as exc:
        raise RegistrationError(str(exc)) from exc
    token = value["auth_token"]
    if not isinstance(token, str) or len(token) < 32 or len(token) > 512:
        raise RegistrationError("Webtoon auth token is missing or invalid")
    chapter_root = _existing_directory(value["chapter_root"], "Chapter folder")
    inbox_root = _existing_directory(value["inbox_root"], "Sync inbox")
    expected_inbox = (chapter_root / "blender" / "inbox").resolve()
    if inbox_root != expected_inbox:
        raise RegistrationError(
            "Sync inbox does not belong to the registered chapter folder"
        )
    raw_frames = value["comic_frame_ids"]
    if not isinstance(raw_frames, list) or not raw_frames:
        raise RegistrationError("Registration contains no comic frames")
    frames = tuple(
        _safe_id(item, "Comic frame ID") for item in raw_frames
    )
    if len(frames) != len(set(frames)):
        raise RegistrationError("Registration contains duplicate comic frame IDs")
    selected = _safe_id(
        value["comic_frame_id"], "Selected comic frame ID", optional=True,
    )
    if not selected:
        selected = frames[0]
    if selected not in frames:
        raise RegistrationError(
            "Selected comic frame is not part of the registered chapter"
        )
    file_uuid = value["blender_file_uuid"]
    if file_uuid:
        try:
            file_uuid = str(uuid.UUID(file_uuid))
        except (ValueError, AttributeError) as exc:
            raise RegistrationError("Blender file UUID is invalid") from exc
    elif not isinstance(file_uuid, str):
        raise RegistrationError("Blender file UUID is invalid")
    return WebtoonRegistration(
        endpoint=endpoint,
        auth_token=token,
        chapter_root=chapter_root,
        inbox_root=inbox_root,
        series_id=_safe_id(value["series_id"], "Series ID"),
        chapter_id=_safe_id(value["chapter_id"], "Chapter ID"),
        comic_frame_ids=frames,
        selected_frame_id=selected,
        blender_file_uuid=file_uuid,
        base_revision=_nonnegative_int(value["base_revision"], "Base revision"),
        source_revision=_nonnegative_int(
            value.get("source_revision", 0), "Source revision",
        ),
    )


def load_frame_collection_ids(
    chapter_root: str | Path, comic_frame_id: str,
) -> tuple[str, ...]:
    """Read one registered frame's explicit participating collections."""

    frame_id = _safe_id(comic_frame_id, "Comic frame ID")
    root = Path(chapter_root).expanduser().resolve(strict=True)
    candidate = root / "blender" / "frames" / f"{frame_id}.json"
    if candidate.is_symlink():
        raise RegistrationError("Comic frame sidecar cannot be a filesystem link")
    try:
        path = candidate.resolve(strict=True)
        path.relative_to(root)
    except (OSError, ValueError) as exc:
        raise RegistrationError("Comic frame sidecar is unavailable") from exc
    if path.stat().st_size > MAX_FRAME_FILE_BYTES:
        raise RegistrationError("Comic frame sidecar exceeds its size limit")
    value = _strict_json(
        path.read_text(encoding="utf-8"),
        label="Comic frame sidecar",
        maximum_bytes=MAX_FRAME_FILE_BYTES,
    )
    if not isinstance(value, Mapping):
        raise RegistrationError("Comic frame sidecar must be an object")
    raw = value.get("included_collection_ids", [])
    if not isinstance(raw, list):
        raise RegistrationError("Comic frame collection IDs must be an array")
    result = tuple(_safe_id(item, "Collection ID") for item in raw)
    if len(result) != len(set(result)):
        raise RegistrationError("Comic frame contains duplicate collection IDs")
    return result


__all__ = [
    "MAX_FRAME_FILE_BYTES",
    "MAX_REGISTRATION_BYTES",
    "RegistrationError",
    "WebtoonRegistration",
    "load_frame_collection_ids",
    "parse_registration_json",
]
