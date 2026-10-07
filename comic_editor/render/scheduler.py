"""Bounded scene evaluation with latest-demand admission and guarded results.

One scene evaluator owns model/source caches. GPU kernels use their own context
owner. The GUI polls completed immutable blocks and never waits for this lane.
Cancellation is observed between ordinary native capture blocks; a kernel
already executing may finish, but cannot publish into another revision.
"""
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from dataclasses import replace
import copy
import math
from threading import Condition, Event

from comic_editor.render.service import DocumentRenderService, TileBatchPolicy, RenderRequest, RenderQuality
from comic_editor.render.scene import DetachedSceneBackend
from comic_editor.render.admission import RENDER_ADMISSION, WorkCancelled, snapshot_working_bytes
from comic_editor.render.source_resources import ReadyOriginals


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


def demand_working_bytes(demand):
    size = snapshot_working_bytes(demand.snapshot)
    if demand.feedback_target:
        size += 32 * 1024 * 1024
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
    def __init__(self, *, handoff_budget=64 * 1024 * 1024):
        self.executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="scene-evaluate")
        self.condition = Condition()
        self.stopped = Event()
        self.cancelled = Event()
        self.future = None
        self.pending = None
        self.ready = deque()
        self.ready_bytes = 0
        self.handoff_budget = max(1, handoff_budget)
        self.submitted = self.completed = self.discarded = 0
        self._backend = None
        self._cache_state = None
        self._cache_identity = None
        self._originals = ReadyOriginals()
        self._transfers = set()

    @property
    def busy(self):
        return self.future is not None or self.pending is not None or bool(self._transfers)

    def submit(self, demand):
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

    def _start(self):
        if self.future is not None or self.pending is None or self.stopped.is_set():
            return
        demand, self.pending = self.pending, None
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
            while self.ready and self.ready_bytes + size > self.handoff_budget:
                if token.is_set() or self.stopped.is_set():
                    return False
                self.condition.wait(.05)
            if token.is_set() or self.stopped.is_set():
                return False
            self.ready.append(completion)
            self.ready_bytes += size
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
            with RENDER_ADMISSION.reserve(demand.consumer, demand_working_bytes(demand),
                    priority=1 if demand.record else 2 if demand.consumer == "navigator" else 0,
                    cancelled=lambda: token.is_set() or self.stopped.is_set()):
                return self._evaluate_admitted(demand, token)
        except WorkCancelled:
            return

    def _evaluate_admitted(self, demand, token):
        try:
            if token.is_set() or self.stopped.is_set():
                return
            snapshot = demand.snapshot
            if demand.consumer == "navigator":
                chapter = copy.copy(snapshot.chapter)
                chapter.objects = dict(chapter.objects)
                for identifier, obj in chapter.objects.items():
                    if getattr(obj, "reference_role", "") == "uv_map" and obj.visible:
                        chapter.objects[identifier] = copy.copy(obj)
                        chapter.objects[identifier].visible = False
                snapshot = replace(snapshot, chapter=chapter)
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
            backend.scene._effect_preview_channel = 'canvas' if demand.record else demand.consumer
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
                # Valid native results may be reused by a preview. Its private
                # bookkeeping and new captures are discarded afterwards.
                preview_backend.scene.adopt_cache_state(backend.scene.cache_state())
                preview_service = DocumentRenderService(preview_backend)
                preview_service.projection.revision = snapshot.document.revision
                try:
                    result = preview_service.render_region(snapshot.document, request)
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
                if not snapshot.document.live_preview:
                    self._prepare_feedback(demand, backend, token)
                return
            requests = list(demand.requests)
            groups = {}
            for request in requests:
                address = request.address
                group = (address.level, 0, address.y) if demand.record else (address.level,address.x // 4,address.y // 4)
                groups.setdefault(group, []).append(request)
            def distance(item):
                request = groups[item][0]
                side = (1 if demand.record else 4) * request.tile_size / request.scale
                return ((item[1] + .5) * side - demand.center[0]) ** 2 + ((item[2] + .5) * side - demand.center[1]) ** 2
            for group in sorted(groups, key=distance):
                for phase in demand.phases:
                    if token.is_set() or self.stopped.is_set():
                        return
                    restored = {}
                    missing = []
                    for tile in groups[group]:
                        image = backend.lookup_tile(tile, phase)
                        if image is not None and not image.isNull():
                            restored[tile.address] = image, True
                        else:
                            missing.append(tile)
                    with backend.record_tiles(demand.record):
                        result = service.render_tiles(snapshot.document, missing,
                            TileBatchPolicy(demand.center), phase=phase, defer_effects=False)
                        if demand.record:
                            for tile in missing:
                                image,exact = result.tiles[tile.address]
                                if exact:
                                    backend.retain_tile(tile,phase,image)
                    result.tiles.update(restored)
                    if result.error or result.pending or not all(exact for _, exact in result.tiles.values()):
                        self._publish(SceneCompletion(demand, done=True,
                            error=result.error or "Scene evaluation did not produce an exact block"), token)
                        return
                    if not demand.record and not self._publish(SceneCompletion(demand,phase,result.tiles),token):
                        return
                if demand.record:
                    backend.finish_recording()
                    manifest = backend.record_manifest()
                    if not self._publish(SceneCompletion(demand,recorded=True,
                            recorded_rows=(group[2],),cache_manifest=manifest),token):
                        return
            if demand.record:
                backend.finish_recording()
            self._prepare_feedback(demand, backend, token)
            self._publish(SceneCompletion(demand, done=True,recorded=demand.record), token)
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
        self._originals.clear()
        if self._backend is not None:
            self._backend.close()
            self._backend = None
