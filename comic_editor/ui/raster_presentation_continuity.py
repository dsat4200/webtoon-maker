"""Bounded provisional presentation retained across committed native contacts.

These already composed native images never enter the projection/artwork cache.
They preserve the most recent displayed ink while its exact scene is pending.
"""
from PySide6.QtGui import QImage


class RasterPresentationContinuity:
    def __init__(self, budget=64 * 1024 * 1024):
        self.budget = budget
        self.reset()

    def reset(self):
        self.identity = None
        self.minimum_revision = -1
        self.clear()

    def clear(self):
        self.document = None
        self.batch = ()
        self.overlays = {}
        self.byte_count = 0
        self.view = None

    def changed(self, change, action, document):
        # A forward source packet can extend this same displayed scene. A
        # restore, placement, effects, visibility or ordering change cannot.
        if (action in {'undo', 'redo'} or change.conservative or change.document_fields or change.orders
                or any(item.affects_artwork and (item.structural
                    or item.entity[0] != 'object' or item.fields - {'pixels', 'interaction_rect'})
                    for item in change.entities)
                or any(item.resource != 'raster' or item.entity[0] != 'object'
                    for item in change.resources)):
            self._bind(document)
            # Publication precedes exact projection invalidation. No delayed
            # pre-restore contact preview may rearm the provisional owner.
            self.minimum_revision = max(self.minimum_revision, document.revision + 1)
            self.clear()

    def _bind(self, document):
        if self.identity != document.identity:
            self.clear()
            self.identity = document.identity
            self.minimum_revision = -1

    def accepts(self, document):
        self._bind(document)
        return document.revision >= self.minimum_revision

    @staticmethod
    def _storage_bytes(batch, overlays):
        images = [tile.image for _phase, tiles in batch for tile in tiles]
        images.extend(tile.image for tile, _replace in overlays.values())
        if any(not isinstance(image, QImage) or image.isNull() for image in images):
            return None
        # Independent QImage handles retain COW storage. Repeated aliases are
        # charged once; the bounded owner never retains a widget or evaluator.
        return sum({image.cacheKey(): image.sizeInBytes() for image in images}.values())

    def remember(self, document, batch, tiles=(), *, replace_background=False, complete=False, view=None):
        if not self.accepts(document):
            return False
        if self.document is not None and (
                self.document.identity != document.identity
                or self.document.configuration != document.configuration
                or self.document.bounds != document.bounds or self.view != view):
            self.clear()
        overlays = {} if complete else dict(self.overlays)
        for tile in tiles:
            key = tuple(tile.world_rect.getRect())
            # A later complete scene patch owns the overlap with every earlier
            # patch, including differently aligned source and world grids.
            overlays.pop(key, None)
            overlays[key] = tile, replace_background
        retained = tuple((phase, tuple(values)) for phase, values in batch)
        size = self._storage_bytes(retained, overlays)
        if (size is None or size > self.budget or not retained
                or self.document is not None and self.document.revision > document.revision):
            return False
        self.document, self.batch, self.overlays, self.byte_count = document, retained, overlays, size
        self.view = view
        return True

    def fallback(self, document, *, revision, view=None):
        if not self.accepts(document) or self.document is None:
            return None
        if (self.document.identity != document.identity
                or self.document.configuration != document.configuration
                or self.document.bounds != document.bounds
                or self.document.pixel_contract != document.pixel_contract or self.view != view):
            self.clear()
            return None
        if self.document.revision < revision or self.document.revision > document.revision:
            return None
        return self.batch, tuple(self.overlays.values())
