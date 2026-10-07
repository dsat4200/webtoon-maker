"""Transfer ready original pixels between consecutive detached scene owners.

Only existing source LRUs supply buffers. Immutable source pins and native tile
versions guard adoption; no rendered result, disk key, or live editor crosses
this short handoff. The replacement store keeps its own bounded bookkeeping.
"""
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtGui import QImage

from comic_editor.core.tile_backing import EditableTile


def _identity(snapshot):
    return (snapshot.document.identity, snapshot.document.pixel_contract.signature,
            getattr(snapshot.pixel_environment, 'signature', None))


def _image_identifier(key):
    if isinstance(key, str):
        return key
    if isinstance(key, tuple) and len(key) > 1 and key[0] in ('native', 'working'):
        return key[1]
    return None


def _tile_pin(owner, key, *, prepare=False):
    value = owner.entries.get(key)
    if isinstance(value, Path):
        if prepare:
            owner.residency.prepare(owner, key)
        pin = owner.snapshot_pins.get(key)
        return ('file', pin) if pin is not None else None
    if isinstance(value, EditableTile) and value.frozen is not None:
        return 'frozen', value.frozen
    return None


@dataclass(frozen=True)
class _ImageBuffer:
    key: object
    pin: object
    image: QImage


@dataclass(frozen=True)
class _TileBuffer:
    identifier: str
    key: tuple
    version: tuple
    pin: object
    image: QImage


class ReadyOriginals:
    """A worker-only handoff, consumed into the replacement's existing LRUs."""
    def __init__(self):
        self.identity = None
        self.images = ()
        self.tiles = ()

    @property
    def byte_count(self):
        return sum(record.image.sizeInBytes() for record in (*self.images, *self.tiles))

    def clear(self):
        self.identity = None
        self.images = self.tiles = ()

    def capture(self, snapshot):
        self.identity = _identity(snapshot)
        images = []
        for key, image in snapshot.images._decoded.items():
            identifier = _image_identifier(key)
            source = snapshot.images._sources.get(identifier)
            if source is not None:
                images.append(_ImageBuffer(key, source._encoded.pin, QImage(image)))
        self.images = tuple(images)
        tiles = []
        for (owner, key), (image, _content_key) in snapshot.tiles.residency.entries.items():
            version = owner.version(key)
            pin = _tile_pin(owner, key)
            if pin is not None:
                tiles.append(_TileBuffer(owner.object_id, key, version, pin, QImage(image)))
        self.tiles = tuple(tiles)

    def adopt(self, snapshot):
        if self.identity != _identity(snapshot):
            self.clear()
            return
        images, tiles = snapshot.images, snapshot.tiles
        # Imported/edited buffers already captured from the document remain
        # the most recent entries if incoming ready originals fill the budget.
        current_images = tuple(images._decoded.items())
        current_tiles = tuple(tiles.residency.entries.items())
        for record in self.images:
            source = images._sources.get(_image_identifier(record.key))
            if source is not None and source._encoded.pin is record.pin:
                images._cache_decoded(record.key, QImage(record.image))
        for key, image in current_images:
            images._cache_decoded(key, image)
        for record in self.tiles:
            owner = tiles._tiles.get(record.identifier)
            if (owner is not None and record.key in owner
                    and owner.version(record.key) == record.version
                    and _tile_pin(owner, record.key, prepare=True) == record.pin):
                tiles.residency.retain(owner, record.key, QImage(record.image))
        for (owner, key), (image, _content_key) in current_tiles:
            tiles.residency.retain(owner, key, image)
        # The new stores now own the handles. Retaining a second source LRU
        # here would defeat their eviction budgets across later evaluations.
        self.images = self.tiles = ()
