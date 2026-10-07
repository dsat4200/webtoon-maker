"""Bridge the editor's faithful document renderer to retained presentation."""
import math
import time
from contextlib import nullcontext

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from comic_editor.ui.document_presentation import (
    PresentedTile, PresentationStats, draw_document_tiles, draw_document_border,
)
from comic_editor.render.service import (
    RenderDocument, RenderRequest, RenderResult, RenderQuality, RenderStatus, TileBatchPolicy,
)
from comic_editor.core.models import RasterObject
from comic_editor.render.sampling import artwork_density
from comic_editor.ui.color_resources import projection_color_identity


class DocumentProjectionFeatures:
    _document_projection_enabled = False
    _projection_async_enabled = True
    _projection_cull_outside_view = True

    def _uses_document_projection(self):
        return bool(self._document_projection_enabled and self.chapter is not None)

    def _effect_result_ready(self, scope, key):
        # Incomplete tiles were never marked valid. Only they need another
        # capture; completed neighbors must survive an unrelated worker result.
        self._invalidate_scene_cache(projection=False)
        self._projection_work_waiting = False

    def _projection_configuration(self):
        pixels = ("pixel-environment", self.chapter.pixel_contract.signature,
                  projection_color_identity(self, self.chapter.pixel_contract))
        mask_entities = tuple(sorted(
            [("layer", identifier) for identifier, entity in self.chapter.layers.items()
             if entity.mask_only] +
            [("object", identifier) for identifier, entity in self.chapter.objects.items()
             if entity.mask_only]))
        if getattr(self, "_disk_cache_capture", False):
            return (id(self.chapter), id(self.tiles), id(self.images),
                    self.chapter.width, self.chapter.height, self.chapter.background,
                    self.chapter.view_overflow, ("", 0.), (), None, mask_entities, pixels)
        previous = self._live_underlay_object_id, self._live_underlay_amount
        try:
            self._set_live_underlay_context()
            underlay = self._live_underlay_object_id, self._live_underlay_amount
        finally:
            self._live_underlay_object_id, self._live_underlay_amount = previous
        selected = (self.chapter.objects if self.selected_kind == "object"
                    else self.chapter.layers).get(self.selected_id)
        mask_only = ((self.selected_kind, self.selected_id)
                     if selected is not None and selected.mask_only else None)
        return (id(self.chapter), id(self.tiles), id(self.images),
                self.chapter.width, self.chapter.height, self.chapter.background,
                self.chapter.view_overflow, underlay, tuple(sorted(self._solo_entities)), mask_only,
                mask_entities, pixels)

    def _invalidate_selection_scene_cache(self):
        # Selection/tool UI is drawn after document presentation. Underlay and
        # selected mask-only visibility have their own retained configuration.
        # A canceled live preview can change artwork without a model signal;
        # conservatively retire such captures before returning to ordinary UI.
        preview = getattr(self, "_projection_captured_live_preview", False)
        self._invalidate_scene_cache(projection=preview)
        self._projection_captured_live_preview = False

    def _projection_has_live_preview(self, *, include_ink=True):
        return (include_ink and bool(getattr(self, "_drawing", False))) or any(bool(getattr(self, name, None)) for name in (
            "_transform_preview_quad", "_multi_transform_preview_quads",
            "_selection_raster_states", "_selection_vector_preview", "_selection_shape_nodes",
            "_vector_gesture_mode", "_cage_session", "_text_editing", "_text_placement",
            "_gradient_preview_active", "_render_excluded_object_id", "_page_gap_draft",
            "_fill_gesture_active", "_text_property_drag", "_shape_property_drag",
            "_raster_paste_overlay", "_overlay_color_preview",
            "_modifier_handle_drag", "_mesh_warp_parameter_drag_id", "_smudge_parameter_drag_id",
        )) or getattr(self, "_selection_before_tiles", None) is not None

    def _paint_ready_document_projection(self, painter, *, live_ink=False):
        """Paint is presentation only; capture/evaluation run on separate lanes."""
        configuration = self._projection_configuration()
        projection = self._document_projection
        document = self._render_document_state()
        visible = self.visible_document_rect()
        if document.overflow <= 0:
            visible = visible.intersected(document.bounds)
        if getattr(self, '_projection_windows_configuration', None) != configuration:
            self._projection_windows = {}
            self._projection_windows_configuration = configuration
        coverage = self._projection_windows.get(0)
        if coverage is None or not coverage.contains(visible):
            coverage = visible.adjusted(-projection.tile_size, -projection.tile_size,
                                        projection.tile_size, projection.tile_size)
            self._projection_windows[0] = QRectF(coverage)
        if document.overflow <= 0:
            coverage = coverage.intersected(document.bounds)
        requests = projection.requests(coverage, 1.)
        if self._projection_cull_outside_view:
            requests = self._projection_visible_requests(requests, projection.tile_size)
        phases = (None,)
        if (live_ink and not self._is_show_on_top(self.selected_kind, self.selected_id)
                and not any(obj.blend_mode != 'normal' for obj in self.chapter.objects.values())
                and self._show_on_top_plan().entries):
            phases = ('base', 'top')
        projection.configure((*configuration, phases[0]), document=configuration[:3])
        self._scene_controller.request(document, requests, phases, visible)
        batch, complete = self._scene_controller.ready_batch(document, requests, phases)
        previous = getattr(self, "_projection_completed_view", None)
        if previous is not None and previous[0] != configuration:
            previous = None
        preview = self._scene_controller.preview
        self._projection_provisional_visible = False
        if self._scene_controller.preview_mode:
            overview = self._scene_controller.overview
            complete = (overview is not None and overview[0] == document
                        and overview[1].world_rect == visible)
            if complete:
                batch = [(None, [overview[1]])]
                self._projection_completed_view = configuration, batch, document.revision
                self._projection_presented_revision = document.revision
            elif preview is not None and preview[0].configuration == configuration:
                batch = [(None, [preview[1]])]
                self._projection_provisional_visible = True
            else:
                batch = previous[1] if previous is not None else []
        elif complete:
            storage = {tile.image.cacheKey(): tile.image.sizeInBytes() for _, tiles in batch for tile in tiles}
            if sum(storage.values()) <= projection.budget:
                self._projection_completed_view = configuration, batch, document.revision
            self._projection_presented_revision = document.revision
        elif previous is not None and previous[2] != document.revision:
            # An edited view stays coherent until every requested replacement is
            # ready. Unchanged artwork can be progressively exposed on a pan.
            batch = previous[1]
        self._projection_frame_pending = not complete
        # Keep the tool's release/cancel bookkeeping aligned with the actual
        # ready preview, without invoking its old inline viewport renderer.
        mesh = self._mesh_warp_preview_modifier()
        self._mesh_warp_preview_presented = bool(mesh is not None and
            self._projection_provisional_visible and preview is not None and preview[0] == document)
        if self._mesh_warp_preview_presented:
            self._mesh_warp_preview_session_id = mesh.modifier_id
        stats = []
        feedback_tiles = 0
        self._raster_feedback_contact_covered = False
        draw_live = (live_ink and not self._projection_provisional_visible
            and tuple(phase for phase, _ in batch) == phases)
        with self._show_on_top_scene() if draw_live else nullcontext():
            for phase, tiles in batch:
                stats.append(draw_document_tiles(painter, tiles, self.camera_transform(), self.size(),
                    owner=self, smooth=True))
                if phase is None:
                    feedback_tiles = self._scene_controller.present_feedback(painter, document)
                if draw_live:
                    self._show_on_top_phase = phase
                    painter.save()
                    try:
                        painter.setTransform(self.camera_transform())
                        painter.setClipRect(document.bounds, Qt.IntersectClip)
                        self._set_live_underlay_context()
                        if not feedback_tiles:
                            self._draw_predictive_ink(painter)
                        self._draw_live_vector_gesture(painter)
                    finally:
                        self._clear_live_underlay_context()
                        painter.restore()
        if not feedback_tiles:
            feedback_tiles = self._scene_controller.present_feedback(painter, document)
        self._document_presentation_stats = PresentationStats(
            stats[-1].backend if stats else "pending", sum(item.tiles for item in stats),
            sum(item.uploads for item in stats), stats[-1].texture_bytes if stats else 0)
        if feedback_tiles:
            self._projection_provisional_visible = True
        projection._trim({request.address for request in requests})
        self._paint_projection_grid(painter, self)
        draw_document_border(painter, document.bounds, self.camera_transform(), self.size(), owner=self)
        from comic_editor.ui.raster_feedback import present_pending_raster_gesture
        present_pending_raster_gesture(self, painter, document)

    def _collect_document_projection(self, phase=None):
        projection = self._document_projection
        view_configuration = self._projection_configuration()
        # Base/top alternate within one frame. Their guard windows describe
        # the same camera coverage and must not reset on every phase switch.
        if getattr(self, "_projection_windows_configuration", None) != view_configuration:
            self._projection_windows = {}
            self._projection_windows_configuration = view_configuration
        configuration = (*view_configuration, phase)
        self._render_service.configure(configuration, document=configuration[:3])
        visible = self.visible_document_rect()
        # A guard band warms the corners exposed by canvas rotation and the
        # next small pan. Its size is tied to the stable tile grid, not an
        # ever-changing capture rectangle.
        density = 1.0
        level = projection.resolution_level(density)
        margin = projection.tile_size if level == 0 else 0.
        windows = self._projection_windows
        window = windows.get(level)
        if window is None or not window.contains(visible):
            window = visible.adjusted(-margin, -margin, margin, margin)
            windows[level] = QRectF(window)
        visible = QRectF(window)
        if self.chapter.view_overflow <= 0:
            visible = visible.intersected(QRectF(0, 0, self.chapter.width, self.chapter.height))
        requests = projection.requests(visible, density)
        if self._projection_cull_outside_view:
            requests = self._projection_visible_requests(requests, margin)
        previous_phase = getattr(self, "_projection_capture_phase", None)
        self._projection_capture_phase = phase
        try:
            tiles = self._render_service.collect(requests, self._render_document_tile,
                                       render_many=self._render_document_tiles)
        finally:
            self._projection_capture_phase = previous_phase
        self._projection_collection_complete = (
            len(tiles) == len(requests) and all(tile.valid for tile in tiles))
        return [PresentedTile((phase, tile.request.address), tile.image,
                              tile.request.world_rect, tile.request.source_rect)
                for tile in tiles if tile.valid]

    def _projection_visible_requests(self, requests, margin):
        # A tilted viewport's bounding box can cover thousands of document
        # pixels which cannot contribute to the screen. Cull only final output
        # tiles; effect sources and the fixed capture devices stay unchanged.
        inverse, valid = self.camera_transform().inverted()
        if not valid:
            return requests
        padding = margin * abs(self.scale) + 2. / max(1., self.devicePixelRatioF())
        viewport = QPainterPath()
        viewport.addRect(QRectF(self.rect()).adjusted(-padding, -padding, padding, padding))
        coverage = inverse.map(viewport)
        return [request for request in requests if coverage.intersects(request.capture_rect)]

    def _render_document_tile(self, request):
        return self._render_document_region(request.capture_rect, request.scale,
            QSize(request.pixel_size, request.pixel_size),
            (request.address.level, request.address.x, request.address.y),
            exact=True)

    def _render_document_state(self):
        configuration = self._projection_configuration()
        return RenderDocument(configuration[:3], configuration,
            self._document_projection.revision, self.chapter.width, self.chapter.height,
            self.chapter.background, self.chapter.view_overflow, configuration[7],
            self._projection_has_live_preview(), self.chapter.pixel_contract)

    def _render_document_tiles(self, requests):
        document = self._render_document_state()
        phase = getattr(self, "_projection_capture_phase", None)
        deferred = getattr(self, "_projection_defer_effects", False)
        center = self.visible_document_rect().center()
        policy = TileBatchPolicy((center.x(), center.y()),
            getattr(self, "_projection_render_deadline", None),
            getattr(self, "_projection_blocks_started", 0))

        def capture(request):
            # Keep this entry point visible to existing performance instrumentation.
            result = self._render_document_region(request.bounds, request.scale,
                QSize(*request.pixel_size), request.key,
                exact=request.quality is RenderQuality.EXACT, requested=request.requested,
                render_document=document, render_request=request)
            if isinstance(result, RenderResult):
                return result
            # Compatibility for external instrumentation supplying image tuples.
            image, exact = result
            return RenderResult(request, document, image,
                RenderStatus.EXACT if exact else RenderStatus.PROVISIONAL)

        batch = self._render_service.render_tiles(document, requests, policy,
            phase=phase, defer_effects=deferred, capture=capture)
        self._projection_blocks_started = batch.blocks_started
        self._projection_yielded = batch.yielded
        return batch.tiles

    def _render_document_region(self, rect, scale, size, key, *, exact=False, requested=None,
                                render_document=None, render_request=None):
        document = render_document or self._render_document_state()
        request = render_request or RenderRequest(tuple(rect.getRect()), scale, (size.width(), size.height()),
            key, document.revision, tuple(requested.getRect()) if requested is not None else None,
            getattr(self, "_projection_capture_phase", None),
            RenderQuality.EXACT if exact else RenderQuality.INTERACTIVE,
            getattr(self, "_projection_defer_effects", False))
        result = self._render_service.render_region(document, request)
        if result.status is RenderStatus.PENDING:
            self._projection_work_waiting = True
        elif result.status is RenderStatus.FAILED:
            self._projection_render_error = result.error
            self._projection_error_revision = result.document.revision
        return result if render_request is not None else (result.image, result.exact)

    def _projection_phase_batch(self, phases):
        configuration = self._projection_configuration()
        previous = getattr(self, "_projection_completed_view", None)
        if previous is not None and previous[0] != configuration:
            previous = self._projection_completed_view = None
        revision = self._document_projection.revision
        progress = getattr(self, "_projection_progress_view", None)
        if progress is not None and (progress[0] != configuration or progress[2] != revision):
            progress = self._projection_progress_view = None
        deferred = getattr(self, "_projection_defer_effects", False)
        jobs = self._effect_jobs
        waiting = (deferred and getattr(self, "_projection_work_waiting", False)
                   and (jobs.has_running() or bool(jobs.pending)))
        failed = (deferred and getattr(self, "_projection_render_error", None)
                  and getattr(self, "_projection_error_revision", None) == revision)
        batch, complete = [], not (waiting or failed)
        if complete:
            self._projection_work_waiting = False
            self._projection_render_error = None
            for phase in phases:
                tiles = self._collect_document_projection(phase)
                batch.append((phase, tiles))
                complete = complete and self._projection_collection_complete
                if not complete and deferred:
                    break
        self._projection_frame_pending = not complete
        if complete:
            self._projection_progress_view = None
            revision = self._document_projection.revision
            # Usually these references share the projection cache's storage.
            # Keep at most one additional bounded view while it is replaced.
            storage = {int(tile.image.cacheKey()): tile.image.sizeInBytes()
                       for _, tiles in batch for tile in tiles}
            if sum(storage.values()) <= self._document_projection.budget:
                self._projection_completed_view = (configuration, batch, revision)
            else:
                self._projection_completed_view = None
            self._projection_presented_revision = revision
            return batch
        # Opening a chapter or panning leaves the artwork revision unchanged.
        # Present finished tiles instead of holding the whole new viewport
        # behind its slowest effect block. Edits and layered show-on-top passes
        # retain whole-frame publication.
        if (deferred and phases == (None,)
                and (previous is None or previous[2] == revision)):
            level = self._document_projection.resolution_level(
                self.scale * max(1., self.devicePixelRatioF()))
            old = (previous[1][0][1] if previous is not None and previous[1]
                   and previous[1][0][0] is None else [])
            if not old or all(tile.key[1].level == level for tile in old):
                coverage = self.visible_document_rect().adjusted(
                    -self._document_projection.tile_size, -self._document_projection.tile_size,
                    self._document_projection.tile_size, self._document_projection.tile_size)
                merged = {tile.key: tile for tile in old if tile.world_rect.intersects(coverage)}
                if progress is not None:
                    merged.update((tile.key, tile) for tile in progress[1][0][1]
                                  if tile.world_rect.intersects(coverage))
                for _phase, tiles in batch:
                    merged.update((tile.key, tile) for tile in tiles
                                  if tile.world_rect.intersects(coverage))
                visible = list(merged.values())
                storage = {int(tile.image.cacheKey()): tile.image.sizeInBytes()
                           for tile in visible}
                if visible and sum(storage.values()) <= self._document_projection.budget:
                    shown = [(None, visible)]
                    self._projection_progress_view = (configuration, shown, revision)
                    return shown
        return previous[1] if previous is not None else []

    def _paint_document_projection(self, painter, *, live_ink=False):
        if painter.device() is self and getattr(self, "_scene_controller", None) is not None:
            return self._paint_ready_document_projection(painter, live_ink=live_ink)
        if self._projection_has_live_preview() and not self._text_editing:
            # Editing snapshots may be provisional. Present them immediately
            # through the same kernels without admitting them to exact tiles.
            from comic_editor.ui.native_artwork import paint_scene
            previous = getattr(self, "_capture_live_ink", False)
            self._capture_live_ink = live_ink
            try:
                paint_scene(self, painter, self.visible_document_rect())
            finally:
                self._capture_live_ink = previous
            self._paint_projection_grid(painter, None)
            draw_document_border(painter, QRectF(0, 0, self.chapter.width, self.chapter.height),
                                 self.camera_transform(), self.size(), owner=None)
            return
        # Detached captures and exports stay synchronous. Only interactive
        # widget presentation may defer exact effect work to the job queue.
        owner = self if painter.device() is self else None
        stroke = getattr(self, '_projection_stroke_preview', None)
        if owner is not None and (self._drawing or stroke is not None):
            refresh = self._drawing or stroke[3] != self._document_projection.revision
            if refresh and self._capture_stroke_projection_preview():
                stroke = self._projection_stroke_preview
            if self._drawing and stroke is not None:
                self._paint_projection_frame(painter, owner, live_ink=live_ink, stroke_only=True)
                return
        previous = (getattr(self, "_projection_defer_effects", False),
                    getattr(self, "_projection_render_deadline", None))
        deferred = bool(owner is not None and self._projection_can_defer_effects())
        if owner is not None and not deferred:
            jobs = self._effect_jobs
            if jobs.has_finished:
                jobs.poll()
            if (any(job[5] for job in jobs.running_jobs)
                    or any(job[5] for job in jobs.pending.values())):
                # Drawing takes priority over queued background work. Retain
                # finished checkpoints and never wait for a canceled worker.
                jobs.cancel(clear_retained=False)
            self._projection_work_waiting = False
        self._projection_defer_effects = deferred
        self._projection_render_deadline = time.perf_counter() + .008 if deferred else None
        self._projection_yielded = False
        self._projection_blocks_started = 0
        try:
            self._paint_projection_frame(painter, owner, live_ink=live_ink)
        finally:
            self._projection_defer_effects, self._projection_render_deadline = previous
        if (deferred and self._projection_frame_pending and self._projection_yielded
                and not getattr(self, "_projection_work_waiting", False)
                and not getattr(self, "_projection_render_error", None)):
            timer = getattr(self, "_projection_redraw_timer", None)
            if timer is None:
                timer = self._projection_redraw_timer = QTimer(self)
                timer.setSingleShot(True)
                timer.timeout.connect(self.update)
            timer.start(0)

    def _projection_can_defer_effects(self):
        # Paint contact and live source previews need immediate ink. Radial
        # settings/gradient edits can keep the previous complete frame while
        # exact samples finish, including their final release repaint.
        # Initial loading/navigation can also publish completed tiles.
        previous = getattr(self, "_projection_completed_view", None)
        radial = self._radial_preview_current()
        radial_drag = bool(self._modifier_handle_drag and "radial" in self._modifier_handle_drag)
        radial_mask = radial and self._radial_mask_gradient_active()
        stroke = getattr(self, '_projection_stroke_preview', None)
        late_stroke = (stroke is not None and stroke[0] == self._projection_configuration()
                       and stroke[3] == self._document_projection.revision)
        compatible = (previous is None or
                      previous[0] == self._projection_configuration()
                      and (previous[2] == self._document_projection.revision or radial or late_stroke)
                      and any(tiles for _, tiles in previous[1]))
        return bool(self._projection_async_enabled and compatible
                    and not self._drawing
                    and (not getattr(self, "_pen_contact_active", False) or radial_drag or radial_mask)
                    and (not self._projection_has_live_preview() or radial_drag or radial_mask))

    def _capture_stroke_projection_preview(self):
        """Composite fresh ink in scene order while expensive filters finish.

        The existing bounded effect previews handle masks and nested modifiers.
        This viewport image lives outside exact document/graph tile caches; it
        is replaced only by a complete exact projection after contact ends.
        """
        if (not self._projection_async_enabled or self.active_tone_mask_id
                or self.preview_tone_mask_id or self._projection_has_live_preview(include_ink=False)
                or self.selected_kind != 'object'
                or not isinstance(self.chapter.objects.get(self.selected_id), RasterObject)):
            self._projection_stroke_preview = None
            return False
        visible = self.visible_document_rect()
        density = artwork_density(self.scale)
        size = (max(1, round(visible.width()*density)), max(1, round(visible.height()*density)))
        pixel_bytes = QImage.toPixelFormat(self.chapter.pixel_contract.image_format).bitsPerPixel() // 8
        if size[0]*size[1]*pixel_bytes > self._document_projection.budget:
            self._projection_stroke_preview = None
            return False
        jobs = self._effect_jobs
        if jobs.has_finished:
            jobs.poll()
        if any(job[5] for job in jobs.running_jobs) or any(job[5] for job in jobs.pending.values()):
            jobs.cancel(clear_retained=False)
        self._projection_work_waiting = False
        document = self._render_document_state()
        previous = getattr(self, '_projection_stroke_preview', None)
        completed = getattr(self, '_projection_completed_view', None)
        same_view = (previous is not None and previous[0] == document.configuration
                     and previous[2] == visible and previous[1].image.size() == QSize(*size))
        dirty = QRectF(getattr(self, '_stroke_dirty_world', QRectF()))
        predictive = self._predictive if self._show_on_top_live_ink() else None
        if predictive is not None:
            start, end, width, _color = predictive
            margin = width / 2 + 2
            dirty = dirty.united(QRectF(start, end).normalized().adjusted(
                -margin, -margin, margin, margin))
        if same_view and len(previous) > 4:
            dirty = dirty.united(previous[4])
        coverage_needed = visible if document.overflow > 0. else visible.intersected(document.bounds)
        completed_covers = (completed is not None and completed[0] == document.configuration
                            and self._completed_projection_covers(completed, coverage_needed))
        seed = same_view or completed_covers
        area, offset, capture_size = visible, (0, 0), size
        if seed and not dirty.isEmpty():
            # Replace the affected composite, including erased pixels and all
            # covering layers. Repainting unrelated artwork causes a large
            # first-contact stall even when the active effect has a cheap draft.
            patch = QRect(math.floor((dirty.left()-visible.left())*density)-2,
                          math.floor((dirty.top()-visible.top())*density)-2,
                          math.ceil(dirty.width()*density)+5,
                          math.ceil(dirty.height()*density)+5).intersected(QRect(0, 0, *size))
            if patch.isEmpty():
                return False
            offset, capture_size = (patch.x(), patch.y()), (patch.width(), patch.height())
            area = QRectF(visible.x()+patch.x()/density, visible.y()+patch.y()/density,
                          patch.width()/density, patch.height()/density)
        request = RenderRequest(tuple(area.getRect()), density, capture_size, ('stroke-preview',),
                                document.revision, quality=RenderQuality.INTERACTIVE)
        previous_ink = getattr(self, '_capture_live_ink', False)
        self._capture_live_ink = self._show_on_top_live_ink()
        try:
            result = self._render_service.render_region(document, request)
        finally:
            self._capture_live_ink = previous_ink
        if result.image.isNull():
            self._projection_stroke_preview = None
            return False
        image = result.image
        if capture_size != size or offset != (0, 0):
            image = QImage(previous[1].image) if same_view else QImage(*size, document.pixel_contract.image_format)
            if not same_view:
                image.fill(Qt.transparent)
            painter = QPainter(image)
            try:
                if not same_view:
                    camera = QTransform.fromTranslate(-visible.x(), -visible.y()) * QTransform.fromScale(density, density)
                    for _, tiles in completed[1]:
                        draw_document_tiles(painter, tiles, camera, QSize(*size), owner=None, smooth=True)
                painter.setCompositionMode(QPainter.CompositionMode_Source)
                painter.drawImage(*offset, result.image)
            finally:
                painter.end()
        tile = PresentedTile(('stroke-preview', document.identity, document.revision), image,
                             visible, QRectF(0, 0, *size))
        self._projection_stroke_preview = document.configuration, tile, visible, document.revision, dirty
        return True

    @staticmethod
    def _completed_projection_covers(completed, visible):
        """Every retained pass must cover the camera before a dirty-only patch."""
        if not completed[1]:
            return False
        needed = QPainterPath()
        needed.addRect(visible)
        for _, tiles in completed[1]:
            coverage = QPainterPath()
            coverage.setFillRule(Qt.WindingFill)
            for tile in tiles:
                coverage.addRect(tile.world_rect)
            if not needed.subtracted(coverage).isEmpty():
                return False
        return True

    def _paint_projection_frame(self, painter, owner, *, live_ink, stroke_only=False):
        stats = []
        with self._show_on_top_scene() as phases:
            if (not live_ink or self._is_show_on_top(self.selected_kind, self.selected_id)
                    or any(obj.blend_mode != "normal" for obj in self.chapter.objects.values())):
                phases = (None,)
            if stroke_only:
                self._projection_frame_pending = True
                batch = []
            else:
                batch = self._projection_phase_batch(phases)
            stroke = getattr(self, '_projection_stroke_preview', None)
            showing_stroke = (self._projection_frame_pending and stroke is not None
                    and stroke[0] == self._projection_configuration()
                    and stroke[3] == self._document_projection.revision
                    and stroke[2].contains(self.visible_document_rect()))
            if showing_stroke:
                batch = [(None, [stroke[1]])]
            elif not self._projection_frame_pending:
                self._projection_stroke_preview = None
            # A retained combined view cannot place ordinary prediction below
            # promoted artwork. Wait for the matching finished phase set.
            draw_live = (live_ink and not showing_stroke
                         and tuple(phase for phase, _ in batch) == tuple(phases))
            for phase, tiles in batch:
                stats.append(draw_document_tiles(
                    painter, tiles, self.camera_transform(), self.size(),
                    owner=owner, smooth=True))
                if draw_live:
                    self._show_on_top_phase = phase
                    painter.save()
                    try:
                        painter.setTransform(self.camera_transform())
                        painter.setClipRect(QRectF(0, 0, self.chapter.width,
                                                   self.chapter.height), Qt.IntersectClip)
                        self._set_live_underlay_context()
                        self._draw_predictive_ink(painter)
                        self._draw_live_vector_gesture(painter)
                    finally:
                        self._clear_live_underlay_context()
                        painter.restore()
        self._document_presentation_stats = PresentationStats(
            stats[-1].backend if stats else "pending", sum(item.tiles for item in stats),
            sum(item.uploads for item in stats), stats[-1].texture_bytes if stats else 0)
        self._paint_projection_grid(painter, owner)
        draw_document_border(painter, QRectF(0, 0, self.chapter.width, self.chapter.height),
                             self.camera_transform(), self.size(), owner=owner)
        if owner is not None and self._projection_frame_pending and not stroke_only:
            painter.save()
            painter.resetTransform()
            label = ("Artwork rendering failed" if getattr(self, "_projection_render_error", None)
                     else "Preparing artwork…")
            box = QRect(12, max(0, self.height() - 38), 220, 26)
            painter.fillRect(box, QColor(36, 36, 40, 230))
            painter.setPen(QColor("#eeeeee"))
            painter.drawText(box.adjusted(8, 0, -8, 0), Qt.AlignVCenter, label)
            painter.restore()

    def _paint_projection_grid(self, painter, owner):
        if not self.settings.grid_overlay_visible:
            self._projection_grid_cache = None
            return
        camera = self.camera_transform()
        chapter_rect = QRectF(0, 0, self.chapter.width, self.chapter.height)
        visible = self.visible_document_rect()
        if owner is None:
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setTransform(camera)
            painter.setClipRect(chapter_rect, Qt.IntersectClip)
            self._draw_grid(painter, visible)
            painter.restore()
            return
        # Qt's non-multisampled GL paint engine does not antialias cosmetic
        # lines. Keep the grid's established coverage in one small UI overlay;
        # its camera changes never invalidate or read back document artwork.
        layer_id = None
        if self.selected_kind == "layer" and self.selected_id in self.chapter.layers:
            layer_id = self.selected_id
        elif self.selected_kind == "object" and self.selected_id in self.chapter.objects:
            layer_id = self.chapter.objects[self.selected_id].parent_layer_id
        grid = self.resolved_grid(layer_id)
        ratio = self.devicePixelRatioF()
        key = (self.width(), self.height(), ratio, self.chapter.width, self.chapter.height,
               tuple(sorted(grid.to_dict().items())),
               camera.m11(), camera.m12(), camera.m21(), camera.m22(), camera.dx(), camera.dy())
        cached = getattr(self, "_projection_grid_cache", None)
        if cached is None or cached[0] != key:
            image = QImage(round(self.width() * ratio), round(self.height() * ratio),
                           QImage.Format_ARGB32_Premultiplied)
            image.setDevicePixelRatio(ratio)
            image.fill(Qt.transparent)
            overlay = QPainter(image)
            try:
                overlay.setRenderHint(QPainter.Antialiasing, True)
                overlay.setTransform(camera)
                overlay.setClipRect(chapter_rect)
                self._draw_grid(overlay, visible)
            finally:
                overlay.end()
            self._projection_grid_cache = key, image
        painter.save()
        painter.resetTransform()
        painter.drawImage(0, 0, self._projection_grid_cache[1])
        painter.restore()
