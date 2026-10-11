"""GUI adapter: request detached work and present only completed resources."""
from PySide6.QtCore import QObject, QRectF, QTimer, Qt, Signal
from dataclasses import replace
import math

from comic_editor.core.tools import ToolKind
from comic_editor.render.projection import ProjectionRequest
from comic_editor.render.admission import CopyLease, RENDER_ADMISSION
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.render.ordinary_contact_capture import CaptureAdmissionUnavailable, CaptureUnavailable, ContactSceneSnapshot, OrdinaryContactCapture
from comic_editor.ui.document_presentation import PresentedTile
from comic_editor.ui.raster_feedback import RasterFeedbackPatchCache


class SceneController(QObject):
    filteredFinished = Signal()
    bootstrapFinished = Signal()

    def __init__(self, canvas):
        super().__init__(canvas)
        self.canvas = canvas
        self.scheduler = SceneScheduler()
        self.capture = None
        self.snapshot = None
        self.desired = None
        self.serial = 0
        self.dispatched = None
        self.preview = None
        self._preview_snapshot = self._preview_request = None
        from comic_editor.ui.release_handoff import ReleaseHandoff
        self.release_handoff = ReleaseHandoff(self)
        if hasattr(canvas, "frameSwapped"):
            canvas.frameSwapped.connect(self.release_handoff.swapped)
        self.overview = None
        self.error = ""
        self.preview_mode = False
        self.feedback_target = ''
        self._contact_reuse_gate = None
        self._contact_preview_key = self._contact_preview_owner = None
        self._contact_preview_generation = 0
        self._contact_preview_dependency_generation = 0
        self._contact_preview_presented_revision = -1
        self.feedback = None
        self.feedback_dirty = set()
        self.feedback_patches = RasterFeedbackPatchCache()
        from comic_editor.ui.raster_presentation_continuity import RasterPresentationContinuity
        self.raster_continuity = RasterPresentationContinuity()
        self._presentation_batch = None
        self._bootstrap = None
        self._bootstrap_serial = 0
        self._bootstrap_dispatched = False
        self._bootstrap_failed = None
        self.bootstrap_scheduler = SceneScheduler(ready_callback=self.bootstrapFinished.emit)
        self.bootstrap_timer = QTimer(self)
        self.bootstrap_timer.setInterval(8)
        self.bootstrap_timer.timeout.connect(self._advance_bootstrap)
        self.bootstrapFinished.connect(self._advance_bootstrap, Qt.QueuedConnection)
        self._filtered_state = None
        self._filtered_requests = ()
        self._filtered_lifetime = {'state':None}
        self.filteredFinished.connect(self.advance,Qt.QueuedConnection)
        lifetime = self._filtered_lifetime
        self.destroyed.connect(lambda: lifetime['state'].close() if lifetime['state'] is not None else None)
        self._cpu_pending = set()
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        scheduler = self.scheduler
        self.destroyed.connect(lambda: scheduler.close())
        bootstrap_scheduler = self.bootstrap_scheduler
        self.destroyed.connect(lambda: bootstrap_scheduler.close())

    def reset(self, *, retain_presentation=False):
        if not retain_presentation:
            self.raster_continuity.reset()
            self._presentation_batch = None
        self._retire_contact_preview()
        self._retire_bootstrap()
        self._bootstrap_failed = None
        self._retire_filtered()
        self.release_handoff.clear("controller reset")
        self._preview_snapshot = self._preview_request = None
        self.scheduler.cancel()
        self.capture = self.snapshot = self.desired = self.dispatched = self.preview = self.overview = None
        self.error = ""
        self.canvas._projection_render_error = ""
        self.canvas._projection_error_revision = -1
        self._cpu_pending.clear()
        self.feedback_target = ''
        self._contact_reuse_gate = None
        self.feedback = None
        self.feedback_dirty.clear()
        self.feedback_patches.clear()
        self.timer.stop()

    def request(self, document, requests, phases, visible):
        from comic_editor.core.models import RasterObject
        target = self.canvas.selected_id if (self.canvas.selected_kind == 'object' and
            isinstance(self.canvas.chapter.objects.get(self.canvas.selected_id), RasterObject)) else ''
        if target != self.feedback_target:
            self.checkpoint_raster_presentation(document)
            self._retire_bootstrap()
            self._bootstrap_failed = None
            self._retire_filtered()
            self.feedback_target = target
            self.feedback = None
            self.feedback_dirty.clear()
            self.feedback_patches.clear()
            self.dispatched = None
        graphics = getattr(self.canvas,'_graphics_worker',None)
        if self.snapshot is not None and self.snapshot.graphics_worker is not graphics:
            self.retire_feedback()
            self.snapshot = self.capture = self.dispatched = None
            self.desired = None
            self._cpu_pending.clear()
        bytes_per_pixel = 16 if document.pixel_contract.floating else 4
        view_bytes = len(requests) * (self.canvas._document_projection.tile_size +
                     2 * self.canvas._document_projection.gutter) ** 2 * bytes_per_pixel * len(phases)
        self.preview_mode = (not visible.isEmpty() and
            (document.live_preview or view_bytes > self.canvas._document_projection.budget // 2))
        signature = document, tuple(request.address for request in requests), tuple(phases), tuple(visible.getRect())
        self._ensure_bootstrap(document, requests, phases, signature[3])
        if self._filtered_state is not None and (
                document.identity != self.feedback.document.identity or
                document.configuration != self.feedback.document.configuration):
            self.retire_feedback()
        self._filtered_requests = tuple(requests)
        if self._filtered_state is not None and not self._filtered_state.advance(document):
            self.retire_feedback()
        gate = (self._pending_native_contact_gate(document)
            or self._bootstrap_contact_gate(document, signature[3])
            or self._covered_contact_gate(document, signature[3]))
        if gate is not None:
            if gate.busy and self._hold_owned_busy_contact_preview(signature, gate):
                return
            self._retire_contact_preview()
            self._hold_contact_feedback(signature, gate)
            return
        contact_key = self._contact_preview_key_for(document, signature[1], signature[2], signature[3])
        previous_owner = self._contact_preview_owner
        if contact_key != self._contact_preview_key:
            self._retire_contact_preview()
            # A camera/context/owner change can leave the ordinary document
            # signature unchanged. It still retires this preview generation.
            self.desired = None
            self.dispatched = None
            if contact_key is not None:
                self._contact_preview_generation += 1
                self._contact_preview_key = contact_key
                self._contact_preview_owner = (self._contact_preview_generation, contact_key)
        contact_continues = (self._contact_preview_owner is not None
            and self._contact_preview_owner == previous_owner)
        if self._contact_reuse_gate is not None:
            # Release or any eligibility change resumes ordinary guarded work,
            # including a transition with otherwise unchanged demand metadata.
            self._contact_reuse_gate = None
            self.desired = None
        if signature != self.desired:
            self.release_handoff.new_request(signature)
            self.serial += 1
            self.desired = signature
            self.dispatched = None
            if not contact_continues:
                self.scheduler.cancel()
            self._cpu_pending.clear()
            self.error = ""
            self.canvas._projection_render_error = ""
            self.canvas._projection_error_revision = -1
            if self.snapshot is None or self.snapshot.document != document:
                self.capture = self._new_scene_capture(document)
        elif (self.dispatched == signature and not self.preview_mode and not self.error
                and not self.scheduler.busy and any(
                    len(self.canvas._document_projection.ready(requests,
                        configuration=(*document.configuration,phase))) != len(requests)
                    for phase in phases)):
            # Storage can expire without a document change (context loss or
            # cache eviction). Reevaluate the same guarded frozen request.
            self.dispatched = None
        handoff = self.release_handoff.for_request(signature, self.serial)
        if handoff is not None:
            self.preview = handoff
        if self.dispatched != signature and not self.timer.isActive():
            self.timer.start(0)

    def _new_scene_capture(self, document):
        previous = self.capture
        if isinstance(previous, OrdinaryContactCapture):
            if previous.valid(document, self._contact_preview_owner):
                return previous
            previous.retire()
        if self._contact_preview_owner is not None:
            try:
                return OrdinaryContactCapture(self.canvas, document, self._contact_preview_owner)
            except CaptureAdmissionUnavailable:
                # Retain the actual immutable worker while its admission ticket
                # excludes the next bounded copy. Other refusals still fall back.
                if self.capture is None and self._preserve_owned_contact_work(self.desired):
                    return None
            except CaptureUnavailable:
                pass  # Bounded nonblocking refusal retains ordinary exact capture.
        return self.canvas._scene_snapshot_compiler.capture(self.canvas, document)

    def _current_contact_snapshot(self, snapshot, document, addresses, phases, visible):
        # Completion alone does not make R0 current. The actual stable owner and
        # full native/view/configuration key must still be positively current.
        if not isinstance(snapshot, ContactSceneSnapshot):
            return False
        owner = self._contact_preview_owner
        lease = snapshot.capture_lease
        return (owner is not None and snapshot.contact_preview_owner == owner
            and self._contact_preview_key_for(document, addresses, phases, visible) == owner[1]
            and replace(snapshot.document, revision=0) == replace(document, revision=0)
            and snapshot.document.revision <= document.revision
            and snapshot.cache_spec is None and lease is not None and not lease.lease.released
            and snapshot.graphics_worker is getattr(self.canvas, '_graphics_worker', None))

    def _retire_contact_preview(self):
        if isinstance(self.capture, OrdinaryContactCapture):
            self.capture.retire()
            self.capture = None
        if isinstance(self.snapshot, ContactSceneSnapshot):
            self.snapshot = None
        self._contact_preview_key = self._contact_preview_owner = None
        self._contact_preview_presented_revision = -1

    def _contact_preview_key_for(self, document, addresses, phases, visible, *, allow_busy=False):
        canvas = self.canvas
        if (not document.live_preview or not document.contact_only or phases not in ((None,), ('base', 'top'))
                or canvas.tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}
                or not addresses or any(address.level != 0 for address in addresses)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or document.revision != canvas._document_projection.revision
                or not canvas._projection_contact_only()):
            return None
        gate = self._owned_native_contact_gate(document)
        if gate is None or (gate.busy and not allow_busy):
            return None
        current_visible = canvas.visible_document_rect()
        if document.overflow <= 0:
            current_visible = current_visible.intersected(document.bounds)
        if tuple(current_visible.getRect()) != visible:
            return None
        camera = canvas.camera_transform()
        view = tuple(getattr(camera, f'm{i}{j}')() for i in range(1, 4) for j in range(1, 4))
        return (replace(document, revision=0), addresses, phases, visible, view,
            canvas._document_projection.tile_size, canvas._document_projection.gutter,
            canvas.width(), canvas.height(), canvas.devicePixelRatioF(), id(gate),
            id(canvas.chapter.objects.get(canvas.selected_id)), canvas.selected_id,
            id(getattr(canvas, '_graphics_worker', None)),
            getattr(canvas, '_history_generation', 0), self._contact_preview_dependency_generation)

    def _preserve_owned_contact_work(self, signature, *, busy_gate=None):
        """Validate existing immutable R0 work; never advance or adopt here."""
        try:
            document, addresses, phases, visible = signature
            canvas, scheduler = self.canvas, self.scheduler
            owner = self._contact_preview_owner
            if (owner is None or owner != (self._contact_preview_generation, self._contact_preview_key)
                    or not self.preview_mode or self._contact_reuse_gate is not None or self._cpu_pending
                    or self._filtered_state is not None or self._bootstrap is not None
                    or scheduler.stopped.is_set() or scheduler.cancelled.is_set() or scheduler._transfers
                    or not 1 <= len(addresses) <= 256
                    or not all(math.isfinite(value) for value in visible)
                    or visible[2] <= 0 or visible[3] <= 0):
                return False
            current = (document if document.revision == canvas._document_projection.revision
                       else canvas._render_document_state())
            current_signature = (current, addresses, phases, visible)
            if (document.identity != current.identity
                    or not 0 <= document.revision <= current.revision
                    or replace(document, revision=0) != replace(current, revision=0)
                    or self._contact_preview_key_for(current, addresses, phases, visible,
                        allow_busy=True) != self._contact_preview_key):
                return False
            gate = self._owned_native_contact_gate(current)
            if gate is None or (busy_gate is not None and (gate is not busy_gate or not gate.busy)):
                return False
            worker = getattr(canvas, '_graphics_worker', None)
            if worker is not None and getattr(worker, 'closed', True):
                return False
            context_getter = getattr(canvas, 'context', None)
            if callable(context_getter):
                context = context_getter()
                if (context is None or not context.isValid() or not canvas.isValid()
                        or worker is None or getattr(worker, 'share_context', None) is not context):
                    return False

            def positive_lease(holder):
                lease = getattr(holder, 'lease', None)
                return (isinstance(lease, CopyLease) and lease.admission is RENDER_ADMISSION
                        and not lease.released
                        and type(lease.size) is int and lease.size > 0)

            def owned_snapshot(snapshot):
                return (isinstance(snapshot, ContactSceneSnapshot)
                    and snapshot.contact_preview_owner == owner
                    and positive_lease(snapshot.capture_lease)
                    and snapshot.cache_spec is None
                    and snapshot.graphics_worker is worker
                    and snapshot.viewport == (canvas.width(), canvas.height())
                    and 0 <= snapshot.document.revision <= current.revision
                    and replace(snapshot.document, revision=0) == replace(current, revision=0))

            retained = False
            if self.capture is not None:
                if (not isinstance(self.capture, OrdinaryContactCapture)
                        or not positive_lease(self.capture._lease)
                        or not self.capture.valid(current, owner, allow_busy=True,
                            signature=current_signature)):
                    return False
                retained = True
            if self.snapshot is not None:
                if isinstance(self.snapshot, ContactSceneSnapshot):
                    if not owned_snapshot(self.snapshot):
                        return False
                    retained = True
                elif (not retained or self.snapshot.document.live_preview
                        or self.snapshot.document.identity != current.identity
                        or self.snapshot.graphics_worker is not worker):
                    return False
                # A passive prior idle snapshot may coexist with a positively
                # validated private capture. It cannot dispatch or adopt here;
                # every actual active/pending demand still needs a Contact snapshot.
            active, pending = scheduler.active_demand, scheduler.pending
            if (scheduler.future is None) != (active is None):
                return False
            projection = canvas._document_projection
            scale = min(1., 1024. / max(1., visible[2], visible[3]))
            size = max(1, math.ceil(visible[2] * scale)), max(1, math.ceil(visible[3] * scale))
            center = (visible[0] + visible[2] / 2, visible[1] + visible[3] / 2)
            for demand in (active, pending):
                if demand is None:
                    continue
                if (type(demand) is not SceneDemand or not scheduler._contact_preview_bounded(demand)
                        or demand.contact_preview_owner != owner or not owned_snapshot(demand.snapshot)
                        or type(demand.serial) is not int or not 0 <= demand.serial <= self.serial
                        or type(demand.requests) is not tuple
                        or len(demand.requests) != len(addresses)
                        or tuple(request.address for request in demand.requests) != addresses
                        or any(type(request) is not ProjectionRequest
                            or request.tile_size != projection.tile_size
                            or request.gutter != projection.gutter or request.scale != 1.
                            or request.pixel_size != projection.tile_size + 2 * projection.gutter
                            for request in demand.requests)
                        or demand.phases != phases
                        or demand.visible != visible or demand.preview_size != size or demand.center != center
                        or demand.feedback_originals is not None
                        or demand.feedback_target not in ('', self.feedback_target)
                        or demand.feedback_requests != (demand.requests if demand.feedback_target else None)):
                    return False
                retained = True
            if active is not None and pending is not None and (
                    active.serial > pending.serial
                    or active.snapshot.document.revision > pending.snapshot.document.revision
                    or getattr(active.snapshot.pixel_environment, 'signature', None)
                        != getattr(pending.snapshot.pixel_environment, 'signature', None)):
                return False
            return retained
        except Exception:
            # Missing, malformed or stale ownership keeps the original retirement.
            return False

    def _hold_owned_busy_contact_preview(self, signature, gate):
        """Give actual native input priority without cancelling coherent R0 work."""
        if not self._preserve_owned_contact_work(signature, busy_gate=gate):
            return False
        if signature != self.desired:
            self.release_handoff.new_request(signature)
            self.serial += 1
            self.desired = signature
            self.error = ''
            self.canvas._projection_render_error = ''
            self.canvas._projection_error_revision = -1
        # Keep capture/snapshot/dispatched and the actual active cancellation token.
        # The timer rechecks readiness; its busy branch never polls or advances.
        self.timer.start(8)
        return True


    def _accept_contact_preview(self, completion, document, addresses, phases, visible):
        demand, result = completion.demand, completion.preview
        older = demand.snapshot.document
        if (result is None or demand.contact_preview_owner != self._contact_preview_owner
                or self._contact_preview_owner is None
                or self._contact_preview_key_for(document, addresses, phases, visible) != self._contact_preview_key
                or demand.serial > self.serial or not completion.done or completion.error
                or completion.tiles or completion.materialized is not None or completion.overview is not None
                or completion.feedback is not None or completion.recorded or completion.recorded_rows
                or completion.cache_manifest is not None or demand.record
                or demand.presentation_size is not None or demand.feedback_only or demand.consumer != 'canvas'
                or replace(older, revision=0) != replace(document, revision=0)
                or older.revision <= self._contact_preview_presented_revision or older.revision > document.revision
                or demand.visible != visible or demand.phases != phases
                or tuple(request.address for request in demand.requests) != addresses
                or result.document != older or result.error or result.exact or result.status.value != 'provisional'
                or result.request.quality.value != 'interactive'
                or result.request.key != ('detached-preview', demand.serial)
                or result.request.revision != older.revision
                or tuple(result.request.bounds.getRect()) != visible
                or result.request.pixel_size != demand.preview_size
                or result.request.requested_region is not None or not result.request.clip_document
                or result.request.scale != min(1., max(demand.preview_size[0] / visible[2], demand.preview_size[1] / visible[3]))
                or result.request.output_transform != (demand.preview_size[0] / visible[2], 0., 0.,
                    demand.preview_size[1] / visible[3], -visible[0] * (demand.preview_size[0] / visible[2]),
                    -visible[1] * (demand.preview_size[1] / visible[3]))
                or result.request.smooth_sources or result.request.defer_effects
                or result.request.phase is not None or result.request.target is not None
                or result.image.isNull() or result.image.format() != older.pixel_contract.image_format
                or result.image.devicePixelRatio() != 1.
                or (result.image.width(), result.image.height()) != demand.preview_size):
            return False
        self.release_handoff.clear('new owned contact preview')
        self._preview_snapshot, self._preview_request = demand.snapshot, result.request
        # Store the actual older immutable document. This is never current exact
        # artwork and never enters native tile adoption or disk recording.
        self.preview = older, PresentedTile(('preview', demand.serial), result.image,
            result.request.bounds, pixel_contract=older.pixel_contract,
            pixel_environment=demand.snapshot.pixel_environment)
        self._contact_preview_presented_revision = older.revision
        return True

    def _owned_native_contact_gate(self, document):
        """Current unreleased native input, independent of preparation readiness."""
        canvas = self.canvas
        if (not document.live_preview or not getattr(canvas, '_drawing', False)
                or not getattr(canvas, '_raster_contact_active', False)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or canvas.tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH}
                or canvas._projection_has_live_preview(include_ink=False)):
            return None
        gate = getattr(canvas, '_paint_brush_tile_input' if canvas.tool == ToolKind.BRUSH
                       else '_raster_tile_input', None)
        current = getattr(gate, 'current', None)
        if (gate is None or getattr(gate, 'canvas', None) is not canvas
                or getattr(gate, 'chapter', None) is not canvas.chapter
                or getattr(gate, 'tiles', None) is not canvas.tiles
                or getattr(gate, 'identifier', None) != canvas.selected_id
                or getattr(gate, 'closed', True) or getattr(gate, 'released', True)
                or getattr(gate, 'error', None) is not None
                or not callable(current) or not current()):
            return None
        return gate

    def _bootstrap_key(self, document, visible):
        return replace(document, revision=0, live_preview=False, contact_only=False), tuple(visible)

    def _ensure_bootstrap(self, document, requests, phases, visible):
        owner = self._bootstrap
        if owner is not None and (not owner.valid(document, visible)
                or self._owned_native_contact_gate(document) is None):
            self._retire_bootstrap()
        if (self._bootstrap is not None or self._owned_native_contact_gate(document) is None
                or self._feedback_covers(document, visible)
                or self._feedback_contact_covers(document, visible)
                or self._bootstrap_failed == self._bootstrap_key(document, visible)):
            return
        # The ordinary eligibility guard checks scene dependencies without
        # evaluating artwork. A private capture then keeps the independent
        # planes alive across selected source changes instead of restarting.
        from types import SimpleNamespace
        from comic_editor.render.raster_feedback import feedback_chain
        candidate = SimpleNamespace(document=document, chapter=self.canvas.chapter, tiles=self.canvas.tiles)
        if feedback_chain(candidate, self.feedback_target) is None:
            return
        from comic_editor.render.raster_feedback_bootstrap import RasterFeedbackBootstrap
        try:
            self._bootstrap = RasterFeedbackBootstrap(self.canvas, document, self.feedback_target, visible)
            if self._bootstrap.retired:
                self._bootstrap_failed = self._bootstrap_key(document, visible)
                self._retire_bootstrap()
                return
            self._bootstrap.dirty.update(getattr(self.canvas, '_stroke_before', ()))
            self._bootstrap_serial += 1
            self._bootstrap_originals = self.scheduler.ready_feedback_originals(document.identity)
            self._bootstrap_requests = tuple(requests)
            self._bootstrap_phases = tuple(phases)
            self._bootstrap_dispatched = False
            self.bootstrap_timer.start(0)
        except Exception:
            self._bootstrap_failed = self._bootstrap_key(document, visible)
            self._retire_bootstrap()

    def _bootstrap_contact_gate(self, document, visible):
        if self._bootstrap is not None and self._bootstrap.valid(document, visible):
            return self._owned_native_contact_gate(document)
        return None

    def _retire_bootstrap(self):
        if self._bootstrap is not None:
            self._bootstrap.retire()
        self._bootstrap = None
        originals = getattr(self, '_bootstrap_originals', None)
        if originals is not None:
            originals.cancel()
        self._bootstrap_originals = None
        self._bootstrap_dispatched = False
        self.bootstrap_scheduler.cancel()
        self.bootstrap_timer.stop()

    def _advance_bootstrap(self):
        owner = self._bootstrap
        if owner is None or self.canvas.chapter is None:
            self._retire_bootstrap()
            return
        canvas = self.canvas
        document = canvas._render_document_state()
        visible = canvas.visible_document_rect()
        if document.overflow <= 0:
            visible = visible.intersected(document.bounds)
        visible = tuple(visible.getRect())
        gate = self._owned_native_contact_gate(document)
        if not owner.valid(document, visible) or gate is None:
            self._retire_bootstrap()
            return
        # Native packet admission gets the first turn. Once ready, two-ms
        # metadata slices yield to the event loop without eight-ms idle gaps;
        # worker-only completion polling retains the ordinary eight-ms cadence.
        if gate.busy:
            self.bootstrap_timer.setInterval(8)
            return
        self.bootstrap_timer.setInterval(8 if self._bootstrap_dispatched else 0)
        try:
            captured = owner.advance(document, visible, seconds=.002)
            if owner.retired:
                self._bootstrap_failed = self._bootstrap_key(document, visible)
                self._retire_bootstrap()
                canvas.update()
                return
            if captured and not self._bootstrap_dispatched:
                self.bootstrap_scheduler.submit(SceneDemand(self._bootstrap_serial, owner.snapshot,
                    self._bootstrap_requests, self._bootstrap_phases,
                    ((canvas._raster_contact_point.x(), canvas._raster_contact_point.y())
                        if getattr(canvas, '_raster_contact_point', None) is not None else
                        (visible[0] + visible[2] / 2, visible[1] + visible[3] / 2)), visible,
                    consumer='raster-contact', feedback_target=owner.identifier,
                    feedback_only=True, feedback_originals=self._bootstrap_originals))
                self._bootstrap_dispatched = True
                self.bootstrap_timer.setInterval(8)
            for completion in self.bootstrap_scheduler.poll():
                if completion.demand.serial != self._bootstrap_serial:
                    continue
                if completion.feedback is not None:
                    # The baseline may predate many input packets. Retain the
                    # entire typed source journal, including transparent prune,
                    # so no pre-adoption pixels can overwrite current ink.
                    self._retire_filtered()
                    self.feedback = completion.feedback
                    self.feedback_dirty = set(owner.dirty)
                    self.feedback_dirty.update(getattr(canvas, '_stroke_before', ()))
                    self.feedback_patches.clear()
                    canvas.update()
                    if completion.done:
                        if not self._feedback_covers(document, visible):
                            # A bounded partial view is useful at its contact,
                            # but repeating the same capped preparation on every
                            # packet can never acquire full viewport coverage.
                            self._bootstrap_failed = self._bootstrap_key(document, visible)
                        self._retire_bootstrap()
                        return
                    continue
                if completion.done:
                    self._bootstrap_failed = self._bootstrap_key(document, visible)
                    self._retire_bootstrap()
                    canvas.update()
                    return
        except Exception:
            self._bootstrap_failed = self._bootstrap_key(document, visible)
            self._retire_bootstrap()
            canvas.update()

    def _pending_native_contact_gate(self, document):
        """Give an owned cold input packet priority over obsolete scene work."""
        canvas = self.canvas
        if (not document.live_preview or not getattr(canvas, '_drawing', False)
                or not getattr(canvas, '_raster_contact_active', False)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or canvas.tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH}):
            return None
        gate = getattr(canvas, '_paint_brush_tile_input' if canvas.tool == ToolKind.BRUSH
                       else '_raster_tile_input', None)
        current = getattr(gate, 'current', None)
        if (gate is None or not getattr(gate, 'busy', False)
                or getattr(gate, 'canvas', None) is not canvas
                or getattr(gate, 'chapter', None) is not canvas.chapter
                or getattr(gate, 'tiles', None) is not canvas.tiles
                or getattr(gate, 'identifier', None) != canvas.selected_id
                or getattr(gate, 'closed', True) or getattr(gate, 'released', True)
                or getattr(gate, 'error', None) is not None
                or not callable(current) or not current()):
            return None
        return gate

    def _covered_contact_gate(self, document, visible):
        """Positive ownership gate for already prepared current native ink."""
        canvas = self.canvas
        prepared = self.feedback
        if (not document.live_preview or not getattr(canvas, '_drawing', False)
                or document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images))
                or document.revision != canvas._document_projection.revision
                or not getattr(canvas, '_raster_contact_active', False)
                or canvas.tool not in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER, ToolKind.BRUSH}
                or prepared is None or prepared.identifier != canvas.selected_id
                or not (self._feedback_covers(document, visible)
                    or self._feedback_contact_covers(document, visible))
                or canvas._projection_has_live_preview(include_ink=False)):
            return None
        gate = getattr(canvas, '_paint_brush_tile_input' if canvas.tool == ToolKind.BRUSH
                       else '_raster_tile_input', None)
        current = getattr(gate, 'current', None)
        if (gate is None or getattr(gate, 'canvas', None) is not canvas
                or getattr(gate, 'chapter', None) is not canvas.chapter
                or getattr(gate, 'tiles', None) is not canvas.tiles
                or getattr(gate, 'identifier', None) != prepared.identifier
                or getattr(gate, 'closed', True) or getattr(gate, 'released', True)
                or getattr(gate, 'error', None) is not None
                or not callable(current) or not current()):
            return None
        return gate

    def _hold_contact_feedback(self, signature, gate):
        """Retain ready artwork while native input owns the interaction lane."""
        if self._contact_reuse_gate is not gate:
            self.scheduler.cancel()
            effects = getattr(self.canvas, '_effect_jobs', None)
            if effects is not None:
                effects.cancel(clear_retained=False)
            self._cpu_pending.clear()
        if signature != self.desired:
            self.release_handoff.new_request(signature)
            self.serial += 1
            self.desired = signature
            self.error = ''
            self.canvas._projection_render_error = ''
            self.canvas._projection_error_revision = -1
        self._contact_reuse_gate = gate
        self.capture = self.dispatched = None
        self.timer.stop()

    def _capture_slice_seconds(self, document, visible):
        canvas = self.canvas
        gate = getattr(canvas, '_raster_tile_input', None)
        if (self.feedback is not None and self.feedback.identifier == canvas.selected_id
                and canvas.tool in {ToolKind.RASTER_PENCIL, ToolKind.RASTER_ERASER}
                and getattr(canvas, '_raster_contact_active', False)
                and getattr(canvas, '_drawing', False)
                and (gate is None or not (gate.busy or gate.released))
                and self._feedback_covers(document, visible)):
            # Ready current ink can tolerate a more cooperative metadata lane.
            # Release and cold preparation retain normal exact-work progress.
            return .002
        return .004

    def advance(self):
        canvas = self.canvas
        if self.desired is None or canvas.chapter is None:
            # A queued timer can arrive after contact release retires its
            # demand. This is idle scheduling, not a document replacement:
            # newest displayed source pixels still await an exact scene.
            self.reset(retain_presentation=canvas.chapter is not None)
            return
        document, addresses, phases, visible = self.desired
        if self._filtered_state is not None and not self._filtered_state.advance(document,capture=False):
            self.retire_feedback()
        gate = (self._pending_native_contact_gate(document)
            or self._bootstrap_contact_gate(document, visible)
            or self._covered_contact_gate(document, visible))
        if gate is not None:
            if gate.busy and self._preserve_owned_contact_work(self.desired, busy_gate=gate):
                self.timer.start(8)
                return
            self._hold_contact_feedback(self.desired, gate)
            return
        resume_contact = self._contact_reuse_gate is not None
        self._contact_reuse_gate = None
        if (document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images)) or
                document.revision != canvas._document_projection.revision):
            # Retain a privately frozen capture until a real current paint
            # revalidates its owner. Do not advance/adopt it against stale desired
            # metadata. Existing active-job cancellation remains unchanged.
            preserve = self._preserve_owned_contact_work(self.desired)
            if not preserve:
                if not isinstance(self.capture, OrdinaryContactCapture):
                    self.capture = None
                self.dispatched = None
                self.scheduler.cancel()
            self.timer.stop()
            canvas.update()
            return
        if resume_contact:
            self.dispatched = None
            if self.snapshot is None or self.snapshot.document != document:
                self.capture = self._new_scene_capture(document)
        if (self._contact_preview_owner is not None
                and self._contact_preview_key_for(document, addresses, phases, visible) != self._contact_preview_key):
            self._retire_contact_preview()
            self.scheduler.cancel()
        for completion in self.scheduler.poll():
            if completion.demand.contact_preview_owner is not None:
                if self._accept_contact_preview(completion, document, addresses, phases, visible):
                    canvas.update()
                    continue
                # Only current errors retain ordinary reporting. A retired or
                # non-preview result cannot enter current exact adoption.
                if not completion.error:
                    continue
            if completion.demand.serial != self.serial or completion.demand.snapshot.document != document:
                continue
            if completion.error:
                self.error = completion.error
                canvas._projection_render_error = completion.error
                canvas._projection_error_revision = document.revision
            self._cpu_pending.difference_update(completion.transfer_keys)
            if completion.materialized is not None:
                self._accept_cpu_images(completion.materialized)
            if completion.preview is not None:
                self.release_handoff.clear("new current preview")
                result = completion.preview
                self._preview_snapshot = completion.demand.snapshot
                self._preview_request = result.request
                self.preview = document, PresentedTile(("preview", self.serial), result.image,
                    result.request.bounds, pixel_contract=document.pixel_contract,
                    pixel_environment=completion.demand.snapshot.pixel_environment)
            if completion.overview is not None:
                result = completion.overview
                # Already converted on the detached display edge. Preserve its
                # document/revision ownership without another color conversion.
                self.overview = document, PresentedTile(("overview", self.serial), result.image,
                    QRectF(*result.bounds))
            if completion.feedback is not None and completion.feedback.identifier == self.feedback_target:
                self._retire_filtered()
                self.feedback = completion.feedback
                self.feedback_dirty.clear()
                self.feedback_patches.clear()
                from comic_editor.render.filtered_raster_feedback import FilteredRasterFeedback
                if isinstance(self.feedback,FilteredRasterFeedback):
                    from comic_editor.ui.filtered_raster_feedback import FilteredRasterFeedbackState
                    self._filtered_state = FilteredRasterFeedbackState(canvas,self.feedback,self.filteredFinished.emit)
                    self._filtered_lifetime['state'] = self._filtered_state
            for address, (image, exact) in (completion.tiles or {}).items():
                if exact:
                    request = ProjectionRequest(address, canvas._document_projection.tile_size,
                                                canvas._document_projection.gutter)
                    canvas._document_projection.adopt(request, image,
                        configuration=(*document.configuration, completion.phase),
                        document=document.identity, revision=document.revision,
                        pixel_environment=completion.demand.snapshot.pixel_environment)
            canvas.update()
        if (self.capture is None and isinstance(self.snapshot, ContactSceneSnapshot)
                and self.snapshot.document != document
                and self._preserve_owned_contact_work(self.desired)):
            # Poll first so a completed worker releases admission. Until the
            # next copy is admitted, never redispatch the same older snapshot.
            self.capture = self._new_scene_capture(document)
            if self.capture is None:
                self.timer.start(8)
                return
        if self.capture is not None:
            try:
                captured = self.capture.advance(self._capture_slice_seconds(document, visible))
            except Exception as error:
                self.error = f'{type(error).__name__}: {error}'
                canvas._projection_render_error = self.error
                canvas._projection_error_revision = document.revision
                self.capture = None
                self.dispatched = self.desired
                self.timer.stop()
                canvas.update()
                return
            if not captured:
                self.timer.start(8)
                return
            if self.capture.stale:
                self.capture = None
                self.timer.stop()
                canvas.update()
                return
            completed_capture = self.capture
            self.snapshot = completed_capture.result
            self.capture = None
            if isinstance(completed_capture, OrdinaryContactCapture):
                completed_capture.retire()  # External snapshot keeps its copy lease.
        owned_snapshot = self._current_contact_snapshot(self.snapshot, document, addresses, phases, visible)
        current_snapshot = (self.snapshot is not None and self.snapshot.document == document
            and not isinstance(self.snapshot, ContactSceneSnapshot))
        if self.dispatched != self.desired and (current_snapshot or owned_snapshot):
            requests = tuple(ProjectionRequest(address, canvas._document_projection.tile_size,
                             canvas._document_projection.gutter) for address in addresses)
            # Live ownership covers the full visible native request set even
            # when ordinary exact tiles survived undo or selection changes.
            feedback_requests = requests
            if not self.preview_mode:
                requests = tuple(request for request in requests if any(
                    not canvas._document_projection.ready([request], configuration=(*document.configuration, phase))
                    for phase in phases))
            feedback_target = self.feedback_target if not self._feedback_covers(document, visible) else ''
            if requests or self.preview_mode or feedback_target:
                preview_size = None
                if self.preview_mode:
                    scale = min(1., 1024. / max(1., visible[2], visible[3]))
                    preview_size = max(1, math.ceil(visible[2] * scale)), max(1, math.ceil(visible[3] * scale))
                presentation_size = None
                if self.preview_mode and not document.live_preview:
                    density = min(1., canvas.scale * canvas.devicePixelRatioF(),
                        math.sqrt(max(1, canvas._document_projection.budget // 4) /
                                  max(1., visible[2] * visible[3])))
                    presentation_size = max(1, math.floor(visible[2] * density)), max(1, math.floor(visible[3] * density))
                demand = SceneDemand(self.serial, self.snapshot, requests, phases,
                    (visible[0] + visible[2] / 2, visible[1] + visible[3] / 2), visible, preview_size,
                    presentation_size=presentation_size, feedback_target=feedback_target,
                    feedback_requests=feedback_requests if feedback_target else None,
                    contact_preview_owner=self._contact_preview_owner)
                copies = getattr(getattr(demand.snapshot, 'capture_lease', None), 'lease', None)
                if (owned_snapshot and self.scheduler._contact_preview_bounded(demand)
                        and demand.contact_preview_owner == self._contact_preview_owner
                        and isinstance(copies, CopyLease) and copies.admission is RENDER_ADMISSION
                        and not copies.released and type(copies.size) is int and copies.size > 0):
                    # This bounded owned LIVE request renders only the canonical
                    # full preview. The scheduler never prepares feedback for
                    # LIVE documents, so it needs no filtered-feedback reserve.
                    # Source/native workspace and retained lease charges remain.
                    demand = replace(demand, feedback_target='', feedback_requests=None)
                if self._contact_preview_owner is not None:
                    self.scheduler.submit(demand, coalesce_active=True)
                else:
                    self.scheduler.submit(demand)
            self.dispatched = (self.snapshot.document, addresses, phases, visible) if owned_snapshot else self.desired
            if owned_snapshot and self.snapshot.document != document:
                # Explicit held catch-up also runs after the last input, without
                # relying on another signature change. This next full R1 capture
                # prevents redispatch of the same older R0 on each timer tick.
                self.capture = self._new_scene_capture(document)
        if not self.scheduler.busy and self.capture is None:
            self.timer.stop()
        else:
            self.timer.start(8)

    def ready_batch(self, document, requests, phases):
        projection = self.canvas._document_projection
        batch = []
        complete = True
        for phase in phases:
            tiles = projection.ready(requests, configuration=(*document.configuration, phase))
            complete = complete and len(tiles) == len(requests)
            batch.append((phase, [PresentedTile((phase, tile.request.address), tile.image,
                tile.request.world_rect, tile.request.source_rect, document.pixel_contract,
                tile.pixel_environment) for tile in tiles]))
        return batch, complete

    def changed(self, change, *, action="transient"):
        self.raster_continuity.changed(change, action, self.canvas._render_document_state())
        self.release_handoff.changed(change, action)
        if self._contact_preview_owner is not None:
            from comic_editor.render.raster_feedback_bootstrap import selected_source_change
            if action != 'transient' or not selected_source_change(change, self.canvas.selected_id):
                self._contact_preview_dependency_generation += 1
                self._retire_contact_preview()
                self.scheduler.cancel()
                self.capture = self.dispatched = None
                self.desired = None
        """Use the ordinary typed invalidation stream to guard prepared passes."""
        if self._bootstrap is not None and not self._bootstrap.changed(change):
            self._retire_bootstrap()
            self._bootstrap_failed = None
        prepared = self.feedback
        if prepared is None:
            return
        selected = ('object', prepared.identifier)
        if (change.conservative or change.document_fields or change.orders
                or any(item.affects_artwork and (item.entity != selected
                    or item.structural or item.fields - {'pixels', 'interaction_rect'})
                    for item in change.entities)
                or any(item.entity != selected or item.resource != 'raster' for item in change.resources)):
            self.retire_feedback()
            return
        if self._filtered_state is not None:
            # Eligible history restores use the same typed native source
            # packets as forward pixel edits. All dependency, geometry and
            # native-frame failures still retire through the guards below.
            from comic_editor.render.filtered_raster_feedback import FeedbackUnavailable
            try:
                self._filtered_state.changed(change)
                if action in {'undo','redo'} and change.resources:
                    # Typed publication precedes ordinary projection
                    # invalidation. Exclude every pre-restore frame.
                    state = self._filtered_state
                    state.base_revision = max(state.base_revision,
                        self.canvas._document_projection.revision + 1)
            except FeedbackUnavailable:
                self.retire_feedback()
                return
        self.feedback_dirty.update(item.address for item in change.resources if item.address is not None)

    def _retire_filtered(self):
        if self._filtered_state is not None:
            self._filtered_state.close()
        self._filtered_state = None
        self._filtered_lifetime['state'] = None
        self._filtered_requests = ()

    def retire_feedback(self):
        self._retire_contact_preview()
        self._retire_bootstrap()
        self._bootstrap_failed = None
        self._retire_filtered()
        self.feedback = None
        self.feedback_dirty.clear()
        self.feedback_patches.clear()

    def _feedback_covers(self, document, visible):
        prepared = self.feedback
        if (prepared is None or prepared.identifier != self.feedback_target
                or prepared.document.configuration != document.configuration):
            return False
        if self._filtered_state is not None:
            return self._filtered_state.covers(document,self._filtered_requests,
                getattr(self.canvas,'_projection_completed_view',None))
        # ``changed`` already validates every scene dependency through the
        # ordinary typed invalidation stream. Selected native pixel changes
        # only replace source patches; rebuilding all three scene planes on
        # each such revision would delay exact convergence needlessly.
        from comic_editor.render.raster_feedback import feedback_keys
        extent = QRectF(*visible).intersected(document.bounds)
        side = prepared.world_size
        columns = math.ceil((extent.right() - prepared.origin[0]) / side) - math.floor((extent.left() - prepared.origin[0]) / side)
        rows = math.ceil((extent.bottom() - prepared.origin[1]) / side) - math.floor((extent.top() - prepared.origin[1]) / side)
        if not extent.isEmpty() and columns * rows > len(prepared.tiles):
            return False
        needed = feedback_keys(document, visible, prepared.origin,
                               side, prepared.gutter, budget=None)
        return set(needed).issubset(tile.key for tile in prepared.tiles)

    def _feedback_contact_covers(self, document, visible):
        """A bounded native basis may cover all edited pixels in a wider view."""
        prepared = self.feedback
        if (self._filtered_state is not None or prepared is None
                or prepared.document.configuration != document.configuration
                or prepared.document.identity != document.identity
                or prepared.document.pixel_contract != document.pixel_contract
                or prepared.document.bounds != document.bounds):
            return False
        from comic_editor.render.raster_feedback_coverage import feedback_contact_covers
        canvas = self.canvas
        return feedback_contact_covers(prepared, document, visible, self.feedback_dirty,
            source_owner=canvas.tiles._tiles.get(prepared.identifier),
            presentation_margin=max(prepared.gutter,
                1. / max(.001, abs(canvas.scale) * canvas.devicePixelRatioF())))

    def present_feedback(self, painter, document):
        if self._filtered_state is not None:
            return 0  # Complete native scenes replace the normal batch below.
        prepared = self.feedback
        if (prepared is None or not self.feedback_dirty
                or prepared.identifier != self.canvas.selected_id
                or prepared.document.configuration != document.configuration
                or not self.canvas._projection_frame_pending
                    and not getattr(self.canvas, '_raster_contact_active', False)):
            return 0
        from comic_editor.ui.raster_feedback import present_raster_feedback
        return present_raster_feedback(self.canvas, painter, prepared, self.feedback_dirty, self.feedback_patches)

    def checkpoint_raster_presentation(self, document):
        """Keep committed native ink before a selection retires its source owner."""
        if (self._presentation_batch is None or self.feedback is None
                or self._filtered_state is not None or not self.feedback_dirty
                or self.feedback.document.identity != document.identity
                or self.feedback.document.configuration != document.configuration):
            return
        old_document, batch, view = self._presentation_batch
        if (old_document.configuration != document.configuration or old_document.identity != document.identity
                or view != self.presentation_view()):
            return
        from comic_editor.ui.raster_feedback import _raster_feedback_tiles
        from comic_editor.ui.canvas import RasterCanvasWidget
        camera = self.canvas.camera_transform()
        replace_native = (isinstance(self.canvas, RasterCanvasWidget) and camera.isAffine()
            and camera.m11() > 0. and camera.m22() > 0. and camera.m12() == 0. and camera.m21() == 0.)
        tiles = _raster_feedback_tiles(self.canvas, self.feedback, self.feedback_dirty,
            self.feedback_patches, replace_native, complete=True, prediction=False)
        if tiles:
            self.raster_continuity.remember(document, batch, tiles, replace_background=replace_native, view=view)

    def presentation_view(self):
        camera = self.canvas.camera_transform()
        return ((camera.m11(), camera.m12(), camera.m13(), camera.m21(), camera.m22(), camera.m23(),
            camera.m31(), camera.m32(), camera.m33()), self.canvas.width(), self.canvas.height(),
            self.canvas.devicePixelRatioF())

    def filtered_batch(self,document,requests,batch,*,complete=False):
        state = self._filtered_state
        if state is None:
            return batch,0
        if complete and not document.live_preview:
            state.checkpoint(document.revision)
            return batch,0
        base = getattr(self.canvas,'_projection_completed_view',None)
        supplied = batch
        if not state.native_batch(batch):
            # An owned contact can resume after a viewport preview. Replace
            # complete native tiles from its validated basis, never the
            # preview's unrelated serial key or sampling rectangle.
            if self._contact_reuse_gate is None or not state.covers(document,requests,base):
                return batch,0
            batch = base[1]
        batch,count = state.replace_batch(document,requests,base,batch)
        if not count:
            return supplied,0
        if count and state.completed_revision == document.revision and not state.generations:
            point = getattr(self.canvas,'_raster_contact_point',None)
            gate = getattr(self.canvas,'_paint_brush_tile_input' if self.canvas.tool == ToolKind.BRUSH
                else '_raster_tile_input',None)
            if (point is not None and not (gate is not None and gate.busy)
                    and any(recipe.request.requested.contains(point) for recipe in state.prepared.recipes)):
                self.canvas._raster_feedback_contact_covered = True
        return batch,count

    def ensure_cpu_tiles(self,tiles):
        from comic_editor.render.device import DeviceImage
        if self.snapshot is None or self.desired is None:
            return
        images = {tile.image.cacheKey():tile.image for tile in tiles
            if isinstance(tile.image,DeviceImage) and not tile.image.isNull()
            and tile.image.cacheKey() not in self._cpu_pending}
        if not images:
            return
        self._cpu_pending.update(images)
        self.scheduler.materialize(SceneDemand(self.serial,self.snapshot,(),(),(0.,0.)),images)
        self.timer.start(8)

    def _accept_cpu_images(self,images):
        if self.release_handoff.ticket is not None:
            self.release_handoff.clear("materialization/storage transition")
        projection = self.canvas._document_projection
        for tiles in projection._configurations.values():
            for tile in tiles.values():
                value = images.get(tile.image.cacheKey())
                if value is not None:
                    projection.bytes += value.sizeInBytes()-tile.image.sizeInBytes()
                    tile.image = value
        def converted(tile):
            image = images.get(tile.image.cacheKey())
            return replace(tile,image=image) if image is not None else tile
        for attribute in ('_projection_completed_view','_projection_progress_view'):
            previous = getattr(self.canvas,attribute,None)
            if previous is not None:
                setattr(self.canvas,attribute,(previous[0],[(phase,[converted(tile) for tile in tiles])
                    for phase,tiles in previous[1]],previous[2]))
        if self.preview is not None:
            self.preview = self.preview[0],converted(self.preview[1])
