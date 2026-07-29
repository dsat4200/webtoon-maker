from __future__ import annotations

import json
import os
import time

import pytest
from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.models import ChapterDocument, RasterObject
from comic_editor.core.persistence import SeriesRepository


def test_series_chapter_and_sparse_tiles_round_trip(tmp_path):
    repository = SeriesRepository(tmp_path / "demo")
    series = repository.create("Demo Series")
    chapter, tiles = repository.create_chapter(series, "Opening")
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    tiles.paint_dab(raster.object_id, QPointF(10, 10), 8, QColor("black"))
    tiles.paint_dab(raster.object_id, QPointF(900, 2200), 8, QColor("black"))
    repository.save_chapter(chapter, tiles)

    loaded_series = repository.load_series()
    loaded, loaded_tiles = repository.load_chapter(chapter.chapter_id)
    assert loaded_series.name == "Demo Series"
    assert loaded.to_dict() == chapter.to_dict()
    assert {key for key, _ in loaded_tiles.iter_tiles(raster.object_id)} == {(0, 0), (3, 8)}


def test_autosave_recovery_is_newer_and_independent(tmp_path):
    repository = SeriesRepository(tmp_path / "demo")
    series = repository.create("Demo")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    chapter.name = "Recovered name"
    repository.save_chapter(chapter, tiles, autosave=True)
    recovery_file = repository.chapter_root(chapter.chapter_id) / "autosave" / "chapter.json"
    future = time.time() + 2
    os.utime(recovery_file, (future, future))
    assert repository.has_recovery(chapter.chapter_id)
    recovered, _ = repository.load_chapter(chapter.chapter_id, recover=True)
    manual, _ = repository.load_chapter(chapter.chapter_id)
    assert recovered.name == "Recovered name"
    assert manual.name == "Chapter"


def test_future_schema_is_rejected_without_rewrite(tmp_path):
    repository = SeriesRepository(tmp_path / "demo")
    series = repository.create("Demo")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    path = repository.chapter_root(chapter.chapter_id) / "chapter.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    data["schema_version"] = 999
    original = json.dumps(data)
    path.write_text(original, encoding="utf-8")
    with pytest.raises(ValueError, match="future"):
        repository.load_chapter(chapter.chapter_id)
    assert path.read_text(encoding="utf-8") == original


def test_manual_save_clears_recovery(tmp_path):
    repository = SeriesRepository(tmp_path / "demo")
    series = repository.create("Demo")
    chapter, tiles = repository.create_chapter(series, "Chapter")
    repository.save_chapter(chapter, tiles, autosave=True)
    assert (repository.chapter_root(chapter.chapter_id) / "autosave").exists()
    repository.save_chapter(chapter, tiles)
    assert not (repository.chapter_root(chapter.chapter_id) / "autosave").exists()


def test_interrupted_multifile_save_restores_last_good_revision(tmp_path):
    repository = SeriesRepository(tmp_path / "demo")
    series = repository.create("Demo")
    chapter, tiles = repository.create_chapter(series, "Good")
    raster = next(obj for obj in chapter.objects.values() if isinstance(obj, RasterObject))
    tiles.paint_dab(raster.object_id, QPointF(20, 20), 12, QColor("black"))
    repository.save_chapter(chapter, tiles)

    # A second completed save creates the last-good snapshot.
    chapter.name = "Second"
    repository.save_chapter(chapter, tiles)
    root = repository.chapter_root(chapter.chapter_id)
    (root / ".save_pending").write_text("{}", encoding="utf-8")
    data = json.loads((root / "chapter.json").read_text(encoding="utf-8"))
    data["name"] = "Partial"
    (root / "chapter.json").write_text(json.dumps(data), encoding="utf-8")

    recovered, recovered_tiles = repository.load_chapter(chapter.chapter_id)
    assert recovered.name == "Good"
    assert [key for key, _ in recovered_tiles.iter_tiles(raster.object_id)] == [(0, 0)]
    assert not (root / ".save_pending").exists()
