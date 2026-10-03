"""Raster history evicts losslessly and survives later edits and publication."""
import gc
from pathlib import Path

import numpy as np
import pytest
from PySide6.QtCore import QPoint
from PySide6.QtGui import QColorSpace, QImage

from comic_editor.core.commands import CommandStack, TilePatchCommand
from comic_editor.core.tile_history import HistoryTileMap, TileHistoryCache
from comic_editor.core.tiles import TileStore


@pytest.mark.parametrize('format,dtype', [
    (QImage.Format_RGBA8888_Premultiplied, np.uint8),
    (QImage.Format_RGBA16FPx4_Premultiplied, np.float16),
    (QImage.Format_RGBA32FPx4_Premultiplied, np.float32),
])
def test_spilled_history_keeps_precision_metadata_and_isolated_pixels(format, dtype):
    data = np.random.default_rng(30).random((31, 37, 4)).astype(dtype)
    if dtype == np.uint8:
        data = np.random.default_rng(30).integers(0, 256, (31, 37, 4), dtype=dtype)
    image = QImage(data.data, 37, 31, data.strides[0], format).copy()
    image.setColorSpace(QColorSpace(QColorSpace.SRgbLinear))
    image.setDevicePixelRatio(1.5)
    image.setDotsPerMeterX(900)
    image.setDotsPerMeterY(1000)
    image.setOffset(QPoint(3, 4))
    image.setText('Artist', 'Example')
    expected = bytes(image.constBits())
    cache = TileHistoryCache(0)
    values = HistoryTileMap(cache, {(0, 0): image, (1, 0): None})
    image.fill(0)
    restored = values[(0, 0)]
    assert bytes(restored.constBits()) == expected
    assert restored.format() == format
    assert restored.colorSpace() == QColorSpace(QColorSpace.SRgbLinear)
    assert restored.devicePixelRatio() == 1.5
    assert (restored.dotsPerMeterX(), restored.dotsPerMeterY()) == (900, 1000)
    assert restored.offset() == QPoint(3, 4) and restored.text('Artist') == 'Example'
    restored.fill(0)
    assert bytes(values[(0, 0)].constBits()) == expected
    assert values[(1, 0)] is None and cache.bytes == 0


def test_many_commands_share_one_budget_and_restore_exact_tiles(tmp_path):
    store = TileStore(tile_size=16)
    store._history_cache = TileHistoryCache(16 * 16 * 4)
    history, expected = CommandStack(), []
    for step in range(10):
        before = store.snapshot('art', {(0, 0)})
        data = np.random.default_rng(step).integers(1, 256, (16, 16, 4), np.uint8)
        data[..., 3] = 255
        image = QImage(data.data, 16, 16, data.strides[0], QImage.Format_RGBA8888_Premultiplied).copy()
        store.set_tile('art', (0, 0), image)
        after = store.snapshot('art', {(0, 0)})
        expected.append(QImage(store.tile('art', (0, 0))))
        history.push(TilePatchCommand('Stroke', store, 'art', before, after), already_done=True)
        assert store._history_cache.bytes <= store._history_cache.budget
    store.save_directory(tmp_path / 'tiles', {'art'}, complete=True)
    for step in reversed(range(10)):
        history.undo()
        current = store.tile('art', (0, 0))
        assert current is None if step == 0 else current == expected[step - 1]
    for image in expected:
        history.redo()
        assert store.tile('art', (0, 0)) == image
    assert store._history_cache.bytes <= store._history_cache.budget
    assert store._history_cache.spills > 0 and store._history_cache.reads > 0
    command = history.top_undo_command
    command.after[(0, 0)] = expected[0]
    assert command.after[(0, 0)] == expected[0]
    private_root = store._history_cache._backing.root
    del command, before, after
    history.clear()
    gc.collect()
    assert store._history_cache.bytes == 0 and not list(private_root.iterdir())


def test_unavailable_private_disk_keeps_recoverable_history(monkeypatch):
    cache = TileHistoryCache(0)
    image = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    image.fill(0xff123456)
    monkeypatch.setattr(cache, '_spill', lambda *_: (_ for _ in ()).throw(OSError('Full disk')))
    values = HistoryTileMap(cache, {(0, 0): image})
    assert values[(0, 0)] == image
    assert isinstance(cache.failure, OSError)


def test_repeat_undo_reuses_immutable_history_bounds(monkeypatch):
    from PySide6.QtGui import QColor
    store = TileStore(tile_size=16)
    red = QImage(16, 16, QImage.Format_ARGB32_Premultiplied)
    red.fill(QColor('red'))
    blue = QImage(red)
    blue.setPixelColor(3, 4, QColor('blue'))
    command = TilePatchCommand('Stroke', store, 'art', {(0, 0): red}, {(0, 0): blue})
    original, calls = store._alpha_bbox, []
    def counted(image):
        calls.append(image.cacheKey())
        return original(image)
    monkeypatch.setattr(store, '_alpha_bbox', counted)
    for _ in range(4):
        command.undo()
        assert store.content_bounds('art').getRect() == (0., 0., 16., 16.)
        command.redo()
        assert store.tile('art', (0, 0)) == blue
    assert len(calls) == 2
