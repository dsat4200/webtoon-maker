"""Bounded scene evaluation with latest-demand admission and guarded results.

One scene evaluator owns model/source caches. GPU kernels use their own context
owner. The GUI polls completed immutable blocks and never waits for this lane.
Cancellation is observed between native capture blocks and cooperatively
inside circular integration. Superseded work cannot publish into another revision.
"""
from collections import deque
from contextlib import contextmanager, nullcontext
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace
import copy
import math
from threading import Condition, Event

from comic_editor.render.service import DocumentRenderService, TileBatchPolicy, RenderRequest, RenderQuality
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.admission import CopyLease, RENDER_ADMISSION, WorkCancelled, snapshot_working_bytes
from comic_editor.render.ordinary_contact_capture import ContactSceneSnapshot
from comic_editor.render.geometry_capture import GeometrySceneSnapshot
from comic_editor.render.source_resources import ReadyOriginals
from comic_editor.render.projection import ProjectionRequest


@contextmanager
def _scene_cancellation(scene, cancelled):
    """Borrow the admitted worker token only for this native capture block."""
    values = vars(scene)
    absent = '_scene_cancelled' not in values
    previous = values.get('_scene_cancelled')
    values['_scene_cancelled'] = cancelled
    try:
        yield
    finally:
        if absent:
            values.pop('_scene_cancelled', None)
        else:
            values['_scene_cancelled'] = previous


@dataclass(frozen=True)
class SceneDemand:
    serial: int
    snapshot: object
    requests: tuple
    phases: tuple
    center: tuple
    visible: tuple | None = None
    preview_size: tuple | None = None
    consumer: str = "canvas"
    record: bool = False
    presentation_size: tuple | None = None
    feedback_target: str = ''
    feedback_requests: tuple | None = None
    feedback_only: bool = False
    feedback_originals: object = None
    contact_preview_owner: tuple | None = None
    interaction_preview_owner: tuple | None = None
    visibility_preview_owner: tuple | None = None


@dataclass(frozen=True)
class SceneCompletion:
    demand: SceneDemand
    phase: object = None
    tiles: object = None
    done: bool = False
    error: str = ""
    preview: object = None
    materialized: object = None
    transfer_keys: tuple = ()
    recorded: bool = False
    overview: object = None
    recorded_rows: tuple = ()
    cache_manifest: object = None
    feedback: object = None
    cache_keys: tuple = ()


def demand_working_bytes(demand):
    size = snapshot_working_bytes(demand.snapshot)
    if demand.feedback_target and not demand.snapshot.document.live_preview:
        from comic_editor.render.filtered_raster_feedback import FILTERED_PREPARED_BUDGET
        size += FILTERED_PREPARED_BUDGET
    if demand.presentation_size is not None and demand.visible is not None:
        width,height = demand.presentation_size
        tile_size = max((request.tile_size for request in demand.requests),default=256)
        chunk_width = min(width,math.ceil(tile_size*width/demand.visible[2])+1)
        chunk_height = min(height,math.ceil(tile_size*height/demand.visible[3])+1)
        # Display surface, global sample coordinates, and bounded float64
        # bilinear gathers coexist with the native block already reserved.
        size += width*height*4+(width+height)*8+chunk_width*chunk_height*256
    return size


class SceneScheduler:
    def __init__(self, *, handoff_budget=64 * 1024 * 1024, ready_callback=None):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scene-evaluate")
        self.condition = Condition()
        self.stopped = Event()
        self.cancelled = Event()
        self.future = None
        self.active_demand = None
        self.pending = None
        self.ready = deque()
        self.ready_bytes = 0
        self.handoff_budget = max(1, handoff_budget)
        self.submitted = self.completed = self.discarded = 0
        self._backend = None
        self._cache_state = None
        self._cache_identity = None
        self._geometry_draft_cache = self._geometry_draft_owner = None
        self._originals = ReadyOriginals()
        self._transfers = set()
        self.ready_callback = ready_callback

    @property
    def busy(self):
        return self.future is not None or self.pending is not None or bool(self._transfers)

    def _contact_preview_bounded(self, demand):
        if demand.contact_preview_owner is None:
            return False
        document = demand.snapshot.document
        size = demand.preview_size
        return (demand.contact_preview_owner is not None and document.live_preview
            and document.contact_only and demand.consumer == 'canvas' and not demand.record
            and not demand.feedback_only and demand.presentation_size is None
            and demand.phases in ((None,), ('base', 'top')) and demand.visible is not None
            and size is not None and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
            and size[0] * size[1] * (16 if document.pixel_contract.floating else 4)
                <= self.handoff_budget)

    def _coalesces_contact_preview(self, demand):
        active = self.active_demand
        if (active is None or self.future is None or self.cancelled.is_set() or self._transfers
                or not self._contact_preview_bounded(active) or not self._contact_preview_bounded(demand)):
            return False
        return (active.contact_preview_owner == demand.contact_preview_owner
            and active.serial <= demand.serial
            and active.snapshot.document.revision <= demand.snapshot.document.revision
            and replace(active.snapshot.document, revision=0) == replace(demand.snapshot.document, revision=0)
            and active.requests == demand.requests and active.phases == demand.phases
            and active.visible == demand.visible and active.preview_size == demand.preview_size
            and active.feedback_target == demand.feedback_target
            and getattr(active.snapshot.pixel_environment, 'signature', None)
                == getattr(demand.snapshot.pixel_environment, 'signature', None))

    def _geometry_preview_bounded(self, demand):
        snapshot = demand.snapshot
        size = demand.preview_size
        return (isinstance(snapshot, GeometrySceneSnapshot)
            and demand.interaction_preview_owner is not None
            and demand.interaction_preview_owner == snapshot.interaction_preview_owner
            and snapshot.document.live_preview and not snapshot.document.contact_only
            and snapshot.cache_spec is None and demand.consumer == 'canvas' and not demand.record
            and demand.contact_preview_owner is None and not demand.feedback_only
            and not demand.feedback_target and demand.feedback_requests is None
            and demand.feedback_originals is None and demand.presentation_size is None
            and demand.phases in ((None,), ('base', 'top')) and demand.visible is not None
            and size is not None and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
            and size[0] * size[1] * (16 if snapshot.document.pixel_contract.floating else 4)
                <= self.handoff_budget
            and isinstance(getattr(snapshot.capture_lease, 'lease', None), CopyLease)
            and snapshot.capture_lease.lease.admission is RENDER_ADMISSION
            and snapshot.capture_lease.lease.scope is snapshot.interaction_preview_owner
            and not snapshot.capture_lease.lease.released)

    def _coalesces_geometry_preview(self, demand):
        active = self.active_demand
        if (active is None or self.future is None or self.cancelled.is_set() or self._transfers
                or not self._geometry_preview_bounded(active) or not self._geometry_preview_bounded(demand)):
            return False
        return (active.interaction_preview_owner == demand.interaction_preview_owner
            and active.serial <= demand.serial
            and active.snapshot.document.revision <= demand.snapshot.document.revision
            and replace(active.snapshot.document, revision=0) == replace(demand.snapshot.document, revision=0)
            and active.requests == demand.requests and active.phases == demand.phases
            and active.visible == demand.visible and active.preview_size == demand.preview_size
            and getattr(active.snapshot.pixel_environment, 'signature', None)
                == getattr(demand.snapshot.pixel_environment, 'signature', None))

    def _visibility_preview_bounded(self, demand):
        from comic_editor.render.scene import SceneSnapshot
        snapshot, size = demand.snapshot, demand.preview_size
        return (type(snapshot) is SceneSnapshot and demand.visibility_preview_owner is not None
            and not snapshot.document.live_preview and not snapshot.document.contact_only
            and not snapshot.document.pixel_contract.floating and not snapshot.document.overflow
            and snapshot.document.underlay == ('', 0.) and snapshot.cache_spec is None
            and demand.consumer == 'canvas' and not demand.record
            and demand.contact_preview_owner is None and demand.interaction_preview_owner is None
            and not demand.feedback_only and not demand.feedback_target
            and demand.feedback_requests is None and demand.feedback_originals is None
            and demand.presentation_size is None and demand.visible is not None
            and demand.phases in ((None,), ('base', 'top'))
            and size is not None and len(size) == 2
            and all(type(value) is int and value > 0 for value in size)
            and size[0]*size[1]*4 <= self.handoff_budget)

    def submit(self, demand, *, coalesce_active=False):
        # Only complete immutable native-contact snapshots take this exception.
        # Existing exact/export/materialization/other consumers still cancel.
        if demand.contact_preview_owner is not None and not self._contact_preview_bounded(demand):
            demand = replace(demand, contact_preview_owner=None)
        if not coalesce_active or not (self._coalesces_contact_preview(demand)
                                      or self._coalesces_geometry_preview(demand)):
            self.cancel()
        self.pending = demand
        self._start()

    def cancel(self):
        self.cancelled.set()
        self.pending = None
        for future in tuple(self._transfers):
            future.cancel()
        with self.condition:
            self.discarded += len(self.ready)
            self.ready.clear()
            self.ready_bytes = 0
            self.condition.notify_all()

    def ready_feedback_originals(self, identity):
        """Queue a source-only handoff on this lane's owning thread.

        Callers use it only if already ready; cold input never waits for another
        scene evaluator. Adoption retains the existing pin/version validation.
        """
        return self.executor.submit(self._feedback_originals, identity)

    def _feedback_originals(self, identity):
        originals = ReadyOriginals()
        backend = self._backend
        if (backend is not None and backend.snapshot.document.identity == identity
                and not backend.snapshot.document.live_preview):
            originals.capture(backend.snapshot)
        elif self._originals.identity is not None and self._originals.identity[0] == identity:
            originals.identity = self._originals.identity
            originals.images = self._originals.images
            originals.tiles = self._originals.tiles
        return originals

    def _start(self):
        if self.future is not None or self.pending is None or self.stopped.is_set():
            return
        demand, self.pending = self.pending, None
        self.active_demand = demand
        token = self.cancelled = Event()
        self.future = self.executor.submit(self._evaluate, demand, token)
        self.submitted += 1

    def _publish(self, completion, token):
        size = sum(int(image.sizeInBytes()) for image, exact in (completion.tiles or {}).values() if exact)
        if completion.preview is not None:
            size += int(completion.preview.image.sizeInBytes())
        if completion.overview is not None:
            size += int(completion.overview.image.sizeInBytes())
        size += sum(int(image.sizeInBytes()) for image in (completion.materialized or {}).values())
        if completion.feedback is not None:
            size += completion.feedback.byte_count
        with self.condition:
            if completion.demand.feedback_only and completion.feedback is not None:
                # Each progressive value contains the preceding ready patches.
                # Keep one latest handoff instead of retaining cumulative plane
                # aliases for every patch while the GUI processes input.
                retained = deque()
                for previous in self.ready:
                    if (previous.demand.feedback_only
                            and previous.demand.serial == completion.demand.serial
                            and previous.feedback is not None):
                        self.ready_bytes -= previous.feedback.byte_count
                    else:
                        retained.append(previous)
                self.ready = retained
            contact_preview = ((self._contact_preview_bounded(completion.demand)
                                or self._geometry_preview_bounded(completion.demand))
                and completion.preview is not None and completion.done and not completion.error
                and not completion.tiles and completion.materialized is None
                and completion.overview is None and completion.feedback is None
                and not completion.recorded and not completion.recorded_rows and completion.cache_manifest is None)
            if contact_preview:
                # Do not hold render admission while waiting for the GUI. The
                # owned preview size was bounded before submission; retain one
                # latest provisional output for this owner, never exact tiles.
                retained = deque()
                for previous in self.ready:
                    if (previous.preview is not None and previous.done and not previous.error
                            and not previous.recorded and not previous.recorded_rows
                            and previous.cache_manifest is None
                            and previous.demand.serial <= completion.demand.serial
                            and previous.demand.snapshot.document.revision <= completion.demand.snapshot.document.revision
                            and previous.demand.contact_preview_owner
                            == completion.demand.contact_preview_owner and not previous.tiles
                            and previous.demand.interaction_preview_owner
                            == completion.demand.interaction_preview_owner
                            and previous.materialized is None and previous.overview is None
                            and previous.feedback is None):
                        self.ready_bytes -= int(previous.preview.image.sizeInBytes())
                        self.discarded += 1
                    else:
                        retained.append(previous)
                self.ready = retained
                if self.ready_bytes + size > self.handoff_budget:
                    return False
            while not contact_preview and self.ready and self.ready_bytes + size > self.handoff_budget:
                if token.is_set() or self.stopped.is_set():
                    return False
                self.condition.wait(.05)
            if token.is_set() or self.stopped.is_set():
                return False
            self.ready.append(completion)
            self.ready_bytes += size
        if self.ready_callback is not None:
            try:
                self.ready_callback()
            except RuntimeError:
                # A queued GUI receiver may retire immediately after publish.
                # Its owner closes this lane; completed resources remain guarded.
                pass
        return True

    def materialize(self, demand, images):
        """Queue a real raster/display consumer on the existing detached lane."""
        if self.stopped.is_set():
            return
        from comic_editor.render.device import DeviceImage
        keys,owned = tuple(images),{}
        for key,image in images.items():
            view = image.copy(image.rect())
            if isinstance(view,DeviceImage):
                owned[key] = view
        if self.cancelled.is_set():
            self.cancelled = Event()
        token = self.cancelled
        future = self.executor.submit(self._materialize,demand,owned,token,keys)
        self._transfers.add(future)

    def _materialize(self,demand,images,token,keys):
        try:
            with RENDER_ADMISSION.reserve('display-edge',2*sum(image.sizeInBytes() for image in images.values()),
                    priority=0,cancelled=lambda:token.is_set() or self.stopped.is_set()):
                completed = {}
                for key,image in images.items():
                    if token.is_set() or self.stopped.is_set():
                        return
                    if not image.isNull():
                        try:
                            completed[key] = image.materialize()
                        except RuntimeError:
                            if not image.isNull():
                                raise
                            # Lost device storage is an ordinary cache miss.
                            # The next unchanged demand can use exact CPU
                            # evaluation rather than becoming a terminal error.
                self._publish(SceneCompletion(demand,materialized=completed,transfer_keys=keys),token)
        except WorkCancelled:
            pass
        except Exception as error:
            self._publish(SceneCompletion(demand,error=f'{type(error).__name__}: {error}',transfer_keys=keys),token)
        finally:
            for image in images.values():
                image.release()

    def _evaluate(self, demand, token):
        try:
            if demand.visibility_preview_owner is not None and not self._visibility_preview_bounded(demand):
                raise ValueError('Visibility presentation requires its bounded isolated demand')
            if isinstance(demand.snapshot, (ContactSceneSnapshot, GeometrySceneSnapshot)):
                if token.is_set() or self.stopped.is_set():
                    return  # Retired work never inspects its released lease/context.
                snapshot = demand.snapshot
                holder = snapshot.capture_lease
                copies = getattr(holder, 'lease', None)
                bounded = (self._contact_preview_bounded(demand) if isinstance(snapshot, ContactSceneSnapshot)
                           else self._geometry_preview_bounded(demand))
                if (not bounded
                        or snapshot.cache_spec is not None or snapshot.graphics_worker is not None
                            and getattr(snapshot.graphics_worker, 'closed', False)
                        or not isinstance(copies, CopyLease)
                        or copies.admission is not RENDER_ADMISSION or copies.released):
                    raise ValueError('Private preview capture requires its live owned copy lease')
                owner = (snapshot.contact_preview_owner if isinstance(snapshot, ContactSceneSnapshot)
                         else snapshot.interaction_preview_owner)
                if copies.scope is not owner:
                    raise ValueError('Private preview copies require their actual interaction scope')
                # Charge the complete unchanged worker estimate AND retained
                # private copies; credit those exact already-reserved copies
                # once through the existing admission implementation.
                size = demand_working_bytes(demand) + copies.size
                with RENDER_ADMISSION.reserve(demand.consumer, size, priority=0,
                        cancelled=lambda: token.is_set() or self.stopped.is_set(), copies=copies,
                        own_copies=True):
                    return self._evaluate_admitted(demand, token)
            if self._background_cache(demand):
                # A chapter warm/record request may contain hundreds of rows.
                # Reserve only each ordinary native block, so a newly visible
                # view can acquire the same admission budget between rows.
                return self._evaluate_admitted(demand, token)
            with RENDER_ADMISSION.reserve(demand.consumer, demand_working_bytes(demand),
                    priority=1 if demand.record else 2 if demand.consumer == "navigator" else 0,
                    cancelled=lambda: token.is_set() or self.stopped.is_set()):
                return self._evaluate_admitted(demand, token)
        except WorkCancelled:
            return

    @staticmethod
    def _background_cache(demand):
        return demand.record or demand.consumer in {'cache', 'cache-memory'}

    def _cache_block_admission(self, demand, token):
        if not self._background_cache(demand):
            return nullcontext()
        return RENDER_ADMISSION.reserve('cache', demand_working_bytes(demand), priority=2,
            cancelled=lambda: token.is_set() or self.stopped.is_set())

    def _evaluate_admitted(self, demand, token):
        if demand.interaction_preview_owner != self._geometry_draft_owner:
            self._geometry_draft_cache = None
            self._geometry_draft_owner = demand.interaction_preview_owner
        try:
            if token.is_set() or self.stopped.is_set():
                return
            snapshot = demand.snapshot
            if self._background_cache(demand) and (
                    snapshot.document.live_preview or demand.preview_size is not None
                    or demand.presentation_size is not None or demand.feedback_only
                    or demand.feedback_target or demand.contact_preview_owner is not None):
                raise ValueError('Background caching requires ordinary exact native tiles')
            if demand.feedback_only:
                # A contact's separable planes have a stable dependency owner.
                # Selected pixels can advance while this private preparation
                # finishes; it must neither render nor record ordinary tiles.
                if demand.record or snapshot.cache_spec is not None or snapshot.document.live_preview:
                    raise ValueError('Contact preparation requires a private nonrecording snapshot')
                originals = demand.feedback_originals
                if originals is not None and originals.done() and not originals.cancelled():
                    originals.result().adopt(snapshot)
                from comic_editor.render.raster_feedback import prepare_raster_feedback
                feedback = prepare_raster_feedback(snapshot, demand.feedback_target, demand.visible,
                    budget=64 * 1024 * 1024,
                    cancelled=lambda: token.is_set() or self.stopped.is_set(), focus=demand.center,
                    progress=lambda prepared: self._publish(SceneCompletion(demand, feedback=prepared), token))
                self._publish(SceneCompletion(demand, done=True, feedback=feedback), token)
                return
            if demand.consumer == "navigator":
                chapter = copy.copy(snapshot.chapter)
                chapter.objects = dict(chapter.objects)
                for identifier, obj in chapter.objects.items():
                    if getattr(obj, "reference_role", "") == "uv_map" and obj.visible:
                        chapter.objects[identifier] = copy.copy(obj)
                        chapter.objects[identifier].visible = False
                snapshot = replace(snapshot, chapter=chapter)
            with self._cache_block_admission(demand, token):
                if self._backend is None or self._backend.snapshot is not snapshot:
                    if self._backend is not None:
                        self._cache_state = self._backend.scene.cache_state()
                        self._originals.capture(self._backend.snapshot)
                        self._backend.close()
                    identity = (snapshot.document.identity, snapshot.document.pixel_contract.signature,
                        getattr(snapshot.pixel_environment, "signature", None), snapshot.graphics_worker)
                    if identity != self._cache_identity:
                        self._cache_state = None
                    self._cache_identity = identity
                    self._backend = DetachedSceneBackend(snapshot)
                    self._originals.adopt(snapshot)
                    if self._cache_state is not None:
                        self._backend.scene.adopt_cache_state(self._cache_state)
                        self._cache_state = None
            backend = self._backend
            backend.scene._effect_preview_channel = 'canvas' if self._background_cache(demand) else demand.consumer
            if demand.record and not backend._record_allowed():
                raise ValueError('This scene cannot record exact cache entries')
            service = DocumentRenderService(backend)
            service.projection.revision = snapshot.document.revision
            if (snapshot.document.live_preview or demand.preview_size is not None) and demand.visible is not None:
                x, y, width, height = demand.visible
                scale = min(1., 1024. / max(1., width, height))
                size = demand.preview_size or (max(1, math.ceil(width * scale)), max(1, math.ceil(height * scale)))
                sx, sy = size[0] / width, size[1] / height
                request = RenderRequest(demand.visible, min(1., max(sx, sy)), size,
                    ("detached-preview", demand.serial), snapshot.document.revision,
                    quality=RenderQuality.INTERACTIVE,
                    output_transform=(sx, 0., 0., sy, -x * sx, -y * sy),
                    smooth_sources=demand.consumer == "navigator")
                # Lower-density source captures belong to this provisional
                # consumer. Their source/effect keys must never populate the
                # retained native evaluator, including a later overview pass.
                preview_backend = DetachedSceneBackend(snapshot)
                preview_backend.scene._effect_preview_channel = demand.consumer
                if demand.consumer == "canvas":
                    preview_backend.artwork_scale = 1.
                    preview_backend.native_preview = True
                    if self._visibility_preview_bounded(demand):
                        from comic_editor.render.live_canvas_preview import VisibilityCanvasPreviewPolicy
                        preview_backend.live_canvas_preview_policy = VisibilityCanvasPreviewPolicy(
                            snapshot.document.identity, snapshot.document.revision, demand.serial,
                            lambda: token.is_set() or self.stopped.is_set())
                    elif (snapshot.document.live_preview and not snapshot.document.contact_only
                            and not demand.record
                            and not snapshot.document.pixel_contract.floating
                            and not snapshot.document.overflow
                            and snapshot.document.underlay == ('', 0.)):
                        from comic_editor.render.live_canvas_preview import LiveCanvasPreviewPolicy
                        preview_backend.live_canvas_preview_policy = LiveCanvasPreviewPolicy(
                            snapshot.document.identity, snapshot.document.revision, demand.serial,
                            lambda: token.is_set() or self.stopped.is_set())
                # A live draft uses one declared sampling policy even when the
                # retained native evaluator is warm. Its effect/source-capture
                # caches start empty, matching an independent cold reference.
                # Snapshot-owned original pixels remain reusable; this private
                # evaluator never transfers new captures/results back. Other
                # preview contracts keep their existing native cache adoption.
                if getattr(preview_backend, 'live_canvas_preview_policy', None) is None:
                    preview_backend.scene.adopt_cache_state(backend.scene.cache_state())
                elif (self._geometry_preview_bounded(demand)
                        and demand.interaction_preview_owner == self._geometry_draft_owner
                        and self._geometry_draft_cache is not None):
                    # Transfer only this gesture's private draft/native-subset
                    # semantic caches. No editor, source owner, or exact/disk
                    # evaluator receives the transferred derived results.
                    preview_backend.scene.adopt_cache_state(self._geometry_draft_cache)
                preview_service = DocumentRenderService(preview_backend)
                preview_service.projection.revision = snapshot.document.revision
                try:
                    # A whole-chapter thumbnail must relinquish its native
                    # reservation when a visible/input consumer arrives. Its
                    # private partial image is discarded; idle retry captures
                    # the latest revision through this same renderer.
                    with _scene_cancellation(preview_backend.scene,
                            lambda: token.is_set() or self.stopped.is_set()
                                or demand.consumer == 'navigator'
                                    and RENDER_ADMISSION.higher_priority_waiting(2)):
                        result = preview_service.render_region(snapshot.document, request)
                    if (self._geometry_preview_bounded(demand) and not token.is_set()
                            and not self.stopped.is_set() and not result.image.isNull()
                            and result.status.value == 'provisional'):
                        self._geometry_draft_cache = preview_backend.scene.cache_state()
                finally:
                    preview_backend.close()
                if demand.consumer == "navigator" and not result.image.isNull():
                    from comic_editor.render.pixels import display_image, pixel_scope
                    with pixel_scope(snapshot.document.pixel_contract, environment=snapshot.pixel_environment):
                        result = replace(result, image=display_image(result.image, snapshot.document.pixel_contract))
                if not result.image.isNull() and result.status.value in {"exact", "provisional"}:
                    self._publish(SceneCompletion(demand, done=demand.presentation_size is None, preview=result), token)
                else:
                    self._publish(SceneCompletion(demand, done=True, error=result.error or result.status.value), token)
                    return
                if demand.presentation_size is not None:
                    from comic_editor.render.overview import exact_overview
                    overview = exact_overview(demand, backend, service,
                        lambda: token.is_set() or self.stopped.is_set())
                    self._publish(SceneCompletion(demand, done=True, overview=overview), token)
                if not snapshot.document.live_preview and demand.visibility_preview_owner is None:
                    self._prepare_feedback(demand, backend, token)
                return
            requests = list(demand.requests)
            groups = {}
            for request in requests:
                address = request.address
                group = (address.level, 0, address.y) if self._background_cache(demand) else (address.level,address.x // 4,address.y // 4)
                groups.setdefault(group, []).append(request)
            def distance(item):
                request = groups[item][0]
                side = (1 if self._background_cache(demand) else 4) * request.tile_size / request.scale
                return ((item[1] + .5) * side - demand.center[0]) ** 2 + ((item[2] + .5) * side - demand.center[1]) ** 2
            for group in sorted(groups, key=distance):
                row_cache_keys = []
                for phase in demand.phases:
                    if token.is_set() or self.stopped.is_set():
                        return
                    with self._cache_block_admission(demand, token):
                        restored = {}
                        missing = []
                        for tile in groups[group]:
                            image = backend.lookup_tile(tile, phase)
                            if image is not None and not image.isNull():
                                restored[tile.address] = image, True
                            else:
                                missing.append(tile)
                        with backend.record_tiles(demand.record), _scene_cancellation(
                                backend.scene, lambda: token.is_set() or self.stopped.is_set()):
                            result = service.render_tiles(snapshot.document, missing,
                                TileBatchPolicy(demand.center), phase=phase, defer_effects=False)
                            if demand.record:
                                for tile in missing:
                                    image,exact = result.tiles[tile.address]
                                    if exact:
                                        backend.retain_tile(tile,phase,image)
                        result.tiles.update(restored)
                        cache_keys = []
                        retain_memory = getattr(backend, 'retain_memory_tile', None)
                        if (callable(retain_memory) and not result.error and not result.pending
                                and all(exact for _, exact in result.tiles.values())):
                            template = groups[group][0]
                            for address, (image, exact) in result.tiles.items():
                                tile = ProjectionRequest(address, template.tile_size, template.gutter)
                                key = retain_memory(tile, phase, image)
                                if key is not None:
                                    cache_keys.append((address, phase, key))
                        row_cache_keys.extend(cache_keys)
                    if result.error or result.pending or not all(exact for _, exact in result.tiles.values()):
                        self._publish(SceneCompletion(demand, done=True,
                            error=result.error or "Scene evaluation did not produce an exact block"), token)
                        return
                    if not demand.record and not self._publish(SceneCompletion(demand,phase,result.tiles,
                            cache_keys=tuple(cache_keys)),token):
                        return
                if demand.record:
                    backend.finish_recording()
                    manifest = backend.record_manifest()
                    if not self._publish(SceneCompletion(demand,recorded=True,
                            recorded_rows=(group[2],),cache_manifest=manifest,
                            cache_keys=tuple(row_cache_keys)),token):
                        return
            if demand.record:
                backend.finish_recording()
            self._prepare_feedback(demand, backend, token)
            self._publish(SceneCompletion(demand, done=True,recorded=demand.record), token)
        except WorkCancelled:
            if demand.consumer == 'navigator':
                self._publish(SceneCompletion(demand, done=True), token)
            return
        except Exception as error:
            self._publish(SceneCompletion(demand, done=True, error=f"{type(error).__name__}: {error}"), token)
        finally:
            if (token.is_set() or self.stopped.is_set()) and self._backend is not None:
                if not self.stopped.is_set():
                    self._cache_state = self._backend.scene.cache_state()
                    self._originals.capture(self._backend.snapshot)
                self._backend.close()
                self._backend = None

    def _prepare_feedback(self, demand, backend, token):
        if demand.feedback_target and not demand.record and not demand.snapshot.document.live_preview:
            from comic_editor.render.raster_feedback import prepare_raster_feedback
            feedback = prepare_raster_feedback(demand.snapshot, demand.feedback_target, demand.visible,
                caches=backend.scene.cache_state(),
                cancelled=lambda: token.is_set() or self.stopped.is_set())
            if feedback is None and demand.phases == (None,):
                from comic_editor.render.filtered_raster_feedback import prepare_filtered_raster_feedback
                feedback = prepare_filtered_raster_feedback(demand.snapshot,demand.feedback_target,
                    demand.requests if demand.feedback_requests is None else demand.feedback_requests,
                    demand.visible,caches=backend.scene.cache_state(),
                    cancelled=lambda: token.is_set() or self.stopped.is_set())
            if feedback is not None:
                self._publish(SceneCompletion(demand, feedback=feedback), token)

    def poll(self):
        with self.condition:
            completed = tuple(self.ready)
            self.ready.clear()
            self.ready_bytes = 0
            self.condition.notify_all()
        if self.future is not None and self.future.done():
            self.future.result()
            self.future = None
            self.active_demand = None
            self.completed += 1
            self._start()
        self._transfers = {future for future in self._transfers if not future.done()}
        return completed

    def close(self):
        if self.stopped.is_set():
            return
        self.stopped.set()
        self.cancel()
        try:
            self.executor.submit(self._close_backend)
        except RuntimeError:
            # Python may have already shut down executors before Qt destroys
            # children. No new IO or renderer work may start during teardown.
            self._backend = None
        self.executor.shutdown(wait=False, cancel_futures=False)

    def _close_backend(self):
        self._cache_state = None
        self._geometry_draft_cache = self._geometry_draft_owner = None
        self._originals.clear()
        if self._backend is not None:
            self._backend.close()
            self._backend = None
