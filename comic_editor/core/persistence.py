"""Portable series folders with versioned, atomic chapter saves."""
from __future__ import annotations

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Callable, Iterable, Mapping

from .models import (
    ChapterDocument, ChapterReference, ImageObject, RasterObject, SeriesDocument,
)
from .images import ImageStore
from .tiles import TileStore
from comic_editor.three_d.documents import BlenderChapterDocument
from comic_editor.three_d.repository import (
    BLENDER_DIR, BlenderSidecarData, BlenderSidecarRepository,
)


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
        self.last_load_warnings: list[str] = []
        self.last_save_warnings: list[str] = []
        self.last_loaded_blender: BlenderSidecarData | None = None

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
            # Overlays contain only the currently loaded editor sessions.  A
            # repository-level pass is therefore required to rebind sidecars
            # for chapters that were copied without ever being opened.
            staged_repository.rebind_blender_sidecars(series.series_id)
            self._remove_clone_transients(staging)
            os.replace(staging, destination)
        except Exception:
            if staging.exists():
                shutil.rmtree(staging)
            raise
        return SeriesRepository(destination)

    def rebind_blender_sidecars(self, series_id: str) -> list[str]:
        """Atomically bind every persisted chapter sidecar to ``series_id``.

        Save As copies unopened chapter folders verbatim.  Rewriting only the
        mutable sidecar manifest preserves immutable cache blobs and frame
        files while ensuring every cloned chapter has the clone's identity.
        The returned chapter IDs are those whose manifests changed.
        """
        series_id = str(series_id).strip()
        if not series_id:
            raise ValueError("A Blender sidecar series ID is required")
        chapters_root = self.root / "chapters"
        if not chapters_root.is_dir():
            return []

        changed: list[str] = []
        for manifest_path in sorted(
            chapters_root.glob(f"*/{BLENDER_DIR}/manifest.json")
        ):
            if manifest_path.is_symlink():
                raise ValueError("Blender sidecar manifest cannot be a filesystem link")
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            document = BlenderChapterDocument.from_dict(payload)
            chapter_id = manifest_path.parent.parent.name
            if document.chapter_id != chapter_id:
                raise ValueError(
                    "Blender sidecar chapter ID does not match its chapter folder"
                )
            if document.series_id == series_id:
                continue
            document.series_id = series_id
            document.revision += 1
            atomic_json(manifest_path, document.to_dict())
            changed.append(chapter_id)
        return changed

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
        *,
        blender_sidecar: BlenderSidecarData | None = None,
        blender_blobs: Mapping[str, bytes | str | Path] | None = None,
        protected_blender_hashes: Iterable[str] | None = None,
    ) -> None:
        self.last_save_warnings = []
        images = images or ImageStore()
        chapter.validate()
        raster_object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        image_object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, ImageObject)
        }
        chapter_root = self.chapter_root(chapter.chapter_id)
        if (
            blender_sidecar is not None
            and blender_sidecar.document.chapter_id != chapter.chapter_id
        ):
            raise ValueError("Blender sidecar belongs to a different chapter")
        if blender_sidecar is not None:
            blender_sidecar.validate()
            layer_frame_ids = {
                layer.comic_frame_id for layer in chapter.layers.values()
                if layer.layer_kind == "blender" and layer.comic_frame_id
            }
            if set(blender_sidecar.document.frame_ids) != layer_frame_ids:
                raise ValueError(
                    "Blender sidecar frames do not match the chapter's 3D layers"
                )
        if autosave:
            destination = chapter_root / "autosave"
            tile_root = destination / "raster"
            image_root = destination / "images"
            tiles.save_directory(tile_root, raster_object_ids, complete=True)
            images.save_directory(image_root, image_object_ids, complete=True)
            if blender_sidecar is not None:
                BlenderSidecarRepository(destination / BLENDER_DIR).save(
                    blender_sidecar, blobs=blender_blobs,
                    fallback_blob_roots=[
                        chapter_root / BLENDER_DIR / "cache" / "blobs",
                    ],
                )
            elif (
                any(layer.layer_kind == "blender" for layer in chapter.layers.values())
                and (chapter_root / BLENDER_DIR).is_dir()
            ):
                autosave_blender = destination / BLENDER_DIR
                if autosave_blender.exists():
                    shutil.rmtree(autosave_blender)
                shutil.copytree(chapter_root / BLENDER_DIR, autosave_blender)
            atomic_json(destination / CHAPTER_FILE, chapter.to_dict())
            atomic_json(destination / "recovery.json", {"saved_at": time.time()})
            return
        destination = chapter_root
        tile_root = destination / "raster"
        image_root = destination / "images"
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
            if image_root.is_dir():
                shutil.copytree(image_root, backup / "images")
            blender_root = destination / BLENDER_DIR
            if blender_root.is_dir():
                shutil.copytree(blender_root, backup / BLENDER_DIR)
        atomic_json(pending, {"started_at": time.time()})
        try:
            # Tile files are published before the manifest. If the process is
            # interrupted, PENDING_FILE causes the previous complete revision
            # to be restored on the next open.
            tiles.save_directory(tile_root, raster_object_ids, complete=True)
            images.save_directory(image_root, image_object_ids, complete=True)
            if blender_sidecar is not None:
                BlenderSidecarRepository(destination / BLENDER_DIR).save(
                    blender_sidecar, blobs=blender_blobs,
                )
            atomic_json(manifest, chapter.to_dict())
            pending.unlink(missing_ok=True)
            tiles.dirty.clear()
            images.dirty.clear()
        except Exception:
            # Leave the pending marker and last-good data intact for recovery.
            raise
        autosave_root = destination / "autosave"
        if autosave_root.exists():
            shutil.rmtree(autosave_root)
        if blender_sidecar is not None and protected_blender_hashes is not None:
            try:
                self.collect_blender_cache(
                    chapter.chapter_id,
                    protected_hashes=protected_blender_hashes,
                )
                # Keep the open session aligned with the atomically narrowed
                # on-disk revision catalog so a later save cannot reintroduce
                # already-pruned history IDs.
                persisted_manifest = json.loads(
                    (
                        self.chapter_root(chapter.chapter_id) / BLENDER_DIR
                        / "manifest.json"
                    ).read_text(encoding="utf-8")
                )
                persisted_document = BlenderChapterDocument.from_dict(
                    persisted_manifest
                )
                blender_sidecar.document.cache_revisions = list(
                    persisted_document.cache_revisions
                )
            except Exception as error:
                # The chapter manifest is already published at this point.
                # Cleanup must never turn a successful, durable save into an
                # apparent failure; callers can surface this warning in their
                # status UI and retry collection on a later save.
                self.last_save_warnings.append(
                    "Chapter saved, but 3D cache cleanup was skipped: "
                    f"{error}"
                )

    def load_chapter(
        self, chapter_id: str, recover: bool = False,
        *, include_images: bool = False, include_blender: bool = False,
    ) -> tuple[ChapterDocument, TileStore] | tuple[
        ChapterDocument, TileStore, ImageStore
    ] | tuple[
        ChapterDocument, TileStore, BlenderSidecarData | None
    ] | tuple[
        ChapterDocument, TileStore, ImageStore, BlenderSidecarData | None
    ]:
        root = self.chapter_root(chapter_id)
        if not recover:
            self._recover_interrupted_save(root)
        source = root / "autosave" if recover else root
        data = json.loads((source / CHAPTER_FILE).read_text(encoding="utf-8"))
        self.last_load_warnings = []
        self.last_loaded_blender = None
        chapter = ChapterDocument.from_dict(data, warnings=self.last_load_warnings)
        tiles = TileStore()
        object_ids = {
            object_id for object_id, obj in chapter.objects.items()
            if isinstance(obj, RasterObject)
        }
        tiles.load_directory(source / "raster", object_ids)
        images = ImageStore()
        images.load_directory(source / "images", {
            object_id: (obj.source_filename, obj.source_mime_type)
            for object_id, obj in chapter.objects.items()
            if isinstance(obj, ImageObject)
        })
        has_blender_layers = any(
            layer.layer_kind == "blender" for layer in chapter.layers.values()
        )
        sidecar_root = source / BLENDER_DIR
        if has_blender_layers or sidecar_root.exists():
            if not (sidecar_root / "manifest.json").is_file():
                self.last_load_warnings.append(
                    "3D sidecar is missing; cached 3D content is unavailable."
                )
            else:
                try:
                    self.last_loaded_blender = BlenderSidecarRepository(
                        sidecar_root
                    ).load(expected_chapter_id=chapter.chapter_id)
                except (OSError, ValueError, TypeError, json.JSONDecodeError) as error:
                    self.last_load_warnings.append(
                        f"Could not load 3D sidecar: {error}"
                    )
                else:
                    self.last_load_warnings.extend(
                        self.last_loaded_blender.warnings
                    )
                    layer_frame_ids = {
                        layer.comic_frame_id for layer in chapter.layers.values()
                        if layer.layer_kind == "blender" and layer.comic_frame_id
                    }
                    missing_frames = layer_frame_ids - set(
                        self.last_loaded_blender.frames
                    ) - self.last_loaded_blender.unavailable_frame_ids
                    for frame_id in sorted(missing_frames):
                        self.last_load_warnings.append(
                            f"Comic frame {frame_id} is unavailable."
                        )
                    orphan_frames = set(
                        self.last_loaded_blender.document.frame_ids
                    ) - layer_frame_ids
                    for frame_id in sorted(orphan_frames):
                        self.last_load_warnings.append(
                            f"Comic frame {frame_id} has no owning 3D layer."
                        )
                    path_hint = (
                        self.last_loaded_blender.document.blend_path_hint
                    )
                    if path_hint and not Path(path_hint).expanduser().is_file():
                        self.last_load_warnings.append(
                            "The linked Blender file is unavailable; using the latest cache."
                        )
        if include_images and include_blender:
            return chapter, tiles, images, self.last_loaded_blender
        if include_images:
            return chapter, tiles, images
        if include_blender:
            return chapter, tiles, self.last_loaded_blender
        return chapter, tiles

    def save_blender_sidecar(
        self, chapter_id: str, sidecar: BlenderSidecarData,
        *, autosave: bool = False,
        blobs: Mapping[str, bytes | str | Path] | None = None,
    ) -> None:
        """Save a sidecar directly for sync/import code that owns no tile edit."""
        if sidecar.document.chapter_id != str(chapter_id):
            raise ValueError("Blender sidecar belongs to a different chapter")
        root = self.chapter_root(chapter_id)
        if autosave:
            root /= "autosave"
        BlenderSidecarRepository(root / BLENDER_DIR).save(
            sidecar, blobs=blobs,
            fallback_blob_roots=(
                [
                    self.chapter_root(chapter_id) / BLENDER_DIR
                    / "cache" / "blobs",
                ]
                if autosave else []
            ),
        )

    def load_blender_sidecar(
        self, chapter_id: str, *, recover: bool = False,
    ) -> BlenderSidecarData:
        root = self.chapter_root(chapter_id)
        if recover:
            root /= "autosave"
        return BlenderSidecarRepository(root / BLENDER_DIR).load(
            expected_chapter_id=chapter_id,
        )

    def collect_blender_cache(
        self, chapter_id: str, *, protected_hashes: Iterable[str],
    ) -> set[str]:
        """Collect cache revisions/blobs after save with undo hashes supplied.

        Callers must pass hashes referenced by in-memory undo snapshots.  Disk
        references in current, autosave, ``last_good``, and inbox JSON are
        discovered automatically. Historical revision catalogs are pruned only
        here, after chapter publication. Refusing to run during a pending save
        keeps recovery data conservative.
        """
        root = self.chapter_root(chapter_id)
        if (root / PENDING_FILE).exists():
            raise OSError("Cannot collect 3D cache during a pending chapter save")
        if not (root / CHAPTER_FILE).is_file():
            raise FileNotFoundError("Cannot collect 3D cache before chapter save")
        return BlenderSidecarRepository(root / BLENDER_DIR).collect_garbage(
            protected_roots=[
                root / "autosave" / BLENDER_DIR,
                root / LAST_GOOD_DIR / BLENDER_DIR,
            ],
            protected_hashes=protected_hashes,
        )

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
        images = root / "images"
        if images.exists():
            shutil.rmtree(images)
        backup_images = backup / "images"
        if backup_images.is_dir():
            shutil.copytree(backup_images, images)
        blender = root / BLENDER_DIR
        if blender.exists():
            shutil.rmtree(blender)
        backup_blender = backup / BLENDER_DIR
        if backup_blender.is_dir():
            shutil.copytree(backup_blender, blender)
        shutil.copy2(backup_manifest, root / CHAPTER_FILE)
        pending.unlink(missing_ok=True)
