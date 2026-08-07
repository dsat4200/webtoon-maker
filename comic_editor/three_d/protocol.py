"""Versioned, hostile-input-safe contracts for Blender frame synchronization.

This module deliberately depends only on the Python standard library.  Blender's
add-on carries a small wire-compatible writer, while Webtoon Maker treats every
incoming bundle as untrusted input and validates it here before a repository is
allowed to publish a revision.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from enum import Enum
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import struct
from typing import Any, Iterable, Mapping, Sequence
import uuid


SYNC_SCHEMA_VERSION = 1
RECEIPT_SCHEMA_VERSION = 1
BUNDLE_MANIFEST = "bundle.json"

# These limits are intentionally independent from HTTP request limits.  Geometry
# can be large, but metadata and the number of independently addressable files
# stay bounded so validation cannot be used as an unbounded allocation primitive.
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
MAX_FILE_COUNT = 2048
MAX_JSON_DEPTH = 64
MAX_JSON_ITEMS = 250_000
MAX_BUNDLE_PATH_DEPTH = 32
MAX_BUNDLE_DIRECTORY_COUNT = 2048
MAX_BUNDLE_ACTUAL_ENTRIES = MAX_FILE_COUNT + MAX_BUNDLE_DIRECTORY_COUNT + 1

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_SAFE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_WINDOWS_RESERVED = {
    "con", "prn", "aux", "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}
_ALLOWED_SUFFIXES = {".json", ".glb", ".png", ".jpg", ".jpeg", ".webp"}
_ALLOWED_MEDIA_TYPES = {
    "application/json",
    "model/gltf-binary",
    "image/png",
    "image/jpeg",
    "image/webp",
}
_SUFFIX_MEDIA_TYPES = {
    ".json": {"application/json"},
    ".glb": {"model/gltf-binary"},
    ".png": {"image/png"},
    ".jpg": {"image/jpeg"},
    ".jpeg": {"image/jpeg"},
    ".webp": {"image/webp"},
}


class SyncProtocolError(ValueError):
    """A deterministic validation failure suitable for a rejected receipt."""

    def __init__(self, message: str, *, code: str = "invalid_bundle") -> None:
        super().__init__(message)
        self.code = code


class SyncStatus(str, Enum):
    ACCEPTED = "accepted"
    QUEUED = "queued"
    CONFLICTS = "conflicts"
    REJECTED = "rejected"


class ConflictResolution(str, Enum):
    KEEP_WEBTOON_OVERRIDE = "keep_webtoon_override"
    USE_BLENDER_VALUE = "use_blender_value"


def _require_exact_keys(
    value: Mapping[str, Any], *, required: set[str], optional: set[str], label: str,
) -> None:
    missing = required.difference(value)
    unknown = set(value).difference(required | optional)
    if missing:
        raise SyncProtocolError(
            f"{label} is missing required fields: {', '.join(sorted(missing))}",
            code="malformed_manifest",
        )
    if unknown:
        raise SyncProtocolError(
            f"{label} contains unknown fields: {', '.join(sorted(unknown))}",
            code="future_schema",
        )


def _require_string(value: Any, label: str, *, maximum: int = 1024) -> str:
    if not isinstance(value, str) or not value or len(value) > maximum:
        raise SyncProtocolError(f"{label} must be a non-empty string", code="malformed_manifest")
    return value


def _require_safe_id(value: Any, label: str) -> str:
    text = _require_string(value, label, maximum=128)
    if not _SAFE_ID_RE.fullmatch(text):
        raise SyncProtocolError(f"{label} contains unsafe characters", code="invalid_identity")
    return text


def _require_uuid(value: Any, label: str) -> str:
    text = _require_string(value, label, maximum=64)
    try:
        return str(uuid.UUID(text))
    except (ValueError, AttributeError) as exc:
        raise SyncProtocolError(f"{label} must be a UUID", code="invalid_identity") from exc


def _require_nonnegative_int(value: Any, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise SyncProtocolError(f"{label} must be a non-negative integer", code="malformed_manifest")
    return value


def _reject_constant(value: str) -> None:
    raise SyncProtocolError(f"Non-finite JSON number {value!r} is forbidden", code="malformed_json")


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise SyncProtocolError(f"Duplicate JSON key {key!r}", code="malformed_json")
        result[key] = value
    return result


def strict_json_loads(data: bytes | str, *, label: str = "JSON") -> Any:
    """Decode JSON while rejecting duplicate keys, NaN, and excessive trees."""

    if isinstance(data, bytes):
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise SyncProtocolError(f"{label} is not UTF-8", code="malformed_json") from exc
    else:
        text = data
    try:
        value = json.loads(
            text,
            object_pairs_hook=_pairs_without_duplicates,
            parse_constant=_reject_constant,
        )
    except SyncProtocolError:
        raise
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SyncProtocolError(f"{label} is malformed JSON: {exc}", code="malformed_json") from exc
    _validate_json_tree(value, label=label)
    return value


def _validate_json_tree(value: Any, *, label: str = "value") -> None:
    item_count = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        item_count += 1
        if item_count > MAX_JSON_ITEMS:
            raise SyncProtocolError(f"{label} contains too many values", code="size_limit")
        if depth > MAX_JSON_DEPTH:
            raise SyncProtocolError(f"{label} is nested too deeply", code="size_limit")
        if current is None or isinstance(current, (str, bool, int)):
            continue
        if isinstance(current, float):
            if not math.isfinite(current):
                raise SyncProtocolError(f"{label} contains a non-finite number", code="malformed_json")
            continue
        if isinstance(current, Mapping):
            for key, child in current.items():
                if not isinstance(key, str):
                    raise SyncProtocolError(f"{label} has a non-string object key", code="malformed_json")
                stack.append((child, depth + 1))
            continue
        if isinstance(current, (list, tuple)):
            stack.extend((child, depth + 1) for child in current)
            continue
        raise SyncProtocolError(
            f"{label} contains unsupported value {type(current).__name__}",
            code="malformed_json",
        )


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    _validate_json_tree(value)
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def validate_relative_bundle_path(value: Any) -> str:
    text = _require_string(value, "bundle file path", maximum=512)
    if "\\" in text or "\x00" in text or text.startswith("/"):
        raise SyncProtocolError(f"Unsafe bundle path {text!r}", code="path_traversal")
    path = PurePosixPath(text)
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise SyncProtocolError(f"Unsafe bundle path {text!r}", code="path_traversal")
    if len(path.parts) > MAX_BUNDLE_PATH_DEPTH:
        raise SyncProtocolError(f"Bundle path is nested too deeply: {text!r}", code="size_limit")
    for part in path.parts:
        if part.endswith((" ", ".")) or ":" in part:
            raise SyncProtocolError(f"Unsafe bundle path component {part!r}", code="path_traversal")
        if part.split(".", 1)[0].casefold() in _WINDOWS_RESERVED:
            raise SyncProtocolError(f"Reserved bundle path component {part!r}", code="path_traversal")
    if path.suffix.casefold() not in _ALLOWED_SUFFIXES:
        raise SyncProtocolError(f"Executable or unsupported bundle file {text!r}", code="forbidden_file")
    return path.as_posix()


@dataclass(frozen=True)
class BundleFile:
    path: str
    sha256: str
    size: int
    media_type: str

    def __post_init__(self) -> None:
        object.__setattr__(self, "path", validate_relative_bundle_path(self.path))
        if not isinstance(self.sha256, str) or not _SHA256_RE.fullmatch(self.sha256):
            raise SyncProtocolError("File sha256 must be 64 lowercase hexadecimal characters")
        if isinstance(self.size, bool) or not isinstance(self.size, int) or not 0 <= self.size <= MAX_FILE_BYTES:
            raise SyncProtocolError("File size exceeds the per-file limit", code="size_limit")
        if self.media_type not in _ALLOWED_MEDIA_TYPES:
            raise SyncProtocolError(f"Unsupported media type {self.media_type!r}", code="forbidden_file")
        suffix = PurePosixPath(self.path).suffix.casefold()
        if self.media_type not in _SUFFIX_MEDIA_TYPES[suffix]:
            raise SyncProtocolError(f"Media type does not match {suffix} content", code="forbidden_file")

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "sha256": self.sha256,
            "size": self.size,
            "media_type": self.media_type,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "BundleFile":
        if not isinstance(value, Mapping):
            raise SyncProtocolError("Bundle file entry must be an object")
        _require_exact_keys(
            value, required={"path", "sha256", "size", "media_type"},
            optional=set(), label="bundle file entry",
        )
        return cls(
            path=value["path"], sha256=value["sha256"], size=value["size"],
            media_type=value["media_type"],
        )


@dataclass(frozen=True)
class SyncBundle:
    """A complete Blender publication transaction.

    Geometry is referenced through ``files`` and ``cache_manifest``; it is never
    embedded in ``frame_data``.  ``bundle_sha256`` authenticates manifest
    integrity after transport, while the random loopback bearer token authenticates
    the process that sends the notification.
    """

    transaction_id: str
    series_id: str
    chapter_id: str
    comic_frame_id: str
    blender_file_uuid: str
    base_revision: int
    source_revision: int
    created_at: float
    chapter_data: Mapping[str, Any]
    frame_data: Mapping[str, Any]
    cache_manifest: Mapping[str, Any]
    files: tuple[BundleFile, ...] = ()
    warnings: tuple[str, ...] = ()
    bundle_sha256: str = ""
    schema_version: int = SYNC_SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.schema_version != SYNC_SCHEMA_VERSION:
            code = "future_schema" if self.schema_version > SYNC_SCHEMA_VERSION else "unsupported_schema"
            raise SyncProtocolError(
                f"Sync schema {self.schema_version} is unsupported", code=code,
            )
        object.__setattr__(self, "transaction_id", _require_uuid(self.transaction_id, "transaction_id"))
        object.__setattr__(self, "blender_file_uuid", _require_uuid(self.blender_file_uuid, "blender_file_uuid"))
        object.__setattr__(self, "series_id", _require_safe_id(self.series_id, "series_id"))
        object.__setattr__(self, "chapter_id", _require_safe_id(self.chapter_id, "chapter_id"))
        object.__setattr__(self, "comic_frame_id", _require_safe_id(self.comic_frame_id, "comic_frame_id"))
        _require_nonnegative_int(self.base_revision, "base_revision")
        _require_nonnegative_int(self.source_revision, "source_revision")
        # Blender source revisions and Webtoon's chapter CAS revisions are
        # independent monotonic counters. Older v1 writers used base + 1,
        # which remains valid, but neither counter is ordered against the other.
        if self.source_revision <= 0:
            raise SyncProtocolError("source_revision must be positive", code="stale_revision")
        if isinstance(self.created_at, bool) or not isinstance(self.created_at, (int, float)) or not math.isfinite(self.created_at):
            raise SyncProtocolError("created_at must be a finite number")
        for label, payload in (
            ("chapter_data", self.chapter_data),
            ("frame_data", self.frame_data),
            ("cache_manifest", self.cache_manifest),
        ):
            if not isinstance(payload, Mapping):
                raise SyncProtocolError(f"{label} must be an object")
            payload_version = payload.get("schema_version")
            if isinstance(payload_version, bool) or not isinstance(payload_version, int):
                raise SyncProtocolError(f"{label} requires an integer schema_version", code="malformed_manifest")
            if payload_version != 1:
                code = "future_schema" if payload_version > 1 else "unsupported_schema"
                raise SyncProtocolError(f"{label} schema {payload_version} is unsupported", code=code)
            _validate_json_tree(payload, label=label)
        if len(self.files) > MAX_FILE_COUNT:
            raise SyncProtocolError("Bundle contains too many files", code="size_limit")
        paths = [entry.path for entry in self.files]
        if len(paths) != len(set(paths)):
            raise SyncProtocolError("Bundle declares a file more than once", code="malformed_manifest")
        if sum(entry.size for entry in self.files) > MAX_BUNDLE_BYTES:
            raise SyncProtocolError("Bundle exceeds the total size limit", code="size_limit")
        for warning in self.warnings:
            _require_string(warning, "warning", maximum=4096)
        if self.bundle_sha256 and not _SHA256_RE.fullmatch(self.bundle_sha256):
            raise SyncProtocolError("bundle_sha256 must be lowercase SHA-256")

    def _dict_without_digest(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "series_id": self.series_id,
            "chapter_id": self.chapter_id,
            "comic_frame_id": self.comic_frame_id,
            "blender_file_uuid": self.blender_file_uuid,
            "base_revision": self.base_revision,
            "source_revision": self.source_revision,
            "created_at": self.created_at,
            "chapter_data": dict(self.chapter_data),
            "frame_data": dict(self.frame_data),
            "cache_manifest": dict(self.cache_manifest),
            "files": [entry.to_dict() for entry in self.files],
            "warnings": list(self.warnings),
        }

    def calculated_digest(self) -> str:
        return hashlib.sha256(canonical_json_bytes(self._dict_without_digest())).hexdigest()

    def source_digest(self) -> str:
        """Fingerprint Blender-owned publication content across transaction IDs."""

        frame_data = dict(self.frame_data)
        # The common base is Webtoon CAS context, not Blender source content.
        frame_data.pop("base_state", None)
        payload = {
            "schema_version": self.schema_version,
            "series_id": self.series_id,
            "chapter_id": self.chapter_id,
            "comic_frame_id": self.comic_frame_id,
            "blender_file_uuid": self.blender_file_uuid,
            "source_revision": self.source_revision,
            "chapter_data": dict(self.chapter_data),
            "frame_data": frame_data,
            "cache_manifest": dict(self.cache_manifest),
            "files": [
                entry.to_dict() for entry in sorted(
                    self.files, key=lambda item: item.path,
                )
            ],
            "warnings": list(self.warnings),
        }
        return hashlib.sha256(canonical_json_bytes(payload)).hexdigest()

    def with_calculated_digest(self) -> "SyncBundle":
        return replace(self, bundle_sha256=self.calculated_digest())

    def to_dict(self) -> dict[str, Any]:
        value = self._dict_without_digest()
        value["bundle_sha256"] = self.bundle_sha256 or self.calculated_digest()
        return value

    @classmethod
    def from_dict(cls, value: Any) -> "SyncBundle":
        if not isinstance(value, Mapping):
            raise SyncProtocolError("Bundle manifest must be an object")
        required = {
            "schema_version", "transaction_id", "series_id", "chapter_id",
            "comic_frame_id", "blender_file_uuid", "base_revision",
            "source_revision", "created_at", "chapter_data", "frame_data",
            "cache_manifest", "files", "warnings", "bundle_sha256",
        }
        _require_exact_keys(value, required=required, optional=set(), label="bundle manifest")
        if isinstance(value["schema_version"], bool) or not isinstance(value["schema_version"], int):
            raise SyncProtocolError("schema_version must be an integer")
        if not isinstance(value["files"], list) or not isinstance(value["warnings"], list):
            raise SyncProtocolError("files and warnings must be arrays")
        bundle = cls(
            schema_version=value["schema_version"],
            transaction_id=value["transaction_id"],
            series_id=value["series_id"],
            chapter_id=value["chapter_id"],
            comic_frame_id=value["comic_frame_id"],
            blender_file_uuid=value["blender_file_uuid"],
            base_revision=value["base_revision"],
            source_revision=value["source_revision"],
            created_at=value["created_at"],
            chapter_data=value["chapter_data"],
            frame_data=value["frame_data"],
            cache_manifest=value["cache_manifest"],
            files=tuple(BundleFile.from_dict(entry) for entry in value["files"]),
            warnings=tuple(value["warnings"]),
            bundle_sha256=value["bundle_sha256"],
        )
        if bundle.bundle_sha256 != bundle.calculated_digest():
            raise SyncProtocolError("Bundle manifest digest does not match", code="hash_mismatch")
        return bundle


@dataclass(frozen=True)
class ConflictDescriptor:
    category: str
    path: str
    base_value: Any
    webtoon_value: Any
    blender_value: Any
    default_resolution: ConflictResolution = ConflictResolution.KEEP_WEBTOON_OVERRIDE

    def __post_init__(self) -> None:
        _require_safe_id(self.category, "conflict category")
        _require_string(self.path, "conflict path", maximum=1024)
        for label, value in (
            ("base_value", self.base_value),
            ("webtoon_value", self.webtoon_value),
            ("blender_value", self.blender_value),
        ):
            _validate_json_tree(value, label=label)

    def to_dict(self) -> dict[str, Any]:
        return {
            "category": self.category,
            "path": self.path,
            "base_value": self.base_value,
            "webtoon_value": self.webtoon_value,
            "blender_value": self.blender_value,
            "default_resolution": self.default_resolution.value,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "ConflictDescriptor":
        if not isinstance(value, Mapping):
            raise SyncProtocolError("Conflict descriptor must be an object")
        _require_exact_keys(
            value,
            required={
                "category", "path", "base_value", "webtoon_value",
                "blender_value", "default_resolution",
            },
            optional=set(), label="conflict descriptor",
        )
        try:
            resolution = ConflictResolution(value["default_resolution"])
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("Unknown conflict resolution") from exc
        return cls(
            category=value["category"], path=value["path"],
            base_value=value["base_value"], webtoon_value=value["webtoon_value"],
            blender_value=value["blender_value"], default_resolution=resolution,
        )


@dataclass(frozen=True)
class SyncReceipt:
    transaction_id: str
    status: SyncStatus
    accepted_revision: int | None = None
    conflicts: tuple[ConflictDescriptor, ...] = ()
    warnings: tuple[str, ...] = ()
    errors: tuple[str, ...] = ()
    error_code: str | None = None
    schema_version: int = RECEIPT_SCHEMA_VERSION

    def __post_init__(self) -> None:
        object.__setattr__(self, "transaction_id", _require_uuid(self.transaction_id, "transaction_id"))
        if self.schema_version != RECEIPT_SCHEMA_VERSION:
            raise SyncProtocolError("Unsupported receipt schema", code="future_schema")
        if not isinstance(self.status, SyncStatus):
            try:
                object.__setattr__(self, "status", SyncStatus(self.status))
            except (ValueError, TypeError) as exc:
                raise SyncProtocolError("Unknown receipt status") from exc
        if self.accepted_revision is not None:
            _require_nonnegative_int(self.accepted_revision, "accepted_revision")
        if self.status is SyncStatus.ACCEPTED and self.accepted_revision is None:
            raise SyncProtocolError("Accepted receipts require accepted_revision")
        if self.status is SyncStatus.CONFLICTS and not self.conflicts:
            raise SyncProtocolError("Conflict receipts require conflict descriptors")
        for message in (*self.warnings, *self.errors):
            _require_string(message, "receipt message", maximum=4096)
        if self.error_code is not None:
            _require_safe_id(self.error_code, "error_code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": self.schema_version,
            "transaction_id": self.transaction_id,
            "status": self.status.value,
            "accepted_revision": self.accepted_revision,
            "conflicts": [item.to_dict() for item in self.conflicts],
            "warnings": list(self.warnings),
            "errors": list(self.errors),
            "error_code": self.error_code,
        }

    @classmethod
    def from_dict(cls, value: Any) -> "SyncReceipt":
        if not isinstance(value, Mapping):
            raise SyncProtocolError("Sync receipt must be an object")
        required = {
            "schema_version", "transaction_id", "status", "accepted_revision",
            "conflicts", "warnings", "errors", "error_code",
        }
        _require_exact_keys(value, required=required, optional=set(), label="sync receipt")
        if not isinstance(value["conflicts"], list):
            raise SyncProtocolError("Receipt conflicts must be an array")
        if not isinstance(value["warnings"], list) or not isinstance(value["errors"], list):
            raise SyncProtocolError("Receipt messages must be arrays")
        try:
            status = SyncStatus(value["status"])
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError("Unknown receipt status") from exc
        return cls(
            schema_version=value["schema_version"],
            transaction_id=value["transaction_id"], status=status,
            accepted_revision=value["accepted_revision"],
            conflicts=tuple(ConflictDescriptor.from_dict(item) for item in value["conflicts"]),
            warnings=tuple(value["warnings"]), errors=tuple(value["errors"]),
            error_code=value["error_code"],
        )

    @classmethod
    def rejected(cls, transaction_id: str, error: Exception) -> "SyncReceipt":
        return cls(
            transaction_id=transaction_id,
            status=SyncStatus.REJECTED,
            errors=(str(error),),
            error_code=getattr(error, "code", "internal_error"),
        )


@dataclass(frozen=True)
class ValidatedBundle:
    bundle: SyncBundle
    root: Path
    files: Mapping[str, Path]


def _is_link_or_reparse(path: Path) -> bool:
    """Reject symlinks, NTFS junctions, and other reparse-point entries."""

    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
        return bool(attributes & reparse_flag)
    except OSError:
        # The caller will report the disappearing/inaccessible path through its
        # normal partial-bundle boundary.
        return False


def _has_multiple_hard_links(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_nlink > 1
    except OSError:
        return False


def _resolve_contained(root: Path, relative: str) -> Path:
    candidate = root.joinpath(*PurePosixPath(relative).parts)
    if _is_link_or_reparse(candidate):
        raise SyncProtocolError(f"Bundle member is a filesystem link: {relative}", code="path_traversal")
    if _has_multiple_hard_links(candidate):
        raise SyncProtocolError(f"Bundle member is a hard-linked file: {relative}", code="path_traversal")
    try:
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root.resolve(strict=True))
    except (OSError, ValueError) as exc:
        raise SyncProtocolError(f"Bundle path escapes its ready directory: {relative}", code="path_traversal") from exc
    if not resolved.is_file():
        raise SyncProtocolError(f"Bundle member is not a regular file: {relative}", code="forbidden_file")
    return resolved


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _reject_external_uris(value: Any, *, label: str) -> None:
    stack = [value]
    while stack:
        current = stack.pop()
        if isinstance(current, Mapping):
            for key, child in current.items():
                if key.casefold() == "uri" and isinstance(child, str) and child:
                    raise SyncProtocolError(f"{label} contains an external URI", code="external_uri")
                stack.append(child)
        elif isinstance(current, list):
            stack.extend(current)


def _validate_json_file(path: Path) -> None:
    size = path.stat().st_size
    if size > MAX_JSON_BYTES:
        raise SyncProtocolError(f"JSON file {path.name} exceeds metadata limit", code="size_limit")
    value = strict_json_loads(path.read_bytes(), label=path.name)
    _reject_external_uris(value, label=path.name)


def _validate_glb(path: Path) -> None:
    """Validate the GLB container and reject URI-backed buffers or images."""

    size = path.stat().st_size
    if size < 20:
        raise SyncProtocolError(f"GLB file {path.name} is truncated", code="malformed_glb")
    with path.open("rb") as handle:
        header = handle.read(12)
        magic, version, declared_length = struct.unpack("<4sII", header)
        if magic != b"glTF" or version != 2 or declared_length != size:
            raise SyncProtocolError(f"GLB file {path.name} has an invalid header", code="malformed_glb")
        offset = 12
        first = True
        while offset < size:
            chunk_header = handle.read(8)
            if len(chunk_header) != 8:
                raise SyncProtocolError(f"GLB file {path.name} has a truncated chunk", code="malformed_glb")
            chunk_length, chunk_type = struct.unpack("<II", chunk_header)
            offset += 8
            if chunk_length % 4 or offset + chunk_length > size:
                raise SyncProtocolError(f"GLB file {path.name} has invalid chunk bounds", code="malformed_glb")
            if first:
                if chunk_type != 0x4E4F534A or chunk_length > MAX_JSON_BYTES:
                    raise SyncProtocolError(f"GLB file {path.name} lacks a bounded JSON chunk", code="malformed_glb")
                document = strict_json_loads(handle.read(chunk_length).rstrip(b" \t\r\n\x00"), label=path.name)
                _reject_external_uris(document, label=path.name)
            else:
                handle.seek(chunk_length, os.SEEK_CUR)
            offset += chunk_length
            first = False
        if first or offset != size:
            raise SyncProtocolError(f"GLB file {path.name} is malformed", code="malformed_glb")


def _validate_image(path: Path, suffix: str) -> None:
    """Perform bounded magic checks for bundle image members."""

    size = path.stat().st_size
    with path.open("rb") as handle:
        header = handle.read(12)
        if suffix == ".png":
            valid = header.startswith(b"\x89PNG\r\n\x1a\n")
        elif suffix in {".jpg", ".jpeg"}:
            if size < 5 or not header.startswith(b"\xff\xd8\xff"):
                valid = False
            else:
                handle.seek(-2, os.SEEK_END)
                valid = handle.read(2) == b"\xff\xd9"
        else:  # WebP RIFF container.
            valid = (
                size >= 12
                and header[:4] == b"RIFF"
                and header[8:12] == b"WEBP"
                and int.from_bytes(header[4:8], "little") == size - 8
            )
    if not valid:
        raise SyncProtocolError(
            f"Image file {path.name} does not match its declared type",
            code="forbidden_file",
        )


def _actual_bundle_files(root: Path) -> set[str]:
    result: set[str] = set()
    stack: list[tuple[Path, int]] = [(root, 0)]
    directory_count = 1
    entry_count = 0
    file_count = 0
    while stack:
        base, depth = stack.pop()
        try:
            entries = os.scandir(base)
        except OSError as exc:
            raise SyncProtocolError(
                "Bundle directory cannot be enumerated", code="partial_bundle",
            ) from exc
        with entries:
            for entry in entries:
                entry_count += 1
                if entry_count > MAX_BUNDLE_ACTUAL_ENTRIES:
                    raise SyncProtocolError(
                        "Bundle contains too many filesystem entries",
                        code="size_limit",
                    )
                child = Path(entry.path)
                if _is_link_or_reparse(child):
                    raise SyncProtocolError(
                        "Bundle contains a linked or reparse-point entry",
                        code="path_traversal",
                    )
                if entry.is_dir(follow_symlinks=False):
                    child_depth = depth + 1
                    if child_depth > MAX_BUNDLE_PATH_DEPTH:
                        raise SyncProtocolError(
                            "Bundle directory tree is nested too deeply",
                            code="size_limit",
                        )
                    directory_count += 1
                    if directory_count > MAX_BUNDLE_DIRECTORY_COUNT:
                        raise SyncProtocolError(
                            "Bundle contains too many directories",
                            code="size_limit",
                        )
                    stack.append((child, child_depth))
                    continue
                if not entry.is_file(follow_symlinks=False):
                    raise SyncProtocolError(
                        "Bundle contains a non-regular filesystem entry",
                        code="forbidden_file",
                    )
                try:
                    link_count = entry.stat(follow_symlinks=False).st_nlink
                except OSError as exc:
                    raise SyncProtocolError(
                        "Bundle file metadata is unavailable",
                        code="partial_bundle",
                    ) from exc
                if link_count > 1:
                    raise SyncProtocolError(
                        "Bundle contains a hard-linked file",
                        code="path_traversal",
                    )
                file_count += 1
                if file_count > MAX_FILE_COUNT + 1:  # plus bundle.json
                    raise SyncProtocolError(
                        "Bundle contains too many files", code="size_limit",
                    )
                relative = child.relative_to(root).as_posix()
                if relative != BUNDLE_MANIFEST:
                    result.add(validate_relative_bundle_path(relative))
    return result


def validate_bundle_directory(
    ready_directory: str | Path,
    *,
    expected_series_id: str,
    expected_chapter_id: str,
    expected_blender_file_uuid: str | None,
) -> ValidatedBundle:
    """Fully validate a ready directory without mutating application state."""

    root = Path(ready_directory)
    if not root.name.endswith(".ready") or _is_link_or_reparse(root) or not root.is_dir():
        raise SyncProtocolError("Sync transaction is not an atomic .ready directory", code="partial_bundle")
    try:
        root = root.resolve(strict=True)
    except OSError as exc:
        raise SyncProtocolError("Ready directory is unavailable", code="partial_bundle") from exc
    manifest_path = root / BUNDLE_MANIFEST
    if (
        _is_link_or_reparse(manifest_path)
        or _has_multiple_hard_links(manifest_path)
        or not manifest_path.is_file()
    ):
        raise SyncProtocolError("Ready directory has no regular bundle.json", code="partial_bundle")
    if manifest_path.stat().st_size > MAX_JSON_BYTES:
        raise SyncProtocolError("Bundle manifest exceeds metadata limit", code="size_limit")
    bundle = SyncBundle.from_dict(strict_json_loads(manifest_path.read_bytes(), label=BUNDLE_MANIFEST))
    _reject_external_uris(bundle.chapter_data, label="chapter_data")
    _reject_external_uris(bundle.frame_data, label="frame_data")
    _reject_external_uris(bundle.cache_manifest, label="cache_manifest")
    if root.name != f"{bundle.transaction_id}.ready":
        raise SyncProtocolError("Ready directory name does not match transaction_id", code="invalid_identity")
    if bundle.series_id != _require_safe_id(expected_series_id, "expected_series_id"):
        raise SyncProtocolError("Bundle targets a different series", code="wrong_identity")
    if bundle.chapter_id != _require_safe_id(expected_chapter_id, "expected_chapter_id"):
        raise SyncProtocolError("Bundle targets a different chapter", code="wrong_identity")
    if expected_blender_file_uuid is not None:
        expected_uuid = _require_uuid(expected_blender_file_uuid, "expected_blender_file_uuid")
        if bundle.blender_file_uuid != expected_uuid:
            raise SyncProtocolError("Bundle comes from a different Blender file", code="wrong_identity")

    declared = {entry.path for entry in bundle.files}
    actual = _actual_bundle_files(root)
    if declared != actual:
        missing = sorted(declared - actual)
        extra = sorted(actual - declared)
        detail = []
        if missing:
            detail.append(f"missing {missing}")
        if extra:
            detail.append(f"undeclared {extra}")
        raise SyncProtocolError("Bundle file set mismatch: " + "; ".join(detail), code="partial_bundle")

    validated: dict[str, Path] = {}
    total = 0
    for entry in bundle.files:
        path = _resolve_contained(root, entry.path)
        actual_size = path.stat().st_size
        total += actual_size
        if actual_size != entry.size:
            raise SyncProtocolError(f"Size mismatch for {entry.path}", code="hash_mismatch")
        if total > MAX_BUNDLE_BYTES:
            raise SyncProtocolError("Bundle exceeds total size limit", code="size_limit")
        if _sha256_file(path) != entry.sha256:
            raise SyncProtocolError(f"Hash mismatch for {entry.path}", code="hash_mismatch")
        suffix = path.suffix.casefold()
        if suffix == ".json":
            _validate_json_file(path)
        elif suffix == ".glb":
            _validate_glb(path)
        elif suffix in {".png", ".jpg", ".jpeg", ".webp"}:
            _validate_image(path, suffix)
        validated[entry.path] = path
    return ValidatedBundle(bundle=bundle, root=root, files=validated)


_MISSING_WIRE = {"$webtoon_sync_missing": True}
_BLENDER_AUTHORITATIVE = {"geometry", "hierarchy", "material_assignments", "freestyle"}
_WEBTOON_AUTHORITATIVE = {"renderer_settings", "projection", "drawing_materials", "local_entities"}
_CATEGORY_ALIASES = {
    "object_states": "transforms",
    "transforms": "transforms",
    "poses": "poses",
    "shape_keys": "shape_keys",
    "visibility": "visibility",
    "collection_visibility": "collections",
    "collections": "collections",
    "lights": "lights",
    "cameras": "cameras",
}


def _wire_value(value: Any) -> Any:
    return _MISSING_WIRE if value is _MISSING else value


_MISSING = object()


def _category(path: tuple[str, ...]) -> str:
    return _CATEGORY_ALIASES.get(path[0] if path else "state", "metadata")


def _join_path(path: tuple[str, ...]) -> str:
    return "/" + "/".join(part.replace("~", "~0").replace("/", "~1") for part in path)


def three_way_merge_frame_state(
    base_state: Mapping[str, Any],
    webtoon_state: Mapping[str, Any],
    blender_state: Mapping[str, Any],
) -> tuple[dict[str, Any], tuple[ConflictDescriptor, ...]]:
    """Merge frame state according to ownership and return grouped conflicts.

    Dictionaries are merged recursively; arrays are atomic values.  Conflicting
    shared fields retain Webtoon's value by default.  Geometry/hierarchy always
    follows Blender, while renderer/material/local-entity presentation always
    follows Webtoon.
    """

    for label, value in (
        ("base_state", base_state),
        ("webtoon_state", webtoon_state),
        ("blender_state", blender_state),
    ):
        if not isinstance(value, Mapping):
            raise SyncProtocolError(f"{label} must be an object")
        _validate_json_tree(value, label=label)
    conflicts: list[ConflictDescriptor] = []

    def merge(base: Any, current: Any, incoming: Any, path: tuple[str, ...]) -> Any:
        root = path[0] if path else ""
        if root in _BLENDER_AUTHORITATIVE:
            return incoming
        if root in _WEBTOON_AUTHORITATIVE:
            return current
        mappings = [value for value in (base, current, incoming) if isinstance(value, Mapping)]
        if mappings and all(value is _MISSING or isinstance(value, Mapping) for value in (base, current, incoming)):
            keys: set[str] = set()
            for value in mappings:
                keys.update(value)
            result: dict[str, Any] = {}
            for key in sorted(keys):
                child = merge(
                    base.get(key, _MISSING) if isinstance(base, Mapping) else _MISSING,
                    current.get(key, _MISSING) if isinstance(current, Mapping) else _MISSING,
                    incoming.get(key, _MISSING) if isinstance(incoming, Mapping) else _MISSING,
                    (*path, key),
                )
                if child is not _MISSING:
                    result[key] = child
            return result
        if current == incoming:
            return current
        if current == base:
            return incoming
        if incoming == base:
            return current
        conflicts.append(ConflictDescriptor(
            category=_category(path),
            path=_join_path(path),
            base_value=_wire_value(base),
            webtoon_value=_wire_value(current),
            blender_value=_wire_value(incoming),
        ))
        return current

    result = merge(base_state, webtoon_state, blender_state, ())
    assert isinstance(result, dict)
    return result, tuple(conflicts)


def grouped_conflicts(
    conflicts: Iterable[ConflictDescriptor],
) -> dict[str, tuple[ConflictDescriptor, ...]]:
    grouped: dict[str, list[ConflictDescriptor]] = {}
    for conflict in conflicts:
        grouped.setdefault(conflict.category, []).append(conflict)
    return {category: tuple(items) for category, items in sorted(grouped.items())}


def resolve_conflicts(
    merged_state: Mapping[str, Any],
    conflicts: Sequence[ConflictDescriptor],
    choices: Mapping[str, ConflictResolution | str],
) -> dict[str, Any]:
    """Apply explicit conflict choices to a merge result.

    A choice may be keyed by exact JSON-pointer path or category.  Exact paths
    win; omitted choices use ``keep_webtoon_override``.
    """

    result = json.loads(json.dumps(merged_state))
    for conflict in conflicts:
        raw_choice = choices.get(conflict.path, choices.get(conflict.category, conflict.default_resolution))
        try:
            choice = ConflictResolution(raw_choice)
        except (ValueError, TypeError) as exc:
            raise SyncProtocolError(f"Unknown resolution for {conflict.path}") from exc
        if choice is ConflictResolution.KEEP_WEBTOON_OVERRIDE:
            continue
        parts = [part.replace("~1", "/").replace("~0", "~") for part in conflict.path.split("/")[1:]]
        if not parts:
            raise SyncProtocolError("Root conflict cannot be resolved by path")
        target = result
        for part in parts[:-1]:
            child = target.get(part)
            if not isinstance(child, dict):
                child = {}
                target[part] = child
            target = child
        value = conflict.blender_value
        if value == _MISSING_WIRE:
            target.pop(parts[-1], None)
        else:
            target[parts[-1]] = value
    return result


__all__ = [
    "BUNDLE_MANIFEST",
    "BundleFile",
    "ConflictDescriptor",
    "ConflictResolution",
    "MAX_BUNDLE_BYTES",
    "MAX_FILE_BYTES",
    "MAX_JSON_BYTES",
    "MAX_FILE_COUNT",
    "MAX_BUNDLE_PATH_DEPTH",
    "RECEIPT_SCHEMA_VERSION",
    "SYNC_SCHEMA_VERSION",
    "SyncBundle",
    "SyncProtocolError",
    "SyncReceipt",
    "SyncStatus",
    "ValidatedBundle",
    "canonical_json_bytes",
    "grouped_conflicts",
    "resolve_conflicts",
    "strict_json_loads",
    "three_way_merge_frame_state",
    "validate_bundle_directory",
    "validate_relative_bundle_path",
]
