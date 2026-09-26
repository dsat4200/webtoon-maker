"""Bridge the editor's faithful document renderer to retained presentation."""
from collections import defaultdict
import time

from PySide6.QtCore import QRect, QRectF, QSize, Qt, QTimer
from PySide6.QtGui import QColor, QImage, QPainter, QPainterPath, QTransform

from comic_editor.ui.document_presentation import (
    PresentedTile, PresentationStats, draw_document_tiles, draw_document_border,
)
from comic_editor.ui.document_projection import ProjectionAddress, ProjectionRequest


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
        self._set_live_underlay_context()
        underlay = self._live_underlay_object_id, self._live_underlay_amount
        self._clear_live_underlay_context()
        selected = (self.chapter.objects if self.selected_kind == "object"
                    else self.chapter.layers).get(self.selected_id)
        mask_only = ((self.selected_kind, self.selected_id)
                     if selected is not None and selected.mask_only else None)
        return (id(self.chapter), id(self.tiles), id(self.images),
                self.chapter.width, self.chapter.height, self.chapter.background,
                self.chapter.view_overflow, underlay, tuple(sorted(self._solo_entities)), mask_only)

    def _invalidate_selection_scene_cache(self):
        # Selection/tool UI is drawn after document presentation. Underlay and
        # selected mask-only visibility have their own retained configuration.
        # A canceled live preview can change artwork without a model signal;
        # conservatively retire such captures before returning to ordinary UI.
        preview = getattr(self, "_projection_captured_live_preview", False)
        self._invalidate_scene_cache(projection=preview)
        self._projection_captured_live_preview = False

    def _projection_has_live_preview(self):
        return any(bool(getattr(self, name, None)) for name in (
            "_transform_preview_quad", "_multi_transform_preview_quads",
            "_selection_raster_states", "_selection_vector_preview", "_selection_shape_nodes",
            "_vector_gesture_mode", "_cage_session", "_text_editing", "_text_placement",
            "_gradient_preview_active", "_render_excluded_object_id", "_page_gap_draft",
            "_fill_gesture_active", "_text_property_drag", "_shape_property_drag",
            "_raster_paste_overlay",
        )) or getattr(self, "_selection_before_tiles", None) is not None

    def _collect_document_projection(self, phase=None):
        projection = self._document_projection
        view_configuration = self._projection_configuration()
        # Base/top alternate within one frame. Their guard windows describe
        # the same camera coverage and must not reset on every phase switch.
        if getattr(self, "_projection_windows_configuration", None) != view_configuration:
            self._projection_windows = {}
            self._projection_windows_configuration = view_configuration
        configuration = (*view_configuration, phase)
        projection.configure(configuration, document=configuration[:3])
        visible = self.visible_document_rect()
        # A guard band warms the corners exposed by canvas rotation and the
        # next small pan. Its size is tied to the stable tile grid, not an
        # ever-changing capture rectangle.
        density = self.scale * max(1., self.devicePixelRatioF())
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
            tiles = projection.collect(requests, self._render_document_tile,
                                       render_many=self._render_document_tiles)
        finally:
            self._projection_capture_phase = previous_phase
        self._projection_collection_complete = (
            len(tiles) == len(requests) and all(tile.valid for tile in tiles))
        return [PresentedTile((phase, tile.request.address), tile.image,
                              tile.request.world_rect, tile.request.source_rect) for tile in tiles]

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

    def _render_document_tiles(self, requests):
        # The scene traversal and geometry setup are substantially more costly
        # than slicing an image. Share them across adjacent missing tiles;
        # retained tiles still invalidate and present independently.
        groups = defaultdict(list)
        for request in requests:
            address = request.address
            groups[(address.level, address.x // 4, address.y // 4)].append(request)
        # Unvisited blocks stay explicitly incomplete so collect() does not
        # immediately retry them individually in the same paint.
        result = {request.address: (QImage(), False) for request in requests}
        for key, group in groups.items():
            deadline = getattr(self, "_projection_render_deadline", None)
            if (deadline is not None and time.perf_counter() >= deadline
                    and getattr(self, "_projection_blocks_started", 0) > 0):
                self._projection_yielded = True
                break
            self._projection_blocks_started = getattr(self, "_projection_blocks_started", 0) + 1
            level, block_x, block_y = key
            # Fixed capture devices are essential: Qt's antialiased path clips
            # can change their edge coverage when a partial redraw uses a
            # smaller device. Always reproduce the same block before slicing.
            block = [ProjectionRequest(ProjectionAddress(level, x, y),
                                       group[0].tile_size, group[0].gutter)
                     for y in range(block_y * 4, (block_y + 1) * 4)
                     for x in range(block_x * 4, (block_x + 1) * 4)]
            bounds = QRectF()
            for request in block:
                bounds = bounds.united(request.capture_rect)
            scale = group[0].scale
            size = QSize(round(bounds.width() * scale), round(bounds.height() * scale))
            requested = QRectF()
            for request in group:
                requested = requested.united(request.capture_rect)
            # Publication is gated across the whole requested view. Blocks may
            # finish separately without exposing mixed revisions to the user.
            image, exact = self._render_document_region(
                bounds, scale, size, key, exact=True, requested=requested)
            for request in group:
                offset = request.capture_rect.topLeft() - bounds.topLeft()
                crop = QRect(round(offset.x() * scale), round(offset.y() * scale),
                             request.pixel_size, request.pixel_size)
                result[request.address] = (image.copy(crop) if exact else QImage(), exact)
            if (getattr(self, "_projection_defer_effects", False)
                    and (getattr(self, "_projection_work_waiting", False)
                         or getattr(self, "_projection_render_error", None))):
                break
        return result

    def _render_document_region(self, rect, scale, size, key, *, exact=False, requested=None):
        from comic_editor.ui.async_projection import ProjectionPending, ProjectionFailed
        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        transform = QTransform(scale, 0, 0, scale, -rect.x() * scale, -rect.y() * scale)
        chapter_rect = QRectF(0, 0, self.chapter.width, self.chapter.height)
        visible = rect if requested is None else requested
        effect_region = rect
        if self.chapter.view_overflow <= 0:
            visible = visible.intersected(chapter_rect)
            effect_region = effect_region.intersected(chapter_rect)
        previous = (self._interactive_render, getattr(self, "_effect_viewport_world", None),
                    self._vector_render_scale_override,
                    getattr(self, "_effect_region_requests", False),
                    getattr(self, "_projection_tile_key", None),
                    getattr(self, "_effect_preview_channel", "canvas"),
                    getattr(self, "_projection_exact", False))
        provisional = getattr(self, "_effect_provisional_revision", 0)
        phase = getattr(self, "_projection_capture_phase", None)
        self._interactive_render = True
        # Dirty subsets must not change background-effect source windows and
        # keys within one fixed capture block. Scene culling can still use the
        # requested subset without recomputing every background modifier.
        self._effect_viewport_world = effect_region
        self._vector_render_scale_override = scale
        self._effect_region_requests = True
        self._projection_tile_key = phase, key
        self._effect_preview_channel = "canvas"
        self._projection_exact = exact
        self._projection_captured_live_preview = (
            getattr(self, "_projection_captured_live_preview", False)
            or self._projection_has_live_preview())
        self._render_bounds.prepare()
        painter = QPainter(image)
        finished = True
        try:
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.setTransform(transform)
            if phase != "top":
                painter.fillRect(chapter_rect, QColor(self.chapter.background))
                if self.chapter.view_overflow > 0:
                    # Both overflow passes precede all in-page artwork. Keep
                    # their existing combined opacity/composition in the base.
                    self._render_document_tile_overflow(painter, image.size(), transform, rect)
            painter.setClipRect(chapter_rect)
            self._set_live_underlay_context()
            if not visible.isEmpty():
                self._render_scene_layers(painter, visible, underlay=True, only_phase=phase)
        except ProjectionPending:
            finished = False
            self._projection_work_waiting = True
        except ProjectionFailed as error:
            finished = False
            self._projection_render_error = str(error)
            self._projection_error_revision = self._document_projection.revision
        finally:
            painter.end()
            self._clear_live_underlay_context()
            (self._interactive_render, self._effect_viewport_world,
             self._vector_render_scale_override, self._effect_region_requests,
             self._projection_tile_key, self._effect_preview_channel,
             self._projection_exact) = previous
        exact = finished and provisional == getattr(self, "_effect_provisional_revision", 0)
        return image if exact else QImage(), exact

    def _render_document_tile_overflow(self, painter, size, transform, visible):
        page_area = QPainterPath()
        for identifier in self.chapter.root_page_ids:
            page = self.chapter.layers[identifier]
            if page.visible:
                page_area = page_area.united(self.layer_world_transform(identifier).map(
                    self.layer_effective_path(identifier)))
        chapter_area = QPainterPath()
        chapter_area.addRect(QRectF(0, 0, self.chapter.width, self.chapter.height))
        area = QPainterPath()
        area.addRect(visible)
        outside = area.subtracted(page_area.intersected(chapter_area))
        if outside.isEmpty():
            return
        image = QImage(size, QImage.Format_ARGB32_Premultiplied)
        image.fill(Qt.transparent)
        overflow = QPainter(image)
        previous = self._effect_preview_channel
        try:
            overflow.setRenderHint(QPainter.Antialiasing, True)
            overflow.setTransform(transform)
            overflow.setClipPath(outside)
            self._effect_preview_channel = "overflow"
            self._render_scene_layers(overflow, visible, page_contents_only=True)
        finally:
            self._effect_preview_channel = previous
            overflow.end()
        painter.save()
        painter.setTransform(QTransform())
        painter.setOpacity(self.chapter.view_overflow)
        painter.drawImage(0, 0, image)
        painter.restore()

    def _projection_phase_batch(self, phases):
        configuration = self._projection_configuration()
        previous = getattr(self, "_projection_completed_view", None)
        if previous is not None and previous[0] != configuration:
            previous = self._projection_completed_view = None
        revision = self._document_projection.revision
        deferred = getattr(self, "_projection_defer_effects", False)
        jobs = self._effect_jobs
        running = jobs.running
        waiting = (deferred and getattr(self, "_projection_work_waiting", False)
                   and ((running is not None and not running[2].is_set()) or bool(jobs.pending)))
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
        return previous[1] if previous is not None else []

    def _paint_document_projection(self, painter, *, live_ink=False):
        # Detached captures and exports stay synchronous. Only interactive
        # widget presentation may defer exact effect work to the job queue.
        owner = self if painter.device() is self else None
        previous = (getattr(self, "_projection_defer_effects", False),
                    getattr(self, "_projection_render_deadline", None))
        deferred = bool(owner is not None and self._projection_can_defer_effects())
        if owner is not None and not deferred:
            jobs = self._effect_jobs
            if jobs.running is not None and jobs.running[3].done():
                jobs.poll()
            if ((jobs.running is not None and jobs.running[5])
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
            QTimer.singleShot(0, self.update)

    def _projection_can_defer_effects(self):
        # Queuing pen events is insufficient if whole-view publication hides
        # their ink. Active edits always get a finished synchronous frame.
        # Background preparation is reserved for camera navigation and idle
        # coverage while a matching completed view can remain visible.
        previous = getattr(self, "_projection_completed_view", None)
        return bool(self._projection_async_enabled and previous is not None
                    and previous[0] == self._projection_configuration()
                    and previous[2] == self._document_projection.revision
                    and any(tiles for _, tiles in previous[1])
                    and not self._drawing
                    and not getattr(self, "_pen_contact_active", False)
                    and not self._projection_has_live_preview())

    def _paint_projection_frame(self, painter, owner, *, live_ink):
        stats = []
        with self._show_on_top_scene() as phases:
            if not live_ink or self._is_show_on_top(self.selected_kind, self.selected_id):
                phases = (None,)
            batch = self._projection_phase_batch(phases)
            # A retained combined view cannot place ordinary prediction below
            # promoted artwork. Wait for the matching finished phase set.
            draw_live = live_ink and tuple(phase for phase, _ in batch) == tuple(phases)
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
        if owner is not None and self._projection_frame_pending:
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
