"""Manual navigator caching through the ordinary document render service."""
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import math
import time

from PySide6.QtCore import QObject, QEvent, QRectF, Qt, QTimer
from PySide6.QtGui import QAction
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QTextEdit,
    QPlainTextEdit, QTreeView, QWidget, QWidgetAction,
)

from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.service import RenderPending, RenderFailed
from comic_editor.render.scene import SceneSnapshotCompiler
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.color_resources import semantic_color_identity


class DiskCacheController(QObject):
    def __init__(self, window):
        super().__init__(window)
        self.window, self.canvas = window, window.canvas
        self.canvas._disk_cache_controller = self
        self.backing = self.dependencies = None
        teardown = self._teardown = [None]
        # deleteLater() bypasses closeEvent in tests and during Qt teardown.
        # Capture storage only: QObject children may already be destroyed.
        self.scheduler = SceneScheduler()
        self.cleanup = ThreadPoolExecutor(max_workers=1,thread_name_prefix='cache-retire')
        scheduler,cleanup = self.scheduler,self.cleanup
        def destroy_storage():
            scheduler.close()
            if teardown[0] is not None:
                try:
                    cleanup.submit(teardown[0].close)
                except RuntimeError:
                    pass
            cleanup.shutdown(wait=False)
        self.destroyed.connect(destroy_storage)
        self.compiler = SceneSnapshotCompiler()
        self._capture = self._snapshot = None
        self._serial = 0
        self._maintenance = None
        self._pending_rows = set()
        self._evaluation_done = False
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
            contract=chapter.pixel_contract.signature,
            environment=(*RenderDependencies.environment(), semantic_color_identity(self.canvas, chapter.pixel_contract)))
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
            self.cleanup.submit(self.backing.close)
        self._teardown[0] = None
        self.backing = self.dependencies = None
        self.status.clear()
        self._keys.clear()
        self.observed.clear()
        self._status_queue.clear()
        self._pending_rows.clear()
        self._serial += 1
        self._capture = self._snapshot = None

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
        contract = self.canvas.chapter.pixel_contract
        color = semantic_color_identity(self.canvas, contract)
        # Both tiers share this captured policy/resource identity. Updating the
        # binding also keeps newly saved exact entries reusable after reopening.
        self.backing.contract = contract.signature
        self.backing.environment = (*RenderDependencies.environment(), color)
        marker = (self.canvas._document_projection.revision, request.address,
                  tuple(configuration[3:]), contract.signature, color)
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
        return (self.backing is not None and (self._maintenance is None or self._maintenance[2] != 'clear')
                and semantic_color_identity(self.canvas, self.canvas.chapter.pixel_contract) != ('color-resources-pending',)
                and not self.canvas._projection_has_live_preview()
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
        if self.backing is None or self.building or self._maintenance is not None:
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
        self._serial += 1
        self._pending_rows.clear()
        self._evaluation_done = False
        self.compiler.invalidate()
        with self.capture():
            document = self.canvas._render_document_state()
            self._capture = self.compiler.capture(self.canvas,document)
        self._snapshot = None
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
            self._capture = self._snapshot = None
            self.scheduler.cancel()
            backing,serial = self.backing,self._serial
            def drain_recording():
                # Queued after the canceled evaluator: its completed immutable
                # writes and index publication finish on the detached lane.
                self.scheduler._close_backend()
                if backing is None:
                    return None
                return self._read_manifest(backing)
            self._maintenance = self.scheduler.executor.submit(drain_recording),serial,'cancel'
            self._lock(False)
            self.message = "Canceled; completed sections kept"
            self.refresh_ui()

    @staticmethod
    def _read_manifest(backing):
        committed = PersistentRenderCache(backing.root,contract=backing.contract,environment=backing.environment)
        try:
            return committed.entries,committed.source_digests
        finally:
            committed.close()

    def _adopt_manifest(self,manifest):
        if manifest is None or self.backing is None:
            return
        # This GUI binding is a reader. Only the recording owner publishes an
        # index; reload its committed entries, then validate blobs normally.
        entries,sources = manifest
        verified = {identity for identity in self.backing.verified
            if identity in entries and self.backing.entries.get(identity) == entries[identity]}
        self.backing.entries,self.backing.source_digests = entries,sources
        self.backing._root_epoch = self.backing._root_state.epoch
        self.backing.verified = verified
        self.backing.reads.clear()
        self.backing.verifications.clear()
        self.backing.ready.clear()
        self.backing.ready_bytes = 0
        self.dependencies.memo.clear()
        self.dependencies.tile_memo.clear()
        self._keys.clear()
        self._status_queue.extend(row for row in range(math.ceil(self.canvas.chapter.height /
            self.canvas._document_projection.tile_size)) if row not in self._status_queue)
        self.canvas._invalidate_scene_cache(projection=False)

    def _reset_status(self):
        self._revision = self.canvas._document_projection.revision
        self._keys.clear()
        self.status.clear()
        count = math.ceil(self.canvas.chapter.height / self.canvas._document_projection.tile_size)
        visible = int(self.canvas.center_y / self.canvas._document_projection.tile_size)
        self._status_queue = deque(sorted(range(count), key=lambda row: abs(row - visible)))

    def row_ready(self, row):
        if self.backing is None or (self._maintenance is not None and self._maintenance[2] == 'clear'):
            return False
        if semantic_color_identity(self.canvas, self.canvas.chapter.pixel_contract) == ('color-resources-pending',):
            return False
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
        deadline = time.perf_counter() + .008
        if self._maintenance is not None:
            future,serial,operation = self._maintenance
            if not future.done():
                return
            self._maintenance = None
            try:
                manifest = future.result()
                if serial == self._serial:
                    self._adopt_manifest(manifest)
                    if operation == 'clear':
                        self._reset_status()
                        self.message = 'Cache cleared'
            except Exception as error:
                if serial == self._serial:
                    self.backing.error = str(error)
                    self.message = 'Cache failed: '+str(error)
            self.refresh_ui()
        for completion in self.scheduler.poll():
            if completion.demand.serial != self._serial:
                continue
            if completion.cache_manifest is not None:
                self._adopt_manifest(completion.cache_manifest)
                self._pending_rows.update(completion.recorded_rows)
            if completion.error:
                self.backing.error = completion.error
            if completion.done:
                self._evaluation_done = completion.recorded and not completion.error
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
        if self.building and self._capture is not None:
            try:
                with self.capture():
                    complete = self._capture.advance(.004)
            except Exception as error:
                self.backing.error = f'{type(error).__name__}: {error}'
                self.cancel()
                self.message = 'Cache failed: ' + self.backing.error
                self.refresh_ui()
                return
            if complete:
                if self._capture.stale or self._capture.result is None:
                    self.backing.error = 'Document changed while capturing the cache'
                else:
                    snapshot = self._capture.result
                    # Recording freezes normal durable visibility even when
                    # the editor currently isolates a selected object or mask.
                    state = dict(snapshot.state,_solo_entities=set(),_solo_suspended=True,
                        _disk_cache_capture=True,_live_underlay_object_id='',_live_underlay_amount=0.)
                    self._snapshot = snapshot = replace(snapshot,state=state)
                    phases = (None,'base','top') if self.canvas._show_on_top_plan().entries else (None,)
                    requests = tuple(request for row in self._work for request in self.requests(row))
                    self.scheduler.submit(SceneDemand(self._serial,snapshot,requests,phases,
                        (snapshot.document.width/2,self.start_y),record=True))
                self._capture = None
        checked = 0
        status_changed = False
        # Queue validation walks committed scene dependencies. Leave its rows
        # queued while the editor owns transient geometry or native ink.
        # Completion/read/maintenance/build handling above still runs.
        status_paused = bool(self.canvas._drawing or self.canvas._projection_has_live_preview())
        while not status_paused and self._status_queue and checked < 8 and time.perf_counter() < deadline:
            row = self._status_queue.popleft()
            ready = self.row_ready(row)
            status_changed |= self.status.get(row) != ready
            self.status[row] = ready
            if ready and row in self._pending_rows:
                self._pending_rows.discard(row)
                try:
                    self._work.remove(row)
                except ValueError:
                    pass
            if not self.status[row] and (self.backing.verifications or self.dependencies.hashes):
                self._status_queue.append(row)
            checked += 1
        if changed:
            queued = set(self._status_queue)
            self._status_queue.extend(row for row in self.status if row not in queued)
        if self.building and self._evaluation_done and not self._work:
            self.building = False
            self._lock(False)
            self.message = 'Cached to disk'
        if changed or status_changed or self.building:
            self.refresh_ui()
        self.timer.setInterval(20 if self.building or self._maintenance is not None or self.scheduler.busy or
            self.backing.pending or self._status_queue or self.dependencies.hashes else 150)

    def clear(self, *, selected):
        if self.backing is None:
            return
        self.cancel()
        backing,serial,rows = self.backing,self._serial,set(self.selected_rows)
        def clear_committed():
            self.scheduler._close_backend()
            # Reload after any canceled writer has committed, avoiding an
            # older GUI index overwriting completed immutable sections.
            committed = PersistentRenderCache(backing.root,contract=backing.contract,environment=backing.environment)
            try:
                committed.clear((lambda entry:entry['kind'] != 'projection' or
                    (len(entry.get('key',())) > 3 and entry['key'][3] in rows)) if selected else None)
                return committed.entries,committed.source_digests
            finally:
                committed.close()
        self._maintenance = self.scheduler.executor.submit(clear_committed),serial,'clear'
        self.status.clear()
        self.message = 'Clearing cache…'
        self.timer.start(20)
        self.refresh_ui()

    def refresh_ui(self):
        panel = self.window.navigator_panel
        panel.cache_button.setEnabled(self.backing is not None and self._maintenance is None)
        panel.cache_button.setText("Cancel" if self.building else "Cache to disk")
        panel.clear_selected_action.setEnabled(self.backing is not None and not self.building and self._maintenance is None)
        panel.clear_all_action.setEnabled(self.backing is not None and not self.building and self._maintenance is None)
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
