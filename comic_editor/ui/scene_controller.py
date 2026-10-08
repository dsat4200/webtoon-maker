"""GUI adapter: request detached work and present only completed resources."""
from PySide6.QtCore import QObject, QRectF, QTimer
from dataclasses import replace
import math

from comic_editor.core.tools import ToolKind
from comic_editor.render.projection import ProjectionRequest
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.ui.document_presentation import PresentedTile
from comic_editor.ui.raster_feedback import RasterFeedbackPatchCache


class SceneController(QObject):
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
        self.feedback = None
        self.feedback_dirty = set()
        self.feedback_patches = RasterFeedbackPatchCache()
        self._cpu_pending = set()
        self.timer = QTimer(self)
        self.timer.setInterval(8)
        self.timer.timeout.connect(self.advance)
        scheduler = self.scheduler
        self.destroyed.connect(lambda: scheduler.close())

    def reset(self):
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
            self.feedback_target = target
            self.feedback = None
            self.feedback_dirty.clear()
            self.feedback_patches.clear()
            self.dispatched = None
        graphics = getattr(self.canvas,'_graphics_worker',None)
        if self.snapshot is not None and self.snapshot.graphics_worker is not graphics:
            self.snapshot = self.capture = self.dispatched = None
            self.desired = None
            self._cpu_pending.clear()
        bytes_per_pixel = 16 if document.pixel_contract.floating else 4
        view_bytes = len(requests) * (self.canvas._document_projection.tile_size +
                     2 * self.canvas._document_projection.gutter) ** 2 * bytes_per_pixel * len(phases)
        self.preview_mode = (not visible.isEmpty() and
            (document.live_preview or view_bytes > self.canvas._document_projection.budget // 2))
        signature = document, tuple(request.address for request in requests), tuple(phases), tuple(visible.getRect())
        gate = self._covered_contact_gate(document, signature[3])
        if gate is not None:
            self._hold_contact_feedback(signature, gate)
            return
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
            self.scheduler.cancel()
            self._cpu_pending.clear()
            self.error = ""
            self.canvas._projection_render_error = ""
            self.canvas._projection_error_revision = -1
            if self.snapshot is None or self.snapshot.document != document:
                self.capture = self.canvas._scene_snapshot_compiler.capture(self.canvas, document)
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
                or not self._feedback_covers(document, visible)
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
        """Retain immutable planes while native source packets are presented."""
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
            self.reset()
            return
        document, addresses, phases, visible = self.desired
        gate = self._covered_contact_gate(document, visible)
        if gate is not None:
            self._hold_contact_feedback(self.desired, gate)
            return
        resume_contact = self._contact_reuse_gate is not None
        self._contact_reuse_gate = None
        if (document.identity != (id(canvas.chapter), id(canvas.tiles), id(canvas.images)) or
                document.revision != canvas._document_projection.revision):
            self.capture = None
            self.dispatched = None
            self.scheduler.cancel()
            self.timer.stop()
            canvas.update()
            return
        if resume_contact:
            self.dispatched = None
            if self.snapshot is None or self.snapshot.document != document:
                self.capture = canvas._scene_snapshot_compiler.capture(canvas, document)
        for completion in self.scheduler.poll():
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
                self.feedback = completion.feedback
                self.feedback_dirty.clear()
                self.feedback_patches.clear()
            for address, (image, exact) in (completion.tiles or {}).items():
                if exact:
                    request = ProjectionRequest(address, canvas._document_projection.tile_size,
                                                canvas._document_projection.gutter)
                    canvas._document_projection.adopt(request, image,
                        configuration=(*document.configuration, completion.phase),
                        document=document.identity, revision=document.revision,
                        pixel_environment=completion.demand.snapshot.pixel_environment)
            canvas.update()
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
            self.snapshot = self.capture.result
            self.capture = None
        if self.dispatched != self.desired and self.snapshot is not None and self.snapshot.document == document:
            requests = tuple(ProjectionRequest(address, canvas._document_projection.tile_size,
                             canvas._document_projection.gutter) for address in addresses)
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
                self.scheduler.submit(SceneDemand(self.serial, self.snapshot, requests, phases,
                    (visible[0] + visible[2] / 2, visible[1] + visible[3] / 2), visible, preview_size,
                    presentation_size=presentation_size, feedback_target=feedback_target))
            self.dispatched = self.desired
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
        self.release_handoff.changed(change, action)
        """Use the ordinary typed invalidation stream to guard prepared passes."""
        prepared = self.feedback
        if prepared is None:
            return
        selected = ('object', prepared.identifier)
        if (change.conservative or change.document_fields or change.orders
                or any(item.affects_artwork and (item.entity != selected
                    or item.structural or item.fields - {'pixels', 'interaction_rect'})
                    for item in change.entities)
                or any(item.entity != selected or item.resource != 'raster' for item in change.resources)):
            self.feedback = None
            self.feedback_dirty.clear()
            self.feedback_patches.clear()
            return
        self.feedback_dirty.update(item.address for item in change.resources if item.address is not None)

    def retire_feedback(self):
        self.feedback = None
        self.feedback_dirty.clear()
        self.feedback_patches.clear()

    def _feedback_covers(self, document, visible):
        prepared = self.feedback
        if (prepared is None or prepared.identifier != self.feedback_target
                or prepared.document.configuration != document.configuration):
            return False
        # ``changed`` already validates every scene dependency through the
        # ordinary typed invalidation stream. Selected native pixel changes
        # only replace source patches; rebuilding all three scene planes on
        # each such revision would delay exact convergence needlessly.
        from comic_editor.render.raster_feedback import feedback_keys
        needed = feedback_keys(document, visible, prepared.origin,
                               prepared.tile_size, prepared.gutter)
        return set(needed).issubset(tile.key for tile in prepared.tiles)

    def present_feedback(self, painter, document):
        prepared = self.feedback
        if (prepared is None or not self.feedback_dirty
                or prepared.identifier != self.canvas.selected_id
                or prepared.document.configuration != document.configuration
                or not self.canvas._projection_frame_pending
                    and not getattr(self.canvas, '_raster_contact_active', False)):
            return 0
        from comic_editor.ui.raster_feedback import present_raster_feedback
        return present_raster_feedback(self.canvas, painter, prepared, self.feedback_dirty, self.feedback_patches)

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
