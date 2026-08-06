"""Portable series folders with versioned, atomic chapter saves."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable

from .models import (
    ChapterDocument, ChapterReference, RasterObject, SeriesDocument,
)
from .tiles import TileStore


SERIES_FILE = "series.json"
CHAPTER_FILE = "chapter.json"
PENDING_FILE = ".save_pending"
LAST_GOOD_DIR = "last_good"


def atomic_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    with temporary.open("w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, ensure_ascii=False)
        handle.flush()
        os.fsync(handle.fileno())
    temporary.replace(path)


class SeriesRepository:
    def __init__(self, root: str | Path):
        self.root = Path(root).expanduser().resolve()
        self.series_path = self.root / SERIES_FILE

    @property
    def exists(self) -> bool:
        return self.series_path.is_file()

    def create(self, name: str) -> SeriesDocument:
        if self.root.exists() and any(self.root.iterdir()):
            raise FileExistsError("Series folder must be empty")
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / "chapters").mkdir(exist_ok=True)
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
        self, chapter: ChapterDocument, tiles: TileStore, autosave: bool = False,
    ) -> None:
        chapter.validate()
        raster_object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        chapter_root = self.chapter_root(chapter.chapter_id)
        if autosave:
            destination = chapter_root / "autosave"
            tile_root = destination / "raster"
            tiles.save_directory(tile_root, raster_object_ids, complete=True)
            atomic_json(destination / CHAPTER_FILE, chapter.to_dict())
            atomic_json(destination / "recovery.json", {"saved_at": time.time()})
            return
        destination = chapter_root
        tile_root = destination / "raster"
        destination.mkdir(parents=True, exist_ok=True)
        manifest = destination / CHAPTER_FILE
        pending = destination / PENDING_FILE
        backup = destination / LAST_GOOD_DIR
        if manifest.is_file():
            if backup.exists():
                shutil.rmtree(backup)
            backup.mkdir(parents=True)
            shutil.copy2(manifest, backup / CHAPTER_FILE)
            if tile_root.is_dir():
                shutil.copytree(tile_root, backup / "raster")
        atomic_json(pending, {"started_at": time.time()})
        try:
            # Tile files are published before the manifest. If the process is
            # interrupted, PENDING_FILE causes the previous complete revision
            # to be restored on the next open.
            tiles.save_directory(tile_root, raster_object_ids, complete=True)
            atomic_json(manifest, chapter.to_dict())
            pending.unlink(missing_ok=True)
            tiles.dirty.clear()
        except Exception:
            # Leave the pending marker and last-good data intact for recovery.
            raise
        autosave_root = destination / "autosave"
        if autosave_root.exists():
            shutil.rmtree(autosave_root)

    def load_chapter(
        self, chapter_id: str, recover: bool = False,
    ) -> tuple[ChapterDocument, TileStore]:
        root = self.chapter_root(chapter_id)
        if not recover:
            self._recover_interrupted_save(root)
        source = root / "autosave" if recover else root
        data = json.loads((source / CHAPTER_FILE).read_text(encoding="utf-8"))
        chapter = ChapterDocument.from_dict(data)
        tiles = TileStore()
        object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        tiles.load_directory(source / "raster", object_ids)
        return chapter, tiles

    def has_recovery(self, chapter_id: str) -> bool:
        root = self.chapter_root(chapter_id)
        manual = root / CHAPTER_FILE
        recovery = root / "autosave" / CHAPTER_FILE
        return recovery.is_file() and (
            not manual.is_file() or recovery.stat().st_mtime > manual.stat().st_mtime
        )

    @staticmethod
    def _recover_interrupted_save(root: Path) -> None:
        pending = root / PENDING_FILE
        if not pending.exists():
            return
        backup = root / LAST_GOOD_DIR
        backup_manifest = backup / CHAPTER_FILE
        if not backup_manifest.is_file():
            raise OSError("The first chapter save was interrupted and has no recoverable revision")
        raster = root / "raster"
        if raster.exists():
            shutil.rmtree(raster)
        backup_raster = backup / "raster"
        if backup_raster.is_dir():
            shutil.copytree(backup_raster, raster)
        shutil.copy2(backup_manifest, root / CHAPTER_FILE)
        pending.unlink(missing_ok=True)
