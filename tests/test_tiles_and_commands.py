from __future__ import annotations

from PySide6.QtCore import QPointF
from PySide6.QtGui import QColor

from comic_editor.core.commands import CommandStack, TilePatchCommand
from comic_editor.core.tiles import TileStore


def test_blank_tall_document_allocates_no_raster_area():
    store = TileStore()
    assert list(store.iter_tiles("missing")) == []
    assert "missing" not in store._tiles


def test_single_tile_edit_does_not_create_unrelated_tiles():
    store = TileStore()
    store.paint_dab("object", QPointF(20, 20), 12, QColor("black"))
    assert [key for key, _ in store.iter_tiles("object")] == [(0, 0)]
    assert store.dirty == {("object", 0, 0)}


def test_tile_patch_undo_redo():
    store = TileStore()
    before = {(0, 0): None}
    store.paint_dab("object", QPointF(20, 20), 12, QColor("black"))
    after = store.snapshot("object", {(0, 0)})
    stack = CommandStack()
    stack.push(TilePatchCommand("Stroke", store, "object", before, after), already_done=True)
    stack.undo()
    assert list(store.iter_tiles("object")) == []
    stack.redo()
    assert [key for key, _ in store.iter_tiles("object")] == [(0, 0)]


def test_erasing_to_empty_prunes_sparse_tile():
    store = TileStore()
    store.paint_dab("object", QPointF(20, 20), 12, QColor("black"))
    before = {}
    store.paint_dab(
        "object", QPointF(20, 20), 40, QColor("black"), erase=True, before=before
    )
    store.prune_empty("object", set(before))
    assert list(store.iter_tiles("object")) == []
