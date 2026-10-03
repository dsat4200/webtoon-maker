"""Lossless, bounded raster history shared by all commands for a tile store."""
from collections import OrderedDict
from collections.abc import MutableMapping
import logging
import uuid
import weakref

from PySide6.QtGui import QImage

from .tile_backing import SnapshotBacking, PinnedTile


class HistoryImage:
    def __init__(self, cache, image):
        self.cache, self.key, self.pin = cache, uuid.uuid4().hex, None
        self.width, self.height, self.stride = image.width(), image.height(), image.bytesPerLine()
        self.format, self.color_space = image.format(), image.colorSpace()
        self.dpr = image.devicePixelRatio()
        self.dpm = image.dotsPerMeterX(), image.dotsPerMeterY()
        self.offset, self.colors = image.offset(), image.colorTable()
        self.text = {key: image.text(key) for key in image.textKeys()}
        self._alpha_bounds = None
        self._bounds_known = False
        cache.retain(self, QImage(image))

    def image(self):
        return self.cache.read(self)

    def __del__(self):
        self.cache.forget(self.key)


class TileHistoryCache:
    def __init__(self, budget=64 * 1024 * 1024):
        self.budget, self.bytes = max(0, int(budget)), 0
        self._entries = OrderedDict()
        self._backing = None
        self.spills = self.reads = 0
        self.failure = None

    def forget(self, key):
        entry = self._entries.pop(key, None)
        if entry is not None:
            self.bytes -= int(entry[1].sizeInBytes())

    def _spill(self, value, image):
        if value.pin is not None:
            return
        if self._backing is None:
            self._backing = SnapshotBacking()
        path = self._backing.root / f'{value.key}.raw'
        try:
            with path.open('wb') as stream:
                stream.write(image.constBits())
        except OSError:
            path.unlink(missing_ok=True)
            raise
        value.pin = PinnedTile(self._backing, path)
        self.spills += 1

    def _trim(self):
        while self._entries and self.bytes > self.budget:
            key, (reference, image) = next(iter(self._entries.items()))
            value = reference()
            if value is not None:
                try:
                    self._spill(value, image)
                except OSError as error:
                    # An unavailable/full temporary disk must not discard a
                    # user's undo data. Keep it resident and expose the failure.
                    if self.failure is None:
                        logging.getLogger(__name__).warning('Could not spill raster undo: %s', error)
                    self.failure = error
                    return
            self.forget(key)

    def retain(self, value, image):
        self.forget(value.key)
        self._entries[value.key] = weakref.ref(value), image
        self.bytes += int(image.sizeInBytes())
        self._trim()

    def capture(self, image):
        return None if image is None else HistoryImage(self, image)

    def read(self, value):
        entry = self._entries.get(value.key)
        if entry is not None:
            self._entries.move_to_end(value.key)
            return QImage(entry[1])
        if value.pin is None:
            raise OSError('Raster history has no recoverable pixels')
        data = value.pin.path.read_bytes()
        if len(data) != value.stride * value.height:
            raise OSError('Raster history tile was truncated')
        image = QImage(data, value.width, value.height, value.stride, value.format).copy()
        image.setColorSpace(value.color_space)
        image.setDevicePixelRatio(value.dpr)
        image.setDotsPerMeterX(value.dpm[0])
        image.setDotsPerMeterY(value.dpm[1])
        image.setOffset(value.offset)
        image.setColorTable(value.colors)
        for key, text in value.text.items():
            image.setText(key, text)
        self.reads += 1
        self.retain(value, image)
        return QImage(image)


class HistoryTileMap(MutableMapping):
    """Mapping interface retained for fill replay's focused command updates."""
    def __init__(self, cache, values):
        self.cache, self._values = cache, {}
        for key, image in values.items():
            self[key] = image

    def __getitem__(self, key):
        value = self._values[key]
        return None if value is None else value.image()

    def __setitem__(self, key, image):
        self._values[key] = self.cache.capture(image)

    def __delitem__(self, key):
        del self._values[key]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)

    def items_with_bounds(self, bbox):
        for key, value in self._values.items():
            if value is None:
                yield key, None, None
            else:
                image = value.image()
                if not value._bounds_known:
                    value._alpha_bounds = None if image.isNull() else bbox(image)
                    value._bounds_known = True
                yield key, image, value._alpha_bounds
