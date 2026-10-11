"""Stable owner-thread capture for a cold separable raster contact.

The selected source may advance while its independent scene planes are being
prepared. Everything else retains ordinary typed invalidation. These snapshots
are provisional, have no durable cache, and must never supply exact scene tiles.
"""
from dataclasses import replace
import copy
import math
from types import SimpleNamespace

from PySide6.QtCore import QRectF
from PySide6.QtGui import QTransform

from comic_editor.core.models import RasterObject
from comic_editor.render.scene import SceneCapture, SceneSnapshotCompiler
from comic_editor.render.raster_feedback import (
    _feedback_source_keys, _feedback_transform_coefficients,
)


def visible_source_keys(canvas, document, identifier, visible, *, limit=128):
    """Cull source metadata to all native feedback patches in this view.

    This uses the same saved-model mapping and native patch alignment as
    preparation. It changes neither source sampling nor the object's global
    interaction/transform frame. A bounded source index is frozen immediately
    through the ordinary COW/file-pin capture path, before input can interleave.
    """
    obj = canvas.chapter.objects.get(identifier)
    if not isinstance(obj, RasterObject):
        return None
    mapping = (QTransform.fromTranslate(obj.x, obj.y)
        * canvas._drawing_object_transform(obj)
        * canvas.layer_world_transform(obj.parent_layer_id))
    inverse, valid = mapping.inverted()
    coefficients = _feedback_transform_coefficients(mapping)
    if not valid or not all(math.isfinite(value) for value in coefficients):
        return None
    chain = canvas.chapter.ancestor_layers(obj.parent_layer_id)
    origin = (obj.x + sum(layer.translate_x for layer in chain),
              obj.y + sum(layer.translate_y for layer in chain))
    mapped = (obj.transform_quad is not None
        or any(layer.transform_quad is not None for layer in chain)
        or any(value != round(value) for value in origin))
    rebuild = mapped and (obj.transform_quad is not None
        or any(layer.transform_quad is not None for layer in chain))
    origin = (0, 0) if mapped else origin
    side = canvas.tiles.tile_size
    patch_size = 4 * side if rebuild else side
    extent = QRectF(*visible).intersected(document.bounds)
    if extent.isEmpty():
        return (), coefficients
    left = origin[0] + math.floor((extent.left() - origin[0]) / patch_size) * patch_size - 2
    top = origin[1] + math.floor((extent.top() - origin[1]) / patch_size) * patch_size - 2
    right = origin[0] + math.ceil((extent.right() - origin[0]) / patch_size) * patch_size + 2
    bottom = origin[1] + math.ceil((extent.bottom() - origin[1]) / patch_size) * patch_size + 2
    # Empty native cells need no source ownership. Bound metadata probing as
    # well as retained originals, including sparse large native maps.
    candidates = _feedback_source_keys((left, top, right-left, bottom-top), inverse, side, limit=4096)
    if candidates is None:
        return None
    source = canvas.tiles._tiles.get(identifier)
    keys = []
    for key in candidates:
        if source is not None and key in source.entries:
            keys.append(key)
            if len(keys) > limit:
                return None
    return tuple(keys), coefficients


def selected_source_change(change, identifier):
    """The same positive typed guard used by retained simple feedback."""
    selected = ('object', identifier)
    return not (change.conservative or change.document_fields or change.orders
        or any(item.affects_artwork and (item.entity != selected or item.structural
            or item.fields - {'pixels', 'interaction_rect'}) for item in change.entities)
        or any(item.entity != selected or item.resource != 'raster'
            or item.address is None for item in change.resources))


class _CaptureOwner:
    def __init__(self, canvas, document, tiles):
        self.canvas = canvas
        self.tiles = tiles
        # SceneCapture still validates identity and its revision at each yield.
        # The private owner advances only after the outer typed dependency guard.
        self._document_projection = SimpleNamespace(revision=document.revision)
        self._persistent_render_cache = None
        self._graphics_worker = None
        self._predictive = None

    def __getattr__(self, name):
        return getattr(self.canvas, name)


class RasterFeedbackBootstrap:
    """Capture once, retaining every changed selected source address.

    Call ``changed`` for every typed change from construction through adoption.
    The captured source baseline is immutable; ``dirty`` must be transferred
    intact to presentation, including new and pruned tiles. Completion cannot
    clear those addresses as an ordinary exact revision would.
    """
    def __init__(self, canvas, document, identifier, visible):
        self.canvas = canvas
        self.identifier = identifier
        self.visible = tuple(visible)
        self.document = replace(document, live_preview=False, contact_only=False)
        self.dirty = set()
        self.retired = False
        self.snapshot = None
        self.capture = None
        self._selected_jobs = ()
        self.compiler = SceneSnapshotCompiler()
        self.seeded_records = False
        self.source_mapping = None
        self.selected_source_keys = None
        previous = getattr(canvas, '_scene_snapshot_compiler', None)
        seeded = (previous is not None and previous.identity == document.identity
            and not previous.conservative)
        if seeded:
            # Each immutable pin job is still accepted only when its captured
            # native generation and path match. Sharing a job does not share
            # the mutable owner index or bypass ordinary source validation.
            self.compiler.resource_pins = dict(previous.resource_pins)
        source = canvas.tiles._tiles.get(identifier)
        # Large global drawings can still have a small native footprint in the
        # edited view. Freeze that footprint, including aligned patch gutters,
        # rather than rejecting contact because unrelated source tiles exist.
        if source is not None and len(source) > 128:
            culled = visible_source_keys(canvas, self.document, identifier, visible)
            if culled is None:
                self.retired = True
                return
            self.selected_source_keys, self.source_mapping = culled

        # Freeze only the edited native map before incremental chapter capture.
        # This reads addresses and COW buffers and registers ordinary clean-file
        # revision pins; it never decodes original files or waits for pin IO.
        selected_store = copy.copy(canvas.tiles)
        selected_store._tiles = {identifier: canvas.tiles._tiles[identifier]} if identifier in canvas.tiles._tiles else {}
        capture = SceneCapture(self.compiler, canvas, self.document)
        iterator = capture._capture_tiles(selected_store, 'document', source_keys=(
            {identifier: self.selected_source_keys} if self.selected_source_keys is not None else None))
        while True:
            try:
                next(iterator)
            except StopIteration as finished:
                selected, self._selected_jobs, _addresses = finished.value
                break
        tiles = copy.copy(canvas.tiles)
        tiles._tiles = dict(canvas.tiles._tiles)
        tiles._tiles[identifier] = selected._object_tiles(identifier)
        tiles._alpha_bounds = dict(canvas.tiles._alpha_bounds)
        tiles._alpha_bounds[identifier] = selected._alpha_bounds.get(identifier, {})
        self.owner = _CaptureOwner(canvas, self.document, tiles)
        capture_document = replace(self.document,
            identity=(id(canvas.chapter), id(tiles), id(canvas.images)))
        # The ordinary compiler already owns immutable detached model records.
        # Borrow those records through new containers, retaining its typed dirty
        # set so pre-contact artwork changes are copied rather than discarded.
        # Source pin indices remain private even where unchanged jobs are reused.
        if seeded:
            self.compiler.identity = capture_document.identity
            self.compiler.records = dict(previous.records)
            self.compiler.stroke_records = dict(previous.stroke_records)
            self.compiler.stroke_generation = previous.stroke_generation
            self.compiler.dirty = set(previous.dirty)
            self.compiler.conservative = False
            self.seeded_records = True
        self.compiler.resource_identity = capture_document.identity
        self.capture = self.compiler.capture(self.owner, capture_document)

    def changed(self, change):
        if self.retired:
            return False
        if not selected_source_change(change, self.identifier):
            self.retire()
            return False
        self.dirty.update(item.address for item in change.resources)
        return True

    def valid(self, document, visible=None):
        canvas = self.canvas
        valid = (not self.retired and canvas.chapter is not None
            and document.identity == (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
            and replace(document, revision=self.document.revision,
                live_preview=False, contact_only=False) == self.document
            and canvas.selected_kind == 'object' and canvas.selected_id == self.identifier
            and isinstance(canvas.chapter.objects.get(self.identifier), RasterObject)
            and not canvas._projection_has_live_preview(include_ink=False)
            and (visible is None or tuple(visible) == self.visible))
        if valid and self.source_mapping is not None:
            # An implicit transform frame is model-owned too. A selected frame
            # change that alters its mapping cannot reuse the culled source.
            obj = canvas.chapter.objects[self.identifier]
            mapping = (QTransform.fromTranslate(obj.x, obj.y)
                * canvas._drawing_object_transform(obj)
                * canvas.layer_world_transform(obj.parent_layer_id))
            valid = _feedback_transform_coefficients(mapping) == self.source_mapping
        return valid

    def advance(self, document, visible=None, *, seconds=.002):
        """True means completed or retired; inspect ``snapshot`` before use."""
        if not self.valid(document, visible):
            self.retire()
            return True
        if self.snapshot is not None:
            return True
        if not self.capture.advance(seconds):
            return False
        if self.capture.stale or self.capture.result is None:
            self.retire()
            return True
        snapshot = self.capture.result
        self.snapshot = replace(snapshot, document=self.document, cache_spec=None, graphics_worker=None,
            pin_jobs=tuple(dict.fromkeys((*snapshot.pin_jobs, *self._selected_jobs))))
        self.capture = None
        return True

    def retire(self):
        self.retired = True
        self.capture = self.snapshot = None
        self._selected_jobs = ()
