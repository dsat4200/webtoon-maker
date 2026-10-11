"""UNAPPLIED bounded R0 ordinary contact capture; not a new renderer.

The selected model, native pixels and live render fields freeze together.
Unchanged global metadata then crosses the existing SceneCapture in slices.
Every typed publication and every outer entry must retain positive ownership.
"""
import copy
from dataclasses import dataclass, fields, is_dataclass, replace
from types import SimpleNamespace
import weakref

from PySide6.QtCore import QPointF, QRect, QRectF, QSize
from PySide6.QtGui import QImage, QPainterPath, QPolygonF, QTransform

from comic_editor.core.models import RasterObject
from comic_editor.core.tiles import TileStore
from comic_editor.render.admission import RENDER_ADMISSION
from comic_editor.render.scene import SCENE_FIELDS, SceneCapture, SceneSnapshot, SceneSnapshotCompiler, detached_value


class CaptureUnavailable(Exception):
    pass


class CaptureAdmissionUnavailable(CaptureUnavailable):
    """A bounded native copy can retry after existing work releases admission."""
    pass


def require(condition, reason):
    if not condition:
        raise CaptureUnavailable(reason)


def bounded_value(value, *, limit=4096):
    """Reject unbounded live UI state before recursive detachment."""
    pending, count = [value], 0
    while pending:
        item = pending.pop()
        count += 1
        require(count <= limit, 'live render field metadata cap')
        if item is None or type(item) in (bool, int, float, str, bytes):
            continue
        if isinstance(item, (QPointF, QRect, QRectF, QSize, QTransform)):
            continue
        if isinstance(item, QPainterPath):
            require(item.elementCount() <= limit - count, 'live path cap')
            count += item.elementCount()
        elif isinstance(item, QPolygonF):
            require(len(item) <= limit - count, 'live polygon cap')
            count += len(item)
        elif isinstance(item, QImage):
            raise CaptureUnavailable('unexpected live field pixels')
        elif isinstance(item, dict):
            require(len(item) <= limit - count, 'live dict cap')
            pending.extend(item.keys())
            pending.extend(item.values())
        elif isinstance(item, (tuple, list, set, frozenset)):
            require(len(item) <= limit - count, 'live sequence cap')
            pending.extend(item)
        elif is_dataclass(item) and not isinstance(item, type):
            pending.extend(getattr(item, field.name) for field in fields(item))
        else:
            # QColor and other ordinary Qt value copies are accepted only if
            # detached_value proves they can cross without an editor/callback.
            detached_value(item)
    return count


class _LeaseOwner:
    """One lease is shared by capture and actual immutable worker snapshot.

    Its final release follows the last snapshot/backend owner, not a paint or
    merely dropping the controller's capture reference. Explicit failure-path
    retirement removes local owners; it does not free another worker's lease.
    """
    def __init__(self, lease):
        self.lease = lease

    def __del__(self):
        self.lease.release()


@dataclass(frozen=True)
class ContactSceneSnapshot(SceneSnapshot):
    """Artifact-only provenance; ordinary consumers still use SceneSnapshot.

    A production integration could instead add these two default-None fields to
    SceneSnapshot. This prototype needs no production schema or renderer change.
    """
    contact_preview_owner: tuple | None = None
    capture_lease: object = None


class _PrivateOwner:
    def __init__(self, canvas, chapter, tiles, images, state, document):
        self.canvas, self.chapter, self.tiles, self.images = canvas, chapter, tiles, images
        self.__dict__.update(state)
        self._document_projection = SimpleNamespace(revision=document.revision)
        self.settings = copy.copy(canvas.settings)
        self._persistent_render_cache = None
        self._graphics_worker = getattr(canvas, '_graphics_worker', None)

    def __getattr__(self, name):
        if name in SCENE_FIELDS:
            raise AttributeError(name)  # A render field absent at R0 stays absent.
        return getattr(self.canvas, name)


class OrdinaryContactCapture:
    """One full R0 snapshot, never relabeled as latest authored pixels.

    Construction/advance are document-thread operations. The controller must
    call valid with its current actual positive preview owner before dispatch.
    Existing SceneCapture.advance identity/revision checks are not modified.
    """
    def __init__(self, canvas, document, owner_token, *, max_keys=128,
                 max_images=512, max_records=8192, byte_budget=128 * 1024 * 1024):
        self.canvas, self.document, self.owner_token = canvas, document, owner_token
        self.result = None
        self.stale = False
        self.capture = self.private_owner = self._lease = self._notify = None
        self._revision = document.revision
        self._published = 0
        self.compiler = SceneSnapshotCompiler()
        try:
            controller = canvas._scene_controller
            require(owner_token is not None and owner_token == controller._contact_preview_owner,
                    'actual ordinary contact owner required')
            require(document.live_preview and document.contact_only, 'native P/E contact only')
            desired = controller.desired
            # Integration constructs after desired is set, with the current
            # complete native address/phases/view signature and owner token.
            require(desired is not None and desired[0] == document, 'actual desired R0 required')
            require(controller._contact_preview_key_for(document, *desired[1:]) == owner_token[1],
                    'actual current positive owner key required')
            self.gate = controller._owned_native_contact_gate(document)
            require(self.gate is not None and not self.gate.busy, 'native gate must be settled')
            self.identifier = canvas.selected_id
            original = canvas.chapter.objects.get(self.identifier)
            require(isinstance(original, RasterObject), 'selected native raster required')
            bounded_value(original)
            source = canvas.tiles._tiles.get(self.identifier)
            require(source is not None and len(source) <= max_keys, 'complete native selected map cap')
            require(len(canvas.images._sources) <= max_images, 'full immutable image-source metadata cap')
            require(sum(len(getattr(canvas.chapter, name)) for name in
                        ('layers', 'objects', 'modifiers', 'masks')) <= max_records,
                    'complete global model metadata cap')
            state = {}
            state_stores = {}
            require(len(canvas.images._decoded) <= 3 * max_images, 'decoded alias metadata cap')
            # Freeze iteration and matching source-ownership witnesses before
            # any Qt image calls; cache-only reorder/eviction cannot invalidate
            # this walk or substitute a different decode epoch mid-alias.
            decoded_rows = tuple(canvas.images._decoded.items())
            decode_witnesses = dict(canvas.images._owned_decodes)
            retained = sum(int(image.sizeInBytes()) for _key, image in decoded_rows)
            require(retained <= byte_budget, 'decoded alias retention cap')
            for name in SCENE_FIELDS:
                if not hasattr(canvas, name):
                    continue
                value = getattr(canvas, name)
                if isinstance(value, TileStore):
                    require(sum(len(owner) for owner in value._tiles.values()) <= max_keys,
                            'complete auxiliary native map cap')
                    state_stores[name] = value
                else:
                    bounded_value(value)
                    state[name] = detached_value(value)
            # Count actual retained edited borrowers, not clean file pixels.
            stores = [canvas.tiles, *state_stores.values()]
            for store in stores:
                store_bytes = 0
                maps = [source] if store is canvas.tiles else list(store._tiles.values())
                for native in maps:
                    entries = tuple(native.entries.items())
                    resident_rows = tuple((key, value,
                        native.residency.entries.get((native, key))) for key, value in entries)
                    for key, value, resident in resident_rows:
                        if resident is not None:
                            store_bytes += int(resident[0].sizeInBytes())
                        elif getattr(value, 'frozen', None) is not None:
                            # Account its worst-case native RAM lifetime without
                            # touching/decompressing its ordinary raw pin.
                            store_bytes += value.frozen.stride * value.frozen.height
                # The ordinary target SceneTileStore has a 64MiB residency.
                # A bounded synchronous selected/state freeze must not provoke
                # its edited-buffer spill path while retaining native aliases.
                require(store_bytes <= 64 * 1024 * 1024, 'ordinary capture residency cap')
                retained += store_bytes
            require(retained <= byte_budget, 'native contact capture retention cap')
            lease = RENDER_ADMISSION.reserve_copies(retained)
            if lease is None:
                raise CaptureAdmissionUnavailable('nonblocking native capture admission declined')
            self._lease = _LeaseOwner(lease)
            # Freeze all selected model/native/ink fields without pumping input.
            chapter = copy.copy(canvas.chapter)
            chapter.objects = dict(canvas.chapter.objects)
            chapter.objects[self.identifier] = detached_value(original)
            previous = canvas._scene_snapshot_compiler
            seeded = previous.identity == document.identity and not previous.conservative
            if seeded:
                self.compiler.resource_pins = dict(previous.resource_pins)
            selected_store = copy.copy(canvas.tiles)
            selected_store._tiles = {self.identifier: source}
            frozen, jobs, _addresses = self._freeze_store(selected_store, 'document')
            tiles = copy.copy(canvas.tiles)
            tiles._tiles = dict(canvas.tiles._tiles)
            tiles._tiles[self.identifier] = frozen._object_tiles(self.identifier)
            # Each native map already retains its real source residency. The
            # unchanged SceneCapture reads source.residency (not the container's
            # residency), including this independently frozen selected map.
            tiles._alpha_bounds = dict(canvas.tiles._alpha_bounds)
            tiles._alpha_bounds[self.identifier] = dict(frozen._alpha_bounds.get(self.identifier, {}))
            tiles._alpha_bounds_dirty = set(canvas.tiles._alpha_bounds_dirty)
            tiles.dirty = set(canvas.tiles.dirty)
            pin_jobs = list(jobs)
            for name, value in state_stores.items():
                state[name], jobs, _addresses = self._freeze_store(value, name)
                pin_jobs.extend(jobs)
            self._selected_jobs = tuple(dict.fromkeys(pin_jobs))
            images = canvas.images.clone()
            for key, image in decoded_rows:
                alias = QImage(image)
                images._cache_decoded(key, alias)
                images._adopt_owned_decode(key, alias, decode_witnesses.get(key))
            self.private_owner = _PrivateOwner(canvas, chapter, tiles, images, state, document)
            private_document = replace(document, identity=(id(chapter), id(tiles), id(images)))
            if seeded:
                self.compiler.identity = private_document.identity
                self.compiler.records = dict(previous.records)
                self.compiler.stroke_records = dict(previous.stroke_records)
                self.compiler.stroke_generation = previous.stroke_generation
                self.compiler.dirty = set(previous.dirty)
                self.compiler.dirty.add(('object', self.identifier))
                self.compiler.conservative = False
            self.compiler.resource_identity = private_document.identity
            self.capture = self.compiler.capture(self.private_owner, private_document)
            require(canvas._render_document_state() == document, 'R0 changed during bounded freeze')
            # Normal typed publication is complete when this signal fires. The
            # weak receiver never keeps a superseded capture alive during GC-off
            # production input. There is no timer/slot-order change.
            reference = weakref.ref(self)
            def notify(change, reference=reference):
                owner = reference()
                if owner is not None:
                    owner._changed(change)
            self._notify = notify
            canvas.changesPublished.connect(notify)
        except BaseException:
            self.retire()
            raise

    def _freeze_store(self, source, namespace):
        # Ordinary COW/file-pin metadata path, complete maps, no image decoder
        # or file wait. It is bounded before construction by map key caps.
        capture = SceneCapture(self.compiler, self.canvas, self.document)
        iterator = capture._capture_tiles(source, namespace)
        try:
            while True:
                try:
                    next(iterator)
                except StopIteration as finished:
                    return finished.value
        finally:
            iterator.close()

    def _changed(self, change):
        selected = ('object', self.identifier)
        revision = self.canvas._document_projection.revision
        if (self.stale or not change.transient or change.conservative
                or change.document_fields or change.orders or not change.resources
                or any(item.entity != selected or item.structural
                       or item.fields - {'pixels', 'interaction_rect'} for item in change.entities)
                or any(item.entity != selected or item.resource != 'raster'
                       or item.address is None for item in change.resources)
                or revision != self._revision + 1):
            self.retire()
            return
        self._revision = revision
        self._published += 1

    def valid(self, document, owner_token, *, allow_busy=False, signature=None):
        controller = self.canvas._scene_controller
        desired = controller.desired if signature is None else signature
        return (not self.stale and owner_token == self.owner_token
            and document.live_preview and document.contact_only
            and replace(document, revision=self.document.revision) == self.document
            and document.revision == self._revision == self.canvas._document_projection.revision
            and desired is not None and desired[0] == document
            and controller._contact_preview_key_for(document, *desired[1:], allow_busy=allow_busy) == self.owner_token[1]
            and controller._owned_native_contact_gate(document) is self.gate)

    def advance(self, seconds=.004):
        current = self.canvas._render_document_state()
        if not self.valid(current, self.canvas._scene_controller._contact_preview_owner):
            self.retire()
            return True
        if self.result is not None:
            return True
        if not self.capture.advance(seconds):
            return False
        if self.capture.stale or self.capture.result is None:
            self.retire()
            return True
        # Prototype subclass is only explicit provenance, not another renderer.
        # Older pixels stay R0; all source/effect grids remain ordinary.
        values = {field.name: getattr(self.capture.result, field.name)
                  for field in fields(SceneSnapshot)}
        values.update(document=self.document, cache_spec=None,
            pin_jobs=tuple(dict.fromkeys((*self.capture.result.pin_jobs, *self._selected_jobs))),
            contact_preview_owner=self.owner_token, capture_lease=self._lease)
        self.result = ContactSceneSnapshot(**values)
        self.capture = None
        return True

    def retire(self):
        self.stale = True
        notify, self._notify = self._notify, None
        if notify is not None:
            try:
                self.canvas.changesPublished.disconnect(notify)
            except (RuntimeError, TypeError):
                pass  # Deleted signal or already disconnected; no work restarts.
        capture, self.capture = self.capture, None
        if capture is not None:
            capture.iterator.close()
        self.result = self.private_owner = None
        self._lease = None
        self._selected_jobs = ()

    def __del__(self):
        self.retire()
