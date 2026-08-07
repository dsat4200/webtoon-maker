"""Standard-library writer for Webtoon Maker sync bundles.

Kept separate from Blender APIs so bundle publication can be unit tested without
launching Blender.
"""
from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
import time
from typing import Any, Mapping
import uuid


SYNC_SCHEMA_VERSION = 1
MAX_BUNDLE_BYTES = 512 * 1024 * 1024
MAX_FILE_BYTES = 256 * 1024 * 1024
MAX_FILE_COUNT = 2048
_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_MEDIA_TYPES = {
    ".json": "application/json",
    ".glb": "model/gltf-binary",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
}


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _safe_id(value: str, label: str) -> str:
    if not isinstance(value, str) or not _SAFE_ID.fullmatch(value):
        raise ValueError(f"{label} must be a safe non-empty identifier")
    return value


def _uuid(value: str, label: str) -> str:
    try:
        return str(uuid.UUID(value))
    except (ValueError, AttributeError) as exc:
        raise ValueError(f"{label} must be a UUID") from exc


def _relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Bundle paths must be non-empty POSIX paths")
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe bundle path: {value!r}")
    if len(path.parts) > 32:
        raise ValueError("Bundle path is nested too deeply")
    if path.suffix.casefold() not in _MEDIA_TYPES:
        raise ValueError(f"Unsupported bundle file type: {path.suffix}")
    return path.as_posix()


def _is_link_or_reparse(path: Path) -> bool:
    try:
        if path.is_symlink():
            return True
        is_junction = getattr(path, "is_junction", None)
        if is_junction is not None and is_junction():
            return True
        attributes = getattr(os.lstat(path), "st_file_attributes", 0)
        return bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    except OSError:
        return False


def _copy_hashed(source: Path, destination: Path) -> tuple[str, int]:
    if _is_link_or_reparse(source):
        raise ValueError(f"Source cannot be a filesystem link: {source}")
    source = source.resolve(strict=True)
    if not source.is_file():
        raise ValueError(f"Source is not a regular file: {source}")
    size = source.stat().st_size
    if size > MAX_FILE_BYTES:
        raise ValueError(f"Source exceeds per-file sync limit: {source.name}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256()
    copied = 0
    with source.open("rb") as input_handle, destination.open("xb") as output_handle:
        while chunk := input_handle.read(1024 * 1024):
            output_handle.write(chunk)
            digest.update(chunk)
            copied += len(chunk)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    if copied != size:
        raise OSError(f"Source changed while staging: {source}")
    return digest.hexdigest(), copied


def _write_exclusive_json(path: Path, value: Mapping[str, Any]) -> None:
    encoded = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True, indent=2,
    ).encode("utf-8")
    with path.open("xb") as handle:
        handle.write(encoded)
        handle.flush()
        os.fsync(handle.fileno())


@dataclass(frozen=True)
class ReadyBundle:
    transaction_id: str
    directory: Path
    bundle_sha256: str


def stage_ready_bundle(
    inbox_root: str | Path,
    *,
    series_id: str,
    chapter_id: str,
    comic_frame_id: str,
    blender_file_uuid: str,
    base_revision: int,
    source_revision: int,
    chapter_data: Mapping[str, Any],
    frame_data: Mapping[str, Any],
    cache_manifest: Mapping[str, Any],
    source_files: Mapping[str, str | Path],
    warnings: tuple[str, ...] | list[str] = (),
    transaction_id: str | None = None,
    created_at: float | None = None,
) -> ReadyBundle:
    """Copy, hash, and atomically publish one complete ``.ready`` directory."""

    series_id = _safe_id(series_id, "series_id")
    chapter_id = _safe_id(chapter_id, "chapter_id")
    comic_frame_id = _safe_id(comic_frame_id, "comic_frame_id")
    blender_file_uuid = _uuid(blender_file_uuid, "blender_file_uuid")
    transaction_id = _uuid(transaction_id or str(uuid.uuid4()), "transaction_id")
    if (
        isinstance(base_revision, bool) or not isinstance(base_revision, int)
        or isinstance(source_revision, bool) or not isinstance(source_revision, int)
        or base_revision < 0 or source_revision <= 0
    ):
        raise ValueError("base_revision must be non-negative and source_revision positive")
    if len(source_files) > MAX_FILE_COUNT:
        raise ValueError("Bundle contains too many files")
    normalized_sources: dict[str, Path] = {}
    for relative, source in source_files.items():
        normalized = _relative_path(relative)
        if normalized in normalized_sources:
            raise ValueError(f"Duplicate bundle path: {normalized}")
        normalized_sources[normalized] = Path(source)

    inbox = Path(inbox_root).expanduser()
    inbox.mkdir(parents=True, exist_ok=True)
    if _is_link_or_reparse(inbox):
        raise ValueError("Inbox root cannot be a filesystem link")
    staging = inbox / f".{transaction_id}.staging"
    ready = inbox / f"{transaction_id}.ready"
    if staging.exists() or ready.exists():
        raise FileExistsError(f"Transaction already exists: {transaction_id}")
    staging.mkdir()
    try:
        entries: list[dict[str, Any]] = []
        total = 0
        for relative, source in sorted(normalized_sources.items()):
            digest, size = _copy_hashed(source, staging.joinpath(*PurePosixPath(relative).parts))
            total += size
            if total > MAX_BUNDLE_BYTES:
                raise ValueError("Bundle exceeds total sync size limit")
            suffix = PurePosixPath(relative).suffix.casefold()
            entries.append({
                "path": relative,
                "sha256": digest,
                "size": size,
                "media_type": _MEDIA_TYPES[suffix],
            })
        manifest: dict[str, Any] = {
            "schema_version": SYNC_SCHEMA_VERSION,
            "transaction_id": transaction_id,
            "series_id": series_id,
            "chapter_id": chapter_id,
            "comic_frame_id": comic_frame_id,
            "blender_file_uuid": blender_file_uuid,
            "base_revision": base_revision,
            "source_revision": source_revision,
            "created_at": float(time.time() if created_at is None else created_at),
            "chapter_data": dict(chapter_data),
            "frame_data": dict(frame_data),
            "cache_manifest": dict(cache_manifest),
            "files": entries,
            "warnings": list(warnings),
        }
        digest = hashlib.sha256(canonical_json_bytes(manifest)).hexdigest()
        manifest["bundle_sha256"] = digest
        _write_exclusive_json(staging / "bundle.json", manifest)
        # Directory replacement is atomic on the same filesystem.  A consumer
        # never scans the hidden staging name and only opens the final suffix.
        os.replace(staging, ready)
        return ReadyBundle(transaction_id, ready, digest)
    except Exception:
        if staging.exists():
            shutil.rmtree(staging)
        raise


__all__ = ["ReadyBundle", "SYNC_SCHEMA_VERSION", "canonical_json_bytes", "stage_ready_bundle"]
