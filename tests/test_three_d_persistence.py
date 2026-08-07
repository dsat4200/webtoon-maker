from __future__ import annotations

import copy
import hashlib
import json
import os
import shutil
import time

import pytest

from comic_editor.core.persistence import PENDING_FILE, SeriesRepository
from comic_editor.three_d.documents import (
    BlenderChapterDocument, CacheManifest, ComicFrameDocument,
)
from comic_editor.three_d.repository import BlenderSidecarData
from comic_editor.three_d.repository import BlenderSidecarRepository


def build_project(tmp_path):
    repository = SeriesRepository(tmp_path / "series")
    series = repository.create("Series")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    page = chapter.layers[chapter.root_page_ids[0]]
    layer = chapter.add_blender_layer(page.layer_id)
    frame = ComicFrameDocument(
        frame_id=layer.comic_frame_id,
        chapter_id=chapter.chapter_id,
        included_collection_ids=["collection"],
        source_state={"collection_visibility": {"collection": True}},
        presentation_overrides={"renderer_settings": {"projection": "perspective"}},
    )
    blob = b"test glb payload"
    digest = hashlib.sha256(blob).hexdigest()
    cache = CacheManifest(
        revision="cache-1", source_revision=1, base_glb_hash=digest,
    )
    document = BlenderChapterDocument(
        chapter_id=chapter.chapter_id,
        series_id=series.series_id,
        file_uuid="blend-file-uuid",
        revision=1,
        source_revision=1,
        frame_ids=[frame.frame_id],
        cache_revisions=[cache.revision],
        current_cache_revision=cache.revision,
    )
    sidecar = BlenderSidecarData(document, {frame.frame_id: frame}, cache)
    return repository, series, chapter, tiles, layer, sidecar, digest, blob


def add_historical_cache_revision(
    repository, chapter, sidecar, *, revision="cache-old", blob=b"old cache",
):
    cache = BlenderSidecarRepository(
        repository.chapter_root(chapter.chapter_id) / "blender"
    )
    digest = cache.write_blob(blob)
    manifest = CacheManifest(
        revision=revision,
        source_revision=0,
        base_glb_hash=digest,
    )
    path = cache.cache_revision_path(revision)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest.to_dict()), encoding="utf-8")
    if revision not in sidecar.document.cache_revisions:
        sidecar.document.cache_revisions.insert(0, revision)
    cache.save(sidecar)
    return cache, manifest, digest


def test_sidecar_round_trip_and_existing_load_shape_stays_compatible(tmp_path):
    repository, _series, chapter, tiles, layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    root = repository.chapter_root(chapter.chapter_id)
    assert (root / "blender" / "manifest.json").is_file()
    assert (root / "blender" / "frames" / f"{layer.comic_frame_id}.json").is_file()
    assert (root / "blender" / "cache" / "blobs" / f"{digest}.glb").read_bytes() == blob
    assert (root / "blender" / "cache" / "revisions" / "cache-1.json").is_file()

    ordinary = repository.load_chapter(chapter.chapter_id)
    assert len(ordinary) == 2
    loaded, _loaded_tiles, loaded_sidecar = repository.load_chapter(
        chapter.chapter_id, include_blender=True,
    )
    assert loaded.layers[layer.layer_id].comic_frame_id == layer.comic_frame_id
    assert loaded_sidecar.frames[layer.comic_frame_id].renderer_settings == {
        "projection": "perspective",
    }
    assert repository.last_load_warnings == []


def test_autosave_captures_independent_sidecar_revision(tmp_path):
    repository, _series, chapter, tiles, layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    sidecar.document.revision = 2
    sidecar.frames[layer.comic_frame_id].renderer_settings["projection"] = "orthographic"
    repository.save_chapter(
        chapter, tiles, autosave=True, blender_sidecar=sidecar,
    )
    recovery_file = (
        repository.chapter_root(chapter.chapter_id) / "autosave" / "chapter.json"
    )
    future = time.time() + 2
    os.utime(recovery_file, (future, future))

    _chapter, _tiles, recovered = repository.load_chapter(
        chapter.chapter_id, recover=True, include_blender=True,
    )
    _chapter, _tiles, manual = repository.load_chapter(
        chapter.chapter_id, include_blender=True,
    )
    assert recovered.document.revision == 2
    assert recovered.frames[layer.comic_frame_id].renderer_settings[
        "projection"
    ] == "orthographic"
    assert manual.document.revision == 1
    assert manual.frames[layer.comic_frame_id].renderer_settings[
        "projection"
    ] == "perspective"


def test_interrupted_save_restores_last_good_sidecar(tmp_path):
    repository, _series, chapter, tiles, layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    sidecar.document.revision = 2
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )

    root = repository.chapter_root(chapter.chapter_id)
    (root / PENDING_FILE).write_text("{}", encoding="utf-8")
    manifest = json.loads(
        (root / "blender" / "manifest.json").read_text(encoding="utf-8")
    )
    manifest["revision"] = 999
    (root / "blender" / "manifest.json").write_text(
        json.dumps(manifest), encoding="utf-8",
    )

    _chapter, _tiles, recovered = repository.load_chapter(
        chapter.chapter_id, include_blender=True,
    )
    assert recovered.document.revision == 1
    assert recovered.frames[layer.comic_frame_id].frame_id == layer.comic_frame_id
    assert not (root / PENDING_FILE).exists()


def test_missing_or_corrupt_sidecar_is_a_nonfatal_warning(tmp_path):
    repository, _series, chapter, tiles, layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    frame_path = (
        repository.chapter_root(chapter.chapter_id) / "blender" / "frames"
        / f"{layer.comic_frame_id}.json"
    )
    frame_path.unlink()

    loaded, _tiles = repository.load_chapter(chapter.chapter_id)
    assert loaded.chapter_id == chapter.chapter_id
    assert any("Comic frame" in warning for warning in repository.last_load_warnings)
    assert layer.comic_frame_id in repository.last_loaded_blender.unavailable_frame_ids
    # A missing 3D frame must not block saving unrelated 2D chapter edits.
    loaded.name = "Still editable"
    repository.save_chapter(
        loaded, tiles, blender_sidecar=repository.last_loaded_blender,
    )
    assert not frame_path.exists()

    manifest_path = (
        repository.chapter_root(chapter.chapter_id) / "blender" / "manifest.json"
    )
    manifest_path.write_text("not json", encoding="utf-8")
    loaded, _tiles = repository.load_chapter(chapter.chapter_id)
    assert loaded.chapter_id == chapter.chapter_id
    assert any("Could not load" in warning for warning in repository.last_load_warnings)


def test_clone_includes_current_sidecar_but_not_recovery_trees(tmp_path):
    repository, series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    repository.save_chapter(
        chapter, tiles, autosave=True, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )

    destination = tmp_path / "clone"
    clone = repository.clone_to(destination, series)
    cloned_sidecar = clone.load_blender_sidecar(chapter.chapter_id)
    assert cloned_sidecar.document.file_uuid == "blend-file-uuid"
    assert not list(destination.rglob("autosave"))
    assert not list(destination.rglob("last_good"))


def test_clone_rebinds_every_chapter_sidecar_to_new_series_id(tmp_path):
    repository, series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    second, second_tiles = repository.create_chapter(series, "Second")
    second_page = second.layers[second.root_page_ids[0]]
    second_layer = second.add_blender_layer(second_page.layer_id)
    second_frame = ComicFrameDocument(
        frame_id=second_layer.comic_frame_id,
        chapter_id=second.chapter_id,
    )
    second_sidecar = BlenderSidecarData(
        BlenderChapterDocument(
            chapter_id=second.chapter_id,
            series_id=series.series_id,
            file_uuid="second-blend-file",
            revision=4,
            frame_ids=[second_frame.frame_id],
        ),
        {second_frame.frame_id: second_frame},
    )
    repository.save_chapter(
        second, second_tiles, blender_sidecar=second_sidecar,
    )

    cloned_series = copy.deepcopy(series)
    cloned_series.series_id = "cloned-series-id"
    clone = repository.clone_to(tmp_path / "clone-rebound", cloned_series)

    first_clone = clone.load_blender_sidecar(chapter.chapter_id)
    second_clone = clone.load_blender_sidecar(second.chapter_id)
    assert first_clone.document.series_id == cloned_series.series_id
    assert second_clone.document.series_id == cloned_series.series_id
    assert first_clone.document.revision == sidecar.document.revision + 1
    assert second_clone.document.revision == second_sidecar.document.revision + 1
    assert repository.load_blender_sidecar(
        chapter.chapter_id
    ).document.series_id == series.series_id
    assert repository.load_blender_sidecar(
        second.chapter_id
    ).document.series_id == series.series_id


def test_post_save_cache_gc_failure_is_nonfatal_and_reported(
    tmp_path, monkeypatch,
):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )

    def fail_collection(*_args, **_kwargs):
        raise OSError("cache directory is temporarily locked")

    monkeypatch.setattr(repository, "collect_blender_cache", fail_collection)
    chapter.name = "Durably saved"
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        protected_blender_hashes=set(),
    )

    saved = json.loads(
        (repository.chapter_root(chapter.chapter_id) / "chapter.json").read_text(
            encoding="utf-8"
        )
    )
    assert saved["name"] == "Durably saved"
    assert not (
        repository.chapter_root(chapter.chapter_id) / PENDING_FILE
    ).exists()
    assert repository.last_save_warnings == [
        "Chapter saved, but 3D cache cleanup was skipped: "
        "cache directory is temporarily locked"
    ]


def test_cache_gc_requires_explicit_undo_hashes_and_preserves_references(tmp_path):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache = BlenderSidecarRepository(
        repository.chapter_root(chapter.chapter_id) / "blender"
    )
    undo_blob = b"only an in-memory undo snapshot references this"
    undo_hash = cache.write_blob(undo_blob)

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes={undo_hash},
    ) == set()
    assert cache.blob_path(undo_hash).is_file()
    assert cache.blob_path(digest).is_file()

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == {undo_hash}
    assert not cache.blob_path(undo_hash).exists()
    assert cache.blob_path(digest).is_file()


def test_cache_gc_prunes_obsolete_revision_manifest_and_its_blob(tmp_path):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache, old_manifest, old_hash = add_historical_cache_revision(
        repository, chapter, sidecar,
    )

    chapter.name = "Successful save triggers GC"
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        protected_blender_hashes=set(),
    )
    assert not cache.cache_revision_path(old_manifest.revision).exists()
    assert not cache.blob_path(old_hash).exists()
    assert cache.cache_revision_path("cache-1").is_file()
    assert cache.blob_path(digest).is_file()
    assert sidecar.document.cache_revisions == ["cache-1"]
    assert repository.load_blender_sidecar(
        chapter.chapter_id
    ).document.cache_revisions == ["cache-1"]


def test_cache_gc_keeps_revision_selected_by_frame_or_undo_hash(tmp_path):
    repository, _series, chapter, tiles, layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache, old_manifest, old_hash = add_historical_cache_revision(
        repository, chapter, sidecar,
    )
    frame = sidecar.frames[layer.comic_frame_id]
    frame.baked_variant_hashes["fallback"] = old_hash
    cache.save(sidecar)

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == set()
    assert cache.cache_revision_path(old_manifest.revision).is_file()
    assert cache.blob_path(old_hash).is_file()

    frame.baked_variant_hashes.clear()
    cache.save(sidecar)
    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes={old_hash},
    ) == set()
    assert cache.cache_revision_path(old_manifest.revision).is_file()

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == {old_hash}
    assert not cache.cache_revision_path(old_manifest.revision).exists()


@pytest.mark.parametrize("recovery_name", ["autosave", "last_good"])
def test_cache_gc_keeps_revision_required_by_recovery_state(
    tmp_path, recovery_name,
):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache, old_manifest, old_hash = add_historical_cache_revision(
        repository, chapter, sidecar,
    )
    recovery_document = copy.deepcopy(sidecar.document)
    recovery_document.current_cache_revision = old_manifest.revision
    recovery_document.cache_revisions = [old_manifest.revision]
    recovery_sidecar = BlenderSidecarData(
        recovery_document,
        copy.deepcopy(sidecar.frames),
        copy.deepcopy(old_manifest),
    )
    recovery_root = (
        repository.chapter_root(chapter.chapter_id)
        / recovery_name / "blender"
    )
    BlenderSidecarRepository(recovery_root).save(
        recovery_sidecar,
        fallback_blob_roots=[cache.blobs_root],
    )

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == set()
    assert cache.cache_revision_path(old_manifest.revision).is_file()
    assert cache.blob_path(old_hash).is_file()

    shutil.rmtree(recovery_root.parent)
    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == {old_hash}
    assert not cache.cache_revision_path(old_manifest.revision).exists()


def test_cache_gc_keeps_revision_required_by_queued_inbox(tmp_path):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache, old_manifest, old_hash = add_historical_cache_revision(
        repository, chapter, sidecar,
    )
    ready = cache.root / "inbox" / "transaction.ready"
    ready.mkdir(parents=True)
    (ready / "bundle.json").write_text(json.dumps({
        "cache_manifest": {
            "revision": old_manifest.revision,
            "base_glb_hash": old_hash,
        },
    }), encoding="utf-8")

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == set()
    assert cache.cache_revision_path(old_manifest.revision).is_file()
    assert cache.blob_path(old_hash).is_file()

    shutil.rmtree(ready)
    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == {old_hash}
    assert not cache.cache_revision_path(old_manifest.revision).exists()


def test_cache_gc_aborts_before_mutation_when_any_revision_is_corrupt(tmp_path):
    repository, _series, chapter, tiles, _layer, sidecar, digest, blob = (
        build_project(tmp_path)
    )
    repository.save_chapter(
        chapter, tiles, blender_sidecar=sidecar,
        blender_blobs={digest: blob},
    )
    cache, old_manifest, old_hash = add_historical_cache_revision(
        repository, chapter, sidecar,
    )
    orphan_hash = cache.write_blob(b"unreferenced but protected on abort")
    corrupt = cache.cache_revision_path("corrupt")
    corrupt.write_text("{not-json", encoding="utf-8")

    assert repository.collect_blender_cache(
        chapter.chapter_id, protected_hashes=set(),
    ) == set()
    assert corrupt.is_file()
    assert cache.cache_revision_path(old_manifest.revision).is_file()
    assert cache.blob_path(old_hash).is_file()
    assert cache.blob_path(orphan_hash).is_file()
    assert set(repository.load_blender_sidecar(
        chapter.chapter_id
    ).document.cache_revisions) == {"cache-old", "cache-1"}
