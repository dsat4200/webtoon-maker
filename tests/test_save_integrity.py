from __future__ import annotations

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QBuffer, QByteArray, QIODevice, QPointF
from PySide6.QtGui import QColor, QImage

from comic_editor.core import assets, persistence
from comic_editor.core.assets import AssetRepository, extract_asset
from comic_editor.core.images import ImageStore
from comic_editor.core.models import ChapterDocument, ImageObject, RasterObject, ToneMask
from comic_editor.core.persistence import SeriesRepository
from comic_editor.core.tiles import TileStore


def _png(color):
    image = QImage(8, 8, QImage.Format_ARGB32_Premultiplied)
    image.fill(QColor(color))
    payload = QByteArray()
    buffer = QBuffer(payload)
    buffer.open(QIODevice.WriteOnly)
    assert image.save(buffer, "PNG")
    return bytes(payload)


@pytest.fixture(params=["chapter", "asset"])
def drawing(request, tmp_path):
    chapter = ChapterDocument(name="Good")
    page = chapter.add_page()
    chapter.add_object(page.layer_id, RasterObject())
    tiles = TileStore()
    if request.param == "asset":
        manifest, tiles = extract_asset(chapter, tiles, "layer", page.layer_id, "Asset")
        chapter = manifest.document
        repository = AssetRepository(tmp_path)
        root = repository.asset_root(manifest.asset_id)
        module, filename = assets, assets.ASSET_FILE
        identifier = manifest.asset_id
    else:
        repository = SeriesRepository(tmp_path / "Series")
        repository.create("Series")
        root = repository.chapter_root(chapter.chapter_id)
        module, filename = persistence, persistence.CHAPTER_FILE
        identifier = chapter.chapter_id
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    mask = ToneMask(name="Mask", saved=True)
    chapter.masks[mask.mask_id] = mask
    image = chapter.add_object(raster.parent_layer_id, ImageObject(
        source_filename="original.png", source_mime_type="image/png",
        pixel_width=8, pixel_height=8,
    ))
    images = ImageStore()

    def revision(name, color):
        chapter.name = name
        for object_id in (raster.object_id, mask.mask_id):
            tiles.remove_object(object_id)
            for point in (QPointF(-20, -20), QPointF(20, 20)):
                tiles.paint_dab(object_id, point, 8, QColor(color))
        images.put(image.object_id, image.source_filename, _png(color), "image/png")

    def save(autosave=False):
        if request.param == "asset":
            repository.save(manifest, tiles, images=images, autosave=autosave)
        else:
            repository.save_chapter(chapter, tiles, images, autosave=autosave)

    def load(autosave=False):
        if request.param == "asset":
            value, saved_tiles, saved_images = repository.load(
                identifier, recover=autosave, include_images=True,
            )
            return value.document, saved_tiles, saved_images
        return repository.load_chapter(identifier, recover=autosave, include_images=True)

    revision("Good", "red")
    return SimpleNamespace(
        chapter=chapter, tiles=tiles, images=images, raster=raster, mask=mask,
        image=image, root=root, module=module, filename=filename,
        revision=revision, save=save, load=load,
        has_recovery=lambda: repository.has_recovery(identifier),
    )


def _assert_revision(drawing, name, color, autosave=False):
    chapter, tiles, images = drawing.load(autosave)
    assert chapter.name == name
    for object_id in (drawing.raster.object_id, drawing.mask.mask_id):
        assert set(tiles.object_tiles(object_id)) == {(-1, -1), (0, 0)}
        assert tiles.tile(object_id, (0, 0)).pixelColor(20, 20) == QColor(color)
        assert tiles.tile(object_id, (-1, -1)).pixelColor(236, 236) == QColor(color)
    assert images.source(drawing.image.object_id).data == _png(color)


@pytest.mark.parametrize("autosave", [False, True])
@pytest.mark.parametrize("operation", ["erase", "clear", "move"])
def test_saved_tiles_match_erased_cleared_or_moved_content(drawing, autosave, operation):
    drawing.save(autosave)
    for object_id in (drawing.raster.object_id, drawing.mask.mask_id):
        if operation == "erase":
            before = {}
            for point in (QPointF(-20, -20), QPointF(20, 20)):
                drawing.tiles.paint_dab(
                    object_id, point, 40, QColor("black"), erase=True, before=before,
                )
            drawing.tiles.prune_empty(object_id, set(before))
        elif operation == "clear":
            drawing.tiles.remove_object(object_id)
        else:
            old = drawing.tiles.object_tiles(object_id)
            drawing.tiles.replace_object_tiles(
                object_id, {(x + 3, y + 2): tile for (x, y), tile in old.items()},
            )
    # Complete snapshots must be independent of dirty bookkeeping.
    drawing.tiles.dirty.clear()
    drawing.save(autosave)
    drawing.save(autosave)
    _, loaded, _ = drawing.load(autosave)
    for object_id in (drawing.raster.object_id, drawing.mask.mask_id):
        expected = drawing.tiles.object_tiles(object_id)
        actual = loaded.object_tiles(object_id)
        assert set(actual) == set(expected)
        assert all(actual[key] == expected[key] for key in expected)


@pytest.mark.parametrize("autosave", [False, True])
@pytest.mark.parametrize("failure", ["resources", "manifest", "commit"])
def test_interrupted_save_restores_one_complete_revision(drawing, autosave, failure, monkeypatch):
    drawing.save(autosave)
    root = drawing.root / "autosave" if autosave else drawing.root
    drawing.revision("Partial", "blue")
    original_atomic = drawing.module.atomic_json
    original_images = drawing.images.save_directory
    original_unlink = Path.unlink

    def fail_atomic(path, payload):
        if path == root / drawing.filename:
            raise OSError("injected manifest failure")
        original_atomic(path, payload)

    def fail_images(*args, **kwargs):
        original_images(*args, **kwargs)
        raise OSError("injected resource failure")

    def fail_commit(path, *args, **kwargs):
        if path == root / persistence.PENDING_FILE:
            raise OSError("injected commit failure")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        if failure == "resources":
            fault.setattr(drawing.images, "save_directory", fail_images)
        elif failure == "manifest":
            fault.setattr(drawing.module, "atomic_json", fail_atomic)
        else:
            fault.setattr(Path, "unlink", fail_commit)
        with pytest.raises(OSError, match="injected"):
            drawing.save(autosave)
    assert drawing.chapter.name == "Partial"
    assert drawing.image.object_id in drawing.images.dirty
    assert (root / persistence.PENDING_FILE).exists()
    _assert_revision(drawing, "Good", "red", autosave)
    assert not (root / persistence.PENDING_FILE).exists()


@pytest.mark.parametrize("autosave", [False, True])
def test_repeated_failed_saves_keep_good_backup_and_can_retry(drawing, autosave, monkeypatch):
    drawing.save(autosave)
    root = drawing.root / "autosave" if autosave else drawing.root
    drawing.revision("Retry", "blue")
    original_atomic = drawing.module.atomic_json

    def fail_manifest(path, payload):
        if path == root / drawing.filename:
            raise OSError("injected")
        original_atomic(path, payload)

    with monkeypatch.context() as fault:
        fault.setattr(drawing.module, "atomic_json", fail_manifest)
        for _ in range(2):
            with pytest.raises(OSError, match="injected"):
                drawing.save(autosave)
    _assert_revision(drawing, "Good", "red", autosave)
    assert drawing.chapter.name == "Retry"
    drawing.save(autosave)
    _assert_revision(drawing, "Retry", "blue", autosave)


@pytest.mark.parametrize("autosave", [False, True])
def test_first_save_failure_is_not_recoverable_but_retry_succeeds(drawing, autosave, monkeypatch):
    root = drawing.root / "autosave" if autosave else drawing.root
    original_unlink = Path.unlink

    def fail_commit(path, *args, **kwargs):
        if path == root / persistence.PENDING_FILE:
            raise OSError("injected")
        return original_unlink(path, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(Path, "unlink", fail_commit)
        with pytest.raises(OSError, match="injected"):
            drawing.save(autosave)
    assert not drawing.has_recovery()
    with pytest.raises(OSError, match="no recoverable revision"):
        drawing.load(autosave)
    drawing.revision("Retry", "blue")
    drawing.save(autosave)
    assert not (root / persistence.LAST_GOOD_DIR / drawing.filename).exists()
    _assert_revision(drawing, "Retry", "blue", autosave)


def test_autosave_offer_uses_complete_backup_timestamp(drawing, monkeypatch):
    drawing.save()
    drawing.revision("Recovery", "blue")
    drawing.save(True)
    recovery_root = drawing.root / "autosave"
    stamp = (drawing.root / drawing.filename).stat().st_mtime + 10
    os.utime(recovery_root / drawing.filename, (stamp, stamp))
    original = drawing.module.atomic_json

    def fail_manifest(path, payload):
        if path == recovery_root / drawing.filename:
            raise OSError("injected")
        original(path, payload)

    drawing.revision("Partial", "green")
    with monkeypatch.context() as fault:
        fault.setattr(drawing.module, "atomic_json", fail_manifest)
        with pytest.raises(OSError):
            drawing.save(True)
    assert drawing.has_recovery()
    _assert_revision(drawing, "Recovery", "blue", True)
    _assert_revision(drawing, "Good", "red")


@pytest.mark.parametrize("autosave", [False, True])
def test_retry_without_reopening_keeps_current_edits_and_complete_backup(drawing, autosave, monkeypatch):
    drawing.save(autosave)
    drawing.revision("Retry", "blue")
    root = drawing.root / "autosave" if autosave else drawing.root
    original = drawing.module.atomic_json

    def fail_manifest(path, payload):
        if path == root / drawing.filename:
            raise OSError("injected")
        original(path, payload)

    with monkeypatch.context() as fault:
        fault.setattr(drawing.module, "atomic_json", fail_manifest)
        with pytest.raises(OSError):
            drawing.save(autosave)
    before = drawing.chapter.to_dict()
    drawing.save(autosave)
    assert drawing.chapter.to_dict() == before
    _assert_revision(drawing, "Retry", "blue", autosave)
    backup = root / persistence.LAST_GOOD_DIR
    data = json.loads((backup / drawing.filename).read_text(encoding="utf-8"))
    document = data if drawing.filename == persistence.CHAPTER_FILE else data["document"]
    assert document["name"] == "Good"
    assert QImage(str(backup / "raster" / drawing.raster.object_id / "0_0.png")).pixelColor(20, 20) == QColor("red")


@pytest.mark.parametrize("autosave", [False, True])
def test_failed_recovery_during_retry_does_not_replace_backup(drawing, autosave, monkeypatch):
    drawing.save(autosave)
    drawing.revision("Retry", "blue")
    root = drawing.root / "autosave" if autosave else drawing.root
    original_atomic = drawing.module.atomic_json
    original_copy = persistence.shutil.copytree

    def fail_manifest(path, payload):
        if path == root / drawing.filename:
            raise OSError("injected write failure")
        original_atomic(path, payload)

    with monkeypatch.context() as fault:
        fault.setattr(drawing.module, "atomic_json", fail_manifest)
        with pytest.raises(OSError):
            drawing.save(autosave)
    backup = root / persistence.LAST_GOOD_DIR
    before = {path.relative_to(backup): path.read_bytes() for path in backup.rglob("*") if path.is_file()}

    def fail_restore(source, target, *args, **kwargs):
        if Path(target) == root / "images":
            raise OSError("injected recovery failure")
        return original_copy(source, target, *args, **kwargs)

    with monkeypatch.context() as fault:
        fault.setattr(persistence.shutil, "copytree", fail_restore)
        with pytest.raises(OSError, match="recovery failure"):
            drawing.save(autosave)
    assert (root / persistence.PENDING_FILE).exists()
    assert {path.relative_to(backup): path.read_bytes() for path in backup.rglob("*") if path.is_file()} == before
    assert drawing.chapter.name == "Retry"
    _assert_revision(drawing, "Good", "red", autosave)


def test_incremental_tile_save_still_preserves_unchanged_tiles(tmp_path):
    tiles = TileStore()
    tiles.paint_dab("raster", QPointF(20, 20), 8, QColor("red"))
    tiles.paint_dab("raster", QPointF(-20, -20), 8, QColor("blue"))
    tiles.save_directory(tmp_path, {"raster"})
    unchanged = tmp_path / "raster" / "-1_-1.png"
    original_bytes, original_mtime = unchanged.read_bytes(), unchanged.stat().st_mtime_ns
    tiles.set_tile("raster", (0, 0), None)
    tiles.save_directory(tmp_path, {"raster"})
    assert not (tmp_path / "raster" / "0_0.png").exists()
    assert unchanged.read_bytes() == original_bytes
    assert unchanged.stat().st_mtime_ns == original_mtime
    assert not tiles.dirty
