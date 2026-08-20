from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF
from PySide6.QtGui import QColor, QImage

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


def test_flood_fill_is_four_connected_across_sparse_tiles():
    store = TileStore(tile_size=8)
    barrier = QImage(8, 8, QImage.Format.Format_ARGB32_Premultiplied)
    barrier.fill(QColor(0, 0, 0, 0))
    for y in range(8):
        barrier.setPixelColor(7, y, QColor("black"))
    store.set_tile("object", (0, 0), barrier)
    before = {}

    dirty = store.flood_fill(
        "object", QPointF(2, 2), QRectF(0, 0, 16, 8),
        QColor("red"), tolerance=0, before=before,
    )

    assert dirty == QRectF(0, 0, 7, 8)
    assert set(before) == {(0, 0)}
    assert store.tile("object", (0, 0)).pixelColor(2, 2) == QColor("red")
    assert store.tile("object", (1, 0)) is None


def test_flood_fill_tolerance_and_transparent_hidden_rgb():
    store = TileStore(tile_size=8)
    image = QImage(8, 8, QImage.Format.Format_RGBA8888)
    image.fill(QColor(10, 20, 30, 0))
    image.setPixelColor(4, 4, QColor(200, 100, 50, 0))
    image.setPixelColor(5, 4, QColor(20, 20, 20, 40))
    store.set_tile("object", (0, 0), image)

    store.flood_fill(
        "object", QPointF(0, 0), QRectF(0, 0, 8, 8),
        QColor("blue"), tolerance=0,
    )

    tile = store.tile("object", (0, 0))
    assert tile.pixelColor(4, 4) == QColor("blue")
    assert tile.pixelColor(5, 4).alpha() == 40
