"""Versioned scene inputs and a non-widget owner for the shared kernels.

Capture advances on the document thread in small slices. It copies model
records only when their generation changes, retains edited QImages through
copy-on-write, and pins clean files on the existing revision-reader IO lane.
The evaluator never reaches back into an editor or a mutable source store.
"""
from __future__ import annotations

from collections import OrderedDict
from collections.abc import MutableMapping
from contextlib import contextmanager
from dataclasses import dataclass, fields, is_dataclass
import copy
from pathlib import Path
import time
import weakref
from types import SimpleNamespace

from PySide6.QtCore import QPointF, QRect, QRectF, QSize
from PySide6.QtGui import QImage, QPainterPath, QPolygonF, QTransform

from comic_editor.core.changes import GROUP_KINDS
from comic_editor.core.images import ImageStore
from comic_editor.core.tile_backing import DiskTileMap, EditableTile, SnapshotBacking, prefetch_pins
from comic_editor.core.tiles import TileStore
from comic_editor.render.scene_kernels import SceneKernels
from comic_editor.render.image_storage_cache import QImageStorageCache
from comic_editor.render.image_residency_pool import QImageResidencyPool
from comic_editor.render.pixels import capture_color_environment
from comic_editor.render.service import CaptureState, RenderQuality
from comic_editor.render.scene_culling import SceneRenderBounds
from comic_editor.render.shape_outline import OutlineCache
from comic_editor.render.modifier_rendering import OutlineDistanceCache, BlurPyramidCache
from comic_editor.ui.tiling_rendering import RepeatMapCache


# Rendering/tool values only. No QWidget, timer, network manager, command stack,
# worker callback, or mutable live cache crosses the capture boundary.
SCENE_FIELDS = tuple("""
selected_kind selected_id selected_object_id selected_entities _solo_entities
_solo_suspended _active_top_plan _cage_session _drawing_selection_path
_geometry_transform_target _gradient_preview_active _multi_transform_preview_quads
_multi_transform_start_world_quads _live_underlay_amount _live_underlay_object_id
_predictive _promoted_vector_preview _render_base_alpha _render_cage_source
_render_exclude_text _render_excluded_object_id _render_modifier_sources
_rendering_compound_references _rendering_mask_contributor _rendering_outward_gradient
_rendering_halftone_source _selection_before_tiles _selection_overlay_tiles
_selection_transform_quad _selection_transform_start_quad _selection_vector_preview
_selection_vector_preview_revision _show_on_top_phase _suppress_outline_for_mask
_text_caret_visible _text_cursor_position _text_editing _text_selection_anchor
_tiling_capture_geometry _transform_preview_quad _transform_start_quad
_vector_eraser_preview _vector_eraser_preview_revision _vector_eraser_preview_versions
_vector_gesture_mode _vector_preview_id _vector_preview_tiles _vector_samples
_vector_simplify_overlay _vector_sweep _page_gap_draft _overlay_color_preview
_stroke_hide_border_id _selection_raster_states _selection_shape_nodes
_mask_wand_sample_entities _history_generation
""".split())


class SceneTileMap(DiskTileMap):
    def backing(self, key):
        self.residency.prepare(self, key)
        return super().backing(key)


class SceneTileStore(TileStore):
    def _object_tiles(self, identifier):
        if identifier not in self._tiles:
            self._tiles[identifier] = SceneTileMap(identifier, self.residency, self._tile_changed)
        return self._tiles[identifier]


def detached_value(value):
    """Copy Qt values and plain records without copying editor/native owners."""
    if isinstance(value, (QImage, QPainterPath, QPolygonF, QTransform, QRectF,
                          QRect, QSize, QPointF)):
        return type(value)(value)
    if isinstance(value, dict):
        return {detached_value(k): detached_value(v) for k, v in value.items()}
    if isinstance(value, list):
        return [detached_value(v) for v in value]
    if isinstance(value, tuple):
        return tuple(detached_value(v) for v in value)
    if isinstance(value, set):
        return {detached_value(v) for v in value}
    if is_dataclass(value) and not isinstance(value, type):
        result = copy.copy(value)
        for field in fields(value):
            object.__setattr__(result, field.name, detached_value(getattr(value, field.name)))
        return result
    from PySide6.QtCore import QObject
    if isinstance(value, QObject) or callable(value):
        raise TypeError("A scene snapshot cannot retain an editor or callback")
    return copy.deepcopy(value)


def detached_slices(value):
    """Copy nested model records without making one record a GUI-sized task.

    Qt values keep their normal copy-on-write handles. Model containers yield
    between their children, including individual vector points, so a large
    drawing cannot defeat the capture's time allowance.
    """
    if value is None or isinstance(value, (bool, int, float, str, bytes)):
        return value
    if isinstance(value, tuple) and all(item is None or isinstance(item,
            (bool, int, float, str, bytes)) for item in value):
        return value
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            result[(yield from detached_slices(key))] = yield from detached_slices(child)
            yield
        return result
    if isinstance(value, (list, tuple, set)):
        result = []
        for child in value:
            result.append((yield from detached_slices(child)))
            yield
        return type(value)(result)
    if is_dataclass(value) and not isinstance(value, type):
        result = copy.copy(value)
        for field in fields(value):
            object.__setattr__(result, field.name,
                (yield from detached_slices(getattr(value, field.name))))
            yield
        return result
    result = detached_value(value)
    yield
    return result


@dataclass(frozen=True)
class SceneSnapshot:
    document: object
    chapter: object
    tiles: TileStore
    images: ImageStore
    state: dict
    viewport: tuple[int, int]
    settings: object
    pin_jobs: tuple = ()
    graphics_worker: object = None
    pixel_environment: object = None
    cache_spec: tuple | None = None

    def finish_sources(self):
        """Called only by a detached consumer, never by interactive paint."""
        stores = (self.tiles, *(value for value in self.state.values() if isinstance(value, TileStore)))
        jobs = {}
        for job in self.pin_jobs:
            for address,(version,path) in job.records.items():
                identifier,key = address
                for store in stores:
                    owner = store._tiles.get(identifier)
                    if (owner is not None and (owner.versions.get(key,0),owner.content_keys.get(key,0)) == version
                            and owner.entries.get(key) == path):
                        # Reused batches also contain addresses captured by a
                        # newer job. Match each independently owned store's
                        # revision, including transient vector feedback.
                        jobs[(id(owner),key)] = job
        def prepare(owner, key):
            if key in owner.snapshot_pins:
                return
            job = jobs.get((id(owner), key))
            if job is not None:
                # Visible dependencies overtake unrelated pin IO. Registered
                # revision readers protect every remaining clean source until
                # its immutable pin is complete, without delaying first pixels.
                pin = job.pin(owner.object_id, key)
                owner.entries[key] = pin.path
                owner.snapshot_pins[key] = pin
        for store in stores:
            store.residency.prepare = prepare
        return self


class SceneSnapshotCompiler:
    def __init__(self):
        self.identity = None
        self.records = {}
        self.dirty = set()
        self.conservative = True
        self.copied_records = 0
        self.stroke_records = {}
        self.stroke_generation = 0
        self.resource_identity = None
        self.resource_pins = {}

    def invalidate(self, change=None):
        if change is None or change.conservative:
            self.conservative = True
        else:
            self.dirty.update(change.refs)

    def capture(self, owner, document):
        return SceneCapture(self, owner, document)


class SceneCapture:
    """Owner-thread incremental capture. A revision change discards the slice."""
    def __init__(self, compiler, owner, document):
        self.compiler, self.owner, self.document = compiler, owner, document
        self.result = None
        self.stale = False
        self.iterator = self._capture()

    def advance(self, seconds=.004):
        deadline = time.perf_counter() + max(0., seconds)
        # This GUI-thread slice does not process input events. Validate the
        # full positive contact owner once per entry, then retain the cheap
        # identity/revision checks at every incremental copy yield.
        if (self.document.contact_only and
                (not callable(getattr(self.owner, "_projection_contact_only", None))
                 or not self.owner._projection_contact_only())):
            self.stale = True
            return True
        while True:
            if (self.owner.chapter is None or
                    (id(self.owner.chapter), id(self.owner.tiles), id(self.owner.images)) != self.document.identity or
                    self.owner._document_projection.revision != self.document.revision):
                self.stale = True
                return True
            try:
                next(self.iterator)
            except StopIteration:
                return True
            if time.perf_counter() >= deadline:
                return False

    def _capture(self):
        compiler, owner = self.compiler, self.owner
        identity = self.document.identity
        if compiler.resource_identity != identity:
            compiler.resource_identity = identity
            compiler.resource_pins.clear()
        fresh = compiler.conservative or compiler.identity != identity
        chapter = copy.copy(owner.chapter)
        records = {}
        strokes = {}
        for field in fields(chapter):
            if field.name in GROUP_KINDS:
                group, kind = field.name, GROUP_KINDS[field.name]
                detached = {}
                for identifier, record in getattr(owner.chapter, group).items():
                    ref = kind, identifier
                    saved = compiler.records.get(ref)
                    if fresh or ref in compiler.dirty or saved is None:
                        from comic_editor.core.models import VectorDrawingObject
                        if isinstance(record, VectorDrawingObject):
                            saved = copy.copy(record)
                            for member in fields(record):
                                if member.name != "strokes":
                                    setattr(saved, member.name, (yield from
                                        detached_slices(getattr(record, member.name))))
                            saved.strokes = []
                            for stroke in record.strokes:
                                # Edits to existing points advance this native
                                # render revision; replacement/undo records have
                                # a different owner even when their ID is equal.
                                token = (stroke.render_revision, len(stroke.points),
                                    stroke.color, stroke.closed, stroke.start_cap,
                                    stroke.end_cap, tuple(stroke.clip_polygon or ()),
                                    stroke.tiling_group)
                                key = identifier, stroke.stroke_id
                                previous = compiler.stroke_records.get(key)
                                if (not fresh and previous is not None and
                                        previous[0]() is stroke and previous[1] == token):
                                    frozen = previous[2]
                                else:
                                    frozen = yield from detached_slices(stroke)
                                    compiler.stroke_generation += 1
                                    frozen._scene_generation = compiler.stroke_generation
                                strokes[key] = weakref.ref(stroke), token, frozen
                                saved.strokes.append(frozen)
                                yield
                        else:
                            saved = yield from detached_slices(record)
                        compiler.copied_records += 1
                    elif hasattr(record, "strokes"):
                        for stroke in record.strokes:
                            key = identifier, stroke.stroke_id
                            if key in compiler.stroke_records:
                                strokes[key] = compiler.stroke_records[key]
                    detached[identifier] = saved
                    records[ref] = saved
                    yield
                setattr(chapter, group, detached)
            else:
                setattr(chapter, field.name, (yield from detached_slices(getattr(owner.chapter, field.name))))
        state, jobs, active_resources = {}, [], set()
        for name in SCENE_FIELDS:
            if hasattr(owner, name):
                value = getattr(owner, name)
                if name == '_cage_session' and value is not None:
                    # Gesture history and pending commit callbacks belong to
                    # input. Only the deformation's render inputs cross over.
                    value = {key: value[key] for key in ('grid', 'targets', 'sources') if key in value}
                if isinstance(value, TileStore):
                    saved, pins, addresses = yield from self._capture_tiles(value, name)
                    state[name] = saved
                    jobs.extend(pins)
                    active_resources.update(addresses)
                else:
                    state[name] = yield from detached_slices(value)
                yield
        # A gesture's history baseline stays with the input owner. Rendering
        # needs only its active modifier identity, not the complete drag state.
        mesh = owner._mesh_warp_preview_modifier()
        state['_mesh_warp_preview_id'] = mesh.modifier_id if mesh is not None else None
        state['_smudge_preview_id'] = (mesh.modifier_id if mesh is not None and
            mesh.modifier_type == 'distort_smudge' else None)
        yield
        settings = SimpleNamespace(predictive_ink=bool(owner.settings.predictive_ink),
                                   canvas_renderer=owner.settings.canvas_renderer)
        images = owner.images.clone()
        # Retain already decoded original/working buffers through independent
        # QImage handles. This also supports decoded publication sources whose
        # encoded resource is being adopted by the source owner.
        for key, image in owner.images._decoded.items():
            alias = QImage(image)
            images._cache_decoded(key, alias)
            images._adopt_owned_decode(key, alias, owner.images._owned_decodes.get(key))
            yield
        tiles, pins, addresses = yield from self._capture_tiles(owner.tiles, 'document')
        jobs.extend(pins)
        active_resources.update(addresses)
        jobs = tuple(dict.fromkeys(jobs))
        captured = self.document.configuration[-1] if self.document.configuration else None
        if (isinstance(captured, tuple) and captured and captured[0] == 'pixel-environment'
                and isinstance(captured[2], tuple) and captured[2] and captured[2][0] == 'async-color'):
            from comic_editor.ui.color_resources import captured_canvas_environment, canvas_color_resources
            while True:
                if owner.chapter.pixel_contract.signature != chapter.pixel_contract.signature:
                    self.stale = True
                    return
                resources = canvas_color_resources(owner)
                resources.request(chapter.pixel_contract)
                if captured[2] != resources.ticket:
                    self.stale = True
                    return
                environment = captured_canvas_environment(owner, chapter.pixel_contract, captured[2])
                if environment is not None:
                    break
                yield
        else:
            environment = capture_color_environment(chapter.pixel_contract)
            if (isinstance(captured, tuple) and captured and captured[0] == 'pixel-environment'
                    and captured[2] != environment.signature):
                self.stale = True
                return
        compiler.identity, compiler.records = identity, records
        compiler.stroke_records = strokes
        compiler.resource_pins = {address: value for address, value in compiler.resource_pins.items()
            if address in active_resources}
        compiler.dirty.clear()
        compiler.conservative = False
        backing = getattr(owner, '_persistent_render_cache', None)
        if backing is not None and not backing.closed:
            from comic_editor.ui.cache_dependencies import RenderDependencies
            backing.contract = chapter.pixel_contract.signature
            backing.environment = (*RenderDependencies.environment(), environment.signature)
        self.result = SceneSnapshot(self.document, chapter, tiles, images, state,
            (owner.width(), owner.height()), settings, jobs,
            getattr(owner, "_graphics_worker", None),
            environment,
            (str(backing.root), tuple(backing.contract), tuple(backing.environment))
            if backing is not None and not backing.closed else None)

    def _capture_tiles(self, source_store, namespace):
        """Stage independent native handles and pins for artwork or live ink."""
        compiler = self.compiler
        tiles = SceneTileStore(source_store.tile_size, cache_budget=64 * 1024 * 1024)
        jobs, pending = [], []
        active_resources = set()
        def pin_batch():
            nonlocal backing
            backing = backing or SnapshotBacking()
            job = prefetch_pins(backing, tuple(pending))
            for identifier, key, version, path in pending:
                compiler.resource_pins[(namespace, identifier, key)] = version, path, job
            jobs.append(job)
            pending.clear()
        backing = None
        # This loop reads addresses and edited resident buffers, not file pixels.
        # Register pin batches immediately so resource publication respects them.
        for identifier, source in source_store._tiles.items():
            target = tiles._object_tiles(identifier)
            for key in source:
                source.version(key)
                value = source.entries[key]
                if isinstance(value, Path):
                    address = namespace, identifier, key
                    active_resources.add(address)
                    pin = source.snapshot_pin(key, ready_only=True)
                    if pin is not None:
                        target.entries[key] = pin.path
                        target.snapshot_pins[key] = pin
                    else:
                        target.entries[key] = value
                        version = source.version(key)
                        previous = compiler.resource_pins.get(address)
                        if previous is not None and previous[:2] == (version, value):
                            jobs.append(previous[2])
                        else:
                            pending.append((identifier, key, version, value))
                elif value.frozen is not None:
                    target.entries[key] = EditableTile(value.frozen)
                else:
                    resident = source.residency.entries.get((source, key))
                    if resident is None:
                        raise RuntimeError("Edited tile has no immutable or resident pixels")
                    target[key] = QImage(resident[0])
                target.versions[key] = source.versions.get(key, 0)
                if key in source.content_keys:
                    target.content_keys[key] = source.content_keys[key]
                if len(pending) >= 128:
                    pin_batch()
                yield
            tiles._alpha_bounds[identifier] = dict(source_store._alpha_bounds.get(identifier, {}))
        if pending:
            pin_batch()
        tiles._alpha_bounds_dirty = set(source_store._alpha_bounds_dirty)
        tiles.dirty = set(source_store.dirty)
        return tiles, jobs, active_resources


class _RetainedAliases(MutableMapping):
    """Compatible scope records; every exposed image/state is independently owned."""
    def __init__(self, images):
        self.images = images

    def __len__(self):
        return len(self.images)

    def __iter__(self):
        return iter(self.images)

    def __getitem__(self, scope):
        image = self.images.peek(scope)
        key, state = self.images.metadata(scope)
        return key, image, state, int(image.sizeInBytes())

    def __setitem__(self, scope, value):
        key, image, state, _size = value
        self.images.store(scope, image, metadata=(key, state))

    def __delitem__(self, scope):
        del self.images[scope]

    def move_to_end(self, scope, last=True):
        self.images.move_to_end(scope, last=last)

    def clear(self):
        self.images.clear()


class InlineResults:
    """Bounded retained checkpoints for kernels already running off the GUI."""
    def __init__(self, owner, budget=64 * 1024 * 1024, limit=512, *, pool=None):
        self.owner = owner
        self._pooled_images = (QImageStorageCache(budget, limit, pool=pool,
            reject_oversized=True, metadata_copy=detached_value, allow_null=True,
            metadata_identity=lambda value: value[0])
            if pool is not None else None)
        self.retained, self.pending, self.waiting = OrderedDict(), {}, {}
        if self._pooled_images is not None:
            self.retained = _RetainedAliases(self._pooled_images)
        self.budget, self.limit, self.bytes = budget, limit, 0
        self._retained_images = {}

    @property
    def budget(self):
        return self._pooled_images.budget if self._pooled_images is not None else self._budget

    @budget.setter
    def budget(self, value):
        self._budget = value
        if self._pooled_images is not None:
            self._pooled_images.budget = value

    @property
    def bytes(self):
        return self._pooled_images.bytes if self._pooled_images is not None else self._bytes

    @bytes.setter
    def bytes(self, value):
        self._bytes = value

    @property
    def limit(self):
        return self._pooled_images.limit if self._pooled_images is not None else self._limit

    @limit.setter
    def limit(self, value):
        if value < 1:
            raise ValueError('Invalid checkpoint record limit')
        self._limit = value
        if self._pooled_images is not None:
            self._pooled_images.limit = value
            while len(self.retained) > value:
                self.retained_remove(next(iter(self.retained)))

    def retained_get(self, scope, key):
        entry = self.retained.get(scope)
        if entry is None or entry[0] != key:
            from comic_editor.ui.cache_dependencies import cache_get
            image = cache_get(self.owner, "retained", ("retained", scope, key))
            if image is None:
                return None
            from comic_editor.render.cache import restore_state
            backing = self.owner._persistent_render_cache
            descriptor = backing.descriptor("retained", ("retained", scope, key))
            state = restore_state(backing.entries.get(descriptor.identity, {}).get("state"))
            self.retained_put(scope, key, image, state)
            return QImage(image), state
        self.retained.move_to_end(scope)
        if self._pooled_images is not None:
            self._pooled_images.reuse(scope)
        return QImage(entry[1]), detached_value(entry[2])

    def retained_put(self, scope, key, image, state=None, **_kwargs):
        from comic_editor.ui.cache_dependencies import cache_put
        cache_put(self.owner,'retained',('retained',scope,key),image,state=state)
        return self._retained_store(scope, key, image, state)

    def _retained_store(self, scope, key, image, state):
        if self._pooled_images is not None:
            return self._pooled_images.store(scope, image, metadata=(key, state))
        self.retained_remove(scope)
        image = QImage(image)
        size = int(image.sizeInBytes())
        storage = int(image.cacheKey())
        if size > self.budget:
            return False
        # Stage, tile-graph and pipeline scopes can share one completed image.
        # Bound its COW pixel storage once, and still bound every scope record.
        while self.retained and (
            self.bytes + (0 if storage in self._retained_images else size) > self.budget
            or len(self.retained) >= self.limit
        ):
            self.retained_remove(next(iter(self.retained)))
        self.retained[scope] = key, image, detached_value(state), size
        if storage in self._retained_images:
            self._retained_images[storage][1] += 1
        else:
            self._retained_images[storage] = [size, 1]
            self.bytes += size
        return True

    def retained_remove(self, scope, key=None):
        entry = self.retained.get(scope)
        if entry is not None and (key is None or entry[0] == key):
            if self._pooled_images is not None:
                del self.retained[scope]
                return
            self.retained.pop(scope)
            storage = int(entry[1].cacheKey())
            self._retained_images[storage][1] -= 1
            if not self._retained_images[storage][1]:
                self.bytes -= self._retained_images.pop(storage)[0]

    def adopt_retained(self, values):
        """Rebuild this owner's bounded ledger from actual retained QImages."""
        entries = OrderedDict(values)
        if self._pooled_images is not None:
            self.retained.clear()
            for scope, (key, image, state, _size) in entries.items():
                self._retained_store(scope, key, image, state)
            return
        self.retained = OrderedDict()
        self._retained_images = {}
        self.bytes = 0
        for scope, (key, image, state, _size) in entries.items():
            self._retained_store(scope, key, image, state)

    def result(self, scope, key):
        entry = self.retained_get(("result", scope), key)
        return entry[0] if entry is not None else None

    def request(self, *_args, **_kwargs):
        # The caller is already the admitted scene worker. Use the exact inline
        # kernel instead of opening a nested pool or publishing a draft.
        return False

    def has_running(self, *_args):
        return False


class EvaluatedScene(SceneKernels):
    def __init__(self, snapshot):
        self.snapshot = snapshot
        self.chapter, self.tiles, self.images = snapshot.chapter, snapshot.tiles, snapshot.images
        self.settings = snapshot.settings
        self.__dict__.update(snapshot.state)
        self._graphics_worker = snapshot.graphics_worker
        self.center_x = self.center_y = self.rotation = 0.
        self.scale = 1.
        self._image_residency_pool = QImageResidencyPool(segmented=True)
        self._effect_jobs = InlineResults(self, pool=self._image_residency_pool)
        self._render_bounds = SceneRenderBounds(self)
        for prefix in ("vector_render", "modifier_render", "modifier_source", "tone_mask_contributor"):
            image_cache = (QImageStorageCache(pool=self._image_residency_pool)
                           if prefix in ("modifier_render", "modifier_source")
                           else OrderedDict())
            setattr(self, f"_{prefix}_cache", image_cache)
            setattr(self, f"_{prefix}_cache_bytes", 0)
            setattr(self, f"_{prefix}_cache_budget", 64 * 1024 * 1024)
        for name in ("compound_path", "gradient_geometry", "gradient_scalar", "gradient_render",
                     "gradient_ramp"):
            setattr(self, f"_{name}_cache", OrderedDict())
        self._transform_modifier_preview_cache = None
        self._outline_cache = OutlineCache()
        self._outline_distance_cache = OutlineDistanceCache()
        self._blur_pyramid_cache = BlurPyramidCache()
        self._tiling_sampling_cache = RepeatMapCache()
        self._vector_spatial_indexes = {}
        self._compound_geometry_dirty = True
        self._compound_geometry_signature = None
        self._render_dependencies = None
        self._effect_preview_channel = "canvas"
        self._effect_provisional_revision = 0
        self._projection_exact = True
        self._projection_defer_effects = False
        self._exact_reference_render = False
        self._interactive_render = True
        self._rendering_mask_contributor = 0
        self._render_modifier_sources = set()
        self._blend_capture_objects = set()
        self._persistent_render_cache = None

    # These compatibility metrics are role-local and can overlap. Reading the
    # live map avoids stale values after another role evicts a shared node.
    @property
    def _modifier_render_cache_bytes(self):
        cache = getattr(self, '_modifier_render_cache', None)
        return cache.bytes if isinstance(cache, QImageStorageCache) else self.__dict__.get('_modifier_render_cache_bytes', 0)

    @_modifier_render_cache_bytes.setter
    def _modifier_render_cache_bytes(self, value):
        self.__dict__['_modifier_render_cache_bytes'] = value

    @property
    def _modifier_source_cache_bytes(self):
        cache = getattr(self, '_modifier_source_cache', None)
        return cache.bytes if isinstance(cache, QImageStorageCache) else self.__dict__.get('_modifier_source_cache_bytes', 0)

    @_modifier_source_cache_bytes.setter
    def _modifier_source_cache_bytes(self, value):
        self.__dict__['_modifier_source_cache_bytes'] = value

    @property
    def _modifier_render_cache_budget(self):
        cache = getattr(self, '_modifier_render_cache', None)
        return cache.budget if isinstance(cache, QImageStorageCache) else self.__dict__.get('_modifier_render_cache_budget', 64 * 1024 * 1024)

    @_modifier_render_cache_budget.setter
    def _modifier_render_cache_budget(self, value):
        self.__dict__['_modifier_render_cache_budget'] = value
        cache = getattr(self, '_modifier_render_cache', None)
        if isinstance(cache, QImageStorageCache):
            cache.budget = value

    @property
    def _modifier_source_cache_budget(self):
        cache = getattr(self, '_modifier_source_cache', None)
        return cache.budget if isinstance(cache, QImageStorageCache) else self.__dict__.get('_modifier_source_cache_budget', 64 * 1024 * 1024)

    @_modifier_source_cache_budget.setter
    def _modifier_source_cache_budget(self, value):
        self.__dict__['_modifier_source_cache_budget'] = value
        cache = getattr(self, '_modifier_source_cache', None)
        if isinstance(cache, QImageStorageCache):
            cache.budget = value

    @property
    def solo_entities(self):
        return self._solo_entities

    def width(self):
        return self.snapshot.viewport[0]

    def height(self):
        return self.snapshot.viewport[1]

    def rect(self):
        return QRect(0, 0, *self.snapshot.viewport)

    def hasFocus(self):
        return False

    def _projection_has_live_preview(self):
        return self.snapshot.document.live_preview

    @contextmanager
    def without_solo(self):
        previous = self._solo_suspended
        self._solo_suspended = True
        try:
            yield
        finally:
            self._solo_suspended = previous

    def cache_state(self):
        """Retain bounded semantic results, without retaining a scene owner."""
        values = {}
        values['image_storage_order'] = self._image_residency_pool.storage_order()
        # Prior read history uses owned handles, never persistent storage IDs
        # or authoritative byte summaries. Export itself is not a new hit.
        values['image_reuse_policy'] = ('probation-protected', 1, 'owned-history')
        values['image_reuse_state'] = dict(
            nodes=self._image_residency_pool.protected_storage(),
            aliases={name: mapping.protected_aliases() for name, mapping in (
                ('source', self._modifier_source_cache), ('effect', self._modifier_render_cache),
                ('retained', self._effect_jobs._pooled_images))})
        for prefix in ("vector_render", "modifier_render", "modifier_source", "tone_mask_contributor"):
            name = f"_{prefix}_cache"
            cache = getattr(self, name)
            values[name] = cache.export() if isinstance(cache, QImageStorageCache) else OrderedDict(cache)
            values[f"{name}_bytes"] = (cache.bytes if isinstance(cache, QImageStorageCache)
                                       else getattr(self, f"{name}_bytes"))
        for prefix in ("gradient_geometry", "gradient_scalar", "gradient_render", "gradient_ramp"):
            name = f"_{prefix}_cache"
            values[name] = OrderedDict(getattr(self, name))
        # Checkpoints use the same semantic key checks as their live owner;
        # the replacement owner receives its own LRU bookkeeping.
        values["retained"] = OrderedDict(
            (scope, (key, QImage(image), detached_value(state), size))
            for scope, (key, image, state, size) in self._effect_jobs.retained.items()
        )
        values["retained_bytes"] = self._effect_jobs.bytes
        return values

    def adopt_cache_state(self, values):
        image_names = ("_modifier_render_cache", "_modifier_source_cache")
        pooled = getattr(self, '_image_residency_pool', None) is not None
        if pooled:
            self._adopt_image_residency(values)
        for name in image_names:
            if name in values and not pooled:
                cache = QImageStorageCache(budget=getattr(self, f"{name}_budget"))
                cache.update(values[name])
                setattr(self, name, cache)
                setattr(self, f"{name}_bytes", cache.bytes)
        for name, value in values.items():
            if (name in image_names or name in ('image_storage_order', 'image_reuse_policy', 'image_reuse_state')
                    or name in tuple(f"{cache_name}_bytes" for cache_name in image_names)):
                continue
            if name == "retained":
                if not pooled:
                    self._effect_jobs.adopt_retained(value)
            elif name == "retained_bytes":
                # Older snapshots charged every alias. The adopter derives
                # storage and its byte charge from the current image handles.
                continue
            else:
                setattr(self, name, OrderedDict(value) if isinstance(value, dict) else value)

    def _adopt_image_residency(self, values):
        """Rebuild one bounded union from handles, preserving both LRU orders.

        The transfer contains no pool/owner and no authoritative byte summary.
        Old snapshots without storage order use their existing role order.
        Prior protection restores only for admitted matching handles/aliases;
        older snapshots without history start in probation. No rebuild is a hit.
        """
        policy = values.get('image_reuse_policy')
        if policy is not None and (not isinstance(policy, tuple) or
                policy != ('probation-protected', 1, 'owned-history')):
            raise ValueError('Unsupported image reuse transfer policy')
        reuse = values.get('image_reuse_state')
        if policy is not None and reuse is None:
            raise ValueError('Missing image reuse transfer history')
        if reuse is not None:
            if (policy is None or not isinstance(reuse, dict) or set(reuse) != {'nodes', 'aliases'}
                    or not isinstance(reuse['nodes'], tuple) or len(reuse['nodes']) > 1536
                    or not all(isinstance(image, QImage) for image in reuse['nodes'])
                    or not isinstance(reuse['aliases'], dict) or set(reuse['aliases']) != {'source', 'effect', 'retained'}):
                raise ValueError('Invalid image reuse transfer history')
            if len({int(image.cacheKey()) for image in reuse['nodes']}) != len(reuse['nodes']):
                raise ValueError('Duplicate image reuse storage history')
            normalized = {}
            for aliases in reuse['aliases'].values():
                if (not isinstance(aliases, tuple) or len(aliases) > 512 or
                        not all(isinstance(alias, tuple) and len(alias) == 3 and isinstance(alias[1], QImage)
                                for alias in aliases)):
                    raise ValueError('Invalid image reuse alias history')
            for name, aliases in reuse['aliases'].items():
                try:
                    if len({key for key, _image, _identity in aliases}) != len(aliases):
                        raise ValueError('Duplicate image reuse alias history')
                    normalized[name] = tuple((key, QImage(image), detached_value(identity))
                        for key, image, identity in aliases)
                except TypeError as error:
                    raise ValueError('Invalid image reuse alias identity') from error
            reuse = dict(nodes=tuple(QImage(image) for image in reuse['nodes']), aliases=normalized)
        pool = self._image_residency_pool
        maps = (self._modifier_source_cache, self._modifier_render_cache,
                self._effect_jobs._pooled_images)
        histories = []
        for name, mapping in zip(('_modifier_source_cache', '_modifier_render_cache'), maps):
            entries = values[name] if name in values else mapping.export()
            histories.append([(key, image, None) for key, image in entries.items()
                              if int(image.sizeInBytes()) > 0][-mapping.limit:])
        retained = values.get('retained', OrderedDict(self._effect_jobs.retained))
        histories.append([(scope, image, (key, state))
            for scope, (key, image, state, _size) in retained.items()
            if 0 <= int(image.sizeInBytes()) <= self._effect_jobs.budget][-maps[2].limit:])
        if reuse is not None:
            # Validate identity comparisons before releasing any current
            # ownership. Detached history is optional prior policy, never an
            # authority to replace a semantic key or admit a missing image.
            for name, history in zip(('source', 'effect', 'retained'), histories):
                identities = {key: metadata[0] if name == 'retained' else None
                              for key, _image, metadata in history}
                for key, _image, identity in reuse['aliases'][name]:
                    if key in identities:
                        try:
                            bool(identities[key] == identity)
                        except (TypeError, ValueError) as error:
                            raise ValueError('Invalid image reuse semantic comparison') from error
        # Group all role aliases before admission so a late-loaded role cannot
        # protect cold storage ahead of a more recently reused node.
        groups = OrderedDict()
        for image in values.get('image_storage_order', ()):
            groups.setdefault(int(image.cacheKey()), [])
        for mapping, history in zip(maps, histories):
            for key, image, metadata in history:
                groups.setdefault(int(image.cacheKey()), []).append((mapping, key, image, metadata))
        pool.clear()
        for aliases in groups.values():
            for mapping, key, image, metadata in aliases:
                mapping.store(key, image, metadata=metadata)
        for mapping, history in zip(maps, histories):
            mapping.reorder(key for key, _image, _metadata in history)
        if reuse is not None:
            pool.restore_protected(reuse['nodes'])
            for name, mapping in zip(('source', 'effect', 'retained'), maps):
                mapping.restore_aliases(reuse['aliases'][name])


class DetachedSceneBackend:
    def __init__(self, snapshot):
        self.snapshot = snapshot.finish_sources()
        self.pixel_environment = snapshot.pixel_environment
        self.scene = EvaluatedScene(snapshot)
        if snapshot.cache_spec is not None:
            from comic_editor.render.cache import PersistentRenderCache
            from comic_editor.ui.cache_dependencies import RenderDependencies
            root, contract, environment = snapshot.cache_spec
            self.scene._persistent_render_cache = PersistentRenderCache(root,
                contract=contract, environment=environment)
            self.scene._render_dependencies = RenderDependencies(self.scene, self.scene._persistent_render_cache)
            self.scene.tiles.render_fingerprint = self.scene._render_dependencies.tiles
            self.scene.images.render_fingerprint = self.scene._render_dependencies.image

    def lookup_tile(self, request, phase):
        scene = self.scene
        backing = scene._persistent_render_cache
        document = self.snapshot.document
        if (backing is None or document.live_preview or document.underlay != ("", 0.)
                or scene.solo_entities or document.configuration[9] is not None):
            return None
        scene._render_bounds.prepare()
        from comic_editor.render.pixels import pixel_scope
        with pixel_scope(document.pixel_contract, environment=self.pixel_environment):
            key = scene._render_dependencies.projection_key(request, (*document.configuration, phase))
            return backing.lookup("projection", key, wait=True)

    def close(self):
        backing = self.scene._persistent_render_cache
        # A synchronous detached consumer may create its own application-thread
        # helper. EvaluatedScene has no QObject lifetime signal; retire only its
        # local helper here, never the snapshot's borrowed graphics worker.
        pattern = self.scene.__dict__.pop("_gpu_pattern_renderer", None)
        texture = self.scene.__dict__.pop("_gpu_texture_renderer", None)
        try:
            try:
                if pattern is not None:
                    pattern.close()
            finally:
                if texture is not None:
                    texture.close()
        finally:
            try:
                if backing is not None:
                    backing.close()
            finally:
                self.scene._image_residency_pool.clear()

    def _record_allowed(self):
        document = self.snapshot.document
        return (self.scene._persistent_render_cache is not None and not document.live_preview
            and document.underlay == ('',0.) and not self.scene.solo_entities
            and document.configuration[9] is None and self.scene._effect_preview_channel == 'canvas')

    @contextmanager
    def record_tiles(self,enabled=True):
        """Explicit recording only; ordinary snapshots remain cache readers."""
        if not enabled:
            yield False
            return
        if not self._record_allowed():
            raise ValueError('This scene has live or isolated artwork and cannot record exact tiles')
        from comic_editor.render.pixels import pixel_scope
        from comic_editor.ui.point_lut import graphics_scope
        with pixel_scope(self.snapshot.document.pixel_contract,environment=self.pixel_environment), \
                graphics_scope(self.scene._graphics_worker),self.scene._persistent_render_cache.record():
            yield True

    def retain_tile(self,request,phase,image):
        backing = self.scene._persistent_render_cache
        if not self._record_allowed() or not backing.recording or image.isNull():
            return False
        from comic_editor.render.device import cpu_image
        from comic_editor.ui.cache_dependencies import cache_put
        document = self.snapshot.document
        key = self.scene._render_dependencies.projection_key(request,(*document.configuration,phase))
        image = cpu_image(image)
        cache_put(self.scene,'projection',key,image)
        identity = backing.descriptor('projection',key).identity
        if identity not in backing.entries and identity not in backing.writes and identity not in backing.staged:
            # The recording owner can drain bounded IO; the GUI never waits
            # for a full write queue or silently declares an omitted tile green.
            backing.drain()
            if not backing.retain('projection',key,image):
                raise RuntimeError(backing.error or 'Exact tile was not admitted to disk recording')
        return True

    def finish_recording(self):
        backing = self.scene._persistent_render_cache
        if backing is not None:
            backing.drain()
            if backing.error:
                raise RuntimeError(backing.error)

    def record_manifest(self):
        """Reload the atomically committed index on its detached owner."""
        backing = self.scene._persistent_render_cache
        if backing is None:
            return None
        from comic_editor.render.cache import PersistentRenderCache
        committed = PersistentRenderCache(backing.root,contract=backing.contract,environment=backing.environment)
        try:
            return committed.entries,committed.source_digests
        finally:
            committed.close()

    def matches(self, document):
        return document == self.snapshot.document

    def render_device(self, document, request):
        from comic_editor.render.device_scene import try_device_region
        return try_device_region(self.scene, document, request)

    @contextmanager
    def capture(self, document, request, effect_region):
        scene = self.scene
        scene._effect_viewport_world = QRectF(effect_region)
        scene._vector_render_scale_override = getattr(self, 'artwork_scale', request.scale)
        navigator = scene._effect_preview_channel == "navigator"
        scene._effect_region_requests = not navigator
        scene._projection_tile_key = None if navigator else (request.phase, request.key)
        scene._projection_exact = request.quality is RenderQuality.EXACT or getattr(self, "native_preview", False)
        scene._projection_defer_effects = False
        scene._stroke_projection_active = request.key == ("stroke-preview",)
        scene._live_underlay_object_id, scene._live_underlay_amount = document.underlay
        scene._render_bounds.prepare()
        state = CaptureState()
        previous = scene._effect_provisional_revision
        from comic_editor.ui.point_lut import graphics_scope
        previous_policy = getattr(scene, '_live_canvas_preview_policy', None)
        policy = getattr(self, 'live_canvas_preview_policy', None)
        scene._live_canvas_preview_policy = (policy if policy is not None
            and policy.matches(self.snapshot, document, request) else None)
        try:
            with graphics_scope(scene._graphics_worker):
                yield state
        finally:
            scene._live_canvas_preview_policy = previous_policy
        state.provisional = (document.live_preview or scene._effect_provisional_revision != previous)

    def paint(self, painter, visible, *, phase=None, page_contents_only=False):
        self.scene._render_scene_layers(painter, visible, underlay=True,
            only_phase=phase, page_contents_only=page_contents_only,
            live_ink=self.snapshot.document.live_preview)

    def page_area(self):
        area = QPainterPath()
        for identifier in self.scene.chapter.root_page_ids:
            page = self.scene.chapter.layers[identifier]
            if page.visible:
                area = area.united(self.scene.layer_world_transform(identifier).map(
                    self.scene.layer_effective_path(identifier)))
        return area

    def paint_entity(self, painter, visible, target):
        kind, identifier = target
        scene = self.scene
        if kind == 'layer':
            scene._render_layer(painter, scene.chapter.layers[identifier], 1., visible)
            return
        obj = scene.chapter.objects[identifier]
        parent = scene.layer_world_transform(obj.parent_layer_id)
        inverse, valid = parent.inverted()
        painter.save()
        try:
            painter.setTransform(parent, True)
            scene._blend_capture_objects.add(identifier)
            scene._render_object(painter, obj, 1., inverse.mapRect(visible) if valid else visible)
        finally:
            scene._blend_capture_objects.discard(identifier)
            painter.restore()

    @contextmanager
    def overflow_channel(self):
        scene = self.scene
        previous = scene._effect_preview_channel, scene._live_underlay_object_id, scene._live_underlay_amount
        scene._effect_preview_channel, scene._live_underlay_object_id, scene._live_underlay_amount = "overflow", "", 0.
        try:
            yield
        finally:
            scene._effect_preview_channel, scene._live_underlay_object_id, scene._live_underlay_amount = previous
