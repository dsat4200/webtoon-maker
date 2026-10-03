"""Portable series folders with versioned, atomic chapter saves."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from contextlib import contextmanager
from threading import RLock
from weakref import WeakValueDictionary
from pathlib import Path
from typing import Callable

from .models import (
    ChapterDocument, ChapterReference, ImageObject, RasterObject, SeriesDocument,
)
from .images import ImageStore
from .fill_migration import materialize_legacy_fills
from .tiles import TileStore


SERIES_FILE = "series.json"
CHAPTER_FILE = "chapter.json"
PENDING_FILE = ".save_pending"
LAST_GOOD_DIR = "last_good"
_revision_locks = WeakValueDictionary()
_revision_locks_guard = RLock()


@contextmanager
def resource_revision(root: Path):
    """Serialize readers/publication for one chapter or asset, not the project."""
    from .tile_backing import finish_revision_readers
    address = os.path.normcase(str(root.resolve()))
    with _revision_locks_guard:
        lock = _revision_locks.get(address)
        if lock is None:
            lock = _revision_locks[address] = RLock()
    with lock:
        finish_revision_readers(Path(address))
        yield


def link_or_copy(source: str, destination: str) -> str:
    """Snapshot immutable resource files cheaply on the same filesystem.

    Resource publishers replace files atomically; they never modify a linked
    inode. Filesystems without hard links retain the portable copy fallback.
    """
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)
    return destination


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


def completed_revision_manifest(root: Path, manifest_name: str) -> Path:
    """Locate committed metadata without offering a partial write as recovery."""
    if (root / PENDING_FILE).exists():
        return root / LAST_GOOD_DIR / manifest_name
    return root / manifest_name


def recover_saved_revision(
    root: Path, manifest_name: str, *, extra_files: tuple[str, ...] = (),
    allow_first_save_retry: bool = False,
) -> None:
    pending = root / PENDING_FILE
    if not pending.exists():
        return
    backup = root / LAST_GOOD_DIR
    backup_manifest = backup / manifest_name
    if not backup_manifest.is_file():
        if not allow_first_save_retry:
            raise OSError(
                f"The first {Path(manifest_name).stem} save was interrupted "
                "and has no recoverable revision"
            )
        # A retry has the complete in-memory document. Never promote files
        # from its incomplete first write into a last-good snapshot.
        (root / manifest_name).unlink(missing_ok=True)
        for name in extra_files:
            (root / name).unlink(missing_ok=True)
        return
    for name in ("raster", "masks", "images"):
        target = root / name
        if target.exists():
            shutil.rmtree(target)
        if (backup / name).is_dir():
            shutil.copytree(backup / name, target)
    for name in extra_files:
        if (backup / name).is_file():
            shutil.copy2(backup / name, root / name)
        else:
            (root / name).unlink(missing_ok=True)
    shutil.copy2(backup_manifest, root / manifest_name)
    pending.unlink(missing_ok=True)


def prepare_revision_save(
    root: Path, manifest_name: str, *, extra_files: tuple[str, ...] = (),
) -> None:
    """Protect the last complete disk revision before any resource writes."""
    root.mkdir(parents=True, exist_ok=True)
    recover_saved_revision(
        root, manifest_name, extra_files=extra_files,
        allow_first_save_retry=True,
    )
    backup = root / LAST_GOOD_DIR
    manifest = root / manifest_name
    if manifest.is_file():
        backup.mkdir(exist_ok=True)
        for name in ("raster", "masks", "images"):
            if (root / name).is_dir():
                sync_immutable_tree(root / name, backup / name)
            elif (backup / name).exists():
                shutil.rmtree(backup / name)
        for name in extra_files:
            if (root / name).is_file():
                shutil.copy2(root / name, backup / name)
            else:
                (backup / name).unlink(missing_ok=True)
        # The backup manifest is published last so it denotes a complete copy.
        shutil.copy2(manifest, backup / manifest_name)
    elif backup.exists():
        shutil.rmtree(backup)
    atomic_json(root / PENDING_FILE, {"started_at": time.time()})


def sync_immutable_tree(source: Path, destination: Path) -> None:
    """Reuse already-linked backup resources; mirror changed/deleted files.

    This runs before the pending marker and any live resource writes. A failed
    backup preparation therefore leaves the current published revision intact.
    The backup manifest is published only after the entire mirror succeeds.
    """
    destination.mkdir(parents=True, exist_ok=True)
    names = set()
    for entry in os.scandir(source):
        names.add(entry.name)
        target = destination / entry.name
        if entry.is_dir():
            sync_immutable_tree(Path(entry.path), target)
        else:
            try:
                if os.path.samefile(entry.path, target):
                    continue
            except OSError:
                pass
            temporary = target.with_name(f'.{target.name}.backup.tmp')
            temporary.unlink(missing_ok=True)
            link_or_copy(entry.path, str(temporary))
            temporary.replace(target)
    for entry in os.scandir(destination):
        if entry.name not in names:
            target = Path(entry.path)
            if entry.is_dir():
                shutil.rmtree(target)
            else:
                target.unlink()


class SeriesRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.series_path = self.root / SERIES_FILE
        self.last_load_warnings: list[str] = []

    @property
    def exists(self) -> bool:
        return self.series_path.is_file()

    def create(self, name: str) -> SeriesDocument:
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError("Series folder must be empty")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "chapters").mkdir(exist_ok=True)
        (self.root / "assets").mkdir(exist_ok=True)
        (self.root / "exports").mkdir(exist_ok=True)
        series = SeriesDocument(name=name.strip() or "Untitled Series")
        self.save_series(series)
        return series

    def load_series(
        self, legacy_primary_color: str | None = None,
    ) -> SeriesDocument:
        data = json.loads(self.series_path.read_text(encoding="utf-8"))
        if (
            legacy_primary_color is not None
            and "primary_color" not in data
            and "brush_color" not in data
        ):
            data["primary_color"] = legacy_primary_color
        return SeriesDocument.from_dict(data)

    def save_series(self, series: SeriesDocument) -> None:
        atomic_json(self.series_path, series.to_dict())

    @staticmethod
    def _clone_ignored_names(_directory: str, names: list[str]) -> set[str]:
        """Exclude recovery and incomplete-write artifacts from a clone."""
        ignored: set[str] = set()
        for name in names:
            folded = name.casefold()
            if folded in {"autosave", LAST_GOOD_DIR, PENDING_FILE}:
                ignored.add(name)
            elif folded.endswith(".tmp") or folded.endswith("~"):
                ignored.add(name)
        return ignored

    @classmethod
    def _remove_clone_transients(cls, root: Path) -> None:
        """Remove artifacts an overlay save may have regenerated."""
        candidates = sorted(
            root.rglob("*"), key=lambda item: len(item.parts), reverse=True,
        )
        for path in candidates:
            folded = path.name.casefold()
            transient = (
                folded in {"autosave", LAST_GOOD_DIR, PENDING_FILE}
                or folded.endswith(".tmp")
                or folded.endswith("~")
            )
            if not transient:
                continue
            if path.is_dir():
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)

    def clone_to(
        self,
        destination: str | Path,
        series: SeriesDocument,
        overlay: Callable[["SeriesRepository"], None] | None = None,
    ) -> "SeriesRepository":
        """Clone a series through a sibling staging directory, then publish.

        ``overlay`` can write newer in-memory documents into the staging
        repository.  The original project and destination remain untouched if
        any copy or overlay operation fails.
        """
        destination = Path(destination).expanduser().resolve()
        if destination.exists():
            raise FileExistsError(f"Destination already exists: {destination}")
        if not destination.name:
            raise ValueError("A destination folder name is required")
        if not destination.parent.is_dir():
            raise FileNotFoundError(
                f"Destination parent does not exist: {destination.parent}"
            )
        try:
            destination.relative_to(self.root)
        except ValueError:
            pass
        else:
            raise ValueError("The clone destination cannot be inside the source series")
        if not self.series_path.is_file():
            raise FileNotFoundError(f"Series manifest not found: {self.series_path}")

        staging = destination.parent / (
            f".{destination.name}.clone-{uuid.uuid4().hex}.tmpdir"
        )
        try:
            shutil.copytree(
                self.root, staging, ignore=self._clone_ignored_names,
            )
            staged_repository = SeriesRepository(staging)
            staged_repository.save_series(series)
            if overlay is not None:
                overlay(staged_repository)
            self._remove_clone_transients(staging)
            os.replace(staging, destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return SeriesRepository(destination)

    def chapter_root(self, chapter_id: str) -> Path:
        return self.root / "chapters" / chapter_id

    def create_chapter(self, series: SeriesDocument, name: str) -> tuple[ChapterDocument, TileStore]:
        chapter = ChapterDocument(name=name.strip() or f"Chapter {len(series.chapters) + 1}")
        page = chapter.add_page("Page 1")
        layer = chapter.add_layer(
            page.layer_id, "Drawing Layer",
            type(page.bound).from_dict(page.bound.to_dict()),
        )
        chapter.add_object(layer.layer_id, RasterObject(name="Raster 1"))
        tiles = TileStore()
        series.chapters.append(ChapterReference(chapter.chapter_id, chapter.name))
        self.save_chapter(chapter, tiles)
        self.save_series(series)
        return chapter, tiles

    def save_chapter(
        self, chapter: ChapterDocument, tiles: TileStore,
        images: ImageStore | None = None, autosave: bool = False,
    ) -> None:
        root = self.chapter_root(chapter.chapter_id)
        # Manual saves also prune recovery files, so both destinations share
        # their document lock. Different chapters still proceed independently.
        with resource_revision(root):
            self._save_chapter(chapter, tiles, images, autosave)

    def _save_chapter(self, chapter, tiles, images, autosave):
        images = images or ImageStore()
        chapter.validate()
        tiles.finish_snapshot_prefetch()
        raster_object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        image_object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, ImageObject)
        }
        mask_ids = set(chapter.masks)
        chapter_root = self.chapter_root(chapter.chapter_id)
        destination = chapter_root / "autosave" if autosave else chapter_root
        tile_root = destination / "raster"
        mask_root = destination / "masks"
        image_root = destination / "images"
        manifest = destination / CHAPTER_FILE
        pending = destination / PENDING_FILE
        prepare_revision_save(
            destination, CHAPTER_FILE, extra_files=("recovery.json",),
        )
        image_dirty = set(images.dirty)
        try:
            # Tile files are published before the manifest. If the process is
            # interrupted, PENDING_FILE causes the previous complete revision
            # to be restored on the next open.
            tiles.save_directory(tile_root, raster_object_ids, complete=True)
            tiles.save_directory(mask_root, mask_ids, complete=True)
            images.save_directory(image_root, image_object_ids, complete=True, incremental=True, transactional=True)
            if autosave:
                atomic_json(destination / "recovery.json", {"saved_at": time.time()})
            atomic_json(manifest, chapter.to_dict())
            pending.unlink(missing_ok=True)
            tiles.commit_directory(tile_root)
            tiles.commit_directory(mask_root)
            images.commit_directory(image_root)
        except Exception:
            # Leave the pending marker and last-good data intact for recovery.
            images.dirty.update(image_dirty)
            raise
        if autosave:
            images.dirty.update(image_dirty)
            return
        tiles.dirty.clear()
        images.dirty.clear()
        autosave_root = destination / "autosave"
        if autosave_root.exists():
            tiles.preserve_backing(autosave_root)
            shutil.rmtree(autosave_root)

    def load_chapter(
        self, chapter_id: str, recover: bool = False,
        *, include_images: bool = False,
    ) -> tuple[ChapterDocument, TileStore] | tuple[
        ChapterDocument, TileStore, ImageStore
    ]:
        with resource_revision(self.chapter_root(chapter_id)):
            return self._load_chapter(chapter_id, recover, include_images=include_images)

    def _load_chapter(self, chapter_id, recover, *, include_images):
        root = self.chapter_root(chapter_id)
        source = root / "autosave" if recover else root
        self._recover_interrupted_save(source)
        data = json.loads((source / CHAPTER_FILE).read_text(encoding="utf-8"))
        self.last_load_warnings = []
        chapter = ChapterDocument.from_dict(data, warnings=self.last_load_warnings)
        tiles = TileStore()
        object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        tiles.load_directory(source / "raster", object_ids)
        tiles.load_directory(
            source / "masks", set(chapter.masks), clear=False
        )
        materialize_legacy_fills(chapter, tiles)
        images = ImageStore()
        images.load_directory(source / "images", {
            object_id: (obj.source_filename, obj.source_mime_type)
            for object_id, obj in chapter.objects.items()
            if isinstance(obj, ImageObject)
        })
        tiles.prefetch_snapshot_backing()
        return (chapter, tiles, images) if include_images else (chapter, tiles)

    def has_recovery(self, chapter_id: str) -> bool:
        root = self.chapter_root(chapter_id)
        manual = completed_revision_manifest(root, CHAPTER_FILE)
        recovery = completed_revision_manifest(root / "autosave", CHAPTER_FILE)
        return recovery.is_file() and (
            not manual.is_file() or recovery.stat().st_mtime > manual.stat().st_mtime
        )

    @staticmethod
    def _recover_interrupted_save(root: Path) -> None:
        recover_saved_revision(root, CHAPTER_FILE, extra_files=("recovery.json",))
