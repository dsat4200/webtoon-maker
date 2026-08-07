"""Filesystem repository for Blender chapter metadata and comic frames."""
from __future__ import annotations

import hashlib
import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

from .documents import (
    BlenderChapterDocument, CacheManifest, ComicFrameDocument,
)


BLENDER_DIR = "blender"
BLENDER_MANIFEST_FILE = "manifest.json"
FRAMES_DIR = "frames"
CACHE_DIR = "cache"
CACHE_BLOBS_DIR = "blobs"
CACHE_REVISIONS_DIR = "revisions"
INBOX_DIR = "inbox"
_SAFE_COMPONENT = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_MAX_MANIFEST_BYTES = 16 * 1024 * 1024
_MAX_CACHE_MANIFEST_BYTES = 64 * 1024 * 1024


def _atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(
                payload, handle, indent=2, ensure_ascii=False,
                allow_nan=False,
            )
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _safe_component(value: str, label: str) -> str:
    value = str(value)
    if not _SAFE_COMPONENT.fullmatch(value) or value.endswith((".", " ")):
        raise ValueError(f"Unsafe {label}: {value!r}")
    return value


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _read_json(path: Path, *, limit: int, label: str) -> Any:
    if path.is_symlink():
        raise ValueError(f"{label} cannot be a filesystem link")
    size = path.stat().st_size
    if size > limit:
        raise ValueError(f"{label} exceeds its size limit")
    return json.loads(path.read_text(encoding="utf-8"))


@dataclass
class BlenderSidecarData:
    document: BlenderChapterDocument
    frames: dict[str, ComicFrameDocument] = field(default_factory=dict)
    cache_manifest: CacheManifest | None = None
    warnings: list[str] = field(default_factory=list)
    unavailable_frame_ids: set[str] = field(default_factory=set)

    def add_frame(self, frame: ComicFrameDocument) -> ComicFrameDocument:
        frame.validate()
        if frame.chapter_id != self.document.chapter_id:
            raise ValueError("Comic frame belongs to a different chapter")
        if (
            frame.frame_id in self.frames
            or frame.frame_id in self.document.frame_ids
        ):
            raise ValueError("Comic frame ID already exists")
        self.frames[frame.frame_id] = frame
        self.document.frame_ids.append(frame.frame_id)
        self.document.revision += 1
        return frame

    def create_frame(
        self, frame_id: str | None = None, **values: Any,
    ) -> ComicFrameDocument:
        if frame_id is not None:
            values["frame_id"] = frame_id
        values.setdefault("chapter_id", self.document.chapter_id)
        return self.add_frame(ComicFrameDocument(**values))

    def duplicate_frame(
        self, source_frame_id: str, new_frame_id: str | None = None,
    ) -> ComicFrameDocument:
        source = self.frames[source_frame_id]
        payload = source.to_dict()
        payload["id"] = str(new_frame_id or "")
        if not payload["id"]:
            payload.pop("id")
        duplicate = ComicFrameDocument.from_dict(payload)
        return self.add_frame(duplicate)

    def delete_frame(self, frame_id: str) -> ComicFrameDocument | None:
        """Tombstone a frame ID even when its JSON was already unavailable."""
        frame = self.frames.pop(frame_id, None)
        if frame is None and frame_id not in self.document.frame_ids:
            raise KeyError(frame_id)
        self.unavailable_frame_ids.discard(frame_id)
        self.document.frame_ids = [
            item for item in self.document.frame_ids if item != frame_id
        ]
        self.document.revision += 1
        frame_tombstones = self.document.tombstones.setdefault("frames", {})
        if not isinstance(frame_tombstones, dict):
            frame_tombstones = {}
            self.document.tombstones["frames"] = frame_tombstones
        frame_tombstones[frame_id] = {
            "deleted_revision": self.document.revision,
        }
        return frame

    def validate(self) -> None:
        self.document.validate()
        frame_ids: set[str] = set()
        for key, frame in self.frames.items():
            frame.validate()
            if str(key) != frame.frame_id:
                raise ValueError("Comic frame map keys must match frame IDs")
            if frame.chapter_id != self.document.chapter_id:
                raise ValueError("Comic frame belongs to a different chapter")
            if frame.frame_id in frame_ids:
                raise ValueError("Duplicate comic frame ID")
            frame_ids.add(frame.frame_id)
        self.unavailable_frame_ids = {
            str(item) for item in self.unavailable_frame_ids
        }
        if self.unavailable_frame_ids & frame_ids:
            raise ValueError("Available comic frames cannot also be unavailable")
        if self.document.frame_ids:
            if set(self.document.frame_ids) != (
                frame_ids | self.unavailable_frame_ids
            ):
                raise ValueError("Blender manifest frame list does not match frame files")
        else:
            self.document.frame_ids = list(self.frames)
        if self.cache_manifest is not None:
            self.cache_manifest.validate()
            current = self.document.current_cache_revision
            if current is not None and current != self.cache_manifest.revision:
                raise ValueError("Current cache revision does not match cache manifest")
            self.document.current_cache_revision = self.cache_manifest.revision
            if self.cache_manifest.revision not in self.document.cache_revisions:
                self.document.cache_revisions.append(self.cache_manifest.revision)
        self.warnings = [str(item) for item in self.warnings]


class BlenderSidecarRepository:
    """Read and atomically publish a single chapter's ``blender/`` tree."""

    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()

    @property
    def manifest_path(self) -> Path:
        return self.root / BLENDER_MANIFEST_FILE

    @property
    def frames_root(self) -> Path:
        return self.root / FRAMES_DIR

    @property
    def blobs_root(self) -> Path:
        return self.root / CACHE_DIR / CACHE_BLOBS_DIR

    @property
    def revisions_root(self) -> Path:
        return self.root / CACHE_DIR / CACHE_REVISIONS_DIR

    def frame_path(self, frame_id: str) -> Path:
        return self.frames_root / f"{_safe_component(frame_id, 'frame ID')}.json"

    def cache_revision_path(self, revision: str) -> Path:
        return self.revisions_root / (
            f"{_safe_component(revision, 'cache revision')}.json"
        )

    def blob_path(self, digest: str) -> Path:
        digest = str(digest).lower()
        if len(digest) != 64 or any(
            character not in "0123456789abcdef" for character in digest
        ):
            raise ValueError("Cache blob names must be lowercase SHA-256 digests")
        return self.blobs_root / f"{digest}.glb"

    def write_blob(self, data: bytes, expected_hash: str | None = None) -> str:
        if not isinstance(data, bytes):
            raise TypeError("Cache blobs must be bytes")
        digest = _sha256(data)
        if expected_hash is not None and digest != str(expected_hash).lower():
            raise ValueError("Cache blob hash does not match its declared digest")
        destination = self.blob_path(digest)
        if destination.is_file():
            if destination.is_symlink():
                raise OSError(f"Cache blob cannot be a filesystem link: {digest}")
            if _sha256(destination.read_bytes()) != digest:
                raise OSError(f"Existing cache blob is corrupt: {digest}")
            return digest
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_name(f".{destination.name}.{os.getpid()}.tmp")
        try:
            with temporary.open("xb") as handle:
                handle.write(data)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, destination)
        finally:
            temporary.unlink(missing_ok=True)
        return digest

    def save(
        self,
        sidecar: BlenderSidecarData,
        *,
        blobs: Mapping[str, bytes | str | Path] | None = None,
        fallback_blob_roots: Iterable[str | Path] = (),
    ) -> None:
        """Publish immutable blobs first, frame records next, and manifest last."""
        sidecar.validate()
        supplied = blobs or {}
        for declared_hash, source in supplied.items():
            if isinstance(source, bytes):
                data = source
            else:
                data = Path(source).read_bytes()
            self.write_blob(data, str(declared_hash))

        if sidecar.cache_manifest is not None:
            required = set(sidecar.cache_manifest.object_resources.values())
            required.update(sidecar.cache_manifest.baked_variants.values())
            if sidecar.cache_manifest.base_glb_hash:
                required.add(sidecar.cache_manifest.base_glb_hash)
            missing = [
                digest for digest in required
                if not self.blob_path(digest).is_file()
            ]
            for digest in list(missing):
                for fallback_root in fallback_blob_roots:
                    candidate = Path(fallback_root) / f"{digest}.glb"
                    if not candidate.is_file():
                        continue
                    self.write_blob(candidate.read_bytes(), digest)
                    missing.remove(digest)
                    break
            if missing:
                raise FileNotFoundError(
                    "Cache manifest references missing blobs: " + ", ".join(sorted(missing))
                )

        self.frames_root.mkdir(parents=True, exist_ok=True)
        for frame_id in sidecar.document.frame_ids:
            if frame_id in sidecar.unavailable_frame_ids:
                continue
            _atomic_json(
                self.frame_path(frame_id), sidecar.frames[frame_id].to_dict(),
            )
        if sidecar.cache_manifest is not None:
            _atomic_json(
                self.cache_revision_path(sidecar.cache_manifest.revision),
                sidecar.cache_manifest.to_dict(),
            )
        _atomic_json(self.manifest_path, sidecar.document.to_dict())

        # Stale frame files are harmless until the new manifest exists.  They
        # are removed only after publication so an interrupted write never
        # makes the previous manifest incomplete.
        active = set(sidecar.document.frame_ids)
        for path in self.frames_root.glob("*.json"):
            if path.stem not in active:
                path.unlink(missing_ok=True)

    def load(
        self, *, expected_chapter_id: str | None = None,
    ) -> BlenderSidecarData:
        data = _read_json(
            self.manifest_path, limit=_MAX_MANIFEST_BYTES,
            label="Blender sidecar manifest",
        )
        document = BlenderChapterDocument.from_dict(data)
        if (
            expected_chapter_id is not None
            and document.chapter_id != str(expected_chapter_id)
        ):
            raise ValueError("Blender sidecar belongs to a different chapter")
        warnings: list[str] = []
        unavailable_frame_ids: set[str] = set()
        frames: dict[str, ComicFrameDocument] = {}
        for frame_id in document.frame_ids:
            path = self.frame_path(frame_id)
            if not path.is_file():
                warnings.append(f"Comic frame {frame_id} is missing.")
                unavailable_frame_ids.add(frame_id)
                continue
            try:
                frame = ComicFrameDocument.from_dict(
                    _read_json(
                        path, limit=_MAX_MANIFEST_BYTES,
                        label=f"Comic frame {frame_id}",
                    )
                )
                if frame.frame_id != frame_id:
                    raise ValueError("frame file ID does not match its filename")
                if frame.chapter_id != document.chapter_id:
                    raise ValueError("frame belongs to a different chapter")
            except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                warnings.append(f"Comic frame {frame_id} is unavailable: {error}")
                unavailable_frame_ids.add(frame_id)
                continue
            frames[frame_id] = frame

        cache_manifest: CacheManifest | None = None
        if document.current_cache_revision:
            cache_path = self.cache_revision_path(document.current_cache_revision)
            if not cache_path.is_file():
                warnings.append(
                    f"3D cache revision {document.current_cache_revision} is missing."
                )
            else:
                try:
                    cache_manifest = CacheManifest.from_dict(
                        _read_json(
                            cache_path, limit=_MAX_CACHE_MANIFEST_BYTES,
                            label="3D cache manifest",
                        )
                    )
                    if cache_manifest.revision != document.current_cache_revision:
                        raise ValueError("cache revision ID does not match its filename")
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    warnings.append(f"3D cache manifest is unavailable: {error}")
                    cache_manifest = None
        if cache_manifest is not None:
            required = set(cache_manifest.object_resources.values())
            required.update(cache_manifest.baked_variants.values())
            if cache_manifest.base_glb_hash:
                required.add(cache_manifest.base_glb_hash)
            for digest in sorted(required):
                path = self.blob_path(digest)
                if not path.is_file():
                    warnings.append(f"3D cache blob {digest} is missing.")
                    continue
                try:
                    if path.is_symlink():
                        raise OSError("cache blob is a filesystem link")
                    if _sha256(path.read_bytes()) != digest:
                        warnings.append(f"3D cache blob {digest} is corrupt.")
                except OSError as error:
                    warnings.append(f"3D cache blob {digest} is unavailable: {error}")
        return BlenderSidecarData(
            document=document,
            frames=frames,
            cache_manifest=cache_manifest,
            warnings=warnings,
            unavailable_frame_ids=unavailable_frame_ids,
        )

    @staticmethod
    def _collect_hashes(
        value: Any,
        result: set[str],
        *,
        skip_revision_metadata: bool = False,
    ) -> None:
        if isinstance(value, Mapping):
            for key, item in value.items():
                if skip_revision_metadata and str(key).casefold() in {
                    "revision", "cache_revision", "cache_revision_id",
                    "cache_revisions", "current_cache_revision",
                }:
                    continue
                BlenderSidecarRepository._collect_hashes(
                    item, result,
                    skip_revision_metadata=skip_revision_metadata,
                )
        elif isinstance(value, list):
            for item in value:
                BlenderSidecarRepository._collect_hashes(
                    item, result,
                    skip_revision_metadata=skip_revision_metadata,
                )
        elif isinstance(value, str):
            folded = value.lower()
            if len(folded) == 64 and all(
                character in "0123456789abcdef" for character in folded
            ):
                result.add(folded)

    @staticmethod
    def _collect_revision_ids(
        value: Any, candidates: set[str], result: set[str],
    ) -> None:
        """Find live revision IDs without treating history catalogs as roots."""
        if isinstance(value, Mapping):
            for key, item in value.items():
                if str(key).casefold() == "cache_revisions":
                    continue
                BlenderSidecarRepository._collect_revision_ids(
                    item, candidates, result,
                )
        elif isinstance(value, list):
            for item in value:
                BlenderSidecarRepository._collect_revision_ids(
                    item, candidates, result,
                )
        elif isinstance(value, str) and value in candidates:
            result.add(value)

    def collect_garbage(
        self,
        protected_roots: Iterable[str | Path] = (),
        protected_hashes: Iterable[str] = (),
    ) -> set[str]:
        """Prune obsolete revisions and blobs after a successful chapter save.

        Historical ``cache_revisions`` arrays are catalogs, not live roots.
        The collector first validates every current/recovery/inbox JSON record
        and every cache manifest, then retains revisions selected by current
        state, frame/bundle references, or explicit undo hashes.  No mutation
        occurs if any record is malformed or ambiguous.
        """
        referenced = {
            str(item).lower() for item in protected_hashes
            if len(str(item)) == 64 and all(
                character in "0123456789abcdef"
                for character in str(item).lower()
            )
        }
        roots: list[Path] = []
        for raw_root in (self.root, *(Path(item) for item in protected_roots)):
            if raw_root.is_symlink():
                return set()
            root = raw_root.expanduser().resolve()
            if root not in roots:
                roots.append(root)

        # Pass one validates every revision manifest, including obsolete ones.
        # A corrupt historical record must abort before any pruning occurs.
        revision_records: dict[Path, dict[str, tuple[Path, CacheManifest]]] = {}
        canonical_revisions: dict[str, dict[str, Any]] = {}
        try:
            for root in roots:
                if not root.exists():
                    continue
                if not root.is_dir():
                    return set()
                records: dict[str, tuple[Path, CacheManifest]] = {}
                revisions_root = root / CACHE_DIR / CACHE_REVISIONS_DIR
                if revisions_root.exists():
                    if revisions_root.is_symlink() or not revisions_root.is_dir():
                        return set()
                    for path in sorted(revisions_root.rglob("*.json")):
                        if path.parent != revisions_root:
                            return set()
                        payload = _read_json(
                            path, limit=_MAX_CACHE_MANIFEST_BYTES,
                            label="3D cache manifest",
                        )
                        manifest = CacheManifest.from_dict(payload)
                        if manifest.revision != path.stem:
                            return set()
                        canonical = manifest.to_dict()
                        previous = canonical_revisions.get(manifest.revision)
                        if previous is not None and previous != canonical:
                            return set()
                        canonical_revisions[manifest.revision] = canonical
                        records[manifest.revision] = (path, manifest)
                revision_records[root] = records
        except Exception:
            return set()

        all_revision_ids = set(canonical_revisions)
        documents: dict[Path, BlenderChapterDocument] = {}
        referenced_revisions: dict[Path, set[str]] = {
            root: set() for root in roots
        }
        direct_hashes: set[str] = set()

        # Pass two validates live state records while deliberately excluding
        # cache/revisions, whose liveness is decided only after this scan.
        try:
            for root in roots:
                if not root.exists():
                    continue
                manifest_path = root / BLENDER_MANIFEST_FILE
                if not manifest_path.is_file():
                    return set()
                for path in sorted(root.rglob("*.json")):
                    relative = path.relative_to(root)
                    folded_parts = tuple(part.casefold() for part in relative.parts)
                    if (
                        len(folded_parts) >= 2
                        and folded_parts[:2] == (
                            CACHE_DIR.casefold(), CACHE_REVISIONS_DIR.casefold(),
                        )
                    ):
                        continue
                    payload = _read_json(
                        path, limit=_MAX_CACHE_MANIFEST_BYTES,
                        label="3D saved-state record",
                    )
                    if path == manifest_path:
                        document = BlenderChapterDocument.from_dict(payload)
                        documents[root] = document
                        current = document.current_cache_revision
                        if current is not None:
                            if current not in revision_records.get(root, {}):
                                return set()
                            referenced_revisions[root].add(current)
                    elif (
                        len(folded_parts) == 2
                        and folded_parts[0] == FRAMES_DIR.casefold()
                    ):
                        frame = ComicFrameDocument.from_dict(payload)
                        if frame.frame_id != path.stem:
                            return set()
                    self._collect_hashes(
                        payload, direct_hashes,
                        skip_revision_metadata=True,
                    )
                    self._collect_revision_ids(
                        payload, all_revision_ids,
                        referenced_revisions[root],
                    )
                if root not in documents:
                    return set()
        except Exception:
            return set()

        current_document = documents.get(self.root)
        if current_document is None:
            return set()
        chapter_id = current_document.chapter_id
        if any(document.chapter_id != chapter_id for document in documents.values()):
            return set()

        referenced.update(direct_hashes)
        required_revisions = set().union(*referenced_revisions.values())

        # A frame, recovery record, inbox bundle, or undo snapshot may identify
        # a revision through one of its blobs rather than an explicit revision
        # ID.  Preserve the associated manifest in that case as well.
        for revision_id, canonical in canonical_revisions.items():
            manifest = CacheManifest.from_dict(canonical)
            if manifest.referenced_hashes() & referenced:
                required_revisions.add(revision_id)

        records_by_revision: dict[
            str, list[tuple[Path, Path, CacheManifest]]
        ] = {}
        for root, records in revision_records.items():
            for revision_id, (path, manifest) in records.items():
                records_by_revision.setdefault(revision_id, []).append(
                    (root, path, manifest)
                )
        for revision_id in required_revisions:
            records = records_by_revision.get(revision_id)
            if not records:
                return set()
            for _root, _path, manifest in records:
                referenced.update(manifest.referenced_hashes())

        current_records = revision_records.get(self.root, {})
        kept_current_revisions = set(current_records) & required_revisions
        current_revision = current_document.current_cache_revision
        if current_revision is not None and current_revision not in kept_current_revisions:
            return set()

        blob_paths = (
            sorted(self.blobs_root.glob("*.glb"))
            if self.blobs_root.is_dir() else []
        )
        for path in blob_paths:
            digest = path.stem.lower()
            if path.is_symlink() or len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                return set()

        # Publish the narrowed catalog before deleting files.  An interruption
        # can therefore leave harmless extra manifests, never a dangling live
        # catalog entry.
        ordered_revisions = [
            revision_id for revision_id in current_document.cache_revisions
            if revision_id in kept_current_revisions
        ]
        ordered_revisions.extend(sorted(
            kept_current_revisions - set(ordered_revisions)
        ))
        if ordered_revisions != current_document.cache_revisions:
            current_document.cache_revisions = ordered_revisions
            _atomic_json(self.manifest_path, current_document.to_dict())

        for revision_id, (path, _manifest) in current_records.items():
            if revision_id not in kept_current_revisions:
                path.unlink()

        removed: set[str] = set()
        for path in blob_paths:
            digest = path.stem.lower()
            if digest in referenced:
                continue
            path.unlink()
            removed.add(digest)
        return removed
