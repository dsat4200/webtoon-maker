"""Manual navigator caching through the ordinary document render service."""
from collections import OrderedDict, deque
from contextlib import contextmanager
import math
import time

from PySide6.QtCore import QObject, QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QTextEdit,
    QPlainTextEdit, QTreeView, QWidget, QWidgetAction,
)

from comic_editor.render.cache import PersistentRenderCache, WRITE_BUDGET
from comic_editor.render.service import TileBatchPolicy, RenderPending, RenderFailed
from comic_editor.ui.cache_dependencies import RenderDependencies


class DiskCacheController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.canvas = window, window.canvas
        self.canvas._disk_cache_controller = self
        self.backing = self.dependencies = None
        teardown = self._teardown = [None]
        # deleteLater() bypasses closeEvent in tests and during Qt teardown.
        # Capture storage only: QObject children may already be destroyed.
        self.destroyed.connect(lambda: teardown[0].close() if teardown[0] is not None else None)
        self.building = False
        self.start_y = self.end_y = 0.
        self.status = {}
        self.observed = OrderedDict()
        self._keys = {}
        self._status_queue = deque()
        self._work = deque()
        self._warm = deque()
        self._disabled = []
        self.message = ""
        self._revision = -1
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        window.preview.disk_cache = self
        panel = window.navigator_panel
        panel.cache_button.clicked.connect(self.toggle)
        panel.clear_selected_action.triggered.connect(lambda: self.clear(selected=True))
        panel.clear_all_action.triggered.connect(lambda: self.clear(selected=False))
        self.refresh_ui()

    def bind(self):
        self.detach()
        chapter, repository = self.canvas.chapter, self.window.repository
        session = self.window.active_session
        if chapter is None or repository is None or (session is not None and session.kind != "series"):
            self.refresh_ui()
            return
        self.backing = PersistentRenderCache(repository.chapter_root(chapter.chapter_id) / ".render-cache",
            contract=chapter.pixel_contract.signature, environment=RenderDependencies.environment())
        self._teardown[0] = self.backing
        self.dependencies = RenderDependencies(self.canvas, self.backing)
        self.canvas._persistent_render_cache = self.backing
        self.canvas._render_dependencies = self.dependencies
        self.canvas.tiles.render_fingerprint = self.dependencies.tiles
        self.canvas.images.render_fingerprint = self.dependencies.image
        projection = self.canvas._document_projection
        projection.backing_lookup = self.lookup_tile
        projection.backing_retain = self.retain_tile
        saved = self.window.settings.document_cache_ranges.get(self.window._document_view_key())
        self.start_y, self.end_y = saved or (0., float(chapter.height))
        self.set_range(self.start_y, self.end_y, save=False)
        self._revision = -1
        self.timer.start()
        self.refresh_ui()

    def detach(self):
        self.cancel()
        self.timer.stop()
        projection = self.canvas._document_projection
        projection.backing_lookup = projection.backing_retain = None
        if self.dependencies is not None:
            self.canvas.tiles.render_fingerprint = None
            self.canvas.images.render_fingerprint = None
        self.canvas._persistent_render_cache = self.canvas._render_dependencies = None
        if self.backing is not None:
            self.backing.close()
        self._teardown[0] = None
        self.backing = self.dependencies = None
        self.status.clear()
        self._keys.clear()
        self.observed.clear()
        self._status_queue.clear()

    def set_range(self, start, end, *, save=True):
        if self.canvas.chapter is None or self.building:
            return
        height = self.canvas.chapter.height
        self.start_y = min(max(0., float(start)), max(0., height - 1.))
        self.end_y = min(float(height), max(self.start_y + 1., float(end)))
        if save and self.window._document_view_key() is not None:
            from comic_editor.core.settings import save_settings
            self.window.settings.document_cache_ranges[self.window._document_view_key()] = [self.start_y, self.end_y]
            save_settings(self.window.settings)
        self.refresh_ui()

    @property
    def selected_rows(self):
        side = self.canvas._document_projection.tile_size
        return range(math.floor(self.start_y / side), math.ceil(self.end_y / side))

    @contextmanager
    def capture(self):
        canvas = self.canvas
        previous = getattr(canvas, "_disk_cache_capture", False)
        canvas._disk_cache_capture = True
        try:
            with canvas.without_solo():
                yield
        finally:
            canvas._disk_cache_capture = previous

    def requests(self, row):
        chapter, projection = self.canvas.chapter, self.canvas._document_projection
        return projection.requests(QRectF(0, row * projection.tile_size,
            chapter.width, min(projection.tile_size, chapter.height - row * projection.tile_size)), 1.)

    def tile_key(self, request, configuration):
        marker = (self.canvas._document_projection.revision, request.address, tuple(configuration[3:]))
        key = self._keys.get(marker)
        if key is None:
            previous = self.dependencies.deferred
            self.dependencies.deferred = True
            try:
                key = self.dependencies.projection_key(request, configuration)
            finally:
                self.dependencies.deferred = previous
            self._keys[marker] = key
        return key

    def reusable(self, configuration):
        return (self.backing is not None and not self.canvas._projection_has_live_preview()
                and configuration[7] == ("", 0.) and not configuration[8] and configuration[9] is None)

    def lookup_tile(self, request, configuration):
        if not self.reusable(configuration):
            return None
        try:
            return self.backing.lookup("projection", self.tile_key(request, configuration))
        except RenderFailed as error:
            self.backing.error = str(error)
            return None

    def retain_tile(self, request, configuration, image):
        if self.reusable(configuration):
            try:
                self.backing.retain("projection", self.tile_key(request, configuration), image)
            except RenderFailed as error:
                self.backing.error = str(error)

    def observe(self, kind, key, state):
        # Metadata only; the existing LRUs retain and bound the actual pixels.
        viewport = getattr(self.canvas, "_effect_viewport_world", None)
        if viewport is None:
            return
        marker = (kind, key)
        self.observed.pop(marker, None)
        self.observed[marker] = QRectF(viewport), state
        while len(self.observed) > 2048:
            self.observed.popitem(last=False)

    def _warm_value(self, marker):
        kind, key = marker
        if kind == "retained":
            _, scope, semantic = key
            entry = self.canvas._effect_jobs.retained.get(scope)
            return entry[1] if entry is not None and entry[0] == semantic else None
        pool = self.canvas._modifier_source_cache if kind == "source" else self.canvas._modifier_render_cache
        return pool.get(key)

    def toggle(self):
        if self.building:
            self.cancel()
        else:
            self.start()

    def start(self):
        if self.backing is None or self.building:
            return False
        if not self.window.save():
            return False
        if (self.canvas._projection_has_live_preview() or self.canvas._drawing
                or self.canvas._fill_job_cancel is not None
                or getattr(self.canvas, "_fill_async_gesture", None) is not None):
            self.message = "Finish the current edit first"
            self.refresh_ui()
            return False
        self._keys.clear()
        self.backing.error = ""
        self._work = deque(self.selected_rows)
        side = self.canvas._document_projection.tile_size
        selected = QRectF(0, self._work[0] * side, self.canvas.chapter.width, len(self._work) * side)
        self._warm = deque(marker for marker, (region, state) in self.observed.items() if region.intersects(selected))
        self.building = True
        self._lock(True)
        self.message = "Caching… edits paused"
        self.timer.start()
        self.refresh_ui()
        return True

    def _lock(self, locked):
        canvas, window = self.canvas, self.window
        canvas.document_read_only = canvas.command_stack.read_only = locked
        window.hierarchy_model.read_only = locked
        if locked:
            allowed = (window.navigator_panel, window.chapter_combo, window.project_tabs)
            classes = (QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QTextEdit, QPlainTextEdit, QTreeView)
            self._disabled = []
            for widget in window.findChildren(QWidget):
                if isinstance(widget, classes) and widget.isEnabled() and not any(
                        widget is root or root.isAncestorOf(widget) for root in allowed):
                    self._disabled.append(widget)
                    widget.setEnabled(False)
            for action in window.findChildren(QAction):
                widget = action.defaultWidget() if isinstance(action, QWidgetAction) else None
                if widget is not None and any(widget is root or widget.isAncestorOf(root)
                                               or root.isAncestorOf(widget) for root in allowed):
                    continue
                if action.isEnabled() and action is not window.fullscreen_action:
                    self._disabled.append(action)
                    action.setEnabled(False)
            window.autosave_timer.stop()
        else:
            for item in self._disabled:
                item.setEnabled(True)
            self._disabled.clear()
            window._refresh_actions()
            window.blender_sources.resume_for_context()
            canvas._finish_pending_external_drop_if_ready()
            sources = getattr(window, "_deferred_clipboard_sources", None)
            if sources is not None:
                window._deferred_clipboard_sources = None
                window._clipboard_images_resolved(sources)
        canvas.setFocus()

    def blocks_event(self, watched, event):
        if not self.building or not isinstance(watched, QWidget):
            return False
        if watched is not self.window and not self.window.isAncestorOf(watched):
            return False
        if watched is self.canvas:
            return False
        for root in (self.window.navigator_panel, self.window.chapter_combo, self.window.project_tabs):
            if watched is root or root.isAncestorOf(watched):
                return False
        return event.type() in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease, QEvent.MouseButtonDblClick,
                                QEvent.TabletPress, QEvent.TabletRelease, QEvent.KeyPress, QEvent.KeyRelease,
                                QEvent.Wheel, QEvent.TouchBegin, QEvent.TouchUpdate, QEvent.TouchEnd)

    def cancel(self):
        if self.building:
            self.building = False
            self._work.clear()
            self._warm.clear()
            self._lock(False)
            self.message = "Canceled; completed sections kept"
            self.refresh_ui()

    def _reset_status(self):
        self._revision = self.canvas._document_projection.revision
        self._keys.clear()
        self.status.clear()
        count = math.ceil(self.canvas.chapter.height / self.canvas._document_projection.tile_size)
        visible = int(self.canvas.center_y / self.canvas._document_projection.tile_size)
        self._status_queue = deque(sorted(range(count), key=lambda row: abs(row - visible)))

    def row_ready(self, row):
        with self.capture():
            configuration = (*self.canvas._projection_configuration(), None)
            try:
                return all(self.backing.has("projection", self.tile_key(request, configuration)) for request in self.requests(row))
            except RenderPending:
                return False
            except RenderFailed as error:
                self.backing.error = str(error)
                return False

    def tick(self):
        if self.backing is None:
            return
        changed = self.backing.poll()
        reads_ready = any(future.done() for future in self.backing.reads.values())
        if changed or reads_ready:
            self.canvas._invalidate_scene_cache(projection=False)
        if self._revision != self.canvas._document_projection.revision:
            self._reset_status()
        if self.backing.error and self.building:
            self.cancel()
            self.message = "Cache failed: " + self.backing.error
            self.refresh_ui()
        deadline = time.perf_counter() + .008
        if self.building:
            with self.capture(), self.backing.record():
                while self._warm and self.backing.write_bytes < WRITE_BUDGET:
                    marker = self._warm[0]
                    image = self._warm_value(marker)
                    if image is not None and not self.backing.retain(*marker, image, state=self.observed[marker][1]):
                        break
                    self._warm.popleft()
                    if time.perf_counter() >= deadline:
                        break
                if not self._warm and self._work and self.backing.write_bytes < WRITE_BUDGET:
                    row = self._work[0]
                    document = self.canvas._render_document_state()
                    phases = (None, "base", "top") if self.canvas._show_on_top_plan().entries else (None,)
                    complete = True
                    for phase in phases:
                        requests = self.requests(row)
                        tiles = self.canvas._render_service.collect_tiles(document, requests,
                            TileBatchPolicy((document.width / 2, row * 256), deadline), phase=phase, defer_effects=True)
                        complete &= len(tiles) == len(requests) and all(tile.valid for tile in tiles)
                    if complete:
                        # Advance only when every selected final tile is durable.
                        if self.row_ready(row):
                            self.status[row] = True
                            self._work.popleft()
                        else:
                            self.status[row] = False
                    error = self.canvas._render_service.last_error
                    if error:
                        self.backing.error = error
            if not self._work and not self._warm and not self.backing.writes and self.backing.publication is None:
                self.building = False
                self._lock(False)
                self.message = "Cached to disk"
        checked = 0
        status_changed = False
        while self._status_queue and checked < 8 and time.perf_counter() < deadline:
            row = self._status_queue.popleft()
            ready = self.row_ready(row)
            status_changed |= self.status.get(row) != ready
            self.status[row] = ready
            if not self.status[row] and (self.backing.verifications or self.dependencies.hashes):
                self._status_queue.append(row)
            checked += 1
        if changed:
            queued = set(self._status_queue)
            self._status_queue.extend(row for row in self.status if row not in queued)
        if changed or status_changed or self.building:
            self.refresh_ui()
        self.timer.setInterval(20 if self.building or self.backing.pending or self._status_queue or self.dependencies.hashes else 150)

    def clear(self, *, selected):
        if self.backing is None:
            return
        self.cancel()
        if selected:
            rows = set(self.selected_rows)
            # Source/effect intermediates are shared; clearing a segment also
            # retires these shared values, leaving other final rows reusable.
            self.backing.clear(lambda entry: entry["kind"] != "projection" or
                (len(entry.get("key", ())) > 3 and entry["key"][3] in rows))
        else:
            self.backing.clear()
        self._reset_status()
        self.message = "Cache cleared"
        self.refresh_ui()

    def refresh_ui(self):
        panel = self.window.navigator_panel
        panel.cache_button.setEnabled(self.backing is not None)
        panel.cache_button.setText("Cancel" if self.building else "Cache to disk")
        panel.clear_selected_action.setEnabled(self.backing is not None and not self.building)
        panel.clear_all_action.setEnabled(self.backing is not None and not self.building)
        size = self.backing.disk_bytes / (1024 * 1024) if self.backing else 0
        panel.cache_status.setText(f"{size:.1f} MB")
        panel.cache_status.setEnabled(self.backing is not None and not self.building)
        panel.cache_progress.setText(f"{len(self.selected_rows) - len(self._work)} / {len(self.selected_rows)}"
            if self.building else ("Failed" if self.message.startswith("Cache failed:")
                                  else f"{len(self.selected_rows)} sections" if self.backing else ""))
        text = self.message or "Drag the white handles to choose a full-width section. Green: saved. Red: missing or changed."
        if self.building:
            total = len(self.selected_rows)
            text = f"Caching {total - len(self._work)} / {total} sections. Editing is paused."
        panel.cache_button.setToolTip(text)
        panel.cache_status.setToolTip(text)
        self.window.preview.update()
