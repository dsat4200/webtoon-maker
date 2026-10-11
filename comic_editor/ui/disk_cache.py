"""Native exact RAM warming and optional publication through the render cache."""
from collections import OrderedDict, deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from threading import Event
import math
import time

from PySide6.QtCore import QObject, QEvent, QRectF, QTimer
from PySide6.QtGui import QAction, QImage
from PySide6.QtWidgets import (
    QAbstractButton, QAbstractSpinBox, QComboBox, QLineEdit, QTextEdit,
    QPlainTextEdit, QTreeView, QWidget, QWidgetAction,
)
from comic_editor.render.cache import PersistentRenderCache
from comic_editor.render.resident_cache import ResidentRenderCache
from comic_editor.render.service import RenderPending, RenderFailed
from comic_editor.render.scene import SceneSnapshotCompiler, SceneCapture, DetachedSceneBackend
from comic_editor.render.input_sources import InputSourceSnapshot
from comic_editor.render.scheduler import SceneDemand, SceneScheduler
from comic_editor.ui.cache_dependencies import RenderDependencies
from comic_editor.ui.color_resources import semantic_color_identity


class DiskCacheController(QObject):
    """Historical class name; its ordinary preview tier now lives in RAM."""
    def __init__(self, window):
        super().__init__(window)
        self.window, self.canvas = window, window.canvas
        self.canvas._disk_cache_controller = self
        self.backing = self.dependencies = None
        self.memory = ResidentRenderCache()
        self.contexts = {}
        self.scheduler = SceneScheduler()
        self.cleanup = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cache-retire')
        self.metadata = ThreadPoolExecutor(max_workers=1, thread_name_prefix='cache-metadata')
        self._teardown = [None]
        self._cancel_tokens = set()
        teardown, scheduler, cleanup, metadata = self._teardown, self.scheduler, self.cleanup, self.metadata
        cancel_tokens = self._cancel_tokens
        def destroy_storage():
            for token in tuple(cancel_tokens):
                token.set()
            scheduler.close()
            if teardown[0] is not None:
                try:
                    cleanup.submit(teardown[0].close)
                except RuntimeError:
                    pass
            metadata.shutdown(wait=False, cancel_futures=True)
            cleanup.shutdown(wait=False)
        self.destroyed.connect(destroy_storage)
        self.compiler = SceneSnapshotCompiler()
        self._capture = self._snapshot = self._metadata = None
        self._capture_mode = None
        self._capture_rows = ()
        self._serial = self._binding = 0
        self._maintenance = None
        self._export = self._export_capture = None
        self._export_cancel = Event()
        self._pending_rows = set()
        self._evaluation_done = self.building = False
        self.start_y = self.end_y = 0.
        self.status = {}
        self.observed = OrderedDict()
        self._keys = {}
        self._status_queue = deque()
        self._work = deque()
        self._warm = deque()
        self._disabled = []
        self.message = ''
        self._revision = -1
        self.timer = QTimer(self)
        self.timer.setInterval(20)
        self.timer.timeout.connect(self.tick)
        window.preview.disk_cache = self
        panel = window.navigator_panel
        panel.cache_button.clicked.connect(self.toggle)
        panel.clear_selected_action.triggered.connect(lambda: self.clear(selected=True))
        panel.clear_all_action.triggered.connect(lambda: self.clear(selected=False))
        panel.clear_saved_action.triggered.connect(lambda: self.clear_saved(selected=False))
        self.refresh_ui()

    def bind(self):
        self.detach()
        chapter, repository, session = self.canvas.chapter, self.window.repository, self.window.active_session
        if chapter is None or repository is None or (session is not None and session.kind != 'series'):
            self.refresh_ui()
            return
        root = repository.chapter_root(chapter.chapter_id) / '.render-cache'
        view = self.memory.view(str(root.resolve()))
        previous = self.contexts.get(view.namespace)
        if previous is None or previous['tiles'] is not self.canvas.tiles:
            # Runtime versions may restart at zero after loading new source owners.
            view.clear_runtime()
        self.contexts[view.namespace] = dict(root=root, project=str(repository.root.resolve()),
            tiles=self.canvas.tiles, images=self.canvas.images, view=view,
            contract=chapter.pixel_contract.signature,
            environment=(*RenderDependencies.environment(), semantic_color_identity(self.canvas, chapter.pixel_contract)))
        self.backing = PersistentRenderCache(root, contract=chapter.pixel_contract.signature,
            environment=self.contexts[view.namespace]['environment'], resident=view)
        self._teardown[0] = self.backing
        self.dependencies = RenderDependencies(self.canvas, self.backing)
        self.canvas._persistent_render_cache, self.canvas._render_dependencies = self.backing, self.dependencies
        self.canvas.tiles.render_fingerprint = self.dependencies.tiles
        self.canvas.images.render_fingerprint = self.dependencies.image
        projection = self.canvas._document_projection
        projection.backing_lookup, projection.backing_retain = self.lookup_tile, self.retain_tile
        saved = self.window.settings.document_cache_ranges.get(self.window._document_view_key())
        self.start_y, self.end_y = saved or (0., float(chapter.height))
        self.set_range(self.start_y, self.end_y, save=False)
        self._reset_status()
        if self.backing.entries or view.bytes:
            self._begin_capture('restore', range(math.ceil(chapter.height / projection.tile_size)))
            self.message = 'Loading saved preview into memory…' if self.backing.entries else ''
        self.timer.start()
        self.refresh_ui()

    def detach(self):
        self.cancel()
        self._cancel_metadata()
        if self._export is None:
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
        self._binding += 1
        self._serial += 1
        self._capture = self._snapshot = self._metadata = None
        self._revision = -1
        self.message = ''

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
        previous = getattr(self.canvas, '_disk_cache_capture', False)
        self.canvas._disk_cache_capture = True
        try:
            with self.canvas.without_solo():
                yield
        finally:
            self.canvas._disk_cache_capture = previous

    def requests(self, row):
        chapter, projection = self.canvas.chapter, self.canvas._document_projection
        return projection.requests(QRectF(0, row * projection.tile_size, chapter.width,
            min(projection.tile_size, chapter.height - row * projection.tile_size)), 1.)

    def _marker(self, request, configuration):
        contract = self.canvas.chapter.pixel_contract
        return (self.canvas._document_projection.revision, request.address, tuple(configuration[3:]),
                contract.signature, semantic_color_identity(self.canvas, contract))

    def tile_key(self, request, configuration):
        key = self._keys.get(self._marker(request, configuration))
        if key is None:
            raise RenderPending('Preparing memory cache metadata')
        return key

    def reusable(self, configuration):
        return (self.backing is not None
            and semantic_color_identity(self.canvas, self.canvas.chapter.pixel_contract) != ('color-resources-pending',)
            and not self.canvas._projection_has_live_preview()
            and configuration[7] == ('', 0.) and not configuration[8] and configuration[9] is None)

    def lookup_memory_tile(self, request, configuration):
        if not self.reusable(configuration):
            return None
        key = self._keys.get(self._marker(request, configuration))
        return self.backing.resident.lookup(self.backing.descriptor('projection', key)) if key is not None else None

    def lookup_tile(self, request, configuration):
        # Cold saved values are resolved by the ordinary detached backend. The
        # canvas never performs filesystem IO or dependency graph walks here.
        return self.lookup_memory_tile(request, configuration)

    def retain_tile(self, request, configuration, image):
        if self.reusable(configuration):
            key = self._keys.get(self._marker(request, configuration))
            if key is not None:
                self.backing.retain('projection', key, image)

    def adopt_memory_keys(self, completion, document):
        if (self.backing is None or document.identity != (id(self.canvas.chapter), id(self.canvas.tiles), id(self.canvas.images))
                or document.revision != self.canvas._document_projection.revision or document.live_preview):
            return
        self.backing.contract = document.pixel_contract.signature
        from comic_editor.ui.color_resources import metadata_color_environment
        environment = metadata_color_environment(self.canvas, document.pixel_contract)
        if environment is None:
            return
        self.backing.environment = (*RenderDependencies.environment(), environment.signature)
        for address, phase, key in completion.cache_keys:
            marker = (document.revision, address, tuple((*document.configuration, phase)[3:]),
                      self.backing.contract, environment.signature)
            self._keys[marker] = key
            if address.y not in self._status_queue:
                self._status_queue.append(address.y)

    def observe(self, kind, key, state):
        # Kept for existing exact cache observation hooks, never a pixel owner.
        return

    def toggle(self):
        self.cancel() if self.building else self.start()

    def _begin_capture(self, mode, rows):
        self.compiler.invalidate()
        with self.capture():
            self._capture = self.compiler.capture(self.canvas, self.canvas._render_document_state())
        self._capture_mode, self._capture_rows = mode, tuple(rows)
        self.timer.start(20)

    def start(self):
        if self.backing is None or self.building or self._maintenance is not None or self._export is not None:
            return False
        if (self.canvas._projection_has_live_preview() or self.canvas._drawing or self.canvas._fill_job_cancel is not None
                or getattr(self.canvas, '_fill_async_gesture', None) is not None):
            self.message = 'Finish the current edit first'
            self.refresh_ui()
            return False
        self._serial += 1
        self.backing.error = ''
        self._work = deque(self.selected_rows)
        self._pending_rows.clear()
        self._evaluation_done = False
        self._cancel_metadata()
        self.building = True
        self._lock(True)
        self._begin_capture('warm', self._work)
        self.message = 'Caching to memory… edits paused'
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
        if not self.building:
            return
        self.building = False
        self._serial += 1
        self._work.clear()
        self._capture = self._snapshot = None
        self.scheduler.cancel()
        self._lock(False)
        self.message = 'Canceled; completed memory sections kept'
        self.refresh_ui()

    def _cancel_metadata(self):
        if self._metadata is not None:
            self._metadata[3].set()
            self._metadata[0].cancel()
            self._metadata = None

    def _reset_status(self):
        self._cancel_metadata()
        self._revision = self.canvas._document_projection.revision
        self._keys.clear()
        self.status.clear()
        count = math.ceil(self.canvas.chapter.height / self.canvas._document_projection.tile_size)
        visible = int(self.canvas.center_y / self.canvas._document_projection.tile_size)
        self._status_queue = deque(sorted(range(count), key=lambda row: abs(row - visible)))

    def row_ready(self, row):
        if self.backing is None or self.canvas.chapter is None or not self._keys:
            return False
        if semantic_color_identity(self.canvas, self.canvas.chapter.pixel_contract) == ('color-resources-pending',):
            return False
        with self.capture():
            configuration = self.canvas._projection_configuration()
            phases = (None, 'base', 'top') if self.canvas._show_on_top_plan().entries else (None,)
        if not self.reusable((*configuration, None)):
            return False
        for phase in phases:
            for request in self.requests(row):
                key = self._keys.get(self._marker(request, (*configuration, phase)))
                if key is None or not self.backing.resident.has(self.backing.descriptor('projection', key)):
                    return False
        return True

    @staticmethod
    def _load_memory(snapshot, requests, phases, cancelled):
        if cancelled.is_set():
            return ()
        backend = DetachedSceneBackend(snapshot)
        result = []
        try:
            scene, backing = backend.scene, backend.scene._persistent_render_cache
            for phase in phases:
                if cancelled.is_set():
                    return tuple(result)
                keys = scene._render_dependencies.projection_keys(requests, (*snapshot.document.configuration, phase))
                for request, key in zip(requests, keys):
                    if cancelled.is_set():
                        return tuple(result)
                    backing.lookup('projection', key, wait=True)
                    result.append((request.address, phase, key))
            return tuple(result)
        finally:
            backend.close()

    def _advance_capture(self):
        if self._capture is None:
            return
        with self.capture():
            complete = self._capture.advance(.004)
        if not complete:
            return
        capture, self._capture = self._capture, None
        if capture.stale or capture.result is None:
            if self.building:
                self.cancel()
                self.message = 'Document changed while preparing memory cache'
            return
        snapshot = replace(capture.result, state=dict(capture.result.state, _solo_entities=set(),
            _solo_suspended=True, _disk_cache_capture=True, _live_underlay_object_id='', _live_underlay_amount=0.))
        self._snapshot = snapshot
        phases = (None, 'base', 'top') if self.canvas._show_on_top_plan().entries else (None,)
        requests = tuple(request for row in self._capture_rows for request in self.requests(row))
        if self._capture_mode == 'warm':
            self.memory.eligible.add(self.backing.resident.namespace)
            self.scheduler.submit(SceneDemand(self._serial, snapshot, requests, phases,
                (snapshot.document.width / 2, self.canvas.center_y), consumer='cache-memory'))
        else:
            cancel = Event()
            self._cancel_tokens.add(cancel)
            future = self.metadata.submit(self._load_memory, snapshot, requests, phases, cancel)
            future.add_done_callback(lambda _future: self._cancel_tokens.discard(cancel))
            self._metadata = future, self._binding, snapshot.document, cancel

    def tick(self):
        self._advance_export()
        if self.backing is None:
            return
        deadline = time.perf_counter() + .008
        try:
            self._advance_capture()
            if self._metadata is not None and self._metadata[0].done():
                future, binding, document, _ = self._metadata
                self._metadata = None
                keys = future.result()
                if binding == self._binding:
                    from types import SimpleNamespace
                    self.adopt_memory_keys(SimpleNamespace(cache_keys=keys), document)
                    if self.backing.resident.bytes:
                        self.memory.eligible.add(self.backing.resident.namespace)
                    self.message = 'Saved preview loaded into memory' if any(self.status.values()) or keys else ''
                    self.canvas.update()
            for completion in self.scheduler.poll():
                if completion.demand.serial != self._serial:
                    continue
                self.adopt_memory_keys(completion, completion.demand.snapshot.document)
                self.canvas.update()
                if completion.error:
                    raise RuntimeError(completion.error)
                if completion.done:
                    self._evaluation_done = True
            self.backing.poll()
            if self._maintenance is not None and self._maintenance[0].done():
                future, serial, _ = self._maintenance
                self._maintenance = None
                future.result()
                if serial == self._serial:
                    self.message = 'Saved disk cache cleared'
            if self._revision != self.canvas._document_projection.revision:
                self._reset_status()
                if (not self.building and not self.canvas._projection_has_live_preview() and self._export is None
                        and (self.backing.resident.bytes or self.backing.entries)):
                    self._begin_capture('restore', range(math.ceil(self.canvas.chapter.height / self.canvas._document_projection.tile_size)))
            paused = self.canvas._drawing or self.canvas._projection_has_live_preview()
            while not paused and self._status_queue and time.perf_counter() < deadline:
                row = self._status_queue.popleft()
                self.status[row] = self.row_ready(row)
                if self.status[row]:
                    try:
                        self._work.remove(row)
                    except ValueError:
                        pass
            if self.building and self._evaluation_done and not self._status_queue:
                self.building = False
                self._lock(False)
                self.message = 'Cached to memory' if not self._work else 'Memory cache full; completed sections kept'
                self._work.clear()
        except Exception as error:
            self.backing.error = str(error)
            self.cancel()
            self.message = 'Cache failed: ' + str(error)
        self.refresh_ui()
        self.timer.setInterval(20 if self.building or self._capture is not None or self._metadata is not None
            or self._maintenance is not None or self._export is not None or self._status_queue else 150)

    def clear(self, *, selected):
        if self.backing is None:
            return
        self.cancel()
        self._cancel_metadata()
        self._capture = None
        rows = set(self.selected_rows)
        self.backing.resident.clear((lambda entry: entry.descriptor.kind != 'projection'
            or entry.descriptor.key[3] in rows) if selected else None)
        self.canvas._document_projection.clear()
        self.status.clear()
        self._status_queue.extend(range(math.ceil(self.canvas.chapter.height / self.canvas._document_projection.tile_size)))
        self.message = 'Memory cache cleared'
        self.canvas.update()
        self.refresh_ui()

    def clear_saved(self, *, selected=False):
        if self.backing is None or self._maintenance is not None:
            return
        root, contract, environment = self.backing.root, self.backing.contract, self.backing.environment
        rows, serial = set(self.selected_rows), self._serial
        def clear_disk():
            backing = PersistentRenderCache(root, contract=contract, environment=environment)
            try:
                backing.clear((lambda entry: entry['kind'] != 'projection' or entry['key'][3] in rows) if selected else None)
            finally:
                backing.close()
        self._maintenance = self.metadata.submit(clear_disk), serial, 'clear-saved'
        self.message = 'Clearing saved disk cache…'
        self.timer.start(20)
        self.refresh_ui()

    def _project_contexts(self, session):
        project = str(session.context.repository.root.resolve())
        return [context for namespace, context in self.contexts.items() if context['project'] == project
            and namespace in self.memory.eligible and context['view'].bytes]

    def can_persist(self, session):
        return bool(self._project_contexts(session))

    def persist_for_close(self, session, callback):
        if self._export is not None:
            return False
        contexts = self._project_contexts(session)
        if not contexts:
            return False
        self.cancel()
        self._cancel_metadata()
        self._capture = None
        self._export_cancel = Event()
        self._cancel_tokens.add(self._export_cancel)
        self._export = dict(callback=callback, future=None, contexts=contexts)
        self._export_capture = self._capture_export_sources(contexts)
        self.timer.start(20)
        return True

    def _capture_export_sources(self, contexts):
        # Save may adopt encoded files without changing runtime pixel versions.
        # Seal those aliases with the existing source fingerprint routine before
        # exporting resident values, including chapters in inactive tabs.
        compiler = SceneSnapshotCompiler()
        carrier = SceneCapture(compiler, self.canvas, None)
        sources = []
        for context in contexts:
            tiles, jobs, _ = yield from carrier._capture_tiles(context['tiles'], 'document')
            source_images = context['images']
            images = source_images.clone()
            # Mirror ordinary SceneCapture ownership, including native decoded
            # sources that have not yet adopted an encoded payload. These are
            # bounded COW handles; source decoding stays on the worker.
            for key, image in source_images._decoded.items():
                alias = QImage(image)
                images._cache_decoded(key, alias)
                images._adopt_owned_decode(key, alias, source_images._owned_decodes.get(key))
                yield
            sources.append((context, InputSourceSnapshot(tiles, tuple(jobs)), images, context['view'].snapshot()))
        return sources

    @staticmethod
    def _publish_memory(sources, cancelled):
        for context, source, images, entries in sources:
            if cancelled.is_set():
                return False, ''
            source.finish_sources()
            view = context['view']
            # Share only source metadata here. Validate/write via the existing
            # disk backing, rather than treating resident hits as disk success.
            backing = PersistentRenderCache(context['root'], contract=context['contract'], environment=context['environment'])
            publication_epoch = backing.epoch, backing._root_epoch
            backing.source_digests.update(view.source_digests())
            class SourceAliases:
                remember_source = lambda self, logical, record: view.remember_source(logical, record)
                pixel_identity = lambda self, identifier, address, version, path: (
                    previous if (previous := view.pixel_identity(identifier, address)) is not None and previous[0] == version else None)
                remember_pixel_identity = lambda self, *arguments: view.remember_pixel_identity(*arguments)
            # Fingerprint calculations use only immutable native source owners.
            aliases = SourceAliases()
            aliases.executor, aliases.source_digests = backing.executor, backing.source_digests
            dependencies = RenderDependencies(None, aliases)
            try:
                for identifier in source.tiles._tiles:
                    dependencies.tiles(source.tiles, identifier)
                    if cancelled.is_set():
                        return False, ''
                for identifier in images._sources:
                    dependencies.image(images, identifier)
                # Retaining values can replace the backing's source map with a
                # newly committed manifest. Keep the aliases sealed against
                # this immutable source capture for final metadata publication.
                source_records = dict(backing.source_digests)
                with backing.record():
                    for entry in entries:
                        if cancelled.is_set():
                            break
                        descriptor = entry.descriptor
                        backing.contract, backing.environment = descriptor.contract, descriptor.environment
                        # A damaged old blob must not suppress the known exact
                        # resident value during optional publication.
                        try:
                            existing = backing.lookup(descriptor.kind, descriptor.key, wait=True)
                        except (RenderFailed, ValueError, OSError):
                            existing = None
                        if existing is None:
                            if not backing.retain(descriptor.kind, descriptor.key, entry.value, state=entry.state):
                                backing.drain()
                                if not backing.retain(descriptor.kind, descriptor.key, entry.value, state=entry.state):
                                    raise RuntimeError(backing.error or 'Unable to save an exact cache value')
                backing.drain()
                if backing.error:
                    raise RuntimeError(backing.error)
                if cancelled.is_set():
                    return False, ''
                # Existing blobs need no rewrite, but Save may have adopted a
                # different encoding of their unchanged native source pixels.
                # Publish those seals even when no value was newly retained,
                # using the same merge/epoch guard as ordinary cache writes.
                manifest = backing._publish_index({}, source_records, publication_epoch)
                if manifest is None:
                    raise RuntimeError('Saved cache changed while its preview was being written')
                backing.entries, backing.source_digests = manifest
            finally:
                backing.close()
        return (False, '') if cancelled.is_set() else (True, '')

    def _advance_export(self):
        if self._export is None:
            return
        callback = self._export['callback']
        try:
            if self._export_capture is not None:
                deadline = time.perf_counter() + .004
                while time.perf_counter() < deadline:
                    if self._export_cancel.is_set():
                        self._export_capture = None
                        self._export = None
                        self._cancel_tokens.discard(self._export_cancel)
                        callback(False, '')
                        return
                    try:
                        next(self._export_capture)
                    except StopIteration as result:
                        self._export_capture = None
                        self._export['future'] = self.metadata.submit(self._publish_memory, result.value, self._export_cancel)
                        break
            future = self._export['future']
            if future is not None and future.done():
                self._export = None
                self._cancel_tokens.discard(self._export_cancel)
                callback(*future.result())
        except Exception as error:
            self._export_capture = self._export = None
            self._cancel_tokens.discard(self._export_cancel)
            callback(False, str(error))

    def cancel_persistence(self):
        self._export_cancel.set()

    def release_project(self, session):
        project = str(session.context.repository.root.resolve())
        if self.backing is not None and str(self.window.repository.root.resolve()) == project:
            self._cancel_metadata()
            self._capture = None
        for namespace, context in list(self.contexts.items()):
            if context['project'] == project:
                context['view'].retire()
                del self.contexts[namespace]

    def refresh_ui(self):
        panel = self.window.navigator_panel
        available = self.backing is not None and self._export is None
        panel.cache_button.setEnabled(available and self._maintenance is None)
        panel.cache_button.setText('Cancel' if self.building else 'Cache to memory')
        for action in (panel.clear_selected_action, panel.clear_all_action, panel.clear_saved_action):
            action.setEnabled(available and not self.building and self._maintenance is None)
        size = self.backing.resident.bytes / (1024 * 1024) if self.backing else 0
        panel.cache_status.setText(f'{size:.1f} MB')
        panel.cache_status.setEnabled(available and not self.building)
        ready = sum(self.status.get(row, False) for row in self.selected_rows)
        panel.cache_progress.setText(f'{ready} / {len(self.selected_rows)}' if self.building else
            ('Failed' if self.message.startswith('Cache failed:') else f'{len(self.selected_rows)} sections' if self.backing else ''))
        text = self.message or 'Drag the white handles to choose a full-width section. Green: ready in memory. Red: missing or changed.'
        if self.building:
            text = f'Caching {ready} / {len(self.selected_rows)} sections to memory. Editing is paused.'
        panel.cache_button.setToolTip(text)
        panel.cache_status.setToolTip(text + f' Memory budget: {self.memory.budget / 1024**3:.1f} GB across open projects.')
        self.window.preview.update()
